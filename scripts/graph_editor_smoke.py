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
FIXTURE = json.loads(
    (ROOT / "scripts" / "fixtures" / "mini_workflow.json").read_text(encoding="utf-8")
)


def definition(inputs: dict, outputs: list[str], category: str = "test") -> dict:
    return {
        "input": {"required": inputs},
        "output": outputs,
        "output_name": outputs,
        "category": category,
    }


DEFINITIONS = {
    "UNETLoader": definition(
        {"unet_name": [["base.safetensors"]], "weight_dtype": [["default"]]}, ["MODEL"]
    ),
    "CLIPTextEncode": definition(
        {"text": ["STRING", {"default": "", "multiline": True}], "clip": ["CLIP"]},
        ["CONDITIONING"],
    ),
    "EmptyLatentImage": definition(
        {
            "width": ["INT", {"default": 832}],
            "height": ["INT", {"default": 1216}],
            "batch_size": ["INT", {"default": 1}],
        },
        ["LATENT"],
    ),
    "KSampler": definition(
        {
            "seed": ["INT", {"default": 1, "control_after_generate": True}],
            "steps": ["INT", {"default": 20}],
            "cfg": ["FLOAT", {"default": 7}],
            "sampler_name": [["euler"]],
            "scheduler": [["normal"]],
            "denoise": ["FLOAT", {"default": 1}],
            "model": ["MODEL"],
            "positive": ["CONDITIONING"],
            "negative": ["CONDITIONING"],
            "latent_image": ["LATENT"],
        },
        ["LATENT"],
    ),
    "SaveImage": definition(
        {"filename_prefix": ["STRING", {"default": "ComfyUI"}], "images": ["IMAGE"]}, []
    ),
    "Power Lora Loader (rgthree)": definition({"model": ["MODEL"]}, ["MODEL"]),
    "CLIPLoader": definition(
        {"clip_name": [["clip.safetensors"]], "type": [["stable_diffusion"]]}, ["CLIP"]
    ),
    "VAEDecode": definition({"samples": ["LATENT"], "vae": ["VAE"]}, ["IMAGE"]),
    "VAELoader": definition({"vae_name": [["vae.safetensors"]]}, ["VAE"]),
    "NumberSource": definition(
        {"value": ["INT", {"default": 42}]}, ["INT"], "utilities"
    ),
    "ForcedInput": definition(
        {"value": ["INT", {"forceInput": True}]}, ["INT"], "utilities"
    ),
}


EDITOR = "document.querySelector('#workflow-canvas').workflowGraphEditor"


def screen_point(page, node_id, *, field=None, output=None):
    return page.evaluate(
        """({id,field,output}) => {
      const el=document.querySelector('#workflow-canvas'), editor=el.workflowGraphEditor;
      const node=editor.graph.getNodeById(id), rect=el.getBoundingClientRect();
      const point=field !== null ? node.getConnectionPos(true,node.findInputSlot(field))
        : output !== null ? node.getConnectionPos(false,output) : [node.pos[0]+100,node.pos[1]-15];
      const [x,y]=editor.canvas.ds.convertOffsetToCanvas(point);
      return [rect.left+x,rect.top+y];
    }""",
        {"id": node_id, "field": field, "output": output},
    )


def drag(page, start, end):
    page.mouse.move(*start)
    page.mouse.down()
    page.mouse.move(*end, steps=12)
    page.mouse.up()


def exported(page):
    return page.evaluate(f"{EDITOR}.exportWorkflow()")


