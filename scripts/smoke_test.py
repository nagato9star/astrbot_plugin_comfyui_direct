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

from comfy_client import (  # noqa: E402
    ComfyUIClient,
    execution_error_message,
    image_media_type,
)
from api_to_ui import api_to_ui  # noqa: E402
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


def test_power_lora_preserves_independent_accelerator() -> None:
    """清空 Power 占位槽时必须保留 Anima 双采样的独立加速分支。"""
    wf = {
        "458": {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": "Anima\\anima_baseV10.safetensors"},
        },
        "428": {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {
                "lora_name": "Anima\\加速_Turbo_v2.9.safetensors",
                "strength_model": 0.8,
                "model": ["458", 0],
            },
            "_meta": {"title": "加速"},
        },
        "478": {
            "class_type": "Power Lora Loader (rgthree)",
            "inputs": {
                "model": ["458", 0],
                "lora_1": {
                    "on": False,
                    "lora": "Anima\\风格_占位.safetensors",
                    "strength": 1.0,
                },
            },
            "_meta": {"title": "权重Lora加载器"},
        },
        "509": {
            "class_type": "XB_ROCmKSamplerAdvanced",
            "inputs": {"model": ["478", 0], "end_at_step": 12},
            "_meta": {"title": "一采"},
        },
        "510": {
            "class_type": "XB_ROCmKSamplerAdvanced",
            "inputs": {
                "model": ["428", 0],
                "start_at_step": 12,
                "latent": ["509", 0],
            },
            "_meta": {"title": "二采"},
        },
    }
    slots = {"loras": {"node": "478", "field": "lora"}}
    accelerator = json.loads(json.dumps(wf["428"], ensure_ascii=False))

    apply_slots(
        wf,
        slots,
        {"loras": [{"name": "Anima\\人物_测试.safetensors", "strength": 0.7}]},
    )
    assert wf["478"]["inputs"]["lora_1"]["on"] is True
    assert wf["428"] == accelerator

    apply_slots(wf, slots, {"loras": []})
    assert wf["478"]["inputs"]["lora_1"]["on"] is False
    assert wf["428"] == accelerator
    assert wf["510"]["inputs"]["model"] == ["428", 0]
    print("  power lora preserves independent accelerator OK")


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
    assert resolve_size(1024, 1024, "portrait") == (832, 1216)
    assert resolve_size(1024, 1024, "landscape") == (1216, 832)
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

        configured = WorkflowProfileStore(
            Path(td),
            configured=[
                {
                    "__template_key": "mapping",
                    "workflow": "mini",
                    "prompt": "2 — CLIPTextEncode — 正面提示词",
                    "sampler": "5",
                    "sampler_2": "",
                }
            ],
        )
        assert configured.configured("MINI")["prompt"]["node"] == "2"
        configured_effective = configured.effective("mini", wf)
        assert configured_effective["source"] == "config"
        assert configured_effective["slots"]["prompt"]["node"] == "2"
        assert configured_effective["slots"]["sampler"]["node"] == "5"
        assert set(configured_effective["slots"]) == {"prompt", "sampler"}
        # 配置映射是运行时覆盖；磁盘中的 Workflow Studio 档案保持可回退。
        assert configured.get("mini")["slots"]["prompt"]["node"] == "3"
        fallback = WorkflowProfileStore(Path(td)).effective("mini", wf)
        assert fallback["source"] == "profile"
        assert fallback["slots"]["prompt"]["node"] == "3"

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
    mapping_schema = schema["workflow_node_mappings"]
    assert mapping_schema["type"] == "template_list"
    assert {
        "workflow",
        "prompt",
        "model",
        "loras",
        "size",
        "sampler",
        "sampler_2",
        "negative",
        "artist",
        "quality",
        "trigger_words",
        "clip",
        "vae",
        "guidance",
    }.issubset(mapping_schema["templates"]["mapping"]["items"])
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


