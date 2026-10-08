#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make_subtitles.py — 任意音频/视频 → 干净切分的原始语言字幕 + 待译包

引擎：babelscribe（whisper.cpp + Vulkan/CUDA/Metal/CPU，99 种语言自动识别）

切分策略（两种模式自动切换，取决于转写结果里有没有标点）：
  * 标点模式（英语等）：优先在句末标点收尾，其次句内标点，实在没有就找最大停顿
  * 停顿模式（中文/日文等）：whisper 的 large-v3-turbo 对中文只**零星**输出标点（实测一份 126 字音频里前 5 段全无、后 3 段才有），
    只能靠字符间停顿 + 字数上限来断句。此时会在允许的字数窗口内挑「最大停顿」处切，
    而不是傻切在第 N 个字。

用法:
    python make_subtitles.py <媒体文件或URL> [选项]

选项:
    --outdir DIR      产物目录（默认与输入同目录）
    --lang CODE       auto(默认) 或 en/ja/th/... 强制语种
    --accurate        更慢更准（large-v3 + beam search）
    --max-chars N     每条字幕硬上限（默认：中日韩 20 字，其它 48 字符）
    --soft-min N      软断点（逗号类）触发所需的最小字数，默认 8
    --max-dur SEC     单条字幕最长秒数，默认 6.0
    --min-dur SEC     单条字幕最短秒数，默认 0.8
    --gap SEC         停顿断句阈值（默认 0.7；无标点语言自动降到 0.20）
    --keep-raw        保留 babelscribe 的原始 .raw.json/.raw.srt

产物:
    <stem>.<lang>.srt       原始语言字幕（已重新切分）
    <stem>.cues.json        待译包 {"meta":..., "cues":[{"id","start","end","text"}]}
    <stem>.raw.json         babelscribe 原始输出（--keep-raw 时保留）
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# ---------------------------------------------------------------- 常量

HARD_PUNCT = "。！？!?…."          # 句末：命中即断（ASCII 句点必列，否则英文永远不断句）
SOFT_PUNCT = "，、；,;：:"          # 句内：够长才断
ALL_PUNCT = HARD_PUNCT + SOFT_PUNCT

# 无空格分词的语言：字幕条要短，且大概率没有标点
NO_SPACE_LANGS = {"zh", "ja", "ko", "yue", "th", "lo", "my", "km"}
# 本身就是中文，双语字幕无意义
CJK_NATIVE = {"zh", "yue"}

CJK_CHAR = re.compile(
    r"[\u1100-\u11ff\u2e80-\u303f\u3040-\u30ff\u3130-\u318f\u3400-\u4dbf"
    r"\u4e00-\u9fff\ua960-\ua97f\uac00-\ud7ff\uf900-\ufaff\ufe30-\ufe4f\uff00-\uff60]"
)
SPACE_BEFORE_PUNCT = re.compile(r"\s+([,.;:!?%)\]}，。；：！？、）】」》])")
SPACE_AFTER_OPEN = re.compile(r"([(\[{（【「《])\s+")


def width(s: str) -> int:
    """显示宽度：全角算 2、半角算 1，用于统一的字数上限判定。"""
    return sum(2 if CJK_CHAR.match(c) else 1 for c in s)


def is_url(s: str) -> bool:
    return s.lower().startswith(("http://", "https://"))


def is_cjk(ch: str) -> bool:
    return bool(ch) and bool(CJK_CHAR.match(ch))


def join_text(ws) -> str:
    """把词拼回人看的文本：合并空格、清掉标点前的空格。"""
    s = "".join(w[0] for w in ws)
    s = re.sub(r"[ \t]+", " ", s).strip()
    s = SPACE_BEFORE_PUNCT.sub(r"\1", s)
    s = SPACE_AFTER_OPEN.sub(r"\1", s)
    return s.strip()


