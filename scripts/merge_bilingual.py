#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""merge_bilingual.py — 原文 + 中文译文 → 双语 SRT / ASS（可选烧录）

用法:
    python merge_bilingual.py <stem>.cues.json <译文.json> [选项]

译文 JSON 支持两种写法（id 与 cues.json 里的 id 对应）：
    {"1": "第一句译文", "2": "第二句译文"}
    {"translations": {"1": "...", "2": "..."}}

选项:
    --outdir DIR       产物目录（默认与 cues.json 同目录）
    --mono             单语模式：只输出提供的文本（用于中文原声补标点，不做双语）
    --order MODE       srt 里谁在上：original-first(默认) | zh-first
    --no-ass           不生成 ASS
    --ass-font NAME    ASS 字体名（默认 Microsoft YaHei）
    --burn VIDEO       把双语 ASS 烧进这个视频，输出 <stem>.bilingual.burned.mp4
    --crf N            烧录质量，默认 20

产物:
    <stem>.bilingual.srt    双语 SRT（任何播放器/上传平台通用）
    <stem>.bilingual.ass    双语 ASS（中文大、原文小，适合烧录）
    <stem>.bilingual.burned.mp4  仅在 --burn 时
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


def fmt_srt(t: float) -> str:
    ms = int(round(max(0.0, t) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def fmt_ass(t: float) -> str:
    cs = int(round(max(0.0, t) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def ass_escape(s: str) -> str:
    """ASS 正文转义：换行 -> \\N，花括号会开启 override block 必须换掉。"""
    s = s.replace("\\", "＼").replace("{", "（").replace("}", "）")
    return re.sub(r"\s*\n\s*", r"\\N", s.strip())


def load_translations(path: Path) -> dict[str, str]:
    data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    if isinstance(data, dict) and "translations" in data:
        data = data["translations"]
    if isinstance(data, list):
        # 也容忍 [{"id":1,"text":"..."}] 这种写法
        out = {}
        for it in data:
            if isinstance(it, dict) and "id" in it:
                out[str(it["id"])] = str(it.get("text") or it.get("zh") or "").strip()
        return out
    if not isinstance(data, dict):
        raise SystemExit("译文 JSON 格式不对：应为 {id: 译文} 映射")
    return {str(k): str(v).strip() for k, v in data.items()}


def main() -> int:
    ap = argparse.ArgumentParser(description="原文 + 中文译文 → 双语 SRT/ASS")
    ap.add_argument("cues_json")
    ap.add_argument("translations")
    ap.add_argument("--outdir", default=None)
    ap.add_argument("--mono", action="store_true",
                    help="单语模式：只输出译文/补标点后的文本，不叠原文")
    ap.add_argument("--order", choices=["original-first", "zh-first"], default="original-first")
    ap.add_argument("--no-ass", action="store_true")
    ap.add_argument("--ass-font", default="Microsoft YaHei")
    ap.add_argument("--burn", default=None, help="要烧录的视频路径")
    ap.add_argument("--crf", type=int, default=20)
    args = ap.parse_args()

    cues_path = Path(args.cues_json).expanduser().resolve()
    if not cues_path.is_file():
        print(f"ERROR: 找不到 {cues_path}", flush=True)
        return 2
    bundle = json.loads(cues_path.read_text(encoding="utf-8", errors="replace"))
    cues = bundle.get("cues") or []
    if not cues:
        print("ERROR: cues.json 里没有 cues", flush=True)
        return 2

    zh = load_translations(Path(args.translations).expanduser().resolve())

    missing = [c["id"] for c in cues if not zh.get(str(c["id"]))]
    if missing:
        print(f"WARN: {len(missing)} 条没有译文，将只显示原文（id: {missing[:10]}...）", flush=True)

    outdir = Path(args.outdir).expanduser().resolve() if args.outdir else cues_path.parent
    outdir.mkdir(parents=True, exist_ok=True)
    stem = cues_path.name[: -len(".cues.json")] if cues_path.name.endswith(".cues.json") else cues_path.stem

    # ---- SRT
    tag = "final" if args.mono else "bilingual"
    srt_path = outdir / f"{stem}.{tag}.srt"
    zh_first = args.order == "zh-first"
    parts = []
    for c in cues:
        t = zh.get(str(c["id"]), "")
        if args.mono:
            lines = [t or c["text"]]
        else:
            lines = [t, c["text"]] if (t and zh_first) else ([c["text"], t] if t else [c["text"]])
        parts.append(f"{c['id']}\n{fmt_srt(c['start'])} --> {fmt_srt(c['end'])}\n" + "\n".join(lines) + "\n\n")
    srt_path.write_text("".join(parts), encoding="utf-8-sig")
    print(f"{'单语' if args.mono else '双语'} SRT -> {srt_path}", flush=True)

    # ---- ASS（双语：中文在上更大、原文在下更小；单语：只留底部一行）
    ass_path = None
    if not args.no_ass:
        ass_path = outdir / f"{stem}.{tag}.ass"
        font = args.ass_font
        if args.mono:
            styles = (f"Style: M,{font},58,&H00FFFFFF,&H000000FF,&H00000000,&H7F000000,"
                      f"0,0,0,0,100,100,0,0,1,3,1,2,70,70,66,1\n")
        else:
            styles = (f"Style: ZH,{font},58,&H00FFFFFF,&H000000FF,&H00000000,&H7F000000,"
                      f"0,0,0,0,100,100,0,0,1,3,1,2,70,70,132,1\n"
                      f"Style: SRC,{font},34,&H00DCDCDC,&H000000FF,&H00000000,&H7F000000,"
                      f"0,0,0,0,100,100,0,0,1,2,0,2,70,70,66,1\n")
        head = f"""[Script Info]
Title: {stem}
ScriptType: v4.00+
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
{styles}
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
        ev = []
        for c in cues:
            a, b = fmt_ass(c["start"]), fmt_ass(c["end"])
            t = zh.get(str(c["id"]), "")
            if args.mono:
                ev.append(f"Dialogue: 0,{a},{b},M,,0,0,0,,{ass_escape(t or c['text'])}")
            else:
                if t:
                    ev.append(f"Dialogue: 0,{a},{b},ZH,,0,0,0,,{ass_escape(t)}")
                ev.append(f"Dialogue: 0,{a},{b},SRC,,0,0,0,,{ass_escape(c['text'])}")
        ass_path.write_text(head + "\n".join(ev) + "\n", encoding="utf-8")
        print(f"{'单语' if args.mono else '双语'} ASS -> {ass_path}", flush=True)

    # ---- 可选烧录
    if args.burn:
        video = Path(args.burn).expanduser().resolve()
        if not video.is_file():
            print(f"ERROR: 找不到要烧录的视频 {video}", flush=True)
            return 2
        if ass_path is None:
            print("ERROR: --burn 需要 ASS，别同时用 --no-ass", flush=True)
            return 2
        ff = shutil.which("ffmpeg") or "ffmpeg"
        # ffmpeg 的 subtitles/ass 滤镜在 Windows 上要转义盘符冒号和反斜杠
        esc = str(ass_path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
        out_mp4 = outdir / f"{stem}.{tag}.burned.mp4"
        cmd = [ff, "-v", "error", "-y", "-i", str(video),
               "-vf", f"ass='{esc}'", "-c:v", "libx264", "-preset", "medium",
               "-crf", str(args.crf), "-c:a", "copy", str(out_mp4)]
        print("烧录中（可能较慢）...", flush=True)
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if p.returncode:
            print(f"ERROR: 烧录失败:\n{(p.stderr or '')[-1500:]}", flush=True)
            return 4
        print(f"烧录视频 -> {out_mp4}", flush=True)

    print(f"DONE cues={len(cues)} translated={len(cues) - len(missing)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
