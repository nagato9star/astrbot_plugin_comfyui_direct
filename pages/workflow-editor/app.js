const SLOT_FALLBACK = [
  { id: "prompt", label: "用户要画的内容", help: "必选", basic: true },
  { id: "source_image", label: "编辑来源图片", help: "图片编辑工作流中的 LoadImage 节点", basic: false },
  { id: "resolution", label: "编辑输出分辨率", help: "Qwen Image 2.1 编辑 resolution 输入；0 保留参考图尺寸", basic: false },
  { id: "custom_size", label: "编辑自定义画布", help: "custom_size 开关；启用后使用分辨率选择器画布", basic: false },
  { id: "model", label: "底模", help: "", basic: true },
  { id: "loras", label: "LoRA", help: "", basic: true },
  { id: "size", label: "画面大小", help: "", basic: true },
  { id: "aspect_ratio", label: "分辨率选择器画幅比例", help: "例如 1:1、16:9", basic: false },
  { id: "megapixels", label: "分辨率选择器目标 MP", help: "例如 1.0；Qwen Image 2.1 2K 方图约 4.0", basic: false },
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
  editWorkflows: [],
  recipes: [],
  defaultRecipe: "",
  slotRoles: SLOT_FALLBACK,
  slotOptions: {},
  resources: { unet_name: [], lora_name: [] },
  samplers: ["er_sde", "euler", "dpmpp_2m"],
  schedulers: ["normal", "karras", "simple"],
  recipe: emptyRecipe(),
  activeMode: "workflow",
  activeWorkflow: "",
  workflowSource: "",
  workflowPurpose: "generate",
  workflowFamily: "",
  workflowEditRoute: "",
  workflowEditDescription: "",
  workflowJsonOriginal: "",
  profileSlotsOriginal: "{}",
  profileSlots: {},
  profileSource: "detected",
  profileDropNodes: [],
  history: [],
  runningPid: "",
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

function selectMode(mode) {
  state.activeMode = mode === "recipe" ? "recipe" : "workflow";
  const workflow = state.activeMode === "workflow";
  $("#workflow-pane").hidden = !workflow;
  $("#recipe-pane").hidden = workflow;
  $("#rail-right").hidden = workflow;
  $("#layout").classList.toggle("workflow-mode", workflow);
  $("#tab-workflow").setAttribute("aria-selected", String(workflow));
  $("#tab-recipe").setAttribute("aria-selected", String(!workflow));
}

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

function renderWorkflowRouting() {
  const active = state.activeWorkflow;
  $("#workflow-active-name").textContent = active || "尚未选择";
  $("#workflow-active-source").textContent = active ? (state.workflowSource === "custom" ? "已导入" : state.workflowSource) : "";
  $("#workflow-purpose").value = state.workflowPurpose;
  const isEdit = state.workflowPurpose === "edit";
  const boundFamily = state.families.find((family) => family.workflow === active);
  if (!familyByName(state.workflowFamily) && boundFamily) state.workflowFamily = boundFamily.name;
  fillSelect($("#workflow-family"), state.families.map((family) => family.name), state.workflowFamily, [""]);
  const family = familyByName(state.workflowFamily);
  const route = state.editWorkflows.find((item) => String(item.name || "").toLocaleLowerCase() === state.workflowEditRoute.toLocaleLowerCase());
  $("#workflow-family-wrap").hidden = isEdit;
  $("#workflow-edit-route-wrap").hidden = !isEdit;
  $("#workflow-edit-description-wrap").hidden = !isEdit;
  $("#workflow-edit-route").value = state.workflowEditRoute;
  $("#workflow-edit-description").value = state.workflowEditDescription;
  $("#workflow-edit-route-options").innerHTML = state.editWorkflows
    .filter((item) => item.name)
    .map((item) => `<option value="${escapeAttr(item.name)}"></option>`)
    .join("");
  $("#workflow-bind-status").textContent = isEdit
    ? `编辑路由「${state.workflowEditRoute || "(未命名)"}」：${route?.workflow || "尚未绑定"}`
    : family
      ? `${family.name} 的生图工作流：${family.workflow || "未绑定"}`
      : "选择生图家族后绑定当前工作流；配方数据不会改变。";
  $("#btn-bind-workflow").disabled = !active || (isEdit ? !state.workflowEditRoute.trim() : !family);
  $("#btn-bind-workflow").textContent = isEdit ? "保存编辑路由" : "绑定生图工作流";
  $("#btn-unbind-edit").hidden = !isEdit || !route?.workflow;
  $("#btn-save-workflow-json").disabled = !active;
}

function workflowDraftDirty() {
  return $("#workflow-json").value !== state.workflowJsonOriginal
    || JSON.stringify(state.profileSlots) !== state.profileSlotsOriginal;
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
  const selected = options.find((o) => o === current || o.startsWith(`${current} —`) || o.startsWith(`${current} `)) || current;
  fillSelect(sel, options, selected, [""]);
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
    const drawFamilies = state.families.filter((item) => item.workflow === t.name).map((item) => item.name);
    const editRoutes = state.editWorkflows.filter((item) => item.workflow === t.name).map((item) => item.name);
    const routes = [
      drawFamilies.length ? `生图 ${drawFamilies.join("、")}` : "",
      editRoutes.length ? `编辑 ${editRoutes.join("、")}` : "",
    ].filter(Boolean).join(" · ");
    li.innerHTML = `<strong>${escapeHtml(t.name)}</strong><span class="meta">${escapeHtml(src)} · ${t.node_count || "?"} 个节点${routes ? ` · ${escapeHtml(routes)}` : ""}</span>`;
    li.addEventListener("click", () => bindWorkflow(t.name));
    const del = document.createElement("button");
    del.className = "wf-del";
    del.textContent = "×";
    del.title = "删除这张模板（生图家族、编辑路由或旧配方仍引用时会被拒绝）";
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

function applyRecipeToForm(recipe) {
  state.recipe = {
    ...emptyRecipe(),
    ...recipe,
    defaults: { loras: [], ...(recipe.defaults || {}) },
  };
  $("#recipe-name").value = state.recipe.name || "";
  $("#recipe-desc").value = state.recipe.description || "";
  renderLists();
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
  state.families = res.model_families || state.families;
  state.editWorkflows = res.edit_workflows || state.editWorkflows;
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
  renderWorkflowRouting();
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

async function bindWorkflow(name, force = false) {
  if (!force && state.activeWorkflow && workflowDraftDirty()
      && !confirm("当前工作流 JSON 或节点映射有未保存的修改，确定要放弃吗？")) return false;
  const res = await apiGet("workflow", { name });
  if (!res || !res.ok) {
    toast(res?.error || "加载工作流失败", true);
    return;
  }
  state.slotOptions = res.slot_options || {};
  const switched = state.activeWorkflow !== name;
  state.activeWorkflow = name;
  state.workflowSource = res.source || "";
  state.profileSlots = res.profile_slots || res.detected_slots || {};
  state.profileSource = res.profile_source || "detected";
  state.profileDropNodes = res.drop_nodes || [];
  state.workflowJsonOriginal = JSON.stringify(res.workflow || {}, null, 2);
  state.profileSlotsOriginal = JSON.stringify(state.profileSlots);
  $("#workflow-json").value = state.workflowJsonOriginal;
  const editRoute = state.editWorkflows.find((item) => item.workflow === name && item.workflow);
  const drawFamily = state.families.find((item) => item.workflow === name);
  if (editRoute && !drawFamily) state.workflowPurpose = "edit";
  else if (drawFamily) state.workflowPurpose = "generate";
  if (editRoute && !drawFamily) {
    state.workflowEditRoute = editRoute.name;
    state.workflowEditDescription = editRoute.description || "";
  }
  else if (drawFamily) state.workflowFamily = drawFamily.name;
  else if (state.workflowPurpose === "edit") {
    state.workflowEditRoute = name;
    state.workflowEditDescription = "";
  }
  if (switched) toast(`已打开工作流「${name}」的共享槽位`);
  renderLists();
  renderSlots();
  renderWorkflowRouting();
  selectMode("workflow");
  return true;
}

async function loadRecipe(name) {
  const res = await apiGet("recipe", { name });
  if (!res || !res.ok) {
    toast(res?.error || "读取配方失败", true);
    return;
  }
  applyRecipeToForm(res.recipe);
  selectMode("recipe");
}

async function deleteTemplate(name) {
  if (!confirm(`删除模板「${name}」？生图家族、编辑路由或旧配方仍引用时会被拒绝。`)) return;
  const res = await apiPost("workflow/delete", { name });
  if (!res || !res.ok) {
    toast(res?.error || "删除失败", true);
    return;
  }
  toast(`模板「${name}」已删除`);
  if (state.activeWorkflow === name) {
    state.activeWorkflow = "";
    state.workflowSource = "";
    state.profileSlots = {};
    state.slotOptions = {};
    state.workflowJsonOriginal = "";
    state.profileSlotsOriginal = "{}";
    $("#workflow-json").value = "";
    renderSlots();
    renderWorkflowRouting();
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
  const res = await apiPost("recipe/save", {
    ...state.recipe,
    family: family.name,
  });
  if (!res || !res.ok) {
    toast(res?.error || "保存失败", true);
    return false;
  }
  toast("这套已经记住了");
  await loadLists();
  applyRecipeToForm(res.recipe);
  selectMode("recipe");
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
  const editRoute = state.editWorkflows.find((item) => item.workflow === state.activeWorkflow && item.workflow);
  if ((editRoute || state.workflowPurpose === "edit") && !slotNode("source_image")) {
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
  state.profileSlotsOriginal = JSON.stringify(state.profileSlots);
}

async function importFile(file) {
  const text = await file.text();
  let data;
  try {
    data = JSON.parse(text);
  } catch (e) {
    toast("JSON 格式无效", true);
    return;
  }
  const name = file.name.replace(/\.json$/i, "").replace(/[^A-Za-z0-9_\u4e00-\u9fff-]/g, "_") || "imported";
  const res = await apiPost("workflow/import", { name, workflow: data });
  if (!res || !res.ok) {
    toast(res?.error || "导入失败", true);
    return;
  }
  toast(`已导入 ${name}`);
  await loadLists();
  await bindWorkflow(name);
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
  await bindWorkflow(res.name);
}

async function runGenerate() {
  if (state.runningPid) {
    toast("上一张还在跑，等等或点停止", true);
    return;
  }
  const saved = state.recipes.find((row) => row.id === state.recipe.id || row.name === state.recipe.name);
  if (!saved) return toast("先保存配方，再试画已保存的参数", true);
  const prompt = $("#test-prompt").value.trim();
  if (!prompt) {
    toast("先写一句要画什么", true);
    return;
  }
  $("#preview").textContent = "排队中…";
  const res = await apiPost("generate", {
    prompt,
    recipe: saved.name,
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
      state.runningPid = "";
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
  state.runningPid = "";
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
  await loadRecipe(res.recipe.id || res.recipe.name);
}

async function newRecipe() {
  const recipe = emptyRecipe();
  const family = state.families[0] || null;
  if (family) recipe.family = family.name;
  applyRecipeToForm(recipe);
  selectMode("recipe");
}

async function saveWorkflowBinding(unbind = false) {
  if (!state.activeWorkflow && !unbind) return toast("请先选择工作流", true);
  const mode = $("#workflow-purpose").value;
  if (unbind && mode !== "edit") return;
  let payload;
  if (mode === "edit") {
    const editRoute = $("#workflow-edit-route").value.trim();
    if (!editRoute) return toast("请填写编辑路由名", true);
    payload = {
      edit_route: editRoute,
      description: $("#workflow-edit-description").value.trim(),
      mode,
      workflow: unbind ? "" : state.activeWorkflow,
    };
  } else {
    const family = $("#workflow-family").value;
    if (!family) return toast("请选择生图家族", true);
    payload = { family, mode, workflow: state.activeWorkflow };
  }
  const res = await apiPost("workflow/bind", payload);
  if (!res || !res.ok) return toast(res?.error || "绑定工作流失败", true);
  state.families = res.model_families || state.families;
  state.editWorkflows = res.edit_workflows || state.editWorkflows;
  if (mode === "generate") state.workflowFamily = payload.family;
  else state.workflowEditRoute = payload.edit_route;
  if (mode === "edit") state.workflowEditDescription = payload.description;
  state.workflowPurpose = mode;
  renderLists();
  renderWorkflowRouting();
  const warning = (res.warnings || []).join("；");
  toast(warning ? `已保存；${warning}，请检查节点映射` : (unbind ? `已解除「${payload.edit_route}」的工作流绑定` : mode === "edit" ? `已将「${state.activeWorkflow}」绑定到编辑路由「${payload.edit_route}」` : `已将「${state.activeWorkflow}」绑定到生图家族「${payload.family}」`), !!warning);
}

async function saveWorkflowJson() {
  if (!state.activeWorkflow) return toast("请先选择工作流", true);
  if (JSON.stringify(state.profileSlots) !== state.profileSlotsOriginal) {
    return toast("先保存节点映射，再修改工作流 JSON", true);
  }
  let workflow;
  try {
    workflow = JSON.parse($("#workflow-json").value);
  } catch (e) {
    return toast(`工作流 JSON 格式错误：${e.message}`, true);
  }
  const res = await apiPost("workflow/import", { name: state.activeWorkflow, workflow });
  if (!res || !res.ok) return toast(res?.error || "保存工作流失败", true);
  await loadLists();
  await bindWorkflow(state.activeWorkflow, true);
  toast(`工作流「${state.activeWorkflow}」已保存；请重新核对节点映射`);
}

function bindUi() {
  $("#btn-refresh").addEventListener("click", () => refreshStatus().catch((e) => toast(String(e), true)));
  $("#tab-workflow").addEventListener("click", () => selectMode("workflow"));
  $("#tab-recipe").addEventListener("click", () => selectMode("recipe"));
  $("#btn-bind-workflow").addEventListener("click", () => saveWorkflowBinding().catch((e) => toast(String(e), true)));
  $("#btn-unbind-edit").addEventListener("click", () => saveWorkflowBinding(true).catch((e) => toast(String(e), true)));
  $("#btn-save-workflow-json").addEventListener("click", () => saveWorkflowJson().catch((e) => toast(String(e), true)));
  $("#workflow-purpose").addEventListener("change", () => {
    state.workflowPurpose = $("#workflow-purpose").value;
    if (state.workflowPurpose === "edit") {
      const route = state.editWorkflows.find((item) => item.workflow === state.activeWorkflow && item.workflow);
      state.workflowEditRoute = route?.name || state.activeWorkflow;
      state.workflowEditDescription = route?.description || "";
    } else {
      const family = state.families.find((item) => item.workflow === state.activeWorkflow);
      if (family) state.workflowFamily = family.name;
    }
    renderWorkflowRouting();
  });
  $("#workflow-family").addEventListener("change", () => {
    state.workflowFamily = $("#workflow-family").value;
    renderWorkflowRouting();
  });
  $("#workflow-edit-route").addEventListener("input", () => {
    state.workflowEditRoute = $("#workflow-edit-route").value;
    renderWorkflowRouting();
  });
  $("#workflow-edit-route").addEventListener("change", () => {
    const wanted = $("#workflow-edit-route").value.trim().toLocaleLowerCase();
    const route = state.editWorkflows.find((item) => String(item.name || "").toLocaleLowerCase() === wanted);
    state.workflowEditDescription = route?.description || "";
    renderWorkflowRouting();
  });
  $("#workflow-edit-description").addEventListener("input", () => {
    state.workflowEditDescription = $("#workflow-edit-description").value;
  });
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
    renderLists();
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
  selectMode("workflow");
  try {
    await refreshStatus();
    await loadLists();
    if (state.templates.length) await bindWorkflow(state.templates[0].name);
    else if (state.recipes.length) await loadRecipe(state.recipes[0].id || state.recipes[0].name);
    else await newRecipe();
  } catch (e) {
    toast(String(e), true);
  }
}

main();
