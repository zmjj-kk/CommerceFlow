# CommerceFlow 智能客服 Agent：12 周系统学习计划

> 依据当前项目实际 README、Makefile 与 app 源码组织制定。建议每周学习 6 天、每天 2–3 小时。目标：理解原理、追踪源码、独立实验、测试验证、面试表达与工程改造。

## 0. 当前已知项目基线
- Chat：DeepSeek `deepseek-flash`；Embedding：智谱 `embedding-3`（2048 维）；Rerank：智谱 `rerank`。
- 组件：FastAPI、LangGraph、MySQL、Milvus、MinIO、etcd、两台 MCP Server。
- 初始知识库：52 条文档向量；功能验证曾覆盖 Function Calling、物流 MCP、BM25/Dense/Hybrid/Hybrid+Rerank。
- 现有 Rerank 分数接近 1.0，旧 BGE 校准的置信度阈值未重新评测，不能直接认为效果可靠。
- 训练模块 ch10 的 `ml` 依赖组按需安装，默认 Python 环境不等于模型训练环境已准备好。

## 1. 每周统一学习方式（6 天）
- D1：先画业务流程图；不看实现，先能讲输入、状态、输出和失败路径。
- D2：精读本周关键源码，写调用栈、核心类型、数据流；补齐必要框架原理。
- D3：运行项目已有测试和对应 demo / smoke，记录实际现象。
- D4：使用 Mock 或测试环境注入边界条件（空结果、超时、权限、异常）；不要破坏已有数据库。
- D5：完成一个局部可回滚的小改造，添加相应测试。
- D6：写 1 页总结、1 张流程图、3–5 个面试问答，讲给自己听；第 7 天休息/补漏。

## 2. 十二周安排

### 第 1 周：全景架构、FastAPI 与模型调用（ch01）
- 读：`README.md`、`Makefile`、`app/main.py`、`app/config.py`、`app/api/chat.py`、`app/core/llm.py`、`app/schemas/`。
- 学：FastAPI 路由与 lifespan、Pydantic Settings、异步 HTTP、SSE/流式、OpenAI 兼容接口、tool_calls、结构化输出与 usage。
- 实验：浏览器/Swagger 发请求、读流式事件；对比普通模型调用与工具选择；使用 mock 验证结构化解析。
- 验收：画出 `HTTP → API → LLM → HTTP` 链路；解释同步/异步/流式差异；可定位模型 401、429、超时。
- 对应测试：`tests/test_config.py`、`tests/test_llm.py`、`tests/test_llm_structured.py`、`tests/test_chat_api.py`。

### 第 2 周：SQLAlchemy、MySQL 与 Function Calling（ch02）
- 读：`app/db/base.py`、`models.py`、`repository.py`、`app/tools/business.py`、`app/tools/builtin/`、`app/api/agent.py`。
- 学：异步数据库连接池、事务、DAO/Repository、函数 Schema、参数验证、ToolMessage、工具结果进入模型。
- 实验：追踪订单 1001 查单 → 获取运单号 → 查询物流；测试参数错误、订单不存在、不属于当前用户。
- 验收：独立新增一个只读 Mock 工具并编写测试；说明“模型决定调用”不等于“程序允许执行”。
- 对应测试：`tests/test_repository.py`、`tests/test_agent_api.py`、`tests/test_tools_mock.py`；`make eval-agent` 需服务就绪。

### 第 3 周：知识库清洗、切块、双写和增量更新（ch03）
- 读：`app/kb/chunking.py`、`documents.py`、`dedup.py`、`dualwrite.py`、`milvus_client.py`、`app/core/embeddings.py`、`scripts/build_kb.py`、`scripts/vectorize_kb.py`。
- 学：Chunk 边界、Overlap、元数据、文档指纹、MySQL pending/done、Milvus PK、稠密向量维度一致性、失败恢复。
- 实验：只在独立测试库或临时 Collection 上新增示例；重复入库、模拟嵌入失败后重跑，观察幂等性。
- 验收：能说明为什么先 pending 后向量化；画出“文档→Chunk→MySQL→Embedding→Milvus→状态回填”。
- 对应测试：`tests/test_documents.py`、`tests/test_dualwrite.py`、`tests/test_embeddings.py`、`tests/kb/`。

