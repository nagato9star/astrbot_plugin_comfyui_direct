"""WebUI API：配方工作台（工作流导入 + 节点下拉映射 + 配方/历史）。"""

from __future__ import annotations

import base64
import json
import random
import uuid
from pathlib import Path
from typing import Any

from astrbot.api import logger

from comfy_client import ComfyUIClient
from recipe_store import RecipeStore, materialize_values
from slot_mapping import (
    SLOT_BASIC,
    SLOT_HELP,
    SLOT_ROLES,
    apply_slots,
    collect_trigger_words,
    detect_slots,
    is_ui_workflow,
    list_nodes,
    looks_like_anima,
    merge_slots,
    node_options_for_slot,
    normalize_workflow,
    parse_node_option,
    read_current_values,
    resolve_size,
    slots_from_config,
    ANIMA_DROP_NODES,
)
from workflow_builder import WorkflowBuilder

PLUGIN_NAME = "astrbot_plugin_comfyui_direct"

SAMPLER_NAMES = [
    "er_sde",
    "euler",
    "euler_ancestral",
    "dpmpp_2m",
    "dpmpp_2m_sde",
    "dpmpp_3m_sde",
    "dpmpp_sde",
    "ddim",
    "uni_pc",
    "lcm",
]
SCHEDULERS = ["normal", "karras", "exponential", "sgm_uniform", "simple", "beta"]


def _json(data: dict, status: int = 200):
    try:
        from astrbot.api.web import json_response

        return json_response(data, status_code=status)
    except Exception:
        from quart import jsonify

        resp = jsonify(data)
        resp.status_code = status
        return resp


async def _body() -> dict:
    try:
        from astrbot.api.web import request as web_request

        payload = await web_request.json(default={})
        return payload if isinstance(payload, dict) else {}
    except Exception:
        from quart import request

        return await request.get_json(force=True, silent=True) or {}


def _query(key: str, default: str = "") -> str:
    try:
        from astrbot.api.web import request as web_request

        val = web_request.query.get(key, default)
        return str(val if val is not None else default)
    except Exception:
        from quart import request

        return str(request.args.get(key) or default)


