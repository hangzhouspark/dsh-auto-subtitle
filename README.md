# dsh-auto-subtitle

> 丢一个音视频文件过来，一句话都不用说 —— 自动出「原语言 + 中文」双语字幕。

**完全本地推理，不需要任何 API Key。** 99 种语言自动识别，Windows + NVIDIA 开箱即用。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## 它解决什么问题

把音频转成字幕，现成工具一抓一大把。但直接拿 whisper 的输出当字幕，成品是这样的：

```
3
00:00:12,480 --> 00:00:20,160
Artificial intelligence is changing how we work, but the biggest gains do not come from replacing people.
```

一条 7.8 秒、113 个字符，糊满整个屏幕。whisper 是按「语音段落」切的，不是按「人能读完的一行」切的。

而中文和日文更麻烦 —— **whisper 对中日文几乎不吐标点**。没有标点就没法断句，只能在第 20 个字处硬切，切出这种东西：

```
字幕生成这件
事，难点从来
不在于把语音
转成文字。
```

日文因为连词间空格都没有，情况最糟：每条字幕都卡在正好 20 字处，`申 / し訳ない`、`くま / るさん` 这种断在词中间的比比皆是。

这个技能做的就是中间那段工程：**用词级时间戳重新切分，让每条字幕都落在真正的标点上。**

## 实测

51 分钟日文直播回放（BV1yQHS6TEuV），RTX 3050 Ti Laptop 4 GB：

| 阶段 | 耗时 |
|---|---|
| 转写（Vulkan 后端） | ~13 分钟 |
| 补标点后重切 | **0.3 秒**（复用 `raw.json`，不重新转写） |

字幕质量随标点密度变化 —— 这是整个项目里最反直觉的一个数据：

| 标点密度 | 字幕条数 | 收在标点上 |
|---|---|---|
| 完全不补 | 821 | 25% |
| ~32 字/标点（太稀） | 1005 | 76% |
| **8～18 字/标点** | **1081** | **98.0%** |

短音频上 48 秒英文约 20 秒转完，出 19 条、平均 37.9 字符、最长 48。

## 安装

### 1. 依赖

```bash
pip install babelscribe      # 转写引擎（whisper.cpp 封装）
```

还需要 **Python 3.10+** 和 **ffmpeg**（在 PATH 里即可）。

模型不用手动下：首次转写时 babelscribe 会自动取 `ggml-large-v3-turbo.bin`（约 1.6 GB）到 `~/.babelscribe/models/`。

有 NVIDIA 显卡会自动选 Vulkan 后端，**不用自己编 CUDA**：

```bash
babelscribe devices
```

### 2. 装成技能

```bash
git clone https://github.com/hangzhouspark/dsh-auto-subtitle
```

然后把整个目录放进你的 skills 目录：

| 平台 | 路径 |
|---|---|
| Windows | `%USERPROFILE%\.dsh\skills\` |
| macOS / Linux | `~/.dsh/skills/` |

最终结构是 `<skills>\auto-subtitle\SKILL.md`。**重启会话**（技能目录在会话启动时扫描）。

> SKILL.md 用的是 `name` + `description` frontmatter 这一通用约定，Claude Code 等其它支持 skills 的 agent 也能直接用，把目录放进对应的 skills 目录即可。

## 使用

**直接丢文件，什么都不用说：**

> 【熟肉】某某直播回放.mp3

技能会自己认出这是要字幕，然后跑完。也可以说一句「转字幕」「生成字幕」「翻译这段音频」+ 文件。

默认产出 **「原语言 + 中文」双语**。只要纯中文的话说一声，会走 `--mono`。

产物：

| 文件 | 内容 |
|---|---|
| `<名字>.bilingual.srt` | 双语 SRT，任何播放器/平台通用 |
| `<名字>.bilingual.ass` | 中文大字、原文小字，适合烧录 |
| `<名字>.bilingual.burned.mp4` | 烧进画面的成片（需要 `--burn`） |
| `<名字>.srt` | 只要原语言字幕 |

### 手动跑

```powershell
$env:PYTHONIOENCODING='utf-8'   # Windows 上必须，否则中文乱码
python scripts/make_subtitles.py "音频.mp3" --outdir "out"
python scripts/merge_bilingual.py "out\音频.cues.json" "out\音频.zh.json"
```

`make_subtitles.py` 支持直接传**视频链接**（内部走 yt-dlp）。

## 工作原理

```
音频/视频
   ↓  ffmpeg 转 16 kHz 单声道 wav
   ↓  whisper.cpp（Vulkan / CUDA / Metal / CPU 自动选）
   ↓  词级时间戳 JSON
   ├─ 切分：标点优先 → 停顿兜底 → 硬上限   ← 核心
   ├─ 中/日文：补标点 → 重新对齐 → 再切一遍
   ↓  翻译成中文（每条 ≤16 字，匹配 cue 时长）
   ↓  合成 SRT / ASS，可选烧录
