"""Transactional source/version lifecycle and per-collection indexing checkpoints."""
from dataclasses import asdict

from sqlalchemy import and_, delete, func, select
from sqlalchemy.exc import IntegrityError

import app.db.base as db
from app.db.kb_v2_models import KBDocument, KBVersion, KBSourceChunk, KBIndexState, KBActiveVersion
from app.kb.domain import Chunk, Document, chunk_from_dict


async def stage_document(doc: Document, chunks: list[Chunk], collection: str) -> list[int]:
    if not chunks:
        raise ValueError("empty documents cannot replace an active version; use delete")
    for i, chunk in enumerate(chunks):
        if (chunk.document_id, chunk.version_id, chunk.chunk_index) != (doc.document_id, doc.version_id, i):
            raise ValueError("chunks must be an ordered, complete document version")
    # Unique constraints arbitrate concurrent first imports. Retry the whole transaction.
    for attempt in range(3):
        try:
            async with db.async_session() as s, s.begin():
                source = await s.get(KBDocument, doc.document_id, with_for_update=True)
                if source is None:
                    source = KBDocument(document_id=doc.document_id, source_uri=doc.source_uri)
                    s.add(source)
                    await s.flush()
                source.desired_version_id = doc.version_id
                source.deleted = 0
                version = await s.get(KBVersion, (doc.document_id, doc.version_id))
                if version is None:
                    s.add(KBVersion(document_id=doc.document_id, version_id=doc.version_id,
                                    data=asdict(doc), chunk_count=len(chunks)))
                    await s.flush()
                    for i, c in enumerate(chunks):
                        s.add(KBSourceChunk(chunk_id=c.chunk_id, document_id=c.document_id,
                            version_id=c.version_id, chunk_index=i, content=c.content, data=asdict(c),
                            prev_chunk_id=chunks[i - 1].chunk_id if i else None,
                            next_chunk_id=chunks[i + 1].chunk_id if i + 1 < len(chunks) else None))
                    await s.flush()
                elif version.data != asdict(doc) or version.chunk_count != len(chunks):
                    raise ValueError("immutable version mismatch")
                rows = list((await s.scalars(select(KBSourceChunk).where(
                    KBSourceChunk.document_id == doc.document_id,
                    KBSourceChunk.version_id == doc.version_id).order_by(KBSourceChunk.chunk_index))).all())
                if [r.chunk_id for r in rows] != [c.chunk_id for c in chunks]:
                    raise ValueError("immutable chunk mapping mismatch")
                if any(chunk_from_dict(r.data) != c for r, c in zip(rows, chunks)):
                    raise ValueError("immutable chunk content mismatch")
                for row in rows:
                    if await s.get(KBIndexState, (collection, row.chunk_id)) is None:
                        s.add(KBIndexState(collection=collection, chunk_id=row.chunk_id))
                return [r.id for r in rows]
        except IntegrityError:
            if attempt == 2:
                raise


async def pending_records(collection: str):
    async with db.async_session() as s:
        return (await s.execute(select(KBSourceChunk, KBVersion).join(KBVersion, and_(
            KBSourceChunk.document_id == KBVersion.document_id,
            KBSourceChunk.version_id == KBVersion.version_id)).join(KBIndexState,
            KBIndexState.chunk_id == KBSourceChunk.chunk_id).join(KBDocument,
            KBDocument.document_id == KBSourceChunk.document_id).where(
            KBIndexState.collection == collection, KBIndexState.status == "pending",
            KBDocument.deleted == 0,
            KBDocument.desired_version_id == KBSourceChunk.version_id
        ).order_by(KBSourceChunk.id))).all()


async def mark_done(collection: str, chunk_ids: list[str]):
    async with db.async_session() as s, s.begin():
        states = await s.scalars(select(KBIndexState).where(
            KBIndexState.collection == collection, KBIndexState.chunk_id.in_(chunk_ids)))
        for state in states:
            state.status = "done"


async def activate_ready(collection: str) -> int:
    activated = 0
    async with db.async_session() as s, s.begin():
        sources = list(await s.scalars(select(KBDocument).where(KBDocument.deleted == 0).with_for_update()))
        for source in sources:
            version = await s.get(KBVersion, (source.document_id, source.desired_version_id))
            if version is None:
                continue
            ready = await s.scalar(select(func.count()).select_from(KBIndexState).join(
                KBSourceChunk, KBSourceChunk.chunk_id == KBIndexState.chunk_id).where(
                KBIndexState.collection == collection, KBIndexState.status == "done",
                KBSourceChunk.document_id == source.document_id,
                KBSourceChunk.version_id == version.version_id))
            if ready != version.chunk_count:
                continue
            active = await s.get(KBActiveVersion, (collection, source.document_id))
            if active is None:
                s.add(KBActiveVersion(collection=collection, document_id=source.document_id,
                                      version_id=version.version_id))
                activated += 1
            elif active.version_id != version.version_id:
                active.version_id = version.version_id
                activated += 1
    return activated


async def active_chunk_ids(collection: str) -> list[int]:
    """Snapshot of eligible primary keys, applied BEFORE Milvus recall/ranking."""
    async with db.async_session() as s:
        return list(await s.scalars(select(KBSourceChunk.id).join(KBActiveVersion, and_(
            KBActiveVersion.document_id == KBSourceChunk.document_id,
            KBActiveVersion.version_id == KBSourceChunk.version_id)).join(KBDocument,
            KBDocument.document_id == KBSourceChunk.document_id).where(
            KBActiveVersion.collection == collection, KBDocument.deleted == 0)))


async def delete_document(document_id: str) -> bool:
    # Keep source versions for historical citations; all collections stop recalling them.
    async with db.async_session() as s, s.begin():
        source = await s.get(KBDocument, document_id, with_for_update=True)
        if source is None:
            return False
        source.deleted = 1
        await s.execute(delete(KBActiveVersion).where(KBActiveVersion.document_id == document_id))
        return True


async def get_chunk(chunk_id: str) -> dict | None:
    async with db.async_session() as s:
        row = await s.scalar(select(KBSourceChunk).where(KBSourceChunk.chunk_id == chunk_id))
        if row is None:
            return None
        version = await s.get(KBVersion, (row.document_id, row.version_id))
        return {"chunk": row.data, "document": version.data,
                "prev_chunk_id": row.prev_chunk_id, "next_chunk_id": row.next_chunk_id}
