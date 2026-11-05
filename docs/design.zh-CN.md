# 设计：OpenH3-IR 为什么这样构建

> 本文是 [design.md](design.md) 的简体中文译本。技术标识、代码示例、规则编号和测量数据保留原样。原文包含实现前的设计设想，以及后来被推翻的判断；译文忠实保留这些内容，不代表它们都是当前实现的行为。当前行为以代码及 [AGENTS.md](../AGENTS.md) 为准。

这是编译器的设计参考：MiniMax H3 的条件编码器实际看到了什么、一个参考标签的成本是多少、各阶段之间的契约，以及校验器执行每条规则的依据。修改规则、提示词或阶段边界之前，应先阅读本文。

每项论断都标明了来源：**[SPEC]** 表示 MiniMax 已发布的指南或随附运行时代码所规定的内容；**[INFER]** 表示从三个公开的 Context-IR 输出中推导，或由两个来源相互印证的结论；**[MINE]** 表示作者作出的判断，可以被推翻。数字来自对运行中的端点进行的实际测量，而非估算。

在相信这里的任何一句话之前，有两点需要知道。本文写于编译器构建**之前**，所以文中说“将会”怎样的地方，代码后来已经作出了决定；文档与代码不一致时，**以代码为准，有问题的是本文档**。实际构建中的发现，包括本文哪些立场是错的，都记录在 [build-log.md](build-log.md) 中，其中五项发现推翻了本文的设计决定。

## 0. 结论

**这一层值得构建，而且收益最大的部分，并不是发布文章宣传的那一部分。** 两项发现决定了设计：

1. **参考标签是提示词文本与附件素材之间唯一的绑定，而且这些标签由运行时输出，不是由提示词输出。** ComfyUI 的 H3 tokenizer 会在每张图片的视觉块之前，直接向 token 流写入 `"<Picture 1>: "`；在每段视频的帧块之前写入 `"<Video 1>: "`；为每段音频写入 `"<Audio 1>: "`（只有标签，见 §4.1）；然后在**所有这些内容之后**追加提示词文本（`comfy/text_encoders/minimax.py:141-186`）。提示词里写 `<Image 1>`，引用的是输入表示中根本不存在的名称。维护者当前使用的提示词恰好就是这样。这不是风格问题，而是一个失效的指针，修复它不需要额外计算成本。

2. **IR 文本的成本几乎可以忽略；真正花钱的是参考素材。** H3 是单流打包式 Transformer：IR token 与视频 latent、音频 latent 和参考素材 latent 拼接到**同一个**序列中，每个采样步骤都会让整个打包序列经过每一个 DiT 块（`comfy/ldm/minimax/model.py:513-644`）。实测：与公开示例长度相当的 IR 只占打包序列的 **0.5–1.6%**；而当 `ref_image_size=max` 时，一张参考图片就占 **7,296 行**，一段 5 秒参考视频占 **37,296 行——与生成视频本身一样多**。因此，正确做法与节省提示词恰恰相反：*IR 要写得充分、精确；附件数量和分辨率则要严格控制。*

我**没有**声称：像托管服务那样写 700 个单词，一定优于克制而严谨的 400 词 IR。这尚未得到证明，我在 §10 的 E2 中给出了可证伪的测试。我愿意维护的主张是：**结构**——有实际素材支撑的标签、明确的主体定义、显式的保留契约和合法时间线——才是已经有规范、成本低廉、而当前流程缺失的部分。

需要纠正需求说明中的一个表述，这对构建很重要：MiniMax 的发布说明称，*“我们在 tokenizer 配置中增加了若干特殊 token，例如 `<d>`。”* **实际发布的 tokenizer 并没有这样做。** H3 自身的 `Ref2VA/tokenizer/tokenizer_config.json` 和 `tokenizer.json`，所包含的新增 token 与原版 Qwen2.5 完全相同，都是 26 个，没有 `<d>`、`</d>`、`<scenetrans>`、`<cutoff>`；而且 H3 的 `vocab.json` 与 ComfyUI 已经捆绑的版本逐字节一致（git blob sha1 `4783fe10ac3adce15ac8f358ef5462739852c569` 与 HF etag 匹配；FL2VA 和 Ref2VA 的 tokenizer 配置也是同一个 blob，etag 为 `204d76f7…`）。因此，`<d>` 是**字面文本**，会被 BPE 拆成 `['<d', '>']`，ComfyUI 的分词方式已经是忠实的。对设计的影响是：标记必须逐字节精确——`<D>`、`< d >` 或排版用括号都会产生不同的 token 序列，丢失模型赋予 `<d>` 的含义。不需要编写 tokenizer 补丁。我专门查找过这个问题，它并不存在。

**关于扩展后的范围**（用户不需要看到模式，且 API 可由不熟悉领域的 agent 驱动）：这些不是额外拼接到这一层上的功能，而是这一层自然推导出的结果。模式选择由素材接线和素材角色决定，而这两者本来就归这一层负责，因此模式选择应该放在编译器前端（§12）。默认选择 Ref2VA 是因为它比 FL2VA **严格更具表达能力**，而不是因为我偏爱它。两种调用者也无需分别处理，因为设计已经禁止调用者提供结构：标签、编号、时间线和章节顺序都归编译器负责，所以 agent 的一句话需求与 UI 中完整指定的需求，会从同一位置进入同一流水线（§13）。让这两件事都成立的不变量是：**不存在只有 UI 才能设置、且影响质量的字段，也不存在绕过校验器的路径。**

**关于风格 LoRA**（§14）：触发词只是 IR 中的文本，因此同样遵循 §1 确立的纪律——字节和位置必须正确，否则即使付出全部计算成本，它也会悄无声息地不起作用。它应该进入规范为各模式预留的风格位置；在基础模式中，则插入已有的逗号分隔风格列表，不改变格式。有一点需要调整：agent 应当能够*发现*触发词，但绝不应*负责*输入触发词，正如它不负责输入 `<Picture 1>`。agent 用 id 选择 LoRA，编译器负责字符串。

**关于推理主机：** 已经通过在线验证，结果推翻了“只能处理文本”的假设——模型具有可工作的视觉塔（§5），所以一台主机上的一个模型就能完成分析、规划和文字撰写，无需混合拆分，也没有跨模型漂移。我还实测发现了两种静默失败模式（F15、F16），而不是凭空猜测；这改变了构建顺序：在任何阶段依赖后端之前，必须先加固后端封装。

---

## 1. 编码器实际看到什么（已经验证，不是猜测）

`comfy/text_encoders/minimax.py` 手工构建 token 流。**没有聊天模板，没有特殊 token，`add_special_tokens=False`。** 输入表示如下：

```
t2va    : <prompt>
fl2va   : "<Picture 1>: " <vision> [ "<Picture 2>: " <vision> ] <prompt>
ref2va  : for each attached item, in the runtime's order:
            image -> "<Picture i>: " <vision block>
            audio -> "<Audio j>: "                      (no content enters Qwen)
            video -> "<Video k>: " then, per 2-frame block, "<T.T seconds>" <vision block>
          then <prompt>
```

条件表示是 Qwen3-VL-32B **第 50 层之后、未经归一化的隐藏状态**；磁盘上的 checkpoint 截止于此（`model.embed_tokens.weight [151936, 5120]`、第 0–49 层，没有 `lm_head`，也没有最终归一化层——通过读取 safetensors 头部验证）。文本位置的 adaLN 标记为 1，vision-pad 位置的标记为 0。

由此产生的三个结论，决定了下文所有设计。

### 1.1 两类标签：有素材锚定与无素材锚定 **[SPEC，来自运行时]**

| 标签 | 是否有素材锚定？ | 如何获得含义 |
|---|---|---|
| `<Picture N>` | **有** | 运行时在图片的视觉块之前立即输出这个字符串 |
| `<Video N>` | **有** | 同理，在视频帧块之前输出 |
| `<Audio N>` | **仅有名称** | 运行时输出标签，但音频内容从不进入 Qwen（§4.1） |
| `<Subject N>` | **无** | *仅仅*因为 `subject_definitions` 在文本中定义了它，才存在 |
| `(SN)`、`[Shot N]` | **无** | 纯文本约定 |

正是这个机制，让 Ref2VA 的六章节格式真正发挥作用，而不只是装饰：`subject_definitions` 用一句话把抽象、可复用的身份绑定到一个或多个有素材锚定的标签上；`retention_analysis` 则用一句话说明哪些内容必须保留。流水线中没有其他地方表达这两层含义。

### 1.2 编号归运行时所有，因此不能脱离素材接线单独编写 IR **[SPEC，来自运行时]**

`MiniMaxH3ReferenceToVideo.execute` 按固定顺序输出（`comfy_extras/nodes_minimax_h3.py:210-280`）：**所有参考图片**，然后**对每段参考视频：先输出其配对音轨的 `<Audio j>` 标签，再输出 `<Video k>`**，最后输出**独立音频**。每种类型都按输出顺序从 1 开始编号。

公开的托管版 Ref2VA IR **完全按照这个方式**编号——`<Audio 1>` 是 `<Video 1>` 的同步音轨，`<Audio 2>` 是独立的人声参考。**[INFER，强证据：两个独立来源相互印证]** 托管 Context-IR 与本地 ComfyUI 节点采用相同编号约定，这意味着按本地输出顺序编写的 IR 也兼容托管版本。

因此：**编译器输出的不是一个字符串，而是一份 IR 文档，其中同时包含提示词和有序的“素材→标签→接线”清单**，因为标签只有相对于某一套接线才是正确的。两者必须一起交付、一起计算哈希、相互校验。仅这一项决定，就能从结构上防止 `<Image 1>` 这一类错误——悬空标签会变成构建错误，而不是静默的质量损失。

### 1.3 IR 长度在语义上并非中性 **[SPEC，来自运行时]**

在 `PackedLayout.__init__` 中，文本占据时间 RoPE 位置 `0 … text_len-1`，随后设置 `cursor = float(text_len)`——参考块、关键帧条件行和目标视频的位置原点，都在**文本之后**。改变 IR 长度，就会平移每个条件块的位置坐标系。

实际影响：**不能对两份长度不同的 IR 做 A/B 测试，然后把差异归因于措辞。** 长度像随机种子一样，是混杂因素。§10 的评测框架在测试措辞时会把 token 数控制在一个区间内，并把长度作为独立维度测试。

### 1.4 MiniMax 的另外八个技能：明确的阴性结果，反而支持构建这一层

我检查了全部八个风格技能（不含中文对应版本，约 3,000 行），寻找两份指南没有明说的格式知识。**结果是没有。** 在这些文件中搜索 `integrated_multimodal_description`、`overall_soundscape`、`non_diegetic_music`、`subject_definitions`、`retention_analysis`、`<d>`、`<Picture`、`<Subject`、`(S1)`、`[Shot `，在 **`h3-prompt-writing` 本身之外，命中数为零**。它们是 Hub 编排工作流——镜头表、色板系统、质检清单、审批关卡。

唯一看起来可能有帮助的文件，`co-op-game-intro-generator/references/h3-video-prompt-template.md`，是一个**使用 `[0秒–2秒]` 节拍分段的中文时间线模板**，带有固定的 UI 事件框架和末尾的负面约束列表。它完全不是 H3 IR 格式：没有 `[Shot N] At MM:SS.mmm`，没有参考标签，也没有章节名称。

