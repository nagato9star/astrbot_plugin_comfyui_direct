"""LLM 工具定义（dataclass FunctionTool 模式，v4.5.7+ 推荐）。

comfyui_list_models：查询模型/LoRA/CLIP/VAE/Embedding 清单（自动同步缓存）
comfyui_generate：生成图片（可选模型/LoRA/KSampler 参数）
comfyui_interrupt：中断生成 / 取消排队任务
comfyui_queue：查询队列与 GPU 状态
comfyui_booru：danbooru/gelbooru 查画师/角色触发词
comfyui_civitai_search：civitai 搜图查生成配方
comfyui_model_info：查询模型/LoRA 元数据与触发词（本地 / civitai）
"""

from __future__ import annotations

import asyncio
import json
import random
import time
import uuid
from pathlib import Path
from typing import Any

from astrbot.api import FunctionTool, logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext
from pydantic import ConfigDict, Field
from pydantic.dataclasses import dataclass

from animadex import AnimaDexClient
from comfy_client import ComfyUIClient
from external_search import CivitaiClient, DanbooruClient, GelbooruClient
from recipe_store import RecipeStore, materialize_values
from slot_mapping import apply_slots, parse_lora, resolve_size
from workflow_builder import WorkflowBuilder


@dataclass(config=ConfigDict(arbitrary_types_allowed=True))
class ComfyuiListModelsTool(FunctionTool[AstrAgentContext]):
    """查询 ComfyUI 可用模型/LoRA/CLIP 清单。"""

    name: str = "comfyui_list_models"
    description: str = (
        "查询本机上ComfyUI可用的UNET底模、LoRA、CLIP、VAE、Embedding列表。"
        "LoRA 会附带从 safetensors 头部读出的触发词"
        "（ss_activation_tags / ss_tag_frequency 按频率取 top）。"
        "清单会自动同步并本地缓存，ComfyUI离线时返回最近一次同步结果。"
        "用户想知道有什么模型/LoRA/CLIP可用时使用。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "refresh": {
                    "type": "boolean",
                    "description": "是否强制重新从 ComfyUI 同步一次，默认 false（用本地缓存）",
                }
            },
        }
    )
    client: ComfyUIClient | None = None

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        force = bool(kwargs.get("refresh", False))
        resources, from_cache = await self.client.list_resources(force_refresh=force)

        parts = ["【ComfyUI 可用资源】"]
        if from_cache:
            parts.append("（来源：本地缓存，自动同步失败时回退）")
        parts.append("")
        lora_meta = resources.get("lora_meta") or {}
        for field, title in (
            ("unet_name", "UNET 底模"),
            ("lora_name", "LoRA"),
            ("clip_name", "CLIP"),
            ("vae_name", "VAE"),
            ("embeddings", "Embedding"),
        ):
            items = resources.get(field) or []
            parts.append(f"--- {title} ---")
            if field == "lora_name":
                for m in items:
                    parts.append(f"  {m}")
                    info = lora_meta.get(m)
                    if info and info.get("trigger_words"):
                        parts.append(
                            f"    触发词: {', '.join(info['trigger_words'])}"
                        )
            elif items:
                parts.extend(f"  {m}" for m in items)
            else:
                parts.append("  (无)")
            parts.append("")
        return "\n".join(parts)


@dataclass(config=ConfigDict(arbitrary_types_allowed=True))
class ComfyuiGenerateTool(FunctionTool[AstrAgentContext]):
    """通过 ComfyUI 生成图片。"""

    name: str = "comfyui_generate"
    description: str = (
        "通过本机的ComfyUI生成一张图片。默认加载 V5 工作流模板（五段式提示词："
        "主提示词/画师串/质量/lora触发词 四段拼接 + 负向提示词）。\n"
        "【铁律】\n"
        "1. prompt（tag串）必填。默认模板 V5 是 1024x1024 正方形且易出蹲坐/半身构图，"
        "所以【每次都要按构图传 width/height】：全身站姿→832x1216(2:3)；半身坐姿→768x1024(3:4)；"
        "横版场景→1216x832(3:2)；头像→768x1024；图生图保持原图比例。\n"
        "2. lora【默认绝不传】！V5 已内置 Turbo-ANIMA-v2.9(0.8) + 5个风格lora（Betabeet-000060 1.0 / "
        "deepseek_whale_girl_maid 0.75 / zoda_v3_anima 0.5 / anima_context_detailer_base10 0.76 / "
        "anima_footRepair_v2 1.0）。传 lora 数组=【全量覆盖】Power插槽（顺序映射lora_1..N，多余的关掉），"
        "只传一个会把默认风格链全顶掉。禁止再传任何 turbo/加速 lora（已内置）。"
        "真要换链必须传完整新链（含要保留的默认lora），[]或\"none\"=禁用全部（Turbo也归零），别乱用。\n"
        "3. 只有用户明确要求才覆盖其他参数：提到画师→填 artist（先调 comfyui_booru 查证）；"
        "提到角色→务必先用系统自带 search-characters / get-character 查该角色的规范 danbooru tag"
        "（本地 AnimaDex MCP，角色特征以此为准），再把查到的角色 tag 填进 prompt 或 trigger_words，"
        "禁止凭记忆瞎编角色特征；提到换底模→填 model（不知道文件名先查 comfyui_list_models）；"
        "要求画质→填 quality；调参数→填 steps/cfg/sampler_name 等。\n"
        "4. 用户没有特别要求时，其他参数一律不传，绝不编造模型名/画师名/tag。\n"
        "5. 生成成功后图片由插件直接发送到当前会话，不要再调用 send_message_to_user 发图。\n"
        "6. 传 recipe（配方名）时，配方里保存的参数作为底层默认，本参数显式传的值优先；prompt 按需另传。\n"
        "正确示例：comfyui_generate(prompt=\"1girl, solo, standing, full body, black hair, street\", width=832, height=1216)"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "主提示词：danbooru 风格 tag 串，描述人物/动作/场景/服饰/构图，不含质量词与画师（必填）",
                },
                "artist": {
                    "type": "string",
                    "description": "画师串，格式如 @画师名（@加名字即可，逗号分隔，不用写权重）。用户提到画师/画风时此参数必须填写（先用 comfyui_booru 查证）；不传则保留模板默认画师，传空字符串可清空画师",
                },
                "trigger_words": {
                    "type": "string",
                    "description": "lora触发词，以 @ 开头的逗号分隔串，如 @deadpuritystyle,@f1f。用户提到 lora/触发词时填，否则留空用模板默认",
                },
                "quality": {
                    "type": "string",
                    "description": "质量词串（masterpiece, best quality, score_9 等）。用户要求画质/光影时填，否则留空用模板默认",
                },
                "negative_prompt": {
                    "type": "string",
                    "description": "负向提示词。用户有特殊负向要求时填，否则留空用模板默认",
                },
                "model": {
                    "type": "string",
                    "description": "底模文件名，如 miaomiaoRealskin_anima11.safetensors。用户指定底模时填，不知道文件名先查 comfyui_list_models",
                },
                "lora": {
                    "type": "string",
                    "description": (
                        "LoRA 覆盖，按 Power Lora Loader 插槽 lora_1..lora_8 顺序"
                        "（旧模板按 LoRA 链顺序），每项含 name 与 strength，如 "
                        '[{"name":"anima-base-1-photo-background-v4.safetensors","strength":0.55}]。'
                        "解析器兼容 JSON 字符串、数组对象与 repr 字符串三种传法，"
                        "直接传数组即可，不必手动字符串化。传 [] 或 \"none\" 禁用全部。"
                        "默认值在插件配置里可配多条（含强度），通常无需传"
                    ),
                },
                "steps": {
                    "type": "number",
                    "description": "采样步数。不传用插件配置默认",
                },
                "cfg": {
                    "type": "number",
                    "description": "CFG。不传用插件配置默认",
                },
                "sampler_name": {
                    "type": "string",
                    "description": "采样器名，如 er_sde。不传用插件配置默认",
                },
                "scheduler": {
                    "type": "string",
                    "description": "调度器，如 normal。不传用插件配置默认",
                },
                "denoise": {
                    "type": "number",
                    "description": "降噪强度 0~1。不传用插件配置默认",
                },
                "width": {
                    "type": "number",
                    "description": "图片宽度。不传用插件配置默认",
                },
                "height": {
                    "type": "number",
                    "description": "图片高度。不传用插件配置默认",
                },
                "seed": {
                    "type": "number",
                    "description": "随机种子。不传随机",
                },
                "workflow": {
                    "type": "string",
                    "description": "工作流模板名（anima-v3 默认 / nagato-anima / anima-v2），也可传 JSON 文件路径",
                },
                "recipe": {
                    "type": "string",
                    "description": "配方名：从已保存的配方（comfyui_recipe save 的）加载参数并覆盖本参数。传了 recipe 时以配方为准，本参数没传的项用配方值",
                },
            },
            "required": ["prompt"],
        }
    )
    client: ComfyUIClient | None = None
    builder: WorkflowBuilder | None = None
    output_dir: Path | None = None
    shared: dict = Field(default_factory=dict)  # 跨工具共享状态（如 last_prompt_id）
    defaults: dict = Field(default_factory=dict)  # 插件配置里的生成默认值（LLM 不传时使用）
    recipe_dir: Path | None = None  # 配方目录（comfyui_recipe 保存的）

    @staticmethod
    def _pick(defaults: dict, key: str, value: Any) -> Any:
        """参数优先级：LLM 传值 > 配置默认 > None（模板原值）。

        "" / 0 视为"未配置"，保持模板原值。
        """
        if value is not None:
            return value
        d = defaults.get(key)
        if d in (None, "", 0, 0.0):
            return None
        return d

    def _load_recipe(self, name: str) -> dict | None:
        """按配方名读本地配方 JSON，返回配方参数 dict；不存在返回 None。"""
        if not self.recipe_dir or not name:
            return None
        safe = "".join(c for c in name if c.isalnum() or c in "-_")
        if safe != name:
            return None
        p = self.recipe_dir / f"{safe}.json"
        if not p.is_file():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    async def _wait_outputs(self, prompt_id: str) -> tuple[dict | None, str | None]:
        """轮询执行结果，返回 (outputs, 错误信息)。执行失败/超时返回错误信息。

        ZeroTier 抽风时单次 GET 可能失败（内部已自动重试），连续失败累计
        超过阈值打警告提示链路不稳，但不中断轮询。
        """
        deadline = time.time() + self.client.timeout
        miss = 0
        while time.time() < deadline:
            entry = await self.client.get_history_entry(prompt_id)
            if entry is not None:
                miss = 0
                st = entry.get("status") or {}
                if st.get("status_str") == "error":
                    return None, st.get("message") or "执行出错（详见 ComfyUI 日志）"
                return entry.get("outputs", {}), None
            miss += 1
            if miss == 5:
                logger.warning(
                    f"[ComfyUIDirect] 轮询 {prompt_id} 连续 {miss} 次无响应"
                    f"（ZeroTier 链路抖动?），继续等待不中断"
                )
            await asyncio.sleep(2)
        logger.error(f"[ComfyUIDirect] 生成超时 ({int(self.client.timeout)}s)")
        return None, f"生成超时（{int(self.client.timeout)}s）"

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        prompt = str(kwargs.get("prompt") or "").strip()
        if not prompt:
            return "生成失败：prompt（主提示词）不能为空。"

        # 配方覆盖：传了 recipe 名，配方里有的参数作为底层默认（LLM 显式传值仍优先）
        recipe_name = str(kwargs.get("recipe") or "").strip()
        if recipe_name:
            recipe = self._load_recipe(recipe_name)
            if recipe is None:
                return f"生成失败：配方不存在（{recipe_name}）。可先 comfyui_recipe list 查看。"
            merged = dict(recipe)
            merged.pop("name", None)
            merged.pop("prompt", None)  # prompt 以本参数为准
            # 把配方值塞进 kwargs（LLM 传的值覆盖）
            merged_kwargs = {**merged, **kwargs}
            kwargs = merged_kwargs
            prompt = str(kwargs.get("prompt") or "").strip() or prompt

        seed = kwargs.get("seed")
        if seed is None:
            seed = random.randint(0, 2**31 - 1)

        try:
            pick = lambda key: self._pick(self.defaults, key, kwargs.get(key))  # noqa: E731
            lora_val = pick("lora")
            trigger_val = pick("trigger_words")
            # LLM 没传 trigger_words 时，从 lora_meta 自动查触发词
            if lora_val and not trigger_val:
                trigger_val = await _auto_fill_trigger_words(self.client, lora_val)
            wf = self.builder.build(
                workflow=kwargs.get("workflow"),
                prompt=prompt,
                artist=pick("artist"),
                trigger_words=trigger_val,
                quality=pick("quality"),
                negative_prompt=pick("negative_prompt"),
                model=pick("model"),
                lora=lora_val,
                width=pick("width"),
                height=pick("height"),
                seed=seed,
                steps=pick("steps"),
                cfg=pick("cfg"),
                sampler_name=pick("sampler_name"),
                scheduler=pick("scheduler"),
                denoise=pick("denoise"),
                prefix=f"astrbot_{uuid.uuid4().hex[:8]}",
            )
        except FileNotFoundError as e:
            return f"生成失败：{e}"
        except (ValueError, json.JSONDecodeError) as e:
            return f"生成失败：参数错误（{e}）"

        pid, submit_err = await self.client.submit_prompt_detail(wf)
        if submit_err:
            return f"生成失败：{submit_err}"
        if not pid:
            return (
                "生成失败：无法连接 ComfyUI，请确认本机已开机且ComfyUI已启动、"
                "插件配置的地址（IP/端口）正确。"
            )
        self.shared["last_prompt_id"] = pid

        outputs, wait_err = await self._wait_outputs(pid)
        if wait_err:
            return f"生成失败：{wait_err}"
        if outputs is None:
            return "生成失败：未获取到执行结果。"

        images = []
        for node_out in outputs.values():
            images.extend(node_out.get("images", []))
        if not images:
            return "生成似乎已完成，但未找到输出图片文件。"

        img = images[0]
        filename = img["filename"]
        subfolder = img.get("subfolder", "")
        content = await self.client.download_image(filename, subfolder)
        if not content:
            return (
                f"图片已在ComfyUI生成（{filename}），但下载到本地失败。\n"
                f"可手动访问 {self.client.base_url}/view?filename={filename}&type=output 查看。"
            )

        local_path = self.output_dir / filename
        try:
            local_path.write_bytes(content)
        except OSError as e:
            logger.error(f"[ComfyUIDirect] 写入图片失败: {e}")
            return f"图片已生成但本地保存失败（{e}）。文件名: {filename}"

        try:
            event: AstrMessageEvent = context.context.event
            await event.send(MessageChain().file_image(str(local_path)))
        except Exception as e:
            logger.error(f"[ComfyUIDirect] 图片发送失败: {e}")
            return (
                f"图片已生成但自动发送失败（{e}）。\n"
                f"本地路径: {local_path}\n"
                f"请用 send_message_to_user 发送这张图。"
            )

        return (
            f"图片已生成并直接发送到会话。\n"
            f"本地路径: {local_path}\n"
            f"文件名: {filename}\n"
            f"prompt_id: {pid}"
        )


