# RAG v2 实施与验证

默认 `KB_READ_MODEL=legacy`，现有 MySQL 表和 `knowledge` Collection 保留。新流程使用五张独立表，以及 `knowledge_v2_test` Collection，不执行原表 ALTER、旧索引 drop 或自动生产切换。

## 已实现

- `app/kb/domain.py`：Document、Chunk、统一 Search Record、旧数据适配和引用适配。原 `documents.Chunk` 接口继续可用。
- `app/db/kb_v2_*.py`：不可变文档版本、稳定 Chunk 映射、同文档版本内的邻居、按 Collection 保存索引状态和生效版本。
- `dualwrite.write_document/vectorize_documents`：事务暂存 → Embedding → Milvus upsert → flush → done → 整个版本生效。重跑恢复失败批次及最后激活前的中断。
- `KB_READ_MODEL=legacy|v2` 控制线上检索；四种检索策略共用适配后的 hit。v2 在召回之前限制 SQL 中完整发布的版本，删除后不再召回；历史版本和原文仍保留，支持旧引用定位。
- `KB_V2_DUAL_WRITE=true` 将旧接口/审核流程新增的 pending 行同步至 v2；读模式为 v2 时自动同步。旧流程回滚删除会同时撤销对应 v2 文档的生效状态。
- 新接口沿用 `/api/kb` 的管理员权限：`POST /v2/documents`、`POST /v2/vectorize`、`DELETE /v2/documents/{document_id}`、`GET /v2/chunks/{chunk_id}`。
- 原审核/FAQ/Agent 引用保留编号及 question/answer，新增来源、版本、稳定 Chunk ID 和 content。关键条款特征保留规则版本；置信度算法保持现有词表计算。
- 旧向量化也修正为 flush 成功后再标记 done；分类过滤值经 JSON 转义。

## 部署顺序

在项目环境中运行以下命令，Windows PowerShell 可使用 `.venv-win\Scripts\python.exe`，已有 uv 环境可使用 `uv run python`。

```powershell
# 先启动 MySQL、Milvus，确认上游 Embedding/Rerank 可用。
python -m scripts.kb_v2 snapshot
$env:KB_READ_MODEL = 'legacy'
$env:RAG_EVAL_OUTPUT_DIR = '.rag-upgrade-work/eval-legacy'
python -m scripts.eval_ch04 --skip-gen

# 初始化新表及独立 Collection；也可由运维应用 sql/ch12-rag-v2-ddl.sql。
python -m scripts.kb_v2 init

# 两种来源迁移方式，按实际来源核对后选择。
python -m scripts.kb_v2 build
# 或：无法恢复来源时，将旧行按真实数据库行标识迁移。
# python -m scripts.kb_v2 backfill-legacy

python -m scripts.kb_v2 vectorize
$env:KB_READ_MODEL = 'v2'
$env:RAG_EVAL_OUTPUT_DIR = '.rag-upgrade-work/eval-v2'
python -m scripts.eval_ch04 --skip-gen
```

`snapshot` 导出旧 Chunk 行、源文件 hash、Milvus Schema/索引及检索相关配置到被 Git 忽略的目录，不导出 API 密钥。它是应用层审计快照，不替代数据库一致性备份和 Milvus 备份；正式变更前仍应使用现有运维备份流程。

`build` 重新读取文件并记录可核实的来源。`backfill-legacy` 为每条旧行记录 `legacy://knowledge_chunks/{id}` 和 `legacy_unknown_source`，该 URI 只指向数据库原行，不表示已恢复源文件。**不要将同一批旧文档全量 backfill 后又 build，否则会出现两份检索记录。** 混合来源应先核对清单，选择迁移范围；不能依赖章节名自动对齐。审核产生的新增记录通过双写继续同步。

更新文档使用相同 `source_uri` 重新提交或重跑 `build`，随后 `vectorize`；同内容、同配方重复提交复用原主键，内容、分类或切块参数改变则创建新版本。原 CLI 同时支持 `python -m scripts.build_kb --v2`、`python -m scripts.kb_repatch --v2`、`python -m scripts.vectorize_kb --v2`。