def test_api_to_ui_linked_widget_positions() -> None:
    """连线 widget 保留固定位置，后续值不得整体前移。"""
    sampler_inputs = {
        "model": ["MODEL"],
        "add_noise": [["enable", "disable"]],
        "noise_seed": ["INT", {"default": 0, "control_after_generate": True}],
        "steps": ["INT", {"default": 20}],
        "cfg": ["FLOAT", {"default": 8.0}],
        "sampler_name": [["euler_ancestral", "er_sde"]],
        "scheduler": [["sgm_uniform", "normal"]],
        "start_at_step": ["INT", {"default": 0}],
        "end_at_step": ["INT", {"default": 10000}],
        "return_with_leftover_noise": [["enable", "disable"]],
    }
    object_info = {
        "Int": {
            "input": {"required": {"value": ["INT", {"default": 0}]}},
            "output": ["INT"],
            "output_name": ["INT"],
        },
        "EmptyLatentImage": {
            "input": {
                "required": {
                    "width": ["INT", {"default": 512}],
                    "height": ["INT", {"default": 512}],
                    "batch_size": ["INT", {"default": 1}],
                }
            },
            "output": ["LATENT"],
            "output_name": ["LATENT"],
        },
        "KSamplerAdvanced": {"input": {"required": sampler_inputs}},
        "XB_ROCmKSamplerAdvanced": {"input": {"required": sampler_inputs}},
    }
    api = {
        "10": {"class_type": "Int", "inputs": {"value": 512}},
        "11": {"class_type": "Int", "inputs": {"value": 512}},
        "12": {"class_type": "Int", "inputs": {"value": 863754493834392}},
        "13": {"class_type": "Int", "inputs": {"value": 24}},
        "4": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": ["10", 0], "height": ["11", 0], "batch_size": 1},
        },
        "20": {
            "class_type": "KSamplerAdvanced",
            "inputs": {
                "model": ["99", 0],
                "add_noise": "enable",
                "noise_seed": ["12", 0],
                "steps": 8,
                "cfg": 1,
                "sampler_name": "euler_ancestral",
                "scheduler": "sgm_uniform",
                "start_at_step": 0,
                "end_at_step": 7,
                "return_with_leftover_noise": "enable",
            },
        },
        "21": {
            "class_type": "XB_ROCmKSamplerAdvanced",
            "inputs": {
                "model": ["99", 0],
                "add_noise": "enable",
                "noise_seed": 123,
                "steps": ["13", 0],
                "cfg": 4.6,
                "sampler_name": "er_sde",
                "scheduler": "sgm_uniform",
                "start_at_step": 0,
                "end_at_step": 12,
                "return_with_leftover_noise": "enable",
            },
        },
        "22": {
            "class_type": "KSamplerAdvanced",
            "inputs": {
                "model": ["99", 0],
                "add_noise": "disable",
                "noise_seed": 456,
                "steps": 16,
                "cfg": 2.5,
                "sampler_name": "er_sde",
                "scheduler": "normal",
                "start_at_step": 2,
                "end_at_step": 15,
                "return_with_leftover_noise": "disable",
            },
        },
    }

    ui = api_to_ui(api, object_info)
    by_id = {node["id"]: node for node in ui["nodes"]}
    assert by_id[4]["widgets_values"] == [None, None, 1]
    assert by_id[20]["widgets_values"] == [
        "enable",
        None,
        "fixed",
        8,
        1,
        "euler_ancestral",
        "sgm_uniform",
        0,
        7,
        "enable",
    ]
    assert by_id[21]["widgets_values"] == [
        "enable",
        123,
        "fixed",
        None,
        4.6,
        "er_sde",
        "sgm_uniform",
        0,
        12,
        "enable",
    ]
    assert by_id[22]["widgets_values"] == [
        "disable",
        456,
        "fixed",
        16,
        2.5,
        "er_sde",
        "normal",
        2,
        15,
        "disable",
    ]
    print("  api to ui linked widget positions OK")


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
    """双采样只写映射节点；显式第二段与共享外联节点按槽位联动。"""
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

    # 只映射第一段时，第二段独立的步数节点保持模板值。
    primary_only = _load_fixture("dual_sampler.json")
    primary_only["13"] = {"class_type": "Int", "inputs": {"value": 67890}}
    primary_only["21"]["inputs"]["noise_seed"] = ["13", 0]
    apply_slots(primary_only, {"sampler": {"node": "20"}}, {"steps": 30, "seed": 99})
    assert primary_only["10"]["inputs"]["value"] == 30
    assert primary_only["11"]["inputs"]["value"] == 8
    assert primary_only["12"]["inputs"]["value"] == 99
    assert primary_only["13"]["inputs"]["value"] == 67890

    # V6 式双采样共享同一个总步数节点时，写第一段即可让两段自然读取新值。
    shared = _load_fixture("dual_sampler.json")
    shared["21"]["inputs"]["steps"] = ["10", 0]
    apply_slots(shared, {"sampler": {"node": "20"}}, {"steps": 30})
    assert shared["10"]["inputs"]["value"] == 30
    assert shared["20"]["inputs"]["steps"] == ["10", 0]
    assert shared["21"]["inputs"]["steps"] == ["10", 0]

    # CFG 等浮点参数沿外联数值节点写入时保留小数并维持连线。
    linked_cfg = _load_fixture("dual_sampler.json")
    linked_cfg["13"] = {"class_type": "Int", "inputs": {"value": 1}}
    linked_cfg["20"]["inputs"]["cfg"] = ["13", 0]
    apply_slots(linked_cfg, {"sampler": {"node": "20"}}, {"cfg": 4.6})
    assert linked_cfg["13"]["inputs"]["value"] == 4.6
    assert linked_cfg["20"]["inputs"]["cfg"] == ["13", 0]

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