这不是令人失望，而是提供了信息。**MiniMax 自己的生产技能输出的是松散、混合语言的创作文字，再依赖 Context-IR 把它规范化为 IR。** 它们的内部写作风格*就是*流水线在 Context-IR 之前的那一侧。这是来自 MiniMax 实际发布产物的直接证据，说明了闭源阶段究竟做什么——松散需求进，严格 IR 出——也说明了为什么跳过这一层的本地流水线，会给 H3-Base 输入模型训练时没有见过的形式。这也意味着实现者不必再花时间挖掘那八个技能；本节就是调查结果。

---

## 2. 成本模型（实测）

在 24 fps、合法帧网格（`n % 17 == 5`）下的打包序列行数：视频行数 = `latent_t × (W/32) × (H/32)`，音频行数 = `round(frames/24 × 40) × 2`：

| 时长 | 帧数 | 实际秒数 | 视频行数 | 音频行数 | 加入 600 token IR 后的总行数 | IR 占比 |
|---|---|---|---|---|---|---|
| 5 s | 124 | 5.167 | 37,296 | 414 | 38,310 | **1.57%** |
| 10 s | 243 | 10.125 | 72,576 | 810 | 73,986 | **0.81%** |
| 15 s | 362 | 15.083 | 107,856 | 1,206 | 109,662 | **0.55%** |

（1344×768；9:16 和 1:1 的结果与这些数值相差不超过 3%。）

参考素材的成本，**会加到每一个采样步骤中**：

| 素材 | 行数 |
|---|---|
| 参考图片，`ref_image_size=match`（缩放到生成画面的像素面积） | **1,008** |
| 参考图片，`max`（短边 2048 → 3648×2048） | **7,296** |
| 9 张 `max` 参考图片 | **65,664**（整段 5 秒视频的 1.8 倍） |
| 2 秒参考视频，1344×768 | **17,136** |
| 5 秒参考视频 | **37,296** |
| 15 秒参考视频 | **107,856** |
| 关键帧（fl2va 首帧／尾帧） | 每张 1,008 |

使用与 H3 完全相同的 tokenizer 测得的 IR 大小（`scratchpad/h3tok.py`，基于 ComfyUI 的 `qwen25_tokenizer` 词表和 merges 构建）：

| 产物 | 总单词数 | 主描述字段 | token 数 |
|---|---|---|---|
| 公开的 T2VA IR | 380 | 249（2 个镜头，10 秒） | **537** |
| 公开的 I2VA IR | 726 | 533（1 个静态镜头，8 秒） | **919** |
| 公开的 Ref2VA IR | 562 | 238（1 个镜头，5 秒，编辑任务） | **803** |
| `base-en.txt`（基础指南） | 2,499 | **3,582** |
| `ref-en.txt`（Ref2VA 指南） | 3,676 | **5,277** |

**如何理解公开的用量数字。** T2VA 报告 `prompt_tokens 5,650`。基础指南为 3,582 token，其 SKILL.md 为 493 token——因此，扣除约 1.5 K 的调用框架和需求文本，托管 T2VA 调用基本就是**把公开指南作为系统提示词**。**[INFER]** 但 `completion_tokens 2,915` 对应的实际产物只有 537 token（约 5.4 倍）；I2VA 的 `10,022` 对应 919 token（约 11 倍）。这意味着 completion 计数**不是**输出 IR 的长度，而是包含内部推理的多阶段工作流汇总用量。Ref2VA 的 `33,323` 个 prompt token 同样超过两份指南之和（8,859），再加上对一段 5 秒视频进行任何合理的单次编码所需的 token。**不要把托管服务的数字当成本地单次调用的预算。** 另外，需求说明中“约 100 K 蒸馏成约 4 K”的数字，与三个公开产物（537–919 token）都不吻合；我没有找到它的来源，因此设计不依赖它。

**撰写成本——在推理主机上实测**：`LLM_HOST:8000`，`Huihui-Qwen3.6-27B-abliterated-AWQ-MTP`，vLLM 0.21.0，2×3090。以下数据均来自关闭 thinking 的实际调用：

| 项目 | 实测 |
|---|---|
| 生成吞吐量 | **80–107 tok/s** |
| 用 `ref-en.txt` 作系统提示词 | 5,456 个 prompt token，**冷 prefill 3.25 秒**，热启动约 1.8 秒（前缀缓存已启用） |
| 1344×768 参考图片 | **1,024 个 prompt token**，0.77 秒 |
| 768×768 / 1536×1536 参考图片 | 592 / 2,320 token |
| 550 词的文字撰写轮次（输出 718 token） | **9.0 秒** |
| schema 约束下的 AssetCard（输出 94 token） | **1.0 秒** |
| 3 张图片加跨图片推理 | 388 个 prompt token，0.6 秒 |

因此，带三份参考素材的完整 Ref2VA IR 大约需要 **25–40 秒**：每份素材卡约 7 秒，指南 prefill 约 3 秒（首次调用后有缓存），文字撰写约 11 秒。相比以分钟计的 H3 采样，**IR 撰写不是瓶颈，也不必刻意压缩成本。** 九份参考素材，每份约 1 K token，总计约 9 K——相对于端点 262 K 的上下文容量，几乎不值一提。

---

## 3. 契约：`IRDocument` v1

编译器输出一份 JSON 文档。`prompt` 是其他内容的**纯确定性函数**；重新渲染同一份文档，必须产生逐字节相同的字符串（校验器会断言这一点）。

```jsonc
{
  "ir_version": "1",
  "profile": "h3ir/2026-08-a",              // versioned policy bundle, see §8
  "mode": "t2va|i2va|fl2va|l2va|ref2va",

  "target": {
    "requested_seconds": 10,
    "frames": 243,                          // snapped up to n % 17 == 5
    "effective_seconds": 10.125,            // frames/24 — the ONLY source of S.SS
    "canvas": [1344, 768],
    "fps": 24
  },

  // ORDERED exactly as the runtime emits. Slot order IS the label numbering.
  // `provenance` is present when the app generated the asset (§14.8): a PRIOR for the
  // analyser, never a substitute for describing the actual pixels.
  "manifest": [
    {"slot":0,"label":"<Picture 1>","kind":"image","sha256":"…","wiring":"ref_image_1",
     "px":[1824,1024],"sizing":"max","rows":1824,
     "composition":"bare_plate|composed_scene|unknown",
     "provenance":{"generator":"qwen-image-edit","model":"…","source_prompt":"…",
                   "parent_asset_sha256":"…","edit_instruction":"…","intended_role":"subject"}},
    {"slot":1,"label":"<Audio 1>","kind":"audio","sha256":"…","wiring":"ref_video_audio_1",
     "paired_with":"<Video 1>","seconds":5.0},
    {"slot":2,"label":"<Video 1>","kind":"video","sha256":"…","wiring":"ref_video_1",
     "frames":124,"seconds":5.167,"rows":37296}
  ],

  "subjects": [
    {"label":"<Subject 1>","kind":"person|environment|object|style|action",
     "from":["<Picture 1>"],                // grounded labels this subject is drawn from
     "attributes":["short wavy blonde hair","bright pink suit jacket", …],
     "retention":"fully_preserved|partially_preserved|attribute_transfer|weak_reference",
     "retention_note":"…","appears_in":[1,2]}
  ],

  "speakers": [
    {"id":"(S1)","subject":"<Subject 1>","onscreen":true,"voice_ref":"<Audio 2>",
     "voice":"calm male, measured delivery","first_event":{"shot":1,"order":0}}
  ],

  "shots": [
    {"n":1,"cut_ms":null,                   // shot 1 has no timestamp  [SPEC]
     "framing":"…","camera":{"type":"Push In","amplitude":"small","speed":"slow"},
     "body":"…prose…",
     "dialogue":[{"speaker":"(S1)","language":"English","text":"Follow the wind, live free.",
                  "voiceover":false,"crosses_cut":false,"truncated":false}],
     "onscreen_text":["営業中"]}
  ],

  "audio": {
    "soundscape":"…", "non_diegetic_music":"…|N/A",
    "audio_relations":[{"label":"<Audio 1>","marker":"fully_copy|partially_copy|reference|weak_reference",
                        "note":"…","layer":"soundscape|music|dialogue"}]
  },

  "task_types": ["video editing","audio reuse"],   // ref2va only, enum in §6 R30

  "prompt": "…the exact string handed to the node…",
  "prompt_tokens": 803,
  "hashes": {"request":"…","cards":"…","plan":"…","prompt":"…","render":"…"},

  // §14: resolved before planning; the compiler owns the trigger strings, not the caller
  "loras": [
    {"id":"handpainted-anim-v2","version":2,"file_sha256":"9f2c…",
     "strength_requested":0.9,"strength_applied":0.9,
     "triggers_injected":[{"text":"hndpntd_anim_v2","slot":"style","count":1}],
     "registry_revision":"reg-2026-08-10T14:22Z"}
  ],

  "mode_decision": {"mode":"ref2va","confidence":0.91,"rule_fired":"12.2#1",
                    "signals":["two subjects named from two images"],
                    "alternatives":["fl2va"],"asked":false},

  "provenance": {"analyzer_model":"…","analyzer_version":"…","prose_model":"…","seed":12345,
                 "card_ids":["…"]},
  "diagnostics": [{"rule":"R41","severity":"warn","message":"…"}]
}
```

### 3.1 渲染规则——按模式区分，逐字节精确 **[SPEC]**

两类模式的渲染方式不同，指南和公开产物都证实了这一点。弄错会造成静默的格式错误。

**基础模式（t2va / i2va / fl2va / l2va）**——字段名与内容在*同一行*：

```
integrated_multimodal_description: [Shot 1] …

overall_soundscape: …

non_diegetic_music: …
```

**Ref2VA**——六个章节，名称*独占一行*，内容随后：

```
subject_definitions:
<Subject 1> is …

summary:
[video editing + audio reuse] …

retention_analysis:
<Subject 1> (appears in [Shot 1]): fully_preserved - …

detailed_description:
The target video is in realistic photographic style.
[Shot 1] …

overall_soundscape:
…

non_diegetic_music:
…
```

指令行必须**原样复现，包括长破折号（U+2014）和各模式的括号约定**——规范中的约定并不一致，我保留这种不一致，而不是替它整理：

| 模式 | 第一行 |
|---|---|
| t2va | *（没有指令行，直接从第一个字段开始）* |
| i2va | `For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.` |
| fl2va | `How the reference pictures align with the target video — Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; Picture 2 (from Shot N) aligns with the S.SS-second mark of the target video.` |
| l2va | `How the reference pictures align with the target video — <Picture 1> (from [Shot N]) aligns with the S.SS-second mark of the target video.` |

注意，fl2va 使用**不带括号**的 `Picture 1` / `Shot 1`；i2va 和 l2va 使用**带括号**的形式。其后恰好跟一个空行 **[SPEC]**。

`S.SS` = 请求的**标称**秒数，保留两位小数（`10.00`），*不是*对齐后的 `effective_seconds` **[MINE，核查后推翻了先前判断]**。理由是：托管 API 接收整数时长，因此 H3-Base 的训练分布包含整数 `S.SS` 值；而且指南自己的 L2VA 示例使用 `6.00`，它**根本不在本地 `n % 17 == 5` 网格上**（相邻合法值是 5.875 和 6.583）。写 `10.13` 虽然准确描述了本地渲染时长，却偏离模型的训练分布。因此，指令行遵循标称时长；但**镜头切点仍以 `effective_seconds` 校验**（R23），因为渲染根本到不了的切点是另一种真实错误。两者是有意解耦的。`s_ss_policy` 是 profile 中的字段，E3 会测量哪种策略的尾帧落点更准确。

