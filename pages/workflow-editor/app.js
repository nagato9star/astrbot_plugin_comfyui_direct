/* ComfyUI Direct · Workflow Studio —— 节点画布编辑器（零依赖 vanilla JS） */
"use strict";

const NODE_W = 240;
const HEADER_H = 34;
const ROW_H = 28;

/* ---------------- 节点类型定义 ---------------- */
const TYPE_OF = (cls) => {
  if (["UNETLoader", "CLIPLoader", "VAELoader", "LoraLoaderModelOnly", "Power Lora Loader (rgthree)"].includes(cls)) return "loader";
  if (["CR Prompt Text", "CLIPTextEncode", "JoinStringMulti", "DanbooruText"].includes(cls)) return "conditioning";
  if (["KSampler", "ModelSamplingAuraFlow"].includes(cls)) return "sampler";
  if (["EmptyLatentImage"].includes(cls)) return "latent";
  if (["VAEDecode", "SaveImage"].includes(cls)) return "image";
  return "other";
};

/* 新建节点时的默认结构（节点库 + 新建用） */
const NODE_DEFAULTS = {
  "CR Prompt Text": { title: "文本提示词", inputs: { prompt: "" } },
  "DanbooruText": { title: "Danbooru 文本", inputs: { text: "" } },
  "JoinStringMulti": {
    title: "合并字符串",
    inputs: {
      inputcount: 4, delimiter: " ", return_list: false, "Update inputs": null,
      string_1: [], string_2: [], string_3: [], string_4: [],
    },
  },
  "CLIPTextEncode": { title: "CLIP 文本编码", inputs: { text: "", clip: [] } },
  "UNETLoader": { title: "UNet 加载器", inputs: { unet_name: "", weight_dtype: "default" } },
  "CLIPLoader": { title: "CLIP 加载器", inputs: { clip_name: "", type: "stable_diffusion", device: "default" } },
  "VAELoader": { title: "VAE 加载器", inputs: { vae_name: "" } },
  "LoraLoaderModelOnly": { title: "LoRA 加载器", inputs: { model: [], lora_name: "", strength_model: 0.8 } },
  "Power Lora Loader (rgthree)": {
    title: "Power LoRA (rgthree)",
    inputs: { model: [], clip: [], lora_1: { on: true, lora: "", strength: 0.8 }, lora_2: { on: false, lora: "", strength: 0.8 } },
  },
  "EmptyLatentImage": { title: "空潜变量", inputs: { width: 1024, height: 1536, batch_size: 1 } },
  "KSampler": {
    title: "K 采样器",
    inputs: { seed: 0, steps: 20, cfg: 1, sampler_name: "er_sde", scheduler: "normal", denoise: 1, model: [], positive: [], negative: [], latent_image: [] },
  },
  "ModelSamplingAuraFlow": { title: "采样算法 (AuraFlow)", inputs: { shift: 2, model: [] } },
  "VAEDecode": { title: "VAE 解码", inputs: { samples: [], vae: [] } },
  "SaveImage": { title: "保存图像", inputs: { filename_prefix: "ComfyUI", images: [] } },
};

/* 删除连线时的兜底值 */
const LINK_DEFAULTS = {
  prompt: "", text: "", model: "", clip: "", positive: "", negative: "",
  samples: "", vae: "", latent_image: "", unet: "", images: "",
  seed: 0, steps: 20, cfg: 1, denoise: 1, shift: 2, strength_model: 0.8,
  sampler_name: "euler", scheduler: "normal", delimiter: " ", inputcount: 4,
  return_list: false, batch_size: 1,
  lora_name: "None", unet_name: "None", clip_name: "None", vae_name: "None",
};
const FILE_LISTS = { unet_name: "unet_name", lora_name: "lora_name", clip_name: "clip_name", vae_name: "vae_name", lora: "lora_name" };

const SAMPLER_FALLBACK = ["er_sde", "euler", "euler_ancestral", "dpmpp_2m", "dpmpp_2m_sde", "dpmpp_3m_sde", "dpmpp_sde", "ddim", "uni_pc", "lcm"];
const SCHEDULER_FALLBACK = ["normal", "karras", "exponential", "sgm_uniform", "simple", "beta"];

/* ---------------- 全局状态 ---------------- */
const state = {
  wf: {},           // node_id -> {inputs, class_type, _meta}
  current: "",      // 当前模板名
  source: "",       // custom | builtin | skill
  templates: [],
  models: { unet_name: [], lora_name: [], clip_name: [], vae_name: [] },
  systemStats: null,
  samplers: SAMPLER_FALLBACK,
  schedulers: SCHEDULER_FALLBACK,
  connected: false,
  dirty: false,
  running: false,
  pan: { x: 80, y: 60 },
  zoom: 1,
  drag: null,       // 节点拖动 / 画布平移 / 端口连线
};

