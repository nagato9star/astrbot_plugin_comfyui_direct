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

先在 Workflow Studio 工作流页导入 ComfyUI 工作流，再为已有模型家族绑定生图或编辑图用途。LLM 调用 `comfyui_draw` 时填写模型家族，插件便会走对应工作流自由生图。

调试出满意的底模、LoRA、画幅和采样参数后，可以另存为配方。`comfyui_recipe_draw` 只需配方名和本次提示词，即可快捷复用整套参数。配方引用模型家族；工作流选择和节点槽位由家族及工作流档案统一管理，因此更换家族工作流无需逐份修改配方。

本插件完全开源免费，欢迎 Issue 和 PR。

## ✨ 核心功能

| 功能 | 说明 |
|:---|:---|
| **按模型家族自由生图** | LLM 填写家族名，插件自动选择工作流；底模、LoRA、画幅和采样参数可自由调整 |
| **配方快捷生图** | 输入配方名和本次提示词，复用实验好的模型、LoRA 和采样参数 |
| **Workflow Studio** | 分别管理工作流 JSON、家族绑定与节点映射，以及静态配方和试画 |
| **查一下再画** | 角色、画师、底模、LoRA 不确定时先查短列表；LoRA 支持 style / character 等分类 |

## 图片缓存与清理

Bot 生图、原始工作流执行、输出下载和工作台试画统一将原图缓存到 AstrBot 的 `data/plugin_data/astrbot_plugin_comfyui_direct/output/`（由 `StarTools.get_data_dir` 定位）。缓存按图片内容哈希命名，保留原始字节，同内容复用并刷新保留时间，同名不同内容分别保存。旧版 output 图片也纳入清理。

管理员可发送：

- `/comfyui_cache` 或 `/comfyui_cache status`：查看缓存数量、容量和目录。
- `/comfyui_cache expired`：立即按保留期限和容量上限清理。
- `/comfyui_cache clear`：清理图片缓存。

配置项 `image_cache_auto_clean` 默认开启，启动及每小时执行清理；`image_cache_days` 默认 7 天；`image_cache_max_mb` 默认 1024 MiB。天数或容量设为 0 可关闭对应限制。清理只处理 output 直属图片，保留配方、工作流和生成历史。最近 5 分钟保存或复用的图片始终保留以供发送，所以清空后可能还有近期文件，容量也可能暂时超限。历史里的本地图片路径在清理后可能失效。

## 生图上下文与工作流适配

默认 `llm_tool_mode=basic` 启用自由生图、配方生图和查询；配置编辑工作流后也启用图片编辑。家族目录提供 `prompt_style`。生成与编辑成功回执包含发送状态、本地保存路径和任务 ID，完整参数继续保存到生成历史。配方生图只需提示词及可选配方名。

已配置或保存的工作流映射直接使用，自动检测仅作为无映射时的回退。主提示词节点失效时会提示重新确认映射，避免消耗一次无效生成。图片返回优先选择最终 output；仅有 PreviewImage 等预览输出时使用其真实 type 与 subfolder 下载。

## 🚀 快速开始

### 1. 安装

将插件目录放入 AstrBot 的 `data/plugins/` 下，在 WebUI 重载插件列表。

依赖：`httpx>=0.27`（见 `requirements.txt`，AstrBot 自带）。

### 2. 配置 ComfyUI 地址

在插件配置中确认 `comfyui_host`（默认 `127.0.0.1:8188`，本机）。

### 3. 添加模型家族

先在 Workflow Studio 导入工作流 JSON。随后在插件配置的“生图家族与工作流”中添加家族：

- `name`：暴露给 LLM 的家族名，例如 `anima`、`krea2`、`flux`
- `workflow`：这个家族使用的工作流
- `prompt_style`：`danbooru`、`natural` 或 `auto`
- `description`：适用画面或用途，会随家族清单提供给 LLM

需要图片编辑时，在“独立编辑工作流路由”配置编辑路由名并选择已导入的工作流，也可以在 Workflow Studio 的工作流页直接创建或修改编辑路由。编辑路由与生图家族相互独立；LLM 调用 `comfyui_edit` 时使用 `edit_workflow` 选择路由。工作流页还可编辑 API JSON、独立保存节点映射。工作流、家族和默认配方等选择框由本地插件数据填充；配置页保留按工作流的节点下拉与通用手填条目。导入新工作流后重新打开配置页即可看到新选项。

