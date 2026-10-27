# CommerceFlow 统一身份认证与资源级授权升级方案

> 状态：设计基线（下文现状指改造前）。对应实现、测试结论和部署步骤见[实施说明](CommerceFlow_安全升级实施与部署说明.md)。  
> 项目：`C:\Users\Lenovo\Desktop\MEWHELP python`  
> 建议存放：`docs/CommerceFlow_身份认证与资源级授权升级方案.md`  
> 核心顺序：**先实现完整登录服务（账户、登录、Token 生命周期），再接入 FastAPI 统一认证依赖，最后补齐会话、订单、退款的资源级授权**。  
> 原则：最小侵入、后端可信身份、默认拒绝、先校验再产生副作用、保留现有 LangGraph / Tool Engine 架构。

## 一、目标与边界

### 1.1 要解决的问题

- 完成本地账户与登录服务：用户注册/创建、密码安全存储、登录、获取身份、Token 续期、登出与撤销。
- 客户端不应通过请求体或查询参数任意指定 `user_id` 来访问他人数据。
- 会话的读取、继续对话、`interrupt` 恢复、工单创建均应绑定当前认证用户。
- 查询订单时必须验证订单属于当前用户；只读工具同样需要资源级授权。
- 提交退款申请时，必须验证会话和订单归属，以及退款条件；不能仅凭前端提交的 `conversation_id`、`order_id` 创建工单。
- 继续保留 `InjectedToolArg`，使 LLM 不能自行填写受信任身份参数，并确保 Tool Engine 实际注入的是认证用户 ID。
- 增加拒绝访问、越权、重复提交、并发等自动化测试与必要审计。

### 1.2 本阶段不做的事情

- 不重构整套 LangGraph 或 Function Calling 调度。
- 不直接对接第三方支付并执行真实资金退款。当前 `create-refund` 的业务效果是创建“退款”类型的工单，不能当作资金已经退回。
- 不把 Mock 订单数据误当作生产订单授权：如暂未上线真实订单表，可先完成接口认证与会话授权，再独立实施订单数据迁移。
- 不把 `InjectedToolArg`、`confirmed=True` 或 Prompt 当作登录认证与最终授权。

## 二、已核实的现状与风险

以下根据本项目现有源码核对；升级动作仅为建议，尚未实施。

1. `app/api/agent.py`：`run_agent()` 将 `req.user_id` 直接传入 `runtime.run_turn(req.user_id, ...)`。
2. `app/api/chat.py`：`chat()` 的 SSE 处理逻辑也使用 `req.user_id`。
3. `app/api/conversations.py`：`GET /api/conversations` 从查询参数接收 `user_id`；`GET /api/conversations/{id}/messages` 只检查会话存在，没有核验所有者。
4. `app/graph/runtime.py`：`_ensure_conversation(user_id, conversation_id)` 通过 `repository.get_conversation(conversation_id)` 检查存在性，尚未比较 `conv.user_id`；`resume_turn(conversation_id, ...)` 也只检查存在性。需要同时处理 `stream_turn` 与 `stream_resume` 这两条路径。
5. `app/api/actions.py`：`POST /api/actions/create-ticket` 直接调用 `repository.create_ticket(conversation_id, ...)`；`POST /api/actions/create-refund` 直接以提交的 `conversation_id`、`order_id` 拼接描述并创建退款工单；`POST /api/actions/resume` 仅使用 `conversation_id` 恢复中断。
6. `app/db/models.py`：`Conversation` 已有 `user_id` 字段；`Ticket` 通过 `conversation_id` 关联会话。当前检查的 ORM 定义中没有生产级订单所有权表/退款记录模型。
7. `app/tools/builtin/orders.py`：`query_order(order_id, user_id: InjectedToolArg)` 检查 `owns_order()`；`app/tools/engine.py` 根据 `ToolSpec.inject_user_id` 在执行前注入 `user_id`。该机制阻止模型正常填写身份参数，但不保证进入 Runtime 的身份原本就是可信的。
8. `app/tools/business.py`：`order_snapshot()`、`list_user_orders()`、`owns_order()` 仍基于稳定 Mock 数据；`DEMO_ORDER_IDS = ("1001", "2002")` 会出现在模拟用户列表中，不能用于真实用户间的所有权隔离验收。
9. `app/graph/nodes.py`：`submit_refund` 分支当前产生 `refund_form` 前端操作建议；创建工单由 `interrupt()` 与 `confirmed` 进行确认，但恢复接口本身也必须先验证会话归属。
10. `app/tools/registry.py`：`WRITE_TOOLS = {"create_ticket"}`；未知工具默认 `read`。本方案优先处理身份和资源授权，工具权限名单加固列为紧邻的安全收尾项。