@dataclass(config=ConfigDict(arbitrary_types_allowed=True))
class ComfyuiInterruptTool(FunctionTool[AstrAgentContext]):
    """中断生成 / 取消排队任务。"""

    name: str = "comfyui_interrupt"
    description: str = (
        "中断 ComfyUI 正在执行的生成任务（用户要求停止/改图时用）。"
        "不传 prompt_id 时中断最近一次由本插件提交的任务；可同时从待执行队列移除该任务。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "prompt_id": {
                    "type": "string",
                    "description": "要中断的任务 ID（生成结果里返回的 prompt_id），可选；不传用最近一次生成的任务",
                },
                "remove_from_queue": {
                    "type": "boolean",
                    "description": "是否同时把该任务从待执行队列移除（默认 false，仅中断运行中的）",
                },
            },
        }
    )
    client: ComfyUIClient | None = None
    shared: dict = Field(default_factory=dict)

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        pid = str(kwargs.get("prompt_id") or "").strip() or self.shared.get(
            "last_prompt_id"
        )
        remove = bool(kwargs.get("remove_from_queue", False))

        if remove and pid:
            await self.client.delete_queue_items([pid])
        ok = await self.client.interrupt(prompt_id=pid or None)
        if not ok:
            return "中断失败：无法连接 ComfyUI。"
        if pid:
            parts = [f"已请求中断任务 {pid}。"]
            if remove:
                parts.append("该任务已从待执行队列移除。")
            parts.append("若任务已执行完毕则无需处理。")
            return "".join(parts)
        return "已请求全局中断（当前运行中的任务将被停止）。"


@dataclass(config=ConfigDict(arbitrary_types_allowed=True))
class ComfyuiQueueTool(FunctionTool[AstrAgentContext]):
    """查询 ComfyUI 队列与 GPU 状态。"""

    name: str = "comfyui_queue"
    description: str = (
        "查询 ComfyUI 当前队列（运行中/待执行任务数）与 GPU 显存占用。"
        "用户想知道生成进度、为什么慢、显存够不够时使用。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "include_gpu": {
                    "type": "boolean",
                    "description": "是否附带 GPU/显存状态（默认 true）",
                },
            },
        }
    )
    client: ComfyUIClient | None = None

    @staticmethod
    def _gb(n: int | float) -> str:
        try:
            return f"{n / (1024 ** 3):.1f}GB"
        except (TypeError, ValueError):
            return "?"

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        queue = await self.client.get_queue()
        if queue is None:
            return "查询失败：无法连接 ComfyUI。"
        running = queue.get("queue_running") or []
        pending = queue.get("queue_pending") or []
        running_ids = [str(item[1]) for item in running if isinstance(item, (list, tuple)) and len(item) > 1]
        pending_ids = [str(item[1]) for item in pending if isinstance(item, (list, tuple)) and len(item) > 1]

        parts = [f"【ComfyUI 队列】运行中 {len(running_ids)} 个，待执行 {len(pending_ids)} 个"]
        if running_ids:
            parts.append(f"运行中: {', '.join(running_ids[:3])}")
        if pending_ids:
            parts.append(f"待执行: {', '.join(pending_ids[:5])}" + ("…" if len(pending_ids) > 5 else ""))

        if kwargs.get("include_gpu", True):
            stats = await self.client.get_system_stats()
            if stats:
                sysinfo = stats.get("system", {})
                devices = stats.get("devices", [])
                parts.append("")
                parts.append(f"ComfyUI 版本: {sysinfo.get('comfyui_version', '?')}")
                ram_t = self._gb(sysinfo.get("ram_total"))
                ram_f = self._gb(sysinfo.get("ram_free"))
                parts.append(f"内存: 空闲 {ram_f} / 共 {ram_t}")
                for d in devices[:2]:
                    name = d.get("name", "?")
                    vram_t = self._gb(d.get("vram_total"))
                    vram_f = self._gb(d.get("vram_free"))
                    parts.append(f"GPU {d.get('index', 0)} {name}: 显存空闲 {vram_f} / 共 {vram_t}")
            else:
                parts.append("")
                parts.append("（GPU 状态获取失败）")
        return "\n".join(parts)


