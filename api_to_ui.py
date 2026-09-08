"""API 格式工作流 → UI 格式快照转换器。

背景：插件通过 /prompt API 裸提交时，ComfyUI 保存的 PNG 只有 `prompt`
chunk（API 格式），没有 `workflow` chunk（UI 格式）。前端拖图加载时，
旧版前端/旧版 rgthree 扩展无法从 API 格式还原 Power Lora Loader 的
动态 lora 槽，导致用户在界面里看到 lora 为空。

修复：提交时同步携带 extra_data.extra_pnginfo.workflow（UI 快照），
PNG 即内嵌标准 workflow 元数据，任何版本前端拖图都能完整还原。

转换规则：
- API dict: {node_id: {class_type, inputs: {field: value}}}
- 连线值形如 [source_id, output_slot]，转成 UI links + 端口 link 引用
- widget 值按 object_info 定义顺序排入 widgets_values；
  带 control_after_generate 的 INT 字段需追加 control 值（"fixed"）
- rgthree Power Lora Loader 的动态 lora_N 字典值 → widgets_values
  = [{on, lora, strength}, ...]（与其前端 configure() 逻辑对齐）
"""

from __future__ import annotations

import json
import re
import logging

logger = logging.getLogger("[ComfyUIDirect]")

# 常见连线端口类型（API 值为 [node_id, slot] 二元组）
_LINK_RE = re.compile(r"lora_\d+$")
_DEFAULT_SIZE = [300.0, 130.0]


def _num(v) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str) and v.lstrip("-").isdigit():
        return int(v)
    return None


def _is_link(value) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and _num(value[0]) is not None
        and _num(value[1]) is not None
    )


def _needs_control_after_generate(field_type) -> bool:
    if isinstance(field_type, list) and len(field_type) >= 2:
        cfg = field_type[1]
        return isinstance(cfg, dict) and "control_after_generate" in cfg
    return False


def api_to_ui(api: dict, object_info: dict | None = None) -> dict:
    """把 ComfyUI API 格式工作流转换为 UI 格式（extra_pnginfo.workflow 用）。"""
    nodes: list[dict] = []
    links: list[list] = []
    link_id = 1
    order = 0

    for nid_s, node in api.items():
        try:
            nid = int(nid_s)
        except (TypeError, ValueError):
            continue
        class_type = node.get("class_type", "")
        info = (object_info or {}).get(class_type, {})
        input_def = info.get("input", {})
        out_types = info.get("output", [])
        out_names = info.get("output_name", [])

        in_defs = {}
        for group in ("required", "optional"):
            for k, v in (input_def.get(group) or {}).items():
                in_defs[k] = v

        ui_inputs: list[dict] = []
        ui_outputs: list[dict] = []
        widgets_values: list = []
        rgthree_loras: list[dict] = []
        is_rgthree_pll = "Power Lora Loader" in class_type

        # 输出端口
        for i, t in enumerate(out_types):
            ui_outputs.append(
                {
                    "name": out_names[i] if i < len(out_names) else str(t),
                    "type": t,
                    "links": [],
                    "slot_index": i,
                }
            )

        for field, value in node.get("inputs", {}).items():
            if is_rgthree_pll and _LINK_RE.match(field) and isinstance(value, dict):
                rgthree_loras.append(value)
                continue
            if _is_link(value):
                src, slot = _num(value[0]), _num(value[1])
                ltype = in_defs.get(field, [None])[0] if field in in_defs else None
                if not isinstance(ltype, str):
                    ltype = "*"
                slot_idx = len(ui_inputs)
                ui_inputs.append({"name": field, "type": ltype, "link": link_id})
                links.append([link_id, src, slot, nid, slot_idx, ltype])
                link_id += 1
                continue
            if field in in_defs and _needs_control_after_generate(in_defs[field]):
                widgets_values.extend([value, "fixed"])
            else:
                widgets_values.append(value)

        if is_rgthree_pll:
            # rgthree configure() 期望 widgets_values 为 lora 对象列表
            widgets_values = rgthree_loras

        pos = [(order % 5) * 340.0, (order // 5) * 300.0]
        nodes.append(
            {
                "id": nid,
                "type": class_type,
                "pos": pos,
                "size": list(_DEFAULT_SIZE),
                "flags": {},
                "order": order,
                "mode": 0,
                "inputs": ui_inputs,
                "outputs": ui_outputs,
                "properties": {"Node name for S&R": class_type},
                "widgets_values": widgets_values,
            }
        )
        order += 1

    # 回填源节点输出端口的 links 引用
    by_id = {n["id"]: n for n in nodes}
    for lk in links:
        _, src, slot, _, _, ltype = lk
        src_node = by_id.get(src)
        if src_node and slot < len(src_node["outputs"]):
            src_node["outputs"][slot].setdefault("links", []).append(lk[0])

    return {
        "last_node_id": max((n["id"] for n in nodes), default=0),
        "last_link_id": link_id - 1,
        "nodes": nodes,
        "links": links,
        "groups": [],
        "config": {},
        "extra": {},
        "version": 0.4,
    }


def build_extra_pnginfo(api: dict, object_info: dict | None = None) -> dict | None:
    """构造 /prompt 的 extra_data.extra_pnginfo；失败返回 None（裸提交兜底）。"""
    try:
        return {"workflow": api_to_ui(api, object_info)}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[ComfyUIDirect] UI 快照构造失败，跳过元数据: {e}")
        return None


def _selftest():  # pragma: no cover
    import sys

    api = json.load(open(sys.argv[1]))
    obj = None
    try:
        obj = json.load(open("/tmp/objinfo.json"))
    except Exception:
        pass
    ui = api_to_ui(api, obj)
    print(json.dumps(ui, ensure_ascii=False)[:1500])
    print("nodes:", len(ui["nodes"]), "links:", len(ui["links"]))


if __name__ == "__main__":
    _selftest()
