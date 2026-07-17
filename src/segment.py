#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""功能 B：LLM 将 transcript -> 独立问答片段（QA segmentation）。

输出：out/segments/<base>_segments.json
文件名 base（含前缀）从 transcript 文件名解析，绝不硬编码播客名。
优先用 LLM 切分；LLM 失败或限流时回退到基于关键词的启发式切分。
"""

import json
import time
from pathlib import Path

from src.utils import OUT_DIR, SEGMENTS_DIR, load_dotenv, llm_chat, parse_base

NL = chr(10)


def compress_transcript(transcript, max_turns=200, turn_max_chars=50):
    lines = transcript.split(NL)
    turns, in_header = [], True
    for i, line in enumerate(lines):
        if in_header:
            if line.strip() == "---":
                in_header = False
                continue
            continue
        s = line.strip()
        if not s:
            continue
        if s.startswith("[") and ("SpeakerA:" in s or "SpeakerB:" in s):
            content = lines[i + 1].strip() if i + 1 < len(lines) else ""
            sp = "A" if "SpeakerA:" in s else "B"
            ts = s.split("]")[0].replace("[", "") if "]" in s else ""
            truncated = content[:turn_max_chars] + ("..." if len(content) > turn_max_chars else "")
            turns.append("[" + ts + "] Sp" + sp + ": " + truncated)
    if len(turns) > max_turns:
        step = max(1, len(turns) // max_turns)
        turns = turns[::step]
    return NL.join(turns)


SEGMENT_HEADER = ("分析播客压缩版，识别独立问答片段。新问答始于新听众连线"
                  "（老师你好、钱老师、我的问题是等开场）。片头介绍不算片段。\n\n"
                  "输出JSON数组，每项有index,summary,first_line。只输出JSON数组。\n\n")


def segment_transcript(api_key, base_url, model, transcript):
    compressed = compress_transcript(transcript)
    prompt = SEGMENT_HEADER + compressed
    content = llm_chat(api_key, base_url, model,
                       [{"role": "user", "content": prompt}],
                       temperature=0.1, max_tokens=1024)
    js = content.strip()
    if js.startswith("```"):
        s = js.find("[")
        e = js.rfind("]")
        js = js[s:e + 1] if s != -1 and e != -1 else js
    return json.loads(js)


def _base_from_tx(tx_path: Path) -> str:
    return tx_path.stem.replace("_Transcript", "")


def _heuristic_segments(full_lines, vol):
    he = 0
    for j, line in enumerate(full_lines):
        if line.strip() == "---":
            he = j + 1
            break
    boundaries = [he if he else 0]
    for j in range(max(0, he), len(full_lines) - 2):
        line = full_lines[j].strip()
        nl = full_lines[j + 1].strip() if j + 1 < len(full_lines) else ""
        if "SpeakerB:" in line:
            c = line + " " + nl[:40]
            if any(kw in c for kw in ["老师你好", "钱老师", "哈喽", "我的问题是",
                                      "我想问", "你好钱", "你好老师", "连到你", "听到吗"]):
                boundaries.append(j)
    if len(boundaries) <= 1:
        boundaries = [he if he else 0, len(full_lines)]
    else:
        boundaries.append(len(full_lines))
    segments = []
    for bi in range(len(boundaries) - 1):
        seg_text = NL.join(full_lines[boundaries[bi]:boundaries[bi + 1]])
        if len(seg_text.strip()) < 100:
            continue
        segments.append({
            "segment_index": len(segments) + 1,
            "summary": "Vol." + vol + "_S" + str(len(segments) + 1),
            "text": seg_text,
        })
    return segments


def stage_segment(bases=None, vols=None, verbose=True):
    env = load_dotenv()
    api_key = env.get("LLM_API_KEY") or env.get("AUDIO_API_KEY")
    if not api_key:
        print("[ERROR] 缺少 LLM_API_KEY / AUDIO_API_KEY")
        return 1
    base_url = env.get("LLM_BASE_URL", "")
    model = env.get("LLM_MODEL", "qwen-plus")

    txs = sorted(OUT_DIR.glob("*_Vol.*_Transcript.txt"))
    if bases is not None:
        want = set(bases)
        txs = [t for t in txs if _base_from_tx(t) in want]
    elif vols is not None:
        want = {str(x) for x in vols}
        txs = [t for t in txs if parse_base(_base_from_tx(t))[1] in want]

    if not txs:
        if verbose:
            print("无待切分的 transcript")
        return 0

    SEGMENTS_DIR.mkdir(parents=True, exist_ok=True)
    total = 0
    for i, tx in enumerate(txs, 1):
        base = _base_from_tx(tx)
        _, vol = parse_base(base)
        sp = SEGMENTS_DIR / (base + "_segments.json")
        if sp.exists() and sp.stat().st_size > 50:
            try:
                with open(sp) as f:
                    existing = json.load(f)
                if isinstance(existing, list) and len(existing) > 0:
                    if verbose:
                        print(f"[{i}/{len(txs)}] {base}: {len(existing)} segments (cached)")
                    total += len(existing)
                    continue
            except Exception:
                pass
        transcript = tx.read_text(encoding="utf-8")
        full_lines = transcript.split(NL)
        if verbose:
            print(f"[{i}/{len(txs)}] {base}: {len(compress_transcript(transcript))} turns",
                  end="", flush=True)
        try:
            llm_segs = segment_transcript(api_key, base_url, model, transcript)
            n = len(llm_segs)
            segments = []
            for idx, ls in enumerate(llm_segs):
                sl, el = (idx * len(full_lines) // n,
                          (idx + 1) * len(full_lines) // n if idx < n - 1 else len(full_lines))
                fkw = ls.get("first_line", "")
                if fkw and len(fkw) >= 2:
                    for j in range(max(0, sl - 30), min(len(full_lines), sl + 30)):
                        if fkw[:4] in full_lines[j]:
                            sl = j
                            break
                segments.append({
                    "segment_index": ls.get("index", idx + 1),
                    "summary": ls.get("summary", ""),
                    "text": NL.join(full_lines[sl:el]),
                })
            with open(sp, "w", encoding="utf-8") as f:
                json.dump(segments, f, ensure_ascii=False, indent=2)
            if verbose:
                print(f" -> {len(segments)} segments (LLM)", flush=True)
            total += len(segments)
        except Exception as e:
            segments = _heuristic_segments(full_lines, vol)
            with open(sp, "w", encoding="utf-8") as f:
                json.dump(segments, f, ensure_ascii=False, indent=2)
            tag = "heuristic" if len(segments) > 0 else "single"
            if verbose:
                print(f" -> {len(segments)} segments ({tag}: {e})", flush=True)
            total += len(segments)
        if i < len(txs):
            time.sleep(3)
    if verbose:
        print(NL + "QA segmentation done: " + str(total) + " segments")
    return 0