**最高优先级漏洞链：** 伪造 `user_id` / 枚举 `conversation_id` → 读取或恢复他人的会话；伪造 `conversation_id`、`order_id` → 产生未经授权的退款工单。应先在边界拦截，再在 Repository/Service 层做防御性复核。

## 三、目标架构

```text
用户注册/账户创建 → 登录验证密码 → 签发短期 Access Token
                     ↓
客户端持有 Access Token，并使用受保护的 Refresh Token 续期
                  ↓
FastAPI 统一认证依赖 get_current_user()
  - 验证签名/会话、过期、issuer、audience、账户状态
  - 返回可信 CurrentUser(user_id, roles, ...)
                  ↓
API 对资源参数做授权（conversation_id / order_id）
                  ↓
Runtime（可信 user_id 写入 LangGraph state）
                  ↓
Tool Engine（InjectedToolArg 隐藏 Schema，执行前注入身份）
                  ↓
Repository / Service：订单、退款、工单的资源级授权及业务条件
                  ↓
MySQL 事务 / MCP 服务 + 审计
```

权限边界必须区分：**Authentication**（你是谁）、**Resource Authorization**（你的会话/订单是什么）、**Tool Permission**（某个工具是否需要审批）。三个检查不能互相替代。

## 四、阶段一：新增登录服务 + FastAPI 统一认证（先完成）

本次升级明确**实施一个最小但完整的本地登录服务**，而不再仅仅“验证别人签发的 JWT”。现有项目检查范围内尚未发现成熟的用户注册/登录体系，因此以下作为新增代码的施工设计；如部署环境已有可信 OIDC，可复用其身份源，但此计划默认走本地账户方案。

### 4.1 P1.1 — 账户数据模型及迁移

新建 `users` 数据表及 SQLAlchemy `User` 模型，保持 `user_id: str` 与现有 `Conversation.user_id: String(64)` 兼容，避免破坏 Agent Runtime 的接口。建议字段：

- `user_id`：服务端生成、不可猜测且不可由注册请求指定的稳定主键（字符串，最多 64 字符）。
- `username`：唯一、规范化后的登录标识（也可按业务改为已验证邮箱）。
- `password_hash`：仅保存 Argon2id 密码哈希，绝不存明文密码、可逆加密密码或普通 SHA256。
- `is_active`、`created_at`、`updated_at`；如需管理员角色，使用明确的服务端授权字段，注册请求不能自设 `role=admin`。

新建 `auth_sessions`（或 `refresh_tokens`）表：`id`、`user_id`、`refresh_token_hash`、`family_id`、`expires_at`、`revoked_at`、`created_at` 等，建议将 Token 哈希后入库并建立唯一索引。刷新时原子轮换并撤销旧记录；若检测到已使用过的旧 Refresh Token 重放，应吊销该 Token Family。Access Token 应采用短有效期与必要的账号状态校验；是否支持即时踢下线，可另配 `token_version` / `jti` 撤销机制。

文件：修改 `app/db/models.py`、`app/db/repository.py`；新增 `sql/ch11-auth-ddl.sql`（名称建议，需与项目 DDL 和迁移规范保持一致），更新 `tests/conftest.py` 中 DDL 清单及表清理顺序；历史数据或已有会话的 `user_id` 要制定可信的账户映射/迁移方案，不能自动把任意旧字符串视为已认证账户。

### 4.2 P1.2 — 注册、登录、刷新与登出 API

建议新增 `app/api/auth.py`、`app/schemas/auth.py`、`app/services/auth_service.py`、`app/security/passwords.py`、`app/security/tokens.py`。路由注册到现有 FastAPI 应用中；以下均为**待实现接口**：

