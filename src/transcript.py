#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""功能 A（转录 · 第 2 段）：raw JSON -> 角色分离可读 transcript。

输出：out/<base>_Transcript.txt
文件名 base（含前缀）从 raw JSON 文件名解析，绝不硬编码播客名。
"""

import json
from pathlib import Path

from src.utils import OUT_DIR, fmt_ts, speaker_label, parse_base

NL = chr(10)


def _sentences(payload):
    out = []
    for tr in payload.get("transcripts", []) or []:
        for s in tr.get("sentences", []) or []:
            out.append(dict(
                begin=s.get("begin_time"), end=s.get("end_time"),
                text=s.get("text", ""), sentence_id=s.get("sentence_id"),
                speaker_id=s.get("speaker_id"),
            ))
    out.sort(key=lambda x: (x["begin"] or 0))
    return out


def _merge(sents):
    if not sents:
        return []
    merged = [dict(sents[0])]
    for s in sents[1:]:
        if s["speaker_id"] == merged[-1]["speaker_id"]:
            merged[-1]["text"] += s["text"]
            if s["end"] is not None:
                merged[-1]["end"] = s["end"]
        else:
            merged.append(dict(s))
    return merged


def generate_transcript(raw_json_path):
    with open(raw_json_path, encoding="utf-8") as f:
        payload = json.load(f)
    sents = _sentences(payload)
    if not sents:
        return "(无内容)"
    lines = ["# Podcast Transcript (Paraformer-v2 + Speaker Diarization)", ""]
    props = payload.get("properties", {}) or {}
    if props:
        lines.append("- audio_format       : " + str(props.get("audio_format", "?")))
        lines.append("- sampling_rate      : " + str(props.get("original_sampling_rate", "?")) + " Hz")
        lines.append("- duration           : " + str(props.get("original_duration_in_milliseconds", "?")) + " ms")
        ch = props.get("channels")
        if ch is not None:
            lines.append("- channels           : " + str(ch))
        lines.append("")
    unique = sorted({s["speaker_id"] for s in sents if s["speaker_id"] is not None})
    lines.append("- detected_speakers  : " + str(len(unique)) + " -> "
                 + ", ".join(speaker_label(sp) for sp in unique))
    lines.extend(["", "---", ""])
    for blk in _merge(sents):
        sp = speaker_label(blk["speaker_id"])
        ts = fmt_ts(blk["begin"])
        text = (blk.get("text") or "").strip()
        if not text:
            continue
        lines.append("[" + ts + "] " + sp + ":")
        lines.append(text)
        lines.append("")
    return NL.join(lines)


def _base_from_raw(raw_path: Path) -> str:
    return raw_path.stem.replace("_Transcription.raw", "")


def stage_transcript(vols=None, verbose=True):
    raws = sorted(OUT_DIR.glob("*_Vol.*_Transcription.raw.json"))
    if vols is not None:
        want = {str(x) for x in vols}
        raws = [r for r in raws if parse_base(_base_from_raw(r))[1] in want]

    if not raws:
        if verbose:
            print("无待生成 transcript 的 raw JSON")
        return 0

    total = 0
    for raw_path in raws:
        base = _base_from_raw(raw_path)
        tx_path = OUT_DIR / (base + "_Transcript.txt")
        if tx_path.exists():
            if verbose:
                print(f"{base}: transcript 已存在")
            total += 1
            continue
        try:
            transcript = generate_transcript(raw_path)
            tx_path.write_text(transcript, encoding="utf-8")
            if verbose:
                print(f"{base}: {transcript.count(NL)} 行")
            total += 1
        except Exception as e:
            if verbose:
                print(f"{base}: 失败 {e}")
    if verbose:
        print(NL + "transcript 生成完成: " + str(total) + " 个")
    return 0
