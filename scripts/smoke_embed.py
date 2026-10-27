# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com
"""冒烟:直连配置的 Embedding 上游,验证连通与向量维度。
密钥可通过 .env 的 COMMERCEFLOW_RAG_API_KEY 或 EMBED_API_KEY 提供。
运行:PYTHONPATH=. uv run python scripts/smoke_embed.py"""
import asyncio

from openai import AsyncOpenAI

from app.config import settings


async def main() -> None:
    client = AsyncOpenAI(base_url=settings.embed_base_url, api_key=settings.embed_api_key)
    resp = await client.embeddings.create(model=settings.embed_model, input=["邮费是多少", "运费怎么算"])
    dims = [len(d.embedding) for d in resp.data]
    print(f"返回 {len(resp.data)} 条向量,维度={dims}")
    assert len(dims) == 2 and all(dim == settings.embed_dim for dim in dims), f"期望维度 {settings.embed_dim},实际 {dims}"
    print(f"✅ 冒烟通过:{settings.embed_model},维度 {settings.embed_dim}")


if __name__ == "__main__":
    asyncio.run(main())
