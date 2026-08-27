"""配方与生成历史。

配方保存的是人选定的节点映射，以及底模 / LoRA / 比例 / KSampler 参数。
prompt 每次由 LLM 填，不作为配方身份。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

from slot_mapping import (
    ANIMA_DROP_NODES,
    detect_slots,
    looks_like_anima,
    merge_slots,
    parse_node_option,
    read_current_values,
    slots_from_config,
)

_SLUG_RE = re.compile(r"[^\w\u4e00-\u9fff-]+", re.UNICODE)
RECIPE_VALUE_KEYS = (
    "model",
    "loras",
    "width",
    "height",
    "steps",
    "cfg",
    "sampler_name",
    "scheduler",
    "denoise",
    "trigger_words",
    "artist",
    "quality",
    "negative",
)


def slugify(name: str) -> str:
    text = _SLUG_RE.sub("-", str(name or "").strip()).strip("-")
    return text[:64] or "recipe"


def _short_file(name: str) -> str:
    text = str(name or "").replace("\\", "/").split("/")[-1]
    return text[:-12] if text.endswith(".safetensors") else text


class RecipeStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.recipe_dir = data_dir / "recipes"
        self.history_dir = data_dir / "history"
        self.recipe_dir.mkdir(parents=True, exist_ok=True)
        self.history_dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, recipe_id: str) -> Path:
        rid = slugify(recipe_id)
        return self.recipe_dir / f"{rid}.json"

    def list(self) -> list[dict]:
        rows = []
        for path in sorted(self.recipe_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            rows.append(self._public(data, path.stem))
        return rows

    def names(self) -> list[str]:
        return [r.get("name") or r["id"] for r in self.list()]

    def catalog(self, limit: int = 8) -> str:
        """给 LLM 看的短目录：配方名 + 底模 + LoRA + 尺寸。"""
        parts = []
        for row in self.list()[:limit]:
            bits = [row.get("name") or row["id"]]
            if row.get("description"):
                bits.append(str(row["description"])[:24])
            if row.get("model"):
                bits.append(_short_file(row["model"]))
            loras = row.get("loras") or []
            if loras:
                bits.append("lora=" + "+".join(_short_file(x) for x in loras[:3]))
            if row.get("width") and row.get("height"):
                bits.append(f"{row['width']}x{row['height']}")
            parts.append(" ".join(str(b) for b in bits if b))
        return "；".join(parts)

    def get(self, name_or_id: str) -> dict | None:
        if not name_or_id:
            return None
        wanted = str(name_or_id).strip()
        direct = self.path_for(wanted)
        if direct.is_file():
            return self._load(direct)
        for row in self.list():
            if row.get("name") == wanted or row.get("id") == wanted:
                return self._load(self.path_for(row["id"]))
        return None

    def default(self, preferred: str = "") -> dict | None:
        if preferred:
            found = self.get(preferred)
            if found:
                return found
        rows = self.list()
        for row in rows:
            if row.get("id") == "default" or row.get("name") in ("默认", "default"):
                return self.get(row["id"])
        if rows:
            return self.get(rows[0]["id"])
        return None

    def save(self, recipe: dict) -> dict:
        name = str(recipe.get("name") or "").strip()
        if not name:
            raise ValueError("配方名不能为空")
        rid = str(recipe.get("id") or "").strip() or slugify(name)
        rid = slugify(rid)
        data = {
            "id": rid,
            "name": name,
            "description": str(recipe.get("description") or "").strip(),
            "workflow": str(recipe.get("workflow") or "").strip(),
            "slots": recipe.get("slots") or {},
            "defaults": _clean_defaults(recipe.get("defaults") or {}),
            "drop_nodes": list(recipe.get("drop_nodes") or []),
            "updated_at": int(time.time()),
        }
        path = self.path_for(rid)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return data

    def delete(self, name_or_id: str) -> bool:
        recipe = self.get(name_or_id)
        if recipe is None:
            return False
        path = self.path_for(recipe["id"])
        if path.is_file():
            path.unlink()
            return True
        return False

    def save_history(self, entry: dict) -> Path:
        pid = str(entry.get("prompt_id") or int(time.time()))
        safe = slugify(pid)
        path = self.history_dir / f"{safe}.json"
        payload = dict(entry)
        payload["saved_at"] = int(time.time())
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self._trim_history(40)
        return path

    def list_history(self, limit: int = 20) -> list[dict]:
        files = sorted(self.history_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        out = []
        for path in files[:limit]:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict):
                out.append(data)
        return out

    def history_get(self, prompt_id: str) -> dict | None:
        path = self.history_dir / f"{slugify(prompt_id)}.json"
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def recipe_from_history(self, prompt_id: str, name: str, description: str = "") -> dict:
        entry = self.history_get(prompt_id)
        if entry is None:
            raise ValueError(f"历史不存在: {prompt_id}")
        recipe = {
            "id": slugify(name),
            "name": name,
            "description": description or entry.get("description") or "",
            "workflow": entry.get("workflow") or "",
            "slots": entry.get("slots") or {},
            "defaults": _clean_defaults(entry.get("values") or entry.get("defaults") or {}),
            "drop_nodes": list(entry.get("drop_nodes") or []),
        }
        return self.save(recipe)

    def bootstrap(
        self,
        *,
        workflow_name: str,
        wf: dict,
        config_slots: Any = None,
        config_defaults: dict | None = None,
    ) -> dict | None:
        """没有配方时，用自动检测 + 配置下拉生成「默认」配方。"""
        if self.list():
            return self.default()
        try:
            detected = detect_slots(wf)
            configured = slots_from_config(config_slots)
            slots = merge_slots(detected, configured)
            if "prompt" not in slots:
                logger.warning("[ComfyUIDirect] 未能检测到主提示词节点，跳过默认配方")
                return None
            for role, spec in slots.items():
                if "node" in spec and not spec.get("field"):
                    spec["node"] = parse_node_option(spec["node"])
            defaults = read_current_values(wf, slots)
            for key, val in (config_defaults or {}).items():
                if val in (None, "", 0, 0.0, [], {}):
                    continue
                defaults[key] = val
            recipe = {
                "id": "default",
                "name": "默认",
                "description": "导入工作流后自动生成的默认配方",
                "workflow": workflow_name,
                "slots": slots,
                "defaults": _clean_defaults(defaults),
                "drop_nodes": list(ANIMA_DROP_NODES) if looks_like_anima(wf) else [],
            }
            saved = self.save(recipe)
            logger.info("[ComfyUIDirect] 已创建默认配方")
            return saved
        except Exception as e:
            logger.warning(f"[ComfyUIDirect] 创建默认配方失败: {e}")
            return None

    def _load(self, path: Path) -> dict | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"[ComfyUIDirect] 读取配方失败 {path.name}: {e}")
            return None
        if not isinstance(data, dict):
            return None
        data.setdefault("id", path.stem)
        data.setdefault("slots", {})
        data.setdefault("defaults", {})
        return data

    def _public(self, data: dict, stem: str) -> dict:
        defaults = data.get("defaults") or {}
        return {
            "id": data.get("id") or stem,
            "name": data.get("name") or stem,
            "description": data.get("description") or "",
            "workflow": data.get("workflow") or "",
            "model": defaults.get("model") or "",
            "loras": [
                str(x.get("name") or x)
                for x in (defaults.get("loras") or [])
                if x
            ][:4],
            "width": defaults.get("width") or 0,
            "height": defaults.get("height") or 0,
            "has_slots": bool(data.get("slots")),
        }

    def _trim_history(self, keep: int) -> None:
        files = sorted(self.history_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in files[keep:]:
            try:
                path.unlink()
            except OSError:
                pass


def _clean_defaults(raw: dict) -> dict:
    out: dict[str, Any] = {}
    for key in RECIPE_VALUE_KEYS:
        if key not in raw:
            continue
        val = raw[key]
        if val in (None, "", []):
            continue
        if key in ("width", "height", "steps") and val == 0:
            continue
        if key in ("cfg", "denoise") and val == 0:
            continue
        out[key] = val
    return out


def materialize_values(recipe: dict, overrides: dict[str, Any]) -> dict[str, Any]:
    """优先级：LLM/请求显式值 > 配方 defaults。种子从不复用配方里的旧值。"""
    values = dict(recipe.get("defaults") or {})
    values.pop("seed", None)
    for key, val in overrides.items():
        if val is None or val == "":
            continue
        values[key] = val
    return values