- `POST /api/auth/register`：创建账户；先明确注册策略（默认关闭公开注册，采用管理员创建或邀请码；如决定开放，增加注册频控和反滥用），校验用户名唯一、密码策略，生成 `user_id` 和 Argon2id 哈希。返回最少的公开用户信息，不能返回密码哈希。
- `POST /api/auth/login`：验证用户名/密码与用户启用状态；成功签发短期 Access Token 和可轮换 Refresh Token。登录失败使用统一错误文案，配合 IP + 用户维度限流、节流与监控，避免账号枚举和撞库。
- `POST /api/auth/refresh`：校验 Refresh Token 的真实性、到期时间、撤销状态与账户状态，在数据库事务内完成一次性轮换，签发新 Token。重复使用旧 Token 必须拒绝并按策略撤销其会话链。
- `POST /api/auth/logout`：撤销当前 Refresh Token / 会话，清除客户端凭证。若要求 Access Token 立即失效，明确实现短效 + 撤销表或版本号检查，不能宣称仅清除客户端 Token 就能立即失效。
- `GET /api/auth/me`：通过 `Depends(get_current_user)` 返回最少用户资料，验证当前登录状态，不暴露哈希或机密字段。

建议浏览器端**Access Token 仅保存在内存中**，Refresh Token 使用 `HttpOnly; Secure; SameSite` Cookie，按 HTTPS、同站/跨站部署情况设定 Cookie 策略；涉及 Cookie 的 `refresh/logout` 必须加 CSRF / Origin 防护。也可采用纯 Bearer 方案，但应有专门的安全存储策略，避免把长期 Token 放在 `localStorage` 中。开发环境可单独配置非 HTTPS 模式，禁止复制到生产。

JWT 必须由服务端安全签发并固定验签算法，验证 `sub`、`exp`、`iat`、`iss`、`aud`、`token_type=access`（以及 `jti` 等），拒绝签名错误、过期、类型错误 Token。密钥通过运行环境/密钥管理系统配置，不能提交仓库。Refresh Token 推荐使用加密安全随机字符串并仅保存哈希，不能把 Access Token 当 Refresh Token。

### 4.3 P1.3 — FastAPI 统一身份认证依赖

- 新增 `app/api/deps.py`：`get_current_user()` 解析并验证 Access Token，按 `sub` 查询活动用户，返回 `CurrentUser(user_id, roles, ...)`；统一未认证 401、无权限 403 规则。
- 新增/扩展 `app/security/auth.py`：整合 Token 解码、用户状态与通用安全异常，供 REST、SSE 与工具调用入口复用。
- 修改 `app/config.py`、`.env.example`：令牌签发方/受众/固定算法/有效期、密码哈希参数、注册开关、认证频控等；密码哈希库与 JWT 库版本锁入 `pyproject.toml` / `uv.lock`。仅在部署环境注入密钥，绝不在文档中写真实密钥。
- 修改 `app/schemas/agent.py`、`app/schemas/chat.py`：移除请求体对 `user_id` 的依赖，过渡期需强制校验一致性并设置清晰的弃用计划；生产不允许匿名回退到客户端提供的 ID。
- 修改 `app/api/agent.py`、`app/api/chat.py`、`app/api/conversations.py`、`app/api/actions.py`：使用 `Depends(get_current_user)`，统一把 `current_user.user_id` 传入 Runtime/Service；同步排查管理 API 及其他持有 `conversation_id` 的路由。
- 对 SSE：路由执行时先完成认证，**在创建 `StreamingResponse` 之前**阻止未认证；运行时的会话归属、数据库错误需要使用已有受控错误通道处理，不能泄漏内部细节。

### 4.4 建议的核心调用链与接口形态（伪代码）

```text
POST /api/auth/login  [用户名 + 密码]
    → AuthService 验证 Argon2id 哈希及账户状态
    → 创建 auth_session，签发 Access Token + Refresh Token
    → 前端携带 Access Token 调用 /api/chat /api/agent
    → FastAPI Depends(get_current_user) 校验 Token + 查询用户
    → runtime.run_turn(current_user.user_id, ...)
    → Tool Engine 将可信 user_id 注入 InjectedToolArg
    → Repository 做会话、订单、退款资源级授权
```

