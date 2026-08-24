# ComfyUI Direct (astrbot_plugin_comfyui_direct)

AstrBot 插件：局域网直连 ComfyUI API，提供图片生成、模型查询与提示词优化。

## 功能

- **图片生成** (`comfyui_generate`)：通过 LAN ComfyUI 生成图片，图片直接发送到当前会话
- **模型查询** (`comfyui_list_models`)：查询 UNET 底模 / LoRA / CLIP / VAE / Embedding 清单，自动同步并本地缓存，ComfyUI 离线时回退缓存
- **队列与 GPU** (`comfyui_queue`)：查询运行中/待执行任务数、ComfyUI 版本、内存与显存占用
- **中断生成** (`comfyui_interrupt`)：中断正在执行的任务（默认中断最近一次由插件提交的），可同时从待执行队列移除
- **booru 查询** (`comfyui_booru`)：danbooru（默认）/ gelbooru 查画师/角色触发词、别名与常用 tag（作品高频统计），避免凭记忆编 tag
- **模型元数据** (`comfyui_model_info`)：查模型/LoRA 的标题、作者、标签与触发词——local 读本机已装 safetensors 头部（`/view_metadata`），civitai 按名称搜官方 `trainedWords`
- **civitai 查配方** (`comfyui_civitai_search`)：在 civitai.red（官方 v1 API 兼容镜像）搜参考图，返回完整生成配方（模型/prompt/负向/sampler/steps/cfg/seed），可直接转成 generate 参数照着出图
- **工作流模板** `anima-v3`（自用 Anima 正式版）：提示词分为 画师串 / 质量 / 主提示词 / lora触发词 四段（JoinStringMulti 拼接）+ 反向提示词。**模板不随插件包分发**，存插件数据目录 `data/plugin_data/astrbot_plugin_comfyui_direct/workflows/`
- 工具支持可选参数：模型、LoRA（Power Lora Loader 插槽 / LoRA 链）、KSampler 参数（steps / cfg / sampler_name / scheduler / denoise）、尺寸、seed
- 提交失败时解析 ComfyUI 400 响应，返回真实错误原因（缺节点/模型不存在等）

## 安装

1. 将本目录放入 AstrBot 的 `data/plugins/` 下
2. 在 AstrBot WebUI 重启插件或重载插件列表
3. 在插件配置中确认 ComfyUI 地址（默认 `127.0.0.1:8188`，本机）

依赖：`httpx`（见 `requirements.txt`，AstrBot 自带）。

## 配置（`_conf_schema.json`）

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `comfyui_host` | `127.0.0.1` | ComfyUI 主机 IP（自填） |
| `comfyui_port` | `8188` | ComfyUI 端口 |
| `comfyui_timeout` | `300` | 生成等待超时（秒） |
| `model_cache_ttl` | `600` | 模型清单缓存刷新间隔（秒），0 表示每次强制同步 |
| `default_workflow` | `anima-v3` | 默认工作流模板名 |
| `danbooru_base_url` | `https://danbooru.donmai.us` | danbooru 接口地址（国内可换镜像）；API 文档见配置提示或 Workflow Studio 侧栏链接 |
| `gelbooru_base_url` | `https://gelbooru.com` | gelbooru DAPI 地址（镜像可换） |
| `civitai_api_key` | 空 | civitai API Key（可选，以你的身份调用）：`https://civitai.com/user/account` 获取（镜像站路径相同） |
| `default_artist` / `default_quality` / `default_trigger_words` / `default_negative_prompt` | 空 | 默认画师串/质量词/lora触发词/负向提示词，留空用模板原值 |
| `default_model` | 空 | 默认底模文件名（可用 `comfyui_list_models` 查看），留空用模板原值 |
| `default_lora` | `[]` | 默认 LoRA **列表**（`template_list`：可添加多条，每条含文件名下拉 + 权重），按顺序映射 Power Lora Loader 插槽；留空用模板原值 |
| `default_steps` / `default_cfg` / `default_denoise` | 0 | 默认采样参数，0=用模板原值 |
| `default_sampler_name` / `default_scheduler` | 空 | 默认采样器/调度器（下拉选择），留空=用模板原值 |
| `default_width` / `default_height` | 0 | 默认图片尺寸，0=用模板原值 |
| `prompt_optimize_enabled` | `true` | 启用自然语言优化：中文描述扩展成 Danbooru tags；关闭后接近原样提交 |
| `prompt_builder_max_tokens` | `1000` | 优化模型输出上限 |
| `prompt_builder_provider_id` | 空 | 指定优化用模型 provider id；留空使用当前会话主模型 |
| `prompt_builder_max_content_tags` | `65` | 内容段 tag 数量上限（不计质量词/画师组），0 不裁剪 |
| `prompt_builder_web_search_enabled` | `true` | 指令含“联网/搜索/官方图/设定图”等词时触发 Tavily 联网搜索（需在 AstrBot 配置 Tavily Key） |
| `prompt_builder_search_max_results` / `prompt_builder_search_depth` | `5` / `advanced` | 搜索结果数量与深度 |
| `prompt_builder_search_query_template` | `{prompt} 角色 外观 …` | 搜索词模板，`{prompt}` 代表用户原始需求 |
| `prompt_builder_deep_thinking_enabled` | `true` | 指令含“深度思考”时启用推理（provider 不支持自动降级） |
| `prompt_builder_reasoning_effort` | `high` | 深度思考强度（high / max） |
| `prompt_builder_template` | 空 | 自定义优化模板；支持 `{theme}` `{character_rule}` `{search_block}` `{outfit_transfer_rule}` `{reference_rule}` `{img2img_rule}` `{sensual_rule}` 占位符，可粘贴 anima_master 模板；留空用内置模板 |
| `danbooru_core_tag_lookup_enabled` | `true` | 角色 tag 联网校正（Donmai 不可用时回退 Safebooru） |
| `danbooru_tag_base_urls` | `https://safebooru.donmai.us,https://danbooru.donmai.us` | Donmai 查询地址，逗号分隔 |
| `danbooru_tag_user_agent` | `AstrBotComfyUIDirect/2.1` | Donmai 访问 UA |
| `danbooru_tag_lookup_timeout` | `6` | 单次角色 tag 查询超时（秒） |
| `danbooru_tag_max_candidates` | `6` | 候选 tag 上限 |

