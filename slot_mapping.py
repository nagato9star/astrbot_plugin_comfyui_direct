"""工作流槽位：节点列表、自动检测、按映射写入。

运行时不再猜节点。人在配置/WebUI 下拉框里选定
「哪个节点是提示词 / KSampler / 底模 / LoRA / 尺寸」，
检测结果只作为下拉的初始建议。
"""

from __future__ import annotations

import ast
import json
import math
import re
from typing import Any

from astrbot.api import logger

ARTIST_PATTERN = re.compile(r"\(\s*@[^)]*?:\s*[\d.]+\s*\)")
QUALITY_KEYWORDS = ("masterpiece", "best quality", "score_")
NEGATIVE_MARKERS = ("lowres", "worst quality")

# 配置/WebUI 下拉框的槽位角色。值写入配方 defaults，节点 id 写入配方 slots。
SLOT_ROLES: tuple[tuple[str, str], ...] = (
    ("prompt", "用户要画的内容"),
    ("model", "底模"),
    ("loras", "LoRA"),
    ("size", "画面大小"),
    ("sampler", "出图采样"),
    ("sampler_2", "第二段采样"),
    ("negative", "不要出现的东西"),
    ("artist", "画师风格"),
    ("quality", "画质词"),
    ("trigger_words", "LoRA 触发词"),
    ("clip", "文本编码器(CLIP)"),
    ("vae", "VAE"),
    ("guidance", "引导强度(Flux)"),
)
SLOT_BASIC = ("prompt", "model", "loras", "size", "sampler")
SLOT_HELP: dict[str, str] = {
    "prompt": "机器人会把用户的描述写到这里。必选。",
    "model": "这套默认用哪颗底模。用户说换模型时也写到这里。",
    "loras": "这套默认挂哪些 LoRA。用户点名 LoRA 时覆盖这里。",
    "size": "宽和高写到这里。竖图/横图也靠它。",
    "sampler": "步数、精细程度写到这里。必选。双采样时选第一段。",
    "sampler_2": "双采样的第二段。没有第二段可留空；步数仍会写到外联整数节点。",
    "negative": "不想看到的东西。没有可留空。",
    "artist": "用户点名画师时写到这里。没有可留空。",
    "quality": "画质词。一般不用动。",
    "trigger_words": "某些 LoRA 必须带的触发词。选 LoRA 后会自动填。",
    "clip": "换文本编码器。Flux/Krea/Qwen 这类独立 CLIP 的模型才需要，可留空。",
    "vae": "换 VAE。模型用独立 VAE 时才需要，可留空。",
    "guidance": "FluxGuidance 之类的引导值节点。可留空。",
}

SLOT_CLASS_HINTS: dict[str, tuple[str, ...]] = {
    "prompt": (
        "CR Prompt Text",
        "CLIPTextEncode",
        "DanbooruText",
        "String Literal",
        "PrimitiveStringMultiline",
        "CLIPTextEncodeSDXL",
    ),
    "negative": ("CLIPTextEncode", "CR Prompt Text", "CLIPTextEncodeSDXL"),
    "artist": ("CR Prompt Text", "DanbooruText", "CLIPTextEncode"),
    "quality": ("CR Prompt Text", "CLIPTextEncode"),
    "trigger_words": ("CR Prompt Text", "CLIPTextEncode"),
    "model": (
        "UNETLoader",
        "CheckpointLoaderSimple",
        "UNETLoaderGGUF",
        "CheckpointLoader",
        "UnetLoaderGGUF",
        "CheckpointLoaderNF4",
        "UnetLoaderGGUFAdvanced",
        "DiffusersLoader",
    ),
    "clip": (
        "CLIPLoader",
        "DualCLIPLoader",
        "TripleCLIPLoader",
        "QuadrupleCLIPLoader",
        "CLIPLoaderGGUF",
        "DualCLIPLoaderGGUF",
    ),
    "vae": ("VAELoader",),
    "guidance": ("FluxGuidance",),
    "loras": (
        "Power Lora Loader (rgthree)",
        "LoraLoaderModelOnly",
        "LoraLoader",
        "LoraLoaderModelOnly (rgthree)",
    ),
    "size": (
        "EmptyLatentImage",
        "EmptySD3LatentImage",
        "EmptyHunyuanLatentImage",
        "EmptyFluxLatentImage",
    ),
    "sampler": (
        "KSampler",
        "KSamplerAdvanced",
        "XB_ROCmKSamplerAdvanced",
        "KSamplerSelect",
        "SamplerCustom",
        "SamplerCustomAdvanced",
    ),
    "sampler_2": (
        "KSampler",
        "KSamplerAdvanced",
        "XB_ROCmKSamplerAdvanced",
        "KSamplerSelect",
        "SamplerCustom",
        "SamplerCustomAdvanced",
    ),
}

SAMPLER_CLASSES = set(SLOT_CLASS_HINTS["sampler"])
POWER_LORA_CLASS = "Power Lora Loader (rgthree)"
ANIMA_DROP_NODES = ("445", "446", "447")

# 外联整数 / 种子节点：双采样时 steps、seed 常接到这些节点而不是写在采样器 widget 上
INT_NODE_CLASSES = {
    "Int",
    "INT",
    "Integer",
    "PrimitiveInt",
    "Primitive integer",
    "easy int",
    "ImpactInt",
    "CR Integer",
    "JWInteger",
    "Int Literal",
    "CM_Int",
    "Seed",
    "Seed Everywhere",
    "ttN seed",
    "easy seed",
}
SEED_NODE_CLASSES = {
    "RandomNoise",
    "Noise_RandomNoise",
    "Seed",
    "Seed Everywhere",
    "easy seed",
    "ttN seed",
}
INT_VALUE_KEYS = ("value", "int", "integer", "number", "Number", "seed", "noise_seed")

