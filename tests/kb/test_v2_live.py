"""Actual MySQL + Milvus version publication; no external model calls."""
import uuid

import pytest

from app.config import settings
from app.core import retrieval
from app.db import kb_v2_repository as repo
from app.kb import dualwrite, milvus_client as mc
from app.kb.domain import build_document


async def test_live_version_switch_flush_recovery_and_delete(db_session_factory, monkeypatch):
    collection = "knowledge_v2_live_" + uuid.uuid4().hex[:12]
    client = await mc.acall(mc.get_client, uri=settings.milvus_uri)

    async def embed(texts):
        return [[0.1] * mc.DIM for _ in texts]

    monkeypatch.setattr(dualwrite.embeddings, "embed_texts", embed)
    try:
        old, old_chunks = build_document("# 政策\n## 运费\n旧版满99包邮。", "test://versioned-policy")
        old_ids = await dualwrite.write_document(old, old_chunks, collection=collection)
        assert await dualwrite.vectorize_documents(client, collection=collection) == 1
        hits = await retrieval.search_knowledge("运费", "bm25", client=client, collection=collection, split=False)
        assert hits and hits[0]["version_id"] == old.version_id
        assert hits[0]["content"] == old_chunks[0].content

        new, new_chunks = build_document("# 政策\n## 运费\n新版满199包邮。\n## 规则\n运费买家承担。", "test://versioned-policy")
        new_ids = await dualwrite.write_document(new, new_chunks, collection=collection)
        real_flush = mc.flush

        def fail_flush(*args, **kwargs):
            raise RuntimeError("injected flush failure")

        monkeypatch.setattr(mc, "flush", fail_flush)
        with pytest.raises(RuntimeError, match="injected flush"):
            await dualwrite.vectorize_documents(client, collection=collection)
        assert await repo.active_chunk_ids(collection) == old_ids
        hits = await retrieval.search_knowledge("运费", "bm25", client=client, collection=collection, split=False)
        assert hits and all(h["version_id"] == old.version_id for h in hits)

        monkeypatch.setattr(mc, "flush", real_flush)
        assert await dualwrite.vectorize_documents(client, collection=collection) == 2
        assert await dualwrite.vectorize_documents(client, collection=collection) == 0
        assert set(await repo.active_chunk_ids(collection)) == set(new_ids)
        hits = await retrieval.search_knowledge("运费", "bm25", client=client, collection=collection, split=False)
        assert hits and all(h["version_id"] == new.version_id for h in hits)
        assert (await repo.get_chunk(old_chunks[0].chunk_id))["chunk"]["content"] == old_chunks[0].content
        assert await mc.acall(mc.count, client, collection) == 3
        assert await repo.delete_document(new.document_id)
        assert await retrieval.search_knowledge("运费", "bm25", client=client, collection=collection, split=False) == []
    finally:
        await mc.acall(mc.drop, client, collection)
