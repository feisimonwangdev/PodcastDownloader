# PodcastDownloader — 播客音频转写与分段

从 `in/<前缀>.txt/`文件读取播客链接， 下载音频文件存放在 `resources/<前缀>/` ，读取已下载的播客音频，调用 **AUDIO LLM** 做分角色转录，再调用
**文本 LLM** 做问答分段。产物按播客系列落到 `out/<前缀>/`（前缀取自 m4a 文件名）。

> 本项目只负责「音频 → 转录 → 分段」。Pattern 提取、AI 教练等下游功能在
> **DeepBrain** 项目中（见下文「与 DeepBrain 的分工」）。

## 文件命名规则（关键）

资源文件遵循：`<前缀>_Vol.<号>.m4a`，例如 `Qianjing_Vol.101.m4a`。

- **前缀**即播客标识，不同播客用不同前缀（如 `Qianjing_`、`SomeOther_`）。
- 该前缀**从 m4a 文件名自动派生**，代码中**绝不硬编码**。换播客无需改代码。
- 所有产物共享同一个 `base`（= 资源文件主干，含前缀）：

| 产物 | 路径 |
|---|---|
| 原始 ASR JSON | `out/<前缀>/<base>_Transcription.raw.json` |
| 角色分离可读稿 | `out/<前缀>/<base>_Transcript.txt` |
| 问答分段 | `out/<前缀>/<base>_segments.json` |

`<前缀>` 为 m4a 文件名 `_Vol.` 之前的部分（不含下划线，如 `Qianjing`），作为
`out/` 下的第一层目录；不同播客自动归入各自目录。

例如 `Qianjing_Vol.101.m4a` → `out/Qianjing/Qianjing_Vol.101_Transcription.raw.json` 等。

## 目录结构

```
PodcastDownloader/
├── pipeline.py            # 统一入口
├── in/                    # 播客链接清单（每个系列一个 Xxxxxx.txt）
├── resources/             # 已下载音频（按系列分子目录）
│   └── <前缀>/
│       └── <base>.m4a
├── out/                   # 转写产物（按系列分子目录）
│   └── <前缀>/
│       ├── <base>_Transcription.raw.json
│       ├── <base>_Transcript.txt
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
python3 pipeline.py sync                   # 读取 in/ 链接 -> 下载 -> 转录 -> 分段（推荐）
```

处理流程：

```
resources/<前缀>/<base>.m4a
   │
   ▼ transcribe   — 转码为 mp3 → 上传 GitHub Release → paraformer-v2 ASR → raw JSON
   ▼ transcript   — raw JSON → 角色分离对话文本
   ▼ segment      — transcript → LLM 问答切分
```

## 从链接批量下载并跑全流程（in/ + sync）

把播客单集链接放进 `in/` 目录，**一个系列一个 `.txt` 文件**，文件名即前缀：

```
in/
├── Qianjing.txt      # 前缀 Qianjing_
└── SomeOther.txt     # 前缀 SomeOther_
```

每个 `.txt` 内部，一集一行链接，可含空行（空行/非 http 行会被自动跳过）：

```text
https://www.xiaoyuzhoufm.com/episode/6a56ef13ca0de6c44ae741b7

https://www.xiaoyuzhoufm.com/episode/xxxxxxxxxxxx
```

然后执行：

```bash
python3 pipeline.py sync
```

`sync` 会自动完成：

1. **下载**：读取 `in/*.txt` 全部链接；从链接页面解析标题中的 `Vol.XXX`
   作为期号、从 `og:audio` 解析音频直链；保存到
   `resources/<系列>/<前缀>Vol.<号>.m4a`（`<系列>` 为 base 中 `_Vol.` 之前部分，
   如 Qianjing；前缀来自 `.txt` 文件名，绝不硬编码）。
2. **转录 / 稿本 / 分段**：对已下载音频依次执行三阶段。

**幂等续跑**：每个阶段都按产物是否存在来跳过——已下载的不再下、已转录的
不再转录、已分段的不再分段。中途失败或新增链接后重新运行 `sync` 即可，**缺哪
补哪**，不会重复消耗 ASR/LLM 额度。运行结束会打印总结（下载/转录/稿本/分段
各自的新增·已存在·失败计数）。

> 新增一个播客系列只需在 `in/` 放一个 `Xxxxxx.txt` 并填入链接，无需改动任何代码。

转录阶段读取 **本地** `resources/<前缀>/`，经 ffmpeg 转码为 mp3 后，上传到
**GitHub Release**（默认 `feisimonwangdev/PodcastDownloader`，可用 `GITHUB_REPO`
覆盖）得到公开下载 URL，再提交给 paraformer-v2。上传优先使用已登录的 `gh`
CLI，未登录时回退到 GitHub REST API（需 `GITHUB_TOKEN`）。

> ⚠️ 托管音频的仓库必须是**公开**仓库，否则 ASR 服务端无法匿名拉取音频。
