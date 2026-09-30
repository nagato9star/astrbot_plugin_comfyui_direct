# Anima 提示词写法

适用：生图家族为 `anima`，特别是配置 `prompt_style=danbooru` 的工作流。家族配置和具体工作流中的提示词格式优先。

## 写作方法

- Anima 面向动漫、角色设计、插画和其他非写实艺术图。不要默认把它引导成照片写实风格。
- 优先使用简洁的 Danbooru 风格标签。可按以下语义顺序组织：质量/元信息/年份/安全标签，人物数量与类别，角色名，作品名，画师，普通外观与场景标签。官方模型卡说明各组内部顺序可以调整。
- 先写主体数量、角色和作品，再写动作、表情、发型、服饰、道具、背景、构图与光线。只挑能表达用户要求的标签；训练使用随机标签 dropout，无需把每个可推断细节都塞进提示词。
- 画师标签必须以 `@` 开头。用户点名画师时，优先通过 `comfyui_lookup(type="artist")` 查规范名称；查不到时写可观察到的画面风格，不要编造 artist tag。
- 质量与元信息标签按需要选择。模型卡列出人类质量标签（如 `best quality`）和审美分数标签（如 `score_8`）；它们可以单独使用、组合使用或省略。不要无理由堆叠整串质量标签。
- 描述文字、排版、复杂场景或非动漫插画时，可补充一段简洁自然语言。官方模型卡还提到 `ye-pop`、`deviantart` 数据集标签用于相应的非动漫训练 caption；只有明确采用这种 caption 模式时才放在提示词第一行。
- Anima 的文字渲染能力有限。用户要求可读文字时保留原文并缩短内容；避免承诺长段文字会准确呈现。

## 示例结构

`best quality, year 2025, safe, 1girl, <角色名>, <作品名>, @<已核实画师>, short dark hair, amber eyes, sailor uniform, looking over shoulder, seaside at sunset, medium shot, warm rim light`

示例中的占位内容要由用户要求或工具查询结果替换。缺少角色、作品或画师信息时省略对应标签。

## 来源与适用范围

- 官方 Anima 模型卡：[circlestone-labs/Anima](https://huggingface.co/circlestone-labs/Anima)，Prompting 与 Tag order、质量标签、画师标签、标签 dropout 和非动漫数据集部分。
- 模型卡描述的是它所对应的 Anima checkpoint。若已安装权重或工作流带有更具体的提示词约定，以本地工作流配置为准。