```python
# app/api/deps.py（示意；不含具体库依赖）
async def get_current_user(credentials=Depends(bearer)) -> CurrentUser:
    claims = verify_access_token(credentials.credentials)  # 签名、iss/aud/exp、类型等
    user = await load_active_user(claims["sub"])
    if user is None:
        raise HTTPException(status_code=401, detail="身份验证失败")
    return CurrentUser(user_id=user.user_id, roles=load_roles(user))

# app/api/agent.py（示意；保留原有响应/异常分支）
@router.post("/api/agent")
async def run_agent(req: AgentRequest, user: CurrentUser = Depends(get_current_user)):
    out = await runtime.run_turn(user.user_id, req.message, req.conversation_id)
    # 继续处理原本的 AgentResponse
```

### 4.5 登录服务与认证阶段验收

- 注册：用户名规范化去重、弱密码拒绝、重复注册冲突；注册请求无法指定 `user_id` 或管理员权限；不开放公共注册时能正确拒绝匿名注册。
- 登录：正确凭证成功，错误密码/未知用户统一失败，禁用账户不能登录；数据库里只出现 Argon2id 哈希，不出现明文密码。
- Token：Access Token 过期、签名错误、issuer/audience 错误、错误类型、用户禁用都不能访问；Refresh Token 轮换成功，旧 Token 再用必失败；登出后不能再次刷新。
- 防滥用：暴力登录限流，认证错误不泄露账户是否存在，日志不记录密码、Access Token 或 Refresh Token；Cookie 的 CSRF / Origin 防护经过验证。
- 接口衔接：`GET /api/auth/me` 返回当前用户；`/api/agent`、`/api/chat` 和相关会话/动作接口均从依赖取得身份。客户端伪造请求体/查询参数 `user_id` 无法更改身份。
- 测试：使用 FastAPI `dependency_overrides`、独立测试数据库和模拟时钟覆盖登录、续期、登出及 401/403；集成测试验证真实用户表和 Refresh Token 原子轮换。

**阶段 P1 完成标准**：先完成“创建账户 → 登录 → 获取 Token → 访问受保护的 Agent 接口 → 刷新 → 登出”的闭环，然后才继续改造会话、订单和退款的授权。不能仅有 `get_current_user` 而没有可用的登录签发端。

## 五、阶段二：会话资源级授权

### 5.1 Repository 绑定用户身份

利用现有 `Conversation.user_id`，优先新增而非破坏原接口：

```python
# app/db/repository.py (建议)
async def get_conversation_for_user(conversation_id: int, user_id: str):
    async with db.async_session() as s:
        result = await s.execute(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.user_id == user_id,
            )
        )
        return result.scalar_one_or_none()
```

不得以 `get_conversation(id)` 仅证明存在就放行。会话读取消息列表、追加消息、保存摘要以及工单创建应沿可信所有者使用同样的授权约束；对并发敏感操作需在事务内复核。

### 5.2 Runtime 各路径一并修复

- `_ensure_conversation(user_id, conversation_id)`：继续旧会话时调用 `get_conversation_for_user()`；在 `append_message()`、加载 Checkpoint 或读取摘要**之前**拒绝越权。
- `resume_turn()` / `stream_resume()`：调整为需要可信 `user_id`，先验证所有权，再根据 `thread_id=str(conversation_id)` 读取/恢复 Checkpoint。**仅有 `conversation_id` 不足以授权**。
- `GET /api/conversations`：不再使用查询参数中的任意 `user_id`，从认证上下文取身份。
- `GET /api/conversations/{conversation_id}/messages`：先验证拥有权，再读取消息。
- `POST /api/actions/create-ticket`：创建工单前验证会话归属；在业务层再次检查，避免从其它入口绕过。
- 检查 `feedback.py`、`review.py` 等使用会话或含用户信息的路径，按“用户资源接口 / 管理接口 / 可匿名公共接口”分类做统一保护，不应遗漏 SSE 与管理路由。

