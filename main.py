"""ComfyUI Direct — 局域网直连 ComfyUI API，提供图片生成与模型查询.

2026-08-11 v2.0.0 重构：
- 全部 HTTP 改 httpx.AsyncClient，不再阻塞事件循环
- 端点/端口/超时/缓存/默认模板等参数解耦到 _conf_schema.json，可自填 IP
- 工具改用 dataclass FunctionTool（v4.5.7+ 推荐模式）注册
- 内置 anima-v3 工作流模板（五段式提示词：画师串/质量/主提示词/lora触发词 + 反向）
- 模型/LoRA/CLIP 清单自动同步缓存，离线回退
- 工具支持可选模型、LoRA、KSampler 参数（steps/cfg/sampler_name/scheduler/denoise/seed）
- 队列查询 / 生成中断（comfyui_queue / comfyui_interrupt）
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

# AstrBot 按文件路径导入 main.py，不会把插件目录加入 sys.path。
# 先自举：把本目录插到 sys.path 最前，保证兄弟模块（comfy_client 等）可顶层导入。
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from astrbot.api import AstrBotConfig, logger
from astrbot.api.star import Context, Star, register
from astrbot.core.star.star_tools import StarTools

# 热重载兼容：AstrBot 重载插件时只清 data.plugins.<root>.* 前缀的模块，
# 顶层导入的兄弟模块（external_search/tools 等）会残留旧缓存，
# 导致改代码后热重载仍在跑旧逻辑。这里在导入前主动踢掉它们。
import sys as _sys

for _m in (
    "external_search",
    "tools",
    "comfy_client",
    "workflow_builder",
    "webapi",
    "slot_mapping",
    "recipe_store",
):
    _sys.modules.pop(_m, None)

try:
    from astrbot_plugin_comfyui_direct.animadex import AnimaDexClient
    from astrbot_plugin_comfyui_direct.comfy_client import ComfyUIClient
    from astrbot_plugin_comfyui_direct.external_search import (
        CivitaiClient,
        DanbooruClient,
        GelbooruClient,
    )
    from astrbot_plugin_comfyui_direct.tools import (
        ComfyuiAnimadexTool,
        ComfyuiBooruTool,
        ComfyuiCivitaiSearchTool,
        ComfyuiFetchOutputsTool,
        ComfyuiFreeMemoryTool,
        ComfyuiGenerateTool,
        ComfyuiInterruptTool,
        ComfyuiJobTool,
        ComfyuiListModelsTool,
        ComfyuiModelInfoTool,
        ComfyuiModelsSearchTool,
        ComfyuiNodesTool,
        ComfyuiQueueTool,
        ComfyuiRecipeTool,
        ComfyuiRunWorkflowTool,
        ComfyuiSystemStatsTool,
        ComfyuiUploadFileTool,
        ComfyuiValidateWorkflowTool,
        ComfyuiDrawTool,
        ComfyuiLookupTool,
    )
    from astrbot_plugin_comfyui_direct.workflow_builder import WorkflowBuilder
    from astrbot_plugin_comfyui_direct.recipe_store import RecipeStore
    from astrbot_plugin_comfyui_direct.slot_mapping import (
        SLOT_ROLES,
        merge_slots,
        node_options_for_slot,
        slots_from_config,
    )
except ImportError:
    from animadex import AnimaDexClient
    from comfy_client import ComfyUIClient
    from external_search import CivitaiClient, DanbooruClient, GelbooruClient
    from tools import (
        ComfyuiAnimadexTool,
        ComfyuiBooruTool,
        ComfyuiCivitaiSearchTool,
        ComfyuiFetchOutputsTool,
        ComfyuiFreeMemoryTool,
        ComfyuiGenerateTool,
        ComfyuiInterruptTool,
        ComfyuiJobTool,
        ComfyuiListModelsTool,
        ComfyuiModelInfoTool,
        ComfyuiModelsSearchTool,
        ComfyuiNodesTool,
        ComfyuiQueueTool,
        ComfyuiRecipeTool,
        ComfyuiRunWorkflowTool,
        ComfyuiSystemStatsTool,
        ComfyuiUploadFileTool,
        ComfyuiValidateWorkflowTool,
        ComfyuiDrawTool,
        ComfyuiLookupTool,
    )
    from workflow_builder import WorkflowBuilder
    from recipe_store import RecipeStore
    from slot_mapping import (
        SLOT_ROLES,
        merge_slots,
        node_options_for_slot,
        slots_from_config,
    )

# 与 _conf_schema.json 一致的默认值（配置缺失时的兜底）
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8188
DEFAULT_TIMEOUT = 300
DEFAULT_CACHE_TTL = 600
DEFAULT_WORKFLOW = "anima-v3"


BASIC_LLM_TOOLS = {"comfyui_draw", "comfyui_lookup"}
# 这些工具可能读取任意本地文件、执行未经映射的自定义节点或影响其他任务，
# 默认不交给模型；需要时由管理员显式打开配置。
LLM_UNSAFE_TOOLS = {
    "comfyui_run_workflow",
    "comfyui_upload_file",
    "comfyui_free_memory",
}


def _as_bool(value: Any, default: bool = False) -> bool:
    """Parse config booleans without treating the string ``"false"`` as true."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def _options_target(schema: dict, key: str) -> dict | None:
    """定位配置项里写 options 的目标：template_list 写到 templates.lora.items.name。"""
    item = schema.get(key)
    if not isinstance(item, dict):
        return None
    if item.get("type") == "template_list":
        try:
            return item["templates"]["lora"]["items"]["name"]
        except (KeyError, TypeError):
            return None
    return item


