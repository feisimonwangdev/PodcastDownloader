#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PodcastDownloader 共享工具：路径、dotenv、LLM 客户端、播客前缀派生。

前缀规则（关键，绝不硬编码）：
    资源文件命名为  '<前缀>_Vol.<号>.m4a'  （例：Qianjing_Vol.101.m4a）
    所有输出文件共享同一个 base = 资源文件主干（不含扩展名）：
        out/<base>_Transcription.raw.json
        out/<base>_Transcript.txt
        out/segments/<base>_segments.json
    base 的前缀部分直接从 m4a 文件名解析得到，因此换一个播客
    （不同前缀）无需改动任何代码。
"""

import json
import os
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RESOURCES_DIR = PROJECT_ROOT / "resources"
OUT_DIR = PROJECT_ROOT / "out"
SEGMENTS_DIR = OUT_DIR / "segments"


# ---------------------------------------------------------------------------
# dotenv —— 与 DeepBrain 保持一致的零依赖实现（已设环境变量优先）
# ---------------------------------------------------------------------------

def load_dotenv() -> dict:
    p = PROJECT_ROOT / ".env"
    if not p.exists():
        return {}
    cfg: dict = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in (chr(34), chr(39)):
            v = v[1:-1]
        cfg[k] = v
    return cfg


# ---------------------------------------------------------------------------
# 播客前缀 / 期号解析 —— 全部从文件名派生
# ---------------------------------------------------------------------------

def parse_resource_stem(stem: str):
    """'Qianjing_Vol.101' -> ('Qianjing', '101', 'Qianjing_Vol.101')。

    返回 (prefix, vol, base)。无前缀时 prefix=''；base 始终为完整主干。
    """
    if "_Vol." in stem:
        prefix, rest = stem.split("_Vol.", 1)
        vol = rest.split(".")[0]
        return prefix, vol, stem
    if stem.startswith("Vol."):
        vol = stem.split(".", 1)[1].split(".")[0]
        return "", vol, stem
    return "", "", stem


def parse_base(base: str):
    """从输出 base 反解 (prefix, vol)。例：'Qianjing_Vol.101' -> ('Qianjing','101')。"""
    if "_Vol." in base:
        prefix, rest = base.split("_Vol.", 1)
        return prefix, rest.split(".")[0]
    return "", base


def resource_targets():
    """返回 [(prefix, vol, base), ...]，按 base 排序；base 不含扩展名。"""
    out = []
    for f in sorted(RESOURCES_DIR.glob("*_Vol.*.m4a")):
        prefix, vol, base = parse_resource_stem(f.stem)
        out.append((prefix, vol, base))
    return out


# ---------------------------------------------------------------------------
# LLM 客户端（segment 阶段使用）
# ---------------------------------------------------------------------------

def _split_models(model) -> list:
    """把 model 规格解析为回退列表。

    支持单模型名，也支持用 '｜' / '|' / ',' / '，' 分隔的多模型列表
    （如 .env 中的 LLM_MODEL 写成多个候选）。列表顺序即回退优先级。
    """
    s = str(model)
    for sep in ("｜", ",", "，"):
        s = s.replace(sep, "|")
    return [m.strip() for m in s.split("|") if m.strip()]


class _QuotaError(Exception):
    """鉴权 / 配额 / 余额耗尽类错误：同一模型重试无意义，应直接 fallback。"""


class _TransientError(Exception):
    """网络 / 超时 / 限流等瞬时错误：可在同一模型上重试。"""


# 配额/鉴权类错误关键词（命中即判定为不可重试 -> 直接 fallback 到下一模型）
# 注意：刻意不使用裸数字 '429'/'401'/'403'，以免误命中错误体里的 id/随机数。
_QUOTA_HINTS = (
    "quota", "额度", "余额", "余额不足", "balance", "insufficient",
    "token", "exhausted", "用完", "用尽", "rate limit", "rate_limit",
    "forbidden", "unauthorized", "invalid api key", "invalid apikey",
    "api key", "鉴权", "无权", "无权限", "subscription", "exceed",
    "limit reached", "上限",
)


def _is_quota(status, text):
    """判断错误是否『同一模型重试无意义』，应直接 fallback 到下一模型。

    命中即返回 True（立即 fallback，不重试）：
      * 401/403 鉴权失败；404 模型不存在 —— 永久不可用。
      * 429 且响应体点名配额（quota/额度/余额/token 耗尽等）—— 属配额；
        否则视为瞬时限流，允许在同模型重试。
      * 响应体含配额/鉴权类关键词（token 耗尽、余额不足、quota、forbidden…）。
    分类依据 HTTP 状态码而非文本里的裸数字，避免误命中 id/随机数中的 '429' 等。
    """
    low = (text or "").lower()
    if status in (401, 403, 404):
        return True
    if status == 429 and any(h in low for h in ("quota", "额度", "余额",
                                                "balance", "token", "exhausted", "用尽", "上限")):
        return True
    return any(h in low for h in _QUOTA_HINTS)


def llm_chat(api_key, base_url, model, messages, *,
             temperature=0.3, max_tokens=4096, timeout=180, retries=2):
    """调用 OpenAI 兼容 Chat API，支持按顺序回退的模型列表。

    model 可为单模型名，或用 '｜' / '|' / ',' / '，' 分隔的回退列表
    （如 .env 的 LLM_MODEL = 'a ｜ b ｜ c'）。任一模型成功即返回。

    回退策略（按错误性质区分，避免无效重试）：
      * 鉴权 / 配额 / 余额耗尽（token 用完等）——同一模型重试无意义，
        立即 fallback 到列表下一模型，不等待。
      * 网络超时 / 连接中断 / 5xx / 普通限流——视为瞬时错误，在「同一模型」
        上最多重试 `retries` 次（指数退避），仍失败再 fallback。
    所有候选模型均失败时抛出 RuntimeError，并附带最后一条错误。
    """
    import sys
    url = base_url.rstrip("/") + "/chat/completions"
    models = _split_models(model)
    if not models:
        raise RuntimeError("LLM failed: 未提供有效 model")

    last_err = None
    for m in models:
        try:
            return _call_model(url, api_key, m, messages,
                               temperature, max_tokens, timeout, retries)
        except _QuotaError as e:
            last_err = e
            print(f"[llm] model '{m}' 不可用（配额/鉴权）：{e} -> fallback 下一模型",
                  file=sys.stderr, flush=True)
        except _TransientError as e:
            last_err = e
            print(f"[llm] model '{m}' 瞬时错误：{e} -> fallback 下一模型",
                  file=sys.stderr, flush=True)
    raise RuntimeError("LLM failed: 所有候选模型均失败。最后错误: " + str(last_err)[:200])


def _call_model(url, api_key, m, messages, temperature, max_tokens, timeout, retries):
    body = json.dumps({
        "model": m, "messages": messages,
        "temperature": temperature, "max_tokens": max_tokens,
    }).encode("utf-8")
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=body, method="POST")
            req.add_header("Authorization", "Bearer " + api_key)
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(
                req, timeout=timeout, context=ssl.create_default_context()
            ) as resp:
                raw = resp.read().decode("utf-8")
            api_resp = json.loads(raw)
            if "error" in api_resp:
                msg = str(api_resp["error"].get("message", "?"))[:200]
                if _is_quota(0, msg):
                    raise _QuotaError("API: " + msg)
                raise _TransientError("API: " + msg)
            return api_resp["choices"][0]["message"]["content"]
        except _QuotaError:
            raise
        except urllib.error.HTTPError as e:
            status = e.code or 0
            text = ""
            try:
                text = e.read().decode("utf-8", "ignore")
            except Exception:
                pass
            if _is_quota(status, text or str(e.reason)):
                raise _QuotaError(f"HTTP {status}: {(text or str(e.reason))[:160]}")
            last = _TransientError(f"HTTP {status}: {(text or str(e.reason))[:160]}")
        except (urllib.error.URLError, OSError, ssl.SSLError,
                TimeoutError, KeyError, ValueError) as e:
            last = _TransientError(str(e))
        if attempt < retries:
            time.sleep((attempt + 1) * 5)
    raise last


# ---------------------------------------------------------------------------
# 时间戳 / 说话人标签（与 DeepBrain 保持一致，便于结果对齐）
# ---------------------------------------------------------------------------

def fmt_ts(ms):
    if ms is None:
        return "--:--"
    s = int(ms) / 1000.0
    h, m = int(s // 3600), int((s % 3600) // 60)
    sec = s % 60.0
    return f"{h}:{m:02d}:{sec:06.3f}" if h > 0 else f"{m}:{sec:06.3f}"


_SPK = ["SpeakerA", "SpeakerB", "SpeakerC", "SpeakerD", "SpeakerE",
        "SpeakerF", "SpeakerG", "SpeakerH", "SpeakerI", "SpeakerJ"]


def speaker_label(sid):
    if sid is None:
        return "?"
    return _SPK[sid] if 0 <= sid < len(_SPK) else f"Speaker{sid}"
