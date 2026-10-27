-- Migration 067: AI knowledge sources and support ticket assistant
-- Adds knowledge source indexing, vector chunk storage, and AI-generated
-- ticket suggestions with citation support.

-- Enable pgvector extension (must come before table creation that uses vector type).
-- Ensure-extension-first, but do not hard-fail a fresh replay on a stock Postgres
-- image that does not ship pgvector (e.g. postgres:16/17-bookworm): in that case
-- warn loudly and fall back to a plain REAL[] embedding column with no ANN index.
-- Installing pgvector later and converting the column is a separate, explicit step.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector') THEN
    CREATE EXTENSION IF NOT EXISTS vector;
  ELSE
    RAISE WARNING 'pgvector is not available in this Postgres image: '
      'ai_knowledge_chunks.embedding will be REAL[] and the ivfflat index is skipped';
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS ai_knowledge_sources (
  id SERIAL PRIMARY KEY,
  community_id INTEGER REFERENCES communities(id) ON DELETE CASCADE,
  vendor_id INTEGER REFERENCES hub_users(id),
  module_id INTEGER REFERENCES approved_vendor_modules(id),
  source_name VARCHAR(255) NOT NULL,
  source_type VARCHAR(30) NOT NULL CHECK (source_type IN (
    'github_wiki',
    'github_repo_markdown',
    'mkdocs',
    'docusaurus',
    'generic_url',
    'manual'
  )),
  source_url TEXT,                        -- URL or repo path
  branch VARCHAR(100) DEFAULT 'main',
  docs_path VARCHAR(500) DEFAULT '/',
  refresh_interval VARCHAR(20) DEFAULT 'weekly' CHECK (refresh_interval IN (
    'daily',
    'weekly',
    'on_demand'
  )),
  encrypted_token TEXT,                   -- for private repos (AES-256-GCM)
  is_active BOOLEAN DEFAULT true,
  last_indexed_at TIMESTAMPTZ,
  indexed_page_count INTEGER DEFAULT 0,
  index_errors TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Lookup by community
CREATE INDEX IF NOT EXISTS idx_ai_knowledge_sources_community_id
  ON ai_knowledge_sources (community_id);

-- Lookup by vendor
CREATE INDEX IF NOT EXISTS idx_ai_knowledge_sources_vendor_id
  ON ai_knowledge_sources (vendor_id);

-- Lookup active sources for scheduler
CREATE INDEX IF NOT EXISTS idx_ai_knowledge_sources_active
  ON ai_knowledge_sources (is_active, refresh_interval)
  WHERE is_active = true;

DO $$
BEGIN
  EXECUTE format($sql$
    CREATE TABLE IF NOT EXISTS ai_knowledge_chunks (
      id SERIAL PRIMARY KEY,
      source_id INTEGER NOT NULL REFERENCES ai_knowledge_sources(id) ON DELETE CASCADE,
      content TEXT NOT NULL,
      content_hash VARCHAR(64) NOT NULL,      -- SHA-256 of content for dedup
      source_url TEXT,                        -- original page URL for citation
      source_title VARCHAR(500),
      chunk_index INTEGER NOT NULL DEFAULT 0, -- position within document
      embedding %s,                           -- pgvector (nomic-embed-text dimension 384) or REAL[] fallback
      token_count INTEGER,
      created_at TIMESTAMPTZ DEFAULT NOW(),
      updated_at TIMESTAMPTZ DEFAULT NOW()
    )$sql$,
    CASE WHEN EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')
         THEN 'vector(384)' ELSE 'REAL[]' END);
END $$;

-- Lookup chunks by source (for reindex / deletion)
CREATE INDEX IF NOT EXISTS idx_ai_knowledge_chunks_source_id
  ON ai_knowledge_chunks (source_id);

-- Dedup by content hash within a source
CREATE INDEX IF NOT EXISTS idx_ai_knowledge_chunks_content_hash
  ON ai_knowledge_chunks (source_id, content_hash);

-- Vector similarity search (IVFFlat with 100 lists; tune lists = sqrt(row_count))
-- Uses cosine distance to match nomic-embed-text normalised embeddings
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') THEN
    CREATE INDEX IF NOT EXISTS idx_ai_knowledge_chunks_embedding
      ON ai_knowledge_chunks USING ivfflat (embedding vector_cosine_ops)
      WITH (lists = 100);
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS ai_ticket_suggestions (
  id SERIAL PRIMARY KEY,
  ticket_id INTEGER NOT NULL,             -- references support_tickets(id)
  suggestion_text TEXT NOT NULL,
  confidence_score DECIMAL(4,3) NOT NULL, -- 0.000 to 1.000
  cited_chunks INTEGER[] DEFAULT '{}',    -- array of ai_knowledge_chunks IDs
  feedback VARCHAR(20) CHECK (feedback IN ('helpful', 'not_helpful', NULL)),
  is_auto_posted BOOLEAN DEFAULT false,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Lookup suggestions by ticket
CREATE INDEX IF NOT EXISTS idx_ai_ticket_suggestions_ticket_id
  ON ai_ticket_suggestions (ticket_id);

-- Lookup pending feedback for quality reporting
CREATE INDEX IF NOT EXISTS idx_ai_ticket_suggestions_feedback
  ON ai_ticket_suggestions (feedback, created_at DESC);
