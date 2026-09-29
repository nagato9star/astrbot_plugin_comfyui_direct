import {
  ExecutableNodeDTO,
  LGraph,
  LGraphCanvas,
  LGraphEventMode,
  LGraphNode,
  LiteGraph,
} from "@comfyorg/litegraph";
import "@comfyorg/litegraph/style.css";

const clone = (value) => structuredClone(value);
const isLink = (value, nodes) => Array.isArray(value) && value.length === 2
  && /^\d+(?::\d+)*$/.test(String(value[0])) && Number.isInteger(value[1])
  && nodes.has(String(value[0]));

function fieldsFor(definition) {
  const input = definition?.input || {};
  return { ...(input.required || {}), ...(input.optional || {}) };
}

function widgetKind(spec) {
  const type = spec?.[0];
  if (Array.isArray(type) || type === "COMBO") return "combo";
  if (type === "INT" || type === "FLOAT") return "number";
  if (type === "BOOLEAN") return "toggle";
  if (type === "STRING") return "text";
  return null;
}

function widgetDefault(spec) {
  const type = spec?.[0];
  const options = spec?.[1] || {};
  if (options.default !== undefined) return clone(options.default);
  if (Array.isArray(type)) return type[0] ?? "";
  if (type === "COMBO") return (options.options || options.values || [])[0] ?? "";
  if (type === "BOOLEAN") return false;
  if (type === "INT" || type === "FLOAT") return 0;
  return "";
}

function inputType(spec) {
  const type = spec?.[0];
  return typeof type === "string" ? type : "*";
}

export class WorkflowGraphEditor {
  constructor(canvas, { onChange = () => {}, onSelect = () => {} } = {}) {
    this.element = canvas;
    Object.defineProperty(canvas, "workflowGraphEditor", { value: this });
    this.graph = new LGraph();
    this.canvas = new LGraphCanvas(canvas, this.graph, { autoresize: false });
    this.canvas.background_image = "";
    this.canvas.show_info = false;
    this.canvas.default_link_color = "#9baec1";
    this.definitions = {};
    this.registered = new Set();
    this.loading = false;
    this.dirty = false;
    this.activeName = "";
    this.originalUi = null;
    this._compiledApi = null;
    this._compiledApiPromise = null;
    this._serializedUi = null;
    this.apiCompileCount = 0;
    this.revision = 0;
    this.onChange = onChange;
    this.onSelect = onSelect;
    this.graph.onAfterChange = () => this.changed();
    this.graph.onConnectionChange = () => this.changed();
    this.graph.onNodeAdded = () => this.changed();
    this.graph.onNodeRemoved = () => this.changed();
    this.canvas.onNodeSelected = (node) => this.onSelect(node);
    this.canvas.onNodeDeselected = () => this.onSelect(null);
    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(canvas.parentElement);
    this.resize();
  }

  resize() {
    const bounds = this.element.parentElement.getBoundingClientRect();
    if (bounds.width && bounds.height) this.canvas.resize(Math.floor(bounds.width), Math.floor(bounds.height));
  }

  changed() {
    if (this.loading) return;
    this.revision++;
    this._compiledApi = null;
    this._serializedUi = null;
    this.dirty = true;
    this.onChange();
    this.canvas.setDirty(true, true);
  }

  setDefinitions(definitions) {
    this.definitions = definitions || {};
  }

  register(type, definition = {}, rawNode = null) {
    if (this.registered.has(type)) return;
    const owner = this;
    class ComfyNode extends LGraphNode {
      constructor() {
        super(definition.display_name || type);
        this.comfyClass = type;
        const fields = fieldsFor(definition);
        for (const [name, spec] of Object.entries(fields)) {
          const kind = widgetKind(spec);
          if (kind) {
            const options = { ...(spec?.[1] || {}) };
            if (Array.isArray(spec?.[0])) options.values = spec[0];
            if (kind === "combo" && !Array.isArray(options.values)) options.values = options.options || [];
            this.addWidget(kind, name, widgetDefault(spec), () => owner.changed(), options);
          } else {
            this.addInput(name, inputType(spec));
          }
        }
        if (!Object.keys(fields).length && rawNode) {
          for (const input of rawNode.inputs || []) this.addInput(input.name, input.type || "*");
        }
        const outputs = definition.output || [];
        const names = definition.output_name || [];
        if (outputs.length) outputs.forEach((output, index) => this.addOutput(names[index] || String(output), String(output)));
        else if (rawNode) for (const output of rawNode.outputs || []) this.addOutput(output.name, output.type || "*");
        this.size = [Math.max(250, this.computeSize()[0]), Math.max(90, this.computeSize()[1])];
      }
    }
    ComfyNode.title = definition.display_name || type;
    ComfyNode.desc = definition.description || "";
    LiteGraph.registerNodeType(type, ComfyNode);
    this.registered.add(type);
  }