@dataclass(config=ConfigDict(arbitrary_types_allowed=True))
class ComfyuiBooruTool(FunctionTool[AstrAgentContext]):
    """danbooru / gelbooru 画师/角色触发词查询。"""

    name: str = "comfyui_booru"
    description: str = (
        "从 danbooru（默认）或 gelbooru 查询画师或角色的触发词、别名和常用 tag。"
        "danbooru 查询失败或无结果时自动回退 gelbooru（结果里会标注真实来源）。"
        "用户指定画师风格/角色时，先调用本工具查到真实触发词，"
        "再把 @画师 串填进 comfyui_generate 的 artist/trigger_words 参数，不要凭记忆编 tag。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "enum": ["danbooru", "gelbooru"],
                    "description": "查询源：danbooru（默认）/ gelbooru；danbooru 无结果会自动回退 gelbooru",
                },
                "type": {
                    "type": "string",
                    "enum": ["artist", "character"],
                    "description": "查询类型：artist=画师，character=角色",
                },
                "query": {
                    "type": "string",
                    "description": "画师名或角色名（中文名/罗马音/日文均可，如 初音ミク）",
                },
                "limit": {
                    "type": "number",
                    "description": "取样作品数（1-50，默认 30），越多统计越准但越慢",
                },
            },
            "required": ["type", "query"],
        }
    )
    danbooru: DanbooruClient | None = None
    gelbooru: GelbooruClient | None = None

    @staticmethod
    def _fmt_tags(tags: list[tuple[str, int]]) -> str:
        if not tags:
            return "  (无样本)"
        return "  " + ", ".join(f"{t}({n})" for t, n in tags)

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        source = str(kwargs.get("source") or "danbooru").strip().lower()
        kind = str(kwargs.get("type") or "").strip().lower()
        query = str(kwargs.get("query") or "").strip()
        if source not in ("danbooru", "gelbooru") or kind not in ("artist", "character") or not query:
            return "查询失败：需要 source（danbooru/gelbooru）、type（artist/character）与 query 参数。"
        limit = min(max(int(kwargs.get("limit") or 30), 1), 50)

        # 主源查询失败（danbooru 镜像 403/无结果）时自动回退 gelbooru，不用让 LLM 手动重试
        primary = self.danbooru if source == "danbooru" else self.gelbooru
        fallback = self.gelbooru if source == "danbooru" else self.danbooru
        if primary is None:
            return f"查询失败：{source} 未配置。"

        data = None
        used_source = source
        if kind == "artist":
            data = await primary.search_artist(query, limit)
            if data is None and fallback is not None:
                data = await fallback.search_artist(query, limit)
                used_source = "gelbooru" if source == "danbooru" else "danbooru"
            if data is None:
                return f"{source} 未找到画师：{query}"
            parts = [f"【画师 @{data['artist']}】（来源: {used_source}）"]
            if data["aliases"]:
                parts.append(f"别名: {', '.join(data['aliases'])}")
            parts.append("常用画风 tag（作品取样统计）:")
            posts = (
                GelbooruClient.normalize_posts(data["posts"])
                if used_source == "gelbooru"
                else data["posts"]
            )
            parts.append(self._fmt_tags(DanbooruClient.aggregate_tags(posts)))
            parts.append(f"触发词建议: @{data['artist']}")
            parts.append(f"参考: {data['url']}")
            return "\n".join(parts)

        data = await primary.search_character(query, limit)
        if data is None and fallback is not None:
            data = await fallback.search_character(query, limit)
            used_source = "gelbooru" if source == "danbooru" else "danbooru"
        if data is None:
            return f"{source} 未找到角色：{query}"
        parts = [f"【角色 {data['character']}】（来源: {used_source}）"]
        if data["aliases"]:
            parts.append(f"别名: {', '.join(data['aliases'])}")
        parts.append("常用 tag（作品取样统计）:")
        posts = (
            GelbooruClient.normalize_posts(data["posts"])
            if used_source == "gelbooru"
            else data["posts"]
        )
        parts.append(self._fmt_tags(DanbooruClient.aggregate_tags(posts)))
        parts.append(f"触发词建议: {data['character']}（别名见上）")
        parts.append(f"参考: {data['url']}")
        return "\n".join(parts)





@dataclass(config=ConfigDict(arbitrary_types_allowed=True))
class ComfyuiCivitaiSearchTool(FunctionTool[AstrAgentContext]):
    """civitai 搜图查生成配方。"""

    name: str = "comfyui_civitai_search"
    description: str = (
        "在 civitai 搜索参考图并返回其完整生成配方（模型、正向/负向提示词、采样器、"
        "步数、cfg、seed），可直接转成 comfyui_generate 的参数照着出图。"
        "用户想参考某风格/某模型的作品或找现成提示词时使用。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词（风格/角色/模型名等）",
                },
                "limit": {
                    "type": "number",
                    "description": "返回配方条数（1-10，默认 5）",
                },
                "nsfw": {
                    "type": "boolean",
                    "description": "是否包含 NSFW 内容（默认 false）",
                },
            },
            "required": ["query"],
        }
    )
    client: CivitaiClient | None = None

    @staticmethod
    def _truncate(s: str, n: int) -> str:
        s = (s or "").strip()
        return s if len(s) <= n else s[:n] + "…"

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        query = str(kwargs.get("query") or "").strip()
        if not query:
            return "查询失败：query 不能为空。"
        limit = min(max(int(kwargs.get("limit") or 5), 1), 10)
        nsfw = bool(kwargs.get("nsfw", False))

        items = await self.client.search_images(query, limit, nsfw)
        if not items:
            return f"civitai 未找到相关图片：{query}"

        parts = [f"【civitai 配方参考】搜索: {query}（按最多反应排序）"]
        for i, item in enumerate(items, 1):
            meta = item.get("meta") or {}
            model = (
                meta.get("Model")
                or meta.get("model")
                or item.get("modelName")
                or "未知模型"
            )
            parts.append("")
            parts.append(f"[{i}] 模型: {model}")
            if meta.get("prompt"):
                parts.append(f"prompt: {self._truncate(meta['prompt'], 400)}")
            if meta.get("negativePrompt"):
                parts.append(f"负向: {self._truncate(meta['negativePrompt'], 200)}")
            sampler = meta.get("sampler") or "?"
            steps = meta.get("steps") or "?"
            cfg = meta.get("cfgScale") or "?"
            seed = meta.get("seed") or "?"
            parts.append(f"sampler/steps/cfg/seed: {sampler} / {steps} / {cfg} / {seed}")
            url = item.get("url") or ""
            if url:
                parts.append(f"图: {url}")
        return "\n".join(parts)


