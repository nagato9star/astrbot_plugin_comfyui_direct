"""LLM 工具定义（dataclass FunctionTool 模式，v4.5.7+ 推荐）。

comfyui_draw：按配方生图，可按画面需求选用已安装 LoRA（默认启用）
comfyui_lookup：查询角色/画师/底模/LoRA，支持按用途选择 LoRA（默认启用）
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
import math
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
from comfy_client import ComfyUIClient, safe_output_path
from external_search import CivitaiClient, DanbooruClient, GelbooruClient
from recipe_store import (
    RecipeStore,
    materialize_values,
    model_family,
    recipe_family,
    recipe_template,
    resolve_generation_entry,
)
from slot_mapping import apply_slots, collect_trigger_words, parse_lora, resolve_size
from workflow_builder import WorkflowBuilder

MAX_LLM_LIST_ITEMS = 30
MAX_LLM_DETAIL_ITEMS = 8


def _as_bool(value: Any, default: bool = False) -> bool:
    """Parse tool booleans safely when a provider sends JSON values as text."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "y"}:
        return True
    if text in {"0", "false", "no", "off", "n", ""}:
        return False
    return default


def _bounded_limit(value: Any, default: int = MAX_LLM_LIST_ITEMS) -> int:
    try:
        return min(max(int(value), 1), 50)
    except (TypeError, ValueError):
        return default


