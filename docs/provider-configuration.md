# 供应商配置与历史凭据迁移

2026-10-16 严格审查修复后，小米 MiMo 与 MiniMax 为两个独立供应商。程序不会读取密钥内容来猜测平台，也不会将旧变量中的凭据自动转发给另一平台。

| 项目 | 小米 MiMo | MiniMax |
|---|---|---|
| 内部标识 | `mimo` | `minimax` |
| 界面标签 | 小米 MiMo | MiniMax |
| 密钥变量 | `MIMO_API_KEY` | `MINIMAX_API_KEY` |
| 通用 OpenAI 兼容端点 | `https://api.xiaomimimo.com/v1` | `https://api.minimax.cn/v1` |
| 默认聊天模型 | `mimo-v2.6-pro` | `MiniMax-M2.7` |
| 模型覆盖变量 | `MODEL_MIMO` | `MODEL_MINIMAX` |

上述主机和模型参照 [小米 MiMo 首次 API 调用文档](https://mimo.mi.com/docs/en-US/quick-start/summary/first-api-call) 与 [MiniMax OpenAI 兼容文档](https://platform.minimax.cn/docs/api-reference/text-openai-api) 核对。模型与账户权限仍可能变化，真实调用尚未验收。

## 旧配置必须明确确认

旧版把 `MINIMAX_API_KEY` 同时称作 MiniMax 密钥、又发送给小米 MiMo，存在歧义。现在发现这个变量非空、但没有明确确认时，会禁用其调用并给出启动警告；密钥不会被转给 MiMo，也不会被立即改发给 MiniMax。

- 如果实际使用小米 MiMo：在小米 MiMo 平台确认通用 API 凭据，将其配置在 `MIMO_API_KEY`。
- 如果实际使用 MiniMax：确认 `MINIMAX_API_KEY` 确实属于 MiniMax 后，另设 `SUWAN_MINIMAX_KEY_CONFIRMED=1`。程序才会启用这个供应商。
- 本轮没有读取、改写或迁移机器上的实际 `.env`。警告文字和日志不包含密钥值。

当前实现仅接入小米 MiMo **通用 API**。Token Plan 使用独立端点和凭据；它们不能直接替换通用 API。`MIMO_API_KEY` 若是 `tp-` 或 `ttp-` 开头，会禁用并提示创建通用 API 凭据，避免意外调用或混淆套餐计费。[小米官方认证与端点说明](https://mimo.mi.com/docs/en-US/quick-start/summary/first-api-call)

## 语音与聊天供应商分别选择

当前语音实现固定为小米 MiMo：`MIMO_API_KEY`、通用 API 端点、`mimo-v2.5-tts`、WAV 格式和 MiMo 音色。它不读取 MiniMax 密钥，也不随聊天供应商切换。仅配置 MiniMax 聊天密钥并不能开启 MiMo 语音。[MiMo 语音合成官方文档](https://mimo.mi.com/docs/en-US/quick-start/usage-guide/audio/speech-synthesis-v2.5)

语音请求携带 API/运行时代数，关闭 API 后未执行的旧请求会失败；重新开启后须重新发起。已经交给供应商的同步调用不能被本地强行中止，但取消或超时后不会接收它的音频产物。

## 验证范围

自动回归只使用假密钥和假 SDK，检查客户端实际收到的平台、主机、模型、格式和开关状态；没有访问真实接口。涉及真实账户授权、计费与音色质量的验收须另行进行。
