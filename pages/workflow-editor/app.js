const SLOT_FALLBACK = [
  { id: "prompt", label: "用户要画的内容", help: "必选", basic: true },
  { id: "source_image", label: "编辑来源图片", help: "图片编辑工作流中的 LoadImage 节点", basic: false },
  { id: "model", label: "底模", help: "", basic: true },
  { id: "loras", label: "LoRA", help: "", basic: true },
  { id: "size", label: "画面大小", help: "", basic: true },
  { id: "sampler", label: "出图采样", help: "必选", basic: true },
  { id: "negative", label: "不要出现的东西", help: "", basic: false },
  { id: "artist", label: "画师风格", help: "", basic: false },
  { id: "quality", label: "画质词", help: "", basic: false },
  { id: "trigger_words", label: "LoRA 触发词", help: "", basic: false },
  { id: "clip", label: "文本编码器(CLIP)", help: "Flux/Krea/Qwen 等独立 CLIP 的模型才需要", basic: false },
  { id: "vae", label: "VAE", help: "模型用独立 VAE 时才需要", basic: false },
  { id: "guidance", label: "引导强度(Flux)", help: "FluxGuidance 之类的节点", basic: false },
];

const state = {
  connected: false,
  templates: [],
  families: [],
  recipes: [],
  defaultRecipe: "",
  slotRoles: SLOT_FALLBACK,
  slotOptions: {},
  resources: { unet_name: [], lora_name: [] },
  samplers: ["er_sde", "euler", "dpmpp_2m"],
  schedulers: ["normal", "karras", "simple"],
  recipe: emptyRecipe(),
  activeWorkflow: "",
  profileSlots: {},
  profileSource: "detected",
  profileDropNodes: [],
  history: [],
  runningPid: "",
  graphEditor: null,
  nodeDefinitions: {},
};

