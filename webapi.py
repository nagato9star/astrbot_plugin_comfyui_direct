"""WebUI 后端 API：工作流模板管理 + ComfyUI 状态 + 生成预览（Quart handlers）。

2026-08-11 v2.0.0 新增：配合 pages/workflow-editor 前端使用。
所有路由以插件名作前缀（bridge.apiGet 会转发到 /api/plug/<plugin>/<endpoint>）。
"""

from __future__ import annotations

import base64
from typing import Any

from quart import jsonify, request

from astrbot.api import logger

from comfy_client import ComfyUIClient
from workflow_builder import WorkflowBuilder

PLUGIN_NAME = "astrbot_plugin_comfyui_direct"

# 常用采样器/调度器候选（前端下拉用，ComfyUI 以实际安装为准）
SAMPLER_NAMES = [
    "er_sde", "euler", "euler_ancestral", "dpmpp_2m", "dpmpp_2m_sde",
    "dpmpp_3m_sde", "dpmpp_sde", "ddim", "uni_pc", "lcm",
]
SCHEDULERS = ["normal", "karras", "exponential", "sgm_uniform", "simple", "beta"]


class WorkflowApi:
    def __init__(self, client: ComfyUIClient, builder: WorkflowBuilder) -> None:
        self.client = client
        self.builder = builder

    # ------------------------------------------------------------------
    # 模板管理
    # ------------------------------------------------------------------

    async def list_workflows(self) -> Any:
        return jsonify({"ok": True, "templates": self.builder.list_templates()})

    async def get_workflow(self) -> Any:
        name = (request.args.get("name") or "").strip()
        if not name:
            return jsonify({"ok": False, "error": "缺少 name 参数"})
        try:
            wf = self.builder.load_template(name)  # 去掉 DROP_NODES 后再交给前端
        except FileNotFoundError as e:
            return jsonify({"ok": False, "error": str(e)})
        path = self.builder.resolve_workflow_path(name)
        source = self.builder.source_of(path) or "custom"
        return jsonify({"ok": True, "name": name, "source": source, "workflow": wf})

    async def save_workflow(self) -> Any:
        body = await request.get_json(force=True, silent=True) or {}
        name = str(body.get("name") or "").strip()
        wf = body.get("workflow")
        if not name or not isinstance(wf, dict):
            return jsonify({"ok": False, "error": "参数错误：需要 name 与 workflow"})
        try:
            self.builder.save_template(name, wf)
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)})
        logger.info(f"[ComfyUIDirect] WebUI 已保存工作流模板: {name}")
        return jsonify({"ok": True, "name": name})

    async def delete_workflow(self) -> Any:
        body = await request.get_json(force=True, silent=True) or {}
        name = str(body.get("name") or "").strip()
        if not name:
            return jsonify({"ok": False, "error": "缺少 name 参数"})
        try:
            self.builder.delete_template(name)
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)})
        logger.info(f"[ComfyUIDirect] WebUI 已删除自定义工作流: {name}")
        return jsonify({"ok": True, "name": name})

    # ------------------------------------------------------------------
    # 状态与资源
    # ------------------------------------------------------------------

    async def status(self) -> Any:
        connected = await self.client.ping(timeout=5.0)
        # ?refresh=1：强制重新从 ComfyUI 同步模型清单（前端刷新按钮用）
        force = str(request.args.get("refresh") or "").strip() == "1"
        resources, from_cache = {}, False
        system_stats = None
        if connected:
            try:
                resources, from_cache = await self.client.list_resources(
                    force_refresh=force
                )
                system_stats = await self.client.get_system_stats()
            except Exception as e:
                logger.warning(f"[ComfyUIDirect] 状态接口取资源失败: {e}")
        return jsonify(
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
            }
        )

    # ------------------------------------------------------------------
    # 生成预览（提交即返回 prompt_id，前端轮询）
    # ------------------------------------------------------------------

    async def generate(self) -> Any:
        """POST：提交工作流，返回 prompt_id。"""
        body = await request.get_json(force=True, silent=True) or {}
        wf = body.get("workflow")
        if not isinstance(wf, dict):
            return jsonify({"ok": False, "error": "参数错误：需要 workflow"})
        self.builder.drop_ui_nodes(wf)
        pid, err = await self.client.submit_prompt_detail(wf)
        if err:
            return jsonify({"ok": False, "error": err})
        return jsonify({"ok": True, "prompt_id": pid})

    async def generate_poll(self) -> Any:
        """GET /generate?pid=xxx：查询一次执行状态，完成后返回首张图（webp 预览，省带宽）。"""
        pid = (request.args.get("pid") or "").strip()
        if not pid:
            return jsonify({"ok": False, "error": "缺少 pid 参数"})
        entry = await self.client.get_history_entry(pid)
        if entry is None:
            return jsonify({"ok": True, "done": False})
        st = entry.get("status") or {}
        if st.get("status_str") == "error":
            return jsonify(
                {"ok": True, "done": True, "error": st.get("message") or "执行出错（详见 ComfyUI 日志）"}
            )
        outputs = entry.get("outputs", {})
        images = []
        for node_out in outputs.values():
            images.extend(node_out.get("images", []))
        if not images:
            return jsonify({"ok": True, "done": True, "error": "执行完成但无输出图片"})
        img = images[0]
        content = await self.client.download_image(
            img["filename"], img.get("subfolder", ""), preview="webp;80"
        )
        if not content:
            return jsonify(
                {"ok": True, "done": True, "error": f"图片下载失败: {img['filename']}"}
            )
        return jsonify(
            {
                "ok": True,
                "done": True,
                "filename": img["filename"],
                "data_url": "data:image/webp;base64,"
                + base64.b64encode(content).decode("ascii"),
            }
        )

    async def interrupt(self) -> Any:
        """POST /generate/interrupt：中断生成。body: {"prompt_id": "..."}。"""
        body = await request.get_json(force=True, silent=True) or {}
        pid = str(body.get("prompt_id") or "").strip()
        ok = await self.client.interrupt(prompt_id=pid or None)
        return jsonify({"ok": ok, "prompt_id": pid or None})


def register_web_apis(
    context, client: ComfyUIClient, builder: WorkflowBuilder
) -> None:
    """注册全部 WebUI 接口（main.py 启动时调用）。"""
    api = WorkflowApi(client, builder)
    context.register_web_api(
        f"/{PLUGIN_NAME}/workflows", api.list_workflows, ["GET"], "列出工作流模板"
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/workflow", api.get_workflow, ["GET"], "获取工作流"
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/workflow/save",
        api.save_workflow,
        ["POST"],
        "保存工作流",
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/workflow/delete",
        api.delete_workflow,
        ["POST"],
        "删除自定义工作流",
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/status", api.status, ["GET"], "ComfyUI 状态与模型清单"
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/generate", api.generate, ["POST"], "提交工作流生成"
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/generate", api.generate_poll, ["GET"], "轮询生成结果"
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/generate/interrupt",
        api.interrupt,
        ["POST"],
        "中断生成",
    )
