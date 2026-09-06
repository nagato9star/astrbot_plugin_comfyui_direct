# ✨ ComfyUI Direct

<div align="center">

**局域网直连 ComfyUI。人配一套默认画法，对机器人说话就能画；点名换模、换 LoRA、换画幅也可以。**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![AstrBot](https://img.shields.io/badge/AstrBot-%E2%89%A54.16-green)
![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Linux-lightgrey)
[![Last Commit](https://img.shields.io/github/last-commit/nagato9star/astrbot_plugin_comfyui_direct)](https://github.com/nagato9star/astrbot_plugin_comfyui_direct/commits/main)

</div>

---

## 📢 简介

ComfyUI Direct 是一款基于 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 的生图插件。

你先在配方工作台里导入 ComfyUI 的图，确认「用户要画的内容」和「出图采样」写到哪，再保存一套默认的底模 / LoRA / 画幅。之后对机器人说「画一个站在街上的女孩」就能出图。

用户点名换配方、换底模、换 LoRA、竖图横图、画师、画质、步数时，机器人会改对应的项；**没点名的一律不动**。它不会去猜节点，也不会把整份模型清单塞进上下文。

本插件完全开源免费，欢迎 Issue 和 PR。

## ✨ 核心功能

| 功能 | 说明 |
|:---|:---|
| **对机器人说话就画** | 默认只填要画的内容，底模和 LoRA 用你保存的配方 |
| **点名再改** | 换配方 / 底模 / LoRA / 画幅 / 画师 / 画质 / 步数，用户没说就不改 |
| **配方工作台** | 导入图、确认格子、保存一套默认画法，还能试一张 |
| **查一下再画** | 角色、画师、底模、LoRA 不确定时先查短列表；LoRA 支持 style / character 等分类 |

## 🚀 快速开始

### 1. 安装

将插件目录放入 AstrBot 的 `data/plugins/` 下，在 WebUI 重载插件列表。

依赖：`httpx>=0.27`（见 `requirements.txt`，AstrBot 自带）。

### 2. 配置 ComfyUI 地址

在插件配置中确认 `comfyui_host`（默认 `127.0.0.1:8188`，本机）。

### 3. 映射节点并保存配方

打开插件页面「配方工作台」：

1. 在 ComfyUI 里 **Save (API Format)**，把 JSON 导进来（或点「刚画过的」）。
2. 确认两个必选格子：**用户要画的内容**、**出图采样**。底模 / LoRA / 画幅有就选。双采样工作流会多出一个「第二段采样」，步数若接到外联整数节点也会跟着改。
3. 填这套默认用的底模、LoRA、竖图还是横图，起个好叫的名字，保存。选 LoRA 时触发词会自动填进文本框。每次出图都会换新种子，除非你明确指定。

然后就可以对机器人说：

| 你说 | 机器人做什么 |
|:---|:---|
| 画一个站在街上的女孩 | 只用这句话，其他全用默认配方 |
| 用立绘那套，全身竖图 | 换配方 + 竖图 |
| 换成喵喵底模，加上 zoda | 按关键词匹配已安装的底模/LoRA，只改这一次 |
| 画师用 xxx | 只改画师 |
| 精细一点 | 才动步数 |
| 把这套记住，叫日常 | 把当前底模/LoRA/画幅存成新配方 |

配置项 `llm_tool_mode=full` 才会把旧的调试工具暴露给模型。

## ⚙️ 配置（`_conf_schema.json`）

| 配置项 | 默认值 | 说明 |
|:---|:---|:---|
| `comfyui_host` | `127.0.0.1` | ComfyUI 主机 IP（自填） |
| `comfyui_port` | `8188` | ComfyUI 端口 |
| `comfyui_timeout` | `300` | 生成等待超时（秒） |
| `model_cache_ttl` | `600` | 模型清单缓存刷新间隔（秒），0 表示每次强制同步 |
| `lora_manager_enabled` | `true` | 复用 ComfyUI LoRA Manager 的分类、标签、用途说明、推荐权重和触发词；未安装时自动跳过 |
| `default_workflow` | `anima-v3` | 默认工作流模板名 |
| `default_recipe` | `默认` | LLM 不指定 recipe 时使用的配方 |
| `llm_tool_mode` | `basic` | `basic` 只暴露 draw/lookup；`full` 打开诊断工具 |
| `allow_llm_unsafe_tools` | `false` | 是否允许 LLM 执行任意工作流、读取本地图片上传、释放显存和删除配方；默认关闭 |
| `node_slots` | 空 | 下拉框：哪个节点是提示词 / KSampler / 底模 / LoRA / 尺寸。导入工作流后自动刷新，重载插件生效 |
| `danbooru_base_url` | `https://danbooru.donmai.us` | danbooru 接口地址（国内可换镜像） |
| `gelbooru_base_url` | `https://gelbooru.com` | gelbooru DAPI 地址（镜像可换） |
| `civitai_api_key` | 空 | civitai API Key（可选，以你的身份调用） |
| `default_artist` / `default_quality` / `default_trigger_words` / `default_negative_prompt` | 空 | 默认画师串/质量词/lora触发词/负向提示词，留空用模板原值 |
| `default_model` | 空 | 默认底模文件名（可用 `comfyui_list_models` 查看），留空用模板原值 |
| `default_lora` | `[]` | 默认 LoRA 列表，按顺序映射 Power Lora Loader 插槽 |
| `default_steps` / `default_cfg` / `default_denoise` | `0` | 默认采样参数，0=用模板原值 |
| `default_sampler_name` / `default_scheduler` | 空 | 默认采样器/调度器，留空=用模板原值 |
| `default_width` / `default_height` | `0` | 默认图片尺寸，0=用模板原值 |
| `prompt_optimize_enabled` | `true` | 启用自然语言优化：中文描述扩展成 Danbooru tags |
| `prompt_builder_max_tokens` | `1000` | 优化模型输出上限 |
| `prompt_builder_provider_id` | 空 | 指定优化用模型 provider id；留空使用当前会话主模型 |
| `prompt_builder_max_content_tags` | `65` | 内容段 tag 数量上限，0 不裁剪 |
| `prompt_builder_web_search_enabled` | `true` | 指令含"联网/搜索"等词时触发 Tavily 联网搜索 |
| `prompt_builder_search_max_results` / `prompt_builder_search_depth` | `5` / `advanced` | 搜索结果数量与深度 |
| `prompt_builder_search_query_template` | `{prompt} 角色 外观 …` | 搜索词模板 |
| `prompt_builder_deep_thinking_enabled` | `true` | 指令含"深度思考"时启用推理 |
| `prompt_builder_reasoning_effort` | `high` | 深度思考强度（high / max） |
| `prompt_builder_template` | 空 | 自定义优化模板，支持多个占位符；留空用内置模板 |
| `danbooru_core_tag_lookup_enabled` | `true` | 角色 tag 联网校正（Donmai 不可用时回退 Safebooru） |
| `danbooru_tag_base_urls` | `https://safebooru.donmai.us,https://danbooru.donmai.us` | Donmai 查询地址，逗号分隔 |
| `danbooru_tag_user_agent` | `AstrBotComfyUIDirect/2.1` | Donmai 访问 UA |
| `danbooru_tag_lookup_timeout` | `6` | 单次角色 tag 查询超时（秒） |
| `danbooru_tag_max_candidates` | `6` | 候选 tag 上限 |

**参数优先级**：LLM 传参 > 插件配置默认值 > 模板原值。

## 🖼️ 工作流模板

- 模板源：插件数据目录 `data/plugin_data/astrbot_plugin_comfyui_direct/workflows/`（首次部署需从原环境拷贝，插件包不含模板文件）。anima-v3 使用 rgthree（Power Lora Loader、Image Comparer）、Comfyroll（CR Prompt Text、JoinStringMulti）与 DanbooruText 自定义节点，需自行安装。
- 兼容旧模板：`nagato-anima`、`anima-v2`，从 AstrBot `data/skills/anima-comfyui/references/` 目录加载（不存在时报错提示）。
- 模板即事实来源：模型 / 步数 / cfg / 采样器默认值都在模板里，代码不写死。

## 📂 数据目录

`data/plugin_data/astrbot_plugin_comfyui_direct/`

- `comfyui_models.json`：模型清单缓存
- `workflows/`：自定义工作流模板
- `output/`：生成的图片文件

## 🤖 LLM 工具

| 工具 | 说明 |
|:---|:---|
| `comfyui_list_models` | 查询可用底模 / LoRA / CLIP / VAE / Embedding；支持 `kind`、`query`、`limit` 筛选，LoRA 显示分类、标签、推荐权重和触发词 |
| `comfyui_generate` | 生成图片，可选 model / lora / steps / cfg / sampler / denoise / width / height / seed / workflow 等 |
| `comfyui_prompt_optimize` | 把自然语言需求优化为 Danbooru tags，内置角色 tag 校正 |
| `comfyui_interrupt` | 中断生成（`prompt_id` 可选，默认最近一次），可同时取消排队任务 |
| `comfyui_booru` | 查画师/角色触发词与常用 tag（`source`=danbooru/gelbooru） |
| `comfyui_civitai_search` | 搜参考图并返回生成配方（模型/prompt/负向/sampler/steps/cfg/seed） |
| `comfyui_model_info` | 查模型/LoRA 元数据与触发词（`source`=local / civitai） |
| `comfyui_queue` | 查询队列与 GPU 显存状态 |

默认不会把 `comfyui_run_workflow`、`comfyui_upload_file`、`comfyui_free_memory` 交给 LLM，且 `comfyui_recipe` 的删除动作也要求在 Workflow Studio 手动完成。确有需要时，先开启 `allow_llm_unsafe_tools`。

LoRA 查询示例：`comfyui_lookup(type="lora", query="style")`、`query="character"`、`query="风格"`。匹配范围包括 LoRA 文件名、LoRA Manager/Civitai 标签、分类和用途说明；生成时仍会使用实际安装的文件名和已记录的触发词。`comfyui_list_models(kind="lora", query="style", limit=10)` 可只返回指定分类，减少 LLM 上下文占用。

## 🖥️ WebUI：Workflow Studio

AstrBot Dashboard → 插件页 → **Workflow Studio**，复刻 ComfyUI 风格的节点画布：

- 可视化编辑/管理工作流模板：加载、保存、另存为、删除
- 节点画布：拖拽节点、滚轮缩放、空白平移、拖端口连线、双击标题改名、右键复制/删除
- 节点库：一键添加 CR Prompt Text / JoinStringMulti / KSampler / Power LoRA 等节点
- 模型选择：UNET / LoRA / CLIP / VAE 下拉来自自动同步清单
- 连接状态灯 + 模型清单侧栏 + 「▶ 试跑」：直接提交画布生成，结果内联预览，可一键中断

后端接口（`webapi.py`，Quart）：`workflows` / `workflow` / `workflow/save` / `workflow/delete` / `status` / `generate`。

## ⚠️ 注意事项

- 图片由插件通过 `event.send` 直接发送，不要重复调用 `send_message_to_user`。
- ComfyUI 不可达时生成失败，请确认 ComfyUI 已启动且网络可达。
- 提交失败时会解析 ComfyUI 400 响应，返回真实错误原因（缺节点/模型不存在等）。

## 📄 License

[MIT](LICENSE) © 2026 长门九曜