/* ---------------- 基础工具 ---------------- */
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const world = () => $("#world");
const canvasEl = () => $("#canvas");

function toast(msg, isErr = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = isErr ? "show err" : "show";
  clearTimeout(t._tm);
  t._tm = setTimeout(() => (t.className = ""), 2600);
}

/* ---------------- Bridge 诊断与恢复 ---------------- */
let bridgeLoadErrors = [];
window.addEventListener("error", (e) => {
  const src = e.target && e.target.tagName === "SCRIPT" ? e.target.src : "";
  if (src) bridgeLoadErrors.push(`脚本加载失败: ${src}`);
});

function diagnoseBridge() {
  const lines = [];
  const tags = document.querySelectorAll('script[src*="bridge-sdk"]');
  lines.push(`bridge 脚本标签数: ${tags.length}`);
  if (tags.length) {
    lines.push(`src: ${tags[0].getAttribute("src")}`);
    lines.push(`script 是否已执行: ${window.AstrBotPluginPage ? "是" : "否"}`);
  } else {
    lines.push("未注入 bridge 脚本（AstrBot 版本可能不支持页面 bridge，或页面未走 Dashboard 加载）");
  }
  if (bridgeLoadErrors.length) {
    lines.push("加载错误: " + bridgeLoadErrors.join(" | "));
  }
  lines.push(`iframe 中: ${window.parent !== window ? "是" : "否"}`);
  return lines.join("\n");
}

function setInitState(text, showRetry = false) {
  const overlay = $("#init-overlay");
  if (!overlay) return;
  $("#init-spinner").classList.toggle("hidden", showRetry);
  $("#init-text").textContent = text;
  $("#init-retry").classList.toggle("hidden", !showRetry);
  overlay.classList.remove("hidden");
}
function hideInitState() {
  $("#init-overlay").classList.add("hidden");
}
function showInitError(text) {
  const detail = diagnoseBridge();
  const pre = document.createElement("pre");
  pre.className = "init-detail";
  pre.textContent = detail;
  const box = $("#init-box");
  box.querySelectorAll(".init-detail").forEach((n) => n.remove());
  box.appendChild(pre);
  setInitState(text || "Bridge 初始化失败——请通过 AstrBot Dashboard 打开本页面。", true);
}

async function waitForBridge(timeoutMs = 20000) {
  const start = Date.now();
  while (!window.AstrBotPluginPage) {
    if (Date.now() - start > timeoutMs) return null;
    await new Promise((r) => setTimeout(r, 100));
  }
  return window.AstrBotPluginPage;
}

/* Bridge 不可用时等待重连（如主题切换导致 iframe 重载），期间全屏提示 */
async function ensureBridge(timeoutMs = 15000) {
  if (window.AstrBotPluginPage) return true;
  setInitState("连接已断开，正在重连…");
  const bridge = await waitForBridge(timeoutMs);
  if (bridge) {
    hideInitState();
    return true;
  }
  showInitError("Bridge 连接超时——请点击重试，或通过 AstrBot Dashboard 重新打开本页面。");
  return false;
}

function apiGet(endpoint, params) {
  return ensureBridge().then(() => window.AstrBotPluginPage.apiGet(endpoint, params || {}));
}
function apiPost(endpoint, body) {
  return ensureBridge().then(() => window.AstrBotPluginPage.apiPost(endpoint, body || {}));
}

/* ---------------- 坐标换算 ---------------- */
function toWorld(clientX, clientY) {
  const r = canvasEl().getBoundingClientRect();
  return {
    x: (clientX - r.left - state.pan.x) / state.zoom,
    y: (clientY - r.top - state.pan.y) / state.zoom,
  };
}
function portPos(el) {
  const r = el.getBoundingClientRect();
  const cr = canvasEl().getBoundingClientRect();
  return {
    x: (r.left + r.width / 2 - cr.left - state.pan.x) / state.zoom,
    y: (r.top + r.height / 2 - cr.top - state.pan.y) / state.zoom,
  };
}

/* ---------------- 画布变换 ---------------- */
function applyTransform() {
  world().style.transform = `translate(${state.pan.x}px, ${state.pan.y}px) scale(${state.zoom})`;
}

