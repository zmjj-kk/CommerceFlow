"""Exercise the local v2 server with real authentication and four retrieval strategies.

Credentials and tokens stay on this machine; query/document content is sent only
through the server's configured retrieval providers when vector/rerank is selected.
"""
import argparse
import asyncio
import json
from pathlib import Path
import time
from urllib.parse import urlparse

import httpx


async def main(args):
    parsed = urlparse(args.base_url)
    if parsed.hostname not in ("127.0.0.1", "localhost", "::1") or parsed.scheme not in ("http", "https"):
        raise ValueError("this credentialed canary only supports a loopback server")
    credentials = json.loads(Path(args.credentials).read_text(encoding="utf-8"))
    samples = [json.loads(line) for line in Path("tests/data/eval_ch04.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    chosen = [next(s for s in samples if s["bucket"] == bucket)
              for bucket in ("A_policy", "B_model", "C_colloquial", "E_multi")]
    report = {"url": args.base_url, "requests": [], "reference_checks": 0}
    async with httpx.AsyncClient(base_url=args.base_url, trust_env=False, timeout=90) as client:
        anonymous = await client.get("/api/kb/overview")
        if anonymous.status_code != 401:
            raise RuntimeError("anonymous admin access was not denied")
        response = await client.post("/api/auth/login", json={
            "username": credentials["username"], "password": credentials["password"]})
        if response.status_code != 200:
            raise RuntimeError(f"local login failed: HTTP {response.status_code}")
        headers = {"Authorization": "Bearer " + response.json()["access_token"]}
        try:
            overview = await client.get("/api/kb/overview", headers=headers)
            if overview.status_code != 200 or overview.json().get("read_model") != "v2":
                raise RuntimeError("canary server is not using v2")
            report["collection"] = overview.json()["read_collection"]
            for sample in chosen:
                for strategy in ("vector", "bm25", "hybrid", "hybrid_rerank"):
                    started = time.perf_counter()
                    response = await client.post("/api/kb/search", headers=headers, json={
                        "q": sample["query"], "strategy": strategy, "top_k": 3})
                    elapsed = round((time.perf_counter() - started) * 1000, 2)
                    if response.status_code != 200:
                        raise RuntimeError(f"{strategy} canary failed: HTTP {response.status_code}")
                    hits = response.json()["hits"]
                    if not hits:
                        raise RuntimeError("positive canary returned no evidence")
                    for hit in hits:
                        if not all(hit.get(key) for key in ("chunk_id", "document_id", "version_id", "content", "source_uri")):
                            raise RuntimeError("HTTP response lost source metadata")
                        source = await client.get("/api/kb/v2/chunks/" + hit["chunk_id"], headers=headers)
                        if source.status_code != 200:
                            raise RuntimeError("citation cannot locate original chunk")
                        data = source.json()
                        if (data["chunk"]["content"] != hit["answer"] or
                            hit["content"] != hit["answer"] or
                            data["document"]["document_id"] != hit["document_id"] or
                            data["document"]["version_id"] != hit["version_id"] or
                            data["document"]["source_uri"] != hit["source_uri"]):
                            raise RuntimeError("HTTP citation provenance mismatch")
                        report["reference_checks"] += 1
                    report["requests"].append({"sample_id": sample["id"], "strategy": strategy,
                                                "hits": len(hits), "latency_ms": elapsed})
        finally:
            logout = await client.post("/api/auth/logout", headers={"Origin": args.base_url})
            report["logout_status"] = logout.status_code
            if logout.status_code != 204:
                raise RuntimeError("local canary logout failed")
        revoked = await client.get("/api/auth/me", headers=headers)
        if revoked.status_code != 401:
            raise RuntimeError("logged-out session remained usable")
        report["revoked_session_denied"] = True
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"HTTP v2 canary passed: {len(report['requests'])} queries, {report['reference_checks']} citation checks; {target}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--credentials", default=".rag-upgrade-work/canary-admin.json")
    parser.add_argument("--output", default=".rag-upgrade-work/http-canary-full.json")
    asyncio.run(main(parser.parse_args()))