对外可统一返回 404，避免向无权用户透露会话是否存在；内部审计分别记录 `not_found` 与 `not_owned`，但不得泄露敏感内容。

### 5.3 阶段验收

- 用户 A 创建会话 A，用户 B 不能读取、继续对话、追加消息、查看摘要、恢复 A 的中断或以 A 的会话创建工单。
- 尝试恢复他人会话时，不能先恢复 LangGraph 再校验；数据库状态和工单表均不得发生变化。
- 新建会话始终与认证用户绑定；已有会话归属不因客户端传参变化而改变。

## 六、阶段三：真实订单归属校验

### 6.1 先补齐事实上的数据模型

当前 `app/tools/business.py` 的订单是 Mock 数据；演示订单 `1001`、`2002` 会被加入模拟用户列表。要完成真正的订单访问控制，必须接入可信订单服务或新增真实订单表，例如：

- `orders.order_id`（唯一订单号）
- `orders.user_id`（拥有者，和统一身份体系一致）
- `orders.status`、`orders.tracking_no`、`orders.amount` 等所需业务字段
- 为 `(user_id, order_id)` 设置合理索引/唯一约束；由可信下单流程写入拥有者身份。

具体 DDL 应与现有 `sql/ch02-ddl.sql` 及 MySQL 迁移规范匹配；迁移需支持已有数据回填、数据质量核验和回滚。若使用外部订单平台，则通过受信任 API 完成授权，不能在本地凭模拟关系推断归属。

### 6.2 推荐查询方式

```python
# app/db/repository.py 或 order_repository.py（建议）
async def get_order_for_user(order_id: str, user_id: str):
    async with db.async_session() as s:
        result = await s.execute(
            select(Order).where(
                Order.order_id == order_id,
                Order.user_id == user_id,
            )
        )
        return result.scalar_one_or_none()
```

`query_order` 保留现有 `Annotated[str, InjectedToolArg]` 标注；后端从认证身份注入 `user_id`。替换 `owns_order()` 的 Mock 实现为数据库/订单服务资源级授权；仅返回业务需要的最小字段。不要把 Mock 订单号 1001 的成功响应当作生产权限测试通过。

### 6.3 工具层防护

- 模型可见 Schema 不含 `user_id`；执行器在 Schema 验证后覆盖注入可信 `user_id`。
- 禁止模型参数中额外混入 `user_id`/`conversation_id` 影响执行；对注入参数进行严格白名单与覆盖测试。
- 订单不存在和无权访问可使用统一外部错误文案，不泄露订单归属。
- `query_logistics` 使用从已授权订单获取的运单号；不能以不受信任的运单号绕开订单校验读取他人物流。MCP Server 必要时也须验证调用方授权。

### 6.4 阶段验收

- 测试数据库中，1001 归用户 A；A 可以查到，B 不可查到；不存在的 9999 也不能泄露是否存在。
- 模型伪造 `user_id`、Prompt Injection、直接调用 Tool Engine 都不能绕过用户归属。
- 授权错误不会被 LLM 改写为“查询成功”。

## 七、阶段四：退款申请的资源与业务授权

### 7.1 必须先厘清当前业务性质

当前 `app/graph/nodes.py` 的 `submit_refund` 用来产生 `refund_form`，不是实际支付退款；`app/api/actions.py` 的 `POST /api/actions/create-refund` 会把订单号与原因拼成描述，调用 `repository.create_ticket(..., "退款")`，生成一个**退款工单**，并非真实资金原路退回。

### 7.2 改造顺序

1. `/api/actions/create-refund` 使用 `Depends(get_current_user)`，禁止从请求中自选身份。
2. 确认 `conversation_id` 属于当前用户，且与本次退款申请业务上下文一致。
3. 确认 `order_id` 存在且属于当前用户；服务端自行读取订单状态、金额等，不能信任模型或前端提交的这些字段。
4. 检查退款申请资格（订单状态、退款窗口、重复申请、售后状态、退款上限等）；具体规则由业务明确配置，不能凭空假定所有订单可退。
5. 在事务中创建退款工单/退款申请记录，并控制重复提交。建议增加 `(user_id, order_id, request_kind, idempotency_key)` 等唯一约束或原子去重策略；工单创建与相关状态更新保证一致性。
6. 保留已有前端表单/确认流程，但**表单确认不是授权**；在真正提交接口重新检查所有权和条件。未来接入支付退款时另行增加支付后端二次授权、外部幂等键、审计和回查。
7. 对 `create-ticket` 也应用会话归属和重复创建保护；对 `resume` 的 `confirmed` 必须绑定被授权的会话及对应中断，不能用任意会话 ID 重放确认。