@dataclass(config=ConfigDict(arbitrary_types_allowed=True))
class ComfyuiModelInfoTool(FunctionTool[AstrAgentContext]):
    """模型/LoRA 元数据与触发词查询（本地 safetensors 头部 / civitai trainedWords）。"""

    name: str = "comfyui_model_info"
    description: str = (
        "查询模型或 LoRA 的元数据与触发词：source=local 读 本机 上已装模型的 "
        "safetensors 头部信息（标题/作者/标签/训练触发词）；source=civitai 按名称搜索 "
        "civitai 模型记录（含官方 trainedWords 触发词）。用户想知道某个模型/LoRA 是干嘛的、"
        "触发词是什么时使用。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "模型文件名（local，如 miaomiaoRealskin_anima11.safetensors）或模型名（civitai，如 anima）",
                },
                "source": {
                    "type": "string",
                    "enum": ["local", "civitai"],
                    "description": "查询源：local=本地已装模型（默认），civitai=在线搜索",
                },
                "types": {
                    "type": "string",
                    "enum": ["LORA", "Checkpoint"],
                    "description": "civitai 搜索的模型类型（默认 LORA；底模用 Checkpoint）",
                },
            },
            "required": ["name"],
        }
    )
    client: ComfyUIClient | None = None
    civitai: CivitaiClient | None = None

    @staticmethod
    def _extract_trigger_words(meta: dict) -> list[str]:
        """从 safetensors 元数据提取 LoRA 触发词（ss_tag_frequency 的顶层键）。"""
        freq = meta.get("ss_tag_frequency")
        if isinstance(freq, dict):
            entries = sorted(
                ((str(k), int(v)) for k, v in freq.items() if k),
                key=lambda x: x[1],
                reverse=True,
            )
            return [k for k, _ in entries[:10]]
        return []

    async def _query_local(self, name: str) -> str:
        meta = await self.client.get_model_metadata(name)
        if not meta:
            return f"本地未找到模型 {name}，或该文件没有元数据头部（可试 source=civitai 在线搜索）。"

        parts = [f"【本地模型】{name}"]
        title = (
            meta.get("modelspec.title")
            or meta.get("ss_title")
            or meta.get("sd_models/name")
            or ""
        )
        author = meta.get("modelspec.author") or meta.get("ss_creator") or ""
        tags = meta.get("modelspec.tags") or meta.get("ss_tags") or ""
        if title:
            parts.append(f"标题: {title}")
        if author:
            parts.append(f"作者: {author}")
        if tags:
            parts.append(f"标签: {tags}")
        triggers = self._extract_trigger_words(meta)
        if triggers:
            parts.append(f"触发词: {', '.join(triggers)}")
        elif not title and not author and not tags:
            # 有元数据但都是技术字段：给个头部键名摘要
            keys = [k for k in meta if not k.startswith("ss_")]
            if keys:
                parts.append("元数据键: " + ", ".join(keys[:12]))
        return "\n".join(parts)

    @staticmethod
    def _query_civitai_items(items: list[dict], query: str) -> str:
        if not items:
            return f"civitai 未找到匹配的模型：{query}"
        parts = [f"【civitai 模型】搜索: {query}"]
        for i, item in enumerate(items[:3], 1):
            name = item.get("name") or "?"
            mtype = item.get("type") or "?"
            creator = (item.get("creator") or {}).get("username") or "?"
            stats = item.get("stats") or {}
            parts.append("")
            parts.append(f"[{i}] {name}（{mtype}）by {creator}")
            dl = stats.get("downloadCount", 0)
            like = stats.get("thumbsUpCount", 0)
            if dl or like:
                parts.append(f"下载 {dl} | 点赞 {like}")
            versions = item.get("modelVersions") or []
            if versions:
                v = versions[0]
                vname = v.get("name") or "?"
                base = v.get("baseModel") or "?"
                parts.append(f"版本: {vname}（{base}）")
                tw = [t for t in (v.get("trainedWords") or []) if t]
                if tw:
                    parts.append(f"触发词: {', '.join(tw[:10])}")
                else:
                    parts.append("触发词: 未记录")
        return "\n".join(parts)

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        name = str(kwargs.get("name") or "").strip()
        if not name:
            return "查询失败：name 不能为空。"
        source = str(kwargs.get("source") or "local").strip().lower()

        if source == "local":
            if self.client is None:
                return "查询失败：本地模型接口未配置。"
            return await self._query_local(name)

        if source == "civitai":
            if self.civitai is None:
                return "查询失败：civitai 接口未配置。"
            types = str(kwargs.get("types") or "LORA").strip()
            items = await self.civitai.search_models(name, types=types, limit=3)
            return self._query_civitai_items(items, name)

        return "查询失败：source 仅支持 local / civitai。"


@dataclass(config=ConfigDict(arbitrary_types_allowed=True))
class ComfyuiAnimadexTool(FunctionTool[AstrAgentContext]):
    """本地 AnimaDex 角色库查询（角色/画师/系列）。"""

    name: str = "comfyui_animadex"
    description: str = (
        "从本地 AnimaDex 角色库（36,000+ 动漫游戏角色，离线 SQLite）查询角色/画师/系列的"
        "规范触发词(trigger)、特征标签与关联 LoRA。"
        "本工具是系统自带 search-characters / get-character / search-artists / "
        "search-copyrights 等 MCP 工具的本地封装，二者任选其一即可，不要重复调用。"
        "用户点名作品角色（如 忍野忍/妃咲/铃兰/初音ミク）或指定画师风格时，"
        "先调用本工具查到规范触发词，再把结果填进 comfyui_generate 的 prompt/artist 参数，"
        "不要凭记忆编 tag。中文名/日文名/英文罗马音均可搜（中文会自动映射）。"
        "想拿角色详细设定/关联 LoRA 时，用 type=character 搜索后在结果里取 slug，"
        "再调 get_character 拉完整信息。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["character", "artist", "copyright"],
                    "description": "查询类型：character=角色（默认），artist=画师，copyright=系列/版权",
                },
                "query": {
                    "type": "string",
                    "description": "角色名/画师名/系列名（中文名/日文名/罗马音均可，如 妃咲、初音ミク）",
                },
                "page": {
                    "type": "number",
                    "description": "结果页码（默认 1）",
                },
            },
            "required": ["query"],
        }
    )
    client: AnimaDexClient | None = None

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        query = str(kwargs.get("query") or "").strip()
        if not query:
            return "查询失败：query 不能为空。"
        if self.client is None:
            return "查询失败：AnimaDex 客户端未配置。"
        kind = str(kwargs.get("type") or "character").strip().lower()
        if kind not in ("character", "artist", "copyright"):
            return "查询失败：type 仅支持 character / artist / copyright。"
        try:
            page = max(int(kwargs.get("page") or 1), 1)
        except (TypeError, ValueError):
            page = 1
        if kind == "artist":
            text = await self.client.search_artists(query, page=page)
        elif kind == "copyright":
            text = await self.client.search_copyrights(query, page=page)
        else:
            text = await self.client.search_characters(query, page=page)
        if not text:
            return "查询失败：无法连接本地 AnimaDex 服务（127.0.0.1:11451）。"
        parts = [f"【AnimaDex 本地角色库】{kind}: {query}"]
        parts.append(text)
        return "\n".join(parts)

@dataclass(config=ConfigDict(arbitrary_types_allowed=True))
class ComfyuiRunWorkflowTool(FunctionTool[AstrAgentContext]):
    """直接运行任意工作流 JSON（等价 MCP run_workflow，不依赖拉模板）。"""

    name: str = "comfyui_run_workflow"
    description: str = (
        "直接运行一个工作流 JSON（ComfyUI API 格式）并返回结果。"
        "等价于原生 MCP 的 run_workflow，但不依赖在线模板库。"
        "workflow 参数可以是：JSON 文件路径（本地或本机上已存在的路径）、"
        "或直接传 JSON 字符串（dict 格式，节点 id -> {class_type, inputs}）。"
        "用户有现成工作流 JSON / 想跑非内置模板的工作流时使用。"
        "生成的图片会自动下载到本地并发送到当前会话。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "workflow": {
                    "type": "string",
                    "description": "工作流 JSON：文件路径或 JSON 字符串（必填）",
                },
                "wait": {
                    "type": "boolean",
                    "description": "是否等待执行完成（默认 true；false 只提交并返回 prompt_id）",
                },
            },
            "required": ["workflow"],
        }
    )
    client: ComfyUIClient | None = None
    output_dir: Path | None = None
    shared: dict = Field(default_factory=dict)

    @staticmethod
    def _load_wf(raw: str) -> dict | None:
        raw = (raw or "").strip()
        if not raw:
            return None
        # 先当路径试
        p = Path(raw).expanduser()
        if p.is_file():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                raise ValueError(f"读取工作流文件失败: {e}") from e
        # 再当 JSON 字符串试
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"workflow 既不是有效路径也不是有效 JSON: {e}") from e
        if not isinstance(data, dict):
            raise ValueError("workflow JSON 必须是对象（节点 id -> 节点）")
        return data

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        raw = str(kwargs.get("workflow") or "").strip()
        if not raw:
            return "运行失败：workflow 不能为空。"
        try:
            wf = self._load_wf(raw)
        except ValueError as e:
            return f"运行失败：{e}"
        if wf is None:
            return "运行失败：workflow 解析失败。"

        pid, submit_err = await self.client.submit_prompt_detail(wf)
        if submit_err:
            return f"运行失败：{submit_err}"
        if not pid:
            return "运行失败：无法连接 ComfyUI。"
        self.shared["last_prompt_id"] = pid

        wait = bool(kwargs.get("wait", True))
        if not wait:
            return f"工作流已提交，prompt_id: {pid}（可稍后用 comfyui_job 查状态）"

        # 轮询
        deadline = time.time() + self.client.timeout
        while time.time() < deadline:
            entry = await self.client.get_history_entry(pid)
            if entry is not None:
                st = entry.get("status") or {}
                if st.get("status_str") == "error":
                    return f"工作流执行出错: {st.get('message') or '详见 ComfyUI 日志'}"
                outputs = entry.get("outputs", {})
                images = []
                for node_out in outputs.values():
                    images.extend(node_out.get("images", []))
                if not images:
                    return f"工作流执行完成（prompt_id: {pid}），但无图片输出。"
                img = images[0]
                filename = img["filename"]
                subfolder = img.get("subfolder", "")
                content = await self.client.download_image(filename, subfolder)
                if not content:
                    return f"工作流执行完成，图片下载失败（{filename}）。prompt_id: {pid}"
                local_path = self.output_dir / filename
                try:
                    local_path.write_bytes(content)
                except OSError as e:
                    return f"工作流执行完成但本地保存失败（{e}）。prompt_id: {pid}"
                try:
                    event: AstrMessageEvent = context.context.event
                    await event.send(MessageChain().file_image(str(local_path)))
                except Exception as e:
                    return f"工作流执行完成，图片发送失败（{e}）。本地路径: {local_path}"
                return (
                    f"工作流执行完成，图片已发送。\n"
                    f"本地路径: {local_path}\nprompt_id: {pid}"
                )
            await asyncio.sleep(2)
        return f"工作流执行超时（{int(self.client.timeout)}s）。prompt_id: {pid}"


