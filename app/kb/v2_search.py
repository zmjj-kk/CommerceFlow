"""Milvus recall restricted to a SQL snapshot of fully published source versions."""
import json

from pymilvus import AnnSearchRequest, RRFRanker

from app.config import settings
from app.core import embeddings, rerank
from app.db import kb_v2_repository as repository
from app.kb import milvus_client as mc


async def search(query, strategy, top_k, category, client, collection, bm25_text):
    ids = await repository.active_chunk_ids(collection)
    if not ids:
        return []
    expr = "id in " + json.dumps(ids)
    if category:
        expr += " and " + mc._cat_expr(category)
    text = bm25_text or query
    vector = await embeddings.embed_query(query) if strategy != "bm25" else None

    def work():
        c = client or mc.get_client()
        mc.ensure_collection(c, collection, v2=True)
        if strategy in ("vector", "bm25"):
            dense = strategy == "vector"
            result = c.search(collection, data=[vector if dense else text],
                anns_field="dense" if dense else "sparse", limit=top_k,
                output_fields=mc.V2_OUTPUT, filter=expr,
                search_params={"metric_type": "COSINE" if dense else "BM25"},
                consistency_level="Strong")
        else:
            requests = [AnnSearchRequest(data=[data], anns_field=field,
                param={"metric_type": metric}, limit=settings.recall_top_k, expr=expr)
                for data, field, metric in ((vector, "dense", "COSINE"), (text, "sparse", "BM25"))]
            result = c.hybrid_search(collection, reqs=requests, ranker=RRFRanker(),
                limit=settings.recall_top_k, output_fields=mc.V2_OUTPUT, consistency_level="Strong")
        return [mc._hit(h) for h in result[0]]

    hits = await mc.acall(work)
    if strategy != "hybrid_rerank" or not hits:
        return hits[:top_k]
    ranked = await rerank.rerank(query, [f"{h['question']} {h['answer']}" for h in hits], top_n=top_k)
    return [{**hits[index], "rerank_score": score} for index, score in ranked]