def test_execution_status_and_image_media_type() -> None:
    error = execution_error_message(
        {
            "status_str": "error",
            "completed": False,
            "messages": [
                ["execution_start", {"prompt_id": "p1"}],
                [
                    "execution_error",
                    {
                        "node_id": "428",
                        "node_type": "LoraLoaderModelOnly",
                        "exception_type": "OutOfMemoryError",
                        "exception_message": "CUDA out of memory\nwhile allocating tensor",
                    },
                ],
            ],
        }
    )
    assert "428" in error
    assert "LoraLoaderModelOnly" in error
    assert "OutOfMemoryError" in error
    assert "CUDA out of memory while allocating tensor" in error
    interrupted = execution_error_message(
        {
            "status_str": "error",
            "messages": [
                [
                    "execution_interrupted",
                    {"node_id": "510", "node_type": "XB_ROCmKSamplerAdvanced"},
                ]
            ],
        }
    )
    assert interrupted == "执行已中断（节点 510 (XB_ROCmKSamplerAdvanced)）"
    assert execution_error_message({"message": "legacy error"}) == "legacy error"

    assert image_media_type(b"\x89PNG\r\n\x1a\nrest", "wrong.webp") == "image/png"
    assert image_media_type(b"RIFF\x04\x00\x00\x00WEBPrest", "wrong.png") == "image/webp"
    assert image_media_type(b"\xff\xd8\xffrest", "x.bin") == "image/jpeg"
    assert image_media_type(b"GIF89arest", "x.bin") == "image/gif"
    print("  execution status / image media type OK")