  load(name, api, ui = null) {
    this.loading = true;
    try {
      this.activeName = name || "";
      this.originalUi = ui ? clone(ui) : null;
      this.revision++;
      this._compiledApi = null;
      this._compiledApiPromise = null;
      this._serializedUi = null;
      this.graph.clear();
      const entries = Object.entries(api || {});
      const apiIds = new Set(entries.map(([id]) => String(id)));
      for (const [, node] of entries) this.register(node.class_type, this.definitions[node.class_type]);
      if (ui?.nodes) for (const node of ui.nodes) this.register(node.type, this.definitions[node.type], node);
      if (ui?.nodes?.length) {
        try {
          this.graph.configure(clone(ui));
        } catch (error) {
          this.graph.clear();
          this.buildFromApi(api);
          console.warn("ComfyUI 画布恢复失败，使用 API 图重建", error);
        }
      } else this.buildFromApi(api);
      for (const node of this.graph._nodes) {
        const entry = api?.[String(node.id)];
        node._apiInputs = entry ? clone(entry.inputs || {}) : {};
        node._importedWidgets = ui?.nodes?.find((item) => String(item.id) === String(node.id))?.widgets_values;
        if (!entry && node.type.includes("Power Lora Loader") && Array.isArray(node._importedWidgets)) {
          let index = 1;
          for (const value of node._importedWidgets) {
            if (value && typeof value === "object" && !Array.isArray(value) && "lora" in value) {
              node._apiInputs[`lora_${index++}`] = clone(value);
            }
          }
        }
        this.syncWidgets(node);
      }
      if (!ui && entries.length && !entries.some(([, node]) => node._meta?.pos)) this.graph.arrange(50);
      this.dirty = false;
      this.canvas.setDirty(true, true);
      this.fit();
      this.onSelect(null);
      this.onChange();
    } finally {
      this.loading = false;
    }
  }

  buildFromApi(api) {
    const entries = Object.entries(api || {});
    const nodes = new Map();
    for (const [id, data] of entries) {
      const node = LiteGraph.createNode(data.class_type);
      if (!node) throw new Error(`无法创建节点 ${data.class_type}`);
      node.id = Number(id);
      if (data._meta?.title) node.title = data._meta.title;
      if (data._meta?.pos) node.pos = [Number(data._meta.pos.x) || 0, Number(data._meta.pos.y) || 0];
      this.graph.add(node);
      nodes.set(String(id), node);
    }
    for (const [id, data] of entries) {
      const target = nodes.get(String(id));
      for (const [field, value] of Object.entries(data.inputs || {})) {
        if (!isLink(value, nodes)) continue;
        const source = nodes.get(String(value[0]));
        let slot = target.findInputSlot(field);
        if (slot < 0) {
          target.addInput(field, source.outputs?.[value[1]]?.type || "*");
          slot = target.findInputSlot(field);
        }
        source.connect(value[1], target, slot);
      }
    }
  }

  syncWidgets(node) {
    const inputs = node._apiInputs || {};
    for (const widget of node.widgets || []) {
      if (Object.hasOwn(inputs, widget.name) && !Array.isArray(inputs[widget.name])) widget.value = clone(inputs[widget.name]);
    }
  }

  addNode(type, pos = null) {
    this.register(type, this.definitions[type]);
    const node = LiteGraph.createNode(type);
    if (!node) throw new Error(`远端没有节点 ${type}`);
    const center = this.canvas.ds;
    node.pos = pos || [
      (this.element.width / 2 - center.offset[0]) / center.scale,
      (this.element.height / 2 - center.offset[1]) / center.scale,
    ];
    node._apiInputs = {};
    for (const [name, spec] of Object.entries(fieldsFor(this.definitions[type]))) {
      if (widgetKind(spec)) node._apiInputs[name] = widgetDefault(spec);
    }
    this.graph.add(node);
    this.canvas.selectNode(node);
    this.changed();
    return node;
  }

  deleteSelected() {
    for (const node of Object.values(this.canvas.selected_nodes || {})) this.graph.remove(node);
    this.onSelect(null);
    this.changed();
  }

  async exportWorkflow() {
    return this.exportWorkflowSnapshot();
  }

  async exportWorkflowSnapshot() {
    while (true) {
      const revision = this.revision;
      const api = await this.getCompiledApi(revision);
      if (revision !== this.revision) continue;
      if (!this._serializedUi) this._serializedUi = this.serializeUiWorkflow(api);
      if (revision !== this.revision) {
        this._serializedUi = null;
        continue;
      }
      return { workflow: clone(api), ui_workflow: clone(this._serializedUi) };
    }
  }