字段间隔：一个空行 **[MINE]**。指南使用空行，公开的 I2VA 产物使用 `\n\n`，但公开的 T2VA 产物只使用单个 `\n`。两者都在 H3 的训练分布中；空行是更常见的形式。

风格开头的位置 **[SPEC，`ref-en.txt` §5.2]**：基础模式把风格放在 `[Shot 1]` *内部*；Ref2VA 则把一到两句风格描述独立成行，放在 `[Shot 1]` *之前*。

### 3.2 合法时长网格 **[SPEC，来自运行时]**

`align_frame_count` 向上对齐到 `n % 17 == 5`。在节点注明的训练范围约 124–362 帧内，恰好只有 **15 个合法时长**：

```
124=5.167  141=5.875  158=6.583  175=7.292  192=8.000  209=8.708  226=9.417  243=10.125
260=10.833 277=11.542 294=12.250 311=12.958 328=13.667 345=14.375 362=15.083
```

**192 帧是训练范围内唯一的整数秒时长（8.000 秒）**——这也是指南中的 FL2VA 示例使用八秒视频的原因。“10 秒”的请求实际渲染为 10.125 秒，所以**每个镜头切点都必须 `< 10.125`**。指南自己的 L2VA 示例（`6.00`，位于合法值 5.875 和 6.583 之间）表明，托管阶段*并不会*写出与帧网格精确对应的时长。因此有了 §3.1 的区分：切点按实际帧网格校验，指令行的 `S.SS` 则遵循标称请求。

---

## 4. 阶段架构

五个阶段。最关键的决定是：**结构由编译产生，文字由模型生成。** LLM 绝不决定标签编号、说话人 ID、时间戳、保留标记、任务类型前缀或章节顺序——这些都通过计算得到。LLM 只在形状已经固定的槽位中撰写文字正文。正是这点，让这一层可以测试、缓存和返工。

```
A   Intake      brief + assets + wiring   ->  Request      (deterministic)
A'  Mode        Request + AssetCards      ->  mode_decision (§12; deterministic where the
                                                             wiring decides, one call otherwise)
A'' LoRA        proposed ids + registry   ->  injection plan (§14.4; deterministic)
B   Analyse     each asset                ->  AssetCard    (VLM / whisper, cached per asset)
C   Plan        Request + cards + plans   ->  Plan         (deterministic: labels, speakers,
                                                            timeline, markers, task types)
D   Render      Plan                      ->  prose + prompt (LLM writes bodies; template emits)
E   Validate    IRDocument                ->  pass | repair | fail   (§6)
```

顺序上有一点需要注意：**模式推断需要 AssetCard**（判断是帧锚点还是内容参考，需要知道图片实际是什么），所以在 1–2 张图片的情况下，B 要在 A′ 完成之前运行。清晰的表述方式是：B 是纯粹的逐素材缓存查询，没有依赖，因此总可以先运行；A′ 和 A″ 随后使用其结果。给定这三者的结果，C 之后的所有处理都是确定性的。

**A——输入接收（Intake）。** 规范化以下内容：根据接线而不是用户选择确定的模式、从 `requested_seconds` 到帧网格的映射、画布、每份素材的角色（`frame_anchor | subject | environment | style | storyboard | edit_source | continuation_source | voice_timbre | bgm | music_style | beat_reference | sfx | transcript_source`）、带显式语言标签的台词、画面文字字符串、硬约束。角色是*推导*任务类型与保留标记的输入，因此必须显式提供，不能事后从文字中推断。

**B——分析（成本高、可复用的阶段）。** 每份素材一张 `AssetCard`，按内容哈希缓存。托管阶段看起来神奇的能力，实际上就在这里。它的冗长并非内部写作风格，而是对应于*需要清点的参考素材有多少*：无参考素材的两镜头 T2VA 是 249 词；单镜头 Ref2VA 编辑任务是 238 词；但**单个静态 I2VA 镜头却有 533 词**，因为它有一张需要逐项描述的图片——碗的花纹、汤的颜色、两片带螺旋状油花的叉烧、右侧的海苔、七位家庭成员各自的服装。那个镜头里，除了蒸汽和一次焦点转移，什么都没动。这是把素材清单转写成文字，不是创意写作，而这一部分本地层可以准确复现。**[INFER]**

各类素材卡的内容：

- *图片*：主体（身份特征、服装、配饰）、环境、带位置的道具、光线与颜色、构图／镜头、风格、所有可读文字的原文、明显缺陷。
- *视频*：代表帧的图片卡，加上镜头结构、切点、每个镜头的运镜、主体动作，以及帧网格上的时长。
- *音频*：**带语言识别的转录（whisper）、音色描述、速度／配器、画内声与配乐的判断。** 不可省略——见下文 §4.1。

**§4.1——音频盲区（很容易漏掉）。** tokenizer 输出 `"<Audio j>: "`，**除此之外什么也没有**；音频内容根本不进入 Qwen（`minimax.py:161-163`——音频分支只添加文本标签，没有视觉条目）。音频 latent 会单独送入 DiT。因此，**对于音频参考，IR 文本是条件编码器了解该音频的唯一渠道。** 图片既被描述，*也被看见*；音频则只有描述。因此，`subject_definitions` / `retention_analysis` / `overall_soundscape` 中的音频行，承担着图片行所不承担的关键作用；本地 whisper 处理是这一层的硬依赖，而非锦上添花。这台机器已经通过 toolbox MCP 和本地 whisper.cpp 构建具备 whisper。

**C——规划（完全确定性）。** 按以下顺序计算：

1. 根据接线生成 `manifest`，使用运行时的输出顺序（§1.2）。标签只在这里分配，不在任何其他位置分配。
2. `subjects`——每个可复用的可见单元对应一个主体，并指向有素材锚定的标签。规范规则：如果图片*仅*用于定义角色／场景／风格，则在 `subject_definitions` 中**不应**有独立的 `<Picture N>` 条目，而应在主体定义内部引用。独立的 `<Picture N>` 条目意味着该图片是具体帧或构图锚点 **[SPEC，`ref-en.txt` §2.2]**。
3. `speakers`——按*目标*视频中首次实际发声事件的顺序分配 `(SN)` **[SPEC，`ref-en.txt` §5.4]**。
4. 时间线——镜头切点严格递增，全部 `< effective_seconds`，格式为 `MM:SS.mmm`，镜头 1 不带时间戳。
5. 根据角色和计划是否修改该主体／标签，确定各主体／标签的保留标记。
6. 从素材角色推导任务类型，用 `+` 连接、去重，并按规范列出的顺序排列。

**D——渲染（Render）。** LLM 接收 Plan 和官方指南中的*相关片段*，只撰写：镜头正文、`overall_soundscape`、`non_diegetic_music`、主体定义中属性的措辞、保留说明。所有结构由模板输出。台词文本采用**粘贴，而非生成**——规范要求逐字保留用户提供的文字和标点，因此它根本不应经过模型。

**E——校验与修复。** 见 §6。出现 ERROR 时，针对具体失败片段重新请求模型（有界，最多尝试 2 次），重新校验，然后明确报错。**绝不把无效 IR 交给采样器**——这就是编译器存在的意义。

---

## 5. 模型与后端——推理主机兼任两项工作（已验证）

**推理主机就是大脑，需求说明中预想的混合拆分事实证明并不必要。** 我被要求验证“只能处理文本”的假设，而不是照单全收。**这个假设是错的：`Huihui-Qwen3.6-27B-abliterated-AWQ-MTP` 有可工作的视觉塔。** 实际调用的证据如下：

- 两张四象限色块布局图都按完整顺序正确读取——每张随机猜中的概率为 1/24，两张全对为 1/576。
- 随后，在**同一个请求中放入三张不同图片，12/12 个象限颜色都正确，且正确归属于各自图片**（388 个 prompt token，0.6 秒）。这是跨图片推理，而不仅仅是图像描述——恰好就是 C 阶段把主体分配到 `<Picture N>` 槽位所需要的能力。
- 图片 token 成本的变化符合 patch 16 / merge 2 的 Qwen 式 ViT：1344×768 → 1,024 token，约等于 `(1344/32)×(768/32) = 1,008`，再加视觉分隔符。这是真正的视觉编码器，而不是旁路图像描述。

因此，**一台主机上的一个模型就能覆盖分析、规划和文字撰写。** 看素材的阶段与描写素材的阶段之间，没有跨模型漂移；渲染机器的显存也能留给采样。相比我原本准备设计的混合架构，这个架构更好；它更好是因为测量结果，而不是偏好。

**这一层依赖的端点契约**（均已验证）：OpenAI 兼容的 `http://LLM_HOST:8000/v1`，`max_model_len` 为 262,144，通过 `image_url` 内容片段输入图片，支持 `response_format: json_schema`、前缀缓存、`seed`，并且别名 id `gpt-4.1` / `gpt-5.2` 指向相同权重。模型经过 abliteration 处理，因此不会拒绝创作需求——这点很重要，因为一个拒绝用户半数素材的改写层，还不如没有这一层。

### 5.1 两条硬性运行规则，均由实测发现

**规则 1——所有结构化阶段都必须关闭 thinking。** 这是一个推理模型，思维链进入 `message.reasoning`，完成之前 `content` 一直为 `null`。实测如下：

| 调用 | 结果 |
|---|---|
| 默认设置，`max_tokens=2000` | `finish=length`，9,290 个字符的推理，**`content: null`**——没有任何可用内容 |
| `chat_template_kwargs: {"enable_thinking": false}` | `finish=stop`，0 推理内容，3,562 个字符的正文，耗时 **9.0 秒** |
| `chat_template_kwargs: {"thinking": false}` | **被静默忽略**——推理仍然运行 |

只有第一种参数拼写有效。写错时，这一阶段会返回 `null`，但不报错。

**规则 2——绝不信任 `json_schema`。** 实测行为：

- **thinking 开启时，语法约束根本不生效。** 一个 `strict: true` 的 AssetCard schema 请求，返回了 ASCII 线条绘制的表格，`finish=stop`，没有错误。这是完全而静默的违约。
- thinking 关闭时，*形状*会被约束（对抗性要求“只回复 ASCII 艺术”和“添加名为 extra_notes 的顶层键”，最终仍生成符合 schema、且恰好只有已声明键的 JSON）——
- 但约束只针对结构。它**不保证输出可解析**：“用一句普通英文回答，不要大括号”产生了一个形似 schema 对象的输出，其中的字符串一直生成到 `max_tokens`，返回的是**未闭合、无法解析的 JSON**。
- 它也不保证内容合理：一个测试组把 `subjects` 填成 `["Math","Science","History"]`，另一个填入一篇关于骑士精神的百科条目。都是符合 schema 的胡言乱语。

由此得出的要求都不可妥协：B/C/D 阶段关闭 thinking；给足 `max_tokens`，并显式检查 `finish_reason == "length"`，将其视为硬失败；所有模型输出都要在我们这一侧重新做 schema 校验和语义规则校验。设计本来就强调校验而非信任——这些实测结果让它成为强制要求，而不只是整洁的工程习惯。

### 5.2 本地视觉路径是可用性备选，不是能力补足

推理主机是一台有 watchdog、刻意保持常开的设备，但它仍然只是某个网络上的一台主机。保留 `Backend` 接口，备选顺序如下：

