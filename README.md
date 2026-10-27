# CommerceFlow

**电商售后智能服务系统**

CommerceFlow 面向电商商品咨询、订单物流、政策问答与售后申请，将确定性 Workflow 与具备工具调用能力的 Agent 结合，覆盖检索问答、意图分流、证据约束、人工审批、可观测性和知识反馈闭环。

项目基于已有课程代码及后续工程升级整理，保留源码中的原始来源署名。主题分类微调是独立的离线实验与旁路能力，不是每次在线回答的必经步骤。

[快速开始](#快速开始) · [核心功能](#核心功能) · [系统架构](#系统架构) · [测试与评估](#测试与评估) · [文档](#文档) · [上传 GitHub](GITHUB_UPLOAD.md)

## 核心功能

| 模块 | 实现内容 |
| --- | --- |
| 意图与流程编排 | LangGraph 九类意图、五类路由出口；知识问答、订单业务和售后申请按不同路径处理 |
| RAG 混合检索 | BM25 与 Embedding 双路召回、RRF 融合、Reranker 精排，支持查询改写、拆分及元数据过滤 |
| 证据校验 | 基于精排分、有效证据数、分数差和关键条款命中构建证据信度，生成前决定回答或兜底 |
| 文档生命周期 | Document、Version、Chunk 分层，提供独立 v2 索引、版本发布、失效与删除管理 |
| 上下文管理 | 最近轮次窗口、历史 Token 限制、异步摘要和完整请求预算检查 |
| 工具与 MCP | 内置订单、FAQ、工单和售后工具；独立物流及售后 MCP Server，统一注册、执行、审计与限时调用 |
| 身份与审批 | 账户认证、资源归属校验、一次性审批、工具能力授权及 MCP Schema 白名单 |
| 知识反馈闭环 | 收集低置信度与待解决问题，经标准化、查重和人工审核后补充知识库 |
| 可观测性 | 可选 Langfuse 调用追踪，以及评测、反馈、Token 与成本统计页面 |
| 主题分类实验 | 17 类问题主题的语料处理、多标签训练、阈值扫描、ONNX 导出和旁路推理服务 |

## 系统架构

```mermaid
flowchart TD
    Browser[聊天与管理页面] --> Auth[FastAPI 身份认证与资源授权]
    Auth --> Graph[LangGraph Workflow]
    Graph --> Intent[指代消解与意图识别]
    Intent --> Knowledge[知识问答路径]
    Intent --> Business[业务 Agent 路径]
    Intent --> Refund[售后申请路径]
    Intent --> Fallback[固定回复与人工兜底]
    Knowledge --> Retrieve[BM25 + Embedding + RRF + Reranker]
    Retrieve --> Evidence[生成前证据校验]
    Evidence --> Answer[基于证据回答或兜底]
    Business --> Tools[统一工具执行引擎]
    Refund --> Approval[确认与一次性审批]
    Approval --> Tools
    Tools --> MCP[内置工具 / MCP Server]
    Graph --- Memory[会话窗口与摘要]
    Graph --- Observe[事件、审计与 Langfuse]
    Answer --> Review[问题池与人工审核]
    Review --> KB[MySQL / Milvus 知识库]
    KB --> Retrieve
```

模型可以提出工具调用，但资源归属、审批有效性和工具执行权限由后端检查。订单和物流包含按用户隔离的演示数据，不代表已经对接真实电商平台。

## 技术栈

- **应用与编排**：Python 3.12+、FastAPI、Asyncio、LangChain、LangGraph。
- **数据与检索**：SQLAlchemy、MySQL、Milvus、BM25、Embedding、RRF、Reranker。
- **工具与安全**：MCP、JSON Schema、JWT、Argon2、资源授权与一次性审批。
- **观测与实验**：Langfuse、pytest；可选 PyTorch、Transformers、ONNX Runtime。
- **部署**：Docker Compose；主应用和 MCP Server 作为独立进程运行。

聊天、Embedding 和 Reranker 使用独立的上游配置。默认示例采用 BGE-M3 和 BGE-Reranker 兼容接口，也可以按实际服务配置其他模型；模型名称、接口与向量维度必须相互匹配。

## 快速开始

### 环境要求

- 建议使用 Linux 或 Windows WSL2 运行 Bash / Make 开发脚本。
- Python 3.12+、`uv`、Docker Compose，以及运行开发脚本所需的 `make`。
- 可用的聊天、Embedding 和 Reranker 服务凭证。

### 1. 安装依赖

在仓库根目录执行：

```bash
uv sync --frozen
cp .env.example .env
```

编辑 `.env`，配置自己的模型服务：

```dotenv
CHAT_BASE_URL=https://your-chat-provider.example/v1
CHAT_MODEL=your-chat-model
CHAT_API_KEY=your-chat-api-key
EMBED_API_KEY=your-embedding-api-key
RERANK_API_KEY=your-reranker-api-key
```

按照所选服务填写 `EMBED_BASE_URL`、`EMBED_MODEL`、`EMBED_DIM`、`RERANK_BASE_URL` 和 `RERANK_MODEL`。三个服务是否可以共用密钥取决于供应商；不能假定它们一定相同。也支持显式设置共享检索密钥别名 `COMMERCEFLOW_RAG_API_KEY`。

`.env` 只用于本地配置。项目的 Git 忽略规则排除了密钥文件、日志、数据库、模型权重和本地工作目录。

### 2. 配置身份认证

生成随机签名密钥：

```bash
uv run python -c "import secrets; print(secrets.token_urlsafe(48))"
```

将生成结果写入本地 `.env` 的 `AUTH_SIGNING_KEY`。本地 HTTP 开发还需要按实际访问地址配置：

```dotenv
AUTH_SIGNING_KEY=replace-with-your-generated-random-secret
AUTH_COOKIE_SECURE=false
AUTH_ALLOWED_ORIGINS='["http://localhost:8000"]'
AUTH_REGISTRATION_ENABLED=false
```

部署 HTTPS 服务时使用安全 Cookie 和正确的受信来源。账户创建、旧资源归属迁移、MCP Token 与 Schema 白名单的详细步骤见[安全部署说明](docs/CommerceFlow_安全升级实施与部署说明.md)。

### 3. 启动数据服务

首次建立独立开发环境时执行：

```bash
docker compose up -d
docker compose ps
```

主 Compose 文件提供 MySQL、Milvus、etcd 和 MinIO。当前 `docker-compose.override.yml` 将 MySQL 宿主端口覆盖为 **3307**，请让应用配置与实际映射一致。例如，全新本地演示环境使用：

```dotenv
DATABASE_URL=mysql+asyncmy://root:root@127.0.0.1:3307/commerceflow
TEST_DATABASE_URL=mysql+asyncmy://root:root@127.0.0.1:3307/commerceflow_ch02_test
```

以上账号密码来自仓库的本地演示 Compose 默认配置，不适用于对外部署。已有环境应继续使用自己原来的连接配置，先阅读[名称迁移说明](docs/CommerceFlow-名称迁移说明.md)，不要为了改名重建或删除数据卷。

SQL 脚本仅在新 MySQL 数据卷初始化时自动执行；已有数据库需要按安全部署和 RAG 升级说明逐项应用迁移。

### 4. 创建账号与运行服务

数据库表就绪后，通过交互式脚本创建账户，不将密码放在命令行中：

```bash
uv run python -m scripts.manage_users admin --admin
```

启动主应用：

```bash
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

打开 <http://localhost:8000/login> 登录。

物流与售后 MCP Server 可在独立终端运行：

```bash
uv run python -m mcp_servers.logistics_server
uv run python -m mcp_servers.aftersales_server
```

本地 HTTP MCP 需要显式开发开关、对应服务 Token 和经过审核的 Schema 白名单。不要通过关闭身份校验来绕过初始化问题。配置完成后，Linux / WSL 环境也可以使用 `make dev` 统一拉起开发服务。

### 5. 建立知识库

在模型服务、数据服务和数据库迁移就绪后执行：

```bash
make kb-build
make kb-vectorize
```

RAG v2 的初始化、双写、评测与回滚独立管理，当前默认读取模式为 `legacy`，不能仅因存在 v2 代码就假定已切换新索引。操作步骤见[RAG v2 实施与验证](docs/CommerceFlow-RAG-v2实施与验证.md)。

## 页面与端口

| 入口 | 用途 |
| --- | --- |
| `/login` | 登录 |
| `/` | 客服聊天 |
| `/kb` | 知识库录入与管理 |
| `/review` | 知识反馈待审队列 |
| `/observability` | 观测与成本 |
| `/topics` | 问题主题分布 |
| `/acceptance` | 分类器验收 |

| 端口 | 服务 |
| --- | --- |
| 8000 | FastAPI 主应用 |
| 8101 / 8102 | 物流 / 售后 MCP Server |
| 8110 | 可选主题分类器推理服务 |
| 19530 | Milvus |
| 3307 | 当前覆盖配置下的 MySQL 宿主端口 |
| 3000 | 可选 Langfuse Web |

管理页面及其 API 受管理员权限约束；业务接口仍会核验资源归属。

## 测试与评估

```bash
uv run pytest -q
node --test tests/frontend/auth.test.cjs
```

测试中包含模拟模型的单元测试、SQLite 安全回归，以及需要真实 MySQL / Milvus 的集成测试。数据库测试会重建专用测试库表，必须使用独立的 `*_test` 数据库，不能指向业务库。

检索与生成评估通过独立脚本执行：

```bash
make eval-retrieval
make eval-rag
make calibrate-confidence
```

真实评估需要可用的知识库和上游服务；报告中的数据集、样本量和环境决定了结论适用范围，不能将局部实验指标直接当作线上服务指标。

主题分类训练和导出依赖单独的 `ml` 依赖组：

```bash
make ch10-corpus
make ch10-dataset
make ch10-train
make ch10-eval
make ch10-export
```

实际目标及其前置条件以 `make help` 和 Makefile 为准。

## 项目结构

```text
app/
├── api/          # HTTP 接口、身份与管理功能
├── graph/        # LangGraph 状态、节点、分流和运行管理
├── core/         # 模型、检索、上下文、证据校验、飞轮与观测
├── kb/           # 文档、切块、去重、索引与版本生命周期
├── tools/        # 注册表、统一执行引擎、内置工具与 MCP 客户端
├── security/     # 主体、授权、审批与操作事务
├── db/           # 数据模型和仓储
└── static/       # 聊天与管理页面
mcp_servers/      # 物流、售后 MCP 服务
scripts/          # 建库、评估、账户管理与分类器实验
sql/              # 数据库初始化及迁移
tests/            # 单元、安全、集成与前端测试
docs/             # 设计、升级和验证记录
```

## 文档

- [上下文预算](docs/context-budget.md)
- [安全升级与部署](docs/CommerceFlow_安全升级实施与部署说明.md)
- [RAG v2 实施与验证](docs/CommerceFlow-RAG-v2实施与验证.md)
- [RAG v2 检索对照评测](docs/CommerceFlow-RAG-v2检索对照评测结果.md)
- [名称与已有环境迁移](docs/CommerceFlow-名称迁移说明.md)
- [上传到 GitHub](GITHUB_UPLOAD.md)

`docs/superpowers/` 保存历史设计与实施计划。当前行为以代码、测试和较新的升级记录为准；早期匿名请求、共享订单和自动执行示例不能替代现有身份与审批流程。
