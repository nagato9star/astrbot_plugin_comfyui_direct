# -*- coding: utf-8 -*-
"""冒烟测试：部署/修改后快速验证插件核心逻辑（无 AstrBot 环境也可跑）。

用法：python scripts/smoke_test.py
覆盖：工作流角色覆盖 / 默认值优先级 / 模板管理 / 提交错误解析 / 缓存原子写。
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

import httpx

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PLUGIN_DIR))

if "astrbot" not in sys.modules:
    import types

    _astrbot = types.ModuleType("astrbot")
    _api = types.ModuleType("astrbot.api")

    class _Logger:
        def warning(self, *a, **k):
            pass

        def info(self, *a, **k):
            pass

        def error(self, *a, **k):
            pass

    _api.logger = _Logger()
    sys.modules["astrbot"] = _astrbot
    sys.modules["astrbot.api"] = _api

from comfy_client import ComfyUIClient  # noqa: E402
from recipe_store import RecipeStore  # noqa: E402
from slot_mapping import (  # noqa: E402
    apply_slots,
    collect_trigger_words,
    detect_slots,
    parse_node_option,
    read_current_values,
    resolve_size,
    slots_from_config,
    ui_to_api,
)
from workflow_builder import WorkflowBuilder  # noqa: E402

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"

PLUGIN = _PLUGIN_DIR
# 模板在插件数据目录（插件包不含模板）
_DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "plugin_data" / "astrbot_plugin_comfyui_direct"
_CUSTOM_DIR = (_DATA_DIR / "workflows") if (_DATA_DIR / "workflows").is_dir() else None


def _builder(default_workflow: str = "anima-v3") -> WorkflowBuilder:
    return WorkflowBuilder(plugin_dir=PLUGIN, default_workflow=default_workflow, custom_dir=_CUSTOM_DIR)


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def test_slot_mapping_generic() -> None:
    wf = _load_fixture("mini_workflow.json")
    slots = detect_slots(wf)
    assert slots["prompt"]["node"] == "2"
    assert slots["negative"]["node"] == "3"
    assert slots["model"]["node"] == "1"
    assert slots["sampler"]["node"] == "5"
    assert slots["size"]["node"] == "4"
    assert slots["loras"]["node"] == "7"
    values = read_current_values(wf, slots)
    assert values["model"] == "base.safetensors"
    assert values["width"] == 832 and values["height"] == 1216
    assert values["steps"] == 20
    assert values["loras"][0]["name"] == "style.safetensors"
    apply_slots(
        wf,
        slots,
        {
            "prompt": "cat",
            "model": "other.safetensors",
            "width": 1024,
            "height": 1024,
            "steps": 12,
            "loras": [{"name": "new.safetensors", "strength": 0.4}],
            "seed": 9,
        },
    )
    assert wf["2"]["inputs"]["text"] == "cat"
    assert wf["1"]["inputs"]["unet_name"] == "other.safetensors"
    assert wf["4"]["inputs"]["width"] == 1024
    assert wf["5"]["inputs"]["steps"] == 12
    assert wf["5"]["inputs"]["seed"] == 9
    assert wf["7"]["inputs"]["lora_1"]["lora"] == "new.safetensors"
    print("  slot mapping generic OK")


def test_slot_mapping_anima_like() -> None:
    wf = _load_fixture("anima_like.json")
    slots = detect_slots(wf)
    assert slots["prompt"]["node"] == "22"
    assert slots["artist"]["node"] == "20"
    assert slots["quality"]["node"] == "21"
    assert slots["trigger_words"]["node"] == "23"
    apply_slots(wf, slots, {"prompt": "1girl, garden", "artist": "(@x:1.0)", "trigger_words": "@t"})
    assert wf["22"]["inputs"]["prompt"] == "1girl, garden"
    assert wf["20"]["inputs"]["prompt"] == "(@x:1.0)"
    assert wf["23"]["inputs"]["prompt"] == "@t"
    print("  slot mapping anima-like OK")


def test_config_dropdown_and_size() -> None:
    assert parse_node_option("353 — CR Prompt Text — 主提示词") == "353"
    assert parse_node_option("") == ""
    slots = slots_from_config(
        {"prompt": "2 — CLIPTextEncode — 正面提示词", "sampler": "5 — KSampler"}
    )
    assert slots["prompt"]["node"] == "2"
    assert slots["sampler"]["node"] == "5"
    assert resolve_size(832, 1216, "landscape") == (1216, 832)
    assert resolve_size(832, 1216, "portrait") == (832, 1216)
    print("  config dropdown parse OK")


def test_recipe_store_and_draw_schema() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = RecipeStore(Path(td))
        wf = _load_fixture("mini_workflow.json")
        store.bootstrap(workflow_name="mini", wf=wf, config_slots={"prompt": "2", "sampler": "5"})
        rec = store.default()
        assert rec is not None
        assert rec["slots"]["prompt"]["node"] == "2"
        assert rec["defaults"]["model"] == "base.safetensors"
        assert "seed" not in rec["defaults"]
        store.save(
            {
                "name": "立绘",
                "workflow": "mini",
                "slots": rec["slots"],
                "defaults": rec["defaults"],
            }
        )
        assert "立绘" in store.names()
        catalog = store.catalog()
        assert "立绘" in catalog
        assert "base" in catalog
        hist_id = "abc123"
        store.save_history(
            {
                "prompt_id": hist_id,
                "workflow": "mini",
                "slots": rec["slots"],
                "values": rec["defaults"],
                "prompt": "1girl",
            }
        )
        copied = store.recipe_from_history(hist_id, "从历史")
        assert copied["name"] == "从历史"
        assert copied["defaults"]["model"] == "base.safetensors"
    print("  recipe store / draw schema OK")


def test_ui_to_api() -> None:
    ui = {
        "nodes": [
            {
                "id": 3,
                "type": "KSampler",
                "title": "采样",
                "widgets_values": [11, "randomize", 30, 4.5, "euler", "karras", 1],
                "inputs": [{"name": "model", "link": 1}],
            },
            {"id": 4, "type": "UNETLoader", "widgets_values": ["m.safetensors"]},
        ],
        "links": [[1, 4, 0, 3, 0, "MODEL"]],
    }
    wf = ui_to_api(ui)
    assert wf["3"]["class_type"] == "KSampler"
    assert wf["3"]["inputs"]["model"] == ["4", 0]
    assert wf["3"]["inputs"]["seed"] == 11
    assert wf["4"]["class_type"] == "UNETLoader"
    print("  ui to api OK")


def test_workflow_build() -> None:
    """anima-v3 五段式覆盖 + LoRA 插槽 + KSampler。模板不存在则跳过。"""
    b = _builder()
    try:
        wf = b.build(
            prompt="1girl, garden",
            artist="(@testartist:1.0)",
            trigger_words="@f1f",
            quality="masterpiece, best quality",
            negative_prompt="bad hands",
            model="m.safetensors",
            lora='[{"name":"l1.safetensors","strength":0.5}]',
            steps=20,
            cfg=1,
            seed=1,
        )
    except FileNotFoundError:
        print("  workflow build SKIP (anima-v3 未安装)")
        return
    assert "445" not in wf
    assert wf["353"]["inputs"]["prompt"] == "1girl, garden"
    assert wf["368"]["inputs"]["prompt"] == "(@testartist:1.0)"
    assert wf["461"]["inputs"]["prompt"] == "@f1f"
    assert wf["362"]["inputs"]["steps"] == 20
    assert wf["478"]["inputs"]["lora_1"]["on"] is True
    assert wf["458"]["inputs"]["unet_name"] == "m.safetensors"
    print("  workflow build OK")


def test_defaults_precedence() -> None:
    """配方 defaults 覆盖：显式值优先，0/空视为未配置。种子不复用配方旧值。"""
    from recipe_store import materialize_values

    recipe = {"defaults": {"model": "a.safetensors", "steps": 20, "width": 832, "seed": 7}}
    values = materialize_values(recipe, {"prompt": "1girl", "steps": 12, "width": None})
    assert values["model"] == "a.safetensors"
    assert values["steps"] == 12
    assert values["width"] == 832
    assert values["prompt"] == "1girl"
    assert "seed" not in values
    values2 = materialize_values(recipe, {"prompt": "1girl", "seed": 99})
    assert values2["seed"] == 99
    print("  defaults precedence OK")


def test_dual_sampler_external_int() -> None:
    """双采样 + 外联 Int：步数写到两个整数节点，种子写到共享 Int，连线不断开。"""
    wf = _load_fixture("dual_sampler.json")
    slots = detect_slots(wf)
    assert slots["prompt"]["node"] == "2"
    assert slots["sampler"]["node"] in ("20", "21")
    assert slots.get("sampler_2", {}).get("node") in ("20", "21")
    assert slots["sampler"]["node"] != slots["sampler_2"]["node"]
    values = read_current_values(wf, slots)
    assert values["steps"] == 8
    assert "seed" not in values
    apply_slots(wf, slots, {"prompt": "cat", "steps": 20, "seed": 99})
    assert wf["2"]["inputs"]["text"] == "cat"
    assert wf["20"]["inputs"]["steps"] == ["10", 0]
    assert wf["21"]["inputs"]["steps"] == ["11", 0]
    assert wf["20"]["inputs"]["noise_seed"] == ["12", 0]
    assert wf["10"]["inputs"]["value"] == 20
    assert wf["11"]["inputs"]["value"] == 20
    assert wf["12"]["inputs"]["value"] == 99
    # 映射到整数节点时同样能改步数
    wf2 = _load_fixture("dual_sampler.json")
    slots2 = dict(slots)
    slots2["sampler"] = {"node": "10", "field": "value"}
    apply_slots(wf2, slots2, {"steps": 16, "seed": 3})
    assert wf2["10"]["inputs"]["value"] == 16
    assert wf2["11"]["inputs"]["value"] == 16
    assert wf2["12"]["inputs"]["value"] == 3
    print("  dual sampler external int OK")


def test_collect_trigger_words() -> None:
    meta = {
        "a.safetensors": {"trigger_words": ["zoda", "1girl"]},
        "b.safetensors": {"trigger_words": ["zoda", "maid"]},
    }
    text = collect_trigger_words(
        meta, [{"name": "a.safetensors"}, {"name": "b.safetensors"}]
    )
    assert text == "zoda, 1girl, maid"
    assert collect_trigger_words(meta, []) == ""
    print("  collect trigger words OK")


def test_lora_list_input() -> None:
    """LLM 直接传数组对象（非 JSON 字符串）时，lora 不能丢。"""
    b = _builder()
    try:
        wf = b.build(
            prompt="1girl",
            lora=[{"name": "l1.safetensors", "strength": 0.5}],
        )
    except FileNotFoundError:
        print("  lora list/dict input SKIP (anima-v3 未安装)")
        return
    assert wf["478"]["inputs"]["lora_1"]["on"] is True
    assert wf["478"]["inputs"]["lora_1"]["lora"] == "l1.safetensors"
    wf2 = b.build(prompt="1girl", lora={"name": "l2.safetensors", "strength": 0.7})
    assert wf2["478"]["inputs"]["lora_1"]["on"] is True
    assert wf2["478"]["inputs"]["lora_1"]["lora"] == "l2.safetensors"
    print("  lora list/dict input OK")


def test_template_management() -> None:
    with tempfile.TemporaryDirectory() as td:
        b2 = WorkflowBuilder(plugin_dir=PLUGIN, custom_dir=Path(td))
        wf = _load_fixture("mini_workflow.json")
        b2.save_template("mini", wf)
        names = [t["name"] for t in b2.list_templates()]
        assert "mini" in names
        b2.delete_template("mini")
    print("  template management OK")


def test_submit_error_parse() -> None:
    def handler(request):
        return httpx.Response(
            400,
            json={
                "error": {"message": "ValueError: 模型不存在"},
                "node_errors": {"458": {"errors": [{"message": "File not found", "node_id": "458"}]}},
            },
        )

    c = ComfyUIClient(host="t", port=1, cache_file=None)
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://t:1")

    async def run():
        pid, err = await c.submit_prompt_detail({})
        assert pid is None and err and "模型不存在" in err and "458" in err
        await c.close()

    asyncio.run(run())
    print("  submit error parse OK")


def test_cache_atomic() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "models.json"
        c = ComfyUIClient(host="t", port=1, cache_file=p, cache_ttl=0)
        c._save_cache({"fetched_at": 1, "resources": {"unet_name": ["a.safetensors"]}})
        assert p.exists() and "a.safetensors" in p.read_text(encoding="utf-8")
    print("  cache atomic write OK")


def main() -> None:
    print("[smoke] astrbot_plugin_comfyui_direct 冒烟测试")
    test_slot_mapping_generic()
    test_slot_mapping_anima_like()
    test_config_dropdown_and_size()
    test_recipe_store_and_draw_schema()
    test_ui_to_api()
    test_workflow_build()
    test_defaults_precedence()
    test_dual_sampler_external_int()
    test_collect_trigger_words()
    test_lora_list_input()
    test_template_management()
    test_submit_error_parse()
    test_cache_atomic()
    print("ALL OK")


if __name__ == "__main__":
    main()
