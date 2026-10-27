# 全局上下文预算

`build_window()` 保持原有摘要边界和滑动窗口行为。统一检查位于
`app/core/llm.py` 的 `_get_request_payload()`：LangChain 已组装消息、工具定义、
参数 Schema 后，发送网络请求前执行。同步、异步、流式、非流式、结构化输出及
每一步 Agent 请求都经过同一个入口。没有引入新的记忆存储。

## 配置

| 环境变量 | 默认值 | 含义 |
| --- | --- | --- |
| `CHAT_CONTEXT_LIMIT` | 32768 | 模型请求的运行上限 |
| `CHAT_OUTPUT_RESERVE` | 2048 | 输出预留，同时限制上游输出参数 |
| `CHAT_OUTPUT_TOKEN_PARAMETER` | `max_tokens` | 上游实际接受的输出限制字段，可选 `max_completion_tokens` |
| `CHAT_CONTEXT_SAFETY_MARGIN` | 1024 | 模板和上游协议开销的额外余量 |
| `CHAT_MODEL_CONTEXT_LIMITS` | `{}` | 按精确模型名覆盖上下文上限的 JSON 对象 |

输入预算 = 上下文上限 − 输出预留 − 安全余量。各模型配置的上限必须大于后两项之和，
否则配置加载失败。调用方可进一步调低输出上限，不能提高到预留空间以上。
未匹配模型覆盖时沿用全局运行上限，意图模型和摘要模型也遵循这一规则。
原有 `TOKEN_BUDGET` 和 `CONTEXT_WINDOW_MAX_TOKENS` 不作为完整请求的上限。
当前 DeepSeek 通道按[官方 Chat Completions 文档](https://api-docs.deepseek.com/api/create-chat-completion/)
发送 `max_tokens`；需要 `max_completion_tokens` 的通道应显式切换配置。
最终入口会处理 LangChain 对字段的自动改名，确保发出的字段与配置一致。

当前本地配置使用 DeepSeek 官方的 `deepseek-flash`。2026-10-24 核对的
[官方模型文档](https://api-docs.deepseek.com/quick_start/pricing/)
标示上下文为 1M；默认 32768 是低于该能力的保守运行上限，没有自动放大现有历史窗口。
更换模型或通道时，应核对该部署实际支持的上下文限制并更新配置，不能直接照搬其他模型上限。

## 统计口径

日志 `context_budget_before` 和 `context_budget` 分开记录处理前后的固定提示词和工具定义、历史摘要、近期对话、
本轮业务材料、Agent 累积消息和工具结果，以及其余请求参数，并记录总量、
输入预算、输出预留、安全余量、模型名和采取的操作。预算日志不包含业务正文。

统计使用序列化内容的 UTF-8 字节数，加每消息和工具封装开销，作为文本 Token
的保守估算；`estimator=utf8_bytes_upper_estimate` 明确标注这一点。这不是上游的
精确 tokenizer 计数，也不是计费使用量。中文及长 JSON 往往会被明显高估，可能提前拒绝请求。
安全余量用于覆盖供应商模板差异；供应商实际 `usage_metadata` 仍沿用原有统计。
这个入口面向当前项目的文本 Chat Completions；多模态、状态式 Responses API 和通过
`extra_body` 覆盖消息/工具/模型/输出限制的请求会明确拒绝，避免漏算。

## 缩减规则

在现有 `build_window()` 之后，仅当完整请求超出输入预算时，依次执行：

1. 移除没有工具调用及其他字段的空 assistant 消息。
2. 从旧到新移除可确认非关键的完整历史轮次。目前仅认定封闭问候词表内的社交寒暄；
   未知文本、订单事实、政策条件、工具调用与结果、摘要、本轮用户消息和业务材料全部保留。
3. 检查过长工具结果，仅对合法 JSON 去掉字符串外的空白。字符串、数字原始表示、
   条件、订单号和字段均不改变；重复键 JSON 不压缩。
4. 完整重算。最多一次历史遍历和一次工具结果遍历；不进行语义摘要或按字符硬截断。
5. 仍超预算则抛出 `ContextBudgetExceeded`，不发送请求。结构化输出不对确定性超预算
   原样重试；主 Agent 返回明确说明及人工入口，trace 记录原因与各项预算。

知识证据可能包含跨段条件，不能安全证明非关键，因此保留完整证据，过长时明确拒绝。
订单数据及权限结果同样不做语义删减。所有处理作用于最终请求的副本，
不修改会话 State、数据库历史、摘要锚点、system 内容或工具定义/顺序。
预算充足时消息结构不变；本轮材料仍紧跟最后一条真实用户消息，保持原有轮内缓存前缀。

## 验证

`tests/test_context_budget.py` 覆盖完整请求分项、工具 Schema 独立超限、
单条超长消息、整轮寒暄裁剪、关键业务保护、JSON 无损压缩、模型覆盖、输出上限、
同步/异步及流式/非流式发送前拦截，以及退款路径的安全退出。
原有摘要窗口、Prompt Cache 前缀、结构化输出与用量统计测试同时回归。