### 第 4 周：混合检索、RRF、Rerank 与离线评测（ch04）
- 读：`app/core/retrieval.py`、`query_understanding.py`、`rerank.py`、`app/kb/milvus_client.py`、`scripts/eval_ch04.py`。
- 学：BM25、Dense/COSINE、混合召回、RRF、重排、Query Rewrite、多子句、Recall@K/MRR/NDCG、忠实度。
- 实验：整理至少 20 个有标准答案的问题；四种策略对照，记录准确率、耗时、成本；把失败 Case 分类。
- 特别注意：智谱 Rerank 的分数可能高度饱和，旧置信度阈值需要数据集重新校准，勿直接调一个好看的数字。
- 验收：能准确解释“未召回、排序靠后、无关召回、文本不完整”四类问题的针对性修复。
- 对应命令：`python scripts/smoke_configured_rag.py`、`python scripts/smoke_configured_rerank.py`（在项目虚拟环境下并配置 PYTHONPATH）；`make eval-rag SKIP_GEN=1`（须确认评估前提与调用费用）。

### 第 5 周：LangGraph 状态、路由与 ReAct（ch05）
- 读：`app/graph/state.py`、`build.py`、`routing.py`、`nodes.py`、`runtime.py`。
- 学：StateGraph、节点/边/条件边、消息 reducer、状态合并、ReAct 循环、should_continue、max_agent_steps、Checkpoint。
- 实验：跟踪物流、知识咨询、投诉三条路径；修改单个只读节点/测试临时图，观察状态传播与回环退出。
- 验收：不看代码画出实际图，讲清楚 main_agent → agent_tools → main_agent 何时停止。
- 对应测试：`tests/graph/`、`scripts/smoke_langgraph.py`、`make eval-ch05`。

### 第 6 周：意图路由、指代消解、退款工作流及人工确认（ch06）
- 读：`app/core/intent.py`、`coref.py`、`app/graph/nodes.py` 中 `fetch_order/retrieve_policy`、`app/api/actions.py`。
- 学：意图分类的置信度、确定性节点与模型节点分工、订单归属鉴权、interrupt/resume、人工确认和中断恢复。
- 实验：分别构造缺订单号、非本人订单、用户取消/确认等案例，观察 Checkpoint 和最终副作用。
- 验收：画出退款流程，说明为何 interrupt 前只做只读操作；能演示正常恢复与拒绝越权请求。
- 对应命令：`make smoke-interrupt`；谨慎使用 `make eval-ch06`，注意测试数据与脚本副作用。

### 第 7 周：长会话上下文、摘要与缓存（ch07）
- 读：`app/core/memory.py`、`summarizer.py`、`app/graph/runtime.py`、`app/graph/nodes.py` 中消息组装相关函数。
- 学：长短期上下文、滑窗、Rolling Summary、summary_upto_msg_id、持久化 Checkpoint、Prompt Cache 前缀稳定性。
- 实验：多轮追问与跨话题指代；对比摘要前后消息数组、Token、cache_read；模拟历史摘要边界。
- 验收：准确区分“Checkpoint、消息历史、摘要、长期知识库”四种存储职责。
- 对应测试：`tests/test_memory.py`、`tests/test_summarizer.py`、`tests/test_prompt_cache_prefix.py`；`make eval-ch07`。

### 第 8 周：MCP、工具注册发现、工具可靠性（ch08）
- 读：`app/tools/registry.py`、`mcp_client.py`、`engine.py`、`builtin/`、`mcp_servers/logistics_server.py`、`aftersales_server.py`。
- 学：本地工具和远程 MCP Tool 的区别、Streamable HTTP、动态发现、工具适配、参数验证、超时、重试、审计。
- 实验：新增一个可 Mock 的 MCP 只读工具；模拟超时、429、连接中断及返回结构异常。
- 验收：解释何时允许重试、为何写操作不能盲目重试、如何定位工具故障。
- 对应测试：`tests/tools/`、`tests/test_tool_ticket.py`、`make eval-ch08`。

### 第 9 周：证据门控、可观测、评测及知识飞轮（ch09）
- 读：`app/core/confidence.py`、`selfcheck.py`、`observability.py`、`flywheel.py`、`app/api/review.py`、`app/api/feedback.py`。
- 学：Trace/Span/结构化日志、Latency/Token/Cost、低置信问题池、人工审核、飞轮补库、阈值校准与线上回归。
- 实验：选一批高质量、低质量、有歧义问题，观察召回分数和拒答；尝试基于标注集分析阈值。
- 验收：区分“系统可运行”和“回答可信”；以实际数据报告 3 项指标变化。
- 对应命令：`make flywheel`、`make calibrate-confidence`、`make eval-flywheel`（调用外部模型可能收费，先检查数据与执行范围）；Langfuse 独立容器栈为可选实验。