def _number(value: Any, label: str, *, integer: bool = False,
            minimum: float | None = None, maximum: float | None = None) -> int | float:
    """Validate a generation number and return a plain finite int/float."""
    try:
        number = float(value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{label} 必须是数字") from e
    if not math.isfinite(number):
        raise ValueError(f"{label} 不能是 NaN 或无穷大")
    if integer and not number.is_integer():
        raise ValueError(f"{label} 必须是整数")
    if minimum is not None and number < minimum:
        raise ValueError(f"{label} 不能小于 {minimum:g}")
    if maximum is not None and number > maximum:
        raise ValueError(f"{label} 不能大于 {maximum:g}")
    return int(number) if integer else number


def _validate_generation_values(values: dict[str, Any]) -> dict[str, Any]:
    """Normalize numeric generation values before workflow mutation."""
    result = dict(values)
    limits = {
        "seed": (True, 0, 2**63 - 1),
        "steps": (True, 1, 1000),
        "cfg": (False, 0, 100),
        "denoise": (False, 0, 1),
        "width": (True, 64, 8192),
        "height": (True, 64, 8192),
    }
    for key, (integer, minimum, maximum) in limits.items():
        value = result.get(key)
        if value in (None, ""):
            continue
        result[key] = _number(
            value,
            key,
            integer=integer,
            minimum=minimum,
            maximum=maximum,
        )
    return result


def _event_scope(context: ContextWrapper[AstrAgentContext]) -> str:
    """Return a conversation scope for per-session task state."""
    event = getattr(getattr(context, "context", None), "event", None)
    if event is None:
        return "global"
    umo = getattr(event, "unified_msg_origin", None)
    if umo:
        return str(umo)
    get_session_id = getattr(event, "get_session_id", None)
    if callable(get_session_id):
        try:
            value = get_session_id()
            if value:
                return str(value)
        except Exception:
            pass
    try:
        sender = event.get_sender_id()
        if sender:
            return f"sender:{sender}"
    except Exception:
        pass
    return "global"


def _remember_prompt_id(
    shared: dict, context: ContextWrapper[AstrAgentContext], prompt_id: str
) -> None:
    """Keep the most recent task per conversation, not one global task."""
    by_scope = shared.setdefault("last_prompt_ids", {})
    by_scope[_event_scope(context)] = str(prompt_id)


def _last_prompt_id(shared: dict, context: ContextWrapper[AstrAgentContext]) -> str:
    by_scope = shared.get("last_prompt_ids") or {}
    return str(by_scope.get(_event_scope(context)) or "")


def _usage_tips_text(value: Any) -> str:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return value.strip()[:180]
    if not isinstance(value, dict):
        return ""
    labels = (
        ("strength", "建议权重"),
        ("strength_range", "权重范围"),
        ("clip_strength", "CLIP权重"),
        ("clip_skip", "CLIP跳过层"),
    )
    return ", ".join(
        f"{label}={value[key]}" for key, label in labels if value.get(key) not in (None, "")
    )


def _lora_info_summary(info: dict, detailed: bool = False) -> list[str]:
    """Render the compact normalized LoRA record for an LLM response."""
    lines: list[str] = []
    categories = info.get("categories") or []
    tags = info.get("tags") or []
    if categories:
        lines.append("类别: " + ", ".join(str(x) for x in categories[:8]))
    if tags and detailed:
        lines.append("标签: " + ", ".join(str(x) for x in tags[:16]))
    if info.get("model_name") and detailed:
        lines.append("Civitai名称: " + str(info["model_name"]))
    if info.get("base_model"):
        lines.append("基础模型: " + str(info["base_model"]))
    if detailed and info.get("description"):
        lines.append("用途说明: " + str(info["description"]))
    tips = _usage_tips_text(info.get("usage_tips"))
    if tips:
        lines.append("使用建议: " + tips)
    if info.get("trigger_words"):
        lines.append("触发词: " + ", ".join(str(x) for x in info["trigger_words"][:12]))
    if detailed and info.get("notes"):
        lines.append("备注: " + str(info["notes"]))
    return lines


def _match_lora_resources(
    names: list[str], metadata: dict[str, dict], query: str, limit: int = 8
) -> list[str]:
    """Match LoRAs by filename, LoRA Manager category/tag, or description."""
    q = str(query or "").strip().casefold()
    if not q:
        return []

    category_hits: list[str] = []
    for name in names:
        info = metadata.get(name) or {}
        categories = {str(x).casefold() for x in info.get("categories") or []}
        for category, aliases in ComfyUIClient._LORA_CATEGORY_ALIASES.items():
            if category in categories and any(q == str(alias).casefold() for alias in aliases):
                category_hits.append(name)
                break
    if category_hits:
        return category_hits[:limit]

    q_base = q.replace("\\", "/").rsplit("/", 1)[-1]
    hits: list[str] = []
    for name in names:
        info = metadata.get(name) or {}
        search_text = " ".join(
            str(value)
            for value in (
                name,
                info.get("model_name"),
                info.get("categories"),
                info.get("tags"),
                info.get("description"),
                info.get("notes"),
            )
            if value
        ).casefold()
        if q in search_text or q_base in name.replace("\\", "/").rsplit("/", 1)[-1].casefold():
            hits.append(name)
    return hits[:limit]


@dataclass(config=ConfigDict(arbitrary_types_allowed=True))
class ComfyuiListModelsTool(FunctionTool[AstrAgentContext]):
    """查询 ComfyUI 可用模型/LoRA/CLIP 清单。"""

    name: str = "comfyui_list_models"
    description: str = (
        "查询本机上ComfyUI可用的UNET底模、LoRA、CLIP、VAE、Embedding列表。"
        "LoRA 会附带触发词，以及 LoRA Manager/Civitai 的 style、character 等类别、标签和使用建议。"
        "清单会自动同步并本地缓存，ComfyUI离线时返回最近一次同步结果。"
        "用户询问可用资源，或绘图时需要按画风、角色、服饰、效果挑选已安装 LoRA 时使用。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "refresh": {
                    "type": "boolean",
                    "description": "是否强制重新从 ComfyUI 同步一次，默认 false（用本地缓存）",
                },
                "kind": {
                    "type": "string",
                    "enum": ["all", "unet", "lora", "clip", "vae", "embedding"],
                    "description": "只查看某一类资源，默认 all",
                },
                "query": {
                    "type": "string",
                    "description": "按文件名、LoRA 类别或标签过滤；可选",
                },
                "limit": {
                    "type": "number",
                    "description": "每类最多返回多少项（1-50，默认 30）",
                },
            },
        }
    )
    client: ComfyUIClient | None = None

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        force = _as_bool(kwargs.get("refresh", False))
        kind = str(kwargs.get("kind") or "all").strip().lower()
        field_titles = {
            "unet": ("unet_name", "UNET 底模"),
            "lora": ("lora_name", "LoRA"),
            "clip": ("clip_name", "CLIP"),
            "vae": ("vae_name", "VAE"),
            "embedding": ("embeddings", "Embedding"),
        }
        if kind != "all" and kind not in field_titles:
            return "查询失败：kind 仅支持 all/unet/lora/clip/vae/embedding。"
        query = str(kwargs.get("query") or "").strip().casefold()
        limit = _bounded_limit(kwargs.get("limit"), MAX_LLM_LIST_ITEMS)
        resources, from_cache = await self.client.list_resources(force_refresh=force)

        parts = ["【ComfyUI 可用资源】"]
        if from_cache:
            parts.append("（来源：本地缓存，自动同步失败时回退）")
        parts.append("")
        lora_meta = resources.get("lora_meta") or {}
        fields = list(field_titles.values()) if kind == "all" else [field_titles[kind]]
        for field, title in fields:
            items = resources.get(field) or []
            if query:
                if field == "lora_name":
                    items = _match_lora_resources(items, lora_meta, query, limit=len(items))
                else:
                    items = [item for item in items if query in str(item).casefold()]
            parts.append(f"--- {title} ---")
            shown_items = items[:limit]
            if len(items) > limit:
                parts.append(f"  共 {len(items)} 项，仅显示前 {limit} 项；请继续用 query 筛选。")
            if field == "lora_name":
                for m in shown_items:
                    parts.append(f"  {m}")
                    info = lora_meta.get(m)
                    if info:
                        parts.extend(f"    {line}" for line in _lora_info_summary(info))
            elif shown_items:
                parts.extend(f"  {m}" for m in shown_items)
            else:
                parts.append("  (无)")
            parts.append("")
        return "\n".join(parts)


