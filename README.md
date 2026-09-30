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

先在「配方工作台」导入工作流 JSON。随后打开插件配置，在“生图家族与工作流”中添加一条记录：

- `name`：暴露给 LLM 的家族名，例如 `anima`、`krea2`、`flux`
- `workflow`：这个家族使用的工作流
- `prompt_style`：`danbooru`、`natural` 或 `auto`
- `description`：适用画面或用途，会随家族清单提供给 LLM

需要图片编辑时，在下方“编辑图家族与工作流”添加记录，从已有生图家族和已导入的编辑工作流中分别选择。工作流、家族和默认配方等选择框由本地插件数据填充；“工作流节点映射”也会为每张本地工作流提供专属条目，槽位可按节点下拉选择，通用手填条目继续保留。在工作台导入新工作流后重新打开配置页即可看到新选项。若生图家族也是刚添加的，先保存并重载插件，再到编辑图家族下拉框选择它。每个生图家族名必须唯一；多个家族可以选择同一张工作流。

Qwen 图片编辑：先在「配方工作台」从 ComfyUI 成功任务的历史导入编辑工作流（API 格式），再在“编辑图家族与工作流”选择家族 `qwen` 和导入的模板。智能识别会沿最终编辑分支选提示词、采样、模型及来源图片；多条编辑分支或多个来源图片无法唯一判定时留空。旧档案可点「重新识别」查看新建议，核对后点「保存映射」；配置页手动映射始终优先。确认「用户要画的内容」指向 `TextEncodeQwenImageEdit`，「编辑来源图片」指向该分支的 `LoadImage`。当前消息或引用消息附图时，`comfyui_edit` 会自动上传图片并替换 `LoadImage.image`；也可以使用本插件此前回执中的本地图片路径。图片和文字生成分别使用 `comfyui_edit` 与 `comfyui_draw`。旧配置中保留的 `model_families[].edit_workflow` 仍可用，新“编辑图家族”记录优先。

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
| `model_families` | `anima → anima-v3` | 生图家族配置；工作流可从本地已导入模板下拉选择 |
| `edit_families` | 空 | 编辑图家族配置；选择已有家族和本地编辑工作流，配置后启用 `comfyui_edit` |
| `workflow_node_mappings` | 空 | 可重复添加的工作流节点映射；填写各槽位的节点 ID，优先于 Workflow Studio 档案和自动检测 |
| `default_recipe` | `默认` | 用户只说「画一张」时用的配方；在工作台配方列表点「设为默认」自动写入，也可直接填配方名 |
| `llm_tool_mode` | `basic` | `basic` 暴露自由生图、图片编辑（已配置时）、配方生图和查询；`full` 打开诊断工具 |
| `allow_llm_unsafe_tools` | `false` | 是否开放旧版高级生成、任意 API 工作流执行、本地图片上传、释放显存和删除配方；默认关闭 |
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

插件随包提供 `comfyui-direct-generation` Skill，AstrBot 会从插件 `skills/` 目录自动发现。它指导模型选择自由生图、配方生图或图片编辑工具，并按家族读取 Anima、Krea 2、Qwen-Image-2.1 的提示词参考及兼容 LoRA 规则；可在 AstrBot Skills 管理页面启用或停用。

默认 `llm_tool_mode=basic` 启用以下工具；`comfyui_edit` 仅在至少一个家族配置 `edit_workflow` 后启用：

| 工具 | 说明 |
|:---|:---|
| `comfyui_draw` | 自由生图；必填 `model_family` 和 `prompt`，按家族选择工作流，可自由填写底模、LoRA、尺寸与采样参数，并可另存配方 |
| `comfyui_edit` | 修改当前或引用消息中的图片；无附图时可用本插件上次生成的图片，成功回执给出本地保存路径 |
| `comfyui_recipe_draw` | 快捷配方生图；填写本次 `prompt`，可选 `recipe`、`size` 和 `seed`，其余参数来自配方 |
| `comfyui_lookup` | 查询角色 / 画师规范词，以及底模 / LoRA 文件名；支持按 LoRA 分类、标签、用途挑选，并返回用途说明、推荐权重和触发词 |