class StudioApi:
    def __init__(
        self,
        client: ComfyUIClient,
        builder: WorkflowBuilder,
        store: RecipeStore,
        output_dir: Path,
        shared: dict,
        draw_tool: Any = None,
    ) -> None:
        self.client = client
        self.builder = builder
        self.store = store
        self.output_dir = output_dir
        self.shared = shared
        self.draw_tool = draw_tool

    def _refresh_draw_schema(self) -> None:
        if self.draw_tool is not None and hasattr(self.draw_tool, "refresh_schema"):
            self.draw_tool.refresh_schema()

    async def status(self) -> Any:
        connected = await self.client.ping(timeout=5.0)
        force = _query("refresh") == "1"
        resources, from_cache = {}, False
        system_stats = None
        if connected:
            try:
                resources, from_cache = await self.client.list_resources(force_refresh=force)
                system_stats = await self.client.get_system_stats()
            except Exception as e:
                logger.warning(f"[ComfyUIDirect] 状态接口取资源失败: {e}")
        return _json(
            {
                "ok": True,
                "connected": connected,
                "base_url": self.client.base_url,
                "from_cache": from_cache,
                "resources": resources,
                "system_stats": system_stats,
                "sampler_names": SAMPLER_NAMES,
                "schedulers": SCHEDULERS,
                "default_workflow": self.builder.default_workflow,
                "slot_roles": [
                    {
                        "id": r,
                        "label": lab,
                        "help": SLOT_HELP.get(r, ""),
                        "basic": r in SLOT_BASIC,
                    }
                    for r, lab in SLOT_ROLES
                ],
            }
        )

    async def list_workflows(self) -> Any:
        return _json({"ok": True, "templates": self.builder.list_templates()})

    def _workflow_payload(self, name: str, wf: dict) -> dict:
        detected = detect_slots(wf)
        return {
            "ok": True,
            "name": name,
            "source": self.builder.source_of(self.builder.resolve_workflow_path(name)) or "custom",
            "workflow": wf,
            "nodes": list_nodes(wf),
            "detected_slots": detected,
            "slot_options": {
                role: node_options_for_slot(wf, role, str((detected.get(role) or {}).get("node") or ""))
                for role, _ in SLOT_ROLES
            },
        }

    async def get_workflow(self) -> Any:
        name = _query("name").strip()
        if not name:
            return _json({"ok": False, "error": "缺少 name 参数"})
        try:
            wf = self.builder.load_template(name)
        except FileNotFoundError as e:
            return _json({"ok": False, "error": str(e)})
        return _json(self._workflow_payload(name, wf))

    async def import_workflow(self) -> Any:
        body = await _body()
        name = str(body.get("name") or "").strip()
        raw = body.get("workflow")
        if not name:
            return _json({"ok": False, "error": "需要 name"})
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError as e:
                return _json({"ok": False, "error": f"JSON 解析失败: {e}"})
        if not isinstance(raw, dict):
            return _json({"ok": False, "error": "需要 workflow JSON"})
        object_info = None
        if is_ui_workflow(raw):
            object_info = await self.client.get_object_info()
            if not object_info:
                return _json(
                    {
                        "ok": False,
                        "error": "这是 ComfyUI 前端格式。请先连上 ComfyUI，或改用 Save (API Format)。",
                    }
                )
        try:
            wf = normalize_workflow(raw, object_info)
        except ValueError as e:
            return _json({"ok": False, "error": str(e)})
        try:
            self.builder.save_template(name, wf)
        except ValueError as e:
            return _json({"ok": False, "error": str(e)})
        payload = self._workflow_payload(name, wf)
        if not self.store.list():
            self.store.bootstrap(workflow_name=name, wf=wf)
            self._refresh_draw_schema()
        return _json(payload)

    async def import_from_history(self) -> Any:
        body = await _body()
        prompt_id = str(body.get("prompt_id") or "").strip()
        name = str(body.get("name") or "").strip()
        rows = await self.client.list_history(24)
        entry = next((r for r in rows if r.get("prompt_id") == prompt_id), None) if prompt_id else (rows[0] if rows else None)
        if not entry or not entry.get("workflow"):
            return _json({"ok": False, "error": "ComfyUI 历史里没有可导入的工作流"})
        name = name or f"history-{str(entry['prompt_id'])[:8]}"
        try:
            wf = normalize_workflow(entry["workflow"])
            self.builder.save_template(name, wf)
        except ValueError as e:
            return _json({"ok": False, "error": str(e)})
        return _json(self._workflow_payload(name, wf))

    async def comfy_history(self) -> Any:
        rows = await self.client.list_history(12)
        slim = [
            {
                "prompt_id": r.get("prompt_id"),
                "number": r.get("number"),
                "status": r.get("status"),
                "has_workflow": r.get("has_workflow"),
            }
            for r in rows
        ]
        return _json({"ok": True, "items": slim})

    async def delete_workflow(self) -> Any:
        body = await _body()
        name = str(body.get("name") or "").strip()
        if not name:
            return _json({"ok": False, "error": "缺少 name"})
        try:
            self.builder.delete_template(name)
        except ValueError as e:
            return _json({"ok": False, "error": str(e)})
        return _json({"ok": True, "name": name})

    async def detect(self) -> Any:
        body = await _body()
        name = str(body.get("name") or _query("name")).strip()
        if not name:
            return _json({"ok": False, "error": "缺少 name"})
        try:
            wf = self.builder.load_template(name)
        except FileNotFoundError as e:
            return _json({"ok": False, "error": str(e)})
        detected = detect_slots(wf)
        return _json(
            {
                "ok": True,
                "slots": detected,
                "values": read_current_values(wf, detected),
                "anima": looks_like_anima(wf),
                "slot_options": {
                    role: node_options_for_slot(wf, role, str((detected.get(role) or {}).get("node") or ""))
                    for role, _ in SLOT_ROLES
                },
            }
        )

    async def list_recipes(self) -> Any:
        return _json({"ok": True, "recipes": self.store.list()})

    async def get_recipe(self) -> Any:
        name = _query("name").strip()
        recipe = self.store.get(name) if name else self.store.default()
        if recipe is None:
            return _json({"ok": False, "error": "配方不存在"})
        wf = None
        slot_options = {}
        try:
            if recipe.get("workflow"):
                wf = self.builder.load_template(recipe["workflow"])
                slot_options = {
                    role: node_options_for_slot(
                        wf, role, str((recipe.get("slots") or {}).get(role, {}).get("node") or "")
                    )
                    for role, _ in SLOT_ROLES
                }
        except FileNotFoundError:
            pass
        return _json({"ok": True, "recipe": recipe, "slot_options": slot_options, "nodes": list_nodes(wf) if wf else []})

    async def save_recipe(self) -> Any:
        body = await _body()
        slots = body.get("slots")
        if isinstance(slots, dict):
            normalized = {}
            for role, spec in slots.items():
                if isinstance(spec, str):
                    nid = parse_node_option(spec)
                    if nid:
                        normalized[role] = {"node": nid}
                elif isinstance(spec, dict) and spec.get("node"):
                    spec = dict(spec)
                    spec["node"] = parse_node_option(spec["node"])
                    normalized[role] = spec
            body["slots"] = normalized
        if body.get("from_config"):
            body["slots"] = merge_slots(body.get("slots") or {}, slots_from_config(body.get("node_slots")))
        try:
            saved = self.store.save(body)
        except ValueError as e:
            return _json({"ok": False, "error": str(e)})
        self._refresh_draw_schema()
        return _json({"ok": True, "recipe": saved})

    async def delete_recipe(self) -> Any:
        body = await _body()
        name = str(body.get("name") or "").strip()
        if not self.store.delete(name):
            return _json({"ok": False, "error": "配方不存在"})
        self._refresh_draw_schema()
        return _json({"ok": True, "name": name})

    async def generate(self) -> Any:
        body = await _body()
        prompt = str(body.get("prompt") or "").strip()
        if not prompt:
            return _json({"ok": False, "error": "prompt 不能为空"})
        recipe_name = str(body.get("recipe") or "").strip()
        recipe = self.store.get(recipe_name) if recipe_name else self.store.default()
        if recipe is None:
            return _json({"ok": False, "error": "还没有配方，请先导入工作流并保存配方"})
        slots = recipe.get("slots") or {}
        if not slots.get("prompt"):
            return _json({"ok": False, "error": "配方未指定主提示词节点"})

        seed = body.get("seed")
        if seed is None:
            seed = random.randint(0, 2**31 - 1)
        defaults = recipe.get("defaults") or {}
        loras = body.get("loras") or body.get("lora")
        trigger = body.get("trigger_words")
        if not trigger:
            if loras:
                try:
                    resources, _ = await self.client.list_resources()
                    trigger = collect_trigger_words(resources.get("lora_meta") or {}, loras) or None
                except Exception:
                    trigger = None
            elif not defaults.get("trigger_words"):
                try:
                    resources, _ = await self.client.list_resources()
                    trigger = collect_trigger_words(resources.get("lora_meta") or {}, defaults.get("loras")) or None
                except Exception:
                    trigger = None
        overrides = {
            "prompt": prompt,
            "seed": seed,
            "artist": body.get("artist"),
            "model": body.get("model"),
            "loras": loras,
            "width": body.get("width"),
            "height": body.get("height"),
            "steps": body.get("steps"),
            "cfg": body.get("cfg"),
            "sampler_name": body.get("sampler_name"),
            "scheduler": body.get("scheduler"),
            "denoise": body.get("denoise"),
            "trigger_words": trigger,
        }
        values = materialize_values(recipe, overrides)
        values["seed"] = int(seed)
        if body.get("size"):
            values["width"], values["height"] = resolve_size(
                int(values["width"]) if values.get("width") else None,
                int(values["height"]) if values.get("height") else None,
                str(body.get("size")),
            )
        try:
            wf = self.builder.load_template(recipe.get("workflow") or None)
        except FileNotFoundError as e:
            return _json({"ok": False, "error": str(e)})
        apply_slots(
            wf,
            slots,
            values,
            prefix=f"astrbot_{uuid.uuid4().hex[:8]}",
            drop_nodes=list(recipe.get("drop_nodes") or (ANIMA_DROP_NODES if looks_like_anima(wf) else [])),
        )
        pid, err = await self.client.submit_prompt_detail(wf)
        if err:
            return _json({"ok": False, "error": err})
        self.shared["last_prompt_id"] = pid
        self.shared["pending_values"] = {
            k: values.get(k)
            for k in ("model", "loras", "width", "height", "steps", "cfg", "sampler_name", "scheduler", "denoise", "trigger_words", "seed")
        }
        self.shared["pending_recipe"] = recipe
        self.shared["pending_prompt"] = prompt
        return _json({"ok": True, "prompt_id": pid})

    async def generate_poll(self) -> Any:
        pid = _query("pid").strip()
        if not pid:
            return _json({"ok": False, "error": "缺少 pid"})
        entry = await self.client.get_history_entry(pid)
        if entry is None:
            return _json({"ok": True, "done": False})
        st = entry.get("status") or {}
        if st.get("status_str") == "error":
            return _json({"ok": True, "done": True, "error": st.get("message") or "执行出错"})
        images = []
        for node_out in (entry.get("outputs") or {}).values():
            images.extend(node_out.get("images") or [])
        if not images:
            return _json({"ok": True, "done": True, "error": "执行完成但无输出图片"})
        img = images[0]
        content = await self.client.download_image(
            img["filename"], img.get("subfolder", ""), preview="webp;80"
        )
        if not content:
            return _json({"ok": True, "done": True, "error": f"图片下载失败: {img['filename']}"})
        local_path = self.output_dir / img["filename"]
        try:
            local_path.write_bytes(content)
        except OSError:
            pass
        recipe = self.shared.get("pending_recipe") or {}
        values = self.shared.get("pending_values") or {}
        self.store.save_history(
            {
                "prompt_id": pid,
                "recipe": recipe.get("name"),
                "recipe_id": recipe.get("id"),
                "workflow": recipe.get("workflow"),
                "slots": recipe.get("slots") or {},
                "drop_nodes": recipe.get("drop_nodes") or [],
                "prompt": self.shared.get("pending_prompt") or "",
                "values": {k: v for k, v in values.items() if v not in (None, "", [])},
                "filename": img["filename"],
                "local_path": str(local_path),
            }
        )
        return _json(
            {
                "ok": True,
                "done": True,
                "filename": img["filename"],
                "data_url": "data:image/webp;base64," + base64.b64encode(content).decode("ascii"),
            }
        )

    async def interrupt(self) -> Any:
        body = await _body()
        pid = str(body.get("prompt_id") or "").strip()
        ok = await self.client.interrupt(prompt_id=pid or None)
        return _json({"ok": ok, "prompt_id": pid or None})

    async def list_history(self) -> Any:
        return _json({"ok": True, "items": self.store.list_history(20)})

    async def recipe_from_history(self) -> Any:
        body = await _body()
        pid = str(body.get("prompt_id") or "").strip()
        name = str(body.get("name") or "").strip()
        if not pid or not name:
            return _json({"ok": False, "error": "需要 prompt_id 与 name"})
        try:
            saved = self.store.recipe_from_history(pid, name, str(body.get("description") or ""))
        except ValueError as e:
            return _json({"ok": False, "error": str(e)})
        self._refresh_draw_schema()
        return _json({"ok": True, "recipe": saved})


