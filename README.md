# ✨ ComfyUI Direct

<div align="center">

**局域网直连 ComfyUI API 的 AstrBot 插件，生图、查模型、管工作流，一套搞定。**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![AstrBot](https://img.shields.io/badge/AstrBot-%E2%89%A54.16-green)
![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Linux-lightgrey)
[![Last Commit](https://img.shields.io/github/last-commit/nagato9star/astrbot_plugin_comfyui_direct)](https://github.com/nagato9star/astrbot_plugin_comfyui_direct/commits/main)

</div>

---

## 📢 简介

ComfyUI Direct 是一款基于 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 的生图插件，通过局域网直连 ComfyUI API，让 Bot 可以直接生成图片、查询模型清单、管理工作流模板。内置 Anima 工作流（anima-v3），提示词自动按 画师串 / 质量词 / 主提示词 / LoRA 触发词 四段组装，配合 Danbooru tag 校正，出图又快又准。

本插件完全开源免费，欢迎 Issue 和 PR。

## ✨ 核心功能

| 功能 | 说明 |
|:---|:---|
| **图片生成** | `comfyui_generate` 直连 ComfyUI，图片直接发送到当前会话 |
| **模型查询** | UNET / LoRA / CLIP / VAE / Embedding 清单自动同步并本地缓存，离线回退缓存 |
| **队列与 GPU** | 查询运行中/待执行任务数、ComfyUI 版本、内存与显存占用 |
| **中断生成** | 中断正在执行的任务，可同时从待执行队列移除 |
| **booru 查证** | danbooru / gelbooru 查画师、角色触发词、别名与常用 tag，告别凭记忆编 tag |
| **模型元数据** | 读本地 safetensors 头部元数据，或按名称搜 civitai 官方 trainedWords |
| **civitai 配方** | 搜参考图直接返回完整生成配方（模型/prompt/sampler/steps/cfg/seed） |
| **提示词优化** | 自然语言需求自动扩展成 Danbooru tags，支持联网搜索与深度思考 |
| **Workflow Studio** | WebUI 节点画布编辑器，可视化编辑工作流模板并一键试跑 |

## 🚀 快速开始

### 1. 安装

将插件目录放入 AstrBot 的 `data/plugins/` 下，在 WebUI 重载插件列表。

依赖：`httpx>=0.27`（见 `requirements.txt`，AstrBot 自带）。

### 2. 配置 ComfyUI 地址

在插件配置中确认 `comfyui_host`（默认 `127.0.0.1:8188`，本机）。

### 3. 开始使用

直接让 LLM 调用工具即可，只需填 `prompt`，其余参数用配置默认值。

## ⚙️ 配置（`_conf_schema.json`）

| 配置项 | 默认值 | 说明 |
|:---|:---|:---|
| `comfyui_host` | `127.0.0.1` | ComfyUI 主机 IP（自填） |
| `comfyui_port` | `8188` | ComfyUI 端口 |
| `comfyui_timeout` | `300` | 生成等待超时（秒） |
| `model_cache_ttl` | `600` | 模型清单缓存刷新间隔（秒），0 表示每次强制同步 |
| `default_workflow` | `anima-v3` | 默认工作流模板名 |
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
| `comfyui_list_models` | 查询可用底模 / LoRA / CLIP / VAE / Embedding（可选 `refresh` 强制同步） |
| `comfyui_generate` | 生成图片，可选 model / lora / steps / cfg / sampler / denoise / width / height / seed / workflow 等 |
| `comfyui_prompt_optimize` | 把自然语言需求优化为 Danbooru tags，内置角色 tag 校正 |
| `comfyui_interrupt` | 中断生成（`prompt_id` 可选，默认最近一次），可同时取消排队任务 |
| `comfyui_booru` | 查画师/角色触发词与常用 tag（`source`=danbooru/gelbooru） |
| `comfyui_civitai_search` | 搜参考图并返回生成配方（模型/prompt/负向/sampler/steps/cfg/seed） |
| `comfyui_model_info` | 查模型/LoRA 元数据与触发词（`source`=local / civitai） |
| `comfyui_queue` | 查询队列与 GPU 显存状态 |

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