def smart_join(a: str, b: str) -> str:
    """合并两段文本，中日文之间不加空格。"""
    if not a:
        return b
    if not b:
        return a
    if is_cjk(a[-1]) or is_cjk(b[0]):
        return a + b
    return a + " " + b


# ---------------------------------------------------------------- 下载（可选）

def download(url: str, workdir: Path) -> Path:
    exe = shutil.which("yt-dlp")
    if not exe:
        raise SystemExit("输入是 URL 但找不到 yt-dlp，请先 pip install -U yt-dlp")
    print(f"[1/4] yt-dlp 下载音频: {url}", flush=True)
    tpl = str(workdir / "%(title).120B.%(ext)s")
    cmd = [exe, "-x", "--audio-format", "m4a", "--audio-quality", "5",
           "--no-playlist", "-o", tpl, "--print", "after_move:filepath", url]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode:
        raise SystemExit(f"yt-dlp 失败:\n{(p.stderr or '')[-1500:]}")
    cand = [Path(l.strip()) for l in (p.stdout or "").splitlines() if l.strip()]
    cand = [c for c in cand if c.is_file()]
    if cand:
        return cand[-1]
    got = sorted((p for p in workdir.glob("*") if p.is_file()),
                 key=lambda x: x.stat().st_mtime, reverse=True)
    if not got:
        raise SystemExit("yt-dlp 没有产出文件")
    return got[0]


# ---------------------------------------------------------------- 转写

def run_babelscribe(media: Path, base: Path, lang: str, accurate: bool) -> tuple[dict, Path]:
    """调 babelscribe 出 JSON。base 必须以 .json 结尾——babelscribe 内部用
    base.with_suffix(ext) 生成产物，只有这样才会落成 <stem>.raw.json / <stem>.raw.srt。"""
    from babelscribe.api import transcribe_file  # 延迟导入，缺包时报错更清楚

    print(f"[2/4] babelscribe 转写中（{'large-v3 精确模式' if accurate else 'large-v3-turbo'}）...", flush=True)
    transcribe_file(src=media, lang=lang, accurate=accurate, formats=["json", "srt"], out=base,
                    log=lambda m: print(f"      {m}", flush=True))
    if not base.is_file():
        raise SystemExit(f"babelscribe 未产出 JSON: {base}")
    return json.loads(base.read_text(encoding="utf-8", errors="replace")), base


# ---------------------------------------------------------------- 切分

def flatten(segments: list[dict]) -> list[tuple[str, float, float]]:
    """把 whisper 的 segment 拍平成一个 (词, 起, 止) 流。"""
    words: list[tuple[str, float, float]] = []
    for s in segments:
        for w in s.get("words") or []:
            try:
                txt, a, b = w[0], float(w[1]), float(w[2])
            except (TypeError, ValueError, IndexError):
                continue
            if txt and txt.strip():
                words.append((txt, a, b))
    return words


def stream_has_punct(words) -> bool:
    """转写结果里到底有没有标点。中文 turbo 模型的标点很稀疏，见 punct_spacing。"""
    return any(w[0].strip() and w[0].strip()[-1] in ALL_PUNCT for w in words)


def punct_spacing(words) -> float:
    """平均每多少个字符才出现一个句末标点；inf = 一个都没有。

    用途是判断标点可不可信：whisper 对中文只在部分句子上吐标点（实测一份 126 字
    音频里前 5 段全无标点、后 3 段有），此时「有标点」不等于「能靠标点断句」。
    """
    chars = sum(len(w[0].strip()) for w in words)
    hard = sum(1 for w in words
               if (s := w[0].strip()) and s[-1] in HARD_PUNCT)
    return chars / hard if hard else float("inf")


def is_punct_token(t: str) -> bool:
    s = t.strip()
    return bool(s) and all(c in ALL_PUNCT for c in s)


