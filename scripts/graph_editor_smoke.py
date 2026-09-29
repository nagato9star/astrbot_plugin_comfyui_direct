"""Browser smoke test for the Workflow Studio graph editor.

Requires Playwright and a local Chrome installation. No AstrBot or ComfyUI server is used.
"""

from __future__ import annotations

import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "pages" / "workflow-editor"
FIXTURE = json.loads((ROOT / "scripts" / "fixtures" / "mini_workflow.json").read_text(encoding="utf-8"))


def definition(inputs: dict, outputs: list[str], category: str = "test") -> dict:
    return {
        "input": {"required": inputs},
        "output": outputs,
        "output_name": outputs,
        "display_name": category,
        "category": category,
    }


DEFINITIONS = {
    "UNETLoader": definition({"unet_name": [["base.safetensors"]], "weight_dtype": [["default"]]}, ["MODEL"]),
    "CLIPTextEncode": definition({"text": ["STRING", {"default": ""}], "clip": ["CLIP"]}, ["CONDITIONING"]),
    "EmptyLatentImage": definition({"width": ["INT", {"default": 832}], "height": ["INT", {"default": 1216}], "batch_size": ["INT", {"default": 1}]}, ["LATENT"]),
    "KSampler": definition({"seed": ["INT", {"default": 1}], "steps": ["INT", {"default": 20}], "cfg": ["FLOAT", {"default": 7}], "sampler_name": [["euler"]], "scheduler": [["normal"]], "denoise": ["FLOAT", {"default": 1}], "model": ["MODEL"], "positive": ["CONDITIONING"], "negative": ["CONDITIONING"], "latent_image": ["LATENT"]}, ["LATENT"]),
    "SaveImage": definition({"filename_prefix": ["STRING", {"default": "ComfyUI"}], "images": ["IMAGE"]}, []),
    "Power Lora Loader (rgthree)": definition({"model": ["MODEL"]}, ["MODEL"]),
    "CLIPLoader": definition({"clip_name": [["clip.safetensors"]], "type": [["stable_diffusion"]]}, ["CLIP"]),
    "VAEDecode": definition({"samples": ["LATENT"], "vae": ["VAE"]}, ["IMAGE"]),
    "VAELoader": definition({"vae_name": [["vae.safetensors"]]}, ["VAE"]),
}


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