@dataclass(config=ConfigDict(arbitrary_types_allowed=True))
class ComfyuiJobTool(FunctionTool[AstrAgentContext]):
    """按 prompt_id 查询任务状态 / 等待 / 取消（等价 MCP job）。"""

    name: str = "comfyui_job"
    description: str = (
        "按 prompt_id 查询 ComfyUI 任务状态、等待完成或取消任务。"
        "action=status：查询状态与输出；action=wait：轮询直到完成；"
        "action=cancel：中断任务；action=queue：查看当前队列。"
        "不传 prompt_id 时对最近一次由本插件提交的任务操作。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["status", "wait", "cancel", "queue"],
                    "description": "操作：status=查状态（默认），wait=等待完成，cancel=取消，queue=看队列",
                },
                "prompt_id": {
                    "type": "string",
                    "description": "任务 ID，可选；不传用最近一次生成的任务",
                },
            },
        }
    )
    client: ComfyUIClient | None = None
    shared: dict = Field(default_factory=dict)

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        action = str(kwargs.get("action") or "status").strip().lower()
        pid = str(kwargs.get("prompt_id") or "").strip() or self.shared.get("last_prompt_id", "")

        if action == "queue":
            queue = await self.client.get_queue()
            if queue is None:
                return "查询失败：无法连接 ComfyUI。"
            running = [str(item[1]) for item in queue.get("queue_running") or [] if isinstance(item, (list, tuple)) and len(item) > 1]
            pending = [str(item[1]) for item in queue.get("queue_pending") or [] if isinstance(item, (list, tuple)) and len(item) > 1]
            return f"运行中: {len(running)} | 待执行: {len(pending)}\n运行中任务: {', '.join(running) or '无'}\n待执行任务: {', '.join(pending) or '无'}"

        if not pid:
            return "查询失败：没有可用的 prompt_id（先运行一次生成，或显式传入 prompt_id）。"

        if action == "cancel":
            ok = await self.client.interrupt(prompt_id=pid)
            return "已请求取消任务 " + pid + "。" if ok else "取消失败：无法连接 ComfyUI。"

        if action == "wait":
            deadline = time.time() + self.client.timeout
            while time.time() < deadline:
                entry = await self.client.get_history_entry(pid)
                if entry is not None:
                    st = entry.get("status") or {}
                    if st.get("status_str") == "error":
                        return f"任务 {pid} 执行出错: {st.get('message') or '详见 ComfyUI 日志'}"
                    outputs = entry.get("outputs", {})
                    n = sum(len(o.get("images", [])) for o in outputs.values())
                    return f"任务 {pid} 已完成，输出图片 {n} 张。"
                await asyncio.sleep(2)
            return f"任务 {pid} 等待超时（{int(self.client.timeout)}s）。"

        entry = await self.client.get_history_entry(pid)
        if entry is None:
            # 可能还在排队/运行中
            queue = await self.client.get_queue()
            if queue is not None:
                running = [str(item[1]) for item in queue.get("queue_running") or [] if isinstance(item, (list, tuple)) and len(item) > 1]
                pending = [str(item[1]) for item in queue.get("queue_pending") or [] if isinstance(item, (list, tuple)) and len(item) > 1]
                if pid in running:
                    return f"任务 {pid} 正在运行中。"
                if pid in pending:
                    return f"任务 {pid} 在待执行队列中。"
            return f"任务 {pid} 状态未知（可能已过期或不存在）。"
        st = entry.get("status") or {}
        if st.get("status_str") == "error":
            return f"任务 {pid} 执行出错: {st.get('message') or '详见 ComfyUI 日志'}"
        outputs = entry.get("outputs", {})
        images = []
        for node_out in outputs.values():
            images.extend(node_out.get("images", []))
        if not images:
            return f"任务 {pid} 已完成，无图片输出。"
        f = images[0]
        return f"任务 {pid} 已完成。图片: {f.get('filename')}（subfolder: {f.get('subfolder', '')}）"


@dataclass(config=ConfigDict(arbitrary_types_allowed=True))
class ComfyuiFetchOutputsTool(FunctionTool[AstrAgentContext]):
    """下载 ComfyUI 输出文件到本地（等价 MCP fetch_outputs）。"""

    name: str = "comfyui_fetch_outputs"
    description: str = (
        "按 prompt_id 把 ComfyUI 生成的输出图片下载到本地，并返回本地路径。"
        "任务已完成但图片没收到、或想再拿一次输出时使用。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "prompt_id": {
                    "type": "string",
                    "description": "任务 ID（必填）",
                },
            },
            "required": ["prompt_id"],
        }
    )
    client: ComfyUIClient | None = None
    output_dir: Path | None = None

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        pid = str(kwargs.get("prompt_id") or "").strip()
        if not pid:
            return "下载失败：prompt_id 不能为空。"
        entry = await self.client.get_history_entry(pid)
        if entry is None:
            return f"任务 {pid} 未找到（可能未完成或不存在）。"
        st = entry.get("status") or {}
        if st.get("status_str") == "error":
            return f"任务 {pid} 执行出错: {st.get('message') or '详见 ComfyUI 日志'}"
        outputs = entry.get("outputs", {})
        images = []
        for node_out in outputs.values():
            images.extend(node_out.get("images", []))
        if not images:
            return f"任务 {pid} 无图片输出。"
        saved = []
        for img in images:
            filename = img["filename"]
            subfolder = img.get("subfolder", "")
            content = await self.client.download_image(filename, subfolder)
            if not content:
                continue
            local_path = self.output_dir / filename
            try:
                local_path.write_bytes(content)
                saved.append(str(local_path))
            except OSError:
                continue
        if not saved:
            return f"任务 {pid} 图片全部下载失败（链路问题？）。"
        return "已下载输出图片:\n" + "\n".join(saved)


@dataclass(config=ConfigDict(arbitrary_types_allowed=True))
class ComfyuiSystemStatsTool(FunctionTool[AstrAgentContext]):
    """查询 ComfyUI 系统/显存状态（等价 MCP system_stats）。"""

    name: str = "comfyui_system_stats"
    description: str = (
        "查询 ComfyUI 的系统状态：设备（GPU）、显存占用、系统内存。"
        "用户想知道显存占用、能不能跑大图、为什么变慢时使用。"
    )
    parameters: dict = Field(default_factory=lambda: {"type": "object", "properties": {}})
    client: ComfyUIClient | None = None

    @staticmethod
    def _gb(n: int | float | None) -> str:
        try:
            return f"{n / (1024 ** 3):.1f}GB"
        except (TypeError, ValueError):
            return "?"

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        stats = await self.client.get_system_stats()
        if stats is None:
            return "查询失败：无法连接 ComfyUI。"
        parts = ["【ComfyUI 系统状态】"]
        sysinfo = stats.get("system", {})
        if sysinfo:
            os_ = sysinfo.get("os", "")
            parts.append(f"系统: {os_}")
            ram_total = sysinfo.get("ram_total")
            ram_free = sysinfo.get("ram_free")
            if ram_total:
                parts.append(f"内存: 总 {self._gb(ram_total)} / 空闲 {self._gb(ram_free)}")
        for dev in stats.get("devices", []):
            name = dev.get("name", "?")
            parts.append(f"设备: {name}")
            vr = dev.get("vram_total")
            vf = dev.get("vram_free")
            if vr:
                parts.append(f"显存: 总 {self._gb(vr)} / 空闲 {self._gb(vf)}")
        return "\n".join(parts)


@dataclass(config=ConfigDict(arbitrary_types_allowed=True))
class ComfyuiFreeMemoryTool(FunctionTool[AstrAgentContext]):
    """释放 ComfyUI 显存（等价 MCP free_memory）。"""

    name: str = "comfyui_free_memory"
    description: str = (
        "请求 ComfyUI 卸载模型/清空执行器缓存以释放显存。"
        "显存不足跑不动、或想腾出显存跑大图时使用。"
        "不影响正在运行的任务（下次队列迭代时生效）。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "unload_models": {
                    "type": "boolean",
                    "description": "是否卸载模型（默认 true）",
                },
            },
        }
    )
    client: ComfyUIClient | None = None

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        unload = bool(kwargs.get("unload_models", True))
        ok = await self.client.free_memory(unload_models=unload, free_cache=True)
        return "已请求释放显存（卸载模型+清缓存）。" if ok else "释放失败：无法连接 ComfyUI。"


@dataclass(config=ConfigDict(arbitrary_types_allowed=True))
class ComfyuiNodesTool(FunctionTool[AstrAgentContext]):
    """查询 ComfyUI 节点类信息（等价 MCP nodes）。"""

    name: str = "comfyui_nodes"
    description: str = (
        "查询 ComfyUI 节点类（class_type）信息：action=search 按关键词搜节点类，"
        "action=get 查某个节点类的输入/输出 schema，action=list 列出全部节点类。"
        "写自定义工作流、确认某个节点类是否存在/参数名时使用。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["search", "get", "list"],
                    "description": "search=搜索（默认），get=查单个类 schema，list=全部节点类",
                },
                "query": {
                    "type": "string",
                    "description": "搜索关键词（action=search 时必填）或节点类名（action=get 时必填）",
                },
            },
        }
    )
    client: ComfyUIClient | None = None

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        action = str(kwargs.get("action") or "search").strip().lower()
        query = str(kwargs.get("query") or "").strip()
        obj = await self.client.get_object_info()
        if not obj:
            return "查询失败：无法连接 ComfyUI。"
        if action == "list":
            names = sorted(obj.keys())
            return "全部节点类 (" + str(len(names)) + "):\n" + ", ".join(names)
        if action == "get":
            if not query:
                return "查询失败：query（节点类名）不能为空。"
            info = obj.get(query)
            if not info:
                return f"节点类不存在: {query}"
            inp = info.get("input", {})
            req = inp.get("required", {})
            opt = inp.get("optional", {})
            parts = [f"【节点类 {query}】"]
            if req:
                parts.append("必填输入:")
                for k, v in req.items():
                    types = v[0] if isinstance(v, list) and v else "?"
                    parts.append(f"  {k}: {types}")
            if opt:
                parts.append("可选输入:")
                for k, v in opt.items():
                    types = v[0] if isinstance(v, list) and v else "?"
                    parts.append(f"  {k}: {types}")
            return "\n".join(parts)
        if not query:
            return "查询失败：query（关键词）不能为空。"
        hits = [k for k in obj if query.lower() in k.lower()]
        if not hits:
            return f"未找到包含 '{query}' 的节点类。"
        return "匹配节点类:\n" + "\n".join(sorted(hits)[:50])


