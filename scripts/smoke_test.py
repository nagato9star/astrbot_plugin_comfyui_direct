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
    from typing import Generic, TypeVar

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
    _T = TypeVar("_T")

    class _FunctionTool(Generic[_T]):
        active = True

    class _MessageChain:
        def file_image(self, path):
            return self

    class _ContextWrapper(Generic[_T]):
        pass

    _api.FunctionTool = _FunctionTool
    _event = types.ModuleType("astrbot.api.event")
    _event.AstrMessageEvent = object
    _event.MessageChain = _MessageChain
    _core = types.ModuleType("astrbot.core")
    _agent = types.ModuleType("astrbot.core.agent")
    _run_context = types.ModuleType("astrbot.core.agent.run_context")
    _run_context.ContextWrapper = _ContextWrapper
    _astr_context = types.ModuleType("astrbot.core.astr_agent_context")
    _astr_context.AstrAgentContext = object
    sys.modules["astrbot"] = _astrbot
    sys.modules["astrbot.api"] = _api
    sys.modules["astrbot.api.event"] = _event
    sys.modules["astrbot.core"] = _core
    sys.modules["astrbot.core.agent"] = _agent
    sys.modules["astrbot.core.agent.run_context"] = _run_context
    sys.modules["astrbot.core.astr_agent_context"] = _astr_context

from comfy_client import ComfyUIClient  # noqa: E402
from model_families import ModelFamilyRegistry, WorkflowProfileStore  # noqa: E402
from recipe_store import RecipeStore  # noqa: E402
from slot_mapping import (  # noqa: E402
    apply_slots,
    collect_trigger_words,
    detect_slots,
    node_options_for_slot,
    parse_node_option,
    read_current_values,
    resolve_size,
    slots_from_config,
    ui_to_api,
)
from workflow_builder import WorkflowBuilder  # noqa: E402
from tools import (  # noqa: E402
    ComfyuiDrawTool,
    ComfyuiGenerateTool,
    ComfyuiRecipeDrawTool,
    ComfyuiRecipeTool,
)

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


def test_power_lora_dynamic_slots() -> None:
    """保存的 LoRA 必须能注入原本没有 lora_N 的 Power Loader。"""
    wf = {
        "30": {
            "class_type": "Power Lora Loader (rgthree)",
            "inputs": {"model": ["19", 0], "clip": ["17", 0]},
        }
    }
    slots = {"loras": {"node": "30", "field": "lora"}}
    apply_slots(
        wf,
        slots,
        {
            "loras": [
                {"name": "Krea-2\\krea2-masterpieces-v51.safetensors", "strength": 0.8},
                {"name": "Krea-2\\second.safetensors", "strength": 0.65},
            ]
        },
    )
    inputs = wf["30"]["inputs"]
    assert inputs["lora_1"] == {
        "on": True,
        "lora": "Krea-2\\krea2-masterpieces-v51.safetensors",
        "strength": 0.8,
    }
    assert inputs["lora_2"]["on"] is True
    assert inputs["lora_2"]["strength"] == 0.65
    apply_slots(wf, slots, {"loras": []})
    assert inputs["lora_1"]["on"] is False
    assert inputs["lora_2"]["on"] is False
    print("  power lora dynamic slots OK")


def test_power_lora_reuses_matching_slot() -> None:
    """Krea2 预留在 lora_2 的文件名应在原槽位启用。"""
    wf = {
        "30": {
            "class_type": "Power Lora Loader (rgthree)",
            "inputs": {
                "lora_1": {
                    "on": False,
                    "lora": "Krea-2\\krea2_vrchat photography style.safetensors",
                    "strength": 1,
                },
                "lora_2": {
                    "on": False,
                    "lora": "Krea-2\\krea2-masterpieces-v51.safetensors",
                    "strength": 1,
                },
            },
        }
    }
    apply_slots(
        wf,
        {"loras": {"node": "30", "field": "lora"}},
        {
            "loras": [
                {"name": "Krea-2\\krea2-masterpieces-v51.safetensors", "strength": 0.8}
            ]
        },
    )
    assert wf["30"]["inputs"]["lora_1"]["on"] is False
    assert wf["30"]["inputs"]["lora_2"] == {
        "on": True,
        "lora": "Krea-2\\krea2-masterpieces-v51.safetensors",
        "strength": 0.8,
    }
    print("  power lora matching slot OK")


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
    # 传裸节点 id 时不得重复插入同一 label（配置下拉曾因此出现双份选项）
    wf = _load_fixture("mini_workflow.json")
    for selected in ("2", "2 — CLIPTextEncode — 正面提示词", ""):
        options = node_options_for_slot(wf, "prompt", selected)
        assert len(options) == len(set(options)), options
    assert node_options_for_slot(wf, "prompt", "2")[1].startswith("2 —")
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
        preferred = RecipeStore(Path(td), preferred_default="立绘")
        assert preferred.default()["name"] == "立绘"
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


