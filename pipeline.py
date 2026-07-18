#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PodcastDownloader 统一入口。

分工：
    - 读取 resources/<前缀>/ 中已下载的音频（命名 <前缀>_Vol.<号>.m4a）
    - 功能 A（transcribe + transcript）：AUDIO LLM 分角色转录 -> out/
    - 功能 B（segment）：LLM 将 transcript 切分为问答片段 -> out/<前缀>/
    - 功能 C（sync）：从 in/*.txt 读取播客链接 -> 下载音频 -> 转录 -> 稿本 -> 分段

与 DeepBrain 的区别：本工程只负责「链接 -> 音频 -> 转录 -> 分段」，不提取 Pattern。
所有文件名的前缀均从输入文件名派生（in/Qianjing.txt -> 前缀 'Qianjing_'），
支持不同播客/系列使用不同前缀，无需改动任何代码。

用法：
    python3 pipeline.py status
    python3 pipeline.py transcribe [--vol 101]
    python3 pipeline.py transcript [--vol 101]
    python3 pipeline.py segment    [--vol 101]
    python3 pipeline.py all        [--vol 101]
    python3 pipeline.py sync               # 读取 in/*.txt，下载+转录+分段（已完成自动跳过）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.transcribe import stage_transcribe
from src.transcript import stage_transcript
from src.segment import stage_segment
from src.utils import (
    load_dotenv, RESOURCES_DIR, OUT_DIR, resource_targets, parse_base,
    PROJECT_ROOT, series_out_dir,
)


def show_status():
    import json as _json
    env = load_dotenv()

    resources = sorted(v for (_p, v, _b) in resource_targets())
    raws = sorted(parse_base(p.stem.replace("_Transcription.raw", ""))[1]
                  for p in OUT_DIR.glob("*/*_Vol.*_Transcription.raw.json"))
    transcripts = sorted(parse_base(p.stem.replace("_Transcript", ""))[1]
                         for p in OUT_DIR.glob("*/*_Vol.*_Transcript.txt"))
    segments = 0
    for sf in OUT_DIR.glob("*/*_Vol.*_segments.json"):
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


