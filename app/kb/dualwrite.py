# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com
from app.core import embeddings
from app.db import repository
from app.kb import milvus_client
from app.kb.documents import Chunk


async def write_pending(chunks: list[Chunk]) -> list[int]:
    """按顺序把一份文档的 chunks 写入 MySQL(status=pending),并在同文档内连 prev/next 指针。"""
    ids: list[int] = []
    for c in chunks:
        cid = await repository.insert_knowledge_chunk(
            c.category, c.questions, c.answer,
            section_path=c.section_path, content_type=c.content_type,
            is_key_clause=c.is_key_clause,
        )
        ids.append(cid)
    for i, cid in enumerate(ids):
        prev_id = ids[i - 1] if i > 0 else None
        next_id = ids[i + 1] if i < len(ids) - 1 else None
        await repository.set_chunk_neighbors(cid, prev_id, next_id)
    return ids


def _batches(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


async def vectorize_pending(client=None, batch_size: int = 64, collection: str = milvus_client.COLLECTION) -> int:
    """幂等可重跑:取 pending → 拼 category+questions+answer → 嵌入 + BM25 text → Milvus upsert(PK=id)
    → 回填 vector_id、status=done。
    ch09:client 缺省时在 Milvus 专用线程内惰性创建,upsert/flush 也走 acall——
    server 进程(审核通过写回)与脚本两个场景共用一条安全路径(gRPC 客户端跨线程
    共享在 async server 下会间歇空返回,见 retrieval.py 注)。"""
    if batch_size <= 0 or collection.startswith("knowledge_v2_"):
        raise ValueError("use vectorize_documents for v2; batch_size must be positive")
    pending = await repository.list_pending_chunks()
    from app.config import settings
    mirror_v2 = settings.kb_v2_dual_write or settings.kb_read_model == "v2"
    if mirror_v2:
        from app.kb.domain import adapt_legacy
        for row in pending:
            doc, chunks = adapt_legacy(row)
            await write_document(doc, chunks)
    done = 0
    for batch in _batches(pending, batch_size):
        texts = [f"{r.category}\n{r.questions}\n{r.answer}" for r in batch]
        vectors = await embeddings.embed_texts(texts)
        if len(vectors) != len(batch):
            raise ValueError("embedding response count mismatch")
        rows = [
            {"id": r.id, "dense": v, "text": t,
             "question": r.questions, "answer": r.answer,
             "section_path": r.section_path or "", "content_type": r.content_type or "",
             "category": r.category or ""}
            for r, v, t in zip(batch, vectors, texts)
        ]

        def work_upsert(rows=rows):
            c = client or milvus_client.get_client()
            milvus_client.ensure_collection(c, collection=collection)
            milvus_client.upsert_vectors(c, rows, collection=collection)
            milvus_client.flush(c, collection=collection)
        await milvus_client.acall(work_upsert)
        for r in batch:
            await repository.mark_chunk_vectorized(r.id, str(r.id))
        done += len(batch)
    if mirror_v2:
        await vectorize_documents(client=client, batch_size=batch_size)
    return done


async def write_document(doc, chunks, *, collection=None):
    """Stage a complete immutable source version without changing legacy knowledge."""
    from app.config import settings
    from app.db import kb_v2_repository as repo
    collection = collection or settings.kb_v2_collection
    if not collection.startswith("knowledge_v2_"):
        raise ValueError("v2 must use a separate collection")
    return await repo.stage_document(doc, chunks, collection)


async def vectorize_documents(client=None, batch_size=64, collection=None) -> int:
    from app.config import settings
    from app.db import kb_v2_repository as repo
    from app.kb.domain import Document, chunk_from_dict, search_record
    collection = collection or settings.kb_v2_collection
    if not collection.startswith("knowledge_v2_") or batch_size <= 0:
        raise ValueError("invalid v2 collection or batch_size")
    pending = await repo.pending_records(collection)
    done = 0
    for batch in _batches(pending, batch_size):
        rows = [search_record(chunk_from_dict(c.data), Document(**v.data), c.id) for c, v in batch]
        # Milvus VARCHAR lengths are UTF-8 bytes, not Python characters.
        limits = {"text": 16384, "content": 8192, "answer": 8192, "question": 2048,
                  "section_path": 512, "category": 255, "source_uri": 2048, "title": 2048}
        for row in rows:
            for key, limit in limits.items():
                if len(row[key].encode()) > limit:
                    raise ValueError(f"{row['chunk_id']}: {key} exceeds {limit} UTF-8 bytes")
        vectors = await embeddings.embed_texts([r["text"] for r in rows])
        if len(vectors) != len(rows):
            raise ValueError("embedding response count mismatch")
        for row, vector in zip(rows, vectors):
            if len(vector) != milvus_client.DIM:
                raise ValueError("embedding dimension mismatch")
            row["dense"] = vector

        def work():
            c = client or milvus_client.get_client()
            milvus_client.ensure_collection(c, collection, v2=True)
            milvus_client.upsert_vectors(c, rows, collection)
            milvus_client.flush(c, collection)
        await milvus_client.acall(work)
        # A failed flush leaves every row in this batch pending, safe to upsert again.
        await repo.mark_done(collection, [r["chunk_id"] for r in rows])
        done += len(rows)
    # Also repairs a crash after the last checkpoint but before version activation.
    await repo.activate_ready(collection)
    return done
