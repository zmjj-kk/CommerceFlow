"""Validate a migrated v2 index; --canary also calls configured embedding/rerank APIs."""
import argparse
import asyncio
import json
from pathlib import Path

from sqlalchemy import func, select

from app.config import settings
import app.db.base as db
from app.db import kb_v2_repository as repo
from app.db.kb_v2_models import KBDocument, KBSourceChunk, KBVersion
from app.db.models import KnowledgeChunk
from app.core import retrieval
from app.kb import milvus_client as mc


async def main(args):
    if args.canary and settings.kb_read_model != "v2":
        raise ValueError("run --canary in a dedicated process with KB_READ_MODEL=v2")
    collection = settings.kb_v2_collection
    active_ids = await repo.active_chunk_ids(collection)
    async with db.async_session() as session:
        counts = {model.__tablename__: await session.scalar(select(func.count()).select_from(model))
                  for model in (KBDocument, KBVersion, KBSourceChunk, KnowledgeChunk)}

    def inspect():
        client = mc.get_client()
        mc.ensure_collection(client, collection, v2=True)
        rows = client.query(collection, filter="id >= 0", output_fields=["id", *mc.V2_OUTPUT],
                            limit=16384, consistency_level="Strong")
        return rows, mc.count(client, "knowledge")

    rows, legacy_vectors = await mc.acall(inspect)
    indexed = {r["id"]: r for r in rows}
    if not active_ids or not set(active_ids).issubset(indexed):
        raise RuntimeError("active source chunks missing from index")
    for row_id in active_ids:
        hit = indexed[row_id]
        source = await repo.get_chunk(hit["chunk_id"])
        if source is None:
            raise RuntimeError("indexed chunk has no source")
        if any(hit[k] != source["chunk"][k] for k in ("document_id", "version_id", "content", "chunk_index")):
            raise RuntimeError("index/source provenance mismatch")
        if hit["source_uri"] != source["document"]["source_uri"]:
            raise RuntimeError("source URI mismatch")

    result = {"collection": collection, "read_model": settings.kb_read_model,
              "counts": counts, "active_chunks": len(active_ids),
              "indexed_rows": len(rows), "legacy_vectors": legacy_vectors,
              "source_checks_passed": len(active_ids), "canary": []}
    if args.canary:
        samples = [json.loads(line) for line in Path("tests/data/eval_ch04.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        chosen = [next(s for s in samples if s["bucket"] == bucket)
                  for bucket in ("A_policy", "B_model", "C_colloquial", "E_multi")]
        for sample in chosen:
            for strategy in ("vector", "bm25", "hybrid", "hybrid_rerank"):
                # Exercise the actual read-model switch instead of forcing the collection.
                hits = await retrieval.search_knowledge(sample["query"], strategy, top_k=3)
                if not hits or any(h["id"] not in active_ids for h in hits):
                    raise RuntimeError("canary returned no evidence or an inactive version")
                for hit in hits:
                    source = await repo.get_chunk(hit["chunk_id"])
                    if source["chunk"]["content"] != hit["answer"] or source["document"]["version_id"] != hit["version_id"]:
                        raise RuntimeError("canary citation mismatch")
                result["canary"].append({"sample_id": sample["id"], "strategy": strategy,
                                          "chunk_ids": [h["chunk_id"] for h in hits]})
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "canary"}, ensure_ascii=False))
    print(f"canary checks: {len(result['canary'])}; report: {target}")
    await db.engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--canary", action="store_true")
    parser.add_argument("--output", default=".rag-upgrade-work/v2-validation.json")
    asyncio.run(main(parser.parse_args()))
