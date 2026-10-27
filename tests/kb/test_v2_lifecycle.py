"""Source lifecycle tests use isolated SQLite; no MySQL/Milvus service required."""
from dataclasses import asdict
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.db.base as db
from app.db.kb_v2_models import KBDocument, KBVersion, KBSourceChunk, KBIndexState, KBActiveVersion
from app.db import kb_v2_repository as repo
from app.kb import dualwrite, milvus_client as mc
from app.kb.domain import adapt_legacy, build_document, build_search_text, chunk_from_dict, citation, search_record

COLLECTION = "knowledge_v2_test"


@pytest_asyncio.fixture
async def store(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    @event.listens_for(engine.sync_engine, "connect")
    def enable_foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")
    tables = [m.__table__ for m in (KBDocument, KBVersion, KBSourceChunk, KBIndexState, KBActiveVersion)]
    async with engine.begin() as c:
        await c.run_sync(lambda connection: db.Base.metadata.create_all(connection, tables=tables))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db, "async_session", factory)
    yield factory
    await engine.dispose()


@pytest.fixture
def index(monkeypatch):
    class FakeIndex:
        def __init__(self):
            self.rows = {}
            self.fail_flush = False

        def upsert(self, collection, rows):
            for row in rows:
                self.rows[(collection, row["id"])] = dict(row)

        def flush(self, collection):
            if self.fail_flush:
                raise RuntimeError("flush failed")

    client = FakeIndex()
    monkeypatch.setattr(mc, "ensure_collection", lambda *a, **kw: None)
    monkeypatch.setattr(dualwrite.embeddings, "embed_texts", AsyncMock(
        side_effect=lambda texts: [[0.1] * mc.DIM for t in texts]))
    return client


def document(text="# 政策\n## 运费\n满99元包邮。", source="data/kb/a.md", **kw):
    return build_document(text, source, **kw)


def test_source_identity_projection_and_roundtrip():
    doc, chunks = document(category="售后", document_type="policy")
    other, other_chunks = document(source="data/kb/b.md")
    assert doc.document_id != other.document_id
    assert chunks[0].chunk_id != other_chunks[0].chunk_id
    assert chunks[0].section_path == ("政策", "运费")
    assert chunks[0].content_type == "text"
    assert chunks[0].generated_questions == ()
    assert chunks[0].features == {"is_key_clause": 1, "rule_version": "key-terms-v1"}
    assert chunk_from_dict(asdict(chunks[0])) == chunks[0]
    assert build_search_text(chunks[0], doc) == "售后\n政策 / 运费\n满99元包邮。"
    record = search_record(chunks[0], doc, 1)
    ref = citation({**record, "score": 0.9}, 3)
    assert ref["n"] == 3 and ref["version_id"] == doc.version_id
    assert ref["answer"] == ref["content"]
    with pytest.raises(ValueError):
        search_record(chunks[0], other, 1)


def test_hash_changes_for_content_recipe_and_metadata():
    doc, chunks = document()
    same, same_chunks = document()
    assert same == doc and same_chunks == chunks
    for kwargs in ({"text": "新正文"}, {"chunk_size": 200}, {"category": "分类"}):
        changed, _ = document(**kwargs)
        assert changed.document_id == doc.document_id
        assert changed.version_id != doc.version_id
    with pytest.raises(ValueError):
        document(source=" ")


def test_headingless_and_table_chunks():
    doc, chunks = document(text="没有标题的正文。")
    assert chunks[0].section_path == () and chunks[0].content == "没有标题的正文。"
    _, table = document(text="# 参数\n|型号|价格|\n|---|---|\n|A|1|\n|B|2|", table_max_rows=1)
    assert len(table) == 2 and all(c.content_type == "table" for c in table)
    assert all("|型号|价格|" in c.content for c in table)
    assert [c.chunk_index for c in table] == [0, 1]


def test_zero_overlap_and_section_boundaries():
    _, chunks = document(text="# 标题\n## 甲\n第一句内容。第二句内容。第三句内容。\n## 乙\n独立段落。",
                         chunk_size=10, overlap=0)
    assert len(chunks) > 2
    assert chunks[-1].content == "独立段落。"
    assert chunks[-1].section_path == ("标题", "乙")
    assert all(len(c.content) <= 10 for c in chunks)


