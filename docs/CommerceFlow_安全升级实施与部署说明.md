# CommerceFlow 认证、资源授权与工具权限升级实施说明

日期：2026-10-21。此文记录代码交付与部署步骤；原两份方案保留为设计基线。测试结果见文末验收记录。

## 已实现的调用链

账户创建 → Argon2id 校验 → 短期 JWT Access Token + HttpOnly Refresh Cookie → FastAPI 可信主体 → 会话归属校验 → LangGraph → 工具 SecurityGate → 精确审批 → 业务事务、幂等记录及审计。

- 默认关闭公共注册；通过运维 CLI 创建用户/管理员。HTTP 注册不能指定身份和角色。
- Access Token 固定 HS256，要求 sub/sid/jti/iat/exp/iss/aud/token_type；每次访问复核活动账户和未撤销会话链。
- Refresh Token 为安全随机串，数据库仅留 SHA256 摘要。刷新原子轮换；旧令牌重放撤销整条会话链。登出立即撤销该链对应的 Access Token。
- 登录/注册使用数据库 IP 与规范化用户维度限流，可跨应用进程累计。
- 浏览器 Access Token 仅留内存；Refresh Cookie 为 HttpOnly、SameSite=Strict，生产默认 Secure。refresh/logout 必须校验 Origin；登录拒绝非允许来源。
- /api/agent、/api/chat 不再接收 user_id。会话列表忽略客户端身份参数，只返回当前用户；历史、反馈、继续对话与恢复中断均校验所有者。
- SSE 在响应开始前完成认证与已有会话校验；恢复先验证会话，再读取 Checkpoint，检查其中主体及真实待处理中断。
- 管理 API 统一要求 admin。HTML 页面本身只包含页面外壳，数据接口才是权限边界。
- 订单来自 orders 表，不再为每位用户生成共享 1001/2002。物流/保修/售后查询在调用 MCP 前验证订单或运单归属。
- 授权拒绝后由确定性逻辑结束本轮；写操作结果未知时直接提示查询原操作状态。模型不能将这些状态改写为成功，SSE 同样输出受控结果。
- 所有 Agent 工具（含退款表单建议）经统一引擎。未知身份默认拒绝，客户端/模型不能提交任何注入身份、审批或运行模式字段。
- plan 禁止写操作；default/auto 对写操作均要求显式审批；bypass 默认关闭，启用也不绕过资源、hard deny 和业务审批。未接入可选 AI 分类器。
- 工单审批绑定主体、会话、run/tool_call、工具身份、参数摘要、策略版本和过期时间。confirmed 布尔值不能授权写操作。
- 业务事务内再次校验归属、退款资格、核销审批、创建工单和幂等记录。退款表对订单唯一，普通工单对 operation_id 唯一。
- 同一 HTTP 审批的重试返回原工单；工具引擎对已核销审批拒绝重放。写工具超时不自动重试。
- SecurityDecision 保存可关联的主体、运行、操作、策略和参数摘要。执行前决策审计失败时关闭执行；业务提交审计与工单同事务。旧工具结果审计只存摘要哈希和状态，不保存凭证或业务原文。
- MCP 仅接受本地已知 server/tool、固定端点、已审核 Schema 指纹；远程要求 HTTPS 和独立服务令牌，本地 HTTP Mock 必须显式开启。禁止重定向、环境代理和目标变化。远端工具描述不作为授权依据，结果标为不可信数据。

## 部署顺序

本次没有自动修改 .env、生产数据库或已有会话归属。请先在测试环境执行迁移并配置新密钥，再发布应用。

1. 备份数据库和当前版本。项目目前继承了上层目录的 Git，建议正式发布前纳入独立仓库；本次源码修改前快照在 .security-upgrade-work/before，未包含 .env。
2. 使用现有 WSL Python 环境同步依赖：

   ```bash
   cd '/mnt/c/Users/Lenovo/Desktop/MEWHELP python'
   uv sync --locked --group dev
   ```

