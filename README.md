# ✨ ComfyUI Direct

<div align="center">

**局域网直连 ComfyUI。模型家族负责选择工作流，配方负责复用实验好的 LoRA 与采样参数。**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![AstrBot](https://img.shields.io/badge/AstrBot-%E2%89%A54.16-green)
![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Linux-lightgrey)
[![Last Commit](https://img.shields.io/github/last-commit/nagato9star/astrbot_plugin_comfyui_direct)](https://github.com/nagato9star/astrbot_plugin_comfyui_direct/commits/main)

</div>

---

## 📢 简介

ComfyUI Direct 是一款基于 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 的生图插件。

先在配方工作台导入 ComfyUI 工作流，再到插件配置添加“模型家族”并为它选择工作流。LLM 调用 `comfyui_draw` 时填写模型家族，插件便会走对应工作流自由生图。

调试出满意的底模、LoRA、画幅和采样参数后，可以另存为配方。`comfyui_recipe_draw` 只需配方名和本次提示词，即可快捷复用整套参数。配方引用模型家族；工作流选择和节点槽位由家族及工作流档案统一管理，因此更换家族工作流无需逐份修改配方。

本插件完全开源免费，欢迎 Issue 和 PR。

## ✨ 核心功能

| 功能 | 说明 |
|:---|:---|
| **按模型家族自由生图** | LLM 填写家族名，插件自动选择工作流；底模、LoRA、画幅和采样参数可自由调整 |
| **配方快捷生图** | 输入配方名和本次提示词，复用实验好的模型、LoRA 和采样参数 |
| **配方工作台** | 导入工作流、保存共享槽位映射、编辑配方并试画 |
| **查一下再画** | 角色、画师、底模、LoRA 不确定时先查短列表；LoRA 支持 style / character 等分类 |

## 🚀 快速开始

### 1. 安装

将插件目录放入 AstrBot 的 `data/plugins/` 下，在 WebUI 重载插件列表。

依赖：`httpx>=0.27`（见 `requirements.txt`，AstrBot 自带）。

### 2. 配置 ComfyUI 地址

在插件配置中确认 `comfyui_host`（默认 `127.0.0.1:8188`，本机）。

### 3. 添加模型家族

先在「配方工作台」导入工作流 JSON。随后打开插件配置，在“模型家族与工作流”中添加一条记录：

- `name`：暴露给 LLM 的家族名，例如 `anima`、`krea2`、`flux`
- `workflow`：这个家族使用的工作流
- `prompt_style`：`danbooru`、`natural` 或 `auto`
- `description`：适用画面或用途，会随家族清单提供给 LLM

保存配置并重载插件。每个家族名必须唯一；多个家族可以选择同一张工作流。

### 4. 确认槽位并保存配方

打开插件页面「配方工作台」：

1. 选择模型家族，确认该家族工作流的 **用户要画的内容**、**出图采样** 等槽位。槽位属于工作流，同一工作流的所有配方共用。
2. 填写实验好的底模、LoRA、画幅和采样参数，起一个配方名并保存。配方文件只记录家族与参数。
3. 在右侧输入本次提示词试画。每次生成会使用新种子，除非明确指定。

然后就可以对机器人说：

| 你说 | 机器人做什么 |
|:---|:---|
| 用 anima 家族画一个站在街上的女孩 | 自由生图，使用 anima 对应工作流 |
| 用立绘配方画一个全身女孩 | 快捷复用立绘配方，只写入新提示词 |
| 用 anima 家族，换成喵喵底模并加上 zoda | 在家族工作流中按本次参数自由生成 |
| 画成水彩风格 | 可查询风格 LoRA，按用途和模型适用信息选用，并填写已知触发词 |
| 画师用 xxx | 只改画师 |
| 精细一点 | 才动步数 |
| 把这次参数记住，叫日常 | 将自由生图的实际底模、LoRA、画幅与采样参数存成新配方 |

配置项 `llm_tool_mode=full` 才会把旧的调试工具暴露给模型。

## ⚙️ 配置（`_conf_schema.json`）

| 配置项 | 默认值 | 说明 |
|:---|:---|:---|
| `comfyui_host` | `127.0.0.1` | ComfyUI 主机 IP（自填） |
| `comfyui_port` | `8188` | ComfyUI 端口 |
| `comfyui_timeout` | `300` | 生成等待超时（秒） |
| `model_cache_ttl` | `600` | 模型清单缓存刷新间隔（秒），0 表示每次强制同步 |
| `lora_manager_enabled` | `true` | 复用 ComfyUI LoRA Manager 的分类、标签、用途说明、推荐权重和触发词；未安装时自动跳过 |
| `model_families` | `anima → anima-v3` | 可重复添加的家族配置；包含家族名、工作流、提示词风格和说明 |
| `default_recipe` | `默认` | 用户只说「画一张」时用的配方；在工作台配方列表点「设为默认」自动写入，也可直接填配方名 |
| `llm_tool_mode` | `basic` | `basic` 暴露自由生图、配方生图和查询；`full` 打开诊断工具 |
| `allow_llm_unsafe_tools` | `false` | 是否允许 LLM 执行任意工作流、读取本地图片上传、释放显存和删除配方；默认关闭 |
| `default_workflow` / `node_slots` | 旧版兼容 | `model_families` 为空时使用；新版槽位在配方工作台按工作流保存 |
| `danbooru_base_url` | `https://danbooru.donmai.us` | danbooru 接口地址（国内可换镜像） |
| `gelbooru_base_url` | `https://gelbooru.com` | gelbooru DAPI 地址（镜像可换） |
| `civitai_api_key` | 空 | civitai API Key（可选，以你的身份调用） |
| `animadex_mcp_url` / `animadex_timeout` | `http://127.0.0.1:11451/mcp` / `8.0` | AnimaDex 角色库 MCP 端点与查询超时；不用可忽略 |
| `default_*` 生成参数 | 旧版兼容 | 新版配置页隐藏；已有值仅用于首次生成默认配方和高级兼容入口，日常参数请保存在配方中 |

自由生图的参数优先级为本次 LLM 参数 > 工作流原值。配方生图的优先级为本次提示词/种子/画幅方向 > 配方参数 > 工作流原值。

## 🖼️ 工作流模板

- 模板源：插件数据目录 `data/plugin_data/astrbot_plugin_comfyui_direct/workflows/`（首次部署需从原环境拷贝，插件包不含模板文件）。anima-v3 使用 rgthree（Power Lora Loader、Image Comparer）、Comfyroll（CR Prompt Text、JoinStringMulti）与 DanbooruText 自定义节点，需自行安装。
- 兼容旧模板：`nagato-anima`、`anima-v2`，从 AstrBot `data/skills/anima-comfyui/references/` 目录加载（不存在时报错提示）。
- 模板即事实来源：模型 / 步数 / cfg / 采样器默认值都在模板里，代码不写死。

## 📂 数据目录

`data/plugin_data/astrbot_plugin_comfyui_direct/`

- `comfyui_models.json`：模型清单缓存
- `workflows/`：自定义工作流模板
- `workflow_profiles.json`：按工作流保存的共享节点槽位
- `recipes/`：只保存模型家族与生成参数的快捷配方
- `output/`：生成的图片文件

## 🤖 LLM 工具

默认 `llm_tool_mode=basic` 启用以下三个工具：

| 工具 | 说明 |
|:---|:---|
| `comfyui_draw` | 自由生图；必填 `model_family` 和 `prompt`，按家族选择工作流，可自由填写底模、LoRA、尺寸与采样参数，并可另存配方 |
| `comfyui_recipe_draw` | 快捷配方生图；填写本次 `prompt`，可选 `recipe`、`size` 和 `seed`，其余参数来自配方 |
| `comfyui_lookup` | 查询角色 / 画师规范词，以及底模 / LoRA 文件名；支持按 LoRA 分类、标签、用途挑选，并返回用途说明、推荐权重和触发词 |

`llm_tool_mode=full` 额外启用以下工具，其中标注为需额外开关的工具还要求 `allow_llm_unsafe_tools=true`：

| 工具 | 说明 |
|:---|:---|
| `comfyui_list_models` | 查询可用底模 / LoRA / CLIP / VAE / Embedding；支持 `kind`、`query`、`limit` 筛选，LoRA 显示分类、标签、推荐权重和触发词 |
| `comfyui_generate` | 旧版高级生成入口；保留配方和工作流参数兼容，日常调用优先使用上面的两个明确入口 |
| `comfyui_interrupt` | 中断生成（`prompt_id` 可选，默认最近一次），可同时取消排队任务 |
| `comfyui_booru` | 查画师/角色触发词与常用 tag（`source`=danbooru/gelbooru） |
| `comfyui_civitai_search` | 搜参考图并返回生成配方（模型/prompt/负向/sampler/steps/cfg/seed） |
| `comfyui_model_info` | 查模型/LoRA 元数据与触发词（`source`=local / civitai） |
| `comfyui_animadex` | 从 AnimaDex 查询角色、画师、作品系列及角色详情 |
| `comfyui_queue` | 查询队列与 GPU 显存状态 |
| `comfyui_job` | 按任务 ID 查状态、等待完成、取消任务或查询队列 |
| `comfyui_fetch_outputs` | 按任务 ID 下载生成结果并返回本地路径 |
| `comfyui_system_stats` | 查询设备、显存和系统内存状态 |
| `comfyui_nodes` | 搜索节点类或查询节点输入输出结构 |
| `comfyui_validate_workflow` | 提交前检查工作流节点和必填输入 |
| `comfyui_models_search` | 按目录搜索已安装的模型文件 |
| `comfyui_recipe` | 保存、列出、加载配方；保存时引用模型家族并记录生成参数；删除动作需额外开关 |
| `comfyui_run_workflow` | 运行指定工作流 JSON 并发送结果，需额外开关 |
| `comfyui_upload_file` | 上传本地图片至 ComfyUI 的 input 目录，需额外开关 |
| `comfyui_free_memory` | 请求卸载模型、释放显存，需额外开关 |

默认不会把 `comfyui_run_workflow`、`comfyui_upload_file`、`comfyui_free_memory` 交给 LLM，且 `comfyui_recipe` 的删除动作也要求在 Workflow Studio 手动完成。确有需要时，先开启 `allow_llm_unsafe_tools`。

LoRA 查询示例：`comfyui_lookup(type="lora", query="style")`、`query="character"`、`query="水彩"`。匹配范围包括 LoRA 文件名、LoRA Manager/Civitai 标签、分类和用途说明；机器人可根据画面需求主动查询并选用，文件名以实际查询结果为准。`comfyui_list_models(kind="lora", query="style", limit=10)` 可只返回指定分类，减少 LLM 上下文占用。

`comfyui_draw` 的 `lora` 是字符串：可填文件名、唯一关键词，多个用逗号分隔；指定权重时传 JSON 数组字符串，例如 `"[{\"name\":\"style.safetensors\",\"strength\":0.6}]"`，其中示例文件名需替换成查询结果。传入列表会覆盖工作流中的 LoRA，要保留的原 LoRA 也需列入；省略时沿用工作流原值。

选用 LoRA 时，可将查询返回的已知触发词同步填入 `trigger_words`，保留原词格式，无需用户再次提出。查询未提供触发词时可省略该字段并继续使用 LoRA。插件的绘图工具仍由调用方显式填写触发词；工作台选 LoRA 后自动填充文本框的行为保持不变。

## 🖥️ WebUI：Workflow Studio

AstrBot Dashboard → 插件页 → **Workflow Studio**，复刻 ComfyUI 风格的节点画布：

- 导入和管理工作流模板，并按工作流保存共享槽位映射
- 从已配置的模型家族中选择配方适用范围
- 编辑底模、LoRA、画幅与采样参数，直接试跑并从历史另存配方
- 配方列表一键「设为默认」，机器人只说「画一张」时即用这套
- 模型选择：UNET / LoRA / CLIP / VAE 下拉来自自动同步清单
- 连接状态灯 + 模型清单侧栏 + 「▶ 试跑」：直接提交画布生成，结果内联预览，可一键中断

后端接口（`webapi.py`）：`workflows` / `workflow` / `workflow/profile` / `workflow/delete` / `recipes` / `recipe/save` / `status` / `generate`。

## ⚠️ 注意事项

- 图片由插件通过 `event.send` 直接发送，不要重复调用 `send_message_to_user`。
- ComfyUI 不可达时生成失败，请确认 ComfyUI 已启动且网络可达。
- 提交失败时会解析 ComfyUI 400 响应，返回真实错误原因（缺节点/模型不存在等）。

## 📄 License

[MIT](LICENSE) © 2026 长门九曜