async def test_reimport_and_neighbors_do_not_cross_documents(store):
    a, ac = document(text="# 政策\n## 一\n正文一。\n## 二\n正文二。")
    b, bc = document(text="# 政策\n## 一\n正文一。\n## 二\n正文二。", source="b")
    ids = await repo.stage_document(a, ac, COLLECTION)
    assert await repo.stage_document(a, ac, COLLECTION) == ids
    other_ids = await repo.stage_document(b, bc, COLLECTION)
    assert set(ids).isdisjoint(other_ids)
    first = await repo.get_chunk(ac[0].chunk_id)
    last = await repo.get_chunk(ac[-1].chunk_id)
    assert first["prev_chunk_id"] is None and first["next_chunk_id"] == ac[1].chunk_id
    assert last["next_chunk_id"] is None
    assert len(await repo.pending_records(COLLECTION)) == 4


async def test_flush_failure_remains_pending_then_retry_is_idempotent(store, index):
    doc, chunks = document()
    ids = await repo.stage_document(doc, chunks, COLLECTION)
    index.fail_flush = True
    with pytest.raises(RuntimeError, match="flush failed"):
        await dualwrite.vectorize_documents(index)
    assert len(await repo.pending_records(COLLECTION)) == 1
    assert await repo.active_chunk_ids(COLLECTION) == []
    index.fail_flush = False
    assert await dualwrite.vectorize_documents(index) == 1
    assert await dualwrite.vectorize_documents(index) == 0
    assert len(index.rows) == 1
    assert await repo.active_chunk_ids(COLLECTION) == ids


async def test_update_publishes_only_complete_version_and_keeps_historical_citation(store, index, monkeypatch):
    old, old_chunks = document()
    old_ids = await repo.stage_document(old, old_chunks, COLLECTION)
    await dualwrite.vectorize_documents(index)
    new, new_chunks = document(text="# 政策\n## 运费\n满199包邮。\n## 期限\n七天。")
    new_ids = await repo.stage_document(new, new_chunks, COLLECTION)
    assert await repo.active_chunk_ids(COLLECTION) == old_ids
    calls = 0

    async def flaky(texts):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("embedding unavailable")
        return [[0.1] * mc.DIM for t in texts]

    monkeypatch.setattr(dualwrite.embeddings, "embed_texts", flaky)
    with pytest.raises(RuntimeError):
        await dualwrite.vectorize_documents(index, batch_size=1)
    assert await repo.active_chunk_ids(COLLECTION) == old_ids
    await dualwrite.vectorize_documents(index, batch_size=1)
    assert await repo.active_chunk_ids(COLLECTION) == new_ids
    assert (await repo.get_chunk(old_chunks[0].chunk_id))["document"]["version_id"] == old.version_id
    assert await repo.delete_document(old.document_id)
    assert await repo.active_chunk_ids(COLLECTION) == []
    assert await repo.get_chunk(old_chunks[0].chunk_id) is not None


async def test_stale_completed_version_cannot_publish_over_newer_import(store):
    old, oc = document()
    new, nc = document(text="新版内容")
    await repo.stage_document(old, oc, COLLECTION)
    await repo.stage_document(new, nc, COLLECTION)
    await repo.mark_done(COLLECTION, [c.chunk_id for c in oc])
    assert await repo.activate_ready(COLLECTION) == 0
    assert await repo.active_chunk_ids(COLLECTION) == []


async def test_collection_checkpoints_are_independent(store, index):
    doc, chunks = document()
    await repo.stage_document(doc, chunks, COLLECTION)
    await dualwrite.vectorize_documents(index)
    other = "knowledge_v2_shadow"
    await repo.stage_document(doc, chunks, other)
    assert await repo.active_chunk_ids(other) == []
    assert len(await repo.pending_records(other)) == 1
    await dualwrite.vectorize_documents(index, collection=other)
    assert await repo.active_chunk_ids(other) == await repo.active_chunk_ids(COLLECTION)


async def test_post_checkpoint_crash_is_repaired(store, index):
    doc, chunks = document()
    await repo.stage_document(doc, chunks, COLLECTION)
    await repo.mark_done(COLLECTION, [c.chunk_id for c in chunks])
    assert await repo.active_chunk_ids(COLLECTION) == []
    assert await dualwrite.vectorize_documents(index) == 0
    assert len(await repo.active_chunk_ids(COLLECTION)) == 1