LORA_STRENGTH_MIN = -10.0
LORA_STRENGTH_MAX = 10.0


def normalize_lora_strength(value: Any, default: float = 0.8) -> float:
    """Validate a LoRA weight before putting it into a ComfyUI workflow."""
    try:
        strength = float(default if value is None else value)
    except (TypeError, ValueError) as e:
        raise ValueError("LoRA strength 必须是数字") from e
    if not math.isfinite(strength):
        raise ValueError("LoRA strength 不能是 NaN 或无穷大")
    if not LORA_STRENGTH_MIN <= strength <= LORA_STRENGTH_MAX:
        raise ValueError(
            f"LoRA strength 必须在 {LORA_STRENGTH_MIN:g} 到 {LORA_STRENGTH_MAX:g} 之间"
        )
    return strength


def _is_link(val: Any) -> bool:
    return isinstance(val, list) and val and not isinstance(val[0], dict)


def _is_int_node(node: dict | None) -> bool:
    if not isinstance(node, dict):
        return False
    cls = str(node.get("class_type") or "")
    if cls in INT_NODE_CLASSES:
        return True
    return cls.lower() in {"int", "integer", "primitiveint", "primitive integer"}


def _int_field(node: dict) -> str | None:
    ins = node.get("inputs") or {}
    for key in INT_VALUE_KEYS:
        if key in ins and not _is_link(ins.get(key)):
            return key
    for key in INT_VALUE_KEYS:
        if key in ins:
            return key
    return "value" if _is_int_node(node) else None


def _write_int_node(node: dict, value: int) -> bool:
    field = _int_field(node)
    if not field:
        return False
    node.setdefault("inputs", {})[field] = int(value)
    return True


def _resolve_linked_value(wf: dict, val: Any) -> Any:
    """外联整数节点上的当前数值；不是链接则原样返回。"""
    if not _is_link(val):
        return val
    node = wf.get(str(val[0]))
    if not isinstance(node, dict):
        return None
    field = _int_field(node)
    if not field:
        return None
    inner = (node.get("inputs") or {}).get(field)
    if inner is None or _is_link(inner):
        return None
    return inner


def _write_numeric_input(wf: dict, node: dict, field: str, value: int | float, *, as_int: bool = True) -> bool:
    """写采样器字段：若该口外联了整数节点，改整数节点，不断开连线。"""
    ins = node.setdefault("inputs", {})
    if field not in ins:
        return False
    written: int | float = int(value) if as_int else float(value)
    cur = ins.get(field)
    if _is_link(cur):
        target = wf.get(str(cur[0]))
        if isinstance(target, dict) and (
            _is_int_node(target) or str(target.get("class_type") or "") in SEED_NODE_CLASSES
        ):
            return _write_int_node(target, int(value))
        return False
    ins[field] = written
    return True


def _rank_sampler_ids(wf: dict) -> list[str]:
    """按「能控步数」排序：带 steps / 外联 Int 的 KSampler 优先于 KSamplerSelect。"""
    ranked: list[tuple[int, str]] = []
    for nid, node in wf.items():
        if not isinstance(node, dict):
            continue
        cls = str(node.get("class_type") or "")
        if cls not in SAMPLER_CLASSES:
            continue
        ins = node.get("inputs") or {}
        score = 0
        if "steps" in ins:
            score += 4
        if _is_link(ins.get("steps")):
            score += 3
        if cls in ("KSampler", "KSamplerAdvanced", "XB_ROCmKSamplerAdvanced"):
            score += 2
        if "seed" in ins or "noise_seed" in ins:
            score += 1
        ranked.append((score, str(nid)))
    ranked.sort(key=lambda x: (-x[0], int(x[1]) if x[1].isdigit() else 0))
    return [nid for _, nid in ranked]


def collect_trigger_words(lora_meta: dict[str, Any] | None, loras: Any) -> str:
    """从 lora_meta 按已选 LoRA 拼触发词，去重保序。"""
    try:
        parsed = parse_lora(loras)
    except (ValueError, TypeError):
        return ""
    if not parsed or not lora_meta:
        return ""
    seen: set[str] = set()
    unique: list[str] = []
    for item in parsed:
        name = str(item.get("name") or "").strip()
        info = lora_meta.get(name) or {}
        for word in info.get("trigger_words") or []:
            text = str(word).strip()
            if text and text not in seen:
                seen.add(text)
                unique.append(text)
    return ", ".join(unique)

_WIDGET_SKIP = {"fixed", "randomize", "increment", "decrement", "increment-1", "decrement-1"}


