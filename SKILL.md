---
name: auto-subtitle
description: Turn any audio or video file into subtitles fully offline on this machine — auto-detect the spoken language among 99 languages, transcribe with word-level timestamps, re-segment into readable cues, translate into Chinese, and emit original+Chinese bilingual SRT/ASS, optionally burned into the video. Use when the user uploads, drops, attaches, or names an audio/video file (mp3, wav, m4a, aac, flac, ogg, opus, wma, mp4, mkv, mov, webm, avi, ts, flv) and wants subtitles, captions, a transcript, 字幕, 听写, or 转写; or says "转字幕", "生成字幕", "做个字幕", "加字幕", "翻译这段音频", or asks what was said in a recording — including when they give no instruction beyond the file itself.
---

# Auto Subtitle

把任意音视频变成字幕。**默认产出「原语言 + 中文」双语字幕**，全程本地推理，不需要任何 API Key。

## 触发

两类都要接住：

1. **零指令**：用户只是丢来一个音频/视频文件，没说干什么 → 那就是要字幕，直接开跑，不要反问。
2. **一句话**：「转字幕」「生成字幕」「做个字幕」「加个字幕」「翻译这段音频」+ 文件。

只有一种情况需要先问：用户明确说要**只有中文**（不要原文行），那就加 `--mono`。其余情况一律按双语默认值跑完再说。

## 环境

三样依赖，都在 PATH 里就能跑：

| 依赖 | 用途 | 确认命令 |
|---|---|---|
| Python 3.10+ | 跑本技能的脚本 | `python --version` |
| babelscribe | 转写引擎（whisper.cpp 封装） | `babelscribe devices` |
| ffmpeg | 音频转码，`--burn` 烧录也用它 | `ffmpeg -version` |

装引擎：`pip install babelscribe`。

**模型不用手动下**：首次转写时 babelscribe 会自动取 `ggml-large-v3-turbo.bin`（约 1.6 GB）到 `~/.babelscribe/models/`。首次跑某个语种、或换 `--accurate` 时还会再下模型，属正常，不要当成卡死。

有 NVIDIA 显卡会自动选 Vulkan 后端，**不用自己编 CUDA**。`babelscribe devices` 会列出所有可用设备并显示 auto 会挑哪个。实测 RTX 3050 Ti Laptop（4 GB VRAM）上 48 秒音频约 20 秒转完，51 分钟直播回放约 13 分钟。

本技能的脚本就在 SKILL.md 同级的 `scripts/` 下。

Windows 上运行时务必带 `$env:PYTHONIOENCODING='utf-8'`，否则中文输出在控制台会乱码。

## 流程

### 第 1 步 · 转写 + 重新切分

```powershell
$env:PYTHONIOENCODING='utf-8'
python scripts/make_subtitles.py "<音视频路径或URL>" --outdir "<输出目录>"
```

直接读 stdout 末尾的 `KEY=VALUE`：

| 键 | 含义 |
|---|---|
| `SOURCE_LANG` | 自动识别出的语言，如 `en` `zh` `ja` |
| `STEM` | 产物基名，后面几步都用它 |
| `CUES_JSON` | 待译包，第 3 步的输入 |
| `SOURCE_SRT` | 原语言字幕（已经切分好，不是原始糊成一坨的版本） |
| `NEEDS_TRANSLATION` | `yes` = 需要翻译成中文；`no` = 原声就是中文 |
| `PUNCTUATION_LOST` | `yes` = 断句质量不够，走第 2 步 |
| `TRANSCRIPT_TXT` | 仅当 `PUNCTUATION_LOST=yes` 时出现，第 2 步的输入 |

**为什么要重新切分**：whisper 的天然 segment 经常一条 7–8 秒、上百字符，直接当字幕会糊满屏幕。脚本用词级时间戳按标点+停顿重切成 ≤20 字（中文）/ ≤48 字符（英文）的短句。

### 第 2 步 · 中文/日文原声补标点（仅当 `PUNCTUATION_LOST=yes`）

whisper 对中日文只**零星**吐标点，剩下靠停顿硬切。中文会切出「转成文 / 字」；日文更糟——日语没有词间空格，whisper 的段落又首尾相接，连停顿都找不到，结果每条字幕都卡在正好 20 字处硬切，切出「申 / し訳ない」「くま / るさん」这种东西。修法是让字幕落到真正的标点上：

1. 读 `TRANSCRIPT_TXT`（一整段无标点纯文本）。
2. **只插入标点，一个字都不要改。** 包括听起来明显错的字——ASR 把「机翻」听成「击翻」、把「词级」听成「自己」、把「おやすみ」听成「おやすい」，都照原样保留。改字会让重新对齐失败，标点就贴不回去了。
3. 写成 `<STEM>.punct.txt`（UTF-8）。
4. 带 `--punct-file` 重跑第 1 步（会自动复用 `<STEM>.raw.json`，**秒级完成，不会重新转写**）：

```powershell
python scripts/make_subtitles.py "<输入>" --outdir "<输出目录>" --punct-file "<STEM>.punct.txt"
```

**标点密度是这一步唯一真正重要的指标。** 字幕上限是 20 字，所以标点必须比书面语更密：**每 8～18 字就要有一个「、」或「。」**。密度不够，字幕照样硬切——同一份 51 分钟日语音频实测：