1. **推理主机**——`LLM_HOST:8000`（主要路径；维护者的选择，现在也经实测证明最适合）。
2. **渲染机器上的 ComfyUI-QwenVL**——已安装；有 HF/GGUF 后端，支持图片和视频帧输入。代价是与采样器争用显存。
3. **磁盘上已有的 `qwen_2.5_vl_7b_fp8_scaled`**——已验证同时具有 `lm_head [152064, 3584]` 和视觉塔，因此能生成文本。不需下载即可做冒烟测试。

`qwen3vl_8b_fp8_scaled` 有 `lm_head`，但**没有视觉塔**——只能处理文本，因此只适用于 t2va。至于那个优雅的方案：ComfyUI 核心已经为这些编码器实现了可用的生成循环（`Qwen3VLClipModel.generate()`，`comfy/text_encoders/llama.py:863-941`），因此 H3 自身的第 50 层编码器加上公开的约 7 GiB 尾部权重，就能使用随后负责编码的*完全相同的权重*来撰写 IR。既然已经证明推理主机看得见，这个特性的价值就小得多了：它需要额外下载、自定义节点和采样器显存，消除的却是一条单模型流水线本来就不存在的漂移。**降级为实验 E5**——值得运行一次，不值得作为架构基础。**[MINE]**

**可用性策略：** 如果无法连接推理主机，这一层必须**明确失败，并给出清晰信息**，而不能静默降级到较弱模型——调用者分不清好 IR 和坏 IR，悄悄把质量减半，是维护者最晚才会察觉的故障模式。降级到备选模型必须通过显式启用的开关，并记录日志。

## 6. 校验器

两种严重级别。**ERROR** 阻止渲染（修复或失败）。**WARN** 记录到 `diagnostics` 并展示。标有 ⚑ 的规则，用于捕获我实际预期会发生的故障。

### 绑定与标签

| id | 级别 | 规则 | 检测方式 |
|---|---|---|---|
| R01 ⚑ | ERROR | 提示词中的每个 `<Picture N>` / `<Video N>` / `<Audio N>` 都必须存在于 `manifest` | 正则提取结果 ∩ manifest |
| R02 ⚑ | ERROR | 不允许四个命名空间之外的标签：拒绝 `<Image N>`、`<Ref N>`、`<Frame N>`、`<Img N>`、`<Clip N>` | 正则 `<\s*(?!Picture\|Video\|Audio\|Subject\|d\|/d\|scenetrans\|cutoff)[A-Za-z]+\s*\d*\s*>` |
| R03 ⚑ | ERROR | 每个 manifest 条目的标签至少在提示词中出现 1 次（不能有白白消耗行数的孤立素材） | manifest − 提取结果 |
| R04 | ERROR | manifest 顺序必须等于运行时输出顺序：图片，随后逐段视频（配对音频标签、视频），最后独立音频 | 根据接线重新计算并比较 |
| R05 | ERROR | 数量不超过上限：≤9 张图片、≤3 段视频、≤3 段音频、总计 ≤12 个文件；音频不能是唯一参考素材 | 统计 manifest |
| R06 | ERROR | 任意位置使用的 `<Subject N>` 都必须在 `subject_definitions` 中定义 | 集合差 |
| R07 | WARN | 每个已定义的 `<Subject N>` 都应在 `detailed_description` 中使用 | 集合差 |
| R08 | ERROR | 主体标签编号必须从 1 开始连续 | 排序 |
| R09 | ERROR | 仅用于定义主体／风格的图片，在 `subject_definitions` 中不得有独立的 `<Picture N>` 行 | 角色与章节交叉检查 |
| R10 | ERROR | 帧锚点声明（`the shot begins from <Picture N>`、`ends on`、`keyframe corresponds to`）只能用于角色为 `frame_anchor` 的素材 | 短语匹配与角色对照 |

### 说话人与台词

| id | 级别 | 规则 | 检测方式 |
|---|---|---|---|
| R11 | ERROR | `(SN)` 按首次发声事件顺序分配，从 1 开始连续 | 扫描 `<d>` 块的顺序 |
| R12 | ERROR | **`retention_analysis` 中不能有 `(SN)`**（`ref-en.txt` §5.4 明确禁止） | 扫描章节 |
| R13 ⚑ | ERROR | `<d>` / `</d>` 必须成对且逐字节精确（只能是 `<d>`、`</d>`，不能用 `<D>`、空格或排版括号） | tokenizer 级扫描 |
| R14 | ERROR | 每个 `<d>` 都以 `[Language] ` 开头，语言来自 H3 的 11 种稳定语言（阿拉伯语、中文、英语、法语、德语、意大利语、日语、韩语、葡萄牙语、俄语、西班牙语），或显式允许的额外语言 | 前缀匹配 |
| R15 ⚑ | ERROR | 台词文本必须是用户提供台词的逐字节精确子串——绝不翻译、改写或重新标点 | 与 Request 精确比较 |
| R16 | ERROR | `<d>` 内不得有 emoji、波浪号、项目符号、重复／装饰性标点；完整陈述以 `.`/`?`/`!` 结尾 | 字符集与结尾检查 |
| R17 | ERROR | 画外音必须使用精确短语 `says in an off-screen voiceover`，**并且**紧接着说明嘴唇保持闭合 | 短语相邻性 |
| R18 | ERROR | 复合 ID `(S1,S2)` 只能引用已经编号的说话人 | 成员检查 |
| R19 | WARN | 声明了说话人却没有 `<d>` 块，或 `<d>` 没有说话人 | 关联检查 |
| R20 | ERROR | 跨切点台词的两个连接位置都必须出现 `<scenetrans>`，并有显式的连续性短语 | 成对检查与短语检查 |
| R21 | ERROR | `<cutoff>` 只能出现在最后一个镜头的最后一句台词上 | 位置检查 |

### 时间线与时长

| id | 级别 | 规则 | 检测方式 |
|---|---|---|---|
| R22 ⚑ | ERROR | `[Shot 1]` 不带时间戳 | 正则 |
| R23 ⚑ | ERROR | 切点严格递增，且全部 `< effective_seconds` | 解析 `MM:SS.mmm` |
| R24 | ERROR | 时间戳格式必须严格为 `MM:SS.mmm` | 正则 |
| R25 ⚑ | ERROR | `frames` 位于 `n % 17 == 5` 网格上；`effective_seconds == frames/24`；`S.SS` 符合 `s_ss_policy`（默认：标称秒数，2 位小数） | 算术检查 |
| R26 | ERROR | 镜头编号从 1 开始连续；尾帧锚点落在最后一个镜头（fl2va/l2va） | 序列检查 |
| R27 | WARN | fl2va 超过 1 个镜头时警告（规范：“通常偏向单镜头”），除非用户明确要求 | 镜头数与 Request 对照 |
| R28 | WARN | 镜头数与时长的比例超出约每 1.5 秒至 8 秒一个镜头的范围 | 比值 |

### 章节、标记与任务类型

| id | 级别 | 规则 | 检测方式 |
|---|---|---|---|
| R29 | ERROR | 章节集合和顺序必须与模式完全一致；字段名按模式区分（基础模式为 `integrated_multimodal_description`，ref2va 为 `detailed_description`，绝不混用） | 解析 |
| R30 | ERROR | 任务类型前缀带方括号、用 `+` 连接、无重复，且全部来自 {`keyframe completion`、`reference generation`、`video editing`、`video continuation`、`audio reuse`、`audio reference`} | 枚举检查 |
| R31 ⚑ | ERROR | 只有存在角色为 `edit_source` 的 `<Video N>` 才能使用 `video editing`；`video continuation` 仅用于 `continuation_source`；`audio reuse` 仅在附有音频时使用 | 角色交叉检查 |
| R32 | ERROR | 对于 `video editing`，`summary` 在前缀之后必须以 `The target video is an edited version of <Video 1>.` 开头 | 前缀匹配 |
| R33 | ERROR | 视觉保留标记 ∈ {`fully_preserved`、`partially_preserved`、`attribute_transfer`、`weak_reference`}；音频标记 ∈ {`fully_copy`、`partially_copy`、`reference`、`weak_reference`}；绝不混用 | 按标签类型检查枚举 |
| R34 | ERROR | `retention_analysis` 对每个已定义标签恰好有一行，使用 `label (role): marker - note` | 逐行解析 |
| R35 | ERROR | 不得在 `summary` 中引入新标签 | 标签集合 ⊆ subject_definitions |
| R36 | ERROR | 台词／歌词文本不得再次出现在 `overall_soundscape` 或 `non_diegetic_music` 中 | 子串检查 |
| R37 | ERROR | 没有画外配乐时使用 `N/A`；只有请求完全静音时才能使用 `overall_soundscape: N/A` | 值与 Request 对照 |
| R38 | WARN | `non_diegetic_music` 应说明配器／速度／动态，避免抽象情绪词（“emotional”“epic”“atmosphere of”） | 词表 |
| R39 | ERROR | Ref2VA 风格句位于 `[Shot 1]` 之前；基础模式风格位于 `[Shot 1]` 内部 | 位置检查 |
| R40 | WARN | 对生成任务，Ref2VA 的 `detailed_description` 为 350–500 个单词 | 计数 |

### 文本规范与预算

| id | 级别 | 规则 | 检测方式 |
|---|---|---|---|
| R41 ⚑ | ERROR | 结构性内容中不得有弯引号／排版破折号／NBSP／全角括号；画面文字使用直双引号 `"`，且逐字保留 | Unicode 扫描 |
| R42 | ERROR | 除 `<d>` 内部和引号中的画面文字外，所有章节都使用英文 | 按片段检测文字系统 |
| R43 | WARN | 提示词 token 数超出 350–1,400（公开样本范围 537–919）——这是分布漂移问题，不是成本问题 | tokenizer |
| R44 | WARN | 运镜超出规范的 12 种运动类型（加 `with small/large amplitude`、`at slow/fast speed`），或被堆叠为末尾标签而非自然文字 | 词表 |
| R45 | ERROR | 重新渲染 IRDocument 必须逐字节复现 `prompt` | 渲染后比较 |
| R46 | WARN | 在 768p 的全景镜头中使用带引号的画面文字（输出分辨率下不可读，见 §11） | 构图启发式 |

---

## 7. 故障分类