/* ---------------- 渲染：节点 ---------------- */
function makeWidget(id, key, val) {
  const row = document.createElement("div");
  row.className = "row";
  if (Array.isArray(val)) {
    row.className += " link-row";
    const label = document.createElement("span");
    label.className = "label";
    label.textContent = key;
    const port = document.createElement("span");
    port.className = "port-in";
    port.dataset.in = `${id}:${key}`;
    port.title = "点击删除连线，拖到此处可重新连线";
    row.append(label, port);
    return row;
  }
  const label = document.createElement("span");
  label.className = "label";
  label.textContent = key;
  row.appendChild(label);

  const setDirty = () => (state.dirty = true);
  if (typeof val === "boolean") {
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.className = "chk";
    cb.checked = val;
    cb.addEventListener("change", () => {
      state.wf[id].inputs[key] = cb.checked;
      setDirty();
    });
    row.appendChild(cb);
  } else if (typeof val === "number") {
    const inp = document.createElement("input");
    inp.type = "number";
    inp.step = "any";
    inp.value = String(val);
    inp.addEventListener("change", () => {
      const n = Number(inp.value);
      if (!Number.isNaN(n)) {
        state.wf[id].inputs[key] = n;
        setDirty();
      }
    });
    row.appendChild(inp);
  } else if (typeof val === "string") {
    if (key === "prompt" || key === "text") {
      const ta = document.createElement("textarea");
      ta.value = val;
      ta.addEventListener("change", () => {
        state.wf[id].inputs[key] = ta.value;
        setDirty();
      });
      row.appendChild(ta);
    } else if (FILE_LISTS[key] || key.toLowerCase().includes("lora")) {
      const listKey = FILE_LISTS[key] || "lora_name";
      const sel = document.createElement("select");
      const opts = state.models[listKey] || [];
      if (!opts.includes(val)) opts.unshift(val);
      sel.innerHTML = opts
        .map((o) => `<option value="${escapeHtml(o)}"${o === val ? " selected" : ""}>${escapeHtml(o)}</option>`)
        .join("");
      sel.addEventListener("change", () => {
        state.wf[id].inputs[key] = sel.value;
        setDirty();
      });
      row.appendChild(sel);
    } else if (key === "sampler_name" || key === "scheduler") {
      const list = key === "sampler_name" ? state.samplers : state.schedulers;
      const sel = document.createElement("select");
      if (!list.includes(val)) list.unshift(val);
      sel.innerHTML = list
        .map((o) => `<option value="${escapeHtml(o)}"${o === val ? " selected" : ""}>${escapeHtml(o)}</option>`)
        .join("");
      sel.addEventListener("change", () => {
        state.wf[id].inputs[key] = sel.value;
        setDirty();
      });
      row.appendChild(sel);
    } else {
      const inp = document.createElement("input");
      inp.type = "text";
      inp.value = val;
      inp.addEventListener("change", () => {
        state.wf[id].inputs[key] = inp.value;
        setDirty();
      });
      row.appendChild(inp);
    }
  } else if (val && typeof val === "object") {
    const g = document.createElement("div");
    g.className = "slot-group";
    for (const [sk, sv] of Object.entries(val)) {
      if (sk === "Update inputs") continue;
      if (typeof sv === "boolean") {
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.className = "chk";
        cb.checked = sv;
        cb.title = sk;
        cb.addEventListener("change", () => {
          state.wf[id].inputs[key][sk] = cb.checked;
          setDirty();
        });
        g.appendChild(cb);
      } else if (typeof sv === "number") {
        const inp = document.createElement("input");
        inp.type = "number";
        inp.step = "any";
        inp.value = String(sv);
        inp.title = sk;
        inp.addEventListener("change", () => {
          const n = Number(inp.value);
          if (!Number.isNaN(n)) {
            state.wf[id].inputs[key][sk] = n;
            setDirty();
          }
        });
        g.appendChild(inp);
      } else if (typeof sv === "string") {
        const sel = document.createElement("select");
        const opts = (state.models.lora_name || []).slice();
        if (!opts.includes(sv)) opts.unshift(sv);
        sel.innerHTML = opts
          .map((o) => `<option value="${escapeHtml(o)}"${o === sv ? " selected" : ""}>${escapeHtml(o)}</option>`)
          .join("");
        sel.title = sk;
        sel.addEventListener("change", () => {
          state.wf[id].inputs[key][sk] = sel.value;
          setDirty();
        });
        g.appendChild(sel);
      }
    }
    row.appendChild(g);
  } else {
    return null; // null 值（如 rgthree 的 "Update inputs"）不渲染
  }
  return row;
}

function escapeHtml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function estimateNodeHeight(node) {
  let rows = 0;
  for (const v of Object.values(node.inputs || {})) {
    if (v === null || v === undefined) continue;
    rows += 1;
  }
  return HEADER_H + rows * ROW_H + 14;
}