混合来源可用 `python -m scripts.kb_v2 backfill-legacy --legacy-ids 101 102` 只迁移已经人工核实范围的旧行。`KB_V2_DUAL_WRITE=true` 应在完成初始化后开启；为避免迁移窗口漏掉新增知识，开启双写后应再核对一次旧表新增/待补记录。

手工录入示例：

```json
{"source_uri":"data/kb/returns-policy.md","title":"退货政策","document_type":"policy","category":"售后","text":"# 退货政策\n## 时效\n七天内可申请。"}
```

`source_uri` 由管理员提供并在服务器端生成 ID，不从 Markdown 提取控制字段。它不是可自动抓取的 URL，也不会被当作本地文件路径执行。引用中的 `section_path` 对应切块时的完整标题路径。表格重复表头和句子 overlap 会使内容不是源文件的连续切片，所以目前不声称提供字符偏移。

## 灰度、回滚与限制

双索引评测分别记录 Recall@5、MRR、证据覆盖率、检索延迟及错误样本；根据实际结果约定允许变化，再开启双写并重启服务，最后灰度 `KB_READ_MODEL=v2`。回滚为 `KB_READ_MODEL=legacy` 并重启，旧表及旧索引始终保留。新版专属录入不会反写旧表，灰度期间需要明确知识编辑入口及回滚内容范围。

当前 v2 的生效版本通过 SQL 主键列表限制召回，每次查询一次 SQL，适用于当前小规模知识库。大规模时需要版本集合/分区等更紧凑的过滤方式，并重新测量 P95 延迟。删除只撤销召回资格，历史索引和数据延迟保留，不包含自动物理清理。

项目目前没有知识库多租户/文档 ACL 模型，因此没有添加 tenant/visibility 字段，也不把 category 当授权条件。普通分类仍保留已有无过滤重试；将来加入 ACL 必须独立传递可信过滤条件，并确保回退保持 ACL。

## 首次验证记录

2026-10-23，Windows Python 3.13，独立 `.venv-win` 环境：

- **73 项测试通过**，耗时 13.10 秒：28 项新增 v2 测试，以及 45 项已有切块、检索、FAQ、置信度、Agent/上下文、去重回归。v2 存储测试使用开启外键校验的内存 SQLite；Milvus/Embedding/Rerank 使用测试替身。
- 新增测试覆盖重复导入、版本切换、中途 Embedding/flush 失败重跑、不同 Collection 状态隔离、同名章节不串源、邻居边界、删除/重新导入、历史引用、旧接口双写、四策略召回前过滤、Schema/维度校验及分类转义。
- 全部 242 个 Python 文件通过 AST 语法检查；迁移 CLI `--help` 正常。
- 原有 `test_kb_repository.py`、`test_kb_models.py` 和 `test_dualwrite.py::test_write_pending_links_neighbors` 共 8 项测试均因配置中的测试 MySQL `127.0.0.1:3307` 拒绝连接而在 fixture 初始化阶段报错，未执行测试断言。没有改接其他 MySQL 实例。
- 项目配置中的应用/测试 MySQL 3307、Milvus 19530 当前均不可达，Docker daemon 未启动。真实服务快照、`eval_ch04 --skip-gen` 基线、v2 对照及灰度未执行，也未创建实际新表或 Collection。
- `.rag-upgrade-work/source-baseline/` 已保存 6 份来源文件的指纹和已有评测报告副本；这些报告是已有产物，不是此次重跑结果。

通过的测试命令：

```powershell
.venv-win\Scripts\python.exe -m pytest -q tests/kb/test_v2_lifecycle.py tests/kb/test_v2_search.py tests/kb/test_v2_schema.py tests/test_documents.py tests/test_chunking_split.py tests/test_chunking_overlap.py tests/test_chunking_table.py tests/core/test_retrieval_ch04.py tests/core/test_confidence.py tests/tools/test_query_faq_rag.py tests/graph/test_forced_rag.py tests/graph/test_ch09_confidence_gate.py tests/test_context_assembly.py tests/test_dedup.py --basetemp=.work-tmp/pytest-combined --tb=short
```