| # | 故障 | 原因 | 检测手段 | 后果 |
|---|---|---|---|---|
| F1 | **悬空标签**——提示词写 `<Image 1>`，运行时输出 `<Picture 1>` | 手写提示词，没有编译器 | R02、R01 | 参考素材在文本中没有被正确寻址；当参考素材 ≥2 时，模型只能猜哪个是哪个。**这就是维护者当前的提示词。** |
| F2 | **孤立素材**——附上了却从不提及 | 接线漂移 | R03 | 每步白白付出 1,008–37,296 行的成本 |
| F3 | **编号失步**——IR 按另一套素材顺序编写 | IR 与接线分别编写 | R04 | 每个标签都指向错误素材 |
| F4 | **未定义主体** | 文字凭空引入 `<Subject 3>` | R06 | 无锚定指针，完全没有绑定 |
| F5 | **说话人漂移**——ID 不符合事件顺序，或保留分析中出现 `(SN)` | LLM 自由撰写结构 | R11、R12 | 跨镜头声音／身份分配失效 |
| F6 | **台词变异**——被翻译、重加标点，或保留了 emoji | 模型“改进”台词 | R15、R16 | 说出来的不是用户原话 |
| F7 | **非法时间线**——切点位于实际终点或之后，镜头 1 带时间戳 | 假设 10 秒就是 10.000 秒 | R22–R25 | 出现模型无法到达的镜头；与帧网格偏离 |
| F8 | **网格不匹配**——`S.SS` 不符合策略，或切点超过实际渲染长度 | 混淆标称与对齐后的时长 | R25、R23 | 尾帧锚点落在错误时间；镜头不可达 |
| F9 | **模式／格式混用**——t2va 使用 `detailed_description`，或 Ref2VA 章节挤在同一行 | 所有模式共用一个模板 | R29、R39 | 输入表示偏离训练分布 |
| F10 | **标记误用**——主体使用音频标记，或凭空发明标记 | 用自由文本表达保留关系 | R33、R34 | 保留契约不再能按契约解析 |
| F11 | **错误任务类型**——没有源视频却声明 `video editing` | 从示例复制前缀 | R31、R32 | 声称存在打包输入中实际没有的关系 |
| F12 | **字节级标记损坏**——`<D>`、弯引号、NBSP | 经编辑器或聊天 UI 复制粘贴 | R13、R41 | token 序列改变，标记的已学习含义丢失 |
| F13 | **音频没有任何描述** | 把音频当图片处理，以为模型“看见”了它 | R03 + 素材卡完整性 | 编码器完全不知道音频是什么（§4.1） |
| F14 | **静默的篇幅混杂因素**——比较长度不同的 IR | 不知道 text_len 会移动 RoPE | 评测框架，§10 | 每次 A/B 测试都可能得出错误结论 |

还有三项，都是**在实际运行的推理主机端点上测得的**（§5.1），不是预想——它们是后端故障，而且会静默发生，因此格外危险：

| # | 故障 | 原因 | 检测手段 | 后果 |
|---|---|---|---|---|
| F15 ⚑ | **thinking 吞掉答案**——`content: null`，无错误 | 推理模型的思维链在输出正文前耗尽 `max_tokens` | 每次调用都断言 `content is not None` **且** `finish_reason != "length"` | 阶段没有返回内容，天真的调用者却将其当作空描述 |
| F16 ⚑ | **schema 静默未执行**——`strict: true` 返回 ASCII 表格，`finish=stop` | 推理开启时 `json_schema` 不生效 | 所有结构化阶段关闭 thinking；在我们这一侧重新对每个载荷执行 schema 校验 | 结构化阶段看似正常却返回文字，没有任何异常 |
| F17 | **符合 schema 的胡言乱语／截断 JSON**——`subjects: ["Math","Science","History"]`；或字符串生成到 token 上限仍未闭合 | 语法约束形状，不约束含义或完整性 | 对素材卡各字段进行语义检查 + `finish_reason` 检查 + JSON 解析 | 描述了错误对象、但格式正确的 AssetCard 悄悄进入 IR |

共同教训：**模型的结构化输出保证，比文档暗示的要弱，而且两个方向都弱。** 这一层必须解析、校验并检查所有返回内容是否合理——设计已经对 IR 这样做，对每张中间素材卡也必须同样处理。

另外五种风格 LoRA 故障 F18–F22 放在 §14.7，与所属功能放在一起。F18（LoRA 已接入图，但触发词没有进入文本）与 F1 属于同一种错误形态：指针没有指向任何东西，却支付了全部计算成本。

---

## 8. 可返工性：profile 与缓存键

**Profile。** 每一种真正未知的行为，都放进带版本的 `profile` 对象字段，而不是写成代码分支：`sections_included`、`verbosity_band`、`s_ss_policy`（`snapped` | `nominal`）、`fl2va_bracket_style`、`field_separator`、`style_opening_placement`、`camera_prose_style`、`analysis_depth`、`trigger_repeat_default`、`composed_retention_style`。改变一个判断，意味着发布 `h3ir/2026-09-b`，而不是修改编译器。每份 IRDocument 都记录生成它的 profile，使回归能够归因。

**缓存键**（内容寻址，因此一次改变只需重做对应阶段）：

| 产物 | 键 |
|---|---|
| `AssetCard` | `sha256(asset bytes)` + `analyzer_version` + `model_id` + 帧采样策略 |
| `Plan` | `hash(canonical Request)` + 所有素材卡哈希 + `planner_version` + LoRA 注入计划 |
| 文字正文 | `hash(Plan)` + `renderer_version` + `prose_model_id` + `prose_seed` + `profile` |
| `prompt` | `hash(prose bodies + Plan + profile)` |
| **渲染结果** | `hash(prompt)` + **`lora_set` + 强度** + `seed` + 采样器／步数／shifts + 画布 |

效果：改写一个镜头 → 只重新渲染。替换一张参考图 → 重新分析这一份素材、重新规划、重新渲染。改变时长 → 重新规划和渲染，素材卡不动。升级分析器 → 所有素材卡失效，全部下游重建。随后，ComfyUI 自身的节点缓存可避免重新编码未变化的提示词。

**渲染结果**的键有意与提示词键分开（§14.5）：LoRA 通过两个独立通道影响输出——触发词改变 IR *文本*，权重改变*采样*。提示词哈希看不到后者，因此只用提示词作为渲染缓存键，会产生最糟糕的缓存命中：文本正确、权重错误，而且产物中没有任何线索揭示这一点。

**在产物中记录所有版本**：`ir_version`、`profile`、`analyzer_version`、`planner_version`、`renderer_version`、模型 id、seed。一份无法归因的 IR，也是一份无法改进的 IR。

---

## 9. 已规定／推断／未知

### 已规定——按这些构建，它们不会改变

各模式的章节名称和顺序；各模式的字段名；四个标签命名空间及其语义；保留标记枚举（视觉与音频分开）；六种任务类型与 `+` 规则；说话人 ID 规则，包括禁止在 `retention_analysis` 中出现 `(SN)`；`<d>[Language] …</d>`、`<scenetrans>`、`<cutoff>`、精确的画外音短语及嘴唇闭合说明；`[Shot N] At MM:SS.mmm`；镜头 1 无时间戳；12 种运镜类型及幅度和速度；三种指令行原文；画面文字用直双引号，逐字保留、不翻译；英文正文配原语言台词；Ref2VA 生成任务的 350–500 词要求；`N/A` 约定。来自运行时、因此比文档更有力的规定包括：精确输出的标签字符串、输出顺序、按类型从 1 开始的编号、`n % 17 == 5` 帧网格、短边 768 且面积上限为 768×1344 的画布、≤9 张图片 / ≤3 段视频 / ≤3 段音频 / ≤12 个文件、音频不能是唯一输入，以及提示词不会在任何地方被截断这一事实。

### 从公开输出推断——可据此构建，但值得复查

- **I1** 托管服务的 `<Audio N>` 编号与运行时输出顺序完全一致（`<Video 1>` 的音轨是 `<Audio 1>`；独立人声参考是 `<Audio 2>`）。两个独立来源一致。
- **I2** 托管 T2VA 调用大约是*公开指南作为系统提示词 + 需求文本*（实测 3,582 + 493，对比报告的 5,650）。
- **I3** 报告的 `completion_tokens` 是输出产物的 5–11 倍 ⇒ 托管阶段是包含内部推理的多阶段工作流，用量数字是汇总值。不要据此制定预算。
- **I4** 篇幅随参考素材而变，不随创作野心而变：249 词（T2VA，无参考）、238 词（Ref2VA 编辑）、**533 词（I2VA，一张图，静态镜头）**。长的那份之所以长，是因为它在清点参考图。另外，公开 Ref2VA 的 `detailed_description` 为 238 词，*低于*指南的 350–500 词范围——这是合法的，因为指南将编辑任务排除在该范围之外。因此 R40 只能适用于生成任务。
- **I5** 风格可能以堆叠前缀出现（“Cinematic, medium wide shot, pushing in slowly.”），尽管 `base-en.txt` §4.3 更偏好自然文字 ⇒ 那是偏好，不是约束（因此 R44 是 WARN，而非 ERROR）。
- **I6** 跨镜头身份通过普通文字重新确立（“the captain from Shot 1”）；方括号只用于 `[Shot N]` 标记本身。
- **I7** 保留说明行可以包含自由文本的角色括注（“(source video editing)”），超出指南示例的写法。
- **I8** 实际托管输出的字段分隔符会变化（T2VA 使用 `\n`，I2VA 使用 `\n\n`）⇒ H3-Base 两种都能接受。

### 真正未知——用 profile 隔离，再通过实验解决

- **U1 ⚑** `summary` 和 `retention_analysis` 对 H3-Base 到底是否有贡献，还是只是托管 LLM 的脚手架？它们约占 Ref2VA IR token 的 40%。*我的先验判断：它们重要，因为 H3-Base 训练时的 Context-IR 输出始终包含它们——不管语义贡献如何，省略它们都会偏离训练分布。* 这是价值最高的未知项。→ E1。
- **U2** `S.SS` 应是请求的标称时长，还是对齐后的时长（`frames/24`）？证据偏向标称值（指南自己的 `6.00` 不在网格上；托管 API 接收整数时长），但本地渲染确实比声明长 0.125 秒。→ E3。
- **U3** fl2va 使用不带括号的 `Picture 1`，还是带括号的 `<Picture 1>`，是否有影响？→ E4。
- **U4** 控制 text_len 混杂因素后，超过约 600 token 的篇幅是否有帮助？→ E2。
- **U5** `<d>` 字节写错的影响有多大。→ E6。
- **U6** H3-Base 是否使用 `[task type]` 前缀？→ E1 变体。
- **U7** 素材超过 9 张图片或 15 秒时，托管阶段如何选择／压缩。没有本地可见性；作为产品决策处理（编译器应*拒绝*并请求调整，而不是静默丢弃）。
- **U9** 真实需求上的模式推断准确率，特别是帧锚点与内容参考的判断——这是 §12 中唯一由模型而非接线作出的决定。→ E8。
- **U11** 合成场景参考与拆分参考的差异（一个生产使用方报告，合成关键帧带来的运动不可靠）。尚未为此设计；扩展接口是已经填充的 `AssetCard.composition`。我的机制假说，以及用来区分两种可能原因的第三个测试组，见 §14.8——**[MINE，可证伪]**。
- **U10** abliteration 是否损害了模型遵循格式的精确性。IR 是严格格式，而 abliterated checkpoint 的服从性调优针对的是另一个方向。低成本探测：对 N 份渲染输出运行校验器，跟踪修复前的 ERROR 率——修复前错误率高就是信号。备选方案是仅对*结构化*阶段采用非 abliterated 模型（已有第二台主机可用），创意文字仍由推理主机处理。
- **U8** 发布的 CFG 蒸馏版 Ref2VA checkpoint 是否保留了任何上下文内再生成能力。→ §11。

### 由作者决定——可以修订，而且每项都对应一个 profile 字段

空行字段分隔符；`S.SS` 来自对齐后的时长；IRDocument schema 与“清单和提示词一起交付”的契约；编译结构／生成文字的拆分；五阶段流水线；缓存键组成；后端优先顺序；WARN 与 ERROR 的级别划分；350–1,400 token 区间；将音频分析视为硬依赖。

---

## 10. 实验套件（全部可证伪，固定 seed，每次只改一个变量）

所有实验都在已安装的 `minimax_h3_ref2va_pruned_int8_convrot` 上运行，使用 5 秒 / 1344×768（124 帧）——成本最低的合法配置——并保持 seed、采样器、步数和 shifts 一致。

