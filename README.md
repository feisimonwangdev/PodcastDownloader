# PodcastDownloader — 播客音频转写与分段

从 `resources/` 读取已下载的播客音频，调用 **AUDIO LLM** 做分角色转录，再调用
**文本 LLM** 做问答分段。两类产物分别落在 `out/` 与 `out/segments/`。

> 本项目只负责「音频 → 转录 → 分段」。Pattern 提取、AI 教练等下游功能在
> **DeepBrain** 项目中（见下文「与 DeepBrain 的分工」）。

## 文件命名规则（关键）

资源文件遵循：`<前缀>_Vol.<号>.m4a`，例如 `Qianjing_Vol.101.m4a`。

- **前缀**即播客标识，不同播客用不同前缀（如 `Qianjing_`、`SomeOther_`）。
- 该前缀**从 m4a 文件名自动派生**，代码中**绝不硬编码**。换播客无需改代码。
- 所有产物共享同一个 `base`（= 资源文件主干，含前缀）：

| 产物 | 路径 |
|---|---|
| 原始 ASR JSON | `out/<base>_Transcription.raw.json` |
| 角色分离可读稿 | `out/<base>_Transcript.txt` |
| 问答分段 | `out/segments/<base>_segments.json` |

例如 `Qianjing_Vol.101.m4a` → `Qianjing_Vol.101_Transcription.raw.json` 等。

## 目录结构

```
PodcastDownloader/
├── pipeline.py            # 统一入口
├── resources/             # 已下载音频：<前缀>_Vol.<号>.m4a
├── out/                   # 转写产物
│   ├── <base>_Transcription.raw.json
│   ├── <base>_Transcript.txt
│   └── segments/
│       └── <base>_segments.json
└── src/
    ├── utils.py           # 路径/dotenv/LLM 客户端/前缀派生
    ├── audio_io.py        # 本地音频转码 + 上传到 GitHub Release
    ├── transcribe.py      # 功能 A(1)：ASR 转录 -> raw JSON
    ├── transcript.py      # 功能 A(2)：raw JSON -> 可读稿
    └── segment.py         # 功能 B：LLM 问答切分 -> segments
```

## 配置（.env）

复制 `.env.example` 为 `.env` 并填入密钥：

```env
AUDIO_API_KEY=sk-xxxx
AUDIO_DASHSCOPE_URL=https://llm-tvey7rva6yydj3qy.cn-beijing.maas.aliyuncs.com/api/v1
AUDIO_MODEL=paraformer-v2
GITHUB_REPO=feisimonwangdev/PodcastDownloader   # 上传音频的仓库（需公开）
GITHUB_TOKEN=                                    # 可选，gh 已登录可留空
LLM_API_KEY=sk-xxxx
LLM_BASE_URL=https://llm-tvey7rva6yydj3qy.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
LLM_MODEL=qwen3.7-max-2026-06-08
```

## 依赖

```bash
pip install requests
brew install ffmpeg   # 本地 m4a 兜底转码为 mp3
```

## 使用

```bash
python3 pipeline.py status                 # 查看进度
python3 pipeline.py all                    # 转录 + 分段 全流程
python3 pipeline.py transcribe --vol 101  # 单集：ASR 转录
python3 pipeline.py transcript --vol 101  # 单集：生成可读稿
python3 pipeline.py segment    --vol 101  # 单集：LLM 分段
```

处理流程：

```
resources/<前缀>_Vol.<号>.m4a
   │
   ▼ transcribe   — 转码为 mp3 → 上传 GitHub Release → paraformer-v2 ASR → raw JSON
   ▼ transcript   — raw JSON → 角色分离对话文本
   ▼ segment      — transcript → LLM 问答切分
```

转录阶段读取**本地** `resources/`，经 ffmpeg 转码为 mp3 后，上传到
**GitHub Release**（默认 `feisimonwangdev/PodcastDownloader`，可用 `GITHUB_REPO`
覆盖）得到公开下载 URL，再提交给 paraformer-v2。上传优先使用已登录的 `gh`
CLI，未登录时回退到 GitHub REST API（需 `GITHUB_TOKEN`）。

> ⚠️ 托管音频的仓库必须是**公开**仓库，否则 ASR 服务端无法匿名拉取音频。

## 与 DeepBrain 的分工

| 功能 | 归属 |
|---|---|
| 音频下载 / 转换 | DeepBrain |
| 上传音频到 GitHub Release（供 ASR） | **PodcastDownloader**（默认 `feisimonwangdev/PodcastDownloader`） |
| 转录（AUDIO LLM） | **PodcastDownloader** |
| 分段（LLM） | **PodcastDownloader** |
| Pattern 提取（LLM） | DeepBrain |
| AI 教练 Web 应用 | DeepBrain |

> 注：DeepBrain 的 `upload` 阶段仍可把 mp3 上传到它自己的仓库；
> 但供 paraformer-v2 拉取的公开 URL 由 **PodcastDownloader** 在本项目内
> 上传到 `GITHUB_REPO` 指定的仓库 release 产生。
