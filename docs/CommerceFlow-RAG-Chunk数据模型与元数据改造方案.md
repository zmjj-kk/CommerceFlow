# CommerceFlow RAG Chunk 数据模型与元数据重构方案

> 状态（2026-10-23）：代码、实际数据库及独立 v2 索引迁移完成；142 项相关测试通过，真实双索引四策略检索评测和本地网页登录/HTTP 灰度已完成。主配置继续使用 legacy，独立本地实例使用 v2；生成质量和正式流量切换尚未验收。指标及退化情况见 [真实检索对照结果](CommerceFlow-RAG-v2检索对照评测结果.md)，实现、迁移命令与限制见 [RAG v2 实施与验证](CommerceFlow-RAG-v2实施与验证.md)。原则：渐进迁移、不中断现有查询、保持数据可溯源、可评测回滚。

## 1. 目标与结论

现有 `Chunk(category, questions, answer, section_path, content_type, is_key_clause)` 更像由 Markdown 章节提取的 FAQ/政策问答记录，并非通用文档 Chunk。拟将 **Document（来源与版本）— Chunk（原始内容及位置）— Search Record（为检索优化的投影）** 分层。元数据只保留有明确用途的属性，兼容现有 BM25 + Dense + RRF + Rerank；不因追求“结构纯净”而牺牲检索或线上稳定性。

改造验收应以真实调用和评测结果为依据，而不是仅删除冗余字段。

## 2. 基于实际源码的现状

- `app/kb/documents.py`：`build_chunks()` 使用 `MarkdownHeaderTextSplitter` 读取 h1～h4；`section_path` 是标题完整路径；`questions` 实际是最末级章节标题而非天然用户问题；`answer` 是正文片段；`category` 是上级路径的冗余投影。
- `app/kb/chunking.py`：分段、句子 Overlap 和表格按行拆分已存在，应保留这些能力。
- `app/kb/documents.py`：`is_key_clause` 由标题和正文前 40 字包含关键词的规则得到，属于派生特征。不能未经检索就断定无用：代码注明其被 ch09 置信度/审核流程复用。
- `app/kb/dualwrite.py`：先将 Chunk 以 pending 写入 MySQL，后根据 `category + questions + answer` 生成 Embedding/BM25 文本，向 Milvus upsert；写回 vector_id/status=done，并保存前后邻居。
- `app/kb/milvus_client.py`：Milvus 当前 Collection 保存 id、dense、text、sparse、question、answer、section_path、content_type、category；`sparse` 由 `text` 的 BM25 Function 生成。
- `app/core/retrieval.py`、`app/tools/builtin/faq.py`、`app/graph/nodes.py`：多处依赖命中记录的 `question`、`answer`、`section_path`、`category`，不应直接删字段。
- 当前代码没有在以上链路中体现可靠的 `document_id`、源文档版本或源块偏移，这让跨文档追溯、按文档更新和删除难以做好。

## 3. 目标模型：明确三层职责

### 3.1 Document（文档级）

建议字段：
- `document_id`：稳定逻辑文档 ID（同一份文档跨版本保持不变）。
- `version_id`：具体版本 ID，可基于内容指纹生成。
- `title`、`source_uri` / `source_path`、`content_hash`、`updated_at`。
- `category`：受控分类，仅在确实需要业务过滤时保留；可继承给 Chunk 的检索投影。
- `tenant_id`、`visibility`：**只在多租户/权限需求成立时**引入，服务端生成，不能信任用户文本。

### 3.2 Chunk（真正的切块）

推荐 Python 领域模型（示意，不是立即替换现有代码）：

```python
from dataclasses import dataclass, field

@dataclass
class Chunk:
    chunk_id: str
    document_id: str
    version_id: str
    content: str
    chunk_index: int
    section_path: tuple[str, ...] = ()
    content_type: str = "text"
    metadata: dict[str, str] = field(default_factory=dict)
```

