# 豆包 ASR 转写 Skill

面向 AI Agent 的豆包录音文件识别模型 2.0 标准版工作流，也可独立运行 Python 脚本。提供可恢复的异步任务、真实词级时间信息和停顿候选导出。

## 安装

下载本仓库，放入你的 Agent 的技能目录，将目录命名为 `doubao-asr`；根目录的 `SKILL.md` 是入口。也可以直接把 `SKILL.md` 和脚本路径交给 Agent 使用。不自动修改系统配置、不安装后台服务。

运行需要 Python 3.10+，没有第三方 Python 依赖。FFmpeg 仅在从视频提取音频时需要；已经有合适音频则不需要。

## 配置与转写

先在火山引擎开通豆包录音文件识别模型 2.0 标准版。调用可能产生费用，按控制台实际套餐计费。

在本机进程环境中配置以下变量，或在真实终端加 `--prompt` 隐藏输入。不要将实际值放进聊天、Git、脚本或命令行参数。

| 变量 | 用途 |
|---|---|
| `DOUBAO_API_KEY` | 新版控制台 API Key，默认鉴权方式 |
| `DOUBAO_APP_ID` | 旧版应用 App ID，仅 `--auth legacy` 使用 |
| `DOUBAO_ACCESS_TOKEN` | 旧版 Access Token，仅 `--auth legacy` 使用 |
| `DOUBAO_AUDIO_URL` | 用户授权的、服务端可读取的 HTTPS 音频直链 |

两种鉴权方式选一种，无需 Secret Key。环境变量的值由你在本机安全配置或由既有凭据管理工具注入，本仓库不提供含真实值的配置样例。

示例在仓库目录执行，`audio.wav` 和 `output/asr` 替换为你的本地路径：

```sh
python scripts/doubao_asr.py transcribe --audio "audio.wav" --outdir "output/asr" --format wav --prompt
```

旧版应用：

```sh
python scripts/doubao_asr.py transcribe --audio "audio.wav" --outdir "output/asr" --format wav --auth legacy --prompt
```

隐藏输入模式会依次请求缺少的本机变量值，不保存它们；无交互终端时使用环境变量。不要让 Agent 在对话里代收凭据。

本地音频必须与 HTTPS 地址指向的字节内容相同。脚本记录本地音频 SHA-256 来防止误用旧任务，但不下载直链来验证远程内容。URL 不写入任务文件；你自行提供的存储服务应保持链接在任务期间有效。脚本不会替你公开文件、上传到第三方平台或绕过访问控制。

可选地提取音频（需要已有 FFmpeg；`-n` 避免覆盖）：

```sh
ffmpeg -n -i "input.mp4" -vn -ac 1 -ar 16000 -c:a pcm_s16le "audio.wav"
```

标准版异步任务可能排队较久。默认只查询一次：退出 `0` 为完成，`2` 为待完成，`1` 为需处理的错误。再次执行原命令会恢复查询。完成后重复执行会重用本地结果，不重新收费。等待预算可设 `--wait-seconds 60`，轮询间隔默认 20 秒。

同一输出目录只用于一个音频任务。不要为了重试删除 `task.json`；提交结果不明确时先查询保存的任务 ID，确认服务端状态后再决定是否创建新任务。更换音频或需要不同识别配置时选新目录并明确发起新任务。

## 输出

| 文件 | 内容 |
|---|---|
| `task.json` | 本地任务 ID、源文件哈希、模型资源和数字状态码；无凭据或音频 URL |
| `response.json` | 服务返回的识别字段快照：音频时长、正文、句、词；不归档其他诊断/请求回显字段 |
| `transcript.json` | 句、实际词时间、停顿候选、粒度标记 |
| `words-ms.json` | 词级时间表，时间单位 ms |
| `transcript.txt` | 带秒时间码的逐句稿 |
| `gap-candidates.csv` | 句内/句间时间空档候选，便于听审 |

词级不保证逐字，毫秒单位不保证毫秒精度。没有词时明确降级，不生成假字级时间。停顿候选不代表静音；应与原声、波形一起判断卡壳和剪口。所有结果均为你的私有内容，`.gitignore` 不替代发布前审查。

识别快照会清理本次进程已知的凭据和媒体链接；它不对转写正文做全面内容脱敏。跨进程恢复不会重新读取已丢弃的媒体链接，不能承诺识别正文里的任意未知敏感内容都被清除。

## 离线测试

```sh
python -m unittest discover -s tests -v
```

测试使用合成文本和模拟 API，不发送媒体、不调用收费识别服务。当前公开版已做离线行为测试，未用真实账号执行端到端联网转写；接口依照官方文档及既有工作流整理。

## 范围与许可

仅固定官方识别端点，不含个人路径、项目素材、聊天记录、私人存储账号或历史 Git 数据。不依赖特定剪辑软件。标准版请求不会自动变为极速版、流式版或其他计费资源。

本仓库当前未指定开源许可证；公开可见不代表授予任意再分发许可。

参考：[任务提交](https://docs.volcengine.com/docs/DoubaoVoice/task-submission-http-1?lang=zh)、[结果查询](https://docs.volcengine.com/docs/DoubaoVoice/result-query-http-1?lang=zh)。
