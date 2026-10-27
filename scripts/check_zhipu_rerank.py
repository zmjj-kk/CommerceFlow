"""用项目已配置的智谱凭证验证 rerank 接口；不打印密钥。"""
import asyncio
import httpx
from app.config import settings

async def main():
    url = "https://open.bigmodel.cn/api/paas/v4/rerank"
    payload = {
        "model": "rerank",
        "query": "退货运费由谁承担",
        "documents": ["七天无理由退货的运费由买家承担", "这款商品提供一年质保"],
        "top_n": 2,
    }
    try:
        async with httpx.AsyncClient(timeout=40) as client:
            response = await client.post(url, json=payload, headers={"Authorization": "Bearer " + settings.embed_api_key})
        print("http_status:", response.status_code)
        try:
            obj = response.json()
        except ValueError:
            print("response_is_json: false")
            return
        print("response_keys:", sorted(obj.keys()))
        if response.is_success:
            results = obj.get("results", [])
            print("results_count:", len(results))
            print("results_summary:", [{"index": r.get("index"), "score": r.get("relevance_score")} for r in results])
        else:
            error = obj.get("error")
            if isinstance(error, dict):
                print("error_code:", error.get("code"))
                print("error_type:", error.get("type"))
            else:
                print("error_code:", obj.get("code"))
    except Exception as e:
        print("request_failed:", type(e).__name__)

if __name__ == "__main__":
    asyncio.run(main())
