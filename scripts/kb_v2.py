"""Additive v2 lifecycle CLI. Run with `python -m scripts.kb_v2 --help`."""
import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path

from sqlalchemy import select

from app.config import settings
import app.db.base as db
from app.db.models import KnowledgeChunk
from app.db.kb_v2_models import KBDocument, KBVersion, KBSourceChunk, KBIndexState, KBActiveVersion
from app.db import kb_v2_repository as repo
from app.kb import dualwrite, milvus_client as mc
from app.kb.domain import adapt_legacy, build_document
from app.kb.sources import KB_DIR, SOURCE_TYPES


def sources():
    for name, kind in SOURCE_TYPES.items():
        path = KB_DIR / name
        yield build_document(path.read_text(encoding="utf-8"), f"data/kb/{name}", document_type=kind)


async def main(args):
    collection = args.collection or settings.kb_v2_collection
    if not collection.startswith("knowledge_v2_"):
        raise ValueError("collection must start with knowledge_v2_")
    if args.action == "init":
        # Explicit invocation only; application startup never performs DDL.
        tables = [m.__table__ for m in (KBDocument, KBVersion, KBSourceChunk, KBIndexState, KBActiveVersion)]
        async with db.engine.begin() as connection:
            await connection.run_sync(lambda c: db.Base.metadata.create_all(c, tables=tables))
        await mc.acall(lambda: mc.ensure_collection(mc.get_client(), collection, v2=True))
    elif args.action == "build":
        for doc, chunks in sources():
            ids = await dualwrite.write_document(doc, chunks, collection=collection)
            print(f"{doc.source_uri}: {len(ids)} chunks, document={doc.document_id}, version={doc.version_id}")
    elif args.action == "vectorize":
        print(await dualwrite.vectorize_documents(collection=collection))
    elif args.action == "backfill-legacy":
        async with db.async_session() as session:
            query = select(KnowledgeChunk).order_by(KnowledgeChunk.id)
            if args.legacy_ids:
                query = query.where(KnowledgeChunk.id.in_(args.legacy_ids))
            rows = list(await session.scalars(query))
        for row in rows:
            doc, chunks = adapt_legacy(row)
            await dualwrite.write_document(doc, chunks, collection=collection)
        print(f"{len(rows)} legacy rows staged with unknown original source")
    elif args.action == "delete":
        if not args.document_id:
            raise ValueError("--document-id is required")
        print(await repo.delete_document(args.document_id))
    elif args.action == "snapshot":
        target = Path(args.output or f".rag-upgrade-work/{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}")
        target.mkdir(parents=True, exist_ok=False)
        async with db.async_session() as session:
            rows = list(await session.scalars(select(KnowledgeChunk).order_by(KnowledgeChunk.id)))
            legacy = [{c.name: getattr(row, c.name) for c in KnowledgeChunk.__table__.columns} for row in rows]
        (target / "legacy_chunks.json").write_text(json.dumps(legacy, ensure_ascii=False, default=str), encoding="utf-8")
        def inspect():
            c = mc.get_client()
            schema = c.describe_collection(mc.COLLECTION)
            indexes = {n: c.describe_index(mc.COLLECTION, n) for n in c.list_indexes(mc.COLLECTION)}
            return {"schema": schema, "indexes": indexes}
        manifest = {"milvus": await mc.acall(inspect),
                    "sources": [asdict(doc) for doc, _ in sources()],
                    "config": {k: getattr(settings, k) for k in
                               ("embed_model", "embed_dim", "rerank_model", "recall_top_k",
                                "rerank_top_k", "kb_read_model", "kb_v2_collection")}}
        (target / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, default=str, indent=2), encoding="utf-8")
        print(target)
    await db.engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["snapshot", "init", "build", "vectorize", "delete", "backfill-legacy"])
    parser.add_argument("--collection")
    parser.add_argument("--document-id")
    parser.add_argument("--output")
    parser.add_argument("--legacy-ids", type=int, nargs="+", help="explicit reviewed legacy row IDs to migrate")
    asyncio.run(main(parser.parse_args()))