def register_web_apis(
    context,
    client: ComfyUIClient,
    builder: WorkflowBuilder,
    store: RecipeStore | None = None,
    output_dir: Path | None = None,
    shared: dict | None = None,
    draw_tool: Any = None,
) -> None:
    if store is None or output_dir is None:
        logger.error("[ComfyUIDirect] WebUI 缺少 recipe store，跳过注册")
        return
    api = StudioApi(client, builder, store, output_dir, shared or {}, draw_tool)
    routes = [
        ("/status", api.status, ["GET"], "ComfyUI 状态"),
        ("/workflows", api.list_workflows, ["GET"], "列出工作流"),
        ("/workflow", api.get_workflow, ["GET"], "获取工作流"),
        ("/workflow/import", api.import_workflow, ["POST"], "导入工作流"),
        ("/workflow/import-history", api.import_from_history, ["POST"], "从 ComfyUI 历史导入"),
        ("/workflow/delete", api.delete_workflow, ["POST"], "删除工作流"),
        ("/workflow/detect", api.detect, ["POST"], "检测槽位"),
        ("/comfy-history", api.comfy_history, ["GET"], "ComfyUI 历史"),
        ("/recipes", api.list_recipes, ["GET"], "列出配方"),
        ("/recipe", api.get_recipe, ["GET"], "获取配方"),
        ("/recipe/save", api.save_recipe, ["POST"], "保存配方"),
        ("/recipe/delete", api.delete_recipe, ["POST"], "删除配方"),
        ("/recipe/from-history", api.recipe_from_history, ["POST"], "历史存成配方"),
        ("/generate", api.generate, ["POST"], "按配方试跑"),
        ("/generate", api.generate_poll, ["GET"], "轮询试跑结果"),
        ("/generate/interrupt", api.interrupt, ["POST"], "中断试跑"),
        ("/history", api.list_history, ["GET"], "插件生成历史"),
    ]
    for path, handler, methods, desc in routes:
        context.register_web_api(f"/{PLUGIN_NAME}{path}", handler, methods, desc)
