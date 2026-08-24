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
    )
    from astrbot_plugin_comfyui_direct.workflow_builder import WorkflowBuilder
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
    )
    from workflow_builder import WorkflowBuilder

# 与 _conf_schema.json 一致的默认值（配置缺失时的兜底）
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8188
DEFAULT_TIMEOUT = 300
DEFAULT_CACHE_TTL = 600
DEFAULT_WORKFLOW = "anima-v3"


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


def sync_schema_options(schema_path: Path, resources: dict, stored: dict) -> bool:
    """把 ComfyUI 模型/LoRA 清单写进 _conf_schema.json 的 options（配置面板变下拉）。

    AstrBot 在插件加载时缓存 schema，因此改动在重载插件后生效。
    resources 来自 ComfyUI 资源缓存（离线也可用最近一次清单）。
    """
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"[ComfyUIDirect] 读取 _conf_schema.json 失败，跳过下拉同步: {e}")
        return False
    changed = False
    # (schema 配置项, 资源字段, stored 字典里的键)
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
                names.append(n)  # 已保存的值不在清单里也保留，避免下拉空白
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
    "局域网直连ComfyUI API，提供图片生成与模型查询",
    "2.1.0",
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

        self._client = ComfyUIClient(
            host=host,
            port=port,
            timeout=timeout,
            cache_file=data_dir / "comfyui_models.json",
            cache_ttl=ttl,
        )
        self._builder = WorkflowBuilder(
            plugin_dir=Path(__file__).resolve().parent,
            default_workflow=default_workflow,
            custom_dir=data_dir / "workflows",
        )

        # WebUI 后端接口（页面 pages/workflow-editor）
        try:
            from webapi import register_web_apis

            register_web_apis(self.context, self._client, self._builder)
        except Exception as e:
            logger.error(f"[ComfyUIDirect] WebUI 接口注册失败: {e}")

        shared: dict = {}  # 跨工具共享状态（last_prompt_id 等）
        self._danbooru = DanbooruClient(base_urls=danbooru_urls)
        self._gelbooru = GelbooruClient(base_url=gelbooru_url)
        self._civitai = CivitaiClient(api_key=civitai_key)
        self._animadex = AnimaDexClient(
            mcp_url=str(cfg.get("animadex_mcp_url") or "http://127.0.0.1:11451/mcp"),
            timeout=float(cfg.get("animadex_timeout") or 8.0),
        )
        tools = [
            ComfyuiListModelsTool(client=self._client),
            ComfyuiGenerateTool(
                client=self._client,
                builder=self._builder,
                output_dir=self._output_dir,
                shared=shared,
                defaults=defaults,
                recipe_dir=data_dir / "recipes",
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
            ComfyuiRecipeTool(recipe_dir=data_dir / "recipes"),
        ]
        self.context.add_llm_tools(*tools)
        # 修正工具来源标识：add_llm_tools 按类 __module__（tools）解析来源，
        # 会导致 WebUI 显示"unknown"且插件卸载时工具不被清理。
        # 统一改为插件模块名（main），与注册的 StarMetadata.module_path 对齐。
        for t in tools:
            t.handler_module_path = self.__class__.__module__

        # 启动时后台预热资源清单（自动同步）
        try:
            asyncio.create_task(self._client.warm_up_cache())
        except Exception as e:
            logger.warning(f"[ComfyUIDirect] 启动预热失败: {e}")

        # 后台把模型/LoRA 清单同步进 _conf_schema.json 的 options（配置面板下拉）
        self._schema_synced = False
        try:
            asyncio.create_task(self._sync_schema_task())
        except Exception as e:
            logger.warning(f"[ComfyUIDirect] 下拉选项同步任务启动失败: {e}")

        logger.info(
            f"[ComfyUIDirect] 已加载 v2.0.0 | ComfyUI: {self._client.base_url} "
            f"| 默认模板: {default_workflow} | 数据目录: {data_dir}"
        )

    async def _sync_schema_task(self) -> None:
        """拉取（缓存优先）模型/LoRA 清单并写入 schema options，重载后生效。"""
        try:
            resources, _ = await self._client.list_resources()
            ok = sync_schema_options(
                Path(__file__).resolve().parent / "_conf_schema.json",
                resources,
                self._stored_defaults,
            )
            if ok:
                self._schema_synced = True
                logger.info(
                    "[ComfyUIDirect] 已同步模型下拉选项到 _conf_schema.json（重载插件后生效）"
                )
        except Exception as e:
            logger.warning(f"[ComfyUIDirect] 同步模型下拉选项失败: {e}")

    async def terminate(self) -> None:
        """插件重载/卸载时关闭异步连接。"""
        await self._client.close()
        await self._danbooru.close()
        await self._gelbooru.close()
        await self._civitai.close()
        await self._animadex.close()
        logger.info("[ComfyUIDirect] 已卸载，连接已关闭")