def inject_punct(words, full_text: str) -> tuple[list, int]:
    """把一份「补过标点」的全文与词级时间戳重新对齐，并把该词后面的标点接到
    它文本的末尾。这样下游 build_cues 不用改一行就能按标点断句。

    两个必须做对的细节：
    1. 在「只留字母数字」的归一化空间里找词。半角/全角逗号、模型自己吐的句号
       都会让直接 find() 失配（实测失配率 27%）。原字不变，标点差异不影响匹配。
    2. 词的现有尾部标点要先摘掉，再用补标点文本里的标点替换。否则模型已经吐了
       「号。」而我们又补一个「。」，就会得到「标点符号。。」。
    """
    norm_chars, pos_map = [], []
    for idx, ch in enumerate(full_text):
        if ch.isalnum():
            norm_chars.append(ch)
            pos_map.append(idx)
    norm = "".join(norm_chars)

    out, cur, misses = [], 0, 0
    for tok, a, b in words:
        base = tok.rstrip(ALL_PUNCT)                 # 摘掉尾部标点，准备重新贴
        key = "".join(c for c in base if c.isalnum())
        if not key:
            # 纯标点 token 直接丢掉。whisper 会把 "," 当成独立 token（"累" + ","），
            # 而补标点文本里的「，」已经被上一个词收走了，留着就会得到「累，,」。
            # 补标点文本是标点的唯一权威来源。
            continue
        idx = norm.find(key, cur)
        if idx < 0:                                  # 对不上就原样保留，且不推进游标
            misses += 1
            out.append((tok, a, b))
            continue
        cur = idx + len(key)
        end = pos_map[cur - 1] + 1                   # 该词末字在原文里的下一个位置
        punct = ""
        while end < len(full_text) and full_text[end] in ALL_PUNCT:
            punct += full_text[end]
            end += 1
        out.append((base + punct, a, b))
    return out, misses