def _stored_lora_names(stored: Any) -> list[str]:
    """提取已保存的 LoRA 名（template_list 数组或旧字符串）。"""
    if isinstance(stored, list):
        out = []
        for it in stored:
            if isinstance(it, dict):
                n = str(it.get("name") or "").strip()
                if n:
                    out.append(n)
        return out
    n = str(stored or "").strip()
    return [n] if n else []


def sync_schema_options(
    schema_path: Path,
    resources: dict,
    stored: dict,
    node_options: dict[str, list[str]] | None = None,
    recipe_names: list[str] | None = None,
) -> bool:
    """把模型/LoRA/节点清单写进 _conf_schema.json 的 options（配置面板变下拉）。

    AstrBot 在插件加载时缓存 schema，因此改动在重载插件后生效。
    """
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"[ComfyUIDirect] 读取 _conf_schema.json 失败，跳过下拉同步: {e}")
        return False
    changed = False
    for key, field, stored_key in (
        ("default_model", "unet_name", "model"),
        ("default_lora", "lora_name", "lora"),
    ):
        target = _options_target(schema, key)
        if target is None:
            continue
        names = [""] + [n for n in (resources.get(field) or []) if n]
        if stored_key == "lora":
            cur_names = _stored_lora_names(stored.get(stored_key))
        else:
            cur = str(stored.get(stored_key) or "").strip()
            cur_names = [cur] if cur else []
        for n in cur_names:
            if n and n not in names:
                names.append(n)
        target["options"] = names
        changed = True

    if node_options:
        items = (schema.get("node_slots") or {}).get("items") or {}
        stored_slots = stored.get("node_slots") or {}
        for role, _label in SLOT_ROLES:
            target = items.get(role)
            if not isinstance(target, dict):
                continue
            options = list(node_options.get(role) or [""])
            current = str(stored_slots.get(role) or "").strip()
            if current and current not in options:
                options.insert(1, current)
            target["options"] = options
            changed = True

    if recipe_names is not None:
        target = schema.get("default_recipe")
        if isinstance(target, dict):
            names = [n for n in recipe_names if n]
            current = str(stored.get("default_recipe") or "").strip()
            if current and current not in names:
                names.insert(0, current)
            if "默认" not in names:
                names.insert(0, "默认")
            target["options"] = names
            changed = True

    if not changed:
        return False
    try:
        schema_path.write_text(
            json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return True
    except OSError as e:
        logger.warning(f"[ComfyUIDirect] 写入 _conf_schema.json 失败: {e}")
        return False


def parse_default_lora(raw: Any) -> str:
    """把配置的 default_lora（template_list 数组 / 旧字符串）转成工具可用的 JSON 数组字符串。

    返回 "" 表示未配置（用模板原值）；否则形如 [{"name": "...", "strength": 0.8}, ...]。
    """
    if isinstance(raw, list):
        entries = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            try:
                strength = float(item.get("strength") or 0.8)
            except (TypeError, ValueError):
                strength = 0.8
            entries.append({"name": name, "strength": strength})
        if not entries:
            return ""
        return json.dumps(entries, ensure_ascii=False)
    txt = str(raw or "").strip()
    if not txt:
        return ""
    try:
        parsed = json.loads(txt)
        if isinstance(parsed, list):
            return txt  # 旧 JSON 数组原样使用
    except ValueError:
        pass
    # 裸文件名（旧下拉遗留）包装成单条
    return json.dumps([{"name": txt, "strength": 0.8}], ensure_ascii=False)


@register(
    "astrbot_plugin_comfyui_direct",
    "长门九曜",
    "局域网直连ComfyUI API，按配方生图",
    "2.3.0",
)
class ComfyUIDirectPlugin(Star):
    """通过局域网直连ComfyUI API生成图片和查询模型。"""

    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None) -> None:
        super().__init__(context, config)
        cfg = config or {}
        try:
            host = str(cfg.get("comfyui_host") or DEFAULT_HOST).strip() or DEFAULT_HOST
            port = int(cfg.get("comfyui_port") or DEFAULT_PORT)
            timeout = float(cfg.get("comfyui_timeout") or DEFAULT_TIMEOUT)
            ttl = int(cfg.get("model_cache_ttl") or DEFAULT_CACHE_TTL)
            default_workflow = (
                str(cfg.get("default_workflow") or DEFAULT_WORKFLOW).strip()
                or DEFAULT_WORKFLOW
            )
            # danbooru_base_url 支持逗号分隔多镜像（顺序回退，如官方站+自建镜像）
            danbooru_urls = tuple(
                u.strip()
                for u in str(cfg.get("danbooru_base_url") or "").split(",")
                if u.strip()
            ) or ("https://danbooru.donmai.us",)
            gelbooru_url = str(cfg.get("gelbooru_base_url") or "").strip() or "https://gelbooru.com"
            civitai_key = str(cfg.get("civitai_api_key") or "").strip()
            # 生成默认值：LLM 不传时使用（"" / 0 = 不覆盖，用模板原值）
            defaults: dict = {
                "artist": str(cfg.get("default_artist") or "").strip(),
                "quality": str(cfg.get("default_quality") or "").strip(),
                "trigger_words": str(cfg.get("default_trigger_words") or "").strip(),
                "negative_prompt": str(cfg.get("default_negative_prompt") or "").strip(),
                "model": str(cfg.get("default_model") or "").strip(),
                "lora": parse_default_lora(cfg.get("default_lora")),
                "sampler_name": str(cfg.get("default_sampler_name") or "").strip(),
                "scheduler": str(cfg.get("default_scheduler") or "").strip(),
                "steps": int(cfg.get("default_steps") or 0),
                "cfg": float(cfg.get("default_cfg") or 0),
                "denoise": float(cfg.get("default_denoise") or 0),
                "width": int(cfg.get("default_width") or 0),
                "height": int(cfg.get("default_height") or 0),
            }
            self._stored_defaults = dict(defaults)  # 供下拉同步保留已存值
        except (TypeError, ValueError) as e:
            logger.error(f"[ComfyUIDirect] 配置解析失败，使用默认值: {e}")
            host, port = DEFAULT_HOST, DEFAULT_PORT
            timeout, ttl = DEFAULT_TIMEOUT, DEFAULT_CACHE_TTL
            default_workflow = DEFAULT_WORKFLOW
            danbooru_urls = ("https://danbooru.donmai.us",)
            gelbooru_url = "https://gelbooru.com"
            civitai_key = ""
            defaults = {}
            self._stored_defaults = {}

        data_dir = StarTools.get_data_dir("astrbot_plugin_comfyui_direct")
        self._output_dir = data_dir / "output"
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._schema_path = Path(__file__).resolve().parent / "_conf_schema.json"
        llm_tool_mode = str(cfg.get("llm_tool_mode") or "basic").strip().lower()
        allow_llm_unsafe_tools = _as_bool(cfg.get("allow_llm_unsafe_tools", False))
        lora_manager_enabled = _as_bool(cfg.get("lora_manager_enabled", True))
        default_recipe_name = str(cfg.get("default_recipe") or "默认").strip() or "默认"
        node_slots_cfg = cfg.get("node_slots") if isinstance(cfg.get("node_slots"), dict) else {}
        self._node_slots_cfg = node_slots_cfg

        self._client = ComfyUIClient(
            host=host,
            port=port,
            timeout=timeout,
            cache_file=data_dir / "comfyui_models.json",
            cache_ttl=ttl,
            lora_manager_enabled=lora_manager_enabled,
        )
        self._civitai = CivitaiClient(api_key=civitai_key)
        # 注入 civitai 客户端到 ComfyUIClient，用于本地无触发词时在线回退
        self._client.civitai_client = self._civitai
        self._builder = WorkflowBuilder(
            plugin_dir=Path(__file__).resolve().parent,
            default_workflow=default_workflow,
            custom_dir=data_dir / "workflows",
        )
        self._store = RecipeStore(data_dir)
        self._apply_config_to_default_recipe(
            default_workflow, node_slots_cfg, defaults, default_recipe_name
        )

        shared: dict = {}
        self._danbooru = DanbooruClient(base_urls=danbooru_urls)
        self._gelbooru = GelbooruClient(base_url=gelbooru_url)
        self._animadex = AnimaDexClient(
            mcp_url=str(cfg.get("animadex_mcp_url") or "http://127.0.0.1:11451/mcp"),
            timeout=float(cfg.get("animadex_timeout") or 8.0),
        )

        self._draw_tool = ComfyuiDrawTool(
            client=self._client,
            builder=self._builder,
            store=self._store,
            output_dir=self._output_dir,
            shared=shared,
            config_defaults=defaults,
        )
        self._draw_tool.refresh_schema()
        self._lookup_tool = ComfyuiLookupTool(
            danbooru=self._danbooru,
            gelbooru=self._gelbooru,
            animadex=self._animadex,
            client=self._client,
        )

        tools = [
            self._draw_tool,
            self._lookup_tool,
            ComfyuiListModelsTool(client=self._client),
            ComfyuiGenerateTool(
                client=self._client,
                builder=self._builder,
                output_dir=self._output_dir,
                shared=shared,
                defaults=defaults,
                store=self._store,
            ),
            ComfyuiInterruptTool(client=self._client, shared=shared),
            ComfyuiQueueTool(client=self._client),
            ComfyuiBooruTool(danbooru=self._danbooru, gelbooru=self._gelbooru),
            ComfyuiCivitaiSearchTool(client=self._civitai),
            ComfyuiAnimadexTool(client=self._animadex),
            ComfyuiModelInfoTool(client=self._client, civitai=self._civitai),
            ComfyuiRunWorkflowTool(
                client=self._client, output_dir=self._output_dir, shared=shared
            ),
            ComfyuiJobTool(client=self._client, shared=shared),
            ComfyuiFetchOutputsTool(client=self._client, output_dir=self._output_dir),
            ComfyuiSystemStatsTool(client=self._client),
            ComfyuiFreeMemoryTool(client=self._client),
            ComfyuiNodesTool(client=self._client),
            ComfyuiValidateWorkflowTool(client=self._client),
            ComfyuiUploadFileTool(client=self._client),
            ComfyuiModelsSearchTool(client=self._client),
            ComfyuiRecipeTool(
                store=self._store,
                allow_delete=allow_llm_unsafe_tools,
            ),
        ]
        for t in tools:
            if llm_tool_mode != "full" and t.name not in BASIC_LLM_TOOLS:
                t.active = False
            elif t.name in LLM_UNSAFE_TOOLS and not allow_llm_unsafe_tools:
                t.active = False
        self.context.add_llm_tools(*tools)
        for t in tools:
            t.handler_module_path = self.__class__.__module__

        try:
            from webapi import register_web_apis

            register_web_apis(
                self.context,
                self._client,
                self._builder,
                self._store,
                self._output_dir,
                shared,
                self._draw_tool,
            )
        except Exception as e:
            logger.error(f"[ComfyUIDirect] WebUI 接口注册失败: {e}")

        try:
            asyncio.create_task(self._client.warm_up_cache())
        except Exception as e:
            logger.warning(f"[ComfyUIDirect] 启动预热失败: {e}")

        self._schema_synced = False
        try:
            asyncio.create_task(self._sync_schema_task())
        except Exception as e:
            logger.warning(f"[ComfyUIDirect] 下拉选项同步任务启动失败: {e}")

        logger.info(
            f"[ComfyUIDirect] 已加载 v2.3.0 | ComfyUI: {self._client.base_url} "
            f"| 默认模板: {default_workflow} | 工具模式: {llm_tool_mode} | 数据目录: {data_dir}"
        )

    def _apply_config_to_default_recipe(
        self,
        default_workflow: str,
        node_slots_cfg: dict,
        defaults: dict,
        default_recipe_name: str,
    ) -> None:
        """用配置下拉框的节点映射和默认底模/LoRA/采样参数更新默认配方。"""
        wf = None
        try:
            wf = self._builder.load_template(default_workflow)
        except FileNotFoundError:
            logger.info("[ComfyUIDirect] 默认工作流尚未导入，打开配方工作台后即可创建配方")
        except Exception as e:
            logger.warning(f"[ComfyUIDirect] 读取默认工作流失败: {e}")

        recipe_defaults = dict(defaults)
        raw_lora = defaults.get("lora")
        if raw_lora:
            try:
                parsed = json.loads(raw_lora) if isinstance(raw_lora, str) else raw_lora
                if isinstance(parsed, list):
                    recipe_defaults["loras"] = parsed
            except ValueError:
                pass

        if wf is not None:
            self._store.bootstrap(
                workflow_name=default_workflow,
                wf=wf,
                config_slots=node_slots_cfg,
                config_defaults=recipe_defaults,
            )

        configured = slots_from_config(node_slots_cfg)
        if not configured:
            return
        recipe = self._store.get(default_recipe_name) or self._store.default()
        if recipe is None:
            return
        recipe["slots"] = merge_slots(recipe.get("slots") or {}, configured)
        if not recipe.get("workflow"):
            recipe["workflow"] = default_workflow
        try:
            self._store.save(recipe)
        except ValueError as e:
            logger.warning(f"[ComfyUIDirect] 更新默认配方节点映射失败: {e}")

    def node_dropdowns(self, wf: dict | None = None) -> dict[str, list[str]]:
        if wf is None:
            try:
                wf = self._builder.load_template()
            except FileNotFoundError:
                return {role: [""] for role, _ in SLOT_ROLES}
        recipe = self._store.default()
        selected = (recipe or {}).get("slots") or {}
        return {
            role: node_options_for_slot(
                wf, role, str((selected.get(role) or {}).get("node") or "")
            )
            for role, _ in SLOT_ROLES
        }

    async def _sync_schema_task(self) -> None:
        """拉取模型/LoRA/节点清单并写入 schema options，重载后生效。"""
        try:
            resources, _ = await self._client.list_resources()
            node_options = self.node_dropdowns()
            ok = sync_schema_options(
                self._schema_path,
                resources,
                {
                    **self._stored_defaults,
                    "node_slots": self._node_slots_cfg or {},
                    "default_recipe": (self._store.default() or {}).get("name") or "默认",
                },
                node_options=node_options,
                recipe_names=self._store.names(),
            )
            if ok:
                self._schema_synced = True
                logger.info(
                    "[ComfyUIDirect] 已同步模型/节点下拉选项到 _conf_schema.json（重载插件后生效）"
                )
        except Exception as e:
            logger.warning(f"[ComfyUIDirect] 同步下拉选项失败: {e}")

    async def terminate(self) -> None:
        """插件重载/卸载时关闭异步连接。"""
        await self._client.close()
        await self._danbooru.close()
        await self._gelbooru.close()
        await self._civitai.close()
        await self._animadex.close()
        logger.info("[ComfyUIDirect] 已卸载，连接已关闭")