  async getCompiledApi(revision) {
    if (this._compiledApi) return this._compiledApi;
    if (!this._compiledApiPromise) {
      this._compiledApiPromise = this.compileApiWorkflow().then((api) => {
        if (revision === this.revision) this._compiledApi = api;
        return api;
      }).finally(() => {
        this._compiledApiPromise = null;
      });
    }
    const api = await this._compiledApiPromise;
    if (revision !== this.revision) return this.getCompiledApi(this.revision);
    return api;
  }

  async compileApiWorkflow() {
    this.apiCompileCount++;
    const nodeDtoMap = new Map();
    for (const node of this.graph.computeExecutionOrder(false)) {
      const dto = new ExecutableNodeDTO(node, [], nodeDtoMap);
      nodeDtoMap.set(dto.id, dto);
      if (node.mode === LGraphEventMode.NEVER || node.mode === LGraphEventMode.BYPASS) continue;
      for (const innerNode of dto.getInnerNodes()) nodeDtoMap.set(innerNode.id, innerNode);
    }
    const api = {};
    const executionIds = new Set(nodeDtoMap.keys());
    for (const dto of nodeDtoMap.values()) {
      if (dto.isVirtualNode || dto.mode === LGraphEventMode.NEVER || dto.mode === LGraphEventMode.BYPASS) continue;
      const node = dto.node;
      const inputs = clone(node._apiInputs || {});
      for (const [widgetIndex, widget] of (node.widgets || []).entries()) {
        if (!widget.name || widget.options?.serialize === false) continue;
        const value = widget.serializeValue ? await widget.serializeValue(node, widgetIndex) : widget.value;
        inputs[widget.name] = widget.type === "curve" && value != null
          ? { __type__: "CURVE", __value__: clone(value) }
          : Array.isArray(value) ? { __value__: clone(value) } : clone(value);
      }
      for (const [slot, input] of dto.inputs.entries()) {
        const resolved = dto.resolveInput(slot);
        if (!resolved) {
          if (isLink(inputs[input.name], executionIds)) delete inputs[input.name];
          continue;
        }
        if (resolved.widgetInfo) {
          const value = resolved.widgetInfo.value;
          inputs[input.name] = Array.isArray(value) ? { __value__: clone(value) } : clone(value);
        } else {
          inputs[input.name] = [resolved.origin_id, Number.parseInt(resolved.origin_slot, 10)];
        }
      }
      for (const [field, value] of Object.entries(inputs)) {
        if (isLink(value, executionIds) && !dto.inputs.some((input) => input.name === field && input.linkId != null)) {
          delete inputs[field];
        }
      }
      const title = dto.title || node.title || dto.type;
      api[String(dto.id)] = {
        class_type: dto.comfyClass || node.comfyClass || dto.type,
        inputs,
        _meta: {
          title,
          ...(node.pos ? { pos: { x: node.pos[0], y: node.pos[1] } } : {}),
        },
      };
    }
    const outputIds = new Set(Object.keys(api));
    for (const node of Object.values(api)) {
      for (const [name, value] of Object.entries(node.inputs)) {
        if (Array.isArray(value) && value.length === 2 && !outputIds.has(String(value[0]))) delete node.inputs[name];
      }
    }
    return api;
  }

  serializeUiWorkflow(api) {
    const ui = clone(this.graph.serialize());
    const uiNodes = new Map(ui.nodes.map((node) => [String(node.id), node]));
    for (const [id, node] of uiNodes) {
      const live = this.graph.getNodeById(Number(id));
      if (!live) continue;
      if (Array.isArray(live._importedWidgets) && live._importedWidgets.length > (live.widgets || []).length) {
        node.widgets_values = clone(live._importedWidgets);
      }
      if (node.type?.includes("Power Lora Loader") && api[id]) {
        const loras = Object.entries(api[id].inputs)
          .filter(([key, value]) => /^lora_\d+$/.test(key) && value && typeof value === "object" && !Array.isArray(value))
          .sort(([a], [b]) => Number(a.slice(5)) - Number(b.slice(5)))
          .map(([, value]) => value);
        if (loras.length) node.widgets_values = clone(loras);
      }
    }
    return ui;
  }

  markSaved() {
    this.dirty = false;
    this.onChange();
  }

  fit() {
    const nodes = this.graph._nodes;
    if (!nodes.length) return;
    const left = Math.min(...nodes.map((node) => node.pos[0]));
    const top = Math.min(...nodes.map((node) => node.pos[1]));
    const right = Math.max(...nodes.map((node) => node.pos[0] + node.size[0]));
    const bottom = Math.max(...nodes.map((node) => node.pos[1] + node.size[1]));
    this.canvas.ds.fitToBounds([left - 60, top - 60, right - left + 120, bottom - top + 120]);
    this.canvas.setDirty(true, true);
  }
}
