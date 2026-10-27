-- Additive migration only. The legacy knowledge_chunks table and collection are retained.
CREATE TABLE IF NOT EXISTS kb_documents (
    document_id VARCHAR(64) PRIMARY KEY,
    source_uri TEXT NOT NULL,
    desired_version_id VARCHAR(64) NULL,
    deleted INT NOT NULL DEFAULT 0,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) CHARACTER SET utf8mb4;

CREATE TABLE IF NOT EXISTS kb_document_versions (
    document_id VARCHAR(64) NOT NULL,
    version_id VARCHAR(64) NOT NULL,
    data JSON NOT NULL,
    chunk_count INT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (document_id, version_id),
    FOREIGN KEY (document_id) REFERENCES kb_documents(document_id)
) CHARACTER SET utf8mb4;

CREATE TABLE IF NOT EXISTS kb_source_chunks (
    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    chunk_id VARCHAR(64) NOT NULL UNIQUE,
    document_id VARCHAR(64) NOT NULL,
    version_id VARCHAR(64) NOT NULL,
    chunk_index INT NOT NULL,
    content TEXT NOT NULL,
    data JSON NOT NULL,
    prev_chunk_id VARCHAR(64) NULL,
    next_chunk_id VARCHAR(64) NULL,
    UNIQUE (document_id, version_id, chunk_index),
    INDEX ix_kb_source_chunks_document_id (document_id),
    FOREIGN KEY (document_id) REFERENCES kb_documents(document_id),
    FOREIGN KEY (document_id, version_id) REFERENCES kb_document_versions(document_id, version_id)
) CHARACTER SET utf8mb4;

CREATE TABLE IF NOT EXISTS kb_index_states (
    collection VARCHAR(128) NOT NULL,
    chunk_id VARCHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    PRIMARY KEY (collection, chunk_id),
    FOREIGN KEY (chunk_id) REFERENCES kb_source_chunks(chunk_id)
) CHARACTER SET utf8mb4;

CREATE TABLE IF NOT EXISTS kb_active_versions (
    collection VARCHAR(128) NOT NULL,
    document_id VARCHAR(64) NOT NULL,
    version_id VARCHAR(64) NOT NULL,
    PRIMARY KEY (collection, document_id),
    FOREIGN KEY (document_id) REFERENCES kb_documents(document_id),
    FOREIGN KEY (document_id, version_id) REFERENCES kb_document_versions(document_id, version_id)
) CHARACTER SET utf8mb4;
