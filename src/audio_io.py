#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地音频 -> 公网可访问 URL（供 ASR 服务提交）。

设计要点：
    - 用 ffmpeg 把 m4a 统一转码为 16kHz 单声道 mp3（与 DeepBrain 已验证的
      转码参数一致），避免 m4a 在 paraformer-v2 上识别异常。
    - 转码后的 mp3 上传到 GitHub Release（默认仓库
      feisimonwangdev/PodcastDownloader，可用 GITHUB_REPO 覆盖），
      返回公开下载 URL 供阿里云 paraformer-v2 拉取。
    - 上传优先使用已登录的 gh CLI；若不可用则回退到 GitHub REST API
      （需 GITHUB_TOKEN）。两种途径均只依赖 GitHub，不再使用 file.io 等
      临时服务。

注意：目标仓库必须为「公开」仓库，否则 ASR 服务端无法匿名拉取音频。
"""

import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

DEFAULT_REPO = "feisimonwangdev/PodcastDownloader"


def have_ffmpeg() -> bool:
    return bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe"))


def probe_audio(path: Path) -> dict:
    cmd = ["ffprobe", "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", str(path)]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError("ffprobe failed: " + out.stderr.strip()[:300])
    info = json.loads(out.stdout)
    fmt = info.get("format", {})
    streams = info.get("streams", [])
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    return {
        "format_name": fmt.get("format_name", "?"),
        "duration_sec": float(fmt.get("duration", 0) or 0),
        "codec_name": audio.get("codec_name", "?") if audio else "?",
        "sample_rate": int(audio.get("sample_rate", 0) or 0) if audio else 0,
        "channels": int(audio.get("channels", 0) or 0) if audio else 0,
        "size_bytes": int(fmt.get("size", 0) or 0),
    }


def convert_to_mp3(src: Path, dst: Path) -> None:
    cmd = ["ffmpeg", "-y", "-i", str(src), "-ac", "1", "-ar", "16000",
           "-b:a", "64k", "-map_metadata", "-1", "-loglevel", "error", str(dst)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg convert failed: " + r.stderr.strip()[:300])


# ---------------------------------------------------------------------------
# GitHub Release 上传
# ---------------------------------------------------------------------------

def _gh_available() -> bool:
    return bool(shutil.which("gh"))


def _upload_via_gh(mp3: Path, repo: str, tag: str):
    """用 gh CLI 上传到 release，返回公开下载 URL；失败返回 None。"""
    asset_name = mp3.name
    v = subprocess.run(["gh", "release", "view", tag, "--repo", repo],
                       capture_output=True, text=True, timeout=30)
    if v.returncode != 0:
        c = subprocess.run(
            ["gh", "release", "create", tag, "--repo", repo,
             "--title", f"Audio {tag}", "--notes", "auto-uploaded by PodcastDownloader",
             "--latest=false"],
            capture_output=True, text=True, timeout=60)
        if c.returncode != 0:
            return None
    u = subprocess.run(
        ["gh", "release", "upload", tag, str(mp3), "--repo", repo, "--clobber"],
        capture_output=True, text=True, timeout=600)
    if u.returncode != 0:
        return None
    qv = subprocess.run(
        ["gh", "release", "view", tag, "--repo", repo, "--json", "assets",
         "-q", f'.assets[] | select(.name=="{asset_name}") | .downloadUrl'],
        capture_output=True, text=True, timeout=30)
    url = qv.stdout.strip()
    if not url:
        url = f"https://github.com/{repo}/releases/download/{tag}/{quote(asset_name)}"
    return url


def _api_get(url: str, token: str):
    r = subprocess.run(
        ["curl", "-sS", "--max-time", "30",
         "-H", f"Authorization: Bearer {token}",
         "-H", "Accept: application/vnd.github+json", url],
        capture_output=True, text=True, timeout=40)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except Exception:
        return None


def _api_post(url: str, payload: dict, token: str):
    r = subprocess.run(
        ["curl", "-sS", "--max-time", "60", "-X", "POST",
         "-H", f"Authorization: Bearer {token}",
         "-H", "Accept: application/vnd.github+json",
         "-H", "Content-Type: application/json",
         "-d", json.dumps(payload), url],
        capture_output=True, text=True, timeout=70)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except Exception:
        return None


def _api_delete(url: str, token: str):
    subprocess.run(
        ["curl", "-sS", "--max-time", "30", "-X", "DELETE",
         "-H", f"Authorization: Bearer {token}", url],
        capture_output=True, text=True, timeout=40)


def _upload_via_api(mp3: Path, repo: str, tag: str, token: str):
    """用 GitHub REST API 上传到 release，返回公开下载 URL；失败返回 None。"""
    asset_name = mp3.name
    api = "https://api.github.com"
    rel = _api_get(f"{api}/repos/{repo}/releases/tags/{tag}", token)
    if rel is None:
        rel = _api_post(f"{api}/repos/{repo}/releases",
                        {"tag_name": tag, "name": f"Audio {tag}",
                         "body": "auto-uploaded", "draft": False,
                         "prerelease": False, "make_latest": False}, token)
    if not rel or "id" not in rel:
        return None
    rel_id = rel["id"]
    for a in rel.get("assets", []):
        if a.get("name") == asset_name:
            _api_delete(f"{api}/repos/{repo}/releases/assets/{a['id']}", token)
    up = subprocess.run(
        ["curl", "-sS", "--max-time", "600",
         "-H", f"Authorization: Bearer {token}",
         "-H", "Content-Type: application/octet-stream",
         "--data-binary", f"@{mp3}",
         f"{api}/repos/{repo}/releases/{rel_id}/assets?name={quote(asset_name)}"],
        capture_output=True, text=True, timeout=620)
    try:
        return json.loads(up.stdout).get("browser_download_url")
    except Exception:
        return None


def upload_to_github_release(mp3: Path, repo: str, tag: str,
                             token: str = None) -> str:
    """上传 mp3 到 repo 的 release（按 tag 复用），返回公开下载 URL。

    资产名固定为文件自身的名称（mp3.name），避免调用方传入的名称与实际
    上传文件名不一致导致查询/回退 URL 错位。
    """
    if _gh_available():
        url = _upload_via_gh(mp3, repo, tag)
        if url:
            return url
    if token:
        url = _upload_via_api(mp3, repo, tag, token)
        if url:
            return url
    raise RuntimeError(
        f"上传音频到 GitHub Release 失败（repo={repo} tag={tag}）。"
        f"请确认 gh 已登录（gh auth status）或在 .env 配置 GITHUB_TOKEN。")


def wait_asset_ready(url: str, timeout_sec: int = 180, interval_sec: int = 5) -> bool:
    """等待 GitHub Release 资产真正可下载（CDN 就绪）。

    根因修复：gh release upload 返回后，GitHub CDN 往往需要数秒才对外提供
    下载。若此时立即把 URL 交给 ASR，阿里云端拉取会拿到 404/空响应，导致
    任务直接 FAILED。这里在返回 URL 前轮询 HTTP 状态，确保已可访问，
    消除「上传即提交」的竞态。
    """
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        r = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             "-L", "--max-time", "20", url],
            capture_output=True, text=True, timeout=30)
        if r.stdout.strip() == "200":
            return True
        time.sleep(interval_sec)
    return False


def prepare_public_audio(m4a: Path, repo: str, base: str, token: str = None):
    """转码（m4a -> mp3）并上传到 GitHub Release，返回 (public_url, temp_path_or_None)。

    public_url 为 GitHub 下载直链（访问时会 302 到带签名的 CDN 地址，阿里云
    会跟随重定向）。返回前会等待资产真正可下载，避免 ASR 拉取竞态。
    temp_path 为本地临时 mp3，ASR 用的是 GitHub 上的副本，用后由调用方清理。
    """
    if have_ffmpeg():
        worker = Path(tempfile.gettempdir()) / (base + ".mp3")
        try:
            convert_to_mp3(m4a, worker)
        except Exception as e:
            print(f"[WARN] ffmpeg 转码失败，尝试直接上传原 m4a: {e}")
            worker = m4a
    else:
        print("[WARN] 未检测到 ffmpeg，直接上传原 m4a（可能不被 ASR 支持）。")
        worker = m4a

    tag = "audio-" + base
    url = upload_to_github_release(worker, repo, tag, token)
    if not wait_asset_ready(url):
        print(f"[WARN] 资产在 {180}s 内未就绪，ASR 可能拉取失败: {url}")
    return url, (worker if worker != m4a else None)