@dataclass(config=ConfigDict(arbitrary_types_allowed=True))
class ComfyuiGenerateTool(FunctionTool[AstrAgentContext]):
    """通过 ComfyUI 生成图片。"""

    name: str = "comfyui_generate"
    description: str = (
        "按当前默认配方或指定工作流生成图片，完成后直接发送到当前会话。日常按配方绘图可用 comfyui_draw。"
        "prompt 必填；未覆盖的参数沿用配方、插件配置或模板默认值，width/height 可按构图需求填写。"
        "当 LoRA 有助于实现用户要求的画风、角色、服饰或效果时，可主动查询并选用，用户无需点名 LoRA 或提供文件名。"
        "先用 comfyui_lookup(type=\"lora\", query=需求关键词) 或 comfyui_list_models(kind=\"lora\")，"
        "依据返回的用途说明、模型适用信息和推荐权重选择，再将实际文件名写入 lora 的 JSON 数组字符串。"
        "使用已记录的触发词时同步填写 trigger_words，保留原词格式；查询未提供触发词时可省略该字段并继续使用 LoRA。"
        "省略 lora 会沿用默认设置，传入列表会覆盖对应 LoRA；已有独立加速节点的模板沿用其加速设置。"
        "用户明确要求关闭 LoRA 时可传 \"[]\" 或 \"none\"，旧模板的此操作也会关闭独立加速 LoRA。"
        "角色/画师名称不确定时用 comfyui_lookup 查询；底模、采样参数等按用户要求调整，其余沿用默认值。"
        "recipe 与 workflow 是两个独立入口：传 recipe 时把参数填进该配方绑定的基底工作流（含其保存的 LoRA）；"
        "传 workflow 时按该模板生成、不套配方；两者都省略时优先默认配方，没有默认配方才用配置的默认工作流模板。"
        "生成成功后回复结果即可，图片已由插件发送。"
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
                    "description": "画师串，格式如 @画师名，逗号分隔。用户指定画师时填写，可用 comfyui_lookup 查询规范名称；省略则沿用默认画师。一般画风需求也可通过提示词或 LoRA 实现",
                },
                "trigger_words": {
                    "type": "string",
                    "description": "所选 LoRA 的已知触发词，从 lookup/model_info 返回值或用户提供的信息中取用，保留原始格式，多个用逗号分隔。选用 LoRA 时可同步填写，无需用户另外提出；查询未提供时可省略并继续使用 LoRA",
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
                    "description": "底模文件名。必须用 comfyui_list_models 返回的完整名字（可能带子目录前缀，如 Anima\\miaomiaoHarem_anima16.safetensors）；传短名会自动匹配，匹配到多份会要求重填。用户指定底模时填，不知道文件名先查 comfyui_list_models",
                },
                "lora": {
                    "type": "string",
                    "description": (
                        "本次使用的 LoRA 列表，可按画面需求主动查询并选用已安装资源。"
                        "传 JSON 数组字符串，每项包含查询得到的 name，可附推荐 strength，例如 "
                        '[{"name":"style.safetensors","strength":0.55}]（文件名用实际查询结果替换）。'
                        "按 Power 插槽或旧模板 LoRA 链顺序覆盖；要保留的原 LoRA 也需列入。"
                        "省略则沿用默认设置；用户要求关闭时传 \"[]\" 或 \"none\""
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
                    "description": "工作流模板入口：传模板名或 JSON 路径时按该模板生成，不套配方（显式 recipe 优先于 workflow）。两者都省略时先用默认配方，没有默认配方再用插件配置的默认模板",
                },
                "recipe": {
                    "type": "string",
                    "description": "配方入口：已保存的配方名。传入后按该配方绑定的基底工作流出图，配方默认参数兜底，本次显式传入的参数优先；不传则用当前默认配方",
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
    store: RecipeStore | None = None  # 配方存储（统一用 RecipeStore）

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
        """读配方并转成 generate 参数；省略 name 时读取当前默认配方。"""
        recipe = self._load_recipe_data(name)
        if recipe is None:
            return None
        # 把 RecipeStore 格式转成 generate 工具期望的 flat kwargs
        defaults = dict(recipe.get("defaults") or {})
        flat: dict[str, Any] = {}
        flat["workflow"] = recipe_template(recipe)
        # defaults 里的 loras 要转回 lora 参数串
        loras = defaults.pop("loras", None) if isinstance(defaults, dict) else None
        for k, v in defaults.items():
            flat[k] = v
        if "negative" in flat and "negative_prompt" not in flat:
            flat["negative_prompt"] = flat.pop("negative")
        if loras:
            flat["lora"] = json.dumps(loras, ensure_ascii=False)
        return flat

    def _load_recipe_data(self, name: str) -> dict | None:
        """读取完整配方，供 generate 保留 workflow 与槽位映射。"""
        if not self.store:
            return None
        return self.store.get(name) if name else self.store.default()

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

        # 双入口判定：显式 recipe > 显式 workflow > 默认配方 > 配置默认模板。
        # 显式 workflow 必须强制走模板入口，避免被默认配方静默吞掉。
        recipe_name = str(kwargs.get("recipe") or "").strip()
        workflow_param = str(kwargs.get("workflow") or "").strip()
        has_default = False
        if not recipe_name and not workflow_param and self.store is not None:
            has_default = self.store.default() is not None
        entry, entry_name = resolve_generation_entry(
            recipe_name, workflow_param, has_default_recipe=has_default
        )
        explicit_kwargs = dict(kwargs)
        recipe_data = None
        if entry == "recipe":
            recipe_data = self._load_recipe_data(entry_name)
            if recipe_data is None:
                return f"生成失败：配方不存在（{entry_name}）。可先 comfyui_recipe list 查看。"
            if not (recipe_data.get("slots") or {}).get("prompt"):
                display = str(recipe_data.get("name") or entry_name or "默认")
                return (
                    f"生成失败：配方「{display}」还没指定主提示词节点，"
                    "请主人在配方工作台或配置下拉框里选一下。"
                )
            merged = dict(self._load_recipe(entry_name) or {})
            merged.pop("name", None)
            merged.pop("prompt", None)  # prompt 以本参数为准
            # 把配方值塞进 kwargs（LLM 传的值覆盖），供底模短名解析沿用
            kwargs = {**merged, **kwargs}
            prompt = str(kwargs.get("prompt") or "").strip() or prompt

        seed = kwargs.get("seed")
        if seed is None:
            seed = random.randint(0, 2**31 - 1)

        try:
            pick = lambda key: self._pick(self.defaults, key, kwargs.get(key))  # noqa: E731
            # 底模名解析：传短名/basename 时在资源列表里匹配出完整路径（如 Anima\xxx.safetensors），
            # 避免 ComfyUI 校验 Value not in list；列表拉不到则保持原值交给提交阶段报错。
            model_val = pick("model")
            if model_val and self.client is not None:
                try:
                    resources, _ = await self.client.list_resources()
                    hits = _match_resource(resources.get("unet_name") or [], str(model_val))
                except Exception:
                    hits = []
                if len(hits) == 1:
                    kwargs["model"] = hits[0]
                elif len(hits) > 1:
                    preview = "、".join(hits[:5])
                    return (
                        f"生成失败：底模「{model_val}」匹配到多份：{preview}。"
                        "请让用户选一个或填完整文件名。"
                    )
            lora_val = pick("lora")
            trigger_val = pick("trigger_words")
            generation_values = _validate_generation_values(
                {
                    "seed": seed,
                    "width": pick("width"),
                    "height": pick("height"),
                    "steps": pick("steps"),
                    "cfg": pick("cfg"),
                    "denoise": pick("denoise"),
                }
            )
            seed = generation_values["seed"]
            # 自动填 lora 触发词已禁用（33号要求，lora_meta 触发词乱提示），需要时显式传 trigger_words
            prefix = f"astrbot_{uuid.uuid4().hex[:8]}"
            if recipe_data is not None:
                # 配方生成必须走保存的 workflow + slots。此前这里把配方压平成
                # builder.build() 参数，导致 Power Lora Loader 的动态 lora_N
                # 插槽完全绕过，WebUI 能选到的 LoRA 在机器人调用时不会提交。
                values = materialize_values(
                    recipe_data,
                    {
                        "prompt": prompt,
                        "artist": explicit_kwargs.get("artist"),
                        "trigger_words": explicit_kwargs.get("trigger_words"),
                        "quality": explicit_kwargs.get("quality"),
                        "negative": explicit_kwargs.get("negative_prompt")
                        or explicit_kwargs.get("negative"),
                        # model_val 已完成资源名解析；配方里保存的短名也要沿用
                        # 解析后的完整路径，避免 ComfyUI 校验时再次丢失。
                        "model": kwargs.get("model") if model_val is not None else None,
                        "loras": (
                            parse_lora(
                                explicit_kwargs.get(
                                    "lora",
                                    explicit_kwargs.get("loras"),
                                )
                            )
                            if (
                                "lora" in explicit_kwargs
                                or "loras" in explicit_kwargs
                            )
                            and explicit_kwargs.get(
                                "lora",
                                explicit_kwargs.get("loras"),
                            )
                            not in (None, "")
                            else None
                        ),
                        "width": explicit_kwargs.get("width"),
                        "height": explicit_kwargs.get("height"),
                        "steps": explicit_kwargs.get("steps"),
                        "cfg": explicit_kwargs.get("cfg"),
                        "sampler_name": explicit_kwargs.get("sampler_name"),
                        "scheduler": explicit_kwargs.get("scheduler"),
                        "denoise": explicit_kwargs.get("denoise"),
                        "seed": generation_values.get("seed"),
                    },
                )
                for key, val in self.defaults.items():
                    if key not in values and val not in (None, "", 0, 0.0, []):
                        values["negative" if key == "negative_prompt" else key] = val
                values = _validate_generation_values(values)
                wf = self.builder.load_template(recipe_template(recipe_data) or None)
                apply_slots(
                    wf,
                    recipe_data.get("slots") or {},
                    values,
                    prefix=prefix,
                    drop_nodes=list(recipe_data.get("drop_nodes") or []),
                )
            else:
                wf = self.builder.build(
                    workflow=kwargs.get("workflow"),
                    prompt=prompt,
                    artist=pick("artist"),
                    trigger_words=trigger_val,
                    quality=pick("quality"),
                    negative_prompt=pick("negative_prompt"),
                    model=pick("model"),
                    lora=lora_val,
                    width=generation_values.get("width"),
                    height=generation_values.get("height"),
                    seed=generation_values.get("seed"),
                    steps=generation_values.get("steps"),
                    cfg=generation_values.get("cfg"),
                    sampler_name=pick("sampler_name"),
                    scheduler=pick("scheduler"),
                    denoise=generation_values.get("denoise"),
                    prefix=prefix,
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
        _remember_prompt_id(self.shared, context, pid)

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

        try:
            local_path = safe_output_path(self.output_dir, filename)
            local_path.write_bytes(content)
        except (OSError, ValueError) as e:
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
        pid = str(kwargs.get("prompt_id") or "").strip() or _last_prompt_id(
            self.shared, context
        )
        remove = _as_bool(kwargs.get("remove_from_queue", False))

        if remove and pid:
            await self.client.delete_queue_items([pid])
        if not pid:
            return "中断失败：当前会话没有可中断的生成任务。请传入明确的 prompt_id。"
        ok = await self.client.interrupt(prompt_id=pid or None)
        if not ok:
            return "中断失败：无法连接 ComfyUI。"
        parts = [f"已请求中断任务 {pid}。"]
        if remove:
            parts.append("该任务已从待执行队列移除。")
        parts.append("若任务已执行完毕则无需处理。")
        return "".join(parts)


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

        if _as_bool(kwargs.get("include_gpu", True), default=True):
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
        try:
            limit = min(max(int(kwargs.get("limit") or 30), 1), 50)
        except (TypeError, ValueError):
            limit = 30

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
        try:
            limit = min(max(int(kwargs.get("limit") or 5), 1), 10)
        except (TypeError, ValueError):
            limit = 5
        nsfw = _as_bool(kwargs.get("nsfw", False))

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
        """从 safetensors 元数据提取触发词，兼容字符串/嵌套频率表。"""
        words, _source = ComfyUIClient._extract_trigger_words(meta)
        return words

    async def _query_local(self, name: str) -> str:
        # LoRA Manager 保存的 sidecar/Civitai 信息比 safetensors 头部更完整，
        # 尤其是 style/character 分类、用途说明和推荐权重。
        manager_meta = await self.client.get_lora_manager_metadata(name)
        manager_info = ComfyUIClient.normalize_lora_metadata(manager_meta)
        if manager_info:
            lines = [f"【LoRA Manager】{name}"]
            lines.extend(_lora_info_summary(manager_info, detailed=True))
            return "\n".join(lines)

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
            tags = [str(tag).strip() for tag in (item.get("tags") or []) if str(tag).strip()]
            if tags:
                parts.append(f"标签: {', '.join(tags[:12])}")
            description = " ".join(str(item.get("description") or "").split())
            if description:
                suffix = "…" if len(description) > 320 else ""
                parts.append(f"用途说明: {description[:320]}{suffix}")
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
        "不要凭记忆编 tag。搜索时优先用日文原名或英文罗马音（如 himari、初音ミク），罗马音命中率最高；中文虽可搜但自动映射不保证全中，中文查不到就换罗马音重搜。"
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
                    "description": "角色名/画师名/系列名。最好用罗马音或日文原名（如 himari、ヒマリ）；中文能用但映射不全",
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
        _remember_prompt_id(self.shared, context, pid)

        wait = _as_bool(kwargs.get("wait", True), default=True)
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
                try:
                    local_path = safe_output_path(self.output_dir, filename)
                    local_path.write_bytes(content)
                except (OSError, ValueError) as e:
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
        pid = str(kwargs.get("prompt_id") or "").strip() or _last_prompt_id(
            self.shared, context
        )

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
            try:
                local_path = safe_output_path(self.output_dir, filename)
                local_path.write_bytes(content)
                saved.append(str(local_path))
            except (OSError, ValueError):
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
        unload = _as_bool(kwargs.get("unload_models", True), default=True)
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
            shown = names[:MAX_LLM_LIST_ITEMS]
            suffix = f"\n（共 {len(names)} 个，仅显示前 {MAX_LLM_LIST_ITEMS} 个；用 search 精确查找）" if len(names) > len(shown) else ""
            return "全部节点类 (" + str(len(names)) + "):\n" + ", ".join(shown) + suffix
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
        shown = sorted(names)[:MAX_LLM_LIST_ITEMS]
        suffix = f"\n（共 {len(names)} 个，仅显示前 {MAX_LLM_LIST_ITEMS} 个；请继续用 query 筛选）" if len(names) > len(shown) else ""
        return f"【{folder}】模型 ({len(names)}):\n" + "\n".join(shown) + suffix


@dataclass(config=ConfigDict(arbitrary_types_allowed=True))
class ComfyuiRecipeTool(FunctionTool[AstrAgentContext]):
    """自研：保存/读取/列出生成配方（prompt + 全部参数），可一键复现。"""

    name: str = "comfyui_recipe"
    description: str = (
        "把一次生成的全部参数（prompt/artist/quality/trigger_words/negative_prompt/model/lora/"
        "steps/cfg/sampler/scheduler/denoise/width/height/seed/workflow）保存为命名配方，"
        "之后可以列出、读取、删除。配方存本地 JSON 文件。"
        "保存时会绑定一个基底工作流：显式传 workflow 用它，否则沿用默认配方绑定的工作流，"
        "再没有就用插件配置的默认模板；节点映射也从同工作流的现有配方继承。"
        "用户想复现某张图、保存常用风格参数时使用。"
        "action=save（保存，需 name + 至少 prompt）/ action=list（列出）/"
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
                "workflow": {
                    "type": "string",
                    "description": "save 时绑定的基底工作流模板名；省略则沿用默认配方的工作流，再没有就用配置默认模板",
                },
            },
        }
    )
    store: RecipeStore | None = None
    allow_delete: bool = False
    builder: WorkflowBuilder | None = None
    default_workflow: str = ""

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        action = str(kwargs.get("action") or "save").strip().lower()
        name = str(kwargs.get("name") or "").strip()
        if action not in ("save", "list", "load", "delete"):
            return "操作失败：action 仅支持 save/list/load/delete。"

        if action == "list":
            names = _recipe_enum(self.store)
            if not names:
                return "没有已保存的配方。"
            parts = ["【已保存配方】"]
            if self.store is not None:
                for row in self.store.list():
                    desc = (row.get("description") or "")[:40]
                    parts.append(f"- {row.get('name') or row['id']}: {desc}")
            return "\n".join(parts)

        if not name:
            return "操作失败：name 不能为空。"

        if action == "delete":
            if not self.allow_delete:
                return "为避免模型误删配方，删除操作请在 Workflow Studio 中手动完成。"
            if self.store is not None and self.store.delete(name):
                return f"已删除配方: {name}"
            return f"配方不存在: {name}"

        if action == "load":
            recipe = self.store.get(name) if self.store else None
            if recipe is None:
                return f"配方不存在: {name}"
            defaults = recipe.get("defaults") or {}
            info = {
                "name": recipe.get("name") or name,
                "workflow": recipe_template(recipe),
                "prompt": defaults.get("prompt") or "",
                "model": defaults.get("model") or "",
                "lora": json.dumps(defaults.get("loras") or [], ensure_ascii=False),
                "width": defaults.get("width") or "",
                "height": defaults.get("height") or "",
                "steps": defaults.get("steps") or "",
                "cfg": defaults.get("cfg") or "",
                "sampler_name": defaults.get("sampler_name") or "",
                "scheduler": defaults.get("scheduler") or "",
                "denoise": defaults.get("denoise") or "",
                "trigger_words": defaults.get("trigger_words") or "",
                "artist": defaults.get("artist") or "",
                "quality": defaults.get("quality") or "",
                "negative_prompt": defaults.get("negative") or "",
            }
            return "配方参数:\n" + json.dumps(info, ensure_ascii=False, indent=2)

        # save
        prompt = str(kwargs.get("prompt") or "").strip()
        if not prompt:
            return "保存失败：prompt 不能为空。"
        lora_val = kwargs.get("lora")
        if lora_val:
            try:
                parsed_loras = parse_lora(lora_val)
            except ValueError as e:
                return f"保存失败：{e}"
        else:
            parsed_loras = []
        # prompt 是配方的复现输入；不能误存为 trigger_words，否则下次会把整段
        # 主提示词拼进 LoRA 触发词节点。
        defaults: dict[str, Any] = {"prompt": prompt}
        for key in ("artist", "quality", "model", "sampler_name", "scheduler"):
            v = kwargs.get(key)
            if v is not None and v != "":
                defaults[key] = v
        if kwargs.get("negative_prompt"):
            defaults["negative"] = kwargs["negative_prompt"]
        if parsed_loras:
            defaults["loras"] = parsed_loras
        for key in ("steps", "cfg", "denoise", "width", "height"):
            v = kwargs.get(key)
            if v is not None:
                defaults[key] = v

        # 配方必须绑定一个基底工作流：显式指定 > 默认配方绑定的 > 配置默认模板。
        workflow_name = str(kwargs.get("workflow") or "").strip()
        if workflow_name and self.builder is not None:
            try:
                self.builder.resolve_workflow_path(workflow_name)
            except (FileNotFoundError, ValueError):
                workflow_name = ""
        if not workflow_name:
            base = self.store.default() if self.store else None
            workflow_name = recipe_template(base) or self.default_workflow
        # 槽位映射跟着基底工作流走：同工作流的现有配方（默认配方优先）已映射
        # 过主提示词就直接继承，避免存出没有槽位、生成时无法填 prompt 的配方。
        slots: dict = {}
        slot_from = ""
        if self.store is not None:
            slots, slot_from = self.store.base_slots_for(workflow_name)
        try:
            assert self.store is not None
            self.store.save({
                "name": name,
                "description": prompt[:60],
                "template": workflow_name,
                "workflow": workflow_name,
                "slots": slots,
                "defaults": defaults,
            })
        except (ValueError, OSError) as e:
            return f"保存失败：{e}"
        note = f"配方已保存: {name}（基底工作流 {workflow_name or '未绑定'}"
        if slot_from:
            note += f"，节点映射沿用「{slot_from}」"
        return note + "）"


_DRAW_DESC = (
    "按配方为用户画一张图，完成后直接发送到当前会话。prompt 必填，其余参数可按需覆盖。"
    "普通绘图可以只填 prompt，沿用默认配方。"
    "当 LoRA 有助于实现用户要求的画风、角色、服饰或效果时，可主动查询并选用，用户无需点名 LoRA 或提供文件名。"
    "先用 comfyui_lookup(type=\"lora\", query=需求关键词)，如 style、character、服饰或效果标签；"
    "根据返回的用途说明、模型适用信息和推荐权重选择，将实际文件名填入 lora。"
    "使用已记录的触发词时同步填写 trigger_words；查询未提供触发词时可省略该字段并继续使用 LoRA。"
    "传入 lora 会覆盖配方映射节点的列表，要保留的原 LoRA 也需列入；用户要求沿用配方或已有设置足够时省略 lora。"
    "用户选择配方或底模时填写 recipe/model；要求画幅时填写 size=portrait/landscape/square；"
    "画师、画质、负向内容、steps、cfg 等按用户要求调整，未调整的项沿用默认值。"
    "用户要求记住这套参数时填写 save_as。"
    "换底模时插件会校验模型家族：跨系模型（如 anima 配方换 krea/qwen）会自动切到同系配方，"
    "没有同系配方则报错让主人先建，此时转告用户即可，不要重试别的模型名。"
    "不同系模型请用对应系配方的画法（krea/qwen/flux 用自然语言描述，不要 danbooru 画师串）。"
    "模型和 LoRA 文件名使用查询结果，触发词保留已知原词格式。"
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
                    "description": "要画的内容，必填。按配方模型组织提示词：Anima 使用 danbooru 风格 tag，Krea/Qwen/Flux 使用自然语言描述",
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
                    "description": "本次使用的 LoRA，可按画风、角色、服饰或效果需求主动查询并选用，无需用户提供名称。填写查询得到的文件名或唯一关键词，多个用逗号；指定权重时传 JSON 数组字符串，如 [{\"name\":\"查询得到的文件名\",\"strength\":0.8}]。覆盖配方映射节点原列表，要保留的 LoRA 也需列入；省略则沿用配方。用户要求关闭时传 \"[]\" 或 \"none\"，Power 加载器的清空操作也会关闭独立加速 LoRA",
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
                    "description": "所选 LoRA 的已知触发词，从 lookup/model_info 返回值或用户提供的信息中取用，保留原始格式，多个用逗号分隔。选用 LoRA 时可同步填写，无需用户另外提出；查询未提供时可省略并继续使用 LoRA",
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
        resources = await self._resource_lists()
        names = resources.get("lora_name") or []
        metadata = resources.get("lora_meta") or {}
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
            hits = _match_lora_resources(names, metadata, query)
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
        switched_note = ""
        if model_raw:
            resolved_model, err = await self._resolve_model(model_raw)
            if err:
                return err
            # 家族守卫：跨系模型的 CLIP/采样结构不通用（如 anima 配方硬塞 krea 底模只会出错）。
            # 优先自动切到同家族配方；没有同家族配方就明确拒绝，提示先建配方。
            new_family = model_family(resolved_model)
            cur_family = recipe_family(recipe)
            if new_family and cur_family and new_family != cur_family:
                target = None
                for row in self.store.list():
                    if str(row.get("id")) == str(recipe.get("id")):
                        continue
                    if recipe_family(row) != new_family:
                        continue
                    full = self.store.get(str(row.get("id") or ""))
                    if full and (full.get("slots") or {}).get("prompt"):
                        target = full
                        break
                if target is None:
                    return (
                        f"「{model_raw}」是 {new_family} 系模型，和当前配方「{recipe.get('name')}」"
                        f"（{cur_family} 系）不通用，没法直接换。"
                        f"请主人先在配方工作台给 {new_family} 模型建一套配方，"
                        f"或换回 {cur_family} 系底模。"
                    )
                recipe = target
                slots = recipe.get("slots") or {}
                switched_note = f" 已自动切换到配方「{recipe.get('name')}」。"
                if not slots.get("prompt"):
                    return "新配方还没指定「用户要画的内容」写到哪，请主人在工作台里选一下。"
            if not slots.get("model"):
                # 当前配方（含刚切换的）没开底模格子：换不了指定模型
                if switched_note:
                    resolved_model = None  # 沿用新配方默认底模
                else:
                    return "这套配方还没指定底模格子，换不了模型。请主人在工作台里选一下「底模」。"
        if lora_raw not in (None, ""):
            resolved_loras, err = await self._resolve_loras(lora_raw)
            if err:
                return err
            if not slots.get("loras"):
                return "这套配方还没指定 LoRA 格子，换不了 LoRA。请主人在工作台里选一下「LoRA」。"

        if kwargs.get("steps") not in (None, "") or kwargs.get("cfg") not in (None, ""):
            if not slots.get("sampler") and not slots.get("sampler_2"):
                return "这套配方还没指定出图采样，改不了步数。请主人在工作台里选一下「出图采样」。"

        # 自动填 lora 触发词已禁用（33号要求），需要时显式传 trigger_words

        overrides = {
            "prompt": prompt,
            "seed": seed,
            "artist": kwargs.get("artist"),
            "quality": kwargs.get("quality"),
            "trigger_words": kwargs.get("trigger_words"),
            "negative": kwargs.get("negative_prompt") or kwargs.get("negative"),
            "model": resolved_model,
            "loras": resolved_loras,
            "steps": kwargs.get("steps"),
            "cfg": kwargs.get("cfg"),
        }
        values = materialize_values(recipe, overrides)
        for key, val in self.config_defaults.items():
            if key not in values and val not in (None, "", 0, 0.0, []):
                values["negative" if key == "negative_prompt" else key] = val

        size_token = str(kwargs.get("size") or "").strip()
        try:
            values["seed"] = _number(seed, "seed", integer=True, minimum=0, maximum=2**63 - 1)
            values = _validate_generation_values(values)
            seed = values["seed"]
            if size_token:
                values["width"], values["height"] = resolve_size(
                    int(values["width"]) if values.get("width") else None,
                    int(values["height"]) if values.get("height") else None,
                    size_token,
                    presets=recipe.get("size_presets"),
                )
                values = _validate_generation_values(values)
        except ValueError as e:
            return f"生成失败：参数错误（{e}）"

        try:
            wf = self.builder.load_template(recipe_template(recipe) or None)
        except FileNotFoundError as e:
            return f"生成失败：{e}"

        try:
            apply_slots(
                wf,
                slots,
                values,
                prefix=f"astrbot_{uuid.uuid4().hex[:8]}",
                drop_nodes=list(recipe.get("drop_nodes") or []),
            )
        except (TypeError, ValueError) as e:
            return f"生成失败：参数错误（{e}）"

        pid, submit_err = await self.client.submit_prompt_detail(wf)
        if submit_err:
            return f"生成失败：{submit_err}"
        if not pid:
            return "生成失败：无法连接 ComfyUI。"
        _remember_prompt_id(self.shared, context, pid)

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

        img = None
        for item in images:
            if item.get("type") == "output":
                img = item
                break
        if not img:
            img = images[-1] if images else None
        if not img:
            return "生成完成，但没有有效图片。"

        filename = img["filename"]
        img_type = img.get("type", "output")
        content = await self.client.download_image(
            filename, subfolder=img.get("subfolder", ""), image_type=img_type
        )
        if not content:
            return f"图片已生成但下载失败（{filename}）。"

        try:
            local_path = safe_output_path(self.output_dir, filename)
            local_path.write_bytes(content)
        except (OSError, ValueError) as e:
            return f"图片已生成但本地保存失败（{e}）。"

        used = {
            "prompt": prompt,
            "model": values.get("model"),
            "loras": values.get("loras") or values.get("lora"),
            "width": values.get("width"),
            "height": values.get("height"),
            "steps": values.get("steps"),
            "cfg": values.get("cfg"),
            "sampler_name": values.get("sampler_name"),
            "scheduler": values.get("scheduler"),
            "denoise": values.get("denoise"),
            "trigger_words": values.get("trigger_words"),
            "seed": seed,
        }
        self.store.save_history(
            {
                "prompt_id": pid,
                "recipe": recipe.get("name"),
                "recipe_id": recipe.get("id"),
                "template": recipe_template(recipe),
                "workflow": recipe_template(recipe),
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
                        "template": recipe_template(recipe),
                        "workflow": recipe_template(recipe),
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
            f"lora={lora_note} seed={seed} size={w}x{h}.{switched_note}{saved_note}"
        )


@dataclass(config=ConfigDict(arbitrary_types_allowed=True))
class ComfyuiLookupTool(FunctionTool[AstrAgentContext]):
    """查角色/画师/LoRA 触发词，短回包。"""

    name: str = "comfyui_lookup"
    description: str = (
        "查询角色/画师的规范词，以及已安装的底模和 LoRA。"
        "绘图需要某种画风、角色、服饰或效果时，可主动查询匹配的 LoRA，用户无需点名 LoRA 或提供文件名。"
        "character/artist：把触发词写进 prompt 或 artist。"
        "model/lora：选择符合需求的结果，把实际文件名填进 comfyui_draw 的 model/lora。LoRA 的 query 支持 LoRA Manager/Civitai "
        "分类或标签，例如 style/character/concept/风格/角色；结果会带用途说明、推荐权重和触发词。"
        "选用 LoRA 时可同步传入已记录的触发词；查询未提供触发词时可省略该字段并继续使用 LoRA。"
        "查无结果时可换类别或用途关键词搜索，也可继续使用配方默认值。文件名和规范词以查询结果为准。"
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
                    "description": "角色/画师名、模型文件名或关键词；type=lora 还可填画风、服饰、效果等用途标签或 style/character/concept 分类。支持中文、日文、罗马音",
                },
                "limit": {
                    "type": "number",
                    "description": "type=lora 时最多返回几项（1-8，默认 5）",
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
            return await self._lookup_lora(query, _bounded_limit(kwargs.get("limit"), 5))
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

    async def _lookup_lora(self, query: str, limit: int = 5) -> str:
        if self.client is None:
            return "查询失败：ComfyUI 未配置。"
        resources, _ = await self.client.list_resources()
        names = resources.get("lora_name") or []
        meta = resources.get("lora_meta") or {}
        hits = _match_lora_resources(
            names, meta, query, limit=min(limit, MAX_LLM_DETAIL_ITEMS)
        )
        if not hits:
            return f"未找到匹配 LoRA：{query}（可按文件名、style/character 分类或标签查询）"
        lines = ["【LoRA】把文件名填进 comfyui_draw 的 lora："]
        for name in hits:
            info = meta.get(name) or {}
            lines.append(name)
            lines.extend(f"  {line}" for line in _lora_info_summary(info, detailed=True))
        return "\n".join(lines)


async def _auto_fill_trigger_words(client: ComfyUIClient, lora_input: Any) -> str | None:
    """从 lora_meta 缓存中按 LoRA 文件名查触发词，拼成逗号分隔串返回。"""
    if client is None or lora_input in (None, "", []):
        return None
    try:
        resources, _ = await client.list_resources()
    except Exception:
        return None
    text = collect_trigger_words(resources.get("lora_meta") or {}, lora_input)
    return text or None


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
