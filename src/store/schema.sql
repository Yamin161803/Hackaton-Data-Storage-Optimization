-- Corpus document store (Tier 1)
-- Extends the existing database (image / image_text / keyword tables) with a
-- document + page + manifest layer for PDFs and slide decks (PPTX).
--
-- Design principles (see README / design discussion):
--   * Identity is CONTENT, never filename.
--   * Documents are stored as an ORDERED LIST OF PAGE HASHES (a manifest), not
--     as page content. Pages are content-addressed and deduplicated, so a deck
--     that shares 48/50 pages with another only adds 2 new page rows.
--   * A document-level fingerprint (size -> quick_hash -> full_hash, matching
--     src/videohash.py) gives a fast exact-duplicate gate.
--   * An inverted index (page_hash -> document) makes "which documents share
--     this page?" fast, so near-version detection only diffs candidate docs
--     instead of scanning every document.
--
-- No overlap threshold is used anywhere: shared start/end slides dedup
-- individually as pages and never force a (false) document merge.

PRAGMA foreign_keys = ON;

-- One row per ingested document (PDF or slide deck).
-- Exact-duplicate discard happens BEFORE insert via the fingerprint, so every
-- row here is a distinct file by content.
CREATE TABLE IF NOT EXISTS document (
  id            INTEGER PRIMARY KEY,            -- first 8 bytes of full_hash as int64 (like videohash.video_id)
  full_hash     TEXT    NOT NULL UNIQUE,        -- SHA-256 of whole file (exact-dup key)
  quick_hash    TEXT,                           -- SHA-256 of size + first/last 1 MiB (fast pre-check)
  file_size     INTEGER NOT NULL,               -- bytes (cheapest pre-check)
  source_type   INTEGER NOT NULL,               -- 1=pdf, 2=pptx
  source_name   TEXT,                           -- original filename (metadata/hint only, NOT identity)
  title         TEXT,                           -- document title if known
  page_count    INTEGER NOT NULL DEFAULT 0,     -- number of pages/slides in the manifest
  created_at    TEXT    NOT NULL                -- ISO-8601 ingest time
);

-- One row per UNIQUE page/slide, content-addressed. Shared pages across
-- documents exist once here. Text lives inline for search; a page may also
-- reference an image blob in the existing `image` table via image_id.
--
-- A page has TWO sub-identities so the two source types can be handled right:
--   * text_hash  -- hash of the page's text. For PPTX this is the SPEAKER NOTES;
--                    for PDF it is the page's extractable text.
--   * image_hash -- hash of the page's picture(s). For PPTX the slide picture;
--                    for PDF the combined image(s).
-- The combined `hash` (PRIMARY KEY) is the page's overall identity:
--   * PDF : hash(text + images) combined  -> text and image move together.
--   * PPTX: still unique per (image_hash, text_hash) pair, but we keep the two
--           sub-hashes separate so "same picture, rewritten notes" is detected
--           as a visual-duplicate / reworded slide via image_hash alone.
CREATE TABLE IF NOT EXISTS page (
  hash        TEXT    PRIMARY KEY,              -- overall page identity (see above)
  text_hash   TEXT,                             -- hash of text (PPTX: speaker notes; PDF: page text)
  image_hash  TEXT,                             -- hash of the picture(s) (slide/page image set)
  text        TEXT,                             -- the text itself (notes for PPTX, page text for PDF) for search/export
  image_id    INTEGER,                          -- optional: rendered page image id in the existing image() table
                                                -- (kept as a plain column, not a hard FK, so this store works both
                                                --  standalone and against the shared database without requiring image())
  byte_size   INTEGER NOT NULL DEFAULT 0        -- size of the stored text/content
);

-- Find slides that share a picture (PPTX visual-duplicate detection).
CREATE INDEX IF NOT EXISTS idx_page_image_hash ON page(image_hash);

-- The MANIFEST: the ordered list of pages that make up a document.
-- (document_id, position) is the order; page_hash is the content reference.
-- This is also the INVERTED INDEX: idx_document_page_hash lets us ask
-- "which documents contain this page?" in O(log n).
CREATE TABLE IF NOT EXISTS document_page (
  document_id  INTEGER NOT NULL REFERENCES document(id) ON DELETE CASCADE,
  position     INTEGER NOT NULL,                -- 0-based order within the document
  page_hash    TEXT    NOT NULL REFERENCES page(hash) ON DELETE RESTRICT,
  PRIMARY KEY (document_id, position)
) WITHOUT ROWID;

-- Inverted index: page -> documents that contain it (fast version candidates).
CREATE INDEX IF NOT EXISTS idx_document_page_hash ON document_page(page_hash, document_id);

-- Metadata lookups.
CREATE INDEX IF NOT EXISTS idx_document_source_name ON document(source_name);
CREATE INDEX IF NOT EXISTS idx_document_created_at  ON document(created_at);

-- Optional: version lineage edges (Tier 2, designed-for but populated later).
-- Kept in the schema from day one so adding lineage needs no migration.
CREATE TABLE IF NOT EXISTS document_edge (
  parent_id   INTEGER NOT NULL REFERENCES document(id) ON DELETE CASCADE,
  child_id    INTEGER NOT NULL REFERENCES document(id) ON DELETE CASCADE,
  relation    INTEGER NOT NULL,                 -- 1=extends (superset), 2=shortened (subset), 3=reordered, 4=modified
  added        INTEGER NOT NULL DEFAULT 0,      -- pages present in child but not parent
  removed      INTEGER NOT NULL DEFAULT 0,      -- pages present in parent but not child
  PRIMARY KEY (parent_id, child_id)
) WITHOUT ROWID;

-- Full-text search over page text (keyword search at scale without scanning files).
-- Standard FTS5 (not contentless): the `hash` column is stored and retrievable,
-- so search results can be joined back to pages/documents.
CREATE VIRTUAL TABLE IF NOT EXISTS page_fts USING fts5(
  text,
  hash UNINDEXED
);