| 标点密度 | 条数 | 收在标点上 |
|---|---|---|
| 不补 | 821 | 25% |
| ~32 字/标点（太稀） | 1005 | 76% |
| 8～18 字/标点 | 1081 | **98%** |

「、」用在词组边界、助词后的小停顿、口语换气处——为字幕断行服务，比书面日语用得更勤是对的，这不是把文本改坏了。另外只能用 `。 、 ！ ？ …`：`「」` 这类括号不在脚本的标点表里，会被直接丢掉。

补完标点后，`SOURCE_SRT` 里每条字幕都会落在标点上。若输出 `WARN: ... 对齐失败率 NN%`，说明改动了原字，回去只补标点重试。

**长音频怎么补**：51 分钟音频的全文有 14000+ 字，一次补不完。按 ~1500 字切块，每块交一个并行 agent（附前后各 200 字上下文，但只输出本块），最后**用脚本校验「去掉所有标点后与原文逐字符相等」再拼接**。这个校验不能省，也不能信 agent 的自述——实测 10 块里有 2 块嘴上说「已逐字符校验通过」、实际丢了字或改了字，全靠这一步抓出来。

**改密度重跑很便宜**：因为 `raw.json` 会复用，密度的试错成本接近零。第一次补完发现只有 76% 收在标点上时，直接重补一遍就好，不要将就。

### 第 3 步 · 翻译（仅当 `NEEDS_TRANSLATION=yes`）

读 `CUES_JSON`，里面是 `{"cues": [{"id":1,"start":...,"end":...,"text":"..."}]}`。

- **先通读整段、按整句理解，再把中文分配到各条 cue 上。** 逐条独立翻译会把跨条的一句话翻断（比如 `replacing people.` 单看会译成「取代人们」而丢掉上文）。
- 每条中文控制在 **16 字以内**，且与这条 cue 的时长匹配（中文阅读速度约 4–5 字/秒，1.2 秒的 cue 塞 18 个字观众读不完）。
- 译文写成 `<STEM>.zh.json`，`{"1": "第一条译文", "2": "..."}`，id 必须与 cues 对得上。
- 术语前后一致；专有名词保留原文或按通行译法。

### 第 4 步 · 合成产物

```powershell
python scripts/merge_bilingual.py "<STEM>.cues.json" "<STEM>.zh.json"
```

可选参数：

- `--mono` —— 只留译文，不叠原文（用户要纯中文字幕时）
- `--order zh-first` —— 中文放上面（默认原文在上）
- `--burn "<视频路径>"` —— 把双语字幕烧进画面，输出 `<STEM>.bilingual.burned.mp4`。**只在用户给了视频、且确实需要硬字幕时才烧**，烧录很慢且不可逆。

产物：

- `<STEM>.bilingual.srt` —— 双语 SRT，任何播放器/平台通用
- `<STEM>.bilingual.ass` —— 中文大、原文小，适合烧录或专业播放器

### 第 5 步 · 汇报

跑完给一句话结论 + 关键数字（语言、时长、条数、产物路径）。挑 2–3 条成品字幕贴出来让用户直观看质量。不要贴大段日志。

## 常用开关

| 场景 | 参数 |
|---|---|
| 音频嘈杂、口音重、术语多 | `--accurate`（换 large-v3 + beam search，慢约 2–3 倍但更准） |
| 自动识别语种出错 | `--lang zh`（或 `en` `ja` `th` …） |
| 想要更短/更长的字幕 | `--max-chars 16`（中文按「字」算） |
| 视频链接 | 直接传 URL，脚本内部走 yt-dlp |
| 换标点/换切分策略重跑 | `--punct-file <文件>`（自动复用 `<STEM>.raw.json`，秒级）或显式 `--from-raw <STEM>.raw.json` |

## 坑

- **不要**用 babelscribe 自带的 `.srt` 输出当成品——它一条就是 whisper 的一个 segment，没有长度控制。本技能的脚本已经重切好了。
- **不要**为了「顺手改错字」去动转写文本。要改就改翻译那一层。
- 纯音乐/静音文件会返回 `ERROR: 转写结果为空`，这不是 bug，如实告诉用户。
- 长音频（>30 分钟）第 3 步的 cues 会很多，分批翻译但保持 id 对应，别漏条。**并行 agent 报 `ok` 不等于文件真的写出来了**——实测 20 个翻译批次里 1 个返回 `status: ok` 却没落盘、1 个返回 null。收尾前必须用脚本核对「每个 cue id 都有译文」，不要信 agent 自述。
- 不要用 `python -c "..."` 在 PowerShell 里跑带引号的脚本，引号会被吃掉导致 `SyntaxError`。写成 `.py` 文件再跑。
- 首次跑某语种/换 `--accurate` 会额外下载模型，耗时属正常。

## 自检

改过脚本后，用 `examples/` 下的固定素材回归：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "examples\gen_fixtures.ps1"
python scripts/make_subtitles.py "examples\audio\zh.wav" --outdir "examples\out"
python scripts/make_subtitles.py "examples\audio\en.wav" --outdir "examples\out"
```

`examples\zh.txt` / `examples\en.txt` 由 Windows SAPI 念成 `examples\audio\zh.wav` / `en.wav`（需要中文语音 `Microsoft Huihui Desktop`，中文版 Windows 自带）。期望值：

- 中文：未补标点 9 条、其中约 6 条末尾无标点（`PUNCTUATION_LOST=yes`）；按上面补标点后 10 条、**全部以标点收尾**
- 英文：19 条、最长 ≤48 字符