def test_model_family_routing_and_recipe_decoupling() -> None:
    registry = ModelFamilyRegistry(
        [
            {
                "__template_key": "family",
                "name": "anima",
                "workflow": "anime-flow",
                "prompt_style": "danbooru",
            },
            {
                "__template_key": "family",
                "name": "Krea2",
                "workflow": "photo-flow",
                "prompt_style": "natural",
            },
        ]
    )
    assert registry.names() == ["anima", "Krea2"]
    assert registry.get("KREA2").workflow == "photo-flow"
    assert registry.by_workflow("anime-flow").name == "anima"

    with tempfile.TemporaryDirectory() as td:
        store = RecipeStore(Path(td))
        saved = store.save(
            {
                "name": "柔光立绘",
                "family": "Krea2",
                "workflow": "should-not-be-saved",
                "slots": {"prompt": {"node": "2"}},
                "defaults": {
                    "model": "krea2-photo.safetensors",
                    "loras": [{"name": "soft-light.safetensors", "strength": 0.7}],
                    "steps": 18,
                },
            }
        )
        assert saved["family"] == "Krea2"
        assert "workflow" not in saved and "template" not in saved and "slots" not in saved
        assert registry.resolve_recipe(saved).name == "Krea2"
        disk = json.loads(store.path_for(saved["id"]).read_text(encoding="utf-8"))
        assert "workflow" not in disk and "slots" not in disk
        disabled = store.save(
            {
                "name": "无 LoRA",
                "family": "anima",
                "defaults": {"loras": []},
            }
        )
        assert disabled["defaults"]["loras"] == []
    print("  model family routing / recipe decoupling OK")


def test_workflow_profile_store() -> None:
    with tempfile.TemporaryDirectory() as td:
        profiles = WorkflowProfileStore(Path(td))
        wf = _load_fixture("mini_workflow.json")
        effective = profiles.effective("mini", wf)
        assert effective["slots"]["prompt"]["node"] == "2"
        profiles.save(
            "mini",
            {
                **effective["slots"],
                "prompt": {"node": "3", "field": "text"},
            },
            ["99"],
        )
        reloaded = WorkflowProfileStore(Path(td)).effective("mini", wf)
        assert reloaded["slots"]["prompt"]["node"] == "3"
        assert reloaded["drop_nodes"] == ["99"]
        profiles.save("minimal", {"prompt": {"node": "2"}}, [])
        minimal = profiles.effective("minimal", wf)
        assert set(minimal["slots"]) == {"prompt"}

        legacy_store = RecipeStore(Path(td) / "legacy")
        legacy = legacy_store.save(
            {
                "name": "旧配方",
                "workflow": "legacy-flow",
                "slots": {"prompt": {"node": "7"}},
                "defaults": {"model": "base.safetensors"},
            }
        )
        assert profiles.import_legacy_recipe(legacy) is True
        assert profiles.get("legacy-flow")["slots"]["prompt"]["node"] == "7"
    print("  workflow profile store OK")