@dataclass(config=ConfigDict(arbitrary_types_allowed=True))
class ComfyuiValidateWorkflowTool(FunctionTool[AstrAgentContext]):
    """校验工作流 JSON 的节点类是否存在于 ComfyUI（等价 MCP validate_workflow 本地版）。"""

    name: str = "comfyui_validate_workflow"
    description: str = (
        "提交前校验一个工作流 JSON：检查节点 class_type 是否都存在、"
        "必填输入是否齐全。返回 valid 与错误列表。"
        "写自定义工作流、担心提交 400 时先用它检查。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "workflow": {
                    "type": "string",
                    "description": "工作流 JSON：文件路径或 JSON 字符串（必填）",
                },
            },
            "required": ["workflow"],
        }
    )
    client: ComfyUIClient | None = None

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        raw = str(kwargs.get("workflow") or "").strip()
        if not raw:
            return "校验失败：workflow 不能为空。"
        try:
            wf = ComfyuiRunWorkflowTool._load_wf(raw)
        except ValueError as e:
            return f"校验失败：{e}"
        if wf is None:
            return "校验失败：workflow 解析失败。"
        obj = await self.client.get_object_info()
        if not obj:
            return "校验失败：无法连接 ComfyUI。"
        errors = []
        for nid, node in wf.items():
            ct = node.get("class_type")
            if not ct:
                errors.append(f"节点 {nid}: 缺少 class_type")
                continue
            info = obj.get(ct)
            if not info:
                errors.append(f"节点 {nid}: 节点类不存在 {ct}")
                continue
            req = info.get("input", {}).get("required", {})
            ins = node.get("inputs", {})
            for k in req:
                if k not in ins:
                    errors.append(f"节点 {nid} ({ct}): 缺少必填输入 {k}")
        if errors:
            return "校验结果: invalid\n" + "\n".join(errors[:20])
        return "校验结果: valid（全部节点类与必填输入均通过）"


@dataclass(config=ConfigDict(arbitrary_types_allowed=True))
class ComfyuiUploadFileTool(FunctionTool[AstrAgentContext]):
    """上传图片到 ComfyUI input 目录（等价 MCP upload_file，图生图素材）。"""

    name: str = "comfyui_upload_file"
    description: str = (
        "把本地图片上传到 ComfyUI 的 input 目录，返回文件名，供图生图/ControlNet 工作流引用。"
        "已有本地图片文件、需要作为生成素材时使用。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "本地图片文件路径（必填）",
                },
            },
            "required": ["path"],
        }
    )
    client: ComfyUIClient | None = None

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        path = str(kwargs.get("path") or "").strip()
        if not path:
            return "上传失败：path 不能为空。"
        p = Path(path).expanduser()
        if not p.is_file():
            return f"上传失败：文件不存在 {p}"
        try:
            content = p.read_bytes()
        except OSError as e:
            return f"上传失败：读取文件出错（{e}）"
        name, err = await self.client.upload_image(p.name, content)
        if err:
            return f"上传失败：{err}"
        return f"上传成功，ComfyUI 文件名: {name}"


@dataclass(config=ConfigDict(arbitrary_types_allowed=True))
class ComfyuiModelsSearchTool(FunctionTool[AstrAgentContext]):
    """搜索 ComfyUI 已安装的模型文件（等价 MCP search_models 本地版）。"""

    name: str = "comfyui_models_search"
    description: str = (
        "按目录列出/搜索 ComfyUI 已安装的模型文件。folder 如 checkpoints/loras/clip/vae/unet。"
        "用户想确认某模型是否已安装、文件名是什么时使用。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "folder": {
                    "type": "string",
                    "description": "模型目录：checkpoints / loras / clip / vae / unet（默认 loras）",
                },
                "query": {
                    "type": "string",
                    "description": "文件名关键词过滤（可选）",
                },
            },
        }
    )
    client: ComfyUIClient | None = None

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        folder = str(kwargs.get("folder") or "loras").strip().lower()
        query = str(kwargs.get("query") or "").strip().lower()
        names = await self.client.list_models_folder(folder)
        if names is None:
            return f"查询失败：无法连接 ComfyUI 或目录 {folder} 不存在。"
        if query:
            names = [n for n in names if query in n.lower()]
        if not names:
            return f"{folder} 下未找到匹配模型。"
        return f"【{folder}】模型 ({len(names)}):\n" + "\n".join(sorted(names))


@dataclass(config=ConfigDict(arbitrary_types_allowed=True))
class ComfyuiRecipeTool(FunctionTool[AstrAgentContext]):
    """自研：保存/读取/列出生成配方（prompt + 全部参数），可一键复现。"""

    name: str = "comfyui_recipe"
    description: str = (
        "把一次生成的全部参数（prompt/artist/quality/trigger_words/negative_prompt/model/lora/"
        "steps/cfg/sampler/scheduler/denoise/width/height/seed/workflow）保存为命名配方，"
        "之后可以列出、读取、删除。配方存本地 JSON 文件。"
        "用户想复现某张图、保存常用风格参数时使用。"
        "action=save（保存，需 name + 至少 prompt）/ action=list（列出）/ "
        "action=load（读取一个配方，返回全部参数）/ action=delete（删除）。"
        "load 出的参数可直接传给 comfyui_generate 复现。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["save", "list", "load", "delete"],
                    "description": "save=保存（默认），list=列出，load=读取，delete=删除",
                },
                "name": {
                    "type": "string",
                    "description": "配方名（save/load/delete 必填）",
                },
                "prompt": {"type": "string", "description": "主提示词（save 时必填）"},
                "artist": {"type": "string", "description": "画师串"},
                "quality": {"type": "string", "description": "质量词"},
                "trigger_words": {"type": "string", "description": "lora触发词"},
                "negative_prompt": {"type": "string", "description": "负向提示词"},
                "model": {"type": "string", "description": "底模文件名"},
                "lora": {"type": "string", "description": "LoRA 覆盖 JSON"},
                "steps": {"type": "number", "description": "采样步数"},
                "cfg": {"type": "number", "description": "CFG"},
                "sampler_name": {"type": "string", "description": "采样器"},
                "scheduler": {"type": "string", "description": "调度器"},
                "denoise": {"type": "number", "description": "降噪强度"},
                "width": {"type": "number", "description": "宽度"},
                "height": {"type": "number", "description": "高度"},
                "seed": {"type": "number", "description": "种子"},
                "workflow": {"type": "string", "description": "工作流模板名"},
            },
        }
    )
    recipe_dir: Path | None = None

    def _path(self, name: str) -> Path:
        safe = "".join(c for c in name if c.isalnum() or c in "-_")
        if not safe or safe != name:
            raise ValueError(f"配方名不合法: {name}（仅允许字母数字-_）")
        return self.recipe_dir / f"{safe}.json"

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        action = str(kwargs.get("action") or "save").strip().lower()
        name = str(kwargs.get("name") or "").strip()
        if action not in ("save", "list", "load", "delete"):
            return "操作失败：action 仅支持 save/list/load/delete。"

        if action == "list":
            if not self.recipe_dir.is_dir():
                return "没有已保存的配方。"
            files = sorted(self.recipe_dir.glob("*.json"))
            if not files:
                return "没有已保存的配方。"
            parts = ["【已保存配方】"]
            for f in files:
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    p = (data.get("prompt") or "")[:40]
                    parts.append(f"- {f.stem}: {p}...")
                except (OSError, json.JSONDecodeError):
                    continue
            return "\n".join(parts)

        if not name:
            return "操作失败：name 不能为空。"

        if action == "delete":
            p = self._path(name)
            if p.is_file():
                p.unlink()
                return f"已删除配方: {name}"
            return f"配方不存在: {name}"

        if action == "load":
            p = self._path(name)
            if not p.is_file():
                return f"配方不存在: {name}"
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                return f"读取配方失败: {e}"
            return "配方参数:\n" + json.dumps(data, ensure_ascii=False, indent=2)

        # save
        prompt = str(kwargs.get("prompt") or "").strip()
        if not prompt:
            return "保存失败：prompt 不能为空。"
        keys = (
            "artist", "quality", "trigger_words", "negative_prompt", "model",
            "lora", "steps", "cfg", "sampler_name", "scheduler", "denoise",
            "width", "height", "seed", "workflow",
        )
        data = {"name": name, "prompt": prompt}
        for k in keys:
            v = kwargs.get(k)
            if v is not None and v != "":
                data[k] = v
        try:
            p = self._path(name)
            self.recipe_dir.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except (ValueError, OSError) as e:
            return f"保存失败：{e}"
        return f"配方已保存: {name}"