`llm_tool_mode=full` 额外启用以下工具，其中标注为需额外开关的工具还要求 `allow_llm_unsafe_tools=true`：

| 工具 | 说明 |
|:---|:---|
| `comfyui_list_models` | 查询可用底模 / LoRA / CLIP / VAE / Embedding；支持 `kind`、`query`、`limit` 筛选，LoRA 显示分类、标签、推荐权重和触发词 |
| `comfyui_generate` | 旧版高级生成入口；兼容旧参数，需额外开启 `allow_llm_unsafe_tools`。日常调用使用 `comfyui_draw` 或 `comfyui_recipe_draw` |
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
| `comfyui_recipe` | 保存、列出、加载配方；保存传 `action=save`、`name`、`model_family`，LoRA 用对象数组，`description` 仅写用途说明；删除仅在开放危险工具后出现 |
| `comfyui_run_workflow` | 运行 ComfyUI API Format JSON 并发送结果，需额外开关；普通画布 JSON 请从 Workflow Studio 导入 |
| `comfyui_upload_file` | 上传本地图片至 ComfyUI 的 input 目录，需额外开关 |
| `comfyui_free_memory` | 请求卸载模型、释放显存，需额外开关 |

默认不会把 `comfyui_generate`、`comfyui_run_workflow`、`comfyui_upload_file`、`comfyui_free_memory` 交给 LLM，且配方删除动作也不会暴露。确有需要时，先开启 `allow_llm_unsafe_tools`。

底模与 LoRA 按 `model_family` 查询，支持 `anima`、`krea2`、`sdxl`、`flux`、`illustrious`、`pony` 等家族，以及配置规则中的自定义家族。例如：

```text
comfyui_lookup(type="lora", model_family="anima", query="style")
comfyui_lookup(type="lora", model_family="krea2", query="水彩")
comfyui_lookup(type="model", model_family="sdxl")
comfyui_lookup(type="model", query="miao miao harem")
comfyui_list_models(kind="lora", model_family="anima", limit=5, offset=0)
comfyui_list_models(kind="model", model_family="sdxl", limit=5)
comfyui_list_models(query="miaomao")
```

默认每页 5 项，最多 10 项；有后续结果时返回 `next_offset`。省略家族和 `query` 时给出家族数量，`kind=all` 给资源数量摘要。填写 `query` 后可省略家族按文件名跨家族搜索；`kind=all` 会跨资源类别搜索。名称搜索忽略大小写与常见分隔符，找不到直接命中时返回少量近似名称并提示核对。搜索结果标明家族；选用底模或 LoRA 时仍需匹配生图工作流家族。`kind=model` 与 `kind=unet` 都表示底模。查询底模/LoRA 时可以省略 `query`，角色/画师查询仍需关键词。`comfyui_models_search` 和 `comfyui_model_info` 也支持家族参数；底模元数据用 `types="Checkpoint"`，LoRA 用 `types="LORA"`。

### 中文角色名 → 角色 tag 与外观参考

```python
comfyui_lookup(type="character", query="初音未来")
comfyui_lookup(type="character", query="伊蕾娜")
comfyui_lookup(type="character", query="elysia_(honkai_impact)")
```

