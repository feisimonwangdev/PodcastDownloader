#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PodcastDownloader 统一入口。

分工：
    - 读取 resources/ 中已下载的音频（命名 <前缀>_Vol.<号>.m4a）
    - 功能 A（transcribe + transcript）：AUDIO LLM 分角色转录 -> out/
    - 功能 B（segment）：LLM 将 transcript 切分为问答片段 -> out/segments/

与 DeepBrain 的区别：本工程只负责「音频 -> 转录 -> 分段」，不下载、不提取 Pattern。
所有输出文件名的前缀均从 m4a 文件名派生，支持不同播客使用不同前缀。

用法：
    python3 pipeline.py status
    python3 pipeline.py transcribe [--vol 101]
    python3 pipeline.py transcript [--vol 101]
    python3 pipeline.py segment    [--vol 101]
    python3 pipeline.py all        [--vol 101]
"""

from __future__ import annotations

import argparse
import sys

from src.transcribe import stage_transcribe
from src.transcript import stage_transcript
from src.segment import stage_segment
from src.utils import (
    load_dotenv, RESOURCES_DIR, OUT_DIR, SEGMENTS_DIR, resource_targets, parse_base,
)


def show_status():
    import json as _json
    env = load_dotenv()

    resources = sorted(v for (_p, v, _b) in resource_targets())
    raws = sorted(parse_base(p.stem.replace("_Transcription.raw", ""))[1]
                  for p in OUT_DIR.glob("*_Vol.*_Transcription.raw.json"))
    transcripts = sorted(parse_base(p.stem.replace("_Transcript", ""))[1]
                         for p in OUT_DIR.glob("*_Vol.*_Transcript.txt"))
    segments = 0
    if SEGMENTS_DIR.exists():
        for sf in SEGMENTS_DIR.glob("*_Vol.*_segments.json"):
            try:
                with open(sf) as f:
                    segments += len(_json.load(f))
            except Exception:
                pass

    print("=" * 55)
    print("  PodcastDownloader Pipeline Status")
    print("=" * 55)
    print(f"  Resources (m4a)  : {len(resources)}")
    print(f"  Raw JSON         : {len(raws)}")
    print(f"  Transcripts      : {len(transcripts)}")
    print(f"  QA Segments      : {segments}")
    print(f"  ASR Model        : {env.get('AUDIO_MODEL', '?')}")
    print(f"  LLM Model        : {env.get('LLM_MODEL', '?')}")
    print()

    res_set, raw_set, tx_set = set(resources), set(raws), set(transcripts)
    pending_transcribe = res_set - raw_set
    pending_transcript = raw_set - tx_set
    if pending_transcribe:
        print(f"  Pending transcribe ({len(pending_transcribe)}): " + ", ".join(sorted(pending_transcribe)))
    if pending_transcript:
        print(f"  Pending transcript ({len(pending_transcript)}): " + ", ".join(sorted(pending_transcript)))
    if not pending_transcribe and not pending_transcript:
        print("  All audio processed. Run segment next if needed.")


def stage_all(vols=None):
    errors = 0
    for fn, name in [(lambda: stage_transcribe(vols, verbose=True), "transcribe"),
                     (lambda: stage_transcript(vols, verbose=True), "transcript"),
                     (lambda: stage_segment(vols, verbose=True), "segment")]:
        print(f"\n{'='*40}\n  Stage: {name}\n{'='*40}")
        rc = fn()
        if rc and rc > 0:
            errors += rc
            print(f"  [WARN] {name} stage returned {rc}")
    return errors


def main():
    parser = argparse.ArgumentParser(
        description="PodcastDownloader Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Stages: status, transcribe, transcript, segment, all",
    )
    parser.add_argument("stage", nargs="?", default="status",
                        choices=["status", "transcribe", "transcript", "segment", "all"])
    parser.add_argument("--vol", help="处理单集（如 101）")
    args = parser.parse_args()
    vols = [args.vol] if args.vol else None

    stages = {
        "status": show_status,
        "transcribe": lambda: stage_transcribe(vols, verbose=True),
        "transcript": lambda: stage_transcript(vols, verbose=True),
        "segment": lambda: stage_segment(vols, verbose=True),
        "all": lambda: stage_all(vols),
    }
    return stages[args.stage]()


if __name__ == "__main__":
    sys.exit(main())