Qwen Image 2.1 可分别配置 T2I 与编辑工作流：生图家族 `qwen` 绑定 T2I 模板，独立编辑路由 `qwen-edit` 绑定编辑模板。LLM 调 `comfyui_draw(model_family="qwen")` 生成新图，调 `comfyui_edit(edit_workflow="qwen-edit")` 修改已有图片；两条入口各走自己的工作流，不需要把编辑路由登记为生图家族。T2I 分辨率选择器映射 `aspect_ratio` 与 `megapixels`，例如方图 2K 约为 4.0 MP。编辑时省略 `resolution` 会保留来源图原始尺寸，适合在原图上局部修改；传 `resolution` 会按来源图比例缩放画布。也可以传 `custom_size=true` 启用工作流中的分辨率选择器，或直接传 `width`/`height` 指定新画布；只指定一边时另一边按来源图比例计算。画布宽高写入映射的 `size` 节点（如 `EmptyLatentImage.width/height`）；工作流有直接 `resolution` 输入时也会同步写入。Qwen 多图编辑工作流里的 `images.image_1`、`images.image_2` 会自动识别并按消息附件顺序填入；附件多于工作流输入时用 `image_indices` 选择。请在 Workflow Studio 为尺寸输入保存节点映射；显式传参但缺少所需映射时，工具会提示补齐。配置编辑工作流时，先在 Workflow Studio 从 ComfyUI 历史导入 API 工作流，再创建独立路由并绑定模板。智能识别会沿最终编辑分支选择提示词、采样、模型及来源图片；无法唯一判定时留空。点「重新识别」查看建议，核对后点「保存映射」；配置页手动映射始终优先。确认「用户要画的内容」指向 `TextEncodeQwenImageEdit`，「编辑来源图片」指向该分支的 `LoadImage`。当前消息或引用消息附图时，`comfyui_edit` 会自动上传并填入对应图片节点；也可以使用本插件此前回执中的本地图片路径。只有一条编辑路由时可省略 `edit_workflow`。旧版 `edit_families` 和 `model_families[].edit_workflow` 配置会作为兼容来源读取。

### 4. 分别保存工作流与配方

打开插件页面 Workflow Studio：

1. 在**工作流**页选择模板和生图/编辑图用途。生图工作流绑定到生图家族；编辑工作流绑定到独立编辑路由。需要时修改 API JSON，确认 **用户要画的内容**、**编辑来源图片**、**出图采样** 等槽位后点「保存映射」。槽位归工作流所有。
2. 在**静态配方**页选择家族，填写底模、LoRA、画幅和采样参数，起名并保存。配方文件只记录家族与参数；导入、编辑、绑定工作流及保存节点映射均不会改写配方。只有显式旧版默认参数的首次迁移可能自动创建一条兼容配方。
3. 在右侧使用已保存的配方试画。表单有新修改时先保存配方，再试画已保存的参数。

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
| `model_families` | `anima → anima-v3` | 生图家族配置；工作流可从本地已导入模板下拉选择 |
| `edit_workflows` | 空 | 独立编辑路由；每条记录提供路由名并选择本地编辑工作流，配置后启用 `comfyui_edit` |
| `workflow_node_mappings` | 空 | 可重复添加的工作流节点映射；填写各槽位的节点 ID，优先于 Workflow Studio 档案和自动检测 |
| `default_recipe` | `默认` | 用户只说「画一张」时用的配方；在工作台配方列表点「设为默认」自动写入，也可直接填配方名 |
| `llm_tool_mode` | `basic` | `basic` 暴露自由生图、图片编辑（已配置时）、配方生图和查询；`full` 打开诊断工具 |
| `allow_llm_unsafe_tools` | `false` | 是否允许 LLM 执行任意工作流、读取本地图片上传、释放显存和删除配方；默认关闭 |
| `default_workflow` / `node_slots` | 旧版兼容 | `model_families` 为空时使用；新版槽位在 Workflow Studio 工作流页按工作流保存 |
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

默认 `llm_tool_mode=basic` 启用以下工具；`comfyui_edit` 仅在至少配置一个可用的独立编辑工作流后启用：

| 工具 | 说明 |
|:---|:---|
| `comfyui_draw` | 自由生图；必填 `model_family` 和 `prompt`，按家族选择工作流，可填写底模、LoRA、尺寸、分辨率选择器比例与目标 MP，并可另存配方 |
| `comfyui_edit` | 用独立 `edit_workflow` 路由修改当前或引用消息中的一张或多张图片；可选分辨率与自定义画布参数 |
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