_DRAW_DESC = (
    "给用户画一张图，画完直接发到当前会话。"
    "用户只说「画xxx」时：只填 prompt，其他一律不填，用默认配方。"
    "用户点名某套配方/画风（如立绘、写实）→填 recipe。"
    "用户点名底模/LoRA→填 model / lora，可用关键词，不要编文件名；"
    "拿不准就先 comfyui_lookup。"
    "用户要竖图/横图/方图→填 size=portrait/landscape/square。"
    "用户点名画师/画质/不要出现的东西/触发词→填对应字段。"
    "用户要更精细或更快→才填 steps 或 cfg。"
    "用户说记住这套/存成某某→填 save_as。"
    "没点名的参数绝对不要填、不要编造。"
)


def _recipe_enum(store: RecipeStore | None) -> list[str]:
    if store is None:
        return []
    try:
        return store.names()
    except Exception:
        return []


def _match_resource(names: list[str], query: str, limit: int = 8) -> list[str]:
    q = str(query or "").strip().lower().replace("\\", "/")
    if not q or not names:
        return []
    q_base = q.split("/")[-1]
    exact = []
    for n in names:
        base = n.replace("\\", "/").split("/")[-1].lower()
        if n.lower() == q or base == q_base:
            exact.append(n)
    if exact:
        return exact[:1]
    return [n for n in names if q in n.lower() or q_base in n.replace("\\", "/").split("/")[-1].lower()][:limit]


@dataclass(config=ConfigDict(arbitrary_types_allowed=True))
class ComfyuiDrawTool(FunctionTool[AstrAgentContext]):
    """按配方生图。节点由配置下拉框指定；底模/LoRA 用配方或本次覆盖。"""

    name: str = "comfyui_draw"
    description: str = _DRAW_DESC
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "要画的内容。写成 danbooru 风格 tag，必填",
                },
                "recipe": {
                    "type": "string",
                    "description": "用哪套配方。用户没点名就不要填",
                },
                "model": {
                    "type": "string",
                    "description": "换底模。用户没点名就不要填。关键词或文件名",
                },
                "lora": {
                    "type": "string",
                    "description": "换 LoRA。用户没点名就不要填。关键词、文件名，多个用逗号",
                },
                "size": {
                    "type": "string",
                    "enum": ["portrait", "landscape", "square", "same"],
                    "description": "portrait竖图 landscape横图 square方图。用户没提画幅就不要填",
                },
                "artist": {
                    "type": "string",
                    "description": "画师风格。用户没点名画师就不要填",
                },
                "quality": {
                    "type": "string",
                    "description": "画质词。用户没要求画质就不要填",
                },
                "negative": {
                    "type": "string",
                    "description": "不要出现的东西。用户没说就不要填",
                },
                "trigger_words": {
                    "type": "string",
                    "description": "LoRA 触发词。用户没提或 lookup 没给就不要填",
                },
                "steps": {
                    "type": "number",
                    "description": "采样步数。用户说更精细/更快/改步数才填",
                },
                "cfg": {
                    "type": "number",
                    "description": "CFG。用户明确说改才填",
                },
                "seed": {
                    "type": "number",
                    "description": "种子。用户要复现某张图才填，否则不填",
                },
                "save_as": {
                    "type": "string",
                    "description": "用户说记住这套/存成某某时，填新配方名",
                },
            },
            "required": ["prompt"],
        }
    )
    client: ComfyUIClient | None = None
    builder: WorkflowBuilder | None = None
    store: RecipeStore | None = None
    output_dir: Path | None = None
    shared: dict = Field(default_factory=dict)
    config_defaults: dict = Field(default_factory=dict)
    on_schema_change: Any = None

    def refresh_schema(self) -> None:
        names = _recipe_enum(self.store)
        catalog = ""
        if self.store is not None:
            try:
                catalog = self.store.catalog()
            except Exception:
                catalog = ""
        self.description = _DRAW_DESC + (
            f" 现有配方：{catalog}" if catalog else " 还没有配方，先让主人在工作台保存一套。"
        )
        props = self.parameters.setdefault("properties", {})
        recipe_prop = props.setdefault("recipe", {"type": "string"})
        if names:
            recipe_prop["enum"] = names
            recipe_prop["description"] = "用户点名时才填。可选：" + "、".join(names[:16])
        else:
            recipe_prop.pop("enum", None)
            recipe_prop["description"] = "用户点名时才填。没有配方就不要填"

    async def _resource_lists(self) -> dict:
        if self.client is None:
            return {"unet_name": [], "lora_name": []}
        resources, _ = await self.client.list_resources()
        return resources

    async def _resolve_model(self, query: str) -> tuple[str | None, str | None]:
        names = (await self._resource_lists()).get("unet_name") or []
        hits = _match_resource(names, query)
        if len(hits) == 1:
            return hits[0], None
        if not hits:
            return None, f"没找到叫「{query}」的底模。可以再搜一下，或让用户说完整文件名。"
        preview = "、".join(hits[:6])
        return None, f"「{query}」对上了好几份底模：{preview}。请让用户选一个，或填更完整的名字。"

    async def _resolve_loras(self, raw: Any) -> tuple[list[dict] | None, str | None]:
        names = (await self._resource_lists()).get("lora_name") or []
        try:
            parsed = parse_lora(raw)
        except ValueError:
            parsed = [{"name": part.strip()} for part in str(raw).split(",") if part.strip()]
        if not parsed:
            return [], None
        resolved: list[dict] = []
        for item in parsed:
            query = str(item.get("name") or "").strip()
            if not query:
                continue
            hits = _match_resource(names, query)
            if len(hits) == 1:
                spec = dict(item)
                spec["name"] = hits[0]
                resolved.append(spec)
                continue
            if not hits:
                return None, f"没找到叫「{query}」的 LoRA。可以再搜一下，或让用户说完整文件名。"
            preview = "、".join(hits[:6])
            return None, f"「{query}」对上了好几份 LoRA：{preview}。请让用户选一个，或填更完整的名字。"
        return resolved, None

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        prompt = str(kwargs.get("prompt") or "").strip()
        if not prompt:
            return "生成失败：prompt 不能为空。"
        if self.store is None or self.builder is None or self.client is None:
            return "生成失败：插件未初始化完成。"

        recipe_name = str(kwargs.get("recipe") or "").strip()
        recipe = self.store.get(recipe_name) if recipe_name else self.store.default()
        if recipe is None:
            if recipe_name:
                return f"没有叫「{recipe_name}」的配方。先跟用户说现有配方名，或请主人在配方工作台里新建一套。"
            return "还没有配方。请主人先在配方工作台：导入工作流 → 确认「用户要画的内容」和「出图采样」→ 保存。"

        slots = recipe.get("slots") or {}
        if not slots.get("prompt"):
            return "这套配方还没指定「用户要画的内容」写到哪。请主人在工作台或配置下拉框里选一下。"

        seed = kwargs.get("seed")
        if seed is None:
            seed = random.randint(0, 2**31 - 1)

        model_raw = str(kwargs.get("model") or "").strip()
        lora_raw = kwargs.get("lora") if kwargs.get("lora") not in (None, "") else kwargs.get("loras")
        resolved_model = None
        resolved_loras = None
        if model_raw:
            resolved_model, err = await self._resolve_model(model_raw)
            if err:
                return err
            if not slots.get("model"):
                return "这套配方还没指定底模格子，换不了模型。请主人在工作台里选一下「底模」。"
        if lora_raw not in (None, ""):
            resolved_loras, err = await self._resolve_loras(lora_raw)
            if err:
                return err
            if not slots.get("loras"):
                return "这套配方还没指定 LoRA 格子，换不了 LoRA。请主人在工作台里选一下「LoRA」。"

        if kwargs.get("steps") not in (None, "") or kwargs.get("cfg") not in (None, ""):
            if not slots.get("sampler"):
                return "这套配方还没指定出图采样，改不了步数。请主人在工作台里选一下「出图采样」。"

        # LLM 没传 trigger_words 但传了 lora 时，从 lora_meta 自动查触发词
        auto_trigger = None
        if resolved_loras and not kwargs.get("trigger_words"):
            auto_trigger = await _auto_fill_trigger_words(self.client, resolved_loras)

        overrides = {
            "prompt": prompt,
            "seed": seed,
            "artist": kwargs.get("artist"),
            "quality": kwargs.get("quality"),
            "trigger_words": kwargs.get("trigger_words") or auto_trigger,
            "negative": kwargs.get("negative_prompt") or kwargs.get("negative"),
            "model": resolved_model,
            "loras": resolved_loras,
            "steps": kwargs.get("steps"),
            "cfg": kwargs.get("cfg"),
        }
        values = materialize_values(recipe, overrides)
        for key, val in self.config_defaults.items():
            if key not in values and val not in (None, "", 0, 0.0, []):
                values[key] = val

        size_token = str(kwargs.get("size") or "").strip()
        if size_token:
            values["width"], values["height"] = resolve_size(
                int(values["width"]) if values.get("width") else None,
                int(values["height"]) if values.get("height") else None,
                size_token,
            )

        try:
            wf = self.builder.load_template(recipe.get("workflow") or None)
        except FileNotFoundError as e:
            return f"生成失败：{e}"

        apply_slots(
            wf,
            slots,
            values,
            prefix=f"astrbot_{uuid.uuid4().hex[:8]}",
            drop_nodes=list(recipe.get("drop_nodes") or []),
        )

        pid, submit_err = await self.client.submit_prompt_detail(wf)
        if submit_err:
            return f"生成失败：{submit_err}"
        if not pid:
            return "生成失败：无法连接 ComfyUI。"
        self.shared["last_prompt_id"] = pid

        outputs, wait_err = await _wait_outputs(self.client, pid)
        if wait_err:
            return f"生成失败：{wait_err}"
        if outputs is None:
            return "生成失败：未获取到执行结果。"

        images = []
        for node_out in outputs.values():
            images.extend(node_out.get("images", []))
        if not images:
            return "生成完成，但没有输出图片。"

        img = images[0]
        filename = img["filename"]
        content = await self.client.download_image(filename, img.get("subfolder", ""))
        if not content:
            return f"图片已生成但下载失败（{filename}）。"

        local_path = self.output_dir / filename
        try:
            local_path.write_bytes(content)
        except OSError as e:
            return f"图片已生成但本地保存失败（{e}）。"

        used = {
            "model": values.get("model"),
            "loras": values.get("loras") or values.get("lora"),
            "width": values.get("width"),
            "height": values.get("height"),
            "steps": values.get("steps"),
            "cfg": values.get("cfg"),
            "sampler_name": values.get("sampler_name"),
            "scheduler": values.get("scheduler"),
            "denoise": values.get("denoise"),
            "seed": seed,
        }
        self.store.save_history(
            {
                "prompt_id": pid,
                "recipe": recipe.get("name"),
                "recipe_id": recipe.get("id"),
                "workflow": recipe.get("workflow"),
                "slots": slots,
                "drop_nodes": recipe.get("drop_nodes") or [],
                "prompt": prompt,
                "values": {k: v for k, v in used.items() if v not in (None, "", [])},
                "filename": filename,
                "local_path": str(local_path),
            }
        )

        save_as = str(kwargs.get("save_as") or "").strip()
        saved_note = ""
        if save_as:
            try:
                self.store.save(
                    {
                        "name": save_as,
                        "description": f"从 {recipe.get('name')} 另存",
                        "workflow": recipe.get("workflow"),
                        "slots": slots,
                        "defaults": {k: v for k, v in used.items() if k != "seed" and v not in (None, "", [])},
                        "drop_nodes": recipe.get("drop_nodes") or [],
                    }
                )
                self.refresh_schema()
                saved_note = f" 已另存配方 {save_as}。"
            except ValueError as e:
                saved_note = f" 另存配方失败：{e}"

        try:
            event: AstrMessageEvent = context.context.event
            await event.send(MessageChain().file_image(str(local_path)))
        except Exception as e:
            logger.error(f"[ComfyUIDirect] 图片发送失败: {e}")
            return f"图片已生成但发送失败（{e}）。路径: {local_path}"

        w, h = used.get("width") or "?", used.get("height") or "?"
        model_note = used.get("model") or "配方原底模"
        lora_items = used.get("loras") or []
        if isinstance(lora_items, list) and lora_items:
            lora_note = ",".join(
                str(x.get("name") if isinstance(x, dict) else x) for x in lora_items[:4]
            )
        else:
            lora_note = "配方原 LoRA"
        return (
            f"已发送。配方={recipe.get('name')} 底模={model_note} "
            f"lora={lora_note} seed={seed} size={w}x{h}.{saved_note}"
        )