def test_webapi_original_output_and_interrupt_guard() -> None:
    import webapi as webapi_module

    png = b"\x89PNG\r\n\x1a\noriginal-png-bytes"

    class FakeClient:
        def __init__(self):
            self.download_calls = []
            self.interrupt_calls = []
            self.entries = {
                "ok-pid": {
                    "status": {"status_str": "success", "completed": True, "messages": []},
                    "outputs": {
                        "9": {
                            "images": [
                                {
                                    "filename": "astrbot_test_00001_.png",
                                    "subfolder": "",
                                    "type": "output",
                                }
                            ]
                        }
                    },
                },
                "error-pid": {
                    "status": {
                        "status_str": "error",
                        "completed": False,
                        "messages": [
                            [
                                "execution_error",
                                {
                                    "node_id": "509",
                                    "node_type": "XB_ROCmKSamplerAdvanced",
                                    "exception_type": "RuntimeError",
                                    "exception_message": "sampler exploded",
                                },
                            ]
                        ],
                    },
                    "outputs": {},
                },
            }

        async def get_history_entry(self, prompt_id):
            return self.entries.get(prompt_id)

        async def download_image(
            self,
            filename,
            subfolder="",
            preview=None,
            image_type="output",
        ):
            self.download_calls.append((filename, subfolder, preview, image_type))
            return png

        async def interrupt(self, prompt_id=None):
            self.interrupt_calls.append(prompt_id)
            return True

    async def run():
        query = {"pid": "ok-pid"}
        body = {}

        def fake_query(key, default=""):
            return query.get(key, default)

        async def fake_body():
            return dict(body)

        def fake_json(data, status=200):
            return {**data, "_status": status}

        old_query, old_body, old_json = (
            webapi_module._query,
            webapi_module._body,
            webapi_module._json,
        )
        webapi_module._query = fake_query
        webapi_module._body = fake_body
        webapi_module._json = fake_json
        try:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                output_dir = root / "output"
                output_dir.mkdir()
                store = RecipeStore(root)
                shared = {
                    "web_pending_runs": {
                        "ok-pid": {"workflow": "mini", "prompt": "cat"},
                        "error-pid": {"workflow": "mini", "prompt": "cat"},
                        "interrupt-pid": {"workflow": "mini", "prompt": "cat"},
                    }
                }
                client = FakeClient()
                api = webapi_module.StudioApi(
                    client,
                    None,
                    store,
                    output_dir,
                    shared,
                )

                success = await api.generate_poll()
                assert success["ok"] is True and success["done"] is True
                assert success["data_url"].startswith("data:image/png;base64,")
                assert client.download_calls == [
                    ("astrbot_test_00001_.png", "", None, "output")
                ]
                history = store.list_history(1)[0]
                saved_path = Path(history["local_path"])
                assert saved_path.suffix == ".png"
                assert saved_path.read_bytes() == png

                query["pid"] = "error-pid"
                failed = await api.generate_poll()
                assert "509" in failed["error"] and "sampler exploded" in failed["error"]
                assert "error-pid" not in shared["web_pending_runs"]

                body.clear()
                missing = await api.interrupt()
                assert missing["ok"] is False and missing["_status"] == 400
                assert client.interrupt_calls == []

                body["prompt_id"] = "unknown-pid"
                unknown = await api.interrupt()
                assert unknown["ok"] is False and unknown["_status"] == 404
                assert client.interrupt_calls == []

                body["prompt_id"] = "interrupt-pid"
                interrupted = await api.interrupt()
                assert interrupted["ok"] is True
                assert client.interrupt_calls == ["interrupt-pid"]
        finally:
            webapi_module._query = old_query
            webapi_module._body = old_body
            webapi_module._json = old_json

    asyncio.run(run())
    print("  webapi original output / interrupt guard OK")


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
    test_power_lora_preserves_independent_accelerator()
    test_slot_mapping_anima_like()
    test_config_dropdown_and_size()
    test_recipe_store_and_draw_schema()
    test_model_family_routing_and_recipe_decoupling()
    test_workflow_profile_store()
    test_llm_entry_schemas()
    test_family_and_recipe_generation_paths()
    test_ui_to_api()
    test_api_to_ui_linked_widget_positions()
    test_workflow_build()
    test_defaults_precedence()
    test_generation_entry_resolution()
    test_recipe_base_slots_binding()
    test_dual_sampler_external_int()
    test_collect_trigger_words()
    test_lora_list_input()
    test_template_management()
    test_submit_error_parse()
    test_execution_status_and_image_media_type()
    test_webapi_original_output_and_interrupt_guard()
    test_cache_atomic()
    print("ALL OK")


if __name__ == "__main__":
    main()