/* 自动布局：按依赖关系分层（源节点在左，消费者往右） */
function computeLayout(wf) {
  const children = {};
  for (const [id, node] of Object.entries(wf)) {
    for (const v of Object.values(node.inputs || {})) {
      if (Array.isArray(v) && v.length) {
        const src = String(v[0]);
        (children[src] = children[src] || []).push(id);
      }
    }
  }
  const levels = {};
  const queue = [];
  for (const id of Object.keys(wf)) {
    if (!children[id]) {
      levels[id] = 0;
      queue.push(id);
    }
  }
  while (queue.length) {
    const id = queue.shift();
    for (const c of children[id] || []) {
      const nl = levels[id] + 1;
      if (levels[c] === undefined || nl > levels[c]) {
        levels[c] = nl;
        queue.push(c);
      }
    }
  }
  for (const id of Object.keys(wf)) {
    if (levels[id] === undefined) levels[id] = 0;
  }
  const byLevel = {};
  for (const [id, lv] of Object.entries(levels)) (byLevel[lv] = byLevel[lv] || []).push(id);
  const pos = {};
  let maxH = 0;
  for (const [lv, ids] of Object.entries(byLevel)) {
    let y = 40;
    maxH = 0;
    for (const id of ids) {
      pos[id] = { x: 40 + Number(lv) * 280, y };
      const h = estimateNodeHeight(wf[id]);
      maxH = Math.max(maxH, h);
      y += h + 40;
    }
  }
  return pos;
}

function render() {
  $("#world").querySelectorAll(".node").forEach((n) => n.remove());
  const wf = state.wf;
  const layoutPos = computeLayout(wf);
  const frag = document.createDocumentFragment();

  for (const [id, node] of Object.entries(wf)) {
    const meta = node._meta || {};
    const pos = meta.pos && typeof meta.pos.x === "number" ? meta.pos : layoutPos[id];
    const cls = node.class_type || "unknown";
    const div = document.createElement("div");
    div.className = `node type-${TYPE_OF(cls)}`;
    div.dataset.id = id;
    div.style.left = `${pos.x}px`;
    div.style.top = `${pos.y}px`;

    const header = document.createElement("div");
    header.className = "node-header";
    const title = document.createElement("span");
    title.className = "n-title";
    title.textContent = meta.title || cls;
    const outPort = document.createElement("span");
    outPort.className = "port-out";
    outPort.dataset.out = id;
    outPort.title = "拖拽到目标输入端口连线";
    header.append(title, outPort);
    div.appendChild(header);

    const body = document.createElement("div");
    body.className = "node-body";
    for (const [key, val] of Object.entries(node.inputs || {})) {
      const row = makeWidget(id, key, val);
      if (row) body.appendChild(row);
    }
    div.appendChild(body);
    frag.appendChild(div);
  }
  world().appendChild(frag);
  drawWires();
}

/* ---------------- 渲染：连线 ---------------- */
function drawWires() {
  const svg = $("#wires");
  svg.innerHTML = "";
  const wires = [];
  for (const [id, node] of Object.entries(state.wf)) {
    for (const [key, val] of Object.entries(node.inputs || {})) {
      if (Array.isArray(val) && val.length) {
        wires.push({ from: String(val[0]), to: id, key });
      }
    }
  }
  for (const w of wires) {
    const outEl = $(`[data-out="${w.from}"]`);
    const inEl = $(`[data-in="${w.to}:${w.key}"]`);
    if (!outEl || !inEl) continue;
    const p1 = portPos(outEl);
    const p2 = portPos(inEl);
    const dx = Math.max(40, Math.abs(p2.x - p1.x) / 2);
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", `M ${p1.x} ${p1.y} C ${p1.x + dx} ${p1.y}, ${p2.x - dx} ${p2.y}, ${p2.x} ${p2.y}`);
    path.style.pointerEvents = "stroke";
    path.style.cursor = "pointer";
    path.title = `${w.from} → ${w.to}.${w.key}（点击删除连线）`;
    path.addEventListener("click", () => {
      state.wf[w.to].inputs[w.key] = LINK_DEFAULTS[w.key] !== undefined ? LINK_DEFAULTS[w.key] : "";
      state.dirty = true;
      drawWires();
      render();
    });
    path.addEventListener("mouseenter", () => path.classList.add("wire-hover"));
    path.addEventListener("mouseleave", () => path.classList.remove("wire-hover"));
    svg.appendChild(path);
  }
}