def test_llm_entry_schemas() -> None:
    schema = json.loads((PLUGIN / "_conf_schema.json").read_text(encoding="utf-8"))
    family_schema = schema["model_families"]
    assert family_schema["type"] == "template_list"
    assert {"name", "workflow", "prompt_style", "description"}.issubset(
        family_schema["templates"]["family"]["items"]
    )
    registry = ModelFamilyRegistry(
        [
            {"name": "anima", "workflow": "anime-flow", "prompt_style": "danbooru"},
            {"name": "krea2", "workflow": "photo-flow", "prompt_style": "natural"},
        ]
    )
    draw = ComfyuiDrawTool(families=registry)
    draw.refresh_schema()
    assert draw.name == "comfyui_draw"
    assert draw.parameters["required"] == ["model_family", "prompt"]
    assert draw.parameters["properties"]["model_family"]["enum"] == ["anima", "krea2"]

    with tempfile.TemporaryDirectory() as td:
        store = RecipeStore(Path(td), preferred_default="柔光")
        profiles = WorkflowProfileStore(Path(td))
        store.save(
            {
                "name": "柔光",
                "family": "krea2",
                "defaults": {"loras": [{"name": "soft.safetensors", "strength": 0.7}]},
            }
        )
        recipe_draw = ComfyuiRecipeDrawTool(store=store, families=registry)
        recipe_draw.refresh_schema()
        assert recipe_draw.name == "comfyui_recipe_draw"
        assert recipe_draw.parameters["required"] == ["prompt"]
        assert recipe_draw.parameters["properties"]["recipe"]["enum"] == ["柔光"]
        assert ComfyuiGenerateTool(
            store=store,
            families=registry,
            profiles=profiles,
        ).name == "comfyui_generate"
        assert ComfyuiRecipeTool(store=store, families=registry).name == "comfyui_recipe"
    print("  llm entry schemas OK")


def test_family_and_recipe_generation_paths() -> None:
    import copy
    import types

    class FakeClient(ComfyUIClient):
        timeout = 1

        def __init__(self):
            self.submitted = []

        async def list_resources(self):
            return (
                {
                    "unet_name": ["base.safetensors", "other.safetensors"],
                    "lora_name": ["style.safetensors", "soft.safetensors"],
                    "lora_meta": {},
                },
                False,
            )

        async def submit_prompt_detail(self, workflow):
            self.submitted.append(copy.deepcopy(workflow))
            return f"pid-{len(self.submitted)}", None

        async def get_history_entry(self, prompt_id):
            return {
                "status": {"status_str": "success"},
                "outputs": {
                    "6": {
                        "images": [
                            {
                                "filename": f"{prompt_id}.png",
                                "subfolder": "",
                                "type": "output",
                            }
                        ]
                    }
                },
            }

        async def download_image(self, *args, **kwargs):
            return b"fake-image"

    class FakeEvent:
        unified_msg_origin = "test:family-routing"

        def __init__(self):
            self.sent = 0

        async def send(self, chain):
            self.sent += 1

    async def run():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            builder = WorkflowBuilder(plugin_dir=PLUGIN, default_workflow="mini", custom_dir=root / "workflows")
            builder.save_template("mini", _load_fixture("mini_workflow.json"))
            registry = ModelFamilyRegistry(
                [{"name": "demo", "workflow": "mini", "prompt_style": "natural"}]
            )
            profiles = WorkflowProfileStore(root)
            profiles.ensure("mini", builder.load_template("mini"))
            store = RecipeStore(root, preferred_default="柔光")
            client = FakeClient()
            event = FakeEvent()
            context = types.SimpleNamespace(context=types.SimpleNamespace(event=event))
            draw = ComfyuiDrawTool(
                client=client,
                builder=builder,
                store=store,
                output_dir=root / "output",
                shared={},
                families=registry,
                profiles=profiles,
            )
            (root / "output").mkdir()
            result = await draw.call(
                context,
                model_family="demo",
                prompt="a silver cat",
                model="other",
                lora='[{"name":"soft","strength":0.55}]',
                steps=12,
                save_as="柔光",
            )
            assert "家族=demo" in result
            assert client.submitted[-1]["2"]["inputs"]["text"] == "a silver cat"
            assert client.submitted[-1]["1"]["inputs"]["unet_name"] == "other.safetensors"
            assert client.submitted[-1]["7"]["inputs"]["lora_1"]["lora"] == "soft.safetensors"
            assert client.submitted[-1]["5"]["inputs"]["steps"] == 12
            saved = store.get("柔光")
            assert saved["family"] == "demo" and "workflow" not in saved

            recipe_draw = ComfyuiRecipeDrawTool(
                draw_tool=draw,
                store=store,
                families=registry,
            )
            result2 = await recipe_draw.call(context, recipe="柔光", prompt="a blue bird", seed=8)
            assert "配方=柔光" in result2
            assert client.submitted[-1]["2"]["inputs"]["text"] == "a blue bird"
            assert client.submitted[-1]["1"]["inputs"]["unet_name"] == "other.safetensors"
            assert client.submitted[-1]["5"]["inputs"]["seed"] == 8
            assert event.sent == 2

            builder.save_template("mini-v2", _load_fixture("mini_workflow.json"))
            moved_registry = ModelFamilyRegistry(
                [{"name": "demo", "workflow": "mini-v2", "prompt_style": "natural"}]
            )
            profiles.ensure("mini-v2", builder.load_template("mini-v2"))
            moved_draw = ComfyuiDrawTool(
                client=client,
                builder=builder,
                store=store,
                output_dir=root / "output",
                shared={},
                families=moved_registry,
                profiles=profiles,
            )
            moved_recipe_draw = ComfyuiRecipeDrawTool(
                draw_tool=moved_draw,
                store=store,
                families=moved_registry,
            )
            result3 = await moved_recipe_draw.call(context, recipe="柔光", prompt="new workflow")
            assert "工作流=mini-v2" in result3
            assert event.sent == 3

    asyncio.run(run())
    print("  family / recipe generation paths OK")


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