def check_editor_interactions(page):
    page.locator("#graph-fit").click()
    # Edit inside the node and restore it through both undo shortcuts.
    prompt = page.locator('.graph-node-textarea[data-node-id="2"]')
    prompt.fill("node prompt\nsecond line")
    prompt.blur()
    actual = exported(page)["workflow"]["2"]["inputs"]["text"]
    assert actual == "node prompt\nsecond line", actual
    page.locator("#workflow-canvas").press("Control+z")
    assert exported(page)["workflow"]["2"]["inputs"]["text"] == "edited prompt"
    page.locator("#workflow-canvas").press("Control+Shift+z")
    assert (
        exported(page)["workflow"]["2"]["inputs"]["text"] == "node prompt\nsecond line"
    )
    assert exported(page)["workflow"]["7"]["inputs"]["lora_3"]["on"] is False

    # Search must include nodes which were never present in the workflow.
    count = page.evaluate(f"{EDITOR}.graph._nodes.length")
    box = page.locator("#workflow-canvas").bounding_box()
    page.mouse.dblclick(
        box["x"] + box["width"] - 80, box["y"] + box["height"] - 80, delay=80
    )
    page.locator(".litesearchbox input").fill("NumberSource")
    page.locator(".lite-search-item").filter(has_text="NumberSource").click()
    page.wait_for_function(f"n => {EDITOR}.graph._nodes.length === n+1", arg=count)
    source_id = page.evaluate(
        f"{EDITOR}.graph._nodes.find(n=>n.type==='NumberSource').id"
    )
    page.locator("#graph-fit").click()
    drag(
        page,
        screen_point(page, source_id, output=0),
        screen_point(page, 5, field="steps"),
    )
    actual = exported(page)["workflow"]["5"]["inputs"]["steps"]
    assert actual == [str(source_id), 0], {
        "actual": actual,
        "inputs": page.evaluate(
            f"{EDITOR}.graph.getNodeById(5).inputs.map(i=>({{name:i.name,link:i.link,pos:i.pos}}))"
        ),
        "source": screen_point(page, source_id, output=0),
        "target": screen_point(page, 5, field="steps"),
    }
    assert exported(page)["workflow"][str(source_id)]["inputs"]["value"] == 42
    page.locator("#workflow-canvas").press("Control+z")
    assert exported(page)["workflow"]["5"]["inputs"]["steps"] == 20
    page.locator("#workflow-canvas").press("Control+y")
    assert exported(page)["workflow"]["5"]["inputs"]["steps"] == [str(source_id), 0]

    # Conversion works through the node menu and survives a native UI import.
    page.mouse.click(*screen_point(page, 5), button="right")
    page.locator(".litemenu-entry").filter(has_text="参数输入").click()
    page.locator(".litemenu-entry").filter(has_text="转为输入 · cfg").click()
    assert page.evaluate(
        f"{EDITOR}.graph.getNodeById(5).widgets.find(w=>w.name==='cfg').hidden"
    )
    snapshot = exported(page)
    before = page.evaluate(
        "window.__calls.filter(x=>x.endpoint==='workflow/save').length"
    )
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#graph-import").set_input_files(
        {
            "name": "interaction-copy.json",
            "mimeType": "application/json",
            "buffer": json.dumps(snapshot["ui_workflow"]).encode(),
        }
    )
    page.wait_for_function(
        "n=>window.__calls.filter(x=>x.endpoint==='workflow/save').length>n", arg=before
    )
    assert exported(page)["workflow"]["5"]["inputs"]["steps"] == [str(source_id), 0]
    assert (
        exported(page)["workflow"]["2"]["inputs"]["text"] == "node prompt\nsecond line"
    )
    assert page.evaluate(
        f"{EDITOR}.graph.getNodeById(5).widgets.find(w=>w.name==='cfg').hidden"
    )

    # Clipboard must retain dynamic LoRA objects as well as ordinary widgets.
    page.mouse.click(*screen_point(page, 7))
    page.locator("#graph-inspector-content .graph-node-id").filter(
        has_text="#7 ·"
    ).wait_for(timeout=5000)
    page.locator("#workflow-canvas").press("Control+c")
    page.locator("#workflow-canvas").press("Control+v")
    page.wait_for_function(
        f"n=>{EDITOR}.graph._nodes.length>n", arg=count + 1, timeout=5000
    )
    copied = [
        node
        for nid, node in exported(page)["workflow"].items()
        if nid != "7" and node["class_type"] == "Power Lora Loader (rgthree)"
    ]
    assert copied[0]["inputs"]["lora_1"]["lora"] == "style.safetensors"
    assert copied[0]["inputs"]["lora_3"]["on"] is False

    page.mouse.click(*screen_point(page, 5))
    page.locator("#graph-inspector-content .field").filter(
        has_text="control_after_generate"
    ).locator("select").select_option("increment")
    before = exported(page)["workflow"]["5"]["inputs"]["seed"]
    calls = page.evaluate(
        "window.__calls.filter(x=>x.endpoint==='workflow/run').length"
    )
    page.locator("#workflow-canvas").press("Control+Enter")
    page.wait_for_function(
        "n=>window.__calls.filter(x=>x.endpoint==='workflow/run').length>n", arg=calls
    )
    run = page.evaluate(
        "window.__calls.filter(x=>x.endpoint==='workflow/run').at(-1).body.workflow"
    )
    assert run["5"]["inputs"]["seed"] == before
    assert "control_after_generate" not in run["5"]["inputs"]
    assert exported(page)["workflow"]["5"]["inputs"]["seed"] == before + 1
    page.locator(".graph-palette-node").filter(has_text="ForcedInput").click()
    forced_id = page.evaluate(
        f"{EDITOR}.graph._nodes.find(n=>n.type==='ForcedInput').id"
    )
    page.locator("#graph-fit").click()
    drag(
        page,
        screen_point(page, source_id, output=0),
        screen_point(page, forced_id, field="value"),
    )
    assert exported(page)["workflow"][str(forced_id)]["inputs"]["value"] == [
        str(source_id),
        0,
    ]
    assert (
        page.evaluate(f"{EDITOR}.graph.getNodeById({forced_id}).widgets?.length || 0")
        == 0
    )