def parse_node_option(raw: Any) -> str:
    """配置下拉保存的是 '353 — CR Prompt Text — 主提示词'，取出节点 id。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    for sep in (" — ", " - ", " | ", "|"):
        if sep in text:
            text = text.split(sep, 1)[0].strip()
            break
    return text


def format_node_option(nid: str, node: dict) -> str:
    cls = str(node.get("class_type") or "?")
    title = str((node.get("_meta") or {}).get("title") or "").strip()
    if title and title != cls:
        return f"{nid} — {cls} — {title}"
    return f"{nid} — {cls}"


def is_api_workflow(data: Any) -> bool:
    if not isinstance(data, dict) or not data:
        return False
    if isinstance(data.get("nodes"), list):
        return False
    vals = [v for v in data.values() if isinstance(v, dict)]
    if not vals:
        return False
    return sum(1 for v in vals if "class_type" in v) >= max(1, len(vals) // 2)


def is_ui_workflow(data: Any) -> bool:
    return isinstance(data, dict) and isinstance(data.get("nodes"), list)


def normalize_workflow(data: dict, object_info: dict | None = None) -> dict:
    """把导入的 JSON 收成 API 格式。UI 格式需要 object_info 才能可靠转换。"""
    if is_api_workflow(data):
        return {str(k): v for k, v in data.items() if isinstance(v, dict) and "class_type" in v}
    if is_ui_workflow(data):
        return ui_to_api(data, object_info)
    raise ValueError("无法识别工作流格式。请在 ComfyUI 使用 Save (API Format) 再导入。")


def ui_to_api(ui: dict, object_info: dict | None = None) -> dict:
    """ComfyUI 前端 {nodes, links} → API {nid: {class_type, inputs}}。"""
    nodes = ui.get("nodes") or []
    raw_links = ui.get("links") or []
    links_by_id: dict[int, list] = {}
    for link in raw_links:
        if isinstance(link, list) and len(link) >= 5:
            links_by_id[int(link[0])] = link

    out: dict[str, dict] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        nid = str(node.get("id"))
        cls = str(node.get("type") or node.get("class_type") or "")
        if not nid or not cls:
            continue
        inputs: dict[str, Any] = {}
        for inp in node.get("inputs") or []:
            if not isinstance(inp, dict):
                continue
            name = inp.get("name")
            link_id = inp.get("link")
            if name is None or link_id is None:
                continue
            link = links_by_id.get(int(link_id))
            if not link:
                continue
            inputs[str(name)] = [str(link[1]), int(link[2])]

        widgets = list(node.get("widgets_values") or [])
        widgets = [v for v in widgets if not (isinstance(v, str) and v.lower() in _WIDGET_SKIP)]
        widget_names = _widget_input_names(cls, object_info)
        used = set(inputs)
        wi = 0
        for name in widget_names:
            if name in used:
                continue
            if wi >= len(widgets):
                break
            inputs[name] = widgets[wi]
            wi += 1
        # 没有 object_info 时：把剩余 widgets 按常见字段名尽量填
        if object_info is None and wi < len(widgets):
            for guess, val in zip(
                ("seed", "steps", "cfg", "sampler_name", "scheduler", "denoise", "text", "prompt"),
                widgets[wi:],
            ):
                if guess not in inputs:
                    inputs[guess] = val

        meta = {}
        title = node.get("title")
        if title:
            meta["title"] = title
        pos = node.get("pos")
        if isinstance(pos, (list, tuple)) and len(pos) >= 2:
            meta["pos"] = {"x": pos[0], "y": pos[1]}
        elif isinstance(pos, dict):
            meta["pos"] = pos
        entry: dict[str, Any] = {"class_type": cls, "inputs": inputs}
        if meta:
            entry["_meta"] = meta
        out[nid] = entry

    if not out:
        raise ValueError("UI 工作流里没有可转换的节点。请改用 Save (API Format)。")
    if object_info is None:
        logger.warning("[ComfyUIDirect] UI 格式在无 object_info 时按启发式转换，字段可能错位")
    return out


def _widget_input_names(cls: str, object_info: dict | None) -> list[str]:
    if not object_info:
        return []
    info = object_info.get(cls) or {}
    spec = info.get("input") or {}
    names: list[str] = []
    for bucket in ("required", "optional"):
        block = spec.get(bucket) or {}
        if not isinstance(block, dict):
            continue
        for name, typ in block.items():
            if _is_widget_spec(typ):
                names.append(name)
    return names


def _is_widget_spec(typ: Any) -> bool:
    if not isinstance(typ, list) or not typ:
        return False
    head = typ[0]
    if isinstance(head, list):
        return True
    return head in ("INT", "FLOAT", "STRING", "BOOLEAN", "COMBO")


def list_nodes(wf: dict) -> list[dict]:
    """给下拉框用的节点摘要。"""
    rows = []
    for nid, node in wf.items():
        if not isinstance(node, dict) or "class_type" not in node:
            continue
        cls = str(node.get("class_type") or "")
        title = str((node.get("_meta") or {}).get("title") or "")
        rows.append(
            {
                "id": str(nid),
                "class_type": cls,
                "title": title,
                "label": format_node_option(str(nid), node),
            }
        )
    rows.sort(key=lambda r: (r["class_type"], int(r["id"]) if str(r["id"]).isdigit() else 0))
    return rows


def node_options_for_slot(wf: dict, slot: str, selected: str = "") -> list[str]:
    """某个槽位的下拉选项：候选类优先，当前选中值始终保留。"""
    hints = set(SLOT_CLASS_HINTS.get(slot) or ())
    selected_id = parse_node_option(selected)
    options = [""]
    rest = []
    for row in list_nodes(wf):
        label = row["label"]
        if hints and row["class_type"] not in hints and row["id"] != selected_id:
            rest.append(label)
            continue
        options.append(label)
    options.extend(rest)
    if selected and selected not in options and selected_id:
        for row in list_nodes(wf):
            if row["id"] == selected_id:
                options.insert(1, row["label"])
                break
        else:
            options.insert(1, selected)
    return options


def detect_slots(wf: dict) -> dict[str, dict]:
    """自动建议槽位。结果必须给人确认后写入配方，运行时不再调用。"""
    slots: dict[str, dict] = {}
    roles = _find_prompt_roles(wf)
    if roles.get("main"):
        slots["prompt"] = _slot(roles["main"], wf, "prompt")
    elif roles.get("main_alt"):
        slots["prompt"] = _slot(roles["main_alt"], wf, "prompt")
    if roles.get("artist"):
        slots["artist"] = _slot(roles["artist"], wf, "artist")
    if roles.get("quality"):
        slots["quality"] = _slot(roles["quality"], wf, "quality", mode="append")
    if roles.get("triggers"):
        slots["trigger_words"] = _slot(roles["triggers"], wf, "trigger_words")

    neg = _find_negative_node(wf)
    if neg:
        slots["negative"] = _slot(neg, wf, "negative", mode="append")

    model_id = _first_class(wf, SLOT_CLASS_HINTS["model"])
    if model_id:
        slots["model"] = _slot(model_id, wf, "model")

    clip_id = _first_class(wf, SLOT_CLASS_HINTS["clip"])
    if clip_id:
        slots["clip"] = _slot(clip_id, wf, "clip")

    vae_id = _first_class(wf, SLOT_CLASS_HINTS["vae"])
    if vae_id:
        slots["vae"] = _slot(vae_id, wf, "vae")

    guidance_id = _first_class(wf, SLOT_CLASS_HINTS["guidance"])
    if guidance_id:
        slots["guidance"] = _slot(guidance_id, wf, "guidance")

    lora_id = _first_class(wf, (POWER_LORA_CLASS,)) or _first_class(
        wf, SLOT_CLASS_HINTS["loras"]
    )
    if lora_id:
        slots["loras"] = _slot(lora_id, wf, "loras")

    size_id = _first_class(wf, SLOT_CLASS_HINTS["size"])
    if size_id:
        slots["size"] = _slot(size_id, wf, "size")

    sampler_ids = _rank_sampler_ids(wf)
    if sampler_ids:
        slots["sampler"] = _slot(sampler_ids[0], wf, "sampler")
        if len(sampler_ids) > 1:
            slots["sampler_2"] = _slot(sampler_ids[1], wf, "sampler")

    return slots


def looks_like_anima(wf: dict) -> bool:
    has_join = any(n.get("class_type") == "JoinStringMulti" for n in wf.values() if isinstance(n, dict))
    has_cr = any(n.get("class_type") == "CR Prompt Text" for n in wf.values() if isinstance(n, dict))
    return has_join and has_cr


def _slot(nid: str, wf: dict, role: str, mode: str = "replace") -> dict:
    node = wf.get(nid) or {}
    field = infer_field(node, role)
    out: dict[str, Any] = {"node": str(nid), "mode": mode}
    if field:
        out["field"] = field
    return out


def infer_field(node: dict, role: str) -> str:
    ins = node.get("inputs") or {}
    cls = str(node.get("class_type") or "")
    if role in ("prompt", "artist", "quality", "trigger_words"):
        if "prompt" in ins:
            return "prompt"
        if "text" in ins:
            return "text"
        return "prompt" if cls == "CR Prompt Text" else "text"
    if role == "negative":
        return "text" if "text" in ins else "prompt"
    if role == "model":
        for key in ("unet_name", "ckpt_name"):
            if key in ins:
                return key
        if "Checkpoint" in cls:
            return "ckpt_name"
        return "unet_name"
    if role == "clip":
        for key in ("clip_name", "clip_name1", "clip_name2", "clip_name3", "clip_name4"):
            if key in ins:
                return key
        return "clip_name"
    if role == "vae":
        return "vae_name"
    if role == "guidance":
        return "strength"
    if role == "size":
        return "width"
    if role == "sampler":
        if _is_int_node(node):
            return _int_field(node) or "value"
        if "noise_seed" in ins and "seed" not in ins:
            return "noise_seed"
        return "seed"
    if role == "loras":
        return "lora" if cls == POWER_LORA_CLASS else "lora_name"
    return ""


def _first_class(wf: dict, classes: tuple[str, ...]) -> str | None:
    wanted = set(classes)
    for nid, node in wf.items():
        if isinstance(node, dict) and node.get("class_type") in wanted:
            return str(nid)
    return None


def _classify_prompt_role(text: str) -> str:
    t = text.strip()
    if not t:
        return "main"
    if ARTIST_PATTERN.search(t):
        return "artist"
    if t.startswith("@"):
        return "triggers"
    if any(k in t for k in QUALITY_KEYWORDS):
        return "quality"
    return "main"


def _find_prompt_roles(wf: dict) -> dict[str, str | None]:
    roles: dict[str, str | None] = {
        "artist": None,
        "quality": None,
        "main": None,
        "triggers": None,
        "main_alt": None,
    }
    joins = [nid for nid, n in wf.items() if isinstance(n, dict) and n.get("class_type") == "JoinStringMulti"]
    if joins:
        for jid in joins:
            node = wf[jid]
            entries: list[tuple[int, Any]] = []
            for key, val in (node.get("inputs") or {}).items():
                if not key.startswith("string_"):
                    continue
                try:
                    idx = int(key.split("_")[1])
                except ValueError:
                    continue
                entries.append((idx, val))
            pending: list[str] = []
            seen_at = False
            for _, link in sorted(entries):
                if not isinstance(link, list) or not link:
                    continue
                tid, tn = _follow_prompt_node(wf, str(link[0]))
                if tn.get("class_type") != "CR Prompt Text":
                    continue
                text = str((tn.get("inputs") or {}).get("prompt", ""))
                if not text.strip():
                    pending.append(tid)
                    continue
                role = _classify_prompt_role(text)
                if role == "triggers" and not seen_at and roles["artist"] is None:
                    role = "artist"
                if role == "artist":
                    seen_at = True
                if roles.get(role) is None:
                    roles[role] = tid
            multi = roles["quality"] is not None or roles["triggers"] is not None
            for tid in pending:
                if multi and roles["artist"] is None:
                    roles["artist"] = tid
                elif roles["main"] is None:
                    roles["main"] = tid
                elif multi and roles["triggers"] is None:
                    roles["triggers"] = tid
        return roles

    candidates = [
        (nid, str((n.get("inputs") or {}).get("prompt", "")))
        for nid, n in wf.items()
        if isinstance(n, dict) and n.get("class_type") == "CR Prompt Text"
    ]
    seen_at = False
    for nid, text in candidates:
        role = _classify_prompt_role(text)
        if role == "triggers" and not seen_at and roles["artist"] is None:
            role = "artist"
        if role == "artist":
            roles["artist"] = nid
            seen_at = True
            break
    others = [(nid, t) for nid, t in candidates if nid != roles["artist"]]
    if others:
        roles["main"] = max(others, key=lambda x: len(x[1]))[0]
    else:
        best = None
        for nid, node in wf.items():
            if not isinstance(node, dict) or node.get("class_type") != "CLIPTextEncode":
                continue
            text = str((node.get("inputs") or {}).get("text", ""))
            if any(m in text for m in NEGATIVE_MARKERS):
                continue
            if best is None or len(text) > best[1]:
                best = (nid, len(text))
        if best:
            roles["main_alt"] = best[0]
    return roles


def _follow_prompt_node(wf: dict, tid: str) -> tuple[str, dict]:
    tn = wf.get(tid) or {}
    for _ in range(5):
        if tn.get("class_type") == "CR Prompt Text":
            return tid, tn
        sub = tn.get("inputs") or {}
        ref = sub.get("string") or sub.get("text")
        if isinstance(ref, list) and ref:
            tid = str(ref[0])
            tn = wf.get(tid) or {}
            continue
        break
    return tid, tn


def _find_negative_node(wf: dict) -> str | None:
    for nid, node in wf.items():
        if not isinstance(node, dict) or node.get("class_type") != "CLIPTextEncode":
            continue
        title = str((node.get("_meta") or {}).get("title") or "").lower()
        text = str((node.get("inputs") or {}).get("text", ""))
        if "negative" in title or any(m in text for m in NEGATIVE_MARKERS):
            return str(nid)
    return None


def slots_from_config(raw: Any) -> dict[str, dict]:
    """把配置对象 node_slots 收成配方 slots。"""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict] = {}
    for role, _label in SLOT_ROLES:
        nid = parse_node_option(raw.get(role))
        if nid:
            out[role] = {"node": nid, "mode": "append" if role in ("negative", "quality") else "replace"}
    return out


def merge_slots(detected: dict[str, dict], configured: dict[str, dict]) -> dict[str, dict]:
    merged = dict(detected)
    merged.update(configured)
    return merged


def read_current_values(wf: dict, slots: dict[str, dict]) -> dict[str, Any]:
    """从映射节点读出配方要保存的值：底模、LoRA、尺寸、KSampler。"""
    values: dict[str, Any] = {}
    model_slot = slots.get("model")
    if model_slot:
        node = wf.get(str(model_slot["node"])) or {}
        field = model_slot.get("field") or infer_field(node, "model")
        val = (node.get("inputs") or {}).get(field)
        if isinstance(val, str) and val:
            values["model"] = val

    for role in ("clip", "vae"):
        spec = slots.get(role)
        if not spec:
            continue
        node = wf.get(str(spec["node"])) or {}
        field = spec.get("field") or infer_field(node, role)
        val = (node.get("inputs") or {}).get(field)
        if isinstance(val, str) and val:
            values[role] = val

    guidance_slot = slots.get("guidance")
    if guidance_slot:
        node = wf.get(str(guidance_slot["node"])) or {}
        val = (node.get("inputs") or {}).get("strength")
        if isinstance(val, (int, float)) and not _is_link(val):
            values["guidance"] = float(val)

    lora_slot = slots.get("loras")
    if lora_slot:
        values["loras"] = _read_loras(wf.get(str(lora_slot["node"])) or {})

    size_slot = slots.get("size")
    if size_slot:
        ins = (wf.get(str(size_slot["node"])) or {}).get("inputs") or {}
        if isinstance(ins.get("width"), (int, float)):
            values["width"] = int(ins["width"])
        if isinstance(ins.get("height"), (int, float)):
            values["height"] = int(ins["height"])

    sampler_slot = slots.get("sampler")
    if sampler_slot:
        node = wf.get(str(sampler_slot["node"])) or {}
        ins = node.get("inputs") or {}
        if _is_int_node(node):
            field = _int_field(node)
            raw = ins.get(field) if field else None
            if isinstance(raw, (int, float)):
                values["steps"] = int(raw)
        else:
            for key in ("steps", "cfg", "sampler_name", "sampler", "scheduler", "denoise"):
                val = ins.get(key)
                if _is_link(val):
                    val = _resolve_linked_value(wf, val)
                if val is None or isinstance(val, list):
                    continue
                if key == "sampler" and "sampler" not in values:
                    values["sampler_name"] = val
                else:
                    values[key] = val
    return values


def _read_loras(node: dict) -> list[dict]:
    ins = node.get("inputs") or {}
    cls = node.get("class_type")
    if cls == POWER_LORA_CLASS:
        out = []
        slots = sorted(
            (k for k, v in ins.items() if k.startswith("lora_") and isinstance(v, dict)),
            key=lambda k: int(k.split("_")[1]) if k.split("_")[1].isdigit() else 0,
        )
        for key in slots:
            spec = ins[key]
            if not spec.get("on"):
                continue
            name = str(spec.get("lora") or "").strip()
            if not name:
                continue
            try:
                strength = normalize_lora_strength(spec.get("strength", 0.8))
            except ValueError:
                strength = 0.8
            out.append({"name": name, "strength": strength})
        return out
    name = str(ins.get("lora_name") or "").strip()
    if not name:
        return []
    try:
        strength = normalize_lora_strength(
            ins.get("strength_model", ins.get("strength", 0.8))
        )
    except ValueError:
        strength = 0.8
    return [{"name": name, "strength": strength}]


def parse_lora(lora: Any) -> list[dict]:
    if lora is None:
        return []
    if isinstance(lora, list):
        parsed = lora
    elif isinstance(lora, dict):
        parsed = [lora]
    else:
        txt = str(lora).strip()
        if not txt or txt.lower() in ("none", "null"):
            return []
        try:
            parsed = json.loads(txt)
        except ValueError:
            try:
                parsed = ast.literal_eval(txt)
            except (ValueError, SyntaxError) as e:
                raise ValueError(f"lora 参数格式错误: {lora}") from e
    if isinstance(parsed, dict):
        parsed = [parsed]
    elif isinstance(parsed, str):
        parsed = [parsed]
    elif not isinstance(parsed, list):
        raise ValueError(f"lora 参数格式错误: {lora}")

    out: list[dict] = []
    for item in parsed:
        if isinstance(item, dict):
            if not str(item.get("name") or "").strip():
                continue  # 空名条目（杂交配方残留）直接忽略
            out.append(item)
        elif isinstance(item, str) and item.strip():
            out.append({"name": item})
        else:
            raise ValueError(f"lora 参数格式错误: {lora}")
    return out


def resolve_size(
    width: int | None,
    height: int | None,
    size: str | None,
    presets: dict | None = None,
) -> tuple[int | None, int | None]:
    """画幅 token → 宽高。配方可带 size_presets（如 Flux/Qwen 家族的分辨率档），
    命中时优先用配方档位；否则沿用内置的 SDXL 档和翻转逻辑。"""
    token = str(size or "same").strip().lower()
    if presets and token in presets:
        preset = presets.get(token)
        if isinstance(preset, (list, tuple)) and len(preset) >= 2:
            try:
                pw, ph = int(preset[0]), int(preset[1])
            except (TypeError, ValueError):
                pw = ph = 0
            if pw > 0 and ph > 0:
                return pw, ph
    w, h = width or 0, height or 0
    if not w or not h:
        if token == "portrait":
            return 832, 1216
        if token == "landscape":
            return 1216, 832
        if token == "square":
            return 1024, 1024
        return (w or None), (h or None)
    if token in ("", "same"):
        return w, h
    if token == "portrait":
        return min(w, h), max(w, h)
    if token == "landscape":
        return max(w, h), min(w, h)
    if token == "square":
        side = int(round((w + h) / 2 / 64) * 64) or 1024
        return side, side
    return w, h


def apply_slots(
    wf: dict,
    slots: dict[str, dict],
    values: dict[str, Any],
    *,
    prefix: str | None = None,
    drop_nodes: list[str] | None = None,
) -> dict:
    """按已确认的槽位写节点。None 值跳过。"""
    if drop_nodes:
        for nid in drop_nodes:
            wf.pop(str(nid), None)

    # 槽位指向的节点必须存在，否则对应值会被静默丢弃；统一先告警。
    for role, spec in (slots or {}).items():
        nid = str((spec or {}).get("node") or "")
        if nid and str(nid) not in wf:
            logger.warning(
                f"[slot_mapping] 配方槽位 {role} 指向节点 {nid}，但当前工作流里没有它，该槽位本轮不会写入"
            )

    prompt = values.get("prompt")
    if prompt is not None and slots.get("prompt"):
        _write_text(wf, slots["prompt"], str(prompt), role="prompt")

    for role in ("artist", "trigger_words"):
        val = values.get(role)
        if val is not None and slots.get(role):
            _write_text(wf, slots[role], str(val), role=role)
            if role == "artist":
                _sync_danbooru_text(wf, str(val))

    for role in ("negative", "quality"):
        val = values.get(role)
        if val is not None and slots.get(role):
            _write_text(wf, slots[role], str(val), role=role)

    model = values.get("model")
    if model is not None and slots.get("model"):
        spec = slots["model"]
        node = wf.get(str(spec["node"]))
        if node is not None:
            field = spec.get("field") or infer_field(node, "model")
            node.setdefault("inputs", {})[field] = str(model)

    # 文本编码器 / VAE：与底模同类的字符串覆盖（换家族模型时由配方模板保证结构正确）
    for role in ("clip", "vae"):
        val = values.get(role)
        if val is None or not slots.get(role):
            continue
        spec = slots[role]
        node = wf.get(str(spec["node"]))
        if node is not None:
            field = spec.get("field") or infer_field(node, role)
            node.setdefault("inputs", {})[field] = str(val)

    guidance = values.get("guidance")
    if guidance is not None and slots.get("guidance"):
        spec = slots["guidance"]
        node = wf.get(str(spec["node"]))
        if node is not None:
            try:
                node.setdefault("inputs", {})["strength"] = float(guidance)
            except (TypeError, ValueError):
                pass

    if values.get("loras") is not None and slots.get("loras"):
        _apply_loras(wf, str(slots["loras"]["node"]), parse_lora(values.get("loras")))
    elif values.get("lora") is not None and slots.get("loras"):
        _apply_loras(wf, str(slots["loras"]["node"]), parse_lora(values.get("lora")))

    width, height = values.get("width"), values.get("height")
    size_token = values.get("size")
    if isinstance(size_token, str) and size_token and size_token not in ("same",):
        rw = width if isinstance(width, int) else None
        rh = height if isinstance(height, int) else None
        if slots.get("size") and (rw is None or rh is None):
            ins = (wf.get(str(slots["size"]["node"])) or {}).get("inputs") or {}
            rw = rw or (int(ins["width"]) if isinstance(ins.get("width"), (int, float)) else None)
            rh = rh or (int(ins["height"]) if isinstance(ins.get("height"), (int, float)) else None)
        width, height = resolve_size(rw, rh, size_token)

    if (width is not None or height is not None) and slots.get("size"):
        node = wf.get(str(slots["size"]["node"]))
        if node is not None:
            ins = node.setdefault("inputs", {})
            if width is not None:
                ins["width"] = int(width)
            if height is not None:
                ins["height"] = int(height)

    sampler_spec = slots.get("sampler")
    sampler2_spec = slots.get("sampler_2")
    sampler_keys = ("steps", "cfg", "sampler_name", "scheduler", "denoise", "seed")
    target_id = str(sampler_spec["node"]) if sampler_spec else (
        str(sampler2_spec["node"]) if sampler2_spec else ""
    )
    extra_ids = []
    if sampler2_spec and str(sampler2_spec["node"]) != target_id:
        extra_ids.append(str(sampler2_spec["node"]))
    if target_id and any(values.get(k) is not None for k in sampler_keys):
        _apply_sampler(wf, target_id, values, extra_ids=extra_ids)

    if prefix is not None:
        for node in wf.values():
            if isinstance(node, dict) and node.get("class_type") == "SaveImage":
                node.setdefault("inputs", {})["filename_prefix"] = prefix
                break
    return wf


def _write_text(wf: dict, spec: dict, text: str, *, role: str) -> None:
    node = wf.get(str(spec["node"]))
    if node is None:
        return
    field = spec.get("field") or infer_field(node, role)
    ins = node.setdefault("inputs", {})
    mode = spec.get("mode") or ("append" if role in ("negative", "quality") else "replace")
    if mode == "append":
        existing = str(ins.get(field) or "").strip()
        if existing and text and text not in existing:
            sep = ", " if not existing.rstrip().endswith(",") else " "
            ins[field] = f"{existing}{sep}{text}".strip()
            return
        if existing and not text:
            return
    ins[field] = text


def _sync_danbooru_text(wf: dict, artist_text: str) -> None:
    for node in wf.values():
        if not isinstance(node, dict) or node.get("class_type") != "DanbooruText":
            continue
        t = str((node.get("inputs") or {}).get("text", ""))
        if "@" in t:
            node.setdefault("inputs", {})["text"] = artist_text
            return


def _apply_sampler(
    wf: dict,
    nid: str,
    values: dict[str, Any],
    extra_ids: list[str] | None = None,
) -> None:
    node = wf.get(nid)
    if node is None:
        return

    sampler_ids = list(dict.fromkeys([*(extra_ids or []), *_rank_sampler_ids(wf)]))
    if nid not in sampler_ids and str(node.get("class_type") or "") in SAMPLER_CLASSES:
        sampler_ids.insert(0, nid)
    primary = nid if nid in sampler_ids else (sampler_ids[0] if sampler_ids else nid)

    seed = values.get("seed")
    if seed is not None:
        _apply_seed_all(wf, sampler_ids or [nid], int(seed))

    steps = values.get("steps")
    if steps is not None:
        if _is_int_node(node):
            _write_int_node(node, int(steps))
        _apply_steps_dual(wf, primary, sampler_ids, int(steps), extra_ids=extra_ids or [])

    target = wf.get(primary) if primary else node
    if target is None or _is_int_node(target):
        return
    ins = target.setdefault("inputs", {})
    if values.get("cfg") is not None:
        if not _write_numeric_input(wf, target, "cfg", float(values["cfg"]), as_int=False):
            if "cfg" in ins and not _is_link(ins.get("cfg")):
                ins["cfg"] = float(values["cfg"])
    if values.get("sampler_name"):
        sname = str(values["sampler_name"])
        if "sampler_name" in ins and not _is_link(ins.get("sampler_name")):
            ins["sampler_name"] = sname
        elif "sampler" in ins and not _is_link(ins.get("sampler")):
            ins["sampler"] = sname
    if values.get("scheduler") and "scheduler" in ins and not _is_link(ins.get("scheduler")):
        ins["scheduler"] = str(values["scheduler"])
    if values.get("denoise") is not None:
        if not _write_numeric_input(wf, target, "denoise", float(values["denoise"]), as_int=False):
            if "denoise" in ins and not _is_link(ins.get("denoise")):
                ins["denoise"] = float(values["denoise"])


def _apply_seed_all(wf: dict, sampler_ids: list[str], seed: int) -> None:
    seen: set[str] = set()
    for sid in sampler_ids:
        snode = wf.get(sid)
        if not isinstance(snode, dict):
            continue
        ins = snode.get("inputs") or {}
        for field in ("seed", "noise_seed"):
            if field in ins:
                _write_numeric_input(wf, snode, field, seed)
        noise = ins.get("noise")
        if _is_link(noise):
            tid = str(noise[0])
            tnode = wf.get(tid)
            if tid not in seen and isinstance(tnode, dict) and str(tnode.get("class_type") or "") in SEED_NODE_CLASSES:
                _write_int_node(tnode, seed)
                seen.add(tid)


def _apply_steps_to_node(wf: dict, node: dict | None, steps: int, written_ints: set[str]) -> None:
    if not isinstance(node, dict):
        return
    ins = node.get("inputs") or {}
    if "steps" in ins:
        cur = ins.get("steps")
        if _is_link(cur):
            tid = str(cur[0])
            tnode = wf.get(tid)
            if tid not in written_ints and isinstance(tnode, dict) and _is_int_node(tnode):
                _write_int_node(tnode, steps)
                written_ints.add(tid)
            return
        ins["steps"] = int(steps)
        return
    sigmas = ins.get("sigmas")
    hops = 0
    while _is_link(sigmas) and hops < 6:
        hops += 1
        snode = wf.get(str(sigmas[0]))
        if not isinstance(snode, dict):
            break
        sins = snode.get("inputs") or {}
        if "steps" in sins:
            _apply_steps_to_node(wf, snode, steps, written_ints)
            return
        sigmas = sins.get("sigmas") or sins.get("input")


def _apply_steps_dual(
    wf: dict,
    primary: str,
    sampler_ids: list[str],
    steps: int,
    extra_ids: list[str] | None = None,
) -> None:
    """步数写到选中采样器；双采样第二段、以及外联了整数节点的其它采样器一并改。"""
    written: set[str] = set()
    extra = set(extra_ids or [])
    _apply_steps_to_node(wf, wf.get(primary), steps, written)
    for sid in sampler_ids:
        if sid == primary:
            continue
        node = wf.get(sid)
        ins = (node or {}).get("inputs") or {}
        if sid in extra or _is_link(ins.get("steps")) or _is_link(ins.get("sigmas")):
            _apply_steps_to_node(wf, node, steps, written)


def _apply_loras(wf: dict, nid: str, parsed: list[dict]) -> None:
    node = wf.get(nid)
    if node is None:
        logger.warning(
            f"[slot_mapping] 配方的 loras 槽位指向节点 {nid}，但它不在当前工作流里，本轮 LoRA 全部未写入"
        )
        return
    if node.get("class_type") == POWER_LORA_CLASS:
        if not parsed:
            for other in wf.values():
                if isinstance(other, dict) and other.get("class_type") == "LoraLoaderModelOnly":
                    other.setdefault("inputs", {})["strength_model"] = 0.0
        ins = node.setdefault("inputs", {})
        slots = sorted(
            (k for k, v in ins.items() if k.startswith("lora_") and isinstance(v, dict)),
            key=lambda k: int(k.split("_")[1]) if k.split("_")[1].isdigit() else 0,
        )
        # Power Lora Loader 的 lora_N 是动态可选输入。有些 API 工作流未序列化
        # lora_N，不能只覆盖已有插槽；需按保存的配方主动创建它们。
        numeric_slots = [
            int(slot.split("_", 1)[1])
            for slot in slots
            if slot.split("_", 1)[1].isdigit()
        ]
        next_slot = max(numeric_slots, default=0) + 1
        used_slots: list[str] = []
        unused_slots = list(slots)
        for spec in parsed:
            name = str(spec.get("name") or "").strip()
            normalized_name = name.replace("/", "\\").casefold()
            # 工作流经常预留多个带文件名的槽位（krea2.json 的
            # masterpieces 在 lora_2）。优先复用同名槽位，保持工作流槽位语义。
            slot = next(
                (
                    candidate
                    for candidate in unused_slots
                    if str(ins[candidate].get("lora") or "")
                    .replace("/", "\\")
                    .casefold()
                    == normalized_name
                ),
                None,
            )
            if slot is None and unused_slots:
                slot = unused_slots[0]
            if slot is None:
                while f"lora_{next_slot}" in ins:
                    next_slot += 1
                slot = f"lora_{next_slot}"
                next_slot += 1
            ins[slot] = {
                "on": True,
                "lora": name,
                "strength": normalize_lora_strength(spec.get("strength", 0.8)),
            }
            used_slots.append(slot)
            if slot in unused_slots:
                unused_slots.remove(slot)
        for slot in slots:
            if slot not in used_slots:
                ins[slot]["on"] = False
        return

    # 单节点 LoraLoader / 从该节点起的 LoraLoaderModelOnly 链
    chain = _collect_lora_chain(wf, start=nid)
    if not chain and node.get("class_type") in ("LoraLoader", "LoraLoaderModelOnly"):
        chain = [nid]
    for i, cid in enumerate(chain):
        target = wf.get(cid)
        if target is None:
            continue
        tins = target.setdefault("inputs", {})
        if i < len(parsed):
            spec = parsed[i]
            if spec.get("name"):
                tins["lora_name"] = str(spec["name"])
            if "strength" in spec:
                tins["strength_model"] = normalize_lora_strength(spec["strength"])
                if "strength_clip" in tins and not isinstance(tins.get("strength_clip"), list):
                    tins["strength_clip"] = normalize_lora_strength(spec["strength"])
        else:
            tins["strength_model"] = 0.0


def _collect_lora_chain(wf: dict, start: str | None = None) -> list[str]:
    if start and (wf.get(start) or {}).get("class_type") in (
        "LoraLoaderModelOnly",
        "LoraLoader",
    ):
        cur = start
    else:
        cur = None
        for nid, node in wf.items():
            if isinstance(node, dict) and node.get("class_type") == "UNETLoader":
                cur = nid
                break
        if cur is None:
            return []
    chain: list[str] = []
    seen = {cur}
    for _ in range(50):
        nxt = None
        for nid, node in wf.items():
            if nid in seen or not isinstance(node, dict):
                continue
            ins = node.get("inputs") or {}
            if (
                node.get("class_type") == "LoraLoaderModelOnly"
                and isinstance(ins.get("model"), list)
                and str(ins["model"][0]) == str(cur)
            ):
                chain.append(nid)
                nxt = nid
                seen.add(nid)
                break
        if nxt is None:
            break
        cur = nxt
    return chain