角色查询先使用本地名字索引和配置别名定位规范 tag，再对唯一精确匹配查询 [AnimaDex](https://github.com/zetaneko/AnimaDex) 的结构化角色样例标签。不需要额外 MCP 服务，不拉取图片或帖子样本，不调用 LLM 翻译名字。同名、不同版本或部分匹配返回候选；眼色和发色有多个参考值时分列候选，按版本选用。

首次查询下载约 1 MB 的名字 JSON，来自 [Jannchie/danbooru-tag-index](https://github.com/Jannchie/danbooru-tag-index) 的 Wiki 别名选取、人工校订与错误译名排除列表。名字选取包含 LLM 辅助，覆盖有限，不能当作完整官方角色名库。网络兜底使用 Danbooru 角色 tag / Wiki 别名元数据；外观参考来自 AnimaDex 样例，可能含默认服饰。所有结果标明来源。

- `character_aliases`：可编辑别名到规范 tag 的映射，支持填写自定义外观 tag；同名可配置多行，配置优先于社区索引。
- `character_names_file`：可指定本地 JSON 替代下载。支持 `{"hatsune_miku":{"zh_hans":"初音未来","ja":"初音ミク"}}`，或 `[{"tag":"hatsune_miku","names":["初音未来"],"appearance_tags":["aqua_hair","twintails"]}]`。
- `character_reference_url`：默认 `https://animadex.net`，可换成本地 AnimaDex Web 服务；留空关闭外观联网查询。
- `character_cache_hours`：默认 168 小时，名字与外观缓存保存在插件数据目录的 `character_tags.json`；重启后可复用。
- `character_lookup_timeout`：默认每阶段 6 秒；缓存命中不等待网络。缺少名字时仍可查询英文 tag，缺少外观来源时保留角色 tag。

离线回归：`python scripts/character_lookup_test.py`，覆盖同名候选、错误译名排除、外观角色一致性、配置覆盖、并发请求复用、重启缓存和免帖子取样。

归类优先级：插件配置中的 `resource_family_rules` → 基础模型元数据 → 目录/文件名推断。配置规则支持大小写及斜杠归一化，例如 `kind=lora, family=anima, pattern=Anima/*`；无特征的底模可用完整文件名配置。家族名应与生图路由一致，自定义路由需添加对应规则。SDXL、Pony、Illustrious 分别列出；FLUX Krea 与 Krea2 分开归类。

默认浏览结果排除未知家族；名称查询准确命中文件名或去扩展名的名称时会显示未知资源，并提示兼容性未确认。使用 `model_family="unknown"` 或 `include_unknown=true` 可查看其他未知项。未知资源不会通过近似名称自动推荐；核实后仍可显式填写完整文件名。生图与配方提交会拦截已知家族冲突，同名文件要求明确目录。规则和文件名推断依赖标注准确性，不构成模型张量结构验证。固定在工作流中的加速 LoRA 保持原设置。

LoRA 名称可跨家族查询；按分类、标签和用途关键词挑选 LoRA 时需指定 `model_family`。在线元数据回退只接受文件名匹配的版本，避免直接套用搜索结果首项。升级后资源缓存会重新同步，原有离线缓存仍可回退。


`comfyui_draw` 的 `lora` 使用对象数组，例如 `[ {"name":"style.safetensors","strength":0.6} ]`；文件名应取自查询结果。旧版文件名字符串仍兼容。传入数组会覆盖工作流映射的 Power Loader 占位槽或旧版明确映射的可选 LoRA 链；独立加速 LoRA 始终保持工作流原值。省略时沿用可选 LoRA 原值，传空数组只关闭映射槽位。

选用 LoRA 时，可将查询返回的已知触发词同步填入 `trigger_words`，保留原词格式，无需用户再次提出。查询未提供触发词时可省略该字段并继续使用 LoRA。插件的绘图工具仍由调用方显式填写触发词；工作台选 LoRA 后自动填充文本框的行为保持不变。

## 🖥️ WebUI：Workflow Studio

AstrBot Dashboard → 插件页 → **Workflow Studio**，默认打开可编辑节点画布：

- 在节点库中搜索、点击或拖入节点；双击画布空白处可搜索全部远端节点，空格拖动画布、滚轮围绕鼠标缩放
- 在节点内编辑多行提示词、数值及下拉选项；右键「参数输入」可将参数转为连线端口，右侧检查器也可编辑输入，两侧面板可以收起
- 支持 Ctrl+Z 撤销、Ctrl+Y / Ctrl+Shift+Z 重做、Ctrl+C / Ctrl+V 复制粘贴、Delete 删除；输入文本时保留文本框自身的撤销操作
- Ctrl+S 保存、Ctrl+Enter 运行；种子的 fixed / increment / decrement / randomize 控制影响提交成功后的下一次运行
- 导入 ComfyUI 前端 JSON 时保留原始布局，保存时同时保存执行图与画布图、参数和视角；画布适配屏幕像素比例
- 点击「运行当前画布」可直接提交到配置的远端 ComfyUI，使用画布 JSON 写入输出图片的工作流元数据
- 执行图通过 Comfy Org 的 `ExecutableNodeDTO` 解析连接和旁路节点；API JSON 会缓存，画布编辑后重新编译
- 节点库取自远端 `/object_info`；远端无法连接时，已保存前端画布的工作流仍可打开，添加节点需要恢复连接
- 使用特殊前端扩展控件的自定义节点在检查器中显示其 API 输入；专用 DOM 控件仍需逐个适配

切换到「配方参数」仍可：

- 导入和管理工作流模板，并按工作流保存共享槽位映射
- 从已配置的模型家族中选择配方适用范围
- 编辑底模、LoRA、画幅与采样参数，直接试跑并从历史另存配方
- 配方列表一键「设为默认」，机器人只说「画一张」时即用这套
- 模型选择：UNET / LoRA / CLIP / VAE 下拉来自自动同步清单
- 连接状态灯 + 模型清单侧栏 + 「▶ 试跑」：直接提交画布生成，结果内联预览，可一键中断

后端接口（`webapi.py`）：`workflows` / `workflow` / `workflow/nodes` / `workflow/save` / `workflow/run` / `workflow/profile` / `workflow/delete` / `recipes` / `recipe/save` / `status` / `generate`。

节点画布使用 MIT 许可的 Comfy Org LiteGraph 0.17.2，构建文件随插件打包。修改画布源码后在 `pages/workflow-editor/` 运行 `npm ci && npm run build`。

### 生图任务等待与资源准备

- `comfyui_timeout` 是任务等待上限；`comfyui_request_timeout` 单独控制 HTTP 请求超时，默认 15 秒。连接建立失败可重试提交，提交响应超时会保留“可能已提交”的提示，避免重复排队。
- `comfyui_events_enabled` 默认开启，通过一个可重连的 WebSocket 接收完成、错误和进度事件；HTTP 历史是结果依据，事件连接失效时继续轮询。等待超时会返回任务 ID，WebUI 可点「继续等待」，复用原任务。
- 生图准备使用单次资源快照，共用节点定义缓存。全量 LoRA Manager 分类目录在后台同步；选中的 LoRA 按需读取元数据，查询完整资源信息时才等待全量补全。
- 同一个 WebUI 结果的重复／并发查询复用本地原图，生成历史只写一次；结果缓存保存文件路径，不长期保留 base64 图片。
- `auto_lora_trigger_words` 默认关闭，Bot 和 WebUI 规则一致。启用后只补全未显式设置且已映射的触发词槽位；显式留空保留，训练词频与数据集标签推测不会自动注入。

回归验证：`python scripts/generation_pipeline_test.py`，覆盖提交重试、请求与等待期限、执行事件及断线回退、并发结果查询、资源准备、各生图入口的触发词优先级。

## ⚠️ 注意事项

- 图片由插件通过 `event.send` 直接发送，不要重复调用 `send_message_to_user`。
- ComfyUI 不可达时生成失败，请确认 ComfyUI 已启动且网络可达。
- 提交失败时会解析 ComfyUI 400 响应，返回真实错误原因（缺节点/模型不存在等）。

## 📄 License

[MIT](LICENSE) © 2026 长门九曜
