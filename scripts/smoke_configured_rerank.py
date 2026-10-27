"""验证智谱 Rerank 配置和混合检索，不显示 API Key。"""
import asyncio
from app.config import settings
from app.core import rerank, retrieval

async def main():
    print("model:", settings.rerank_model)
    print("base_url:", settings.rerank_base_url)
    print("same_key_as_embedding:", settings.rerank_api_key == settings.embed_api_key)
    docs = ["退货时运费原则上由买家承担", "智能猫砂盆提供自动清理功能"]
    ranked = await rerank.rerank("退货运费由谁承担", docs, top_n=2)
    print("rerank_results:", ranked)
    assert len(ranked) == 2
    assert sorted(i for i, _ in ranked) == [0, 1]
    hits = await retrieval.search_knowledge("退货运费由谁承担", strategy="hybrid_rerank", top_k=3, split=False)
    print("hybrid_rerank_hits:", len(hits))
    print("hit_ids_scores:", [(h["id"], round(h["rerank_score"], 4)) for h in hits])
    assert hits and all("rerank_score" in h for h in hits)
    print("RERANK_AND_RAG_OK")

if __name__ == "__main__":
    asyncio.run(main())
