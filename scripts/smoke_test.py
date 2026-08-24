# -*- coding: utf-8 -*-
"""冒烟测试：部署/修改后快速验证插件核心逻辑（无 AstrBot 环境也可跑）。

用法：python scripts/smoke_test.py
覆盖：工作流角色覆盖 / 默认值优先级 / 模板管理 / 提交错误解析 / 缓存原子写。
"""

import asyncio
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import httpx

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PLUGIN_DIR))

from comfy_client import ComfyUIClient  # noqa: E402
from tools import ComfyuiGenerateTool  # noqa: E402
from workflow_builder import WorkflowBuilder  # noqa: E402

PLUGIN = _PLUGIN_DIR
# 模板在插件数据目录（插件包不含模板）
_DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "plugin_data" / "astrbot_plugin_comfyui_direct"
_CUSTOM_DIR = (_DATA_DIR / "workflows") if (_DATA_DIR / "workflows").is_dir() else None


def _builder(default_workflow: str = "anima-v3") -> WorkflowBuilder:
    return WorkflowBuilder(plugin_dir=PLUGIN, default_workflow=default_workflow, custom_dir=_CUSTOM_DIR)


def test_workflow_build() -> None:
    """anima-v3 五段式覆盖 + LoRA 插槽 + KSampler。"""
    b = _builder()
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
    assert "445" not in wf
    assert wf["353"]["inputs"]["prompt"] == "1girl, garden"
    assert wf["368"]["inputs"]["prompt"] == "(@testartist:1.0)"
    assert wf["461"]["inputs"]["prompt"] == "@f1f"
    assert wf["362"]["inputs"]["steps"] == 20
    assert wf["478"]["inputs"]["lora_1"]["on"] is True
    assert wf["458"]["inputs"]["unet_name"] == "m.safetensors"
    print("  workflow build OK")


def test_defaults_precedence() -> None:
    """LLM 传参 > 配置默认 > 模板原值。"""
    tool = ComfyuiGenerateTool(client=None, builder=None, output_dir=None)
    defaults = {"artist": "@a", "steps": 25, "width": 0, "trigger_words": ""}
    assert tool._pick(defaults, "artist", None) == "@a"
    assert tool._pick(defaults, "steps", None) == 25
    assert tool._pick(defaults, "width", None) is None
    assert tool._pick(defaults, "trigger_words", None) is None
    assert tool._pick(defaults, "artist", "@b") == "@b"
    print("  defaults precedence OK")


def test_lora_list_input() -> None:
    """LLM 直接传数组对象（非 JSON 字符串）时，lora 不能丢。"""
    b = _builder()
    wf = b.build(
        prompt="1girl",
        lora=[{"name": "l1.safetensors", "strength": 0.5}],
    )
    assert wf["478"]["inputs"]["lora_1"]["on"] is True
    assert wf["478"]["inputs"]["lora_1"]["lora"] == "l1.safetensors"
    wf2 = b.build(prompt="1girl", lora={"name": "l2.safetensors", "strength": 0.7})
    assert wf2["478"]["inputs"]["lora_1"]["on"] is True
    assert wf2["478"]["inputs"]["lora_1"]["lora"] == "l2.safetensors"
    print("  lora list/dict input OK")


def test_template_management() -> None:
    b = _builder()
    wf = b.load_template("anima-v3")  # 数据目录必须有 anima-v3
    names = [t["name"] for t in b.list_templates()]
    assert "anima-v3" in names
    with tempfile.TemporaryDirectory() as td:
        b2 = WorkflowBuilder(plugin_dir=PLUGIN, custom_dir=Path(td))
        b2.save_template("anima-v3", wf)
        assert "custom" in [t["source"] for t in b2.list_templates() if t["name"] == "anima-v3"]
        b2.delete_template("anima-v3")
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
    test_workflow_build()
    test_defaults_precedence()
    test_lora_list_input()
    test_template_management()
    test_submit_error_parse()
    test_cache_atomic()
    print("ALL OK")


if __name__ == "__main__":
    main()