/* ---------------- 交互：平移 / 缩放 ---------------- */
function initCanvasEvents() {
  canvasEl().addEventListener("wheel", (e) => {
    e.preventDefault();
    const before = toWorld(e.clientX, e.clientY);
    state.zoom *= Math.exp(-e.deltaY * 0.0012);
    state.zoom = Math.min(1.6, Math.max(0.3, state.zoom));
    const after = toWorld(e.clientX, e.clientY);
    state.pan.x += (after.x - before.x) * state.zoom;
    state.pan.y += (after.y - before.y) * state.zoom;
    applyTransform();
  });

  canvasEl().addEventListener("mousedown", (e) => {
    if (e.button !== 0) return;
    const node = e.target.closest(".node");
    if (node) {
      if (e.target.closest(".node-header") && !e.target.closest("input") && !e.target.classList.contains("port-out")) {
        startDragNode(e, node);
      }
      return;
    }
    if (e.target.closest(".port-out")) return;
    state.drag = { type: "pan", sx: e.clientX, sy: e.clientY, ox: state.pan.x, oy: state.pan.y };
  });

  canvasEl().addEventListener("mousemove", (e) => {
    if (state.drag && state.drag.type === "pan") {
      state.pan.x = state.drag.ox + (e.clientX - state.drag.sx);
      state.pan.y = state.drag.oy + (e.clientY - state.drag.sy);
      applyTransform();
    } else if (state.drag && state.drag.type === "node") {
      const w = toWorld(e.clientX, e.clientY);
      state.drag.node.style.left = `${w.x - state.drag.offX}px`;
      state.drag.node.style.top = `${w.y - state.drag.offY}px`;
      drawWires();
    } else if (state.drag && state.drag.type === "link") {
      const w = toWorld(e.clientX, e.clientY);
      state.drag.temp.setAttribute("d", tempPath(state.drag.fromPos, w));
    }
  });

  window.addEventListener("mouseup", (e) => {
    if (state.drag && state.drag.type === "link") {
      state.drag.temp.remove();
      const target = document.elementFromPoint(e.clientX, e.clientY);
      const port = target && target.closest ? target.closest(".port-in[data-in]") : null;
      if (port) {
        const [toId, key] = port.dataset.in.split(":");
        if (toId !== state.drag.fromId) {
          state.wf[toId].inputs[key] = [state.drag.fromId, 0];
          state.dirty = true;
        }
      }
      drawWires();
      render();
    } else if (state.drag && state.drag.type === "node") {
      state.dirty = true;
    }
    state.drag = null;
  });
}

function startDragNode(e, node) {
  const w = toWorld(e.clientX, e.clientY);
  const left = parseFloat(node.style.left) || 0;
  const top = parseFloat(node.style.top) || 0;
  state.drag = { type: "node", node, offX: w.x - left, offY: w.y - top };
}

function tempPath(p1, p2) {
  const dx = Math.max(40, Math.abs(p2.x - p1.x) / 2);
  return `M ${p1.x} ${p1.y} C ${p1.x + dx} ${p1.y}, ${p2.x - dx} ${p2.y}, ${p2.x} ${p2.y}`;
}

function initPortEvents() {
  document.addEventListener("mousedown", (e) => {
    const out = e.target.closest(".port-out");
    if (!out) return;
    e.preventDefault();
    const fromPos = portPos(out);
    const temp = document.createElementNS("http://www.w3.org/2000/svg", "path");
    temp.setAttribute("d", tempPath(fromPos, fromPos));
    temp.classList.add("wire-drag");
    temp.style.pointerEvents = "none";
    $("#wires").appendChild(temp);
    state.drag = { type: "link", fromId: out.dataset.out, fromPos, temp };
  });

  document.addEventListener("click", (e) => {
    const port = e.target.closest(".port-in");
    if (!port) return;
    const [id, key] = port.dataset.in.split(":");
    if (state.wf[id] && state.wf[id].inputs[key] !== undefined) {
      state.wf[id].inputs[key] = LINK_DEFAULTS[key] !== undefined ? LINK_DEFAULTS[key] : "";
      state.dirty = true;
      drawWires();
      render();
    }
  });

  document.addEventListener("dblclick", (e) => {
    const title = e.target.closest(".n-title");
    if (!title) return;
    const nodeEl = title.closest(".node");
    const id = nodeEl.dataset.id;
    const meta = (state.wf[id]._meta = state.wf[id]._meta || {});
    const inp = document.createElement("input");
    inp.value = title.textContent;
    title.replaceWith(inp);
    inp.focus();
    inp.select();
    const commit = () => {
      const v = inp.value.trim();
      if (v) meta.title = v;
      render();
      state.dirty = true;
    };
    inp.addEventListener("blur", commit);
    inp.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") { commit(); }
      if (ev.key === "Escape") { render(); }
    });
  });
}

/* ---------------- 右键菜单 ---------------- */
function initContextMenu() {
  const menu = $("#ctx-menu");
  document.addEventListener("contextmenu", (e) => {
    const nodeEl = e.target.closest(".node");
    if (!nodeEl) return;
    e.preventDefault();
    const id = nodeEl.dataset.id;
    menu.innerHTML = "";
    const items = [
      { label: "复制节点", fn: () => cloneNode(id) },
      { label: "删除节点", fn: () => removeNode(id), danger: true },
    ];
    for (const it of items) {
      const b = document.createElement("button");
      b.textContent = it.label;
      if (it.danger) b.className = "danger";
      b.addEventListener("click", () => {
        menu.classList.add("hidden");
        it.fn();
      });
      menu.appendChild(b);
    }
    menu.style.left = `${e.clientX}px`;
    menu.style.top = `${e.clientY}px`;
    menu.classList.remove("hidden");
  });
  document.addEventListener("click", () => menu.classList.add("hidden"));
}