- `content` 保存可追溯的正文，不再将正文强制称为 `answer`。
- `section_path` 保持完整章节层级，展示引用和邻块定位时使用。
- `content_type` 仅表示真实结构类型，如 `text/table/faq`；原来如 policy/manual 这样的业务类型应归到文档分类或另设 document_type，避免枚举语义混用。
- `chunk_index` 定位文档内顺序；需要高精度引用时再加入 `start_offset/end_offset`，必须在切块阶段实测能稳定还原。
- 不把 `is_key_clause` 当基础内容字段；若下游有效，迁到独立 `features.is_key_clause`（有版本的规则生成的派生特征）。

### 3.3 Search Record（索引投影）

检索投影与源 Chunk 分开，可保留针对 FAQ 的 `question`、`answer`、`search_text`、`category`、`section_path` 等**冗余字段**，以保障 Milvus 检索兼容。应有统一构造器：

```python
def build_search_text(chunk, doc, generated_questions=None):
    title = " / ".join(chunk.section_path)
    qs = " ".join(generated_questions or [])
    return "\n".join(x for x in [doc.category, title, qs, chunk.content] if x)
```

注意：不能直接沿用 `questions=title` 并对外宣称是“用户问题”。只有经独立 QA 生成/审核得到的内容才写入 `generated_questions`；原章节标题始终可作为检索增强文本。

## 4. 字段取舍

- **保留**：`content`、`document_id`、`version_id`、`chunk_index`、`section_path`。
- **按需保留**：`category`（业务过滤）、`content_type`（表格/文本差异化处理）、`is_key_clause` 派生信号（以 ch09 实际使用与消融评测为准）。
- **废弃旧语义**：`questions` 不再冒充所有 Chunk 的必填自然语言问题；`answer` 作为兼容投影而非源内容的唯一名称。
- **单一事实来源**：文档归属和版本在 Document；类别可由 Document 下发；章节路径在 Chunk；不要在三个位置人工维护同一个字段。
- 若 `category` 用于授权过滤，则必须采用可信来源、过滤在召回之前执行；不能像可选业务分类一样“查不到就取消权限过滤”。

## 5. 兼容迁移策略（不可直接替换现有 Collection）

### 阶段 0：梳理依赖和建立基线

1. 检查 `app/db/models.py`、`app/db/repository.py`、`app/kb/documents.py`、`app/kb/dualwrite.py`、`app/kb/milvus_client.py`、`app/core/retrieval.py`、`app/tools/builtin/faq.py`、`app/graph/nodes.py` 及全部 SQL/测试；搜 `questions`、`answer`、`category`、`section_path`、`is_key_clause` 的读写位置。
2. 固化线上/测试库当前数据快照、源文件列表、Collection Schema、索引配置与项目配置；不要对正式 Collection 直接 drop。
3. 运行当前测试和 `make eval-rag SKIP_GEN=1`（需已启动依赖服务），保存基线 Recall@5、MRR、证据覆盖率、检索延迟和错误样本；不能提前宣称优化有效。

### 阶段 1：加新模型和兼容适配（保持旧行为）

1. 新增独立 Document/Chunk 领域类与序列化适配器；暂时保留 `documents.Chunk` 的旧接口。
2. 在 MySQL 中新增文档版本信息及 `document_id/version_id/chunk_index/content` 对应列或新表。具体 DDL 以现有 models、约束和迁移脚本核查后确定。
3. 旧数据无可靠来源时，标记为 `legacy_unknown_source` 或按能够确证的导入批次归属；**不得凭 section_path 推断唯一来源文档**。
4. 为源文档内容生成 hash，使用稳定 Chunk ID/映射策略，确保重试不会凭随机 ID 产生重复知识。
5. 现有 `write_pending()`、`vectorize_pending()` 增加适配层，保持旧字段输出与现有查询完全兼容。

### 阶段 2：新索引双写和对照验证