### 第 10 周：问题主题分类模型、数据集、微调与 ONNX（ch10）
- 读：`scripts/ch10/`、`app/core/taxonomy.py`、`app/api/topics.py`、`app/api/acceptance.py`。
- 学：语料构造、人工预标、数据泄漏、训练/验证/测试切分、P/R/F1、混淆矩阵、错误分析、模型导出与服务。
- 实验：先以小样本完成数据划分和评估；有充足算力再安装 `uv --group ml` 对应依赖与训练。
- 验收：独立解释为什么 macro/micro 指标不同，能根据错误分析判断是否该部署模型。
- 对应命令：`make ch10-golden`、`make ch10-dataset`、`make ch10-eval`；训练与导出按 GPU/内存条件决定。

### 第 11 周：工程化升级专题
- 重点：工具调用安全审查、明确读写分级、执行超时、指数退避、幂等操作 ID、审计、限流、故障降级和服务启动管理。
- 参考：`app/tools/engine.py`、`app/graph/routing.py`、`app/core/model_guard.py`、`tests/`。
- 实验：选择一个低风险可靠性改动，例如为只读工具增加故障 Mock 覆盖与可观测字段；写失败注入单测。
- 验收：提交一份“现状→风险→方案→实现→回归→指标”的技术记录；区分既有能力和计划能力。

### 第 12 周：综合验收、成果复盘与面试表达
- 画四张图：系统部署图、一次知识问答链路图、工具/MCP 调用图、人工审核飞轮图。
- 完成四类案例回放：知识检索、物流工具、退款中断、证据不足拒答。
- 产出：源码导读、一组能跑的测试、技术难点与改造清单、STAR 项目故事、15–20 个面试问答。
- 量化必须源自自己实测；未评测不写“提升了 xx%”。区分“已跑通的演示架构”和“达到生产级”的差距。

## 3. 建议重点源码阅读顺序

`app/main.py → app/api/agent.py → app/graph/runtime.py → app/graph/build.py → app/graph/state.py → app/graph/routing.py → app/graph/nodes.py → app/core/llm.py → app/tools/registry.py → app/tools/engine.py → app/tools/mcp_client.py → app/core/retrieval.py → app/kb/dualwrite.py → app/kb/milvus_client.py → app/core/memory.py → app/core/observability.py`

遇到不理解的函数，按“谁调用→接受什么参数→改了什么状态→调用了哪些 I/O→如何失败/重试→对应测试”逐层追踪。不要按字母表阅读整个目录。

## 4. 安全操作与建议命令
从 WSL 执行：

~~~bash
cd "/mnt/c/Users/Lenovo/Desktop/MEWHELP python"
make help
docker compose ps
uv run pytest -q tests/test_config.py tests/test_embeddings.py
# 模型调用/评估前确认配额、数据范围和费用
~~~

- 学习期间创建独立分支或复制实验环境，不要在真实数据上试破坏性操作。
- `make kb-reset` 会清空知识块和重建向量集合；不要用它作为日常学习演示。
- `make seed`、批量评测、训练与飞轮脚本可能写库或产生模型费用，应提前查看脚本。
- 禁止把 `.env`、模型 API Key、生产用户数据复制到学习笔记或 Git 仓库。
- Rerank 已切换为智谱，旧默认模型的分数阈值需要重新评估。

## 5. 每周笔记模板
- 本周业务目标和输入输出：
- 关键源码入口、调用顺序和对象生命周期：
- 核心机制（自己解释）：
- 一张流程图：
- 自己运行的命令、观察到的输出：
- 模拟的失败场景、为何发生、如何恢复：
- 新增/修改的测试：
- 尚未搞懂的 3 个问题：
- 面试三问三答：

## 6. 深度掌握检查标准
一个模块只有同时满足下列条件才算掌握：
1. 不看代码能在 3 分钟内描述核心调用链；
2. 能在真实代码中定位相关函数；
3. 能手动复现实验并说明输出为何如此；
4. 能演示至少一种故障或边界路径；
5. 能完成最小修改并提供通过的测试；
6. 能说清该模块与主流 Agent 工程实践之间的取舍。