function nextNodeId() {
  let max = 0;
  for (const id of Object.keys(state.wf)) {
    if (/^\d+$/.test(id)) max = Math.max(max, Number(id));
  }
  return String(max + 1);
}

function cloneNode(id) {
  const src = state.wf[id];
  const nid = nextNodeId();
  const copy = JSON.parse(JSON.stringify(src));
  copy._meta = Object.assign({}, src._meta || {});
  if (copy._meta.pos) copy._meta.pos = { x: copy._meta.pos.x + 30, y: copy._meta.pos.y + 30 };
  state.wf[nid] = copy;
  state.dirty = true;
  render();
}

function removeNode(id) {
  delete state.wf[id];
  for (const node of Object.values(state.wf)) {
    for (const [key, val] of Object.entries(node.inputs || {})) {
      if (Array.isArray(val) && val.length && String(val[0]) === id) {
        node.inputs[key] = LINK_DEFAULTS[key] !== undefined ? LINK_DEFAULTS[key] : "";
      }
    }
  }
  state.dirty = true;
  render();
}

function addNode(cls) {
  const def = NODE_DEFAULTS[cls];
  if (!def) return;
  const center = toWorld(window.innerWidth / 2, window.innerHeight / 2);
  const id = nextNodeId();
  state.wf[id] = {
    inputs: JSON.parse(JSON.stringify(def.inputs)),
    class_type: cls,
    _meta: { title: def.title, pos: { x: Math.round(center.x - NODE_W / 2), y: Math.round(center.y - 80) } },
  };
  state.dirty = true;
  render();
}

/* ---------------- 工具栏 ---------------- */
async function loadTemplates(keepCurrent) {
  const res = await apiGet("workflows");
  if (!res || !res.ok) {
    toast(res?.error || "获取模板列表失败", true);
    return;
  }
  state.templates = res.templates || [];
  const sel = $("#wf-select");
  sel.innerHTML = "";
  for (const t of state.templates) {
    const o = document.createElement("option");
    o.value = t.name;
    o.textContent = t.title ? `${t.name}（${t.title}）` : t.name;
    sel.appendChild(o);
  }
  if (keepCurrent && state.templates.some((t) => t.name === state.current)) {
    sel.value = state.current;
  } else if (state.templates.length) {
    state.current = sel.value;
  }
  updateSourceBadge();
}

async function loadWorkflow(name) {
  const res = await apiGet("workflow", { name });
  if (!res || !res.ok) {
    toast(res?.error || `加载 ${name} 失败`, true);
    return;
  }
  state.wf = res.workflow;
  state.current = res.name;
  state.source = res.source || "builtin";
  state.dirty = false;
  $("#wf-select").value = name;
  updateSourceBadge();
  render();
}

function wfWithPositions() {
  const copy = JSON.parse(JSON.stringify(state.wf));
  for (const el of $$("#world .node")) {
    const id = el.dataset.id;
    if (copy[id]) {
      copy[id]._meta = copy[id]._meta || {};
      copy[id]._meta.pos = { x: Math.round(parseFloat(el.style.left)), y: Math.round(parseFloat(el.style.top)) };
    }
  }
  return copy;
}

async function saveWorkflow(name) {
  const res = await apiPost("workflow/save", { name, workflow: wfWithPositions() });
  if (!res || !res.ok) {
    toast(res?.error || "保存失败", true);
    return false;
  }
  state.current = name;
  state.dirty = false;
  toast(`已保存 ${name}`);
  await loadTemplates(true);
  updateSourceBadge();
  return true;
}

function updateSourceBadge() {
  const el = $("#wf-source");
  const map = { custom: "自定义", builtin: "内置", skill: "技能" };
  el.textContent = state.current ? `${map[state.source] || state.source} · ${state.current}` : "未命名";
  el.className = "badge" + (state.source === "custom" ? " custom" : "");
}

async function refreshStatus() {
  const dot = $("#conn-dot");
  const txt = $("#conn-text");
  dot.className = "dot wait";
  txt.textContent = "检测中…";
  let res = null;
  try {
    res = await apiGet("status", { refresh: 1 }); // 强制同步模型清单
  } catch (e) { /* bridge 报错 */ }
  if (!res || !res.ok) {
    dot.className = "dot off";
    txt.textContent = "接口异常";
    return;
  }
  state.connected = !!res.connected;
  state.models = res.resources || state.models;
  state.systemStats = res.system_stats || null;
  if (res.sampler_names) state.samplers = res.sampler_names;
  if (res.schedulers) state.schedulers = res.schedulers;
  dot.className = "dot " + (state.connected ? "on" : "off");
  txt.textContent = `${state.connected ? "已连接" : "未连接"} · ${res.base_url}`;
  renderResBox();
}