- **E0——基线。已完成一半。** 撰写部分已经**实测**（§2：80–107 tok/s、指南 prefill 3.25 秒、每张 1344×768 参考图 1,024 token、完整 Ref2VA IR 25–40 秒）。剩下的是采样部分：测量每步秒数与打包行数的关系，对 1/3/9 份参考素材分别扫描 `ref_image_size` 的 match/max，验证 §2 的行数表能否按预测换算为实际耗时。
- **E1 ⚑——章节消融（U1、U6）。** 主体相同、`detailed_description` 相同；四组：完整六章节 / 去掉 `retention_analysis` / 去掉 `summary` + `retention_analysis` / 只有文字描述。**通过填充描述，把 `prompt_tokens` 控制在 ±2% 内**，否则 RoPE 偏移混杂因素（§1.3）会使结果无效。评分：相对于参考素材的身份保持，盲评。
- **E2——篇幅（U4）。** 同一计划分别写成约 400 / 700 / 1,100 token 的三份 IR。同时报告原始组和 token 数匹配组，并明确说明长度与内容无法完全分离。
- **E3——`S.SS` 策略（U2）。** fl2va，243 帧（实际 10.125 秒），比较 `10.00` 与 `10.13`；以提供的尾帧为基准，测量尾帧落点的帧数误差。
- **E4——括号约定（U3）。** 比较 fl2va 指令行中不带括号与带括号的写法。
- **E5——相同权重撰写（§5）。** 同一计划，比较 H3 自身编码器加尾部权重生成的 IR，与另一个更强 VLM 生成的 IR。决定那个优雅方案是否值得进入生产。
- **E6——标记损坏（U5）。** 比较 `<d>`、`<D>`、`< d >`；测量口型同步，以及文字是否真的被说出来。
- **E7 ⚑——维护者实际使用的提示词，以及让结论成立的对照。** 使用他的两张参考图（披甲男子 + 龙），分四组：
  - (a) 原样使用他的文字，包含 `<Image 1>` / `<Image 2>`。
  - (b) 文字不变，只把标签修正为 `<Picture 1>` / `<Picture 2>`。
  - (c) 完整编译的 Ref2VA IR（主体、保留契约、时间线）。
  - (d) **对照：** 基于 (b)，**只在接线上**交换两张图，提示词文本不动——这样文本现在把*龙*的图片说成男子。

  (d) 要判定的是：如果输出遵循标签（出现人形龙，或披甲骑龙者明显用了错误主体），说明标签确实产生绑定，F1 是真实缺陷。如果输出忽略交换，仍然生成男子骑龙，说明产生绑定的是**位置**，标签只是装饰——F1 就缩小为表面问题，我对它影响大小的判断就是错的。无论哪种结果，都值得在依赖 §1.1 之前知道；六章节格式的结构性价值，则可独立通过 (c) 与 (b) 的比较判断。
- **E8——模式推断准确率（§12）。** 一组约 40 条已标注需求（我的需求，加上维护者历史中的真实需求），每条都带附件和已知正确模式。评估准确率，更重要的是评估**错误方向**：将内容参考路由到 FL2VA 是硬失败（该模式无法表达需求）；将帧锚点路由到 Ref2VA 是软失败（仍可表达，但保证较弱）。目标：零硬失败；歧义问题率应足够低，不让 agent 调用者被反复盘问——如果每 10 条需求需要提问超过 1 次，我会视为设计失败，而不是调参问题。
- **E9——两种调用者得到相同结果。** 用相同的 10 条需求，一次像普通 agent 那样通过 HTTP API 调用（一句话 + 文件，没有选项），一次像 UI 那样调用（带上 UI 能设置的所有字段），然后比较 IR。任何与创作质量有关的差异，都违反 §13 的核心规则，是 API 表面的问题，而不是模型问题。

---

## 11. 闭源 `H3-Regenerate-2K` 对本地输出意味着什么

**它是什么：** 不是超分辨率网络，而是 H3-Base 在*上下文中*重新生成自己的 768p 结果，同时重新附上原始多模态上下文，从而恢复普通放大器只能凭空猜测的小字与细节。MiniMax 因“系统复杂性”没有公开它，这意味着所需机制不止一个提示词技巧。

**能在本地复现吗？不能——阻碍在于算术，而不是访问权限。** 2K 画布（短边 2048，16:9 → 3648×2048）每个 latent 帧需要 `(3648/32)×(2048/32) = 7,296` 行。五秒就是 `37 × 7,296 = 269,952` 行，再加上把 768p 结果作为参考视频附上所需的约 37,296 行。开源版本**只有全注意力**（稀疏注意力明确留待后续发布），所以这相当于在一张消费级 GPU 上，让约 25 万行序列以 O(n²) 复杂度经过每个块。15 秒 2K 约为 832,000 行。即使发布的 CFG 蒸馏版 Ref2VA checkpoint 确实保留上下文内再生成能力（**U8**，可以在小画布上低成本探测），这套硬件也跑不动。上游加入稀疏注意力会改变算术成本，但不会改变这里的结论。

**因此，实际的上限是短边 768p**，差距恰好就在 MiniMax 所说的地方：画面中的小字、远处人脸、精细纹理。本地备选是传统视频放大器（已安装 `seedvr2_videoupscaler`），或分块扩散处理——它们都在*猜测* Regenerate-2K 原本会*恢复*的细节。应明确说明这一点，不要把放大后的 768p 渲染称作“2K”。

**IR 设计应该如何应对——四件事，现在做都很便宜：**

1. **把 IR 文档及其清单保存为持久、可重放、按内容计算哈希的产物。** Regenerate-2K 的输入*就是*原始上下文；将来无论通过 API 还是本地使用它，唯一办法都是保留逐字节精确的上下文。这是最重要的预留措施，而且没有额外成本。
2. **把帧网格、画布、seed、采样器和 shifts 与 IR 一起记录**，让再生成可以复现，而不是近似重做。
3. **把 `regenerate` 作为同一个编译器的一种模式，而不是第二条流水线**——它是一份 Ref2VA 计划，第一份参考素材是先前输出（`role: continuation_source` / `edit_source`），原主体和保留契约原样沿用。如果未来发布本地阶段，或维护者认为每个关键镜头调用一次 API 可以接受，这只是计划变体，而不是重写。
4. **利用本地唯一能够引入 2K 细节的位置：** 即使输出是 768p，`ref_image_size=max` 仍能以短边 2048 输入参考图。把那 7,296 行花在重要的身份参考上，其余使用 `match`（1,008）。当计划把引号中的画面文字放进 768p 无法承载的构图时，要给出警告（R46）——在 768p 下这些文字会糊成一团，没有本地阶段能把它救回来。

---

## 12. 模式推断——用户永远不选模式

如果 checkpoint 和模式对用户不可见，就必须有某个地方在任何改写之前，判断用户请求的是五种模式中的哪一种。这个决定属于这一层的前端，因为它恰好由这一层已经负责的两件事决定：**接线和素材角色。**

### 12.1 模式之间真正的区别 **[SPEC]**

根据发布表：`H3-Base-FL2VA` 接收 0 张图（T2VA）、1 张图（首帧*或*尾帧）或 2 张图（首帧和尾帧）；`H3-Base-Ref2VA` 接收全模态参考集合。因此，这不是五种创作意图，而是**两个 checkpoint 加一个数量**，再加上一个真正困难的语义问题：

> 这张图是视频的**某一帧**，还是**会出现在视频中的某个事物**？

这就是全部难点。“让这张照片动起来”（照片*就是*第 0 帧 → I2VA）与“把这个男人放进战场”（男人作为内容出现，重新绘制 → Ref2VA），附件数量和文件类型完全相同。其他情况都可以确定性路由。

### 12.2 路由流程

**先应用确定性规则——不涉及模型：**

| # | 条件 | 模式 |
|---|---|---|
| 1 | 附有任何视频或音频参考 | **Ref2VA**（FL2VA checkpoint 无法接收它们） |
| 2 | 图片超过 2 张 | **Ref2VA** |
| 3 | 0 张图片 | **T2VA** |
| 4 | 1–2 张图片 | → 12.3，唯一需要真正判断的情况 |

**12.3 对 1–2 张图片判断帧锚点还是内容参考。** 根据需求文本和 AssetCard 做一次分类调用（关闭 thinking，使用很小的枚举 schema，遵循 §5.1），返回 `{intent: frame_anchor | content_reference, which: first | last | both, confidence, signals[]}`。要求它权衡以下信号：

- *锚点措辞*：“animate this”“make this move”“bring this to life”“start from this”“end on this”“from this frame”“this is the opening shot”（让它动起来、以它开场或结束、把它作为某一帧）。
- *参考措辞*：“using this character”“this person/product”“in this style”“like this”“put X in”“combine”（使用该角色／人物／产品／风格、把某物放进去、组合）。其中决定性的信号是**从两张不同图片中分别指定两个主体**（“X 图中的男人骑着 Y 图中的龙”）——这只能是 Ref2VA，其他模式无法表达。
- *几何关系*：首帧锚点会被节点拉伸到目标画布（`_resize(..., "disabled")`），因此图片宽高比与请求输出宽高比明显不同时，它不是理想的锚点候选。这是弱信号，但有真实机制依据。
- *配对关系*：两张图被描述为“开始和结束” → FL2VA；被描述为两个不同事物 → Ref2VA。

随后：1 张图 + 锚点 + `first` → **I2VA**；+ `last` → **L2VA**；2 张图 + 锚点 + `both` → **FL2VA**；任何数量 + `content_reference` → **Ref2VA**。

### 12.4 如何安全地处理不确定情况

**存在歧义时默认 Ref2VA**——这有原则上的依据，不是抛硬币。**Ref2VA 严格更具表达能力。** 规范中的 `keyframe completion` 任务类型，就是为了让 Ref2VA IR 能表达“这张图片作为目标视频的首帧、关键帧或尾帧”，因此误路由的锚点仍可表达。反向则不成立：FL2VA 无法表达“这个事物出现，但不是一帧”。这种表达能力上的不对称，提供了安全默认值。它也是目前唯一已安装的 checkpoint。

这个默认选择确实有代价，应记录而不是隐藏：FL2VA 把关键帧作为**固定在第 0 帧（或 `frame_count - 1`）、永不去噪的条件 latent** 注入，提供精确帧保证；Ref2VA 的 `keyframe completion` 则是较软的承诺。因此：

- 置信度高且几何关系一致 → 路由到 FL2VA，获得精确帧保证。
- 置信度低，**且**需求包含锚点措辞 → **问一个问题**（§13.4），而不是静默选择较弱保证，因为这是一种错误默认值会在输出中明显可见的情况。
- 置信度低，但没有锚点措辞 → Ref2VA，不提问，不增加流程负担。

**记录每一次决定**：`mode_decision {mode, confidence, rule_fired, signals[], alternatives, asked: bool}`。UI 显示“正在让你的照片动起来”，记录中保留审计信息。不向用户展示模式名，不等于向调试者隐藏决定过程。

**超容量请求应拒绝，而不是猜测：** 超过 9 张图片、3 段视频、3 段音频、12 个文件，或源素材超过 15 秒时，本地没有已定义行为（**U7**）——托管阶段会以我们看不到的方式压缩。此层应返回清楚、可操作的错误，说明需要移除什么。静默丢弃用户附上的参考素材，是最糟糕的结果。

---

## 13. 两种调用者，一个契约