@dataclass(config=ConfigDict(arbitrary_types_allowed=True))
class ComfyuiLookupTool(FunctionTool[AstrAgentContext]):
    """查角色/画师/LoRA 触发词，短回包。"""

    name: str = "comfyui_lookup"
    description: str = (
        "查规范词或已安装的文件名。用户点名角色/画师/底模/LoRA，你又不确定时再用。"
        "character/artist：把触发词写进 prompt 或 artist。"
        "model/lora：把返回的文件名填进 comfyui_draw。"
        "不要自己编 tag 或文件名。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["character", "artist", "model", "lora"],
                    "description": "character=角色，artist=画师，model=底模文件名，lora=LoRA 文件名与触发词",
                },
                "query": {
                    "type": "string",
                    "description": "名字（中文/日文/罗马音均可）",
                },
            },
            "required": ["type", "query"],
        }
    )
    danbooru: DanbooruClient | None = None
    gelbooru: GelbooruClient | None = None
    animadex: AnimaDexClient | None = None
    client: ComfyUIClient | None = None

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        kind = str(kwargs.get("type") or "").strip().lower()
        query = str(kwargs.get("query") or "").strip()
        if kind not in ("character", "artist", "model", "lora") or not query:
            return "查询失败：需要 type（character/artist/model/lora）和 query。"

        if kind == "lora":
            return await self._lookup_lora(query)
        if kind == "model":
            return await self._lookup_model(query)

        if kind == "character" and self.animadex is not None:
            text = await self.animadex.search_characters(query, page=1)
            if text:
                clipped = text.strip()
                if len(clipped) > 800:
                    clipped = clipped[:800] + "…"
                return f"【角色 {query}】\n{clipped}"

        if kind == "artist":
            data = None
            source = "danbooru"
            if self.danbooru is not None:
                data = await self.danbooru.search_artist(query, 20)
            if data is None and self.gelbooru is not None:
                data = await self.gelbooru.search_artist(query, 20)
                source = "gelbooru"
            if data is None:
                return f"未找到画师：{query}"
            aliases = ", ".join((data.get("aliases") or [])[:6])
            trigger = data.get("artist") or query
            extra = f" 别名: {aliases}" if aliases else ""
            return f"【画师 @{trigger}】来源 {source}{extra}\n触发词: @{trigger}"

        data = None
        source = "danbooru"
        if self.danbooru is not None:
            data = await self.danbooru.search_character(query, 20)
        if data is None and self.gelbooru is not None:
            data = await self.gelbooru.search_character(query, 20)
            source = "gelbooru"
        if data is None:
            return f"未找到角色：{query}"
        name = data.get("character") or query
        aliases = ", ".join((data.get("aliases") or [])[:8])
        extra = f"\n别名: {aliases}" if aliases else ""
        return f"【角色 {name}】来源 {source}{extra}\n触发词: {name}"

    async def _lookup_model(self, query: str) -> str:
        if self.client is None:
            return "查询失败：ComfyUI 未配置。"
        resources, _ = await self.client.list_resources()
        hits = _match_resource(resources.get("unet_name") or [], query)
        if not hits:
            return f"未找到匹配底模：{query}"
        return "【底模】把下面的文件名填进 comfyui_draw 的 model：\n" + "\n".join(hits)

    async def _lookup_lora(self, query: str) -> str:
        if self.client is None:
            return "查询失败：ComfyUI 未配置。"
        resources, _ = await self.client.list_resources()
        names = resources.get("lora_name") or []
        meta = resources.get("lora_meta") or {}
        hits = _match_resource(names, query)
        if not hits:
            return f"未找到匹配 LoRA：{query}"
        lines = ["【LoRA】把文件名填进 comfyui_draw 的 lora："]
        for name in hits:
            info = meta.get(name) or {}
            triggers = info.get("trigger_words") or []
            if triggers:
                lines.append(f"{name}\n  触发词: {', '.join(triggers[:8])}")
            else:
                lines.append(name)
        return "\n".join(lines)


async def _auto_fill_trigger_words(client: ComfyUIClient, lora_input: Any) -> str | None:
    """从 lora_meta 缓存中按 LoRA 文件名查触发词，拼成逗号分隔串返回。

    lora_input 可以是 parse_lora 后的 list[dict]，也可以是原始 JSON 字符串/数组。
    如果全部 LoRA 都没缓存到触发词，返回 None（保持原行为）。
    """
    if client is None:
        return None
    # 先尝试解析出文件名列表
    try:
        parsed = parse_lora(lora_input)
    except Exception:
        parsed = []
    if not parsed:
        # 可能是原始字符串，尝试直接 parse
        try:
            parsed = parse_lora(str(lora_input))
        except Exception:
            return None
    if not parsed:
        return None
    names = [str(item.get("name") or "").strip() for item in parsed if item.get("name")]
    if not names:
        return None
    try:
        resources, _ = await client.list_resources()
    except Exception:
        return None
    lora_meta = resources.get("lora_meta") or {}
    all_triggers: list[str] = []
    for name in names:
        info = lora_meta.get(name)
        if info and info.get("trigger_words"):
            all_triggers.extend(info["trigger_words"][:8])
    if not all_triggers:
        return None
    # 去重保序
    seen: set[str] = set()
    unique: list[str] = []
    for t in all_triggers:
        t = t.strip()
        if t and t not in seen:
            seen.add(t)
            unique.append(t)
    return ", ".join(unique) if unique else None


async def _wait_outputs(client: ComfyUIClient, prompt_id: str) -> tuple[dict | None, str | None]:
    deadline = time.time() + client.timeout
    miss = 0
    while time.time() < deadline:
        entry = await client.get_history_entry(prompt_id)
        if entry is not None:
            st = entry.get("status") or {}
            if st.get("status_str") == "error":
                return None, st.get("message") or "执行出错（详见 ComfyUI 日志）"
            return entry.get("outputs", {}), None
        miss += 1
        if miss == 5:
            logger.warning(
                f"[ComfyUIDirect] 轮询 {prompt_id} 连续 {miss} 次无响应，继续等待"
            )
        await asyncio.sleep(2)
    return None, f"生成超时（{int(client.timeout)}s）"
