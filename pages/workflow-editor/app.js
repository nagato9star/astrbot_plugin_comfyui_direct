const SLOT_FALLBACK = [
  { id: "prompt", label: "用户要画的内容", help: "必选", basic: true },
  { id: "model", label: "底模", help: "", basic: true },
  { id: "loras", label: "LoRA", help: "", basic: true },
  { id: "size", label: "画面大小", help: "", basic: true },
  { id: "sampler", label: "出图采样", help: "必选", basic: true },
  { id: "negative", label: "不要出现的东西", help: "", basic: false },
  { id: "artist", label: "画师风格", help: "", basic: false },
  { id: "quality", label: "画质词", help: "", basic: false },
  { id: "trigger_words", label: "LoRA 触发词", help: "", basic: false },
];

const state = {
  connected: false,
  templates: [],
  recipes: [],
  slotRoles: SLOT_FALLBACK,
  slotOptions: {},
  resources: { unet_name: [], lora_name: [] },
  samplers: ["er_sde", "euler", "dpmpp_2m"],
  schedulers: ["normal", "karras", "simple"],
  recipe: emptyRecipe(),
  history: [],
  runningPid: "",
};

function emptyRecipe() {
  return {
    id: "",
    name: "",
    description: "",
    workflow: "",
    slots: {},
    defaults: { loras: [] },
    drop_nodes: [],
  };
}

const $ = (sel) => document.querySelector(sel);

function toast(msg, isErr = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = isErr ? "show err" : "show";
  clearTimeout(t._tm);
  t._tm = setTimeout(() => (t.className = ""), 2400);
}

async function waitForBridge(timeoutMs = 20000) {
  const start = Date.now();
  while (!window.AstrBotPluginPage) {
    if (Date.now() - start > timeoutMs) return null;
    await new Promise((r) => setTimeout(r, 80));
  }
  return window.AstrBotPluginPage;
}

async function apiGet(endpoint, params) {
  const bridge = window.AstrBotPluginPage;
  if (!bridge) throw new Error("bridge 未就绪");
  return bridge.apiGet(endpoint, params || {});
}
async function apiPost(endpoint, body) {
  const bridge = window.AstrBotPluginPage;
  if (!bridge) throw new Error("bridge 未就绪");
  return bridge.apiPost(endpoint, body || {});
}

function fillSelect(sel, values, current, extra = [""]) {
  const seen = new Set();
  const opts = [...extra, ...(values || [])].filter((v) => {
    if (seen.has(v)) return false;
    seen.add(v);
    return true;
  });
  if (current && !seen.has(current)) opts.splice(1, 0, current);
  sel.innerHTML = opts
    .map((v) => `<option value="${escapeAttr(v)}"${v === current ? " selected" : ""}>${escapeHtml(prettyNode(v))}</option>`)
    .join("");
}

function prettyNode(v) {
  if (!v) return "先不指定";
  const parts = String(v).split(" — ");
  if (parts.length >= 3) return `${parts[2]}（${parts[1]}）`;
  if (parts.length === 2) return parts[1];
  return v;
}