function emptyRecipe() {
  return {
    id: "",
    name: "",
    description: "",
    family: "",
    defaults: { loras: [] },
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

function familyByName(name) {
  const wanted = String(name || "").toLocaleLowerCase();
  return state.families.find((item) => String(item.name || "").toLocaleLowerCase() === wanted) || null;
}

function slotNode(role) {
  const spec = state.profileSlots?.[role];
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
    state.profileSlots = state.profileSlots || {};
    const v = sel.value;
    if (!v) delete state.profileSlots[role.id];
    else state.profileSlots[role.id] = { node: v };
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
  if (guide) guide.classList.toggle("hidden", !!(state.activeWorkflow || state.templates.length));
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
    li.className = t.name === state.activeWorkflow ? "active" : "";
    const src = t.source === "custom" ? "已导入" : (t.source || "");
    li.innerHTML = `<strong>${escapeHtml(t.name)}</strong><span class="meta">${escapeHtml(src)} · ${t.node_count || "?"} 个格子</span>`;
    li.addEventListener("click", () => bindWorkflow(t.name));
    const del = document.createElement("button");
    del.className = "wf-del";
    del.textContent = "×";
    del.title = "删除这张模板（模型家族或旧配方仍引用时会被拒绝）";
    del.addEventListener("click", (e) => {
      e.stopPropagation();
      deleteTemplate(t.name);
    });
    li.appendChild(del);
    wfBox.appendChild(li);
  }
  const rBox = $("#recipe-list");
  rBox.innerHTML = "";
  for (const r of state.recipes) {
    const li = document.createElement("li");
    const isDefault = !!state.defaultRecipe && r.name === state.defaultRecipe;
    li.className = r.id === state.recipe.id ? "active" : "";
    const size = r.width && r.height ? `${r.width}×${r.height}` : "";
    li.innerHTML = `<strong>${escapeHtml(r.name)}${isDefault ? ' <span class="default-tag">默认</span>' : ""}</strong><span class="meta">家族 ${escapeHtml(r.family || "待迁移")} ${size}</span>`;
    if (!isDefault) {
      const def = document.createElement("button");
      def.className = "wf-del recipe-default";
      def.textContent = "设为默认";
      def.title = "用户只说「画一张」时使用这套配方";
      def.addEventListener("click", (e) => {
        e.stopPropagation();
        setDefaultRecipe(r.name);
      });
      li.appendChild(def);
    }
    li.addEventListener("click", () => loadRecipe(r.id || r.name));
    rBox.appendChild(li);
  }
  const familySel = $("#recipe-family");
  fillSelect(familySel, state.families.map((item) => item.name), state.recipe.family, [""]);
  const family = familyByName(state.recipe.family);
  const hint = $("#family-workflow-hint");
  if (hint) {
    hint.textContent = family
      ? `工作流：${family.workflow} · 提示词：${family.prompt_style || "auto"}`
      : "请先在插件配置中添加模型家族并选择工作流";
  }
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
    li.innerHTML = `<div>${escapeHtml(item.recipe || item.family || "未命名")} · ${vals.width || "?"}×${vals.height || "?"}</div>
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
  state.recipe.family = $("#recipe-family").value;
}

function numOrEmpty(v) {
  if (v === "" || v == null) return undefined;
  const n = Number(v);
  return Number.isFinite(n) ? n : undefined;
}

function applyRecipeToForm(recipe, resolved = {}) {
  state.recipe = {
    ...emptyRecipe(),
    ...recipe,
    defaults: { loras: [], ...(recipe.defaults || {}) },
  };
  if (resolved.workflow !== undefined) state.activeWorkflow = resolved.workflow || "";
  if (resolved.slots !== undefined) state.profileSlots = resolved.slots || {};
  if (resolved.dropNodes !== undefined) state.profileDropNodes = resolved.dropNodes || [];
  $("#recipe-name").value = state.recipe.name || "";
  $("#recipe-desc").value = state.recipe.description || "";
  renderLists();
  renderSlots();
  renderDefaults();
  renderGraphWorkflowChoices();
}

function renderGraphWorkflowChoices() {
  const select = $("#graph-workflow-select");
  if (!select) return;
  fillSelect(select, state.templates.map((item) => item.name), state.activeWorkflow, [""]);
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
  state.families = res.model_families || state.families;
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
  state.defaultRecipe = String((recs && recs.default_recipe) || "").trim();
  state.history = (hist && hist.items) || [];
  renderLists();
  renderHistory();
  renderGraphWorkflowChoices();
}

async function setDefaultRecipe(name) {
  try {
    const res = await apiPost("recipe/default", { name });
    if (!res || !res.ok) {
      toast(res?.error || "设置默认配方失败", true);
      return;
    }
    state.defaultRecipe = String(res.default_recipe || name).trim();
    toast(`已把「${state.defaultRecipe}」设为默认配方`);
    renderLists();
  } catch (e) {
    toast(`设置默认配方失败: ${e}`, true);
  }
}

async function bindWorkflow(name) {
  if (state.graphEditor?.dirty && state.graphEditor.activeName !== name && !confirm("当前画布有未保存的修改，继续切换工作流？")) return;
  const res = await apiGet("workflow", { name });
  if (!res || !res.ok) {
    toast(res?.error || "加载工作流失败", true);
    return;
  }
  state.slotOptions = res.slot_options || {};
  const switched = state.activeWorkflow !== name;
  state.activeWorkflow = name;
  state.profileSlots = res.profile_slots || res.detected_slots || {};
  state.profileSource = res.profile_source || "detected";
  state.profileDropNodes = res.drop_nodes || [];
  const matchedFamily = state.families.find((item) => item.workflow === name);
  if (!state.recipe.family && matchedFamily) state.recipe.family = matchedFamily.name;
  if (switched) toast(`已打开工作流「${name}」的共享槽位`);
  if (switched) {
    const detect = await apiPost("workflow/detect", { name });
    if (detect && detect.ok && detect.values) {
      const keepLoras = state.recipe.defaults.loras || [];
      state.recipe.defaults = { loras: keepLoras, ...detect.values };
    }
  } else if (res.detected_slots) {
    const detect = await apiPost("workflow/detect", { name });
    if (detect && detect.ok && detect.values) {
      state.recipe.defaults = { loras: [], ...detect.values, ...state.recipe.defaults };
    }
  }
  renderLists();
  renderSlots();
  renderDefaults();
  renderGraphWorkflowChoices();
  if (state.graphEditor) loadGraphPayload(res);
}

async function loadRecipe(name) {
  const res = await apiGet("recipe", { name });
  if (!res || !res.ok) {
    toast(res?.error || "读取配方失败", true);
    return;
  }
  if (state.graphEditor?.dirty && state.graphEditor.activeName !== res.resolved_workflow
      && !confirm("当前画布有未保存的修改，继续切换工作流？")) return;
  if (res.template_missing) {
    toast(`模型家族配置的工作流「${res.template_missing}」不存在，请检查配置`, true);
  }
  state.slotOptions = res.slot_options || {};
  applyRecipeToForm(res.recipe, {
    workflow: res.resolved_workflow || "",
    slots: res.profile_slots || {},
    dropNodes: res.drop_nodes || [],
  });
  if (state.graphEditor && state.activeWorkflow) await loadGraphForName(state.activeWorkflow);
}

async function deleteTemplate(name) {
  if (!confirm(`删除模板「${name}」？模型家族或旧配方仍引用时会被拒绝。`)) return;
  const res = await apiPost("workflow/delete", { name });
  if (!res || !res.ok) {
    toast(res?.error || "删除失败", true);
    return;
  }
  toast(`模板「${name}」已删除`);
  if (state.activeWorkflow === name) {
    state.activeWorkflow = "";
    state.profileSlots = {};
    state.slotOptions = {};
    renderSlots();
  }
  await loadLists();
}

async function saveRecipe() {
  readFormIntoRecipe();
  if (!state.recipe.name) {
    toast("先给这套起个名字，比如「立绘」", true);
    return false;
  }
  if (!state.recipe.family) {
    toast("先选择模型家族；家族在插件配置中添加", true);
    return false;
  }
  const family = familyByName(state.recipe.family);
  if (!family) {
    toast("当前家族没有配置，请重载插件配置", true);
    return false;
  }
  if (!state.activeWorkflow || state.activeWorkflow !== family.workflow) {
    toast(`当前家族应使用工作流「${family.workflow}」，请先确认该工作流`, true);
    return false;
  }
  if (!slotNode("prompt") || !slotNode("sampler")) {
    toast("请确认「用户要画的内容」和「出图采样」两个格子", true);
    return false;
  }
  const res = await apiPost("recipe/save", {
    ...state.recipe,
    family: family.name,
    profile_slots: state.profileSlots,
    drop_nodes: state.profileDropNodes,
  });
  if (!res || !res.ok) {
    toast(res?.error || "保存失败", true);
    return false;
  }
  toast("这套已经记住了");
  await loadLists();
  applyRecipeToForm(res.recipe, {
    workflow: state.activeWorkflow,
    slots: state.profileSlots,
    dropNodes: state.profileDropNodes,
  });
  return true;
}

async function redetectSlots() {
  if (!state.activeWorkflow) return toast("请先选择工作流", true);
  const res = await apiPost("workflow/detect", { name: state.activeWorkflow });
  if (!res || !res.ok) return toast(res?.error || "节点识别失败", true);
  state.profileSlots = res.slots || {};
  state.slotOptions = res.slot_options || state.slotOptions;
  renderSlots();
  const count = Object.keys(state.profileSlots).length;
  const editFamily = state.families.find((item) => item.edit_workflow === state.activeWorkflow);
  if (editFamily && !slotNode("source_image")) {
    toast("编辑来源图片无法唯一识别，请手动选择 LoadImage 后保存映射", true);
  } else {
    toast(count ? `已加载 ${count} 个节点建议，核对后点「保存映射」` : "无法唯一识别节点，请手动选择后保存", !count);
  }
}

async function saveWorkflowProfile() {
  if (!state.activeWorkflow) return toast("请先选择工作流", true);
  const res = await apiPost("workflow/profile", {
    workflow: state.activeWorkflow,
    slots: state.profileSlots,
    drop_nodes: state.profileDropNodes,
  });
  if (!res || !res.ok) return toast(res?.error || "保存节点映射失败", true);
  if (state.profileSource === "config") {
    toast("映射已保存；配置页手动映射仍优先生效，请在配置页同步修改", true);
  } else {
    state.profileSource = "profile";
    toast("工作流节点映射已保存");
  }
}

async function importFile(file) {
  if (state.graphEditor?.dirty && !confirm("当前画布有未保存的修改，继续导入工作流？")) return;
  const text = await file.text();
  let data;
  try {
    data = JSON.parse(text);
  } catch (e) {
    toast("JSON 格式无效", true);
    return;
  }
  const name = file.name.replace(/\.json$/i, "")
    .replace(/[^A-Za-z0-9_-]/g, "_").replace(/^_+|_+$/g, "").slice(0, 64)
    || `workflow_${Date.now().toString(36)}`;
  if (state.templates.some((item) => item.name === name) && !confirm(`工作流「${name}」已存在，确认覆盖？`)) return;
  if (!Array.isArray(data.nodes) && (!data || typeof data !== "object" || !Object.keys(data).length)) {
    toast("工作流 JSON 结构无效", true);
    return;
  }
  const editor = await ensureGraphEditor();
  if (!Object.keys(state.nodeDefinitions).length) {
    toast("需要连接远端 ComfyUI 读取节点定义后才能导入画布", true);
    return;
  }
  const uiWorkflow = Array.isArray(data.nodes) ? data : null;
  const apiWorkflow = uiWorkflow ? {} : Object.fromEntries(
    Object.entries(data).filter(([, node]) => node && typeof node === "object" && node.class_type)
  );
  if (!uiWorkflow && !Object.keys(apiWorkflow).length) {
    toast("无法识别工作流 JSON 格式", true);
    return;
  }
  editor.load(name, apiWorkflow, uiWorkflow);
  $("#graph-name").value = name;
  const graph = await editor.exportWorkflow();
  const res = await apiPost("workflow/save", { name, ...graph });
  if (!res || !res.ok) {
    toast(res?.error || "导入失败", true);
    return;
  }
  editor.markSaved();
  toast(`已导入 ${name}`);
  state.slotOptions = res.slot_options || {};
  state.activeWorkflow = name;
  renderGraphWorkflowChoices();
  state.profileSlots = res.profile_slots || res.detected_slots || {};
  state.profileSource = res.profile_source || "detected";
  state.profileDropNodes = res.drop_nodes || [];
  await loadLists();
  const detect = await apiPost("workflow/detect", { name });
  if (detect && detect.ok) {
    state.recipe.defaults = { loras: [], ...(detect.values || {}) };
  }
  if (!state.recipe.name) state.recipe.name = name;
  const matchedFamily = state.families.find((item) => item.workflow === name);
  if (matchedFamily) state.recipe.family = matchedFamily.name;
  applyRecipeToForm(state.recipe, {
    workflow: name,
    slots: state.profileSlots,
    dropNodes: state.profileDropNodes,
  });
  if (!matchedFamily) {
    toast(`工作流已导入；请到插件配置添加模型家族并选择「${name}」，随后重载插件`, true);
  }
  loadGraphPayload(res);
  await setGraphMode(true);
}

async function importFromComfy() {
  if (state.graphEditor?.dirty && !confirm("当前画布有未保存的修改，继续导入工作流？")) return;
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
  state.activeWorkflow = res.name;
  state.profileSlots = res.profile_slots || res.detected_slots || {};
  state.profileSource = res.profile_source || "detected";
  state.profileDropNodes = res.drop_nodes || [];
  await bindWorkflow(res.name);
}

async function runGenerate() {
  if (state.runningPid) {
    toast("上一张还在跑，等等或点停止", true);
    return;
  }
  readFormIntoRecipe();
  if (!state.recipe.name) {
    toast("先保存这套，再试画", true);
    return;
  }
  const saved = await saveRecipe();
  if (!saved) return;
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
  pollResult(res.prompt_id, "#preview", res.wait_timeout).catch((error) => toast(String(error), true));
}

async function pollResult(pid, previewSelector = "#preview", waitTimeout = 300) {
  const preview = $(previewSelector);
  const resume = $(previewSelector === "#graph-preview" ? "#graph-resume" : "#btn-resume");
  resume.hidden = true;
  const deadline = performance.now() + Math.max(1, Number(waitTimeout) || 300) * 1000;
  while (performance.now() < deadline) {
    let poll;
    try { poll = await apiGet("generate", { pid }); }
    catch (_) {
      preview.textContent = "连接暂时中断，正在重新查询任务…";
      await new Promise((resolve) => setTimeout(resolve, 500));
      continue;
    }
    if (poll && poll.done) {
      state.runningPid = "";
      if (poll.error) {
        preview.textContent = poll.error;
        toast(poll.error, true);
        return;
      }
      if (poll.data_url) {
        const img = document.createElement("img");
        img.alt = "生成结果";
        img.src = poll.data_url;
        preview.replaceChildren(img);
      } else preview.textContent = poll.message || "执行完成";
      await loadLists();
      return;
    }
    const progress = poll?.progress;
    if (progress?.event === "progress") preview.textContent = `正在生成 · ${progress.value}/${progress.max}`;
    await new Promise((r) => setTimeout(r, 200));
  }
  state.runningPid = "";
  preview.textContent = `等待超时，任务仍可能在排队或执行。任务 ID：${pid}`;
  resume.dataset.promptId = pid;
  resume.hidden = false;
}

function updateGraphStatus() {
  const editor = state.graphEditor;
  if (!editor) return;
  const count = editor.graph._nodes.length;
  const name = editor.activeName || "未命名工作流";
  $("#graph-status").textContent = `${name} · ${count} 个节点${editor.dirty ? " · 未保存" : ""}`;
  $("#graph-undo").disabled = !editor.canUndo;
  $("#graph-redo").disabled = !editor.canRedo;
}

function setGraphPanel(panel, hidden) {
  document.body.classList.toggle(`graph-${panel}-hidden`, hidden);
  $(`#graph-toggle-${panel}`).setAttribute("aria-expanded", String(!hidden));
}

function loadGraphPayload(payload) {
  if (!state.graphEditor || !payload?.workflow) return;
  state.graphEditor.load(payload.name, payload.workflow, payload.ui_workflow);
  $("#graph-name").value = payload.name || "";
  updateGraphStatus();
}

async function loadGraphForName(name) {
  if (!name) return;
  const res = await apiGet("workflow", { name });
  if (!res?.ok) throw new Error(res?.error || "无法加载工作流画布");
  loadGraphPayload(res);
}

function renderGraphPalette() {
  const holder = $("#graph-palette-list");
  const query = $("#graph-search").value.trim().toLocaleLowerCase();
  holder.replaceChildren();
  const entries = Object.entries(state.nodeDefinitions)
    .filter(([type, def]) => {
      const hay = `${type} ${def.display_name || ""} ${def.category || ""}`.toLocaleLowerCase();
      return !query || hay.includes(query);
    })
    .sort((a, b) => String(a[1].category || "").localeCompare(String(b[1].category || "")) || a[0].localeCompare(b[0]))
    .slice(0, query ? 250 : 90);
  let category = "";
  for (const [type, def] of entries) {
    const next = def.category || "其他";
    if (category !== next) {
      category = next;
      const header = document.createElement("div");
      header.className = "graph-category";
      header.textContent = category;
      holder.append(header);
    }
    const button = document.createElement("button");
    button.type = "button";
    button.className = "graph-palette-node";
    button.draggable = true;
    const title = document.createElement("strong");
    title.textContent = def.display_name || type;
    const technical = document.createElement("small");
    technical.textContent = type;
    button.append(title, technical);
    button.addEventListener("click", () => {
      try { state.graphEditor.addNode(type); }
      catch (error) { toast(String(error), true); }
    });
    button.addEventListener("dragstart", (event) => event.dataTransfer.setData("text/x-comfy-node", type));
    holder.append(button);
  }
  if (!entries.length) {
    const empty = document.createElement("p");
    empty.className = "graph-empty";
    empty.textContent = query ? "没有匹配的节点" : "连接 ComfyUI 后加载节点库";
    holder.append(empty);
  }
}

function renderGraphInspector(node) {
  const holder = $("#graph-inspector-content");
  holder.replaceChildren();
  if (!node) {
    holder.textContent = "点击画布中的节点查看参数。";
    return;
  }
  const title = document.createElement("h3");
  title.textContent = node.title || node.type;
  const id = document.createElement("span");
  id.className = "graph-node-id";
  id.textContent = `#${node.id} · ${node.type}`;
  holder.append(title, id);
  const definitions = state.nodeDefinitions[node.type]?.input || {};
  const fields = { ...(definitions.required || {}), ...(definitions.optional || {}) };
  const names = new Set([...Object.keys(fields), ...Object.keys(node._apiInputs || {}),
    ...(node.widgets || []).filter((widget) => widget._seedWidget).map((widget) => widget.name)]);
  for (const name of names) {
    const widget = node.widgets?.find((item) => item.name === name);
    const spec = fields[name] || (widget?._seedWidget ? [widget.options.values, {}] : []);
    const link = node.inputs?.find((input) => input.name === name && input.link != null);
    const label = document.createElement("label");
    label.className = "field";
    const caption = document.createElement("span");
    caption.textContent = name;
    label.append(caption);
    if (link) {
      const source = state.graphEditor.graph.links.get(link.link);
      const linked = document.createElement("div");
      linked.className = "graph-linked-field";
      linked.textContent = source ? `连接自 #${source.origin_id} · 输出 ${source.origin_slot}` : "连线已失效";
      label.append(linked);
      holder.append(label);
      continue;
    }
    const value = widget?.value ?? node._apiInputs?.[name] ?? spec?.[1]?.default ?? "";
    const choices = Array.isArray(spec[0]) ? spec[0]
      : spec[0] === "COMBO" ? (spec[1]?.options || spec[1]?.values || []) : null;
    let control;
    if (Array.isArray(choices) && choices.length) {
      control = document.createElement("select");
      const options = [...choices];
      if (value && !options.includes(value)) options.unshift(value);
      for (const choice of options) control.add(new Option(String(choice), String(choice)));
      control.value = String(value);
    } else if (spec[0] === "BOOLEAN") {
      control = document.createElement("input");
      control.type = "checkbox";
      control.checked = Boolean(value);
    } else if (spec[0] === "INT" || spec[0] === "FLOAT") {
      control = document.createElement("input");
      control.type = "number";
      control.step = spec[0] === "INT" ? "1" : String(spec[1]?.step || "any");
      if (spec[1]?.min !== undefined) control.min = spec[1].min;
      if (spec[1]?.max !== undefined) control.max = spec[1].max;
      control.value = String(value);
    } else {
      control = document.createElement("textarea");
      control.rows = spec[1]?.multiline ? 5 : 2;
      control.value = typeof value === "string" ? value : JSON.stringify(value, null, 2);
    }
    control.addEventListener("change", () => {
      let next;
      if (control.type === "checkbox") next = control.checked;
      else if (control.type === "number") {
        next = Number(control.value);
        if (!Number.isFinite(next) || (spec[0] === "INT" && !Number.isInteger(next))) {
          toast(`${name} 的数字无效`, true);
          return;
        }
      } else if (!Object.hasOwn(fields, name) && typeof value === "object") {
        try { next = JSON.parse(control.value); }
        catch (_) { toast(`${name} 的 JSON 无效`, true); return; }
      } else next = control.value;
      node._apiInputs = node._apiInputs || {};
      if (widget?.options?.serialize !== false) node._apiInputs[name] = next;
      if (widget) widget.value = next;
      state.graphEditor.changed();
    });
    label.append(control);
    if (!Object.hasOwn(fields, name) && !widget) {
      const remove = document.createElement("button");
      remove.type = "button";
      remove.textContent = "移除此输入";
      remove.addEventListener("click", () => {
        delete node._apiInputs[name];
        state.graphEditor.changed();
        renderGraphInspector(node);
      });
      label.append(remove);
    }
    holder.append(label);
  }
  if (node.type.includes("Power Lora Loader")) {
    const addLora = document.createElement("button");
    addLora.type = "button";
    addLora.textContent = "添加 LoRA 槽位";
    addLora.addEventListener("click", () => {
      node._apiInputs = node._apiInputs || {};
      let index = 1;
      while (Object.hasOwn(node._apiInputs, `lora_${index}`)) index++;
      node._apiInputs[`lora_${index}`] = { on: false, lora: "", strength: 0.8 };
      state.graphEditor.changed();
      renderGraphInspector(node);
    });
    holder.append(addLora);
  }
  if (!names.size) {
    const empty = document.createElement("p");
    empty.textContent = "此节点没有可编辑的输入。";
    holder.append(empty);
  }
}

async function ensureGraphEditor() {
  if (state.graphEditor) return state.graphEditor;
  const { WorkflowGraphEditor } = await import("./graph-editor.bundle.js?v=canvas-3");
  const editor = new WorkflowGraphEditor($("#workflow-canvas"), {
    onChange: updateGraphStatus,
    onSelect: renderGraphInspector,
  });
  state.graphEditor = editor;
  const result = await apiGet("workflow/nodes");
  if (result?.ok) {
    state.nodeDefinitions = result.definitions || {};
    editor.setDefinitions(state.nodeDefinitions);
  } else toast(result?.error || "远端节点库不可用；已有画布快照仍可打开", true);
  renderGraphPalette();
  return editor;
}

async function setGraphMode(enabled) {
  document.body.classList.toggle("graph-mode", enabled);
  $("#btn-mode-graph").classList.toggle("active", enabled);
  $("#btn-mode-recipe").classList.toggle("active", !enabled);
  if (!enabled) return;
  const editor = await ensureGraphEditor();
  editor.resize();
  if (state.activeWorkflow && editor.activeName !== state.activeWorkflow) await loadGraphForName(state.activeWorkflow);
}

async function saveGraph() {
  const editor = await ensureGraphEditor();
  const name = $("#graph-name").value.trim();
  if (!/^[A-Za-z0-9_-]{1,64}$/.test(name)) return toast("工作流名称仅允许字母、数字、下划线和连字符", true);
  if (name !== editor.activeName && state.templates.some((item) => item.name === name)
      && !confirm(`工作流「${name}」已存在，确认覆盖？`)) return;
  const graph = await editor.exportWorkflow();
  const res = await apiPost("workflow/save", { name, ...graph });
  if (!res?.ok) return toast(res?.error || "保存工作流失败", true);
  editor.activeName = name;
  editor.markSaved();
  state.activeWorkflow = name;
  state.slotOptions = res.slot_options || {};
  state.profileSlots = res.profile_slots || res.detected_slots || {};
  state.profileSource = res.profile_source || "detected";
  state.profileDropNodes = res.drop_nodes || [];
  await loadLists();
  renderSlots();
  const staleRoles = Object.entries(state.profileSlots).filter(([, spec]) => {
    const nodeId = String(spec?.node || "").split(" ")[0];
    return nodeId && !Object.hasOwn(graph.workflow, nodeId);
  }).map(([role]) => role);
  toast(staleRoles.length
    ? `工作流已保存；请重新确认槽位映射：${staleRoles.join("、")}`
    : `工作流「${name}」已保存`, !!staleRoles);
}

async function runGraph() {
  if (state.runningPid) return toast("上一张仍在运行，请先等待或中断", true);
  const editor = await ensureGraphEditor();
  const graph = await editor.exportWorkflow();
  const preview = $("#graph-preview");
  preview.textContent = "正在提交到远端 ComfyUI…";
  const res = await apiPost("workflow/run", { name: $("#graph-name").value.trim(), ...graph });
  if (!res?.ok) {
    preview.textContent = res?.error || "提交失败";
    return toast(res?.error || "提交失败", true);
  }
  state.runningPid = res.prompt_id;
  editor.afterQueued();
  setGraphPanel("inspector", false);
  $("#graph-run-status").textContent = `#${res.prompt_id.slice(0, 8)}`;
  await pollResult(res.prompt_id, "#graph-preview", res.wait_timeout);
  $("#graph-run-status").textContent = "";
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
  await loadRecipe(res.recipe.id || res.recipe.name);
}

async function newRecipe() {
  const recipe = emptyRecipe();
  const family = state.families[0] || null;
  if (family) recipe.family = family.name;
  state.activeWorkflow = "";
  state.profileSlots = {};
  state.profileDropNodes = [];
  applyRecipeToForm(recipe);
  if (family) await bindWorkflow(family.workflow);
}

function bindUi() {
  for (const [button, selector] of [["#btn-resume", "#preview"], ["#graph-resume", "#graph-preview"]]) {
    $(button).addEventListener("click", () => {
      const pid = $(button).dataset.promptId;
      if (!pid || state.runningPid) return;
      state.runningPid = pid;
      pollResult(pid, selector).catch((error) => { state.runningPid = ""; toast(String(error), true); });
    });
  }
  document.addEventListener("keydown", (event) => {
    if (!document.body.classList.contains("graph-mode") || !(event.ctrlKey || event.metaKey)) return;
    const key = event.key.toLowerCase();
    if (key !== "s" && key !== "enter") return;
    event.preventDefault();
    const action = key === "s" ? saveGraph : runGraph;
    action().catch((error) => toast(String(error), true));
  });
  $("#graph-workflow-select").addEventListener("change", async (event) => {
    const name = event.target.value;
    if (!name) return;
    try { await bindWorkflow(name); }
    catch (error) { toast(String(error), true); }
    event.target.value = state.activeWorkflow;
  });
  $("#btn-mode-graph").addEventListener("click", () => setGraphMode(true).catch((e) => toast(String(e), true)));
  $("#btn-mode-recipe").addEventListener("click", () => setGraphMode(false));
  $("#graph-search").addEventListener("input", renderGraphPalette);
  $("#graph-save").addEventListener("click", () => saveGraph().catch((e) => toast(String(e), true)));
  $("#graph-run").addEventListener("click", () => runGraph().catch((e) => toast(String(e), true)));
  $("#graph-fit").addEventListener("click", () => state.graphEditor?.fit());
  $("#graph-undo").addEventListener("click", () => state.graphEditor?.undo());
  $("#graph-redo").addEventListener("click", () => state.graphEditor?.redo());
  for (const panel of ["palette", "inspector"]) {
    $(`#graph-toggle-${panel}`).addEventListener("click", () => {
      setGraphPanel(panel, !document.body.classList.contains(`graph-${panel}-hidden`));
    });
  }
  $("#graph-stop").addEventListener("click", async () => {
    if (!state.runningPid) return;
    const res = await apiPost("generate/interrupt", { prompt_id: state.runningPid });
    toast(res?.ok ? "已请求中断" : (res?.error || "中断失败"), !res?.ok);
  });
  $("#graph-import").addEventListener("change", (event) => {
    const file = event.target.files?.[0];
    if (file) importFile(file).catch((e) => toast(String(e), true));
    event.target.value = "";
  });
  const graphCanvas = $("#workflow-canvas");
  graphCanvas.addEventListener("dragover", (event) => {
    if (event.dataTransfer.types.includes("text/x-comfy-node")) event.preventDefault();
  });
  graphCanvas.addEventListener("drop", (event) => {
    const type = event.dataTransfer.getData("text/x-comfy-node");
    if (!type || !state.graphEditor) return;
    event.preventDefault();
    const pos = state.graphEditor.eventToGraph(event);
    try { state.graphEditor.addNode(type, pos); }
    catch (error) { toast(String(error), true); }
  });
  $("#btn-refresh").addEventListener("click", () => refreshStatus().catch((e) => toast(String(e), true)));
  $("#btn-save").addEventListener("click", () => saveRecipe().catch((e) => toast(String(e), true)));
  $("#btn-detect-slots").addEventListener("click", () => redetectSlots().catch((e) => toast(String(e), true)));
  $("#btn-save-profile").addEventListener("click", () => saveWorkflowProfile().catch((e) => toast(String(e), true)));
  $("#btn-new-recipe").addEventListener("click", () => newRecipe().catch((e) => toast(String(e), true)));
  $("#btn-delete-recipe").addEventListener("click", async () => {
    if (!state.recipe.name) return;
    if (!confirm(`删掉「${state.recipe.name}」这套？`)) return;
    const res = await apiPost("recipe/delete", { id: state.recipe.id || "", name: state.recipe.name });
    if (!res || !res.ok) return toast(res?.error || "删除失败", true);
    await newRecipe();
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
    const res = await apiPost("generate/interrupt", { prompt_id: state.runningPid });
    if (!res || !res.ok) {
      toast(res?.error || "中断失败", true);
      return;
    }
    toast("已请求中断");
  });
  $("#recipe-family").addEventListener("change", () => {
    state.recipe.family = $("#recipe-family").value;
    const family = familyByName(state.recipe.family);
    if (family) bindWorkflow(family.workflow).catch((e) => toast(String(e), true));
    else renderLists();
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
    if (state.recipes.length) await loadRecipe(state.recipes[0].id || state.recipes[0].name);
    else if (state.families.length) await newRecipe();
    else if (state.templates.length) await bindWorkflow(state.templates[0].name);
    await setGraphMode(true);
  } catch (e) {
    toast(String(e), true);
  }
}

main();