### 7.3 推荐服务入口（接口草图）

```python
# app/services/refunds.py（建议）
async def submit_refund_request(
    *, user_id: str, conversation_id: int,
    order_id: str, reason: str, idempotency_key: str,
) -> str:
    # 1. 在事务内查询本用户会话与本用户订单
    # 2. 核验申请资格及状态
    # 3. 幂等检查/创建记录
    # 4. 创建退款工单并提交
    # 5. 返回工单号；不返回“已退款成功”
    ...
```

### 7.4 阶段验收

- 用户 B 使用用户 A 的订单和会话申请退款：拒绝，数据库无新增工单。
- 用户 A 的会话配用户 B 的订单：拒绝。
- 同一请求重复提交：返回既有记录或明确的冲突状态，不创建重复退款工单。
- 同一订单不符合退款政策：拒绝，并返回受控的业务错误。
- Agent 提议 `submit_refund` 或前端生成 `refund_form` 不能绕过真正的 `/api/actions/create-refund` 授权检查。

## 八、需要同步处理的测试与基础设施

建议新增：

- `tests/test_auth_api.py`：注册（按注册策略）、登录、`/me`、Token 刷新轮换、登出及禁用账号。
- `tests/test_auth_service.py`：Argon2id 哈希验证、错误密码、Token 类型、过期、撤销、重放检测及并发刷新。
- `tests/test_auth_dependencies.py`：401/403、过期/篡改 JWT、禁用用户、依赖注入与错误响应。
- `tests/test_conversation_authorization.py`：列表隔离、消息读取、run_turn / stream_turn、resume_turn / stream_resume、其他用户 Checkpoint 禁止访问。
- `tests/test_order_authorization.py`：订单归属、不存在订单、注入用户参数、恶意多余字段、MCP 物流前置授权。
- `tests/test_refund_authorization.py`：会话+订单联合归属、不可退款状态、重复申请、事务失败回滚、无越权写入。
- 扩展现有 `tests/test_agent_api.py`、`tests/test_chat_api.py`、`tests/test_conversations_api.py`、`tests/test_actions_api.py`、`tests/test_refund_api.py`、`tests/test_repository.py`、`tests/tools/test_order_ownership.py`、`tests/graph/test_agent_nodes.py`。

项目的 `tests/conftest.py` 会使用单独测试数据库执行 `sql/ch02-ddl.sql` 等 DDL；如新建 users/orders/refund_requests 表，需同步测试数据库迁移/清理顺序，并防止测试影响正式库。单元测试可用 `app.dependency_overrides[get_current_user]` 建立 A、B 两个不同身份；集成测试验证真实 Repository 和数据库事务。

**必须覆盖的安全测试矩阵：** 账号枚举/暴力登录、密码哈希、Refresh Token 重放、登出撤销、未登录、伪造身份、过期 Token、会话枚举、跨用户读取历史、跨用户恢复中断、跨用户查单、跨用户申请退款、同一退款重复提交、部分写入后失败、未知 MCP 工具、日志敏感信息泄漏。所有拒绝场景都需断言数据库**未发生副作用**。

建议运行（按实际环境配置）：

```powershell
cd "C:\Users\Lenovo\Desktop\MEWHELP python"
.\.venv\Scripts\python.exe -m pytest tests/test_agent_api.py tests/test_chat_api.py tests/test_conversations_api.py tests/test_actions_api.py tests/test_refund_api.py -v
.\.venv\Scripts\python.exe -m pytest tests/test_repository.py tests/tools/test_order_ownership.py -v
```

之后再运行全部测试；`make eval-agent` 属于模型/业务服务的端到端评测，需相关服务就绪，不能取代权限测试。

## 九、实施次序与交付验收