Windows 受限环境首次运行时异步测试停在本地通信初始化，放开该进程的沙箱限制后上述测试全部通过。没有因环境限制修改生产事件循环。

以上为首次执行时的历史状态。后续真实迁移、检索对照及本地灰度结果见下文“最终验证结果”；正式线上流量及生产验收仍未执行。

## 续执行记录（2026-10-23）

已恢复 WSL `Ubuntu-22.04` 内项目原有 MySQL、etcd、MinIO、Milvus 容器及原数据卷。Docker Desktop 曾因遗留 `dockerInference` 运行时文件启动失败，该单个文件已改名保留；本次实际使用 WSL 原 Docker 引擎，没有恢复出厂设置、重建数据卷或改接 Windows 3306 上的其他服务。

- 原来受阻的 8 项数据库测试全部通过。合并真实 MySQL/Milvus 与原回归测试后，86 项通过，JUnit 结果位于 `.rag-upgrade-work/regression-live.xml`。
- 新增 `tests/kb/test_v2_live.py` 的真实 v2 版本切换、flush 失败恢复与删除测试通过，结果位于 `.rag-upgrade-work/v2-live.xml`；累计 87 项相关测试通过。
- 原数据库一致性逻辑备份：`.rag-upgrade-work/pre-migration-db-20261023/commerceflow.sql`；实际旧 Chunk、Milvus Schema/索引及配置快照：`.rag-upgrade-work/pre-migration-restored-20261023/`。
- 来源审计结果位于 `.rag-upgrade-work/source-mapping-audit.json`：旧库 52 行全部与实际来源文件的标题、正文、完整章节路径、内容类型唯一匹配，未知及歧义均为 0。此次没有凭章节名推断来源，也没有重复 backfill 同一批内容。
- 已创建实际 v2 表及 `knowledge_v2_test` Collection，写入并发布 6 份文档、6 个版本、52 个块。重复导入后数量不变；旧库/旧索引仍各有 52 条。
- `python -m scripts.validate_kb_v2` 的实际索引核对通过：52 条记录的内容、文档、版本和来源 URI 均与 MySQL 一致。结果为 `.rag-upgrade-work/v2-validation.json`。
- 用户已明确授权使用现有 `open.bigmodel.cn` Embedding/Rerank API 发送本次评测问题及知识库文档；没有发送用户聊天记录或密钥。

最初两轮评测的重排阶段出现超时并已中止，日志 `eval-legacy-restored.log` / `eval-v2-restored.log` 不作为验收结果。受控对照使用 `RAG_EVAL_CONCURRENCY=2`、`RAG_EVAL_TIMEOUT=90`，新旧索引串行运行。评测现在每 20 题输出进度，并在每种策略完成后保存 `meta.partial=true` 的阶段结果；只有完整报告才能用于切换判断。

真实服务保持运行时，使用 WSL 控制原容器：

```powershell
wsl -d Ubuntu-22.04 -- docker ps
wsl -d Ubuntu-22.04 -- docker start commerceflow-mysql commerceflow-milvus-etcd commerceflow-milvus-minio commerceflow-milvus
```

为避免 WSL 空闲退出再次中断服务，已启动隐藏的保活进程，PID 保存于 `.rag-upgrade-work/wsl-service-keeper.pid`。完成使用后可执行 `Stop-Process -Id (Get-Content .rag-upgrade-work/wsl-service-keeper.pid)` 停止该保活进程；不删除任何容器或数据卷。

独立进程验证实际读模型开关及四策略引用：

```powershell
$env:KB_READ_MODEL='v2'
python -m scripts.validate_kb_v2 --canary --output .rag-upgrade-work/v2-canary.json
```

`--canary` 检查四个题型各自的四种检索策略及准确版本引用，共 16 组请求，调用已配置的外部 Embedding/Rerank API。它要求独立进程设置 `KB_READ_MODEL=v2`，不修改应用默认配置，也不等同于真实线上用户流量灰度。

