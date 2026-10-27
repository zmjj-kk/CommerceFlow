"""部署验收：检查智谱向量是否能经 Milvus 混合召回（不依赖 Rerank）。"""
import asyncio
from app.core import retrieval
from app.config import settings
from app.kb import milvus_client


async def main():
    print(f"Embedding: {settings.embed_model}, dim={settings.embed_dim}")
    for strategy in ("bm25", "vector", "hybrid"):
        hits = await retrieval.search_knowledge("退货运费由谁承担", strategy=strategy, top_k=3, split=False)
        print(f"{strategy}: {len(hits)} hits, ids={[h['id'] for h in hits]}")
        if not hits:
            raise RuntimeError(f"{strategy} returned no hits")
    c = milvus_client.get_client()
    print("milvus_vector_count:", milvus_client.count(c))


if __name__ == "__main__":
    asyncio.run(main())