双语字幕
```

### 切分算法的四步择优

对每条字幕，在「字数上限」和「时长上限」允许的窗口内，按优先级找断点：

1. 窗口内**最靠右的句末标点**（`。！？!?….`）
2. 窗口内最靠右、且本条已经装够 `soft-min` 字的**句内标点**（`，、；,;：:`）
3. 都没有 → 窗口内**最大的停顿**
4. 再没有 → 才切在硬上限处

字数按显示宽度算：全角算 2、半角算 1，中文上限 40（= 20 字）、英文 48 字符。

### 中日文的补标点通路

whisper 对中日文只零星吐标点，所以上面第 3 步会频繁接手，而中文的停顿又不总在句子边界上（「一句话被切 ⟨0.35s⟩ 成三段」—— 停顿在词中间）。

解法是让 agent 给无标点全文补上标点，再**把标点粘回对应的词**：

```
补标点文本  →  归一化（只留字母数字）  →  在原文里逐词定位  →  把标点附加到该词末尾
```

下游的切分算法一行都不用改，就自动按真标点断句了。约束是**一个字都不能改** —— 包括明显听错的字。改字会让对齐失败，标点就贴不回去了。

## 设计决策与踩过的坑

这些都是实测撞出来的，不是推演出来的。

**不要让 whisper 直接出 SRT。** babelscribe 自带 `.srt` 输出，但它一条就是 whisper 的一个 segment，没有长度控制（实测最长 113 字符 / 7.5 秒）。必须自己用词级时间戳重切。

**`HARD_PUNCT` 一开始漏了 ASCII 句点。** 常量写的是 `"。！？!?…"` —— 全角句号有，半角 `.` 没有。结果英文永远不在句号处断句，`replacing people. They come from...` 整句糊在一条里。补上 `.` 之后英文从 18 条变 19 条，每条都收在句末。同时给句末标点加了最小长度门槛，避免 `Mr.`、`U.S.` 切出极短字幕。

**「有标点」不等于「断句断对了」。** 最早的判断是「任一 token 含标点就算标点模式」，但中日文是零星有标点 —— 进了标点模式却找不到标点可断，退化成在正好第 20 字硬切。现在的判据是「**断出来的字幕有没有落在标点上**」：超过 35% 的字幕末尾没有标点，才算真的丢了标点。

**停顿兜底必须始终生效**，不能按「有没有标点」开关。另外要求本条已经装够 6 成字数才允许按停顿断，否则会切出一堆两三个字的碎条。

**文本对齐要在归一化空间里做。** 第一版 `full_text.find(tok)` 有 27% 失配 —— 模型给半角 `,`、补标点文本给全角 `，` 就对不上了。改成只留字母数字再找，并记录每个字符的原始位置；对不上时**不推进游标**（原实现会错误前进，连带毁掉后面所有匹配）。

**标点会翻倍成 `，,`。** whisper 把 `,` 当独立 token 发出，而补标点文本已经把「，」给了前一个词。修法是**纯标点 token 直接丢弃** —— 补标点文本才是标点的唯一权威来源。同理，词的原有尾部标点要先摘掉再贴新的。

**孤儿尾条合并的上限一开始放到了 `1.35 × limit`。** 合出一条 60 字符的字幕，等于 limit 白设。收到 `1.15`。定位手段是 monkeypatch 掉 `smart_join` 打印每次合并 —— 一眼看出超长条是合并产物。

**并行 agent 的「已完成」不能信。** 长音频要切块交给多个 agent 补标点、分批翻译。实测 10 块补标点里有 2 块嘴上说「已逐字符校验通过」、实际丢了字；20 个翻译批次里 1 个返回 `status: ok` 却没写出文件、1 个返回 null。**收尾必须用脚本核对**（去掉标点后与原文逐字符相等 / 每个 cue id 都有译文），不能看 agent 自述。

**`raw.json` 复用把试错成本降到接近零。** 最初 `--punct-file` 重跑会重新转写一遍（51 分钟音频 = 13 分钟），于是「标点密度不够」时人会倾向于将就。现在自动复用原始转写，重切只要 **0.3 秒** —— 76% 不满意就直接重补一遍。

## 目录结构

```
auto-subtitle/
├── SKILL.md                    # 技能说明（给 agent 看的）
├── scripts/
│   ├── make_subtitles.py       # 转写 + 重新切分（核心）
│   └── merge_bilingual.py      # 合成 SRT/ASS、烧录
└── examples/
    ├── zh.txt / en.txt         # 回归素材朗读稿
    └── gen_fixtures.ps1        # 用 Windows SAPI 念成音频
```

### 回归自检

改过脚本后跑一遍（需要中文版 Windows 才有 `Microsoft Huihui Desktop` 语音）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "examples\gen_fixtures.ps1"
python scripts/make_subtitles.py "examples\audio\zh.wav" --outdir "examples\out"
python scripts/make_subtitles.py "examples\audio\en.wav" --outdir "examples\out"
```

期望值：中文未补标点 9 条（约 6 条末尾无标点），补标点后 10 条且**全部以标点收尾**；英文 19 条、最长 ≤48 字符。

## 已知限制

- **中日文的补标点要靠语言模型。** 脚本本身不做这件事 —— 它吐出无标点全文，由 agent 补完再喂回来。所以这个技能在纯脚本环境下只能跑到「停顿兜底」的质量。
- **停顿不等于句读。** 无标点 ASR 的固有上限：词中间的停顿会被误当成句子边界。补标点能解决绝大部分，但不是 100%。
- **转写错字不修。** 转写层刻意保持原样（改字会破坏对齐），ASR 听错的词会原样留在字幕里。
- **翻译是分批交给并行 agent 做的**（每批 ~55 条 + 前文上下文）。跨条连贯性大体保住，但难免有生硬处。
- 少数条目会因为 whisper 的段落本身就切在词中间而接不上。
- 烧录（`--burn`）很慢且不可逆，只在确实需要硬字幕时才用。

## License

[MIT](LICENSE)
