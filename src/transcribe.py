#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""功能 A（转录 · 第 1 段）：本地 m4a -> ASR(paraformer-v2) -> raw JSON。

核心流程：
    - 读取本地 resources/ 的 m4a，由 audio_io 转码为 mp3 并上传到
      GitHub Release（默认 feisimonwangdev/PodcastDownloader，可用
      .env 的 GITHUB_REPO 覆盖），得到公开下载 URL 供 paraformer-v2 拉取。
    - 输出文件名前缀从 m4a 文件名派生（base），不再硬编码任何播客名。
输出：out/<base>_Transcription.raw.json
"""

import json
import subprocess
import time
from pathlib import Path

from src.utils import (
    OUT_DIR, RESOURCES_DIR, load_dotenv, resource_targets,
)
from src.audio_io import prepare_public_audio, DEFAULT_REPO

MODELS = ["paraformer-v2", "paraformer-8k-v2", "paraformer-realtime-v2"]
HOSTS = [
    ("ws", "llm-tvey7rva6yydj3qy.cn-beijing.maas.aliyuncs.com"),
    ("std", "dashscope.aliyuncs.com"),
]


def _raw_path(base: str) -> Path:
    return OUT_DIR / (base + "_Transcription.raw.json")


def _submit_one(api_key, file_url, model):
    """提交单个 (model, host) 尝试，返回 task_id 或 None。"""
    body = {
        "model": model,
        "input": {"file_urls": [file_url]},
        "parameters": {
            "channel_id": [0],
            "language_hints": ["zh", "en"],
            "diarization_enabled": True,
        },
    }
    for _ht, host in HOSTS:
        try:
            r = subprocess.run(
                ["curl", "-s", "-L", "--max-time", "30",
                 "-H", "Content-Type: application/json",
                 "-H", f"Authorization: Bearer {api_key}",
                 "-H", "X-DashScope-Async: enable",
                 "-d", json.dumps(body),
                 f"https://{host}/api/v1/services/audio/asr/transcription"],
                capture_output=True, text=True, timeout=60,
            )
            try:
                resp = json.loads(r.stdout)
            except Exception:
                continue
            if "output" in resp and "task_id" in resp["output"]:
                return resp["output"]["task_id"], host
        except Exception:
            continue
    return None, None


def _poll_one(api_key, task_id, host, timeout_sec=7200):
    """轮询任务，成功返回解析后的 JSON dict，否则返回 None。"""
    start = time.time()
    while (time.time() - start) < timeout_sec:
        time.sleep(15)
        r = subprocess.run(
            ["curl", "-s", "-L", "--max-time", "30",
             "-H", f"Authorization: Bearer {api_key}",
             f"https://{host}/api/v1/tasks/{task_id}"],
            capture_output=True, text=True, timeout=60,
        )
        try:
            resp = json.loads(r.stdout)
        except Exception:
            continue
        st = resp.get("output", {}).get("task_status", "?")
        if st == "SUCCEEDED":
            results = resp["output"].get("results", [])
            succ = [x for x in results if x.get("subtask_status") == "SUCCEEDED"]
            if not succ:
                print(f"    [WARN] 任务成功但无成功子结果: {json.dumps(resp.get('output', {}), ensure_ascii=False)[:400]}")
                return None
            d = subprocess.run(
                ["curl", "-s", "-L", "--max-time", "60", succ[0]["transcription_url"]],
                capture_output=True, text=True, timeout=90,
            ).stdout
            return json.loads(d)
        if st in ("FAILED", "CANCELED"):
            print(f"    [ASR 任务 {st}] 详情: {json.dumps(resp.get('output', {}), ensure_ascii=False)[:600]}")
            return None
    return None


def stage_transcribe(bases=None, vols=None, verbose=True):
    env = load_dotenv()
    api_key = env.get("AUDIO_API_KEY")
    if not api_key:
        print("[ERROR] 缺少 AUDIO_API_KEY")
        return 1
    model = env.get("AUDIO_MODEL", "paraformer-v2")
    repo = env.get("GITHUB_REPO", DEFAULT_REPO)
    token = env.get("GITHUB_TOKEN") or None

    if bases is not None:
        want = set(bases)
        # 与默认分支一致：已转录（raw 存在）的集也跳过，满足「已转录不再转录」
        targets = [(p, v, b) for (p, v, b) in resource_targets()
                   if b in want and not _raw_path(b).exists()]
    elif vols is None:
        targets = [(p, v, b) for (p, v, b) in resource_targets()
                   if not _raw_path(b).exists()]
    else:
        want = {str(x) for x in vols}
        targets = [(p, v, b) for (p, v, b) in resource_targets() if v in want]

    if not targets:
        if verbose:
            print("无待转录 episode（已全部完成或不在 resources/ 中）")
        return 0

    if verbose:
        print(f"提交 {len(targets)} 个 episode ...")
    done, failed = 0, 0
    for prefix, vol, base in targets:
        m4a = RESOURCES_DIR / (base + ".m4a")
        if not m4a.exists():
            if verbose:
                print(f"Vol.{vol}: 资源缺失 {m4a.name}")
            failed += 1
            continue
        if verbose:
            print(f"Vol.{vol}: 准备音频并上传到 GitHub Release ...")
        try:
            url, temp = prepare_public_audio(m4a, repo=repo, base=base, token=token)
        except Exception as e:
            if verbose:
                print(f"Vol.{vol}: 音频准备失败 {e}")
            failed += 1
            continue

        task_id, host = None, None
        for m in (model,) + tuple(MODELS):
            task_id, host = _submit_one(api_key, url, m)
            if task_id:
                if verbose:
                    print(f"Vol.{vol}: 提交成功 task_id={task_id} model={m} host={host}")
                break
        if not task_id:
            if verbose:
                print(f"Vol.{vol}: 提交失败")
            failed += 1
            continue

        data = _poll_one(api_key, task_id, host)
        if data is None:
            if verbose:
                print(f"Vol.{vol}: 轮询失败/超时")
            failed += 1
            continue

        data["episode"] = f"Vol.{vol}"
        data["source"] = f"{base}.m4a"
        data["podcast"] = prefix
        _raw_path(base).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if verbose:
            n = len(data["transcripts"][0]["sentences"])
            print(f"Vol.{vol}: 转录完成 {n} 句 -> {_raw_path(base).name}")
        done += 1

        if temp is not None and temp.exists():
            try:
                temp.unlink()
            except Exception:
                pass

    if verbose:
        print(f"\n转录阶段完成: 成功 {done} / 失败 {failed} / 共计 {len(targets)}")
    return 0 if failed == 0 else 1