1. 建独立测试 Collection（如 `knowledge_v2_test`）及新 Schema，不原地覆盖 `knowledge`；复用现有 Embedding 维度、中文 Analyzer、BM25 Function、COSINE 索引。
2. 新索引以 `content + 章节标题 + 可选问句` 构造 `search_text`；对比旧索引 `category + questions + answer`，保证可追踪差异。
3. MySQL pending → Embedding → Milvus upsert/flush → done，失败保持可重跑；version_id 与索引记录严格关联。
4. 增量更新使用版本切换：先完整构建新版本，再切换生效版本；旧版本延迟清理，避免读到部分写入数据。
5. 显式检查邻居链边界：只允许同一 `document_id + version_id` 的 Chunk 互相关联；检查是否应跨章节链接。
6. 新旧 Collection 输出由适配器统一为当前 hit 接口，避免同时修改 Rerank、引用和 Agent。

### 阶段 3：检索与引用改造

1. 检索返回稳定 `chunk_id`、`document_id`、`version_id`、`content`、`section_path`；向旧消费方暂时投影 `question`、`answer`。
2. 引用中携带来源文档、章节和版本，能从回答追到准确知识版本；源文件允许时支持定位原文。
3. `category` 精确筛选统一值域和校验；普通分类检索可以评估是否需要无过滤降级，但授权级过滤绝不能降级取消。
4. 检查 `is_key_clause` 是否实际影响 ch09 置信度、审核回写、排序；保留派生计算或以可重复的消融试验决定去留。

### 阶段 4：灰度与清理

1. 使用开关切换新旧检索投影；先测试、后小流量灰度，观察召回、延迟、异常与引用正确率。
2. 达到验收门槛后才切换默认索引；保留原 Collection 和数据快照直到验证结束。
3. 下游完全迁移后，再移除旧 `questions/answer` 专属强制字段与无用适配；禁止一开始就删除。

## 6. 测试与验收清单

- 切块：标题树、无标题正文、中文长句、跨章节、Overlap、表格表头重复、空段落。
- 来源：同名章节来自不同文件不串源；更新后引用能找到正确版本；删除文档不留下可召回的旧版本。
- 幂等：同一文档重复导入，Chunk 数量和主键不增长；Embedding/Milvus 中途失败后重跑结果一致。
- 检索：vector / bm25 / hybrid / hybrid_rerank 四策略均可用；多意图查询、category 过滤、RRF、Rerank 与回退路径正常。
- 安全：租户/文档授权先过滤再召回；文档内容和用户查询不得覆盖服务端元数据。
- 回归：原 API 返回与引用编号保持兼容；`tests/test_documents.py`、`tests/test_dualwrite.py`、`tests/test_chunking_*.py` 及检索评测全部通过。
- 验收门槛：先记录基线，再商定 Recall@5、MRR、证据覆盖率、P95 延迟、失败率和引用定位准确率的允许变化；不得虚构提升百分比。

## 7. 风险与回滚

1. **Embedding 文本变化**会改变 Dense/BM25 召回，必须重新向量化并评测；不得仅改 Python 类而忽视索引。
2. **字段名变化**影响 MySQL Repository、Milvus Schema、`_hit()`、Rerank 输入、置信度计算、前端引用与评测脚本。
3. **来源无法恢复**：历史数据可标记 unknown，但不得编造 `document_id`/版本。
4. **删除 is_key_clause 过早**：可能损害置信度/审核信号，必须先核查。
5. **回滚**：切回旧 Collection 与旧适配器；保留旧表/旧字段直到稳定性验证完成；严禁未经备份做不可逆迁移。

## 8. 推荐实施顺序与完成定义

优先级 P0：依赖盘点、基线评测、文档来源映射。
P1：新 Document/Chunk 模型、兼容适配、新 Schema 与测试 Collection。
P2：增量双写、文档版本生命周期、引用溯源、灰度。
P3：问句生成、分类简化、关键条款特征消融等可选优化。

**完成定义**：新模型能准确表达文档来源和内容；旧 API/RAG 路径仍可用；重复入库无副作用；引用定位正确；离线评测和灰度数据证明没有不可接受的性能回退；能随时切回旧索引。

> 本文保留原始设计与验收要求；当前代码实现不代表已修改实际数据库、创建实际索引或通过生产灰度验收。