> *“提供完整 API，让不熟悉 X/Y/Z 的 agent 也能驱动它”* · *“UI 里不要有‘Comfy 行话’，要真正对用户友好”*

这两个要求其实是同一个要求：**API 才是产品，UI 只是它的一个客户端。** 如果 UI 需要 API 没暴露的字段，或 API 依赖只有 UI 才能补充的创作知识，两种调用者得到的质量就不同。因此规则必须是结构性的：

> **不存在只有 UI 才能设置、且影响质量的字段，也不存在绕过校验器的路径。** 一条代码路径，一个校验器，没有特权客户端。

### 13.1 最小可用请求是一句话加一些文件

```http
POST /v1/briefs
{ "intent": "the man in armour rides the dragon into a huge battle, cinematic",
  "assets": [ {"url": "...", "note": "the man"}, {"url": "...", "note": "the dragon"} ] }
```

没有模式，没有 checkpoint，也没有画布、帧网格、章节名、运镜词汇或标签语法。其他字段全部可选，并有良好默认值。仅这一点，就让一个完全不知道 FL2VA 是什么的 agent 也能驱动这一层——而这正是它存在的明确原因。

`note` 是每份素材可选的自由文本；它是给 C 阶段的*提示*，不是必需项。agent 即使附上两张没有说明的图片，也仍应得到正确结果，因为 AssetCard 会描述它们。

完整接口面很小：`POST /v1/briefs`、`PATCH /v1/briefs/{id}`、`GET /v1/briefs/{id}`，以及用于发现风格的 `GET /v1/loras`（§14.4）。需求可以携带 `"loras": [{"id": "…"}]`——只传 id，不传触发词，理由与不传 `<Picture 1>` 相同。

### 13.2 创作能力在服务端，调用者输入的是约束——不是计划

调用者*可以*提供更多内容：时长、宽高比、镜头数量、台词、“不要运镜”。这些都作为**计划的约束**进入，而不是作为计划本身。结构、标签、说话人 ID、时间戳、保留标记和章节顺序，仍由编译器负责。写成契约就是：

> **调用者省略的，由这一层妥善决定；调用者指定的，这一层要么遵守，要么说明理由后拒绝。** 绝不静默覆盖，也绝不静默服从非法要求。

这直接回答了“重编码的 agent 不可能真正退一步发挥创意”的担忧：调用者不需要当艺术总监，也不允许半懂不懂地承担这个角色。

### 13.3 绝不因描述不充分而失败；因矛盾而失败

- “给我的狗做一个视频” → 完整、符合规范、通过校验的 IR。不提问，不报错。
- “10 秒，4 个镜头，第一个切点在 12 秒” → 返回 `422`，指出矛盾（R23：切点超过实际渲染的 10.125 秒终点），并给出具体修正方式。

这种不对称，决定了一个工具能否被不熟悉领域的 agent 驱动。

### 13.4 只澄清一次，而且已经应用默认值

对于唯一一个真正有歧义、且代价较高的决定（§12.4），响应为：

```jsonc
{ "status": "needs_input",
  "question": { "id": "anchor_or_reference",
    "ask": "Should your photo be the video's opening frame, or should the person in it appear in a new scene?",
    "options": [ {"id":"opening_frame","label":"Use it as the opening frame"},
                 {"id":"appears_in","label":"Put the person in a new scene"} ],
    "recommended": "appears_in" },
  "default_if_unanswered": "appears_in",
  "expires_in": 900,
  "result": { /* a complete, usable IR built with the default */ } }
```

重要性质是：**默认值已经应用，结果已经有效。** 忽略问题的 agent 仍能得到好视频；人类看到的是一个普通语言的选择器；想回答的 agent 只需发送一个字段。每条需求最多一个问题——不断盘问调用者的工具，算不上“可由不熟悉领域的 agent 驱动”。

### 13.5 每个响应分三层，因此没人必须看到“ref2va”

| 层 | 面向对象 | 内容 |
|---|---|---|
| `presentation` | 最终用户 / UI | “让你的照片动起来 · 10 秒 · 宽屏 · 带声音”。没有模式名、帧数或节点名。 |
| `plan` | 审阅者、修改者 | 普通语言表达的创作决定：镜头、主体、台词、声音。可编辑。 |
| `ir` | 调试者、复现者 | 完整 `IRDocument`（§3），加上 `mode_decision` 和 `diagnostics`。 |

UI 渲染 `presentation` + `plan`，从不提 checkpoint。开发者仍能拿到全部信息。“没有 Comfy 行话”通过*分层*实现，而不是通过隐藏信息实现。

### 13.6 修改是一项操作，不是重跑

```http
PATCH /v1/briefs/{id}   { "change": "make it darker and have him say it slower" }
```

这直接对应 §8 的缓存键：只改文字就重新渲染（约 11 秒）；改时长就重新规划；替换一份素材就重新分析一张卡。`POST` 接收幂等键，避免不断重试的 agent 重复做工。每次修订都是有自己哈希的新不可变版本，因此“第三版最好”可以恢复——这就是 Higgsfield 的通过回复修改的行为，而缓存设计已经支持它。

### 13.7 校验器规则是公开错误词汇

每个规则 id（R01–R46）都是稳定、有文档的错误码，包含人类可读消息和机器可读的 `fix` 提示。大多数规则永远不应暴露给调用者——标签归编译器负责，因此 R01/R02/R04 是内部不变量；如果它们触发，就是我们的 bug，而不是调用者的问题。真正面向调用者的规则，只有矛盾类规则（R23 非法切点、R25 非法时长、R31 声称编辑却没有源视频、R05 超容量）。这个接口面应小、有文档、可操作。

### 13.8 这一层止步于哪里

“超级计算机”编排器——接收普通需求、规划、选模型、生成、在聊天中返回——是此 API 的**调用者**，而不是它的一部分。编排器负责：是否要做视频、选 H3 还是 LTX 或其他模型，以及拼接超过 15 秒的序列。这一层负责：**需求 → 通过校验的 IR + 接线，或清晰的拒绝。** 保持这个边界，才能让两部分都可测试，也才能在重写编排器时，不碰理解 H3 格式的部分。

---

## 14. 风格 LoRA：注册表，以及触发词究竟放在哪里

> *“每个 LoRA 都应该有配套的 `howtouse.md`，应用的‘超级计算机’应该能访问它……‘哦，这个片段我也加上这个 LoRA，接线已经就绪，而且我准确知道它的触发词’。”*

这是对契约的扩展，而不是改变，因为触发词是 **IR 中的文本**，而 §1 已经明确了文本的处理方式：逐字节精确分词、追加到所有参考块之后、绝不截断。因此，LoRA 触发词必须遵循与 `<d>` 和 `<Picture N>` 相同的纪律——字节和位置必须正确，否则就会静默不起作用。

**有一点需要调整，这会让功能更安全。** agent 当然应当能够*发现*触发词，但不应*负责*触发词。它通过 id 选择 LoRA；编译器负责触发词字符串、大小写、出现次数和位置——正如编译器自己负责 `<Picture N>`，而不相信调用者能手工输入正确。原则相同，原因也相同：让调用者输入逐字节精确的 token，就意味着它可能被输错。因此，agent 的工作变成“这个片段需要吉卜力风格”，而底层接线才真正算是就绪。

### 14.1 各模式的触发词位置 **[SPEC 槽位，MINE 放置方式]**

规范已经为各模式预留风格声明槽位，这就是正确位置：

| 模式 | 槽位 | 渲染结果 |
|---|---|---|
| ref2va | `[Shot 1]` **之前**独立成行的风格句 | `The target video is in hndpntd_anim_v2 hand-painted animation style with soft watercolour backgrounds.` |
| t2va / i2va / fl2va / l2va | `[Shot 1]` **内部**、逗号分隔的风格前缀 | `[Shot 1] hndpntd_anim_v2, hand-painted 2D animation, a medium-wide shot frames…` |

基础模式的放置方式完全符合现有格式：规范自己的示例就以逗号分隔风格列表开头（`[Shot 1] Live-action, cinematic, a medium-wide shot…`），公开的 T2VA 产物开头也是 `Cinematic, medium wide shot, pushing in slowly.`——因此，**风格前缀本来就是一个列表，触发词只需作为第一项插入。** **[INFER，来自公开产物]**

三条放置规则，全部属于 **[MINE]**，并都放进 profile：

1. **默认只出现一次。** 重复触发词是社区习惯，不是规范；每增加一次出现，就增加文本行数，移动所有条件块的 RoPE 原点（§1.3）。如果某个 LoRA 确实需要重复，可用注册表中的 `repeat` 覆盖。
2. **绝不放入内容正文。** 触发词属于整段视频的风格，不属于其中某个名词。
3. **绝不放入 `<d>` 片段或引号中的画面文字。** 那些是用户的原样内容（R15），不应把它们计为触发词出现，也不能修改它们来制造一次出现。

### 14.2 冲突——五种真实情况及解决方式

**(a) 触发词与 H3 保留语法冲突。** 某个 LoRA 的触发词若是 `<painted>`，它就符合 R02 要拒绝的形状；削弱 R02 来容纳它，会重新打开整类悬空标签错误（§7 F1）。解决方式：**在注册表导入时校验触发词字符串**，并给出普通语言说明后拒绝该 LoRA。包含 `<…>`、`[Shot`、`(S1)`、`<d>`、`<scenetrans>` 或 `<cutoff>` 的触发词，在此格式中确实不可用，不重新训练也无法用别名替代。这是真实约束；应在导入时暴露，而不是等到静默质量损失时才发现。

**(b) 常见单词触发词泄漏到内容正文。** 触发词是 `watercolor`，主体描述又写了 “a watercolor painting on the wall”，就会出现两次触发，其中一次附着在道具上。检测是精确的：统计 `<d>` 与引号文本之外的出现次数，超过声明次数即为 R50。修复方式是要求文字阶段在内容描述中避免这个词；由于文字阶段本来就受约束，这是一条可执行指令。

**(c) 触发词出现在用户台词中。** 按上面的规则 3，不计入次数，而且绝不改写。值得特别说明，因为天真的实现 `ir.count(trigger)` 会在这里出错：既可能漏检（触发词只在台词里，却以为已经存在），又有修改台词的风险。

**(d) 两个 LoRA 相互矛盾。** 注册表用 `conflicts_with` 表达；解析阶段拒绝该组合，并指出两个名称。

**(e) 非英文触发词与英文正文规则（R42）冲突。** 已注册触发词可豁免——它是不透明 token，不是文字正文。**只有已注册的才可以。** 这样豁免范围保持很窄，由注册表把关，而不是给 R42 开一个普遍缺口。

### 14.3 `howtouse.md`——给编译器的 front matter，给规划器的文字

**我的判断：两者都要，但严格按使用方拆分。** 原则与 §4 相同——*结构靠解析，判断靠阅读*。触发词必须逐字节保留，而交给模型阅读的文字有损；规划器判断“这是否适合请求”则属于判断，确实需要文字。因此，确定性编译器**只读** front matter，规划模型**只读**正文。绝不反过来。