底模与 LoRA 按 `model_family` 查询，支持 `anima`、`krea2`、`sdxl`、`flux`、`illustrious`、`pony` 等家族，以及配置规则中的自定义家族。例如：

```text
comfyui_lookup(type="lora", model_family="anima", query="style")
comfyui_lookup(type="lora", model_family="krea2", query="水彩")
comfyui_lookup(type="model", model_family="sdxl")
comfyui_list_models(kind="lora", model_family="anima", limit=5, offset=0)
comfyui_list_models(kind="model", model_family="sdxl", limit=5)
```

默认每页 5 项，最多 10 项；有后续结果时返回 `next_offset`。省略家族时先给出各家族数量，`kind=all` 仅给资源数量摘要。`kind=model` 与 `kind=unet` 都表示底模。查询底模/LoRA 时可以省略 `query`，角色/画师查询仍需关键词。`comfyui_models_search` 和 `comfyui_model_info` 也支持家族参数；底模元数据用 `types="Checkpoint"`，LoRA 用 `types="LORA"`。

归类优先级：插件配置中的 `resource_family_rules` → 基础模型元数据 → 目录/文件名推断。配置规则支持大小写及斜杠归一化，例如 `kind=lora, family=anima, pattern=Anima/*`；无特征的底模可用完整文件名配置。家族名应与生图路由一致，自定义路由需添加对应规则。SDXL、Pony、Illustrious 分别列出；FLUX Krea 与 Krea2 分开归类。

默认结果排除未知家族，使用 `model_family="unknown"` 或 `include_unknown=true` 可查看，并明确标记兼容性未确认。未知资源不会通过模糊关键词自动选用；核实后仍可显式填写完整文件名。生图与配方提交会拦截已知家族冲突，同名文件要求明确目录。规则和文件名推断依赖标注准确性，不构成模型张量结构验证。固定在工作流中的加速 LoRA 保持原设置。

LoRA 查询同时支持文件名、分类、标签和用途关键词。在线元数据回退只接受文件名匹配的版本，避免直接套用搜索结果首项。升级后资源缓存会重新同步，原有离线缓存仍可回退。


`comfyui_draw` 的 `lora` 是字符串：可填文件名、唯一关键词，多个用逗号分隔；指定权重时传 JSON 数组字符串，例如 `"[{\"name\":\"style.safetensors\",\"strength\":0.6}]"`，其中示例文件名需替换成查询结果。传入列表会覆盖工作流映射的 Power Loader 占位槽或旧版明确映射的可选 LoRA 链；独立加速 LoRA 始终保持工作流原值。省略时沿用可选 LoRA 原值，传 `"[]"` 或 `"none"` 只关闭映射槽位。

选用 LoRA 时，可将查询返回的已知触发词同步填入 `trigger_words`，保留原词格式，无需用户再次提出。查询未提供触发词时可省略该字段并继续使用 LoRA。插件的绘图工具仍由调用方显式填写触发词；工作台选 LoRA 后自动填充文本框的行为保持不变。

## 🖥️ WebUI：Workflow Studio

AstrBot Dashboard → 插件页 → **Workflow Studio**，分为工作流与静态配方两页：

- 工作流页导入、编辑 API JSON、绑定生图家族或独立编辑路由，并独立保存共享槽位映射
- 配方页只保存家族与底模、LoRA、画幅、采样等静态参数
- 从已保存配方试跑，并从历史另存新配方
- 配方列表一键「设为默认」，机器人只说「画一张」时即用这套
- 模型选择：UNET / LoRA / CLIP / VAE 下拉来自自动同步清单
- 连接状态灯 + 「配方试画」：按已保存配方生成，结果内联预览，可一键中断

后端接口（`webapi.py`）：`workflows` / `workflow` / `workflow/import` / `workflow/profile` / `workflow/bind` / `workflow/delete` / `recipes` / `recipe/save` / `status` / `generate`。

## ⚠️ 注意事项

- 图片由插件通过 `event.send` 直接发送，不要重复调用 `send_message_to_user`。
- ComfyUI 不可达时生成失败，请确认 ComfyUI 已启动且网络可达。
- 提交失败时会解析 ComfyUI 400 响应，返回真实错误原因（缺节点/模型不存在等）。

## 📄 License

[MIT](LICENSE) © 2026 长门九曜
