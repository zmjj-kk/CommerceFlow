from unittest.mock import AsyncMock

import pytest

from app.core import retrieval
from app.kb import milvus_client as mc, v2_search
from app.kb.domain import build_document, search_record


@pytest.mark.parametrize("strategy", ["vector", "bm25", "hybrid", "hybrid_rerank"])
async def test_all_strategies_filter_before_recall_and_preserve_provenance(monkeypatch, strategy):
    doc, chunks = build_document("# 运费\n满99包邮", "data/kb/a.md")
    record = search_record(chunks[0], doc, 7)
    calls = []

    class Client:
        def search(self, collection, **kwargs):
            calls.append(kwargs)
            assert kwargs["filter"].startswith("id in [7]")
            assert 'category == "售后"' in kwargs["filter"]
            return [[{"id": 7, "distance": 0.9, "entity": record}]]

        def hybrid_search(self, collection, **kwargs):
            calls.append(kwargs)
            assert len(kwargs["reqs"]) == 2
            for request in kwargs["reqs"]:
                assert "id in [7]" in request.expr
                assert 'category == "售后"' in request.expr
            return [[{"id": 7, "distance": 0.9, "entity": record}]]

    monkeypatch.setattr(v2_search.repository, "active_chunk_ids", AsyncMock(return_value=[7]))
    monkeypatch.setattr(mc, "ensure_collection", lambda *a, **kw: None)
    monkeypatch.setattr(v2_search.embeddings, "embed_query", AsyncMock(return_value=[0.1] * mc.DIM))
    monkeypatch.setattr(v2_search.rerank, "rerank", AsyncMock(return_value=[(0, 0.95)]))
    hits = await retrieval.search_knowledge("运费", strategy, category="售后", client=Client(),
                                            collection="knowledge_v2_test", split=False)
    assert calls and hits[0]["document_id"] == doc.document_id
    assert hits[0]["version_id"] == doc.version_id
    assert hits[0]["answer"] == hits[0]["content"]
    if strategy == "hybrid_rerank":
        assert hits[0]["rerank_score"] == 0.95


async def test_no_active_versions_never_calls_milvus_or_embeddings(monkeypatch):
    monkeypatch.setattr(v2_search.repository, "active_chunk_ids", AsyncMock(return_value=[]))
    embed = AsyncMock(side_effect=AssertionError("must not embed"))
    monkeypatch.setattr(v2_search.embeddings, "embed_query", embed)
    assert await retrieval.search_knowledge("运费", collection="knowledge_v2_test", split=False) == []


def test_category_escaping_and_legacy_adapter():
    category = '运费" or id > 0 or category == "'
    expr = mc._cat_expr(category)
    assert '\\"' in expr
    hit = mc._hit({"id": 1, "distance": 0.8, "entity": {
        "question": "q", "answer": "a", "section_path": "s", "content_type": "policy", "category": "c"}})
    assert hit["source_status"] == "legacy_unknown_source"
    assert "document_id" not in hit


def test_same_section_name_in_different_sources_is_not_demoted():
    def h(i, source):
        return {"id": i, "document_id": source, "version_id": "v1", "section_path": "政策 / 运费"}
    assert [h["id"] for h in retrieval._merge_round_robin([[h(1, "a"), h(3, "c")], [h(2, "b")]])] == [1, 2, 3]


async def test_read_switch_defaults_to_legacy_and_routes_v2(monkeypatch):
    monkeypatch.setattr(retrieval.settings, "kb_read_model", "v2")
    search = AsyncMock(return_value=[])
    monkeypatch.setattr(v2_search, "search", search)
    assert await retrieval.search_knowledge("运费", split=False) == []
    assert search.call_args.args[5] == retrieval.settings.kb_v2_collection