function renderResBox() {
  const box = $("#res-box");
  const names = { unet_name: "UNET 底模", lora_name: "LoRA", clip_name: "CLIP", vae_name: "VAE", embeddings: "Embedding" };
  box.innerHTML = "";
  for (const [key, title] of Object.entries(names)) {
    const arr = state.models[key] || [];
    const line = document.createElement("div");
    line.className = "res-line";
    line.innerHTML = `<span>${title}</span><span>${arr.length}</span>`;
    box.appendChild(line);
  }
  if (state.systemStats) {
    const s = state.systemStats;
    box.appendChild(Object.assign(document.createElement("hr"), { style: "border-color:#3a3a3a;margin:8px 0" }));
    const ver = s.system && s.system.comfyui_version;
    if (ver) {
      const l1 = document.createElement("div");
      l1.className = "res-line";
      l1.innerHTML = `<span>ComfyUI</span><span>v${escapeHtml(ver)}</span>`;
      box.appendChild(l1);
    }
    for (const d of (s.devices || []).slice(0, 2)) {
      const l2 = document.createElement("div");
      l2.className = "res-line";
      const total = d.vram_total ? (d.vram_total / 1073741824).toFixed(1) + "G" : "?";
      const free = d.vram_free ? (d.vram_free / 1073741824).toFixed(1) + "G" : "?";
      l2.innerHTML = `<span title="${escapeHtml(d.name || "")}">GPU ${d.index ?? 0}</span><span>${free} / ${total}</span>`;
      box.appendChild(l2);
    }
  }
}

/* ---------------- 试跑 ---------------- */
async function runGenerate() {
  if (state.running) return;
  if (!Object.keys(state.wf).length) {
    toast("画布为空，无法试跑", true);
    return;
  }
  if (!state.connected) {
    toast("ComfyUI 未连接", true);
  }
  const btn = $("#btn-run");
  const panel = $("#result-panel");
  const body = $("#result-body");
  panel.classList.remove("hidden");
  body.innerHTML = `<div><span class="spinner"></span>正在提交…</div>`;
  state.running = true;
  btn.disabled = true;
  try {
    const res = await apiPost("generate", { workflow: JSON.parse(JSON.stringify(state.wf)) });
    if (!res || !res.ok) {
      body.innerHTML = `<div class="r-err">提交失败：${escapeHtml(res?.error || "未知错误")}</div>`;
      return;
    }
    const pid = res.prompt_id;
    let stopped = false;
    body.innerHTML = `<div><span class="spinner"></span>已提交（${escapeHtml(pid)}），正在生成… <button id="btn-stop" title="中断该任务">中断</button></div>`;
    $("#btn-stop").addEventListener("click", async () => {
      stopped = true;
      try {
        await apiPost("generate/interrupt", { prompt_id: pid });
        body.innerHTML = `<div>已请求中断，等待确认…</div>`;
      } catch (e) {
        body.innerHTML = `<div class="r-err">中断请求失败：${escapeHtml(String(e))}</div>`;
      }
    });
    const deadline = Date.now() + 300000;
    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 2000));
      let poll;
      try {
        poll = await apiGet("generate", { pid });
      } catch (e) {
        body.innerHTML = `<div class="r-err">轮询接口异常：${escapeHtml(String(e))}</div>`;
        return;
      }
      if (!poll || !poll.ok) {
        body.innerHTML = `<div class="r-err">${escapeHtml(poll?.error || "查询失败")}</div>`;
        return;
      }
      if (poll.done) {
        if (poll.data_url) {
          body.innerHTML = `
            <img src="${poll.data_url}" alt="生成结果" />
            <div class="r-line ${stopped ? "r-err" : "r-ok"}">${stopped ? "已中断（此前完成的图片）" : "✓ 生成完成"}</div>
            <div class="r-line">文件名：${escapeHtml(poll.filename || "")}</div>`;
        } else {
          body.innerHTML = `<div class="r-err">${stopped ? "任务已中断。" : escapeHtml(poll.error || "执行完成但无图片")}</div>`;
        }
        return;
      }
    }
    body.innerHTML = `<div class="r-err">生成超时（300s），请在 ComfyUI 端查看。</div>`;
  } catch (e) {
    body.innerHTML = `<div class="r-err">请求异常：${escapeHtml(String(e))}</div>`;
  } finally {
    state.running = false;
    btn.disabled = false;
  }
}

/* ---------------- JSON 弹窗 ---------------- */
function openJsonModal() {
  $("#modal-json").value = JSON.stringify(wfWithPositions(), null, 2);
  $("#modal-error").textContent = "";
  $("#modal").classList.remove("hidden");
  $("#modal-json").focus();
}
function closeJsonModal() {
  $("#modal").classList.add("hidden");
}