def stage_sync(in_dir=None, verbose=True):
    """读取 in/*.txt 的链接 -> 下载音频 -> 转录 -> 稿本 -> 分段。

    每个阶段只对「尚未完成」的集执行（按产物文件是否存在判定），
    因此重跑可安全续跑：已下载/转录/分段的会被跳过，缺哪补哪。
    结束时输出总结（下载/转录/稿本/分段 各自的新增·已存在·失败计数）。

    链接文件命名即系列前缀：in/Qianjing.txt -> 前缀 'Qianjing_'，
    文件名绝不硬编码，新增系列只需在 in/ 放一个 Xxxxxx.txt。
    """
    from src.download import read_link_files, download_episode

    in_dir = Path(in_dir) if in_dir else (PROJECT_ROOT / "in")
    links = read_link_files(in_dir)
    if not links:
        print(f"[sync] 在 {in_dir} 未找到任何链接（*.txt 中的 http 链接）。")
        return 0

    # ---- Phase 1: 下载音频 ----
    print("\n" + "=" * 64)
    print(f"  SYNC · 读取 {len(links)} 个链接"
          f"（来自 {len({s for _, _, s, _ in links})} 个系列文件）")
    print("=" * 64)
    resolved = []  # (prefix, url, base|None, newly, err)
    for i, (prefix, url, stem, ln) in enumerate(links, 1):
        try:
            base, newly = download_episode(url, prefix)
            resolved.append((prefix, url, base, newly, None))
            print(f"  [{i}/{len(links)}] {prefix}  {url}")
            print(f"        -> {base}.m4a  " + ("[新下载]" if newly else "[已存在·跳过]"))
        except Exception as e:
            resolved.append((prefix, url, None, False, str(e)))
            print(f"  [{i}/{len(links)}] {prefix}  {url}")
            print(f"        [ERROR] 下载失败: {e}")

    # 去重（同一 vol 多个链接只处理一次）
    bases, _seen = [], set()
    for (_p, _u, b, _n, _e) in resolved:
        if b and b not in _seen:
            _seen.add(b)
            bases.append(b)

    def raw_ok(b):
        return (series_out_dir(b) / (b + "_Transcription.raw.json")).exists()

    def tx_ok(b):
        return (series_out_dir(b) / (b + "_Transcript.txt")).exists()

    def seg_ok(b):
        p = series_out_dir(b) / (b + "_segments.json")
        return p.exists() and p.stat().st_size > 50

    pre = {b: (raw_ok(b), tx_ok(b), seg_ok(b)) for b in bases}

    # ---- Phase 2-4: 转录 / 稿本 / 分段 ----
    print("\n" + "=" * 64)
    print("  Phase: transcribe (ASR 分角色转录)")
    print("=" * 64)
    stage_transcribe(bases=bases, verbose=verbose)

    print("\n" + "=" * 64)
    print("  Phase: transcript (生成可读稿)")
    print("=" * 64)
    stage_transcript(bases=bases, verbose=verbose)

    print("\n" + "=" * 64)
    print("  Phase: segment (QA 分段)")
    print("=" * 64)
    stage_segment(bases=bases, verbose=verbose)

    # ---- 总结 ----
    print("\n" + "=" * 64)
    print("  SYNC 总结")
    print("=" * 64)
    dl_new = sum(1 for (_p, _u, _b, n, _e) in resolved if n)
    dl_exist = sum(1 for (_p, _u, _b, n, _e) in resolved if (not n) and _b is not None)
    dl_fail = sum(1 for (_p, _u, b, _n, _e) in resolved if b is None)
    tr_new = sum(1 for b in bases if (not pre[b][0]) and raw_ok(b))
    tr_exist = sum(1 for b in bases if pre[b][0])
    tr_fail = sum(1 for b in bases if (not pre[b][0]) and not raw_ok(b))
    tx_new = sum(1 for b in bases if (not pre[b][1]) and tx_ok(b))
    tx_exist = sum(1 for b in bases if pre[b][1])
    tx_fail = sum(1 for b in bases if (not pre[b][1]) and not tx_ok(b))
    sg_new = sum(1 for b in bases if (not pre[b][2]) and seg_ok(b))
    sg_exist = sum(1 for b in bases if pre[b][2])
    sg_fail = sum(1 for b in bases if (not pre[b][2]) and not seg_ok(b))

    print(f"  链接总数            : {len(links)}")
    print(f"  音频  下载 {dl_new} / 已存在 {dl_exist} / 失败 {dl_fail}")
    print(f"  转录  新增 {tr_new} / 已存在 {tr_exist} / 失败 {tr_fail}")
    print(f"  稿本  新增 {tx_new} / 已存在 {tx_exist} / 失败 {tx_fail}")
    print(f"  分段  新增 {sg_new} / 已存在 {sg_exist} / 失败 {sg_fail}")
    if dl_fail or tr_fail or tx_fail or sg_fail:
        print("  ⚠ 存在失败项，重新运行 `python3 pipeline.py sync` 可续跑"
              "（已完成的会自动跳过）。")
    else:
        print("  ✓ 全部完成。")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="PodcastDownloader Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Stages: status, transcribe, transcript, segment, all, sync",
    )
    parser.add_argument("stage", nargs="?", default="status",
                        choices=["status", "transcribe", "transcript", "segment", "all", "sync"])
    parser.add_argument("--vol", help="处理单集（如 101）")
    parser.add_argument("--in-dir", help="链接目录（默认 in/）")
    args = parser.parse_args()
    vols = [args.vol] if args.vol else None

    stages = {
        "status": show_status,
        "transcribe": lambda: stage_transcribe(vols=vols, verbose=True),
        "transcript": lambda: stage_transcript(vols=vols, verbose=True),
        "segment": lambda: stage_segment(vols=vols, verbose=True),
        "all": lambda: stage_all(vols),
        "sync": lambda: stage_sync(in_dir=args.in_dir, verbose=True),
    }
    return stages[args.stage]()


if __name__ == "__main__":
    sys.exit(main())