def test_generation_entry_resolution() -> None:
    """双入口判定：显式 recipe > 显式 workflow > 默认配方 > 模板。"""
    from recipe_store import resolve_generation_entry

    assert resolve_generation_entry("Krea", "anima-v3", has_default_recipe=True) == (
        "recipe",
        "Krea",
    )
    # 显式 workflow 强制模板入口，不被默认配方吞掉
    assert resolve_generation_entry("", "anima-v3", has_default_recipe=True) == (
        "workflow",
        "anima-v3",
    )
    assert resolve_generation_entry("", "", has_default_recipe=True) == ("recipe", "")
    assert resolve_generation_entry("", "", has_default_recipe=False) == ("workflow", "")
    print("  generation entry resolution OK")


def test_recipe_base_slots_binding() -> None:
    """配方保存继承基底工作流的槽位映射：默认配方优先，其次同工作流配方。"""
    with tempfile.TemporaryDirectory() as td:
        store = RecipeStore(Path(td))
        assert store.base_slots_for("mini") == ({}, "")
        wf = _load_fixture("mini_workflow.json")
        store.bootstrap(workflow_name="mini", wf=wf, config_slots={"prompt": "2", "sampler": "5"})
        slots, src = store.base_slots_for("mini")
        assert slots["prompt"]["node"] == "2"
        assert src == "默认"
        store.save(
            {
                "name": "立绘",
                "workflow": "mini",
                "slots": slots,
                "defaults": {"model": "a.safetensors"},
            }
        )
        slots2, src2 = store.base_slots_for("mini")
        assert slots2["prompt"]["node"] == "2"
        assert src2 == "默认"
        # 未绑定 / 无同工作流配方时无可继承映射
        assert store.base_slots_for("") == ({}, "")
        assert store.base_slots_for("other-wf") == ({}, "")
    print("  recipe base slots binding OK")


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
    test_power_lora_dynamic_slots()
    test_power_lora_reuses_matching_slot()
    test_slot_mapping_anima_like()
    test_config_dropdown_and_size()
    test_recipe_store_and_draw_schema()
    test_model_family_routing_and_recipe_decoupling()
    test_workflow_profile_store()
    test_llm_entry_schemas()
    test_family_and_recipe_generation_paths()
    test_ui_to_api()
    test_workflow_build()
    test_defaults_precedence()
    test_generation_entry_resolution()
    test_recipe_base_slots_binding()
    test_dual_sampler_external_int()
    test_collect_trigger_words()
    test_lora_list_input()
    test_template_management()
    test_submit_error_parse()
    test_cache_atomic()
    print("ALL OK")


if __name__ == "__main__":
    main()