3. 在目标数据库执行已有基础迁移（ch02/ch03/ch04/ch07/ch08/ch09/ch10），确保已执行 sql/ch06-ticket-type.sql，再执行新增 sql/ch11-security-ddl.sql。新增迁移只加表，不会把旧字符串自动转为登录身份。不要重复执行不具备幂等性的建表语句。
4. 在密钥管理系统或私有运行环境配置 AUTH_SIGNING_KEY（随机值，至少 32 字节，多个 worker 共用），不要提交密钥。生产使用 HTTPS、AUTH_COOKIE_SECURE=true；仅本地 HTTP 开发使用 false。反向代理需正确配置可信代理和公开 Origin。
5. 配置 AUTH_ALLOWED_ORIGINS 为精确来源数组，例如 ["https://support.example.com"]。公共注册默认保持 AUTH_REGISTRATION_ENABLED=false。
6. 创建账户；密码在终端交互输入，不放命令行参数：

   ```bash
   .venv/bin/python -m scripts.manage_users admin --admin
   .venv/bin/python -m scripts.manage_users customer
   ```

7. 如需迁移旧会话，人工确认 legacy_user_id → 新 user_id 的 JSON 对照表，先预览，再在维护窗口执行：

   ```bash
   .venv/bin/python -m scripts.migrate_legacy_owners reviewed-owner-map.json
   .venv/bin/python -m scripts.migrate_legacy_owners reviewed-owner-map.json --apply
   ```

   不允许重新分配已经存在的认证账户。迁移后的旧待确认 Checkpoint 不直接恢复，需要开始新的认证轮次；旧审批不继承权限。

8. 从可信订单平台导出并审核订单 JSON，先使用 scripts.import_orders 校验预览，确认后 --apply。导入拒绝更改已有订单所有者；退款必须同时具备支持的状态、refundable=true 和尚未到期的 UTC refund_deadline。没有可信订单数据时查询为空、退款拒绝，不回退到 Mock 所有权。
9. 配置 MCP：生产提供独立 HTTPS 地址和 MCP_LOGISTICS_TOKEN/MCP_AFTERSALES_TOKEN；本地 Mock 仅可设置 MCP_ALLOW_LOCAL_HTTP=true。运行 scripts.inspect_mcp_contracts 查看候选 Schema，人工核对后将指纹配置到 MCP_SCHEMA_PINS。该脚本本身不会批准能力。端点或 Schema 变化需重新审核；未配置指纹时工具不对模型开放。
10. 发布前端和后端，访问 /login。验证登录 → Agent/SSE → 审批 → 续期 → 登出闭环。

## 接口迁移

| 场景 | 新契约 |
| --- | --- |
| 注册 | POST /api/auth/register；默认关闭 |
| 登录 | POST /api/auth/login，JSON username/password；返回 access_token，响应设置刷新 Cookie |
| 身份 | GET /api/auth/me，Authorization: Bearer |
| 刷新/退出 | POST /api/auth/refresh、logout，刷新 Cookie + 合法 Origin |
| Agent/聊天 | JSON message/conversation_id，禁止 user_id 字段 |
| 会话列表 | GET /api/conversations，不再传用户 ID |
| HTTP 工单预览 | POST /api/actions/prepare，conversation_id/tool_name/arguments |
| HTTP 工单提交 | 原 create-ticket/create-refund 字段 + 预览返回的 approval_id |
| Agent 工单恢复 | conversation_id/confirmed/approval_id；订单选择仍传 order_id，两者互斥 |
| 超时状态查询 | GET /api/actions/operations/{approval_id}，仅本人可查 |

客户端先显示服务端返回的准确预览，再提交绑定的 approval_id。过期或参数变化时需重新生成预览。退款接口的成功含义是“退款申请工单已提交”，不会操作支付账户。

操作超时后，先查询原 approval_id；completed 返回已提交的 ticket_no。pending_or_not_executed 表示尚无已提交记录，不能据此假定外部副作用不存在或创建新的操作重试。当前写入仅涉及本地 MySQL，事务内部分失败会回滚。

scripts.eval_agent / eval_ch06 / eval_ch08 使用 COMMERCEFLOW_ACCESS_TOKEN，不再信任脚本固定 user_id。端到端样例中的订单号也必须属于该已登录测试账户。裸循环 bare_agent_loop 没有登录主体，因此工具执行会被拒绝；可用 HTTP Agent 接口做完整演示。

## 测试与运行维护

```bash
.venv/bin/python -m pytest tests/security -q
.venv/bin/python -m pytest tests/db/test_security_transactions.py -q
.venv/bin/python -m pytest -q
```

```powershell
node --test tests/frontend/auth.test.cjs
node --check app/static/auth.js
```

