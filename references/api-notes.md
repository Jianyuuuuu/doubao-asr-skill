# API 与时间戳注意事项

核对日期：2026-09-23。服务的模型、配额和接口行为以官方文档与控制台为准。

- 官方异步端点：`https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit`、同目录的 `query`。
- 标准版 2.0 资源：`volc.seedasr.auc`；请求模型名称：`bigmodel`。
- 新版鉴权：`X-Api-Key`。旧版兼容：`X-Api-App-Key` + `X-Api-Access-Key`。不要将 TTS、极速转写或其他产品的资源 ID 混用。
- 每个任务用一个 UUID；提交和查询使用同一 `X-Api-Request-Id`，`X-Api-Sequence` 为 `-1`。
- HTTP 成功不代表识别成功，需检查 `X-Api-Status-Code`。`20000000` 为成功，`20000001` / `20000002` 为处理中/排队中。其他错误先看数字代码并查官方说明；不要仅因为响应正文为空就重提。
- 官方提交页描述文件不超过 512 MB、时长不超过 5 小时。脚本仅检查本地大小，时长需事先通过媒体信息确认。请求支持格式列表见 `--help`；容器后缀、真实编码和 `--format` 必须一致。
- 标准任务通常可能需要数小时；媒体 URL 必须覆盖服务端实际抓取窗口。URL 到期、需要网页 Cookie 或返回 HTML 会导致读取失败。

脚本将 `show_utterances` 开启，请求句和词信息；原始口播分析关闭语义顺滑 `enable_ddc` 和文本规范化 `enable_itn`。这些选项不会让识别变成逐字强制对齐，也不保证保留每个口误。

停顿候选基于词的结束到下一词开始：同一 `utterance` 标为句内，跨 `utterance` 标为句间。模型分句可能不同于真实语义；没有词级返回时不计算假停顿。词时间有重叠时以已覆盖的最远终点计算后续间隙，防止制造伪空档。未经波形和听感验证，不据此直接删除声音。

源音频片段的时间从零开始。若剪辑素材从整条录制的 30 秒处提取，回到原片要加上这个偏移；变速还需要按实际剪辑映射换算。本技能不猜测这些关系。

程序不自动重试 `submit`。提交后连接断开时记录 `submit_unknown`，下一次仅查询保存的任务。任务不存在、鉴权错误、资源未开通等情况需要明确处理后决定重提，不用循环创建新任务解决。

官方资料：
- [任务提交 HTTP](https://docs.volcengine.com/docs/DoubaoVoice/task-submission-http-1?lang=zh)
- [结果查询 HTTP](https://docs.volcengine.com/docs/DoubaoVoice/result-query-http-1?lang=zh)
- [旧版控制台鉴权参考](https://docs.volcengine.com/docs/6561/2534847?lang=zh)
