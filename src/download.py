#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 in/*.txt 读取播客链接并下载音频到 resources/<前缀>/。

链接文件命名约定（绝不硬编码播客名）：
    in/Qianjing.txt  -> 前缀 'Qianjing_'
    in/Xxxxxx.txt    -> 前缀 'Xxxxxx_'
每行为一个播客单集链接，可含空行；文件名即系列前缀。

产出：resources/<系列>/<前缀>Vol.<号>.m4a
    其中「<系列>」为 base 中 `_Vol.` 之前的部分（如 Qianjing，不含下划线），
    作为 resources/ 下第一层目录（即播客系列标识）；<base> = <前缀>Vol.<号>。
    Vol 号从单集页面标题中的 'Vol.XXX' 解析；音频直链从 og:audio 解析。
已存在同名文件则跳过（不重复下载）。
"""

import re
import subprocess
import sys
import time
from pathlib import Path

from src.utils import series_resources_dir

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def read_link_files(in_dir):
    """返回 [(prefix, url, src_stem, line_no), ...]；跳过空行与非 http(s) 行。

    prefix 直接从链接文件名派生（文件名去掉 .txt + 末尾下划线），
    因此新增一个系列只需在 in/ 放一个 Xxxxxx.txt，无需改动任何代码。
    """
    in_dir = Path(in_dir)
    out = []
    if not in_dir.exists():
        return out
    for txt in sorted(in_dir.glob("*.txt")):
        prefix = txt.stem + "_"          # 'Qianjing' -> 'Qianjing_'
        try:
            lines = txt.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for i, line in enumerate(lines, 1):
            u = line.strip()
            if not u:
                continue
            if not (u.startswith("http://") or u.startswith("https://")):
                print(f"  [WARN] {txt.name}:{i} 不是 http(s) 链接，已跳过: {u[:60]}")
                continue
            out.append((prefix, u, txt.stem, i))
    return out


def _fetch(url, timeout=30, referer=None):
    cmd = ["curl", "-sSL", "--max-time", str(timeout), "-A", UA, "-o", "-"]
    if referer:
        cmd += ["-e", referer]
    cmd.append(url)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 15)
    if r.returncode != 0:
        raise RuntimeError("HTTP %d: %s" % (r.returncode, (r.stderr or "").strip()[:160]))
    return r.stdout


def _meta(html, prop):
    pats = [
        re.compile(r'<meta[^>]+property=["\']' + re.escape(prop) + r'["\'][^>]*?content=["\'](.*?)["\']', re.I | re.S),
        re.compile(r'<meta[^>]+content=["\'](.*?)["\'][^>]*?property=["\']' + re.escape(prop) + r'["\']', re.I | re.S),
        re.compile(r'<meta[^>]+name=["\']' + re.escape(prop) + r'["\'][^>]*?content=["\'](.*?)["\']', re.I | re.S),
    ]
    for p in pats:
        m = p.search(html)
        if m:
            return m.group(1).strip()
    return None


def parse_vol(title):
    """从标题解析 Vol 号；返回 int 或 None。支持 Vol.101 / Vol 101 / vol.101。"""
    if not title:
        return None
    m = re.search(r'[Vv]ol\.?\s*0*(\d+)', title)
    return int(m.group(1)) if m else None


def resolve_episode(url, prefix):
    """解析单集页面，返回 (vol, audio_url, title, base)。

    base = '<prefix>Vol.<号>'（如 'Qianjing_Vol.171'），与 resources/
    out/ 的命名约定保持一致，前缀由调用方从文件名传入，绝不硬编码。
    """
    html = _fetch(url, timeout=30)
    audio = (_meta(html, "og:audio:secure_url")
             or _meta(html, "og:audio")
             or _meta(html, "og:audio:url"))
    title = (_meta(html, "og:title")
             or _meta(html, "og:audio:title")
             or _meta(html, "og:audio:alt"))
    if not title:
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        if m:
            title = m.group(1).strip()
    if not audio:
        # 兜底：从页面里找第一个音视频直链
        m = re.search(r'(https?://[^\s"\'<>]+\.(?:m4a|mp3|wav|aac))', html)
        if m:
            audio = m.group(1)
    if not audio:
        raise RuntimeError("页面未找到音频直链(og:audio)")
    vol = parse_vol(title)
    if vol is None:
        raise RuntimeError("无法从标题解析 Vol 号: " + repr(title)[:120])
    base = f"{prefix}Vol.{vol}"
    return vol, audio, title, base


def _download_file(url, dest, referer, attempts=2):
    dest.parent.mkdir(parents=True, exist_ok=True)
    last = None
    for att in range(1, attempts + 1):
        try:
            r = subprocess.run(
                ["curl", "-sSL", "--max-time", "900", "-A", UA,
                 "-e", referer or "https://www.xiaoyuzhoufm.com/",
                 "-o", str(dest), url],
                capture_output=True, text=True, timeout=920)
            if r.returncode != 0:
                raise RuntimeError((r.stderr or "").strip()[:160])
            if dest.stat().st_size < 1024:
                raise RuntimeError("文件过小 (<1KB)，可能下载失败")
            return
        except Exception as e:
            last = e
            if att < attempts:
                print(f"      [retry {att}] 下载失败: {e}", file=sys.stderr, flush=True)
                time.sleep(3)
    raise last


def download_episode(url, prefix, force=False):
    """下载单集音频到 resources/<前缀>Vol.<号>.m4a。

    返回 (base, newly_downloaded)。已存在同名文件则跳过（newly=False）。
    """
    vol, audio_url, title, base = resolve_episode(url, prefix)
    dest = series_resources_dir(base) / (base + ".m4a")
    if dest.exists() and not force:
        return base, False
    print(f"      ↓ 下载音频 -> {dest.name} (Vol.{vol})", file=sys.stderr, flush=True)
    _download_file(audio_url, dest, referer=url)
    mb = dest.stat().st_size / 1_048_576
    print(f"      ✓ 下载完成 {mb:.1f} MB", file=sys.stderr, flush=True)
    return base, True
