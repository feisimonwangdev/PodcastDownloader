#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""功能 B：LLM 将 transcript -> 独立问答片段（QA segmentation）。

输出：out/<前缀>/<base>_segments.json
文件名 base（含前缀）从 transcript 文件名解析，绝不硬编码播客名。
优先用 LLM 切分；LLM 失败或限流时回退到基于关键词的启发式切分。
"""

import json
import re
import time
from pathlib import Path

from src.utils import OUT_DIR, load_dotenv, llm_chat, parse_base, series_out_dir

NL = chr(10)


OPEN_KW = ["老师你好", "钱老师", "哈喽", "我的问题是", "我想问", "你好钱",
           "你好老师", "连到你", "听到吗", "主持人"]


def compress_transcript(transcript, max_turns=200, turn_max_chars=50):
    """压缩 transcript 供 LLM 做分段。

    关键修正：原先只保留 SpeakerA/SpeakerB 的发言，但 ASR 给的说话人标签是
    任意分配的（同一集里听众可能是 SpeakerC~G）。若只保留 A/B，听众连线的
    开场白（老师你好/我的问题是…）会被整体过滤掉，LLM 看不到分段边界，
    从而把整集误合并成 1 段。现改为保留「所有说话人」的发言，并优先保留
    含开场白关键字的行，确保分段边界不丢失。
    """
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
        m = re.match(r"^\s*\[[^\]]*\]\s*(Speaker[A-Za-z]+):", s)
        if not m:
            continue
        sp = m.group(1)
        content = lines[i + 1].strip() if i + 1 < len(lines) else ""
        ts = s.split("]")[0].replace("[", "") if "]" in s else ""
        truncated = content[:turn_max_chars] + ("..." if len(content) > turn_max_chars else "")
        is_open = any(kw in (s + " " + content[:40]) for kw in OPEN_KW)
        turns.append((is_open, "[" + ts + "] " + sp + ": " + truncated))
    opens = [t for f, t in turns if f]
    others = [t for f, t in turns if not f]
    if len(turns) > max_turns:
        step = max(1, len(others) // max(1, max_turns - len(opens)))
        others = others[::step]
    return NL.join(opens + others)


# 强来电开场标记：一个听众连线通常只出现一次，用作确定性分段边界
STRONG_OPEN = ["老师你好", "连到你", "听到吗", "你好钱", "你好老师", "哈喽"]


def _header_end(full_lines):
    for j, line in enumerate(full_lines):
        if line.strip() == "---":
            return j + 1
    return 0


def _opening_boundaries(full_lines):
    """返回全文里出现强来电开场白的说话人行索引（确定性分段边界）。

    用于兜底：LLM 有时即便看到开场白仍把多个听众合并成一段（且带温度
    导致结果不稳定）。此函数不依赖 LLM，确定性地找出每个听众连线的起点。
    """
    he = _header_end(full_lines)
    bounds = []
    for j in range(max(0, he), len(full_lines) - 2):
        line = full_lines[j].strip()
        nl = full_lines[j + 1].strip() if j + 1 < len(full_lines) else ""
        if re.match(r"^\s*\[[^\]]*\]\s*Speaker[A-Za-z]+:", line):
            c = line + " " + nl[:40]
            if any(kw in c for kw in STRONG_OPEN):
                bounds.append(j)
    # 同一听众连续多个强标记只保留首个，避免过切
    dedup = []
    for b in bounds:
        if not dedup or b - dedup[-1] > 3:
            dedup.append(b)
    return dedup


SUMMARIZE_HEADER = ("用一句话概括这段播客问答片段的核心问题与主持人建议。"
                    "只输出概括文本，不要解释、不要序号。")


def _summarize_segment(api_key, base_url, model, text):
    snippet = text[:1500]
    try:
        return llm_chat(api_key, base_url, model,
                        [{"role": "user", "content": SUMMARIZE_HEADER + "\n\n" + snippet}],
                        temperature=0.2, max_tokens=128).strip()
    except Exception:
        return ""


def _caller_bound_segments(full_lines, bounds, vol):
    """按听众开场白边界切分（确定性）。每段以某个听众开场白起始。"""
    he = _header_end(full_lines)
    pts = [he] + bounds + [len(full_lines)]
    segs = []
    for bi in range(len(pts) - 1):
        txt = NL.join(full_lines[pts[bi]:pts[bi + 1]])
        if len(txt.strip()) < 100:
            if segs:
                segs[-1]["text"] = segs[-1]["text"] + NL + txt
            continue
        segs.append({"segment_index": len(segs) + 1, "summary": "", "text": txt})
    for i, s in enumerate(segs):
        s["segment_index"] = i + 1
    return segs


def _enrich_summaries(api_key, base_url, model, segments, vol):
    for s in segments:
        summary = _summarize_segment(api_key, base_url, model, s["text"])
        s["summary"] = summary or ("Vol." + vol + "_S" + str(s["segment_index"]))
        time.sleep(1)
    return segments


def _map_llm_segments(llm_segs, full_lines):
    n = len(llm_segs)
    segments, spans = [], []
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
        spans.append((sl, el))
    return segments, spans


def _strict_merged(spans, bounds):
    """严格判定：是否有某段内跨越了 >=2 个真实听众开场白边界（错位合并）。"""
    return any(sum(1 for x in bounds if sl <= x < el) >= 2 for sl, el in spans)


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
        if re.match(r"^\s*\[[^\]]*\]\s*Speaker[A-Za-z]+:", line):
            c = line + " " + nl[:40]
            if any(kw in c for kw in ["老师你好", "钱老师", "哈喽", "我的问题是",
                                      "我想问", "你好钱", "你好老师", "连到你", "听到吗", "主持人"]):
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

    txs = sorted(OUT_DIR.glob("*/*_Vol.*_Transcript.txt"))
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

    total = 0
    for i, tx in enumerate(txs, 1):
        base = _base_from_tx(tx)
        _, vol = parse_base(base)
        sp = series_out_dir(base) / (base + "_segments.json")
        sp.parent.mkdir(parents=True, exist_ok=True)
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
            mapped, spans = _map_llm_segments(llm_segs, full_lines)
            bounds = _opening_boundaries(full_lines)
            cb = _caller_bound_segments(full_lines, bounds, vol)
            # 兜底：LLM 把多个听众合并时，强制按听众开场白边界切分
            #   - 段数少于真实边界应切数，或某段内跨越 >=2 个真实边界（错位合并）
            # 用严格边界判定，避免中段闲聊里的开场白关键词造成误触发
            if (len(bounds) >= 2 and len(mapped) < len(cb)) or _strict_merged(spans, bounds):
                segments = _enrich_summaries(api_key, base_url, model, cb, vol)
                tag = "caller-bound"
            else:
                segments = mapped
                tag = "LLM"
            with open(sp, "w", encoding="utf-8") as f:
                json.dump(segments, f, ensure_ascii=False, indent=2)
            if verbose:
                print(f" -> {len(segments)} segments ({tag})", flush=True)
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
