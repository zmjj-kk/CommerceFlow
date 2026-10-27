"""Source models and the single search projection used by the v2 index."""
from dataclasses import dataclass, field
import hashlib
import json

from app.kb import chunking
from app.kb.documents import KEY_TERMS, is_key

CHUNKING_VERSION = "markdown-v1"
FEATURE_RULE_VERSION = "key-terms-v1"


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Document:
    document_id: str
    version_id: str
    title: str
    source_uri: str
    content_hash: str
    category: str = ""
    document_type: str = "text"


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    document_id: str
    version_id: str
    content: str
    chunk_index: int
    section_path: tuple[str, ...] = ()
    content_type: str = "text"
    metadata: dict = field(default_factory=dict)
    features: dict = field(default_factory=dict)
    generated_questions: tuple[str, ...] = ()


def build_document(md: str, source_uri: str, *, title: str = "", category: str = "",
                   document_type: str = "text", chunk_size: int = 400,
                   overlap: int = 60, table_max_rows: int = 10) -> tuple[Document, list[Chunk]]:
    """The caller supplies source identity; Markdown can never overwrite it.

    Version identity includes the splitting recipe and trusted document metadata,
    so changing either requires a new immutable projection.
    """
    if not source_uri.strip():
        raise ValueError("source_uri is required; do not infer it from headings")
    if chunk_size <= 0 or overlap < 0 or table_max_rows <= 0:
        raise ValueError("invalid chunking parameters")
    sections = chunking.split_sections(md)
    title = title or next((s.metadata.get("h1") for s in sections if s.metadata.get("h1")), source_uri)
    content_hash = hashlib.sha256(md.encode()).hexdigest()
    document_id = fingerprint(source_uri)
    version_id = fingerprint([content_hash, title, category, document_type,
                              CHUNKING_VERSION, chunk_size, overlap, table_max_rows,
                              FEATURE_RULE_VERSION, KEY_TERMS])
    doc = Document(document_id, version_id, title, source_uri, content_hash, category, document_type)
    chunks = []
    for section in sections:
        path = tuple(section.metadata[k] for k in ("h1", "h2", "h3", "h4") if section.metadata.get(k))
        body = section.page_content.strip()
        if not body:
            continue
        table = chunking.is_table_block(body)
        pieces = (chunking.split_table_rows(body, table_max_rows) if table else
                  (chunking.apply_sentence_overlap(chunking.recursive_split(body, chunk_size), overlap)
                   if overlap else chunking.recursive_split(body, chunk_size)))
        for piece in pieces:
            index = len(chunks)
            chunks.append(Chunk(
                fingerprint([document_id, version_id, index, piece]), document_id,
                version_id, piece, index, path, "table" if table else "text",
                features={"is_key_clause": is_key(path[-1] if path else title, piece),
                          "rule_version": FEATURE_RULE_VERSION},
            ))
    return doc, chunks


def build_search_text(chunk: Chunk, doc: Document) -> str:
    return "\n".join(x for x in (doc.category, " / ".join(chunk.section_path),
                                 " ".join(chunk.generated_questions), chunk.content) if x)


def search_record(chunk: Chunk, doc: Document, row_id: int) -> dict:
    if (chunk.document_id, chunk.version_id) != (doc.document_id, doc.version_id):
        raise ValueError("chunk/document version mismatch")
    return {"id": row_id, "chunk_id": chunk.chunk_id, "document_id": doc.document_id,
            "version_id": doc.version_id, "chunk_index": chunk.chunk_index,
            "content": chunk.content, "text": build_search_text(chunk, doc),
            "question": " / ".join(chunk.section_path) or doc.title, "answer": chunk.content,
            "section_path": " / ".join(chunk.section_path), "content_type": chunk.content_type,
            "category": doc.category, "source_uri": doc.source_uri, "title": doc.title,
            "is_key_clause": chunk.features.get("is_key_clause", 0),
            "source_status": chunk.metadata.get("source_status", "known")}


def adapt_legacy(row) -> tuple[Document, list[Chunk]]:
    """Reference the actual DB row; never claim to recover its original source file."""
    uri = f"legacy://knowledge_chunks/{row.id}"
    document_id = fingerprint(uri)
    document_type = getattr(row, "content_type", None) or "legacy"
    version_id = fingerprint([row.questions, row.answer, row.category, row.section_path,
                              row.is_key_clause, document_type])
    doc = Document(document_id, version_id, row.questions, uri,
                   hashlib.sha256(row.answer.encode()).hexdigest(), row.category, document_type)
    chunk = Chunk(fingerprint([document_id, version_id, 0]), document_id, version_id,
                  row.answer, 0, tuple((row.section_path or "").split(" / ")) if row.section_path else (),
                  "table" if chunking.is_table_block(row.answer) else "text",
                  metadata={"source_status": "legacy_unknown_source", "legacy_id": str(row.id)},
                  features={"is_key_clause": row.is_key_clause, "rule_version": "legacy"})
    return doc, [chunk]


def chunk_from_dict(data: dict) -> Chunk:
    data = dict(data)
    for key in ("section_path", "generated_questions"):
        data[key] = tuple(data.get(key, ()))
    return Chunk(**data)


def citation(hit: dict, number: int) -> dict:
    result = {"n": number, **{k: hit[k] for k in
              ("id", "section_path", "question", "answer", "content_type")}}
    result.update({k: hit[k] for k in ("chunk_id", "document_id", "version_id", "content",
                   "source_uri", "title", "chunk_index", "source_status") if k in hit})
    return result