**参数优先级**：LLM 传参 > 插件配置默认值 > 模板原值。LLM 只需填 `prompt`，其余配置好默认后模型"懒"也出图正确。

## 工作流模板

- 模板源：插件数据目录 `data/plugin_data/astrbot_plugin_comfyui_direct/workflows/`（首次部署需从原环境拷贝，插件包不含模板文件，避免秘制蓝图随仓库分发）。anima-v3 使用 rgthree（Power Lora Loader、Image Comparer）、Comfyroll（CR Prompt Text、JoinStringMulti）与 DanbooruText 自定义节点，需自行安装。
- 兼容旧模板：`nagato-anima`、`anima-v2`，从 AstrBot `data/skills/anima-comfyui/references/` 目录加载（不存在时报错提示）。
- 模板即事实来源：模型 / 步数 / cfg / 采样器默认值都在模板里，代码不写死。

## 数据目录

`data/plugin_data/astrbot_plugin_comfyui_direct/`

- `comfyui_models.json`：模型清单缓存
- `output/`：生成的图片文件

## LLM 工具

| 工具 | 说明 |
| --- | --- |
| `comfyui_list_models` | 查询可用底模 / LoRA / CLIP / VAE / Embedding（可选 `refresh` 强制同步） |
| `comfyui_generate` | 生成图片，可选 model / lora / steps / cfg / sampler_name / scheduler / denoise / width / height / seed / workflow 等 |
| `comfyui_prompt_optimize` | 把自然语言需求优化为 Danbooru tags，内置角色 tag 校正（danbooru 查证 + 漏输出自动插入） |
| `comfyui_interrupt` | 中断生成（`prompt_id` 可选，默认最近一次），可同时取消排队任务 |
| `comfyui_booru` | 查画师/角色触发词与常用 tag（`source`=danbooru/gelbooru，`type`=artist/character） |
| `comfyui_civitai_search` | 搜参考图并返回生成配方（模型/prompt/负向/sampler/steps/cfg/seed） |
| `comfyui_model_info` | 查模型/LoRA 元数据与触发词（`source`=local 本地元数据 / civitai trainedWords） |
| `comfyui_queue` | 查询队列与 GPU 显存状态 |

## WebUI：Workflow Studio

AstrBot Dashboard → 插件页 → **Workflow Studio**（`pages/workflow-editor`），复刻 ComfyUI 风格的节点画布：

- 可视化编辑/管理工作流模板：加载、保存、另存为、删除（自定义副本优先于内置）
- 节点画布：拖拽节点、滚轮缩放、空白平移、拖端口连线、点连线删除、双击标题改名、右键复制/删除
- 节点库：一键添加 CR Prompt Text / JoinStringMulti / KSampler / Power LoRA 等节点
- 模型选择：UNET / LoRA / CLIP / VAE 下拉来自自动同步的清单；采样器/调度器候选下拉
- 连接状态灯 + 模型清单侧栏（含 ComfyUI 版本 / GPU 显存）+ 「▶ 试跑」：直接把当前画布提交给 ComfyUI 生成，结果图 webp 预览内联显示，可一键中断，失败显示 ComfyUI 真实报错

后端接口（`webapi.py`，Quart）：`workflows` / `workflow` / `workflow/save` / `workflow/delete` / `status` / `generate`（POST 提交、GET 轮询）。

自定义模板保存在 `data/plugin_data/astrbot_plugin_comfyui_direct/workflows/`，查找优先级：插件数据目录 → 技能目录。

## 注意事项

- 图片由插件通过 `event.send` 直接发送，不要重复调用 `send_message_to_user`。
- ComfyUI 不可达时生成失败，请确认 ComfyUI 已启动且网络可达。