async def test_reject_empty_or_invalid_chunk_order(store):
    doc, chunks = document()
    with pytest.raises(ValueError):
        await repo.stage_document(doc, [], COLLECTION)
    other, other_chunks = document(source="other")
    with pytest.raises(ValueError):
        await repo.stage_document(doc, other_chunks, COLLECTION)


async def test_deleted_document_stays_unpublished_until_reimport_completes(store, index):
    old, oc = document()
    await repo.stage_document(old, oc, COLLECTION)
    await dualwrite.vectorize_documents(index)
    await repo.delete_document(old.document_id)
    new, nc = document(text="新版内容")
    await repo.stage_document(new, nc, COLLECTION)
    assert await repo.active_chunk_ids(COLLECTION) == []
    await dualwrite.vectorize_documents(index)
    assert len(await repo.active_chunk_ids(COLLECTION)) == 1


def test_legacy_backfill_never_infers_source_from_headings():
    from types import SimpleNamespace
    row = SimpleNamespace(id=1, questions="运费", answer="包邮", category="政策",
                          section_path="政策 / 运费", is_key_clause=1)
    doc, chunks = adapt_legacy(row)
    row.id = 2
    other, _ = adapt_legacy(row)
    assert doc.document_id != other.document_id
    assert doc.source_uri == "legacy://knowledge_chunks/1"
    assert chunks[0].metadata["source_status"] == "legacy_unknown_source"
    assert chunks[0].generated_questions == ()


async def test_document_api_preserves_source_and_exposes_historical_chunk(store, index):
    from app.api.kb import DocumentInput, ingest_document, source_chunk, remove_document
    from fastapi import HTTPException
    body = DocumentInput(source_uri="data/kb/policy.md", text="# 运费\n满99包邮。", category="售后")
    result = await ingest_document(body)
    assert (await ingest_document(body))["ids"] == result["ids"]
    stored = await source_chunk(result["chunk_ids"][0])
    assert stored["document"]["source_uri"] == body.source_uri
    assert stored["chunk"]["content"] == "满99包邮。"
    await dualwrite.vectorize_documents(index)
    await remove_document(result["document_id"])
    assert await repo.active_chunk_ids(COLLECTION) == []
    assert await source_chunk(result["chunk_ids"][0]) == stored
    with pytest.raises(HTTPException) as error:
        await source_chunk("missing")
    assert error.value.status_code == 404


async def test_v2_mirrors_legacy_pending_and_recovers_after_legacy_already_done(store, index, monkeypatch):
    from types import SimpleNamespace
    row = SimpleNamespace(id=91, questions="运费", answer="包邮", category="政策",
                          section_path="政策 / 运费", content_type="policy", is_key_clause=1)
    monkeypatch.setattr("app.config.settings.kb_v2_dual_write", True)
    monkeypatch.setattr(dualwrite.repository, "list_pending_chunks", AsyncMock(return_value=[row]))
    mark = AsyncMock()
    monkeypatch.setattr(dualwrite.repository, "mark_chunk_vectorized", mark)
    index.fail_flush = True
    with pytest.raises(RuntimeError):
        await dualwrite.vectorize_pending(index)
    mark.assert_not_awaited()
    assert len(await repo.pending_records(COLLECTION)) == 1
    # The source has been staged independently of legacy state, so a later repair
    # can publish v2 even when the legacy batch no longer appears in pending.
    monkeypatch.setattr(dualwrite.repository, "list_pending_chunks", AsyncMock(return_value=[]))
    index.fail_flush = False
    await dualwrite.vectorize_pending(index)
    assert len(await repo.active_chunk_ids(COLLECTION)) == 1


@pytest.mark.parametrize("vectors", [[], [[0.1]]])
async def test_invalid_embedding_response_cannot_mark_done_or_activate(store, index, monkeypatch, vectors):
    doc, chunks = document()
    await repo.stage_document(doc, chunks, COLLECTION)
    monkeypatch.setattr(dualwrite.embeddings, "embed_texts", AsyncMock(return_value=vectors))
    with pytest.raises(ValueError, match="embedding"):
        await dualwrite.vectorize_documents(index)
    assert len(await repo.pending_records(COLLECTION)) == 1
    assert await repo.active_chunk_ids(COLLECTION) == []
    assert index.rows == {}