- **P0 — 接口与认证方案审计（半天）**：梳理所有读写会话、订单、退款及管理接口；确认公开注册策略、账号数据迁移与 Token/Cookie 方案。
- **P1.1 — 账户数据及登录服务（2–3 天）**：`users` / `auth_sessions` 模型和 DDL、注册/创建账户、Argon2id、登录、Access + Refresh Token、刷新轮换、登出、`/me`、限流及单元测试。
- **P1.2 — 全站统一认证接入（1 天）**：`get_current_user()`、账号状态校验、API 替换 `req.user_id`、SSE 前置 401、测试无凭证/伪造身份；不得存在匿名回退。
- **P2 — 会话授权（1 天）**：Repository 所有者过滤、Runtime 旧会话/恢复权限、历史消息与工单创建授权；测试跨用户访问。
- **P3 — 订单授权（1–2 天）**：选择真实订单来源、必要数据迁移、替换 Mock 所有权判断、Tool Engine 注入参数测试、物流访问约束。
- **P4 — 退款授权（1–2 天）**：退款工单提交前检查会话+订单、业务资格、事务与幂等；测试越权、重复和失败回滚。
- **P5 — 安全收尾（半天）**：审计脱敏、速率限制、未知工具默认拒绝、回归测试与上线检查。

时间为粗略开发估算，取决于是否已有身份提供方和真实订单数据。建议每阶段独立提交并有完整回归测试，不要一次性重构所有模块。

### 最终 Definition of Done

- [ ] 用户账户、Argon2id 密码哈希、登录、Token 续期与登出闭环完成，注册策略明确且受控。
- [ ] Refresh Token 使用安全存储、轮换及撤销机制；登录限流、Token 错误和重放防护测试通过。
- [ ] 每个面向用户且涉及敏感数据的 FastAPI 入口从可信认证上下文获得 `user_id`。
- [ ] 请求体与查询参数中的 `user_id` 无法伪造账户身份。
- [ ] 所有会话读取、写入、继续、恢复和工单创建均验证所有者。
- [ ] 所有订单查询与关联物流访问均验证订单归属。
- [ ] 退款申请先验证会话、订单、退款资格与幂等，失败无写入副作用。
- [ ] Agent `InjectedToolArg`/Tool Engine 不接受模型伪造身份，且不会丢失受信任用户上下文。
- [ ] 跨用户与未认证安全测试通过，原有 Agent/聊天功能未回归失败。
- [ ] 审计有身份、动作、资源和拒绝原因的必要元信息，但不泄露 Token、密码或过量个人数据。

## 十、发布与回滚注意事项

- 开始前备份测试/正式数据库，先在测试环境运行并核对 `users`、`auth_sessions` 等迁移，确认已有会话的账户归属映射；生产启用认证时同步上线登录/Token 传递/刷新/登出前端功能，并明确兼容窗口。
- 可分阶段上线代码，但**不允许**为兼容遗留客户端在生产环境默默退回“信任客户端 user_id”；无法认证应失败关闭（fail closed）。
- 采用可回滚、尽量向后兼容的数据库迁移（先加表和列，再切流量，最后清理旧逻辑），避免直接删除历史数据。
- 发布后重点监控注册/登录失败与限流、Token 刷新/撤销异常、401/403/404、SSE 恢复失败、工单生成率、跨用户拒绝次数及数据库事务错误，审计需脱敏。
- 若发生事故，回滚到上一个**仍启用认证与资源授权**的稳定版本，而不是关闭权限门禁。

---

**核心结论：** 本次先补齐可实际使用的本地登录服务与可信身份认证闭环，再消除“客户端可决定身份”和“会话只校验存在而非归属”的风险；随后完成会话、真实订单和退款工单的资源级授权，才能让 `InjectedToolArg` 与 Tool Engine 的权限机制真正发挥作用。

## 实施进度（2026-10-21）

本文件保留为设计基线。对应认证、资源授权、审批和安全网关代码已加入项目；实际验收结果及部署必需步骤见 [安全升级实施与部署说明](CommerceFlow_安全升级实施与部署说明.md)。生产配置与数据库迁移不因源码更新而自动完成。