## 本地网页登录灰度

用户确认后，已另行备份 `.rag-upgrade-work/pre-auth-20261023/commerceflow.sql`，执行既有 `sql/ch11-security-ddl.sql` 中缺少的 9 张认证/审批表，并初始化本地 `rag_canary_admin`。没有自动分配历史会话或订单归属。管理员随机密码及灰度专用签名配置仅保存在 `.rag-upgrade-work/canary-admin.json`，文件访问权限已限制为当前 Windows 用户及 SYSTEM；不在文档或运行日志中复制密码。

灰度实例只监听 `http://127.0.0.1:8001`，读模型为 v2，默认 `.env` 保留 legacy。它使用独立的图 Checkpoint 文件。启动入口为 `scripts/start_rag_canary.ps1`，PID 位于 `.rag-upgrade-work/canary-server.pid`；该脚本仅给子进程设置 v2 开关、本地 HTTP Cookie 和签名配置。

已完成真实 HTTP 验证：登录页面 200，匿名访问管理员接口 401，管理员登录和身份校验通过；`/api/kb/overview` 明确报告 `v2/knowledge_v2_test`；三条检索命中的来源字段和原文定位一致；登出 204，旧会话随后被拒绝。结果为 `.rag-upgrade-work/http-canary.json`。为此修正了 `/api/kb/search` 的手工投影，保留已有字段并透传新增的稳定 Chunk、文档、版本及来源信息。

补充知识库 API、认证和资源授权回归共 49 项全部通过，JUnit 为 `.rag-upgrade-work/api-auth-regression.xml`。三份 JUnit 合并去重后共 **136 项相关测试通过**。

完整的本地 HTTP 四策略灰度检查命令：

```powershell
python -m scripts.validate_rag_http_canary
```

该命令只从本机文件读取凭据，拒绝非回环目标地址，令牌仅在内存中使用。它登录真实服务，执行四题型 × 四策略共 16 次检索，逐条对照引用原文，最后登出并检查会话失效。调用向量/重排策略时，会使用项目已配置的 BigModel API。

## 最终验证结果（2026-10-23）

实际迁移、完整检索对照与本地灰度均已完成。6 份源文件及旧表 52 行全字段与迁移前快照一致；v2 的 6 个文档、6 个版本、52 个块已发布，全部索引来源与 MySQL 一致。

- 四份 JUnit 合并去重后 **142 项相关测试全部通过**，包括原受阻的 8 项数据库测试；245 个 Python 文件通过 AST 检查。
- 新旧索引四策略各 240 道可答题，合计 **1,920 次真实检索，失败 0 次**；完整结果分别为 `.rag-upgrade-work/eval-legacy-controlled/rag_eval.json` 和 `.rag-upgrade-work/eval-v2-controlled/rag_eval.json`，均无 `meta.partial` 标志。
- 真实 HTTP 四策略灰度 **16 次检索、48 次引用原文核对全部通过**；登录、匿名拦截、登出和旧会话失效也通过。报告为 `.rag-upgrade-work/http-canary-full.json`。
- 灰度评估页遵循 `RAG_EVAL_OUTPUT_DIR`，展示本轮完整 v2 报告；主配置的历史报告不被覆盖。对应 6 项评估 API 回归通过，已计入 142 项。

完整指标表、运行口径和证据路径见 [真实检索对照与本地灰度结果](CommerceFlow-RAG-v2检索对照评测结果.md)。v2 重排 Recall 为 0.976（旧版 0.974），MRR 为 0.687（旧版 0.698）；并非四策略全面提升。主 `.env` 仍保留 legacy，本地 `http://127.0.0.1:8001/login` 使用 v2，凭据仅在 `.rag-upgrade-work/canary-admin.json`。

本轮 `--skip-gen` 未运行生成质量、LLM 裁判及 D 桶库外拒答；本地灰度没有引入真实线上流量。正式默认切换需结合指标退化接受门槛及后续业务验收决定。