security/ 使用独立临时 SQLite，验证真实表、事务、认证路由和 LangGraph 中断，不需要 LLM/Milvus。MySQL 并发测试和原数据库测试使用 TEST_DATABASE_URL；保护逻辑要求数据库名以 _test 结尾，并且地址与正式库不同。Milvus 回归需要测试服务就绪。

使用认证部署后的稳定版本回滚；没有此前已验证的认证版本时，应进入维护状态修复，不能关闭认证或重新信任客户端 user_id。新增表不要随代码回滚自动删除。监控登录限流、401/403/404、刷新重放、审批拒绝、执行结果未知和审计写入失败。按组织保留策略定期清理旧审计及过期会话；Refresh Token 的已使用记录必须保留到整个会话链过期，才能继续检测重放。

## 验收记录

本轮实际执行结果（分组存在重叠，不累加为测试总数）：

| 验证范围 | 实际结果 |
| --- | --- |
| 全项目可独立运行回归 | 427 passed，103 项依赖服务测试显式排除 |
| 最终认证、资源授权、审批与工具安全回归 | 90 passed |
| Agent / SSE / 图编排 / 工具及真实 MCP 针对性回归 | 阶段验收 245 passed，包含本地 MCP 真实进程与受限 HTTP 客户端 |
| MySQL 迁移、Repository、工具审计及安全事务 | 11 passed，使用隔离的原生 MySQL 9.4 实例 |
| MySQL 并发限流及安全事务补充复验 | 1 passed；包含并发限流、刷新重放撤销、工单幂等和不同会话退款去重 |
| 浏览器端认证、续期、退出、确认与内容转义 | Node 8 passed |
| 页面内联 JavaScript | 13 段语法检查通过 |
| 依赖锁与 Python 源码 | uv lock --check --offline 和 compileall 通过 |

MySQL 专项使用新的临时数据目录、端口及测试账户，未对现有数据库服务应用迁移。限流计数的过期清理放在独立后台任务中；MySQL 清理使用 READ COMMITTED，避免与计数原子更新形成范围锁竞争。

最初全量 pytest 在 MySQL/Milvus 未就绪时，以 248 passed、25 个服务连接错误停止。随后通过独立环境完成了上述核心 MySQL 与 MCP 补验；仍未将其宣称为全部外部依赖集成通过。其余数据库回归与 Milvus 测试需在目标测试服务就绪后继续执行完整 pytest。MySQL 8.x 部署应在对应版本的预发布环境再跑迁移与事务测试。

本次未验收第三方支付退款、真实订单平台接入或生产 HTTPS/MCP 部署。源码、DDL 和部署工具已交付，生产密钥、账户映射与数据接入仍须按本说明执行。

本地原始日志保存在 .security-upgrade-work/，包括 final-regression-test.log、security-complete.log、mysql-test.log、mysql-final-test.log；该目录不入库。

## 主要代码入口

| 范围 | 文件 |
| --- | --- |
| 登录和 HTTP 认证 | [auth API](../app/api/auth.py)、[依赖](../app/api/deps.py)、[账户服务](../app/services/auth_service.py) |
| JWT、密码和主体 | [tokens](../app/security/tokens.py)、[passwords](../app/security/passwords.py)、[principal](../app/security/principal.py) |
| 工具策略和精确审批 | [gate](../app/security/gate.py)、[policy](../app/security/policy.py)、[approvals](../app/security/approvals.py) |
| 工具执行和 MCP | [engine](../app/tools/engine.py)、[registry](../app/tools/registry.py)、[mcp_client](../app/tools/mcp_client.py) |
| 业务事务 | [tickets](../app/services/tickets.py)、[refunds](../app/services/refunds.py) |
| 归属和数据迁移 | [repository](../app/db/repository.py)、[模型](../app/db/models.py)、[新增 DDL](../sql/ch11-security-ddl.sql) |
| 会话和中断恢复 | [runtime](../app/graph/runtime.py)、[nodes](../app/graph/nodes.py) |
| 浏览器登录 | [auth.js](../app/static/auth.js)、[login](../app/static/login.html) |
| 运维入口 | [账户创建](../scripts/manage_users.py)、[历史归属映射](../scripts/migrate_legacy_owners.py)、[订单导入](../scripts/import_orders.py)、[MCP 指纹查看](../scripts/inspect_mcp_contracts.py) |
| 关键测试 | tests/security/、tests/db/test_security_transactions.py、tests/frontend/auth.test.cjs |