def build_cues(words, limit: int, soft_min: int, max_dur: float, min_dur: float,
               gap: float | None = None) -> list[dict]:
    """把词流切成字幕条。这是整个技能质量的关键——whisper 的天然 segment 经常
    长达 7-8 秒、上百字符，直接当字幕会糊在屏幕上。"""
    n = len(words)
    # 停顿兜底始终生效：标点优先，但窗口里没有标点时必须退到「最大停顿」，
    # 否则就会在正好第 N 个字上硬切，出现「转成文|字」这种断在词中间的字幕。
    # 实测中文句末停顿 0.25-0.45s、字间 0-0.17s，故 0.20 是可用阈值。
    gap_pref = gap if gap is not None else 0.20
    min_w = max(8, limit // 3)          # 一条字幕至少这么宽，否则不考虑断
    min_tail = max(8, limit // 3)       # 短于这个宽度的尾条会被并回上一条
    gap_min_w = max(min_w, int(limit * 0.6))   # 停顿断句额外要求本条已装够 6 成

    wpref = [0] * (n + 1)
    for idx, (t, _, _) in enumerate(words):
        wpref[idx + 1] = wpref[idx] + width(t)

    cues: list[dict] = []
    i = 0
    while i < n:
        start_t = words[i][1]

        # --- 字数 / 时长允许吃到的最远位置（标点可以少量超字）
        curw, j, max_end = 0, i, i
        while j < n:
            w = width(words[j][0])
            if j > i and curw + w > limit and not is_punct_token(words[j][0]):
                break
            if j > i and (words[j][2] - start_t) > max_dur:
                break
            curw += w
            j += 1
            max_end = j
        if max_end <= i:
            max_end = i + 1

        # --- 句末标点：取窗口内最靠右的一个（且本条已装够 min_w，避免 Mr. 之类
        #     缩写把字幕切成极短的一条）
        hard_at = None
        for k in range(i + 1, max_end + 1):
            if (wpref[k] - wpref[i]) < min_w:
                continue
            s = words[k - 1][0].strip()
            if s and s[-1] in HARD_PUNCT:
                # 别把小数点切开：句点后紧跟数字就不算句末
                if s[-1] == "." and k < n and words[k][0].strip()[:1].isdigit():
                    continue
                hard_at = k
        # --- 句内标点：取最靠右且已够长的一个
        soft_at = None
        if hard_at is None:
            for k in range(i + 1, max_end + 1):
                s = words[k - 1][0].strip()
                if s and s[-1] in SOFT_PUNCT and (wpref[k] - wpref[i]) >= soft_min:
                    soft_at = k
        # --- 都没有：在窗口里挑「最大停顿」处切，而不是傻切在第 N 个字
        gap_at, gap_val = None, 0.0
        if hard_at is None and soft_at is None:
            for k in range(i + 1, max_end):
                if (wpref[k] - wpref[i]) < gap_min_w:
                    continue
                g = words[k][1] - words[k - 1][2]
                if g > gap_val:
                    gap_val, gap_at = g, k

        if hard_at is not None:
            end = hard_at
        elif soft_at is not None:
            end = soft_at
        elif gap_at is not None and gap_val >= gap_pref:
            end = gap_at
        else:
            end = max_end

        cur = words[i:end]
        t = join_text(cur)
        if t:
            cues.append({"start": round(cur[0][1], 3),
                         "end": round(max(cur[-1][2], cur[0][1] + 0.3), 3),
                         "text": t})
        i = end

    # --- 合并孤儿尾条（避免出现 "people." 这种只有一两个词的字幕）
    merged: list[dict] = []
    for c in cues:
        if merged and width(c["text"]) < min_tail:
            w_new = width(merged[-1]["text"]) + width(c["text"]) + 1
            # 上限卡在 limit*1.15：之前放到 1.35 会合出 60 字符的超长条，等于白设 limit
            if w_new <= limit * 1.15:
                merged[-1]["text"] = smart_join(merged[-1]["text"], c["text"])
                merged[-1]["end"] = c["end"]
                continue
        merged.append(c)
    cues = merged

    # --- 去重叠：后一条不能早于前一条结束
    for k in range(1, len(cues)):
        if cues[k]["start"] < cues[k - 1]["end"]:
            cues[k - 1]["end"] = max(cues[k - 1]["start"] + 0.3,
                                     min(cues[k - 1]["end"], cues[k]["start"]))
    # --- 补最短时长：往右撑，但不吃掉下一条的时间
    for k, c in enumerate(cues):
        if c["end"] - c["start"] < min_dur:
            room = cues[k + 1]["start"] if k + 1 < len(cues) else c["start"] + min_dur
            c["end"] = round(max(c["end"], min(c["start"] + min_dur, room)), 3)
            if c["end"] <= c["start"]:
                c["end"] = round(c["start"] + 0.3, 3)

    for k, c in enumerate(cues, 1):
        c["id"] = k
    return cues


# ---------------------------------------------------------------- 输出

def fmt_ts(t: float, sep: str = ",") -> str:
    ms = int(round(max(0.0, t) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def write_srt(cues: list[dict], path: Path) -> None:
    parts = [f"{c['id']}\n{fmt_ts(c['start'])} --> {fmt_ts(c['end'])}\n{c['text']}\n\n" for c in cues]
    # utf-8-sig：Windows 播放器对无 BOM 的 UTF-8 中文容易乱码
    path.write_text("".join(parts), encoding="utf-8-sig")


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="音频/视频 → 原始语言字幕 + 待译包")
    ap.add_argument("input", help="媒体文件路径，或视频/音频 URL")
    ap.add_argument("--outdir", default=None, help="产物目录，默认与输入同目录")
    ap.add_argument("--lang", default="auto", help="auto(默认) 或 en/ja/th/...")
    ap.add_argument("--accurate", action="store_true", help="large-v3 + beam search，更慢更准")
    ap.add_argument("--max-chars", type=int, default=None, help="每条字幕硬上限")
    ap.add_argument("--soft-min", type=int, default=8, help="软断点最小字数")
    ap.add_argument("--max-dur", type=float, default=6.0, help="单条最长秒数")
    ap.add_argument("--min-dur", type=float, default=0.8, help="单条最短秒数")
    ap.add_argument("--gap", type=float, default=None, help="停顿断句阈值（秒）")
    ap.add_argument("--keep-raw", action="store_true", help="保留 babelscribe 原始输出")
    ap.add_argument("--from-raw", default=None,
                    help="复用已有的 <stem>.raw.json 跳过转写（补标点重跑时自动启用）")
    ap.add_argument("--punct-file", default=None,
                    help="一份给无标点原声补好标点的纯文本（只插标点、不改字）；"
                         "给了就按标点重新断句，中文断句质量显著提升")
    args = ap.parse_args()

    tmpdir = None
    try:
        # ---- 0. 拿文件
        if is_url(args.input):
            tmpdir = tempfile.mkdtemp(prefix="autosub_")
            media = download(args.input, Path(tmpdir))
        else:
            media = Path(args.input).expanduser()
            if not media.is_file():
                print(f"ERROR: 找不到文件: {media}", flush=True)
                return 2
        media = media.resolve()

        outdir = Path(args.outdir).expanduser().resolve() if args.outdir else media.parent
        outdir.mkdir(parents=True, exist_ok=True)
        stem = media.stem
        raw_json = outdir / f"{stem}.raw.json"
        raw_srt = outdir / f"{stem}.raw.srt"

        # ---- 1+2. 转写（或复用已有 raw.json —— 补标点重跑时不必再转一遍）
        reuse = None
        if args.from_raw:
            reuse = Path(args.from_raw).expanduser()
            if not reuse.is_file():
                print(f"ERROR: --from-raw 不存在: {reuse}", flush=True)
                return 4
        elif args.punct_file and raw_json.is_file():
            # 补标点重跑是最常见的二次调用，自动复用转写结果，省掉整段转写时间
            reuse = raw_json
            print(f"      复用已有转写 {reuse.name}，跳过转写（换标点免费重切）", flush=True)

        t0 = time.time()
        if reuse:
            try:
                data = json.loads(reuse.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                print(f"ERROR: 读取 {reuse} 失败: {e}", flush=True)
                return 4
            raw_json = reuse
            took = 0.0
            transcribed = False
        else:
            data, raw_json = run_babelscribe(media, raw_json, args.lang, args.accurate)
            took = time.time() - t0
            transcribed = True
        meta = data.get("meta") or {}
        lang = (meta.get("lang") or args.lang or "auto").lower()
        segs = data.get("segments") or []

        words = flatten(segs)
        if not words:
            print("ERROR: 转写结果为空，可能是纯音乐/静音文件", flush=True)
            return 3

        # ---- 3a. 可选：拿 agent 补好标点的全文与词级时间戳重新对齐
        punct_used, punct_miss = None, 0
        if args.punct_file:
            pf = Path(args.punct_file).expanduser()
            if not pf.is_file():
                print(f"ERROR: --punct-file 不存在: {pf}", flush=True)
                return 4
            # 去掉所有空白再对齐：中文本来没空格，英文也能靠 strip 后的词找到，
            # 且紧邻的标点不会被空格挡住
            full_text = re.sub(r"\s+", "", pf.read_text(encoding="utf-8"))
            words, punct_miss = inject_punct(words, full_text)
            punct_used = pf
            ratio = punct_miss / max(1, len(words))
            if ratio > 0.15:
                print(f"WARN: 补标点全文与转写结果对齐失败率 {ratio:.0%}"
                      f"（{punct_miss}/{len(words)} 词没找到）。"
                      f"补标点时请只插入标点、不要改动任何原字。", flush=True)
            elif punct_miss:
                print(f"      对齐补标点：{len(words) - punct_miss}/{len(words)} 词命中", flush=True)

        # ---- 3. 重新切分
        limit = args.max_chars
        if limit is None:
            limit = 20 * 2 if lang in NO_SPACE_LANGS else 48
        elif lang in NO_SPACE_LANGS:
            limit = limit * 2          # 用户给的是「字」数，内部统一按显示宽度算

        has_punct = stream_has_punct(words)
        spacing = punct_spacing(words)
        cues = build_cues(words, limit, args.soft_min, args.max_dur, args.min_dur, args.gap)
        if not cues:
            print("ERROR: 切分后没有字幕条", flush=True)
            return 3

        src_srt = outdir / f"{stem}.{lang}.srt"
        write_srt(cues, src_srt)

        # ---- 4. 待译包
        need = lang not in CJK_NATIVE
        # 判据不看「有没有标点」，而看「断出来的字幕有没有落在标点上」：whisper 对中文
        # 会零星吐几个句号，但大部分字幕仍是被停顿硬切开的，那种断句会断在词中间。
        unmarked = sum(1 for c in cues
                       if not (c["text"].strip() and c["text"].strip()[-1] in ALL_PUNCT))
        punct_lost = lang in NO_SPACE_LANGS and unmarked > 0.35 * len(cues)

        # 标点不可靠时吐一份纯文本给 agent 补标点，补完用 --punct-file 再跑一次
        transcript = None
        if punct_lost:
            transcript = outdir / f"{stem}.transcript.txt"
            transcript.write_text("".join(t for t, _, _ in words), encoding="utf-8")
        bundle = {
            "meta": {
                "source_file": str(media),
                "lang": lang,
                "device": meta.get("device"),
                "model": meta.get("model"),
                "engine": meta.get("tool"),
                "cue_count": len(cues),
                "needs_translation": need,
                "punctuation_lost": punct_lost,
                "target_lang": "zh",
            },
            "cues": [{"id": c["id"], "start": c["start"], "end": c["end"], "text": c["text"]} for c in cues],
        }
        bundle_path = outdir / f"{stem}.cues.json"
        bundle_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=1), encoding="utf-8")

        # raw.json 是转写缓存，永远保留：补标点重跑要复用它，否则长音频每改一次标点
        # 就得再等一遍完整转写（实测 51 分钟音频要 13 分钟）。raw.srt 是没重切过的
        # 半成品，默认删掉。
        if transcribed and not args.keep_raw:
            raw_srt.unlink(missing_ok=True)

        # ---- 报告
        avg = sum(len(c["text"]) for c in cues) / len(cues)
        print("", flush=True)
        print(f"[3/4] 语言={lang}  设备={meta.get('device')}", flush=True)
        mode = "标点断句" if spacing <= 60 else ("标点+停顿" if has_punct else "停顿断句")
        print(f"      转写 {took:.1f}s  {len(segs)} 个 whisper 段 -> {len(cues)} 条字幕（{mode}）", flush=True)
        print(f"      平均 {avg:.1f} 字符/条，最长 {max(len(c['text']) for c in cues)} 字符", flush=True)
        print(f"[4/4] 产物:", flush=True)
        print(f"      原始字幕 {src_srt}", flush=True)
        print(f"      待译包   {bundle_path}", flush=True)
        if punct_lost:
            print(f"      注意：whisper 对 {lang} 只零星输出标点，字幕会切在词中间，建议补标点",
                  flush=True)
            print(f"      待补标点 {transcript}"
                  f"（{unmarked}/{len(cues)} 条字幕末尾无标点，补完用 --punct-file 重跑）", flush=True)
        print("", flush=True)
        print(f"NEEDS_TRANSLATION={'yes' if need else 'no'}", flush=True)
        print(f"PUNCTUATION_LOST={'yes' if punct_lost else 'no'}", flush=True)
        print(f"SOURCE_LANG={lang}", flush=True)
        print(f"CUES_JSON={bundle_path}", flush=True)
        print(f"SOURCE_SRT={src_srt}", flush=True)
        print(f"STEM={outdir / stem}", flush=True)
        if transcript:
            print(f"TRANSCRIPT_TXT={transcript}", flush=True)
        if punct_used:
            print(f"PUNCT_FILE={punct_used}", flush=True)
        return 0
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