function escapeHtml(s) {
  return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function escapeAttr(s) {
  return escapeHtml(s).replace(/"/g, "&quot;");
}

function slotNode(role) {
  const spec = state.recipe.slots?.[role];
  if (!spec) return "";
  if (typeof spec === "string") return spec;
  return spec.node || "";
}

function renderSlotSelect(role, parent) {
  const label = document.createElement("label");
  label.className = "field";
  const span = document.createElement("span");
  span.textContent = role.label;
  if (role.help) {
    const help = document.createElement("small");
    help.textContent = role.help;
    span.appendChild(help);
  }
  const sel = document.createElement("select");
  sel.dataset.slot = role.id;
  const current = slotNode(role.id);
  const options = state.slotOptions[role.id] || [""];
  fillSelect(sel, options, current, [""]);
  if (current && ![...sel.options].some((o) => o.value === current || o.value.startsWith(`${current} `) || o.value.startsWith(`${current} —`))) {
    const opt = document.createElement("option");
    opt.value = current;
    opt.textContent = prettyNode(current);
    opt.selected = true;
    sel.insertBefore(opt, sel.firstChild);
  }
  for (const o of sel.options) {
    if (o.value === current || o.value.startsWith(`${current} —`) || o.value.startsWith(`${current} `)) {
      o.selected = true;
      break;
    }
  }
  sel.addEventListener("change", () => {
    state.recipe.slots = state.recipe.slots || {};
    const v = sel.value;
    if (!v) delete state.recipe.slots[role.id];
    else state.recipe.slots[role.id] = { node: v };
  });
  label.append(span, sel);
  parent.appendChild(label);
}

function renderSlots() {
  const basic = $("#slot-grid");
  const extra = $("#slot-grid-extra");
  basic.innerHTML = "";
  if (extra) extra.innerHTML = "";
  for (const role of state.slotRoles) {
    renderSlotSelect(role, role.basic === false ? extra || basic : basic);
  }
  const guide = $("#empty-guide");
  if (guide) guide.classList.toggle("hidden", !!(state.recipe.workflow || state.templates.length));
}

function loraTriggerWords(name) {
  const meta = (state.resources.lora_meta || {})[name] || {};
  return (meta.trigger_words || []).map((t) => String(t).trim()).filter(Boolean);
}

function collectLoraTriggerWords() {
  const seen = new Set();
  const words = [];
  for (const item of state.recipe.defaults.loras || []) {
    for (const t of loraTriggerWords(item.name || "")) {
      if (!seen.has(t)) {
        seen.add(t);
        words.push(t);
      }
    }
  }
  return words.join(", ");
}

function syncTriggerWordsFromLoras() {
  const box = $("#def-trigger-words");
  if (!box) return;
  const joined = collectLoraTriggerWords();
  const cur = box.value.trim();
  const auto = (box.dataset.auto || "").trim();
  if (!cur || cur === auto) {
    box.value = joined;
    box.dataset.auto = joined;
  } else if (joined) {
    const have = new Set(cur.split(",").map((s) => s.trim()).filter(Boolean));
    const extra = joined.split(",").map((s) => s.trim()).filter((s) => s && !have.has(s));
    if (extra.length) box.value = `${cur}, ${extra.join(", ")}`;
  }
  state.recipe.defaults.trigger_words = box.value.trim();
}

function renderLoras() {
  const box = $("#lora-list");
  const loras = state.recipe.defaults.loras || [];
  box.innerHTML = "";
  loras.forEach((item, idx) => {
    const row = document.createElement("div");
    row.className = "lora-row";
    const sel = document.createElement("select");
    fillSelect(sel, state.resources.lora_name || [], item.name || "", [""]);
    sel.addEventListener("change", () => {
      state.recipe.defaults.loras[idx].name = sel.value;
      syncTriggerWordsFromLoras();
    });
    const strength = document.createElement("input");
    strength.type = "number";
    strength.step = "0.05";
    strength.value = item.strength ?? 0.8;
    strength.addEventListener("change", () => {
      state.recipe.defaults.loras[idx].strength = Number(strength.value);
    });
    const del = document.createElement("button");
    del.type = "button";
    del.textContent = "×";
    del.addEventListener("click", () => {
      state.recipe.defaults.loras.splice(idx, 1);
      renderLoras();
      syncTriggerWordsFromLoras();
    });
    row.append(sel, strength, del);
    box.appendChild(row);
  });
}

function renderLists() {
  const wfBox = $("#wf-list");
  wfBox.innerHTML = "";
  for (const t of state.templates) {
    const li = document.createElement("li");
    li.className = t.name === state.recipe.workflow ? "active" : "";
    const src = t.source === "custom" ? "已导入" : (t.source || "");
    li.innerHTML = `<strong>${escapeHtml(t.name)}</strong><span class="meta">${escapeHtml(src)} · ${t.node_count || "?"} 个格子</span>`;
    li.addEventListener("click", () => bindWorkflow(t.name));
    wfBox.appendChild(li);
  }
  const rBox = $("#recipe-list");
  rBox.innerHTML = "";
  for (const r of state.recipes) {
    const li = document.createElement("li");
    li.className = r.name === state.recipe.name ? "active" : "";
    const size = r.width && r.height ? `${r.width}×${r.height}` : "";
    li.innerHTML = `<strong>${escapeHtml(r.name)}</strong><span class="meta">${escapeHtml(r.workflow || "")} ${size}</span>`;
    li.addEventListener("click", () => loadRecipe(r.name));
    rBox.appendChild(li);
  }
  const wfSel = $("#recipe-workflow");
  fillSelect(wfSel, state.templates.map((t) => t.name), state.recipe.workflow, [""]);
}

function renderDefaults() {
  const d = state.recipe.defaults || {};
  fillSelect($("#def-model"), state.resources.unet_name || [], d.model || "", [""]);
  $("#def-width").value = d.width || "";
  $("#def-height").value = d.height || "";
  $("#def-steps").value = d.steps || "";
  $("#def-cfg").value = d.cfg || "";
  fillSelect($("#def-sampler"), state.samplers, d.sampler_name || "", [""]);
  fillSelect($("#def-scheduler"), state.schedulers, d.scheduler || "", [""]);
  $("#def-denoise").value = d.denoise ?? "";
  const tw = $("#def-trigger-words");
  if (tw) {
    tw.value = d.trigger_words || "";
    tw.dataset.auto = d.trigger_words || "";
  }
  renderLoras();
  if (tw && !tw.value.trim()) syncTriggerWordsFromLoras();
}

function renderHistory() {
  const box = $("#history-list");
  box.innerHTML = "";
  for (const item of state.history) {
    const li = document.createElement("li");
    const vals = item.values || {};
    li.innerHTML = `<div>${escapeHtml(item.recipe || "未命名")} · ${vals.width || "?"}×${vals.height || "?"}</div>
      <div class="meta">${escapeHtml((item.prompt || "").slice(0, 80))}</div>`;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = "记住这套";
    btn.addEventListener("click", () => saveHistoryAsRecipe(item));
    li.appendChild(btn);
    box.appendChild(li);
  }
}

function readFormIntoRecipe() {
  const d = state.recipe.defaults || {};
  d.model = $("#def-model").value;
  d.width = numOrEmpty($("#def-width").value);
  d.height = numOrEmpty($("#def-height").value);
  d.steps = numOrEmpty($("#def-steps").value);
  d.cfg = numOrEmpty($("#def-cfg").value);
  d.sampler_name = $("#def-sampler").value;
  d.scheduler = $("#def-scheduler").value;
  d.denoise = numOrEmpty($("#def-denoise").value);
  const tw = $("#def-trigger-words");
  d.trigger_words = tw ? tw.value.trim() : "";
  state.recipe.defaults = d;
  state.recipe.name = $("#recipe-name").value.trim();
  state.recipe.description = $("#recipe-desc").value.trim();
  state.recipe.workflow = $("#recipe-workflow").value;
}

function numOrEmpty(v) {
  if (v === "" || v == null) return undefined;
  const n = Number(v);
  return Number.isFinite(n) ? n : undefined;
}

function applyRecipeToForm(recipe) {
  state.recipe = {
    ...emptyRecipe(),
    ...recipe,
    slots: recipe.slots || {},
    defaults: { loras: [], ...(recipe.defaults || {}) },
  };
  $("#recipe-name").value = state.recipe.name || "";
  $("#recipe-desc").value = state.recipe.description || "";
  renderLists();
  renderSlots();
  renderDefaults();
}

async function refreshStatus() {
  const res = await apiGet("status", { refresh: 1 });
  if (!res || !res.ok) {
    $("#conn-dot").className = "dot off";
    $("#conn-text").textContent = "接口异常";
    return;
  }
  state.connected = !!res.connected;
  state.resources = res.resources || state.resources;
  if (res.sampler_names) state.samplers = res.sampler_names;
  if (res.schedulers) state.schedulers = res.schedulers;
  if (res.slot_roles) state.slotRoles = res.slot_roles;
  $("#conn-dot").className = "dot " + (state.connected ? "on" : "off");
  $("#conn-text").textContent = `${state.connected ? "已连接" : "未连接"} · ${res.base_url}`;
  const dev = (res.system_stats || {}).devices || [];
  if (dev[0] && dev[0].vram_total) {
    const free = (dev[0].vram_free / 1073741824).toFixed(1);
    const total = (dev[0].vram_total / 1073741824).toFixed(1);
    $("#gpu-text").textContent = `显存 ${free}/${total}G`;
  }
  renderDefaults();
}

async function loadLists() {
  const [wfs, recs, hist] = await Promise.all([
    apiGet("workflows"),
    apiGet("recipes"),
    apiGet("history"),
  ]);
  state.templates = (wfs && wfs.templates) || [];
  state.recipes = (recs && recs.recipes) || [];
  state.history = (hist && hist.items) || [];
  renderLists();
  renderHistory();
}

async function bindWorkflow(name) {
  const res = await apiGet("workflow", { name });
  if (!res || !res.ok) {
    toast(res?.error || "加载工作流失败", true);
    return;
  }
  state.slotOptions = res.slot_options || {};
  state.recipe.workflow = name;
  if (res.detected_slots) {
    state.recipe.slots = { ...res.detected_slots, ...state.recipe.slots };
  }
  if (res.detected_slots) {
    const detect = await apiPost("workflow/detect", { name });
    if (detect && detect.ok && detect.values) {
      state.recipe.defaults = { loras: [], ...detect.values, ...state.recipe.defaults };
    }
  }
  $("#recipe-workflow").value = name;
  renderLists();
  renderSlots();
  renderDefaults();
}

async function loadRecipe(name) {
  const res = await apiGet("recipe", { name });
  if (!res || !res.ok) {
    toast(res?.error || "读取配方失败", true);
    return;
  }
  state.slotOptions = res.slot_options || {};
  applyRecipeToForm(res.recipe);
}

async function saveRecipe() {
  readFormIntoRecipe();
  if (!state.recipe.name) {
    toast("先给这套起个名字，比如「立绘」", true);
    return;
  }
  if (!state.recipe.workflow) {
    toast("先在左边导入或点选一张工作流图", true);
    return;
  }
  if (!slotNode("prompt") || !slotNode("sampler")) {
    toast("请确认「用户要画的内容」和「出图采样」两个格子", true);
    return;
  }
  const res = await apiPost("recipe/save", state.recipe);
  if (!res || !res.ok) {
    toast(res?.error || "保存失败", true);
    return;
  }
  toast("这套已经记住了");
  await loadLists();
  applyRecipeToForm(res.recipe);
}

async function importFile(file) {
  const text = await file.text();
  let data;
  try {
    data = JSON.parse(text);
  } catch (e) {
    toast("不是有效 JSON", true);
    return;
  }
  const name = file.name.replace(/\.json$/i, "").replace(/[^A-Za-z0-9_\u4e00-\u9fff-]/g, "_") || "imported";
  const res = await apiPost("workflow/import", { name, workflow: data });
  if (!res || !res.ok) {
    toast(res?.error || "导入失败", true);
    return;
  }
  toast(`已导入 ${name}`);
  state.slotOptions = res.slot_options || {};
  state.recipe.workflow = name;
  state.recipe.slots = res.detected_slots || {};
  await loadLists();
  const detect = await apiPost("workflow/detect", { name });
  if (detect && detect.ok) {
    state.recipe.defaults = { loras: [], ...(detect.values || {}) };
  }
  if (!state.recipe.name) state.recipe.name = name;
  applyRecipeToForm(state.recipe);
}

async function importFromComfy() {
  const hist = await apiGet("comfy-history");
  const item = ((hist && hist.items) || []).find((x) => x.has_workflow);
  if (!item) {
    toast("ComfyUI 历史里没有工作流", true);
    return;
  }
  const name = `history-${String(item.prompt_id).slice(0, 8)}`;
  const res = await apiPost("workflow/import-history", { prompt_id: item.prompt_id, name });
  if (!res || !res.ok) {
    toast(res?.error || "导入失败", true);
    return;
  }
  toast("已从 ComfyUI 历史导入");
  await loadLists();
  state.slotOptions = res.slot_options || {};
  state.recipe.workflow = res.name;
  state.recipe.slots = res.detected_slots || {};
  applyRecipeToForm(state.recipe);
  await bindWorkflow(res.name);
}

async function runGenerate() {
  readFormIntoRecipe();
  if (!state.recipe.name) {
    toast("先保存这套，再试画", true);
    return;
  }
  await saveRecipe();
  const prompt = $("#test-prompt").value.trim();
  if (!prompt) {
    toast("先写一句要画什么", true);
    return;
  }
  $("#preview").textContent = "排队中…";
  const res = await apiPost("generate", {
    prompt,
    recipe: state.recipe.name,
    size: $("#test-size").value,
  });
  if (!res || !res.ok) {
    toast(res?.error || "提交失败", true);
    $("#preview").textContent = res?.error || "失败";
    return;
  }
  state.runningPid = res.prompt_id;
  pollResult(res.prompt_id);
}

async function pollResult(pid) {
  for (let i = 0; i < 150; i++) {
    const poll = await apiGet("generate", { pid });
    if (poll && poll.done) {
      if (poll.error) {
        $("#preview").textContent = poll.error;
        toast(poll.error, true);
        return;
      }
      $("#preview").innerHTML = `<img alt="preview" src="${poll.data_url}" />`;
      await loadLists();
      return;
    }
    await new Promise((r) => setTimeout(r, 2000));
  }
  $("#preview").textContent = "等待超时";
}

async function saveHistoryAsRecipe(item) {
  const name = prompt("给这套起个名字", item.recipe ? `${item.recipe}-2` : "新套装");
  if (!name) return;
  const res = await apiPost("recipe/from-history", { prompt_id: item.prompt_id, name });
  if (!res || !res.ok) {
    toast(res?.error || "保存失败", true);
    return;
  }
  toast("已经存成一套新配方");
  await loadLists();
  applyRecipeToForm(res.recipe);
}

function bindUi() {
  $("#btn-refresh").addEventListener("click", () => refreshStatus().catch((e) => toast(String(e), true)));
  $("#btn-save").addEventListener("click", () => saveRecipe().catch((e) => toast(String(e), true)));
  $("#btn-new-recipe").addEventListener("click", () => applyRecipeToForm(emptyRecipe()));
  $("#btn-delete-recipe").addEventListener("click", async () => {
    if (!state.recipe.name) return;
    if (!confirm(`删掉「${state.recipe.name}」这套？`)) return;
    const res = await apiPost("recipe/delete", { name: state.recipe.name });
    if (!res || !res.ok) return toast(res?.error || "删除失败", true);
    applyRecipeToForm(emptyRecipe());
    await loadLists();
  });
  $("#file-import").addEventListener("change", (e) => {
    const file = e.target.files && e.target.files[0];
    if (file) importFile(file).catch((err) => toast(String(err), true));
    e.target.value = "";
  });
  $("#btn-from-comfy").addEventListener("click", () => importFromComfy().catch((e) => toast(String(e), true)));
  $("#btn-add-lora").addEventListener("click", () => {
    state.recipe.defaults.loras = state.recipe.defaults.loras || [];
    state.recipe.defaults.loras.push({ name: "", strength: 0.8 });
    renderLoras();
  });
  const tw = $("#def-trigger-words");
  if (tw) {
    tw.addEventListener("input", () => {
      state.recipe.defaults.trigger_words = tw.value.trim();
    });
  }
  $("#btn-run").addEventListener("click", () => runGenerate().catch((e) => toast(String(e), true)));
  $("#btn-stop").addEventListener("click", async () => {
    if (!state.runningPid) return;
    await apiPost("generate/interrupt", { prompt_id: state.runningPid });
    toast("已请求中断");
  });
  $("#recipe-workflow").addEventListener("change", () => {
    if ($("#recipe-workflow").value) bindWorkflow($("#recipe-workflow").value);
  });
  document.querySelectorAll(".size-presets button").forEach((btn) => {
    btn.addEventListener("click", () => {
      const [w, h] = String(btn.dataset.size || "").split(",");
      if (w) $("#def-width").value = w;
      if (h) $("#def-height").value = h;
    });
  });
}

async function main() {
  const overlay = $("#init-overlay");
  const bridge = await waitForBridge();
  if (!bridge) {
    $("#init-text").textContent = "请通过 AstrBot Dashboard 打开本页面";
    return;
  }
  try {
    if (bridge.ready) await bridge.ready();
  } catch (_) {
    /* ignore */
  }
  overlay.classList.add("hidden");
  bindUi();
  try {
    await refreshStatus();
    await loadLists();
    if (state.recipes.length) await loadRecipe(state.recipes[0].name);
    else if (state.templates.length) await bindWorkflow(state.templates[0].name);
  } catch (e) {
    toast(String(e), true);
  }
}

main();