```yaml
---
id: handpainted-anim-v2               # stable identity, independent of filename
name: Ghibli Animation           # label for the presentation layer
kind: style                      # style | subject | motion | quality
target: minimax_h3               # minimax_h3 | krea2 — which model it patches
h3_variant: [ref2va]             # WHICH CHECKPOINT — see 14.4
file: handpainted_anim_v2.safetensors
sha256: "9f2c…"                  # identity of the weights, not the path
version: 2
triggers:
  - text: "hndpntd_anim_v2"      # quoted: casing, underscores, spacing are load-bearing
    required: true
    placement: style             # style | none
    repeat: 1
strength: { default: 0.8, min: 0.4, max: 1.0 }
constrains:                      # null = no constraint
  aspect: null
  duration_frames: null          # or an explicit subset of the legal grid (§3.2)
  steps: null
  canvas: null
conflicts_with: [photoreal-v1]
license: "…"
---
```

正文按以下顺序组织，因为它会由执行语义匹配的模型阅读：

- **适合什么**——用用户的语言，而不是训练者的语言：“类似吉卜力工作室的手绘动画、柔和水彩背景、轻柔的角色运动。” 这段文字用来匹配“做得有点吉卜力风格”这样的请求。
- **什么时候不要用**——负向匹配可防止过度触发，而这正是大多数注册表遗漏的部分。
- **强度指导**——在整个取值范围内，具体会发生什么变化。
- **已知故障模式**——会破坏什么（人脸、可读文字、快速运动）。这些会变成规划器警告；在 768p 输出下（§11），破坏小字的 LoRA 比在 2K 下更值得注意。
- **风格开头示例**——一两个，使用 IR 形式，展示触发词在槽位中的位置。

### 14.4 选择是一个编译器阶段，checkpoint 约束是真实的

FL2VA 和 Ref2VA 是**两个不同的任务专用 checkpoint，各自拥有 Omni Transformer 权重**——所以基于其中一个训练的 LoRA，不会自动对另一个有效。因此需要 `h3_variant`。这让 LoRA 选择与模式推断（§12）耦合，解析顺序也很重要：

> **先能力，后可用性。** 模式由请求*需要什么*决定（§12）。如果所选 LoRA 不适用于该模式的 checkpoint，这一层必须**明确暴露冲突**——绝不为了迁就 LoRA 静默改变模式，也绝不静默丢弃 LoRA。

在输入接收与规划之间增加一个阶段：

```
A   Intake              brief + assets + wiring + proposed lora ids
A'  LoRA resolution     ids -> registry records; legality vs the request; strength clamp;
                        trigger injection plan   (deterministic)
B   Analyse             AssetCards
C   Plan                receives the injection plan as a hard constraint
D   Render              places the triggers in the mode's style slot
E   Validate            proves every required trigger is present, in slot, byte-exact
```

**给 agent 的发现接口：** `GET /v1/loras` 返回 id、name、kind、target、variant、constraints，以及*“适合什么”／“什么时候不要用”*的文字——包括做选择需要的全部信息，但没有任何必须逐字节重新输入的内容。随后用 `POST /v1/briefs {"loras": [{"id": "handpainted-anim-v2"}]}` 选择，强度可选。不提供 `loras` 的 agent 仍能得到好视频；指定了不适合的 LoRA，则得到具体拒绝。

### 14.5 为复现而记录的注册表信息

在 `IRDocument.provenance` 中，与 seed 一起随结果返回：

```jsonc
"loras": [
  {"id":"handpainted-anim-v2","version":2,"file_sha256":"9f2c…",
   "strength_requested":0.9,"strength_applied":0.9,
   "triggers_injected":[{"text":"hndpntd_anim_v2","slot":"style","count":1}],
   "registry_revision":"reg-2026-08-10T14:22Z"}
]
```

它必须进入**两个**缓存键，而不只是一个，因为 LoRA 通过两个独立通道影响渲染：触发词改变 IR *文本*（所以已经包含在 `prompt` 哈希里），权重改变*采样*（提示词哈希看不到）。因此，渲染身份是 `hash(prompt) + lora_set + strengths + seed`。这里做错，会产生最糟糕的缓存命中——文本正确、权重错误，而且无法从产物中辨别。

### 14.6 新增校验规则

| id | 级别 | 规则 |
|---|---|---|
| R47 ⚑ | ERROR | 每个 `required` 触发词都必须在渲染后的 IR 中逐字节精确出现，次数符合声明；计数时**排除** `<d>` 片段和引号中的画面文字 |
| R48 ⚑ | ERROR | 每个触发词都位于声明槽位（ref2va 风格行／基础模式 `[Shot 1]` 前缀），而不是埋在镜头正文中 |
| R49 | ERROR | 触发词字符串不得与 H3 保留语法冲突——注册表导入时和编译时都要检查 |
| R50 | WARN | 触发词出现次数超过声明（常见单词泄漏到内容正文） |
| R51 | ERROR | LoRA 的 `h3_variant` 必须包含路由模式所用的 checkpoint |
| R52 | ERROR | 请求满足每个非 null 的 `constrains` 字段（指出具体违反项） |
| R53 | ERROR | 任意已选择的两个 LoRA，都不能出现在对方的 `conflicts_with` 中 |
| R54 | WARN | 强度被限制到声明范围内时，记录请求值和实际应用值 |
| R56 | ERROR | 磁盘文件的 sha256 与注册表记录一致 |
| R57 | WARN | 已注册触发词豁免 R42 的英文正文规则；风格槽位中*未注册*的非英文 token 仍然报错 |

### 14.7 新增故障模式

| # | 故障 | 原因 | 检测手段 | 后果 |
|---|---|---|---|---|
| F18 ⚑ | **静默无效 LoRA**——已接入图，但触发词没进入文本、大小写错误，或只出现在 `<d>` 片段内 | 触发词和权重由系统的不同部分接线 | R47、R48 | 支付全部 LoRA 计算成本，却没有风格效果；输出只是看起来“有点不对”，而非明显损坏——最难察觉的故障 |
| F19 | **触发词泄漏**——常见词触发词影响了道具，而不是整个片段 | 文字阶段不知道触发词词汇 | R50 | 风格作用范围错误 |
| F20 | **变体不匹配**——将针对 FL2VA 训练的 LoRA 用在 Ref2VA checkpoint 上 | 两个 checkpoint 放在同一个文件夹 | R51 | 出现不可预测的质量下降，看起来像 LoRA 不好用 |
| F21 | **静默限幅或静默丢弃**——悄悄限制强度，或悄悄忽略冲突 LoRA | 出于“好心” | R54、R53 | 用户以为风格已全强度应用，实际并非如此 |
| F22 | **稳定 id 下的文件被替换** | 文件名相同，权重不同 | R56 | 昨天可复现的渲染今天无法复现，记录中却看不出原因 |

这一整节最主要是为了防止 F18，它与 F1（`<Image 1>`）形态相同：指针没有指向任何东西，付出全部计算却没有效果。

### 14.8 两个较小的事项

**机器生成参考素材的来源记录——值得有一个字段，而且理由不只是复用。** 当 Qwen Image Edit 和 Krea 2 的静帧成为 H3 参考素材时，应用通常*知道*参考素材是怎么生成的。增加 `asset.provenance {generator, model, source_prompt, parent_asset_sha256, edit_instruction}`。但最诱人的用法恰恰是错的：**绝不用生成提示词替代 AssetCard。** H3 根据像素进行条件生成，而生成图像经常不符合提示词——复用提示词，相当于按照创作意图而不是实际结果给渲染评分。提示词应作为*先验*交给分析器（“这张图由以下提示词生成：……；请根据你实际看到的内容纠正它”）。

真正的收益在其他地方：

1. 它记录整条链，让渲染可以端到端复现。
2. 它顺带提供 `composition`（独立素材底图还是完整合成场景），供下文“合成与拆分”的问题使用。
3. **它消除了 §12 中最难的判断**——当静帧由应用生成时，应用*知道*它是为了作为开场帧，还是作为主体底图制作的。这是在 §12.3 原本只能用语言启发式的地方提供事实依据；一旦存在，就应直接跳过帧锚点与内容参考分类调用。

**完整合成场景与拆分素材——尚未设计，但接口已预留，我也有一个机制假说。** 现在记录 `AssetCard.composition: bare_plate | composed_scene | unknown`（已知时来自来源记录，否则来自分析器），这样等测量结果出来，保留措辞就可以根据已有字段分支，而不是再加新字段。

再给一个可证伪的预测，属于 **[MINE]**，供测试参考：§1.1 能预测*为什么*拆分参考素材可能更好。完整合成关键帧容易诱导对整帧使用一条 `fully_preserved` 保留说明——这会过度约束其中的一切，包括本应运动的部分。独立底图则让每个 `<Subject N>` 带自己的标记，因此身份可以 `fully_preserved`，布局仍然自由。如果机制成立，**当放宽完整合成组的保留措辞时，效果差异应该消失**（场景使用 `partially_preserved`，各主体使用各自标记）——这是值得加入测试的低成本第三组，它能够区分“完整合成参考素材更差”与“完整合成参考素材容易诱发过度约束的保留措辞”。两者的解决方式完全不同：前者需要改素材，后者只需要改 IR 中的一行。登记为 **U11**。

---

## 15. 构建顺序

按以下顺序安排，使每一步都可以独立验证，而且最早的步骤就能提供构建这一层所追求的缺陷预防能力。

1. **Tokenizer 与预算工具、帧网格、`IRDocument`/`Plan` schema。** 黄金测试样本：将三份公开 IR 原样保存到 `scratchpad/golden/{t2va,i2va,ref2va}.ir.txt`——解析器必须对三者都实现逐字节无损往返，校验器必须对三者都报告**零 ERROR**（预期会有 WARN；精确 token 计数器为 `scratchpad/h3tok.py`）。
2. **校验器（§6）**，对上述样本和**每份样本故意破坏后的变异版本**运行——无法在变异样本上失败的规则，不算规则。将维护者的真实提示词也作为样本，它必须触发 R02。
3. **Ref2VA 的确定性 Plan + Render**（唯一已安装的 checkpoint），只用模板，文字用桩替代。到这里，即使没有模型参与，这一层也已经能防止 F1–F12。
4. **推理主机的 `Backend`**——将 §5.1 的两条硬规则直接内置在客户端中（`enable_thinking: false`，并对每个响应检查 `content is None`、`finish_reason == "length"`、JSON 解析和 schema 合规性）。F15–F17 是后端问题，因此在后端封装中统一捕获一次，而不是让每个阶段各自处理。
5. **图片的 `AssetCard`**（推理主机），然后是**用于音频的 whisper**（§4.1——音频进入编码器的唯一渠道是文本，因此这不是可选项）。
6. **模式推断（§12）**，包括决策记录和 E8 的已标注需求集合。
7. **LoRA 注册表（§14）**——解析 `howtouse.md`，导入时校验触发词（R49、R56），解析阶段，注入各模式的风格槽位，以及校验器中的 R47/R48。刻意按这个顺序构建：**先写校验规则，再写注入**，这样第一个接入的 LoRA 就有证据证明触发词已进入文本，而不是靠假设。否则 F18 不可见。
8. **HTTP 接口面（§13）**——`POST/PATCH /v1/briefs`、`GET /v1/loras`、三层响应、已预先应用默认值的单次澄清。然后运行 E9，证明 agent 调用者和 UI 调用者得到相同 IR。
9. **E7 和 E1**——这两个实验决定继续投入篇幅优化是否有意义。值得在文字调优之前运行。
10. 安装 FL2VA 权重后，再做 **t2va / i2va / fl2va / l2va**。

UI 有意放在最后，而且不在这个列表里：按 §13，它是第 7 步的客户端；如果构建 UI 需要修改第 7 步，那就说明 API 设计错了。