/* ---------------- 初始化 ---------------- */
function buildPalette() {
  const box = $("#palette");
  box.innerHTML = "";
  for (const [cls, def] of Object.entries(NODE_DEFAULTS)) {
    const b = document.createElement("button");
    const color = {
      loader: "#d65745", conditioning: "#3d7ee0", sampler: "#8e5ae0",
      latent: "#3fa0a0", image: "#4e9d5a", other: "#6b6f76",
    }[TYPE_OF(cls)];
    b.innerHTML = `<span class="p-color" style="background:${color}"></span>${escapeHtml(def.title)}`;
    b.title = cls;
    b.addEventListener("click", () => addNode(cls));
    box.appendChild(b);
  }
}

function initToolbar() {
  $("#btn-new").addEventListener("click", () => {
    if (state.dirty && !confirm("当前工作流有未保存修改，确定新建？")) return;
    state.wf = {};
    state.current = "";
    state.source = "";
    state.dirty = false;
    updateSourceBadge();
    render();
  });
  $("#btn-save").addEventListener("click", async () => {
    const name = state.current || prompt("新模板名（仅字母数字_-.）：", "my-workflow");
    if (!name) return;
    const ok = await saveWorkflow(name);
    if (ok) await loadWorkflow(name);
  });
  $("#btn-saveas").addEventListener("click", async () => {
    const name = prompt("另存为模板名（仅字母数字_-.）：", "");
    if (!name) return;
    await saveWorkflow(name);
  });
  $("#btn-delete").addEventListener("click", async () => {
    if (!state.current) return toast("没有可删除的模板", true);
    if (!confirm(`确定删除自定义模板 ${state.current}？（内置/技能模板无法删除）`)) return;
    const res = await apiPost("workflow/delete", { name: state.current });
    if (!res || !res.ok) {
      toast(res?.error || "删除失败", true);
      return;
    }
    toast(`已删除 ${state.current}`);
    await loadTemplates(false);
    if (state.templates.length) await loadWorkflow(state.templates[0].name);
  });
  $("#btn-json").addEventListener("click", openJsonModal);
  $("#modal-cancel").addEventListener("click", closeJsonModal);
  $("#modal-apply").addEventListener("click", () => {
    try {
      const parsed = JSON.parse($("#modal-json").value);
      if (typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("需要节点对象");
      state.wf = parsed;
      state.dirty = true;
      closeJsonModal();
      render();
      toast("已应用 JSON");
    } catch (e) {
      $("#modal-error").textContent = `JSON 解析失败：${e.message}`;
    }
  });
  $("#btn-models").addEventListener("click", async () => {
    await refreshStatus();
    toast(state.connected ? "已同步模型清单（见左侧资源栏）" : "未连接，显示的是缓存清单");
  });
  $("#btn-refresh").addEventListener("click", refreshStatus);
  $("#btn-run").addEventListener("click", runGenerate);
  $("#btn-close-result").addEventListener("click", () => $("#result-panel").classList.add("hidden"));
  $("#wf-select").addEventListener("change", () => loadWorkflow($("#wf-select").value));
  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("#modal").classList.contains("hidden")) closeJsonModal();
  });
}

let _started = false;

async function main() {
  if (_started) return; // 防 iframe 重复挂载导致重复初始化
  _started = true;
  setInitState("正在连接 AstrBot…");
  let bridge = await waitForBridge();
  // 超时后自动整页重载一次（会话级标记，防循环）：可解决过期 asset_token 缓存导致的桥加载失败
  if (!bridge && !sessionStorage.getItem("wf_bridge_reloaded")) {
    sessionStorage.setItem("wf_bridge_reloaded", "1");
    location.reload();
    return;
  }
  if (!bridge) {
    showInitError("Bridge 初始化失败（未找到 AstrBotPluginPage）——请通过 AstrBot Dashboard 打开本页面。");
    return;
  }
  try {
    await bridge.ready();
  } catch (e) {
    showInitError(`Bridge 初始化失败（${escapeHtml(String(e))}）——请通过 AstrBot Dashboard 打开本页面。`);
    return;
  }
  hideInitState();
  buildPalette();
  initToolbar();
  initCanvasEvents();
  initPortEvents();
  initContextMenu();
  applyTransform();
  try {
    await Promise.all([refreshStatus(), loadTemplates(false)]);
  } catch (e) {
    /* Bridge 不可用时已由 ensureBridge 全屏提示 */
  }
  if (state.templates.length) {
    await loadWorkflow(state.templates[0].name);
  } else {
    toast("没有可用模板，请先点击「新建」");
  }
}

$("#init-retry").addEventListener("click", () => location.reload());
main();