def check_dpr(page):
    page.wait_for_function(
        f"!{EDITOR}.canvas.dirty_canvas && !{EDITOR}.canvas.dirty_bgcanvas"
    )
    metrics = page.evaluate(f"""() => {{
      const editor={EDITOR}, el=editor.element, rect=el.getBoundingClientRect(), ctx=el.getContext('2d');
      return {{width:el.width,height:el.height,cssWidth:rect.width,cssHeight:rect.height,dpr:devicePixelRatio,
        pixels:[[2,2],[el.width-3,2],[2,el.height-3],[el.width-3,el.height-3]].map(([x,y])=>[...ctx.getImageData(x,y,1,1).data])}};
    }}""")
    assert abs(metrics["width"] - metrics["cssWidth"] * metrics["dpr"]) <= 1, metrics
    assert abs(metrics["height"] - metrics["cssHeight"] * metrics["dpr"]) <= 1, metrics
    assert all(pixel[3] == 255 for pixel in metrics["pixels"]), metrics


def check_coordinates(page):
    page.locator("#graph-fit").click()
    check_dpr(page)
    box = page.locator("#workflow-canvas").bounding_box()
    pivot = [box["width"] * 0.65, box["height"] * 0.7]
    before = page.evaluate(
        f"p=>({{point:{EDITOR}.canvas.ds.convertCanvasToOffset(p),scale:{EDITOR}.canvas.ds.scale}})",
        pivot,
    )
    page.mouse.move(box["x"] + pivot[0], box["y"] + pivot[1])
    page.mouse.wheel(0, -120)
    page.wait_for_function(f"s=>{EDITOR}.canvas.ds.scale!==s", arg=before["scale"])
    after = page.evaluate(f"p=>{EDITOR}.canvas.ds.convertCanvasToOffset(p)", pivot)
    # Browser pointer coordinates round fractional CSS pixels.
    scale = page.evaluate(f"{EDITOR}.canvas.ds.scale")
    assert all(abs(a - b) * scale < 1 for a, b in zip(before["point"], after)), (
        before,
        after,
    )
    offset = page.evaluate(f"[...{EDITOR}.canvas.ds.offset]")
    start = [box["x"] + box["width"] - 110, box["y"] + box["height"] - 100]
    page.locator("#workflow-canvas").focus()
    page.keyboard.down("Space")
    drag(page, start, [start[0] + 50, start[1] + 35])
    page.keyboard.up("Space")
    assert page.evaluate(f"[...{EDITOR}.canvas.ds.offset]") != offset
    target = {"x": box["width"] * 0.55, "y": box["height"] * 0.65}
    page.locator(".graph-palette-node").filter(has_text="NumberSource").drag_to(
        page.locator("#workflow-canvas"), target_position=target
    )
    position = page.evaluate(
        f"{EDITOR}.canvas.ds.convertOffsetToCanvas({EDITOR}.graph._nodes.at(-1).pos)"
    )
    assert abs(position[0] - target["x"]) < 2 and abs(position[1] - target["y"]) < 2, (
        position,
        target,
    )
    check_dpr(page)
    old_width = page.locator("#workflow-canvas").bounding_box()["width"]
    page.locator("#graph-toggle-palette").click()
    page.locator("#graph-toggle-inspector").click()
    page.wait_for_function(
        "document.querySelector('#workflow-canvas').getBoundingClientRect().width>1300"
    )
    assert page.locator("#workflow-canvas").bounding_box()["width"] > old_width
    check_dpr(page)
    page.locator("#graph-toggle-palette").click()
    page.locator("#graph-toggle-inspector").click()
    page.locator("#graph-fit").click()
    start = screen_point(page, 2)
    original = exported(page)["workflow"]["2"]["_meta"]["pos"]
    drag(page, start, [start[0] + 45, start[1] + 30])
    assert exported(page)["workflow"]["2"]["_meta"]["pos"] != original
    drag(page, screen_point(page, 3, output=0), screen_point(page, 5, field="positive"))
    assert exported(page)["workflow"]["5"]["inputs"]["positive"] == ["3", 0]


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
            init_script = """
                window.__calls = [];
                window.__fixture = JSON.parse(%s);
                window.__definitions = JSON.parse(%s);
                window.AstrBotPluginPage = {
                  ready: async () => {},
                  apiGet: async (endpoint) => {
                    if (endpoint === 'status') return {ok:true, connected:true, base_url:'test', resources:{}, model_families:[]};
                    if (endpoint === 'workflows') return {ok:true, templates:[{name:'mini', node_count:10}]};
                    if (endpoint === 'recipes') return {ok:true, recipes:[]};
                    if (endpoint === 'history') return {ok:true, items:[]};
                    if (endpoint === 'workflow/nodes') return {ok:true, definitions:window.__definitions};
                    if (endpoint === 'workflow') return {ok:true, name:'mini', workflow:window.__saved?.workflow || window.__fixture, ui_workflow:window.__saved?.ui_workflow, profile_slots:{}, detected_slots:{}, slot_options:{}};
                    if (endpoint === 'generate') return {ok:true, done:true, message:'执行完成，无图片输出'};
                    return {ok:true};
                  },
                  apiPost: async (endpoint, body) => {
                    window.__calls.push({endpoint, body});
                    if (endpoint === 'workflow/detect') return {ok:true, values:{}, slots:{}};
                    if (endpoint === 'workflow/save') { window.__saved = body; return {ok:true, name:body.name, workflow:body.workflow, ui_workflow:body.ui_workflow, profile_slots:{}, detected_slots:{}, slot_options:{}}; }
                    if (endpoint === 'workflow/run') return {ok:true, prompt_id:'test-prompt-id'};
                    return {ok:true};
                  }
                };
                """ % (
                json.dumps(json.dumps(FIXTURE)),
                json.dumps(json.dumps(DEFINITIONS)),
            )
            page.add_init_script(init_script)
            page.goto(f"http://127.0.0.1:{server.server_port}/index.html")
            page.locator("#graph-status").get_by_text("10 个节点").wait_for(
                timeout=15000
            )
            page.wait_for_timeout(400)
            if os.environ.get("GRAPH_EDITOR_SCREENSHOT"):
                page.evaluate("""() => {
                  const editor = document.querySelector('#workflow-canvas').workflowGraphEditor;
                  const rect = editor.element.getBoundingClientRect();
                  editor.canvas.ds.changeScale(0.68, [rect.left + rect.width / 2, rect.top + rect.height / 2]);
                  editor.canvas.selectNode(editor.graph.getNodeById(2));
                  editor.canvas.setDirty(true, true);
                }""")
                page.wait_for_timeout(100)
                page.screenshot(
                    path=os.environ["GRAPH_EDITOR_SCREENSHOT"], full_page=True
                )
                page.evaluate(
                    "document.querySelector('#workflow-canvas').workflowGraphEditor.fit()"
                )
            page.mouse.click(*screen_point(page, 7))
            page.locator("#graph-inspector-content .graph-node-id").filter(
                has_text="#7"
            ).wait_for(timeout=5000)
            page.get_by_role("button", name="添加 LoRA 槽位").click()
            page.mouse.click(*screen_point(page, 2))
            page.locator("#graph-inspector-content .graph-node-id").filter(
                has_text="#2"
            ).wait_for(timeout=5000)
            page.locator("#graph-inspector-content textarea").fill("edited prompt")
            page.locator("#graph-inspector-content textarea").blur()
            page.locator("#graph-save").click()
            page.wait_for_function(
                "window.__calls.some(x => x.endpoint === 'workflow/save')"
            )
            saved = page.evaluate(
                "window.__calls.find(x => x.endpoint === 'workflow/save').body"
            )
            assert len(saved["workflow"]) == 10, saved["workflow"].keys()
            assert saved["workflow"]["5"]["inputs"]["positive"] == ["2", 0]
            assert saved["workflow"]["2"]["inputs"]["text"] == "edited prompt"
            assert (
                saved["workflow"]["7"]["inputs"]["lora_1"]["lora"]
                == "style.safetensors"
            )
            assert saved["workflow"]["7"]["inputs"]["lora_3"]["on"] is False
            assert (
                len(
                    next(
                        node["widgets_values"]
                        for node in saved["ui_workflow"]["nodes"]
                        if node["id"] == 7
                    )
                )
                == 3
            )
            assert len(saved["ui_workflow"]["nodes"]) == 10
            original_pos = next(
                node["pos"] for node in saved["ui_workflow"]["nodes"] if node["id"] == 2
            )
            start = screen_point(page, 2)
            page.mouse.move(*start)
            page.mouse.down()
            page.mouse.move(start[0] + 32, start[1] + 21, steps=8)
            page.mouse.up()
            page.locator("#graph-save").click()
            page.wait_for_function(
                "window.__calls.filter(x => x.endpoint === 'workflow/save').length === 2"
            )
            moved = page.evaluate(
                "window.__calls.filter(x => x.endpoint === 'workflow/save').at(-1).body"
            )
            moved_pos = next(
                node["pos"] for node in moved["ui_workflow"]["nodes"] if node["id"] == 2
            )
            assert moved_pos != original_pos, (original_pos, moved_pos)
            page.locator("#btn-mode-recipe").click()
            page.locator("#wf-list li").first.click()
            page.locator("#btn-mode-graph").click()
            page.locator("#graph-save").click()
            page.wait_for_function(
                "window.__calls.filter(x => x.endpoint === 'workflow/save').length === 3"
            )
            reopened = page.evaluate(
                "window.__calls.filter(x => x.endpoint === 'workflow/save').at(-1).body"
            )
            reopened_pos = next(
                node["pos"]
                for node in reopened["ui_workflow"]["nodes"]
                if node["id"] == 2
            )
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
            page.wait_for_function(
                "window.__calls.filter(x => x.endpoint === 'workflow/save').length === 4"
            )
            rewired = page.evaluate(
                "window.__calls.filter(x => x.endpoint === 'workflow/save').at(-1).body"
            )
            assert rewired["workflow"]["5"]["inputs"]["positive"] == ["3", 0]
            compile_count = page.evaluate(
                "document.querySelector('#workflow-canvas').workflowGraphEditor.apiCompileCount"
            )
            page.locator("#graph-run").click()
            page.wait_for_function(
                "window.__calls.some(x => x.endpoint === 'workflow/run')"
            )
            run = page.evaluate(
                "window.__calls.find(x => x.endpoint === 'workflow/run').body"
            )
            assert run["workflow"]["5"]["inputs"]["model"] == ["7", 0]
            assert (
                page.evaluate(
                    "document.querySelector('#workflow-canvas').workflowGraphEditor.apiCompileCount"
                )
                == compile_count
            )
            saved_ui = page.evaluate("window.__saved.ui_workflow")
            page.locator("#graph-import").set_input_files(
                {
                    "name": "native-ui-copy.json",
                    "mimeType": "application/json",
                    "buffer": json.dumps(saved_ui).encode("utf-8"),
                }
            )
            page.wait_for_function(
                "window.__calls.filter(x => x.endpoint === 'workflow/save').length === 5"
            )
            imported = page.evaluate(
                "window.__calls.filter(x => x.endpoint === 'workflow/save').at(-1).body"
            )
            assert imported["workflow"]["5"]["inputs"]["positive"] == ["3", 0]
            assert imported["workflow"]["7"]["inputs"]["lora_3"]["on"] is False
            check_editor_interactions(page)
            page.evaluate("""() => {
              const editor = document.querySelector('#workflow-canvas').workflowGraphEditor;
              editor.graph.getNodeById(7).mode = 4;
              editor.changed();
            }""")
            page.locator("#graph-save").click()
            page.wait_for_function(f"!{EDITOR}.dirty")
            bypassed = page.evaluate(
                "window.__calls.filter(x => x.endpoint === 'workflow/save').at(-1).body.workflow"
            )
            assert "7" not in bypassed
            assert bypassed["5"]["inputs"]["model"] == ["1", 0]
            assert not errors, errors
            page.wait_for_function(
                "document.querySelector('#graph-run-status').textContent === ''"
            )
            page.evaluate("""() => {
              const bridge=window.AstrBotPluginPage, get=bridge.apiGet, post=bridge.apiPost;
              let failOnce=true;
              bridge.apiGet=async (endpoint,...args) => {
                if (endpoint==='generate') {
                  if (failOnce) {failOnce=false;throw new Error('temporary connection loss');}
                  return window.__resumeDone ? {ok:true,done:true,message:'resumed without resubmission'} : {ok:true,done:false};
                }
                return get(endpoint,...args);
              };
              bridge.apiPost=async (endpoint,body) => {
                const result=await post(endpoint,body);
                return endpoint==='workflow/run' ? {ok:true,prompt_id:'slow-pid',wait_timeout:1} : result;
              };
            }""")
            page.locator("#graph-run").click()
            page.locator("#graph-resume").wait_for(state="visible")
            assert "slow-pid" in page.locator("#graph-preview").text_content()
            submitted_count = page.evaluate(
                "window.__calls.filter(x=>x.endpoint==='workflow/run').length"
            )
            page.evaluate("window.__resumeDone=true")
            page.locator("#graph-resume").click()
            page.get_by_text("resumed without resubmission", exact=True).wait_for()
            assert (
                page.evaluate(
                    "window.__calls.filter(x=>x.endpoint==='workflow/run').length"
                )
                == submitted_count
            )
            assert not errors, errors
            for ratio in (1, 1.5, 2):
                context = browser.new_context(
                    viewport={"width": 1440, "height": 900}, device_scale_factor=ratio
                )
                dpr_page = context.new_page()
                dpr_page.on("pageerror", lambda error: errors.append(str(error)))
                dpr_page.add_init_script(init_script)
                dpr_page.goto(f"http://127.0.0.1:{server.server_port}/index.html")
                dpr_page.locator("#graph-status").get_by_text("10 个节点").wait_for(
                    timeout=15000
                )
                check_coordinates(dpr_page)
                assert not errors, errors
                context.close()
                print(f"graph editor DPR {ratio} OK", flush=True)
            browser.close()
            print("graph editor browser smoke OK")
    finally:
        server.shutdown()
        server.server_close()
        os.chdir(original)


if __name__ == "__main__":
    main()