def main() -> None:
    import os

    original = Path.cwd()
    os.chdir(PAGE)
    server = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                executable_path=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            )
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.add_init_script(
                """
                window.__calls = [];
                window.__fixture = JSON.parse(%s);
                window.__definitions = JSON.parse(%s);
                window.AstrBotPluginPage = {
                  ready: async () => {},
                  apiGet: async (endpoint) => {
                    if (endpoint === 'status') return {ok:true, connected:true, base_url:'test', resources:{}, model_families:[]};
                    if (endpoint === 'workflows') return {ok:true, templates:[{name:'mini', node_count:10}, ...(window.__saved?.name && window.__saved.name !== 'mini' ? [{name:window.__saved.name, node_count:10}] : [])]};
                    if (endpoint === 'recipes') return {ok:true, recipes:[]};
                    if (endpoint === 'history') return {ok:true, items:[]};
                    if (endpoint === 'workflow/nodes') return {ok:true, definitions:window.__definitions};
                    if (endpoint === 'workflow') return {ok:true, name:window.__saved?.name || 'mini', workflow:window.__saved?.workflow || window.__fixture, ui_workflow:window.__saved?.ui_workflow, profile_slots:{}, detected_slots:{}, slot_options:{}};
                    if (endpoint === 'generate') return {ok:true, done:true, message:'执行完成，无图片输出'};
                    return {ok:true};
                  },
                  apiPost: async (endpoint, body) => {
                    window.__calls.push({endpoint, body});
                    if (endpoint === 'workflow/detect') return {ok:true, values:{}, slots:{}};
                    if (endpoint === 'workflow/save') { window.__saved = body; return {ok:true, name:body.name, workflow:body.workflow, ui_workflow:body.ui_workflow, profile_slots:{}, detected_slots:{}, slot_options:{}}; }
                    if (endpoint === 'workflow/import') { window.__saved = {name:body.name, workflow:window.__saved?.workflow || window.__fixture, ui_workflow:body.workflow}; return {ok:true, name:body.name}; }
                    if (endpoint === 'workflow/run') return {ok:true, prompt_id:'test-prompt-id'};
                    return {ok:true};
                  }
                };
                """ % (json.dumps(json.dumps(FIXTURE)), json.dumps(json.dumps(DEFINITIONS)))
            )
            page.goto(f"http://127.0.0.1:{server.server_port}/index.html")
            page.locator("#graph-status").get_by_text("10 个节点").wait_for(timeout=15000)
            page.wait_for_timeout(400)
            def node_header_screen(node_id: int) -> dict:
                return page.evaluate("""id => {
                  const canvas = document.querySelector('#workflow-canvas');
                  const editor = canvas.workflowGraphEditor;
                  const node = editor.graph.getNodeById(id);
                  const [x, y] = editor.canvas.ds.convertOffsetToCanvas([node.pos[0] + 35, node.pos[1] - 12]);
                  const rect = canvas.getBoundingClientRect();
                  return {x:rect.left + x, y:rect.top + y};
                }""", node_id)
            if os.environ.get("GRAPH_EDITOR_SCREENSHOT"):
                page.locator("#graph-expand").click()
                page.evaluate("""() => {
                  const editor = document.querySelector('#workflow-canvas').workflowGraphEditor;
                  editor.canvas.ds.changeScale(0.65, [editor.element.width / 2, editor.element.height / 2]);
                  editor.canvas.setDirty(true, true);
                }""")
                page.screenshot(path=os.environ["GRAPH_EDITOR_SCREENSHOT"], full_page=True)
                page.locator("#graph-expand").click()
            point = node_header_screen(7)
            page.mouse.click(point["x"], point["y"])
            page.locator("#graph-inspector-content .graph-node-id").filter(has_text="#7").wait_for(timeout=5000)
            page.get_by_role("button", name="添加 LoRA 槽位").click()
            point = node_header_screen(2)
            page.mouse.click(point["x"], point["y"])
            page.locator("#graph-inspector-content .graph-node-id").filter(has_text="#2").wait_for(timeout=5000)
            page.locator("#graph-inspector-content textarea").fill("edited prompt")
            page.locator("#graph-inspector-content textarea").blur()
            page.locator("#graph-save").click()
            page.wait_for_function("window.__calls.some(x => x.endpoint === 'workflow/save')")
            saved = page.evaluate("window.__calls.find(x => x.endpoint === 'workflow/save').body")
            assert len(saved["workflow"]) == 10, saved["workflow"].keys()
            assert saved["workflow"]["5"]["inputs"]["positive"] == ["2", 0]
            assert saved["workflow"]["2"]["inputs"]["text"] == "edited prompt"
            assert saved["workflow"]["7"]["inputs"]["lora_1"]["lora"] == "style.safetensors"
            assert saved["workflow"]["7"]["inputs"]["lora_3"]["on"] is False
            assert len(next(node["widgets_values"] for node in saved["ui_workflow"]["nodes"] if node["id"] == 7)) == 3
            assert len(saved["ui_workflow"]["nodes"]) == 10
            original_pos = next(node["pos"] for node in saved["ui_workflow"]["nodes"] if node["id"] == 2)
            point = node_header_screen(2)
            page.mouse.move(point["x"], point["y"])
            page.mouse.down()
            page.mouse.move(point["x"] + 32, point["y"] + 21, steps=8)
            page.mouse.up()
            page.locator("#graph-save").click()
            page.wait_for_function("window.__calls.filter(x => x.endpoint === 'workflow/save').length === 2")
            moved = page.evaluate("window.__calls.filter(x => x.endpoint === 'workflow/save').at(-1).body")
            moved_pos = next(node["pos"] for node in moved["ui_workflow"]["nodes"] if node["id"] == 2)
            assert moved_pos != original_pos, (original_pos, moved_pos)
            page.locator("#tab-recipe").click()
            page.locator("#wf-list li").first.click()
            page.locator("#graph-save").click()
            page.wait_for_function("window.__calls.filter(x => x.endpoint === 'workflow/save').length === 3")
            reopened = page.evaluate("window.__calls.filter(x => x.endpoint === 'workflow/save').at(-1).body")
            reopened_pos = next(node["pos"] for node in reopened["ui_workflow"]["nodes"] if node["id"] == 2)
            assert reopened_pos == moved_pos, (moved_pos, reopened_pos)
            ports = page.evaluate("""() => {
              const canvas = document.querySelector('#workflow-canvas');
              const editor = canvas.workflowGraphEditor;
              const source = editor.graph.getNodeById(3).getConnectionPos(false, 0);
              const targetNode = editor.graph.getNodeById(5);
              const target = targetNode.getConnectionPos(true, targetNode.findInputSlot('positive'));
              const rect = canvas.getBoundingClientRect();
              const screen = (point) => {
                const [x, y] = editor.canvas.ds.convertOffsetToCanvas(point);
                return [rect.left + x, rect.top + y];
              };
              return { source: screen(source), target: screen(target) };
            }""")
            page.mouse.move(*ports["source"])
            page.mouse.down()
            page.mouse.move(*ports["target"], steps=12)
            page.mouse.up()
            page.locator("#graph-save").click()
            page.wait_for_function("window.__calls.filter(x => x.endpoint === 'workflow/save').length === 4")
            rewired = page.evaluate("window.__calls.filter(x => x.endpoint === 'workflow/save').at(-1).body")
            assert rewired["workflow"]["5"]["inputs"]["positive"] == ["3", 0]
            compile_count = page.evaluate("document.querySelector('#workflow-canvas').workflowGraphEditor.apiCompileCount")
            page.locator("#graph-run").click()
            page.wait_for_function("window.__calls.some(x => x.endpoint === 'workflow/run')")
            run = page.evaluate("window.__calls.find(x => x.endpoint === 'workflow/run').body")
            assert run["workflow"]["5"]["inputs"]["model"] == ["7", 0]
            assert page.evaluate("document.querySelector('#workflow-canvas').workflowGraphEditor.apiCompileCount") == compile_count
            saved_ui = page.evaluate("window.__saved.ui_workflow")
            page.locator("#graph-import").set_input_files({
                "name": "native-ui-copy.json",
                "mimeType": "application/json",
                "buffer": json.dumps(saved_ui).encode("utf-8"),
            })
            page.locator("#graph-save").click()
            page.wait_for_function("window.__calls.filter(x => x.endpoint === 'workflow/save').length === 5")
            imported = page.evaluate("window.__calls.filter(x => x.endpoint === 'workflow/save').at(-1).body")
            assert imported["workflow"]["5"]["inputs"]["positive"] == ["3", 0]
            assert imported["workflow"]["7"]["inputs"]["lora_3"]["on"] is False
            page.evaluate("""() => {
              const editor = document.querySelector('#workflow-canvas').workflowGraphEditor;
              editor.graph.getNodeById(7).mode = 4;
              editor.changed();
            }""")
            page.locator("#graph-save").click()
            page.wait_for_function("window.__calls.filter(x => x.endpoint === 'workflow/save').length === 6")
            bypassed = page.evaluate("window.__calls.filter(x => x.endpoint === 'workflow/save').at(-1).body.workflow")
            assert "7" not in bypassed
            assert bypassed["5"]["inputs"]["model"] == ["1", 0]
            page.locator(".graph-palette-node").filter(has_text="SaveImage").click()
            page.locator("#graph-status").get_by_text("11 个节点").wait_for()
            page.locator(".graph-palette-node").filter(has_text="UNETLoader").drag_to(
                page.locator("#workflow-canvas"), target_position={"x": 200, "y": 150}
            )
            page.locator("#graph-status").get_by_text("12 个节点").wait_for()
            assert not errors, errors
            browser.close()
            print("graph editor browser smoke OK")
    finally:
        server.shutdown()
        server.server_close()
        os.chdir(original)


if __name__ == "__main__":
    main()
