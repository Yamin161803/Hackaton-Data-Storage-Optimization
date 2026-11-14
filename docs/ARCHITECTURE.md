# Architecture & Design

Technical documentation of the data-storage-optimization system: the models,
the key engineering decisions, the data structures, and the exact mechanics of
the PDF/PPTX breakdown. Three parts:

1. **Voice-to-text with speaker identification**
2. **The data structure** — why it is scalable, stable, and searchable (for
   future search features and a knowledge-base AI)
3. **PDF/PPTX breakdown** — hash crafting, storage, picture removal and exact
   reassembly, and why it saves storage

---

## Part 1 — Voice-to-text with speaker identification

### Models and libraries

| Concern | Model / library | Notes |
|---|---|---|
| Transcription (ASR) | **faster-whisper** (`WhisperModel`), model `large-v3` | CTranslate2 reimplementation of OpenAI Whisper; ~4× faster, lower memory. |
| Speaker diarization | **pyannote.audio** v4 `Pipeline`, `pyannote/speaker-diarization-community-1` | "who spoke when"; gated model (one-time HF terms acceptance + token). |
| Audio I/O | **soundfile** (libsndfile) | load audio in-memory as a tensor; avoids torchcodec/CUDA-lib breakage. |
| Decode / frames | **ffmpeg** (imageio-ffmpeg bundled 9.x) | version-checked: only a system ffmpeg that supports `-fps_mode` (≥5.1) is used, else the bundled binary. |

### Device auto-detection (one code path, CPU and GPU)

`fw_device()` probes CUDA via `ctranslate2.get_cuda_device_count()`:

- **GPU present** → `device="cuda", compute_type="float16"` (production, e.g. RTX 3070 — `large-v3` in fp16 ≈ 5 GB VRAM, ~10–15× real-time).
- **No GPU** → `device="cpu", compute_type="int8"` (laptop testing, ~1× real-time).

The same code runs on a laptop and the GPU box with no edits. pyannote uses the GPU when PyTorch sees one, else CPU.

### The pipeline (`src/transcription/diarize.py`)

1. **Transcribe** with faster-whisper → text segments with `[start, end]` timestamps (VAD filtering on).
2. **Diarize** with pyannote → speaker *turns* `(start, end, speaker_label)`.
3. **Merge** — each transcript segment is assigned the speaker with the most **temporal overlap** (`assign_speaker()` picks `argmax` of `overlap(segment, turn)`), producing `SPEAKER_00 / SPEAKER_01 / …` labels on the transcript.

### Key decisions
- **faster-whisper `large-v3`** over reference Whisper: best accuracy/speed/VRAM trade-off; `BatchedInferencePipeline` is available for throughput on the GPU.
- **Diarization is a separate model**, not part of Whisper — transcription gives *what/when*, pyannote gives *who*; they are merged by timestamp overlap.
- **soundfile in-memory loading** instead of letting pyannote decode files — sidesteps a torchcodec/CUDA shared-library failure on CPU-only machines.
- **Videos are discarded after processing** by design: a video becomes transcript + sampled frames; the raw video is not stored (a separate pipeline from the PDF/PPTX store in Parts 2–3).

---

## Part 2 — The data structure (scalable, stable, searchable)

The store (`src/store/`, SQLite) is a **content-addressed document store** with a
page/slide-level dedup layer. It is designed so the corpus stays cheap to grow,
safe to query, and ready to feed search + a knowledge-base AI.

### Core tables (`src/store/schema.sql`)

- **`document`** — one row per ingested file. Holds the fingerprint
  (`full_hash`, `quick_hash`, `file_size`), a regeneration-stable
  `content_hash`, `source_type` (pdf/pptx), `source_name`, `page_count`,
  `created_at`.
- **`page`** — one row per **unique** page/slide, content-addressed by `hash`
  with sub-identities `text_hash` and `image_hash`, plus the extracted `text`.
- **`document_page`** — the **manifest**: the ordered list `(document_id,
  position, page_hash)`. A document stores *references*, never page content.
- **`document_edge`** — version lineage (a DAG): `relation`
  (extends/shortened/reordered/modified) + `added`/`removed`/`shared` page
  counts.
- **`page_fts`** — an **FTS5** virtual table over page text for keyword search.
- **`image` / `image_hash` / `page_image`** — the shared image object store and
  page→image references (see Part 3).
- **`document_skeleton`** — the exact-restore blueprint per document (Part 3).

### Identity = content, never filename

Hashing (`store.py`), all SHA-256:
- `hash_text(text)` = SHA-256 of **normalized** text (`normalize_text`: collapse
  whitespace, drop page-number/footer lines) — robust to re-export and
  re-pagination.
- `hash_images([…])` = SHA-256 of the sorted, de-duplicated set of image hashes.
- `combined_hash(text_hash, image_hash)` = a page's overall identity.
- `document.id` = first 8 bytes of `full_hash` as a signed int64
  (`_id_from_hash`), a stable `INTEGER PRIMARY KEY`.
- `content_hash` = SHA-256 of the **ordered manifest of page hashes** —
  identical for a file and any content-faithful regeneration of it.

### What makes it scalable
- **Page/slide deduplication.** A document is a manifest of page-hash
  references; a 50-slide deck that shares 48 slides with another only adds 2 new
  `page` rows. Storage grows with *unique* content, not file count.
- **Inverted index** `idx_document_page_hash (page_hash, document_id)`: "which
  documents contain this page?" is O(log n). Version/relationship detection only
  diffs *candidate* documents that share ≥1 page, never a full scan.
- **Sharded content-addressed blob store** (images on disk under
  `objects/ab/cd/<sha256>`): any directory stays small even at millions of
  objects.
- **SQLite** keeps the whole index in one portable file — trivial to snapshot
  and copy (fits the "image the drive, analyse offline" model).

### What makes it stable
- **Two dedup gates on ingest:** (1) `full_hash` exact byte-duplicate → discard;
  (2) `content_hash` same-pages-different-bytes → `content_duplicate`. A
  regenerated-then-re-uploaded file is recognized, so nothing is double-stored.
- **Family threshold.** Two documents merge into one version family only when
  they share **≥3 pages** (`document_edge.shared`); sharing 1–2 pages is a *weak
  link* (a bridge), so identical title/closing slides don't wrongly merge
  unrelated decks.
- **No overlap-percentage heuristic** for identity — everything keys on exact
  content hashes; lineage is set-difference, not a fuzzy threshold.

### What makes it searchable (future search + KB AI)
- **FTS5 keyword search** over `page_fts` with ranking + snippets, joined back
  to pages/documents — fast keyword retrieval without scanning files.
- **Per-page text is stored and content-addressed**, so it can be exported as a
  clean corpus (one record per unique page, with source metadata) and indexed
  into a vector/semantic knowledge base later. The schema already separates
  *identity* (hashes) from *content* (text + referenced images), which is
  exactly the shape a RAG/embeddings layer needs.
- **Metadata columns** (`source_name`, `created_at`, `source_type`) are indexed
  for sort/filter. A semantic (embeddings) layer is a planned bolt-on: add a
  `page_vector` table keyed by `page.hash` — no migration of existing data.

---

## Part 3 — PDF / PPTX breakdown: hashes, storage, picture removal & exact reassembly

Goal: store files so they can be restored **pixel-identical** (PDF) /
**byte-identical** (PPTX), while pulling pictures out into a shared, compressed,
deduplicated store. We never "re-create" a file from extracted text — we keep
the original skeleton and swap images out and back in.

### Hash crafting (fingerprint gate)

`src/videohash.py` defines a three-tier fingerprint, cheapest first:
1. **`file_size`** — one `stat()`, free.
2. **`quick_hash`** = SHA-256 of `size + first 1 MiB + last 1 MiB`
   (`QUICK_HASH_METHOD = "sha256-size-first-last-1MiB"`) — reads ≤2 MiB, catches
   most non-duplicates instantly.
3. **`full_hash`** = SHA-256 of the whole file — exact confirmation.

Page-level hashing (Part 2) then keys on normalized content so re-exports match.

### The shared image object store (`src/store/image_store.py`)

- **Content-addressed**, keyed by the SHA-256 of the original image bytes.
  `put(data)` stores a picture **once**; 100 PDFs using the same logo keep a
  single blob.
- **Lossless compression**: the blob is `zlib.compress(level=9)`'d; if smaller
  it is kept (`codec="zlib"`), else stored raw (`codec="raw"`). A `.meta`
  sidecar records how to restore.
- `get_original(hash)` returns bytes **identical** to what was put in — this
  lossless round-trip is what makes restore exact.
- Sharded on disk (`ab/cd/<hash>.blob`).

### PDF breakdown — in-place image-XObject substitution (`pdf_skeleton.py`, pikepdf)

**Store (`store_pdf`):**
1. Open the original PDF with **pikepdf**.
2. Walk every image XObject via `_iter_image_xobjects` — it iterates page
   `/Resources /XObject` **and follows each image's `/SMask`** (soft mask), which
   is itself an image stream.
3. For each image: read the exact **raw compressed stream bytes**
   (`obj.read_raw_bytes()`), store them in the ImageStore, and record the
   **full `/Filter` chain** (e.g. `[/ASCII85Decode, /FlateDecode]`) — the whole
   chain, not just the first filter.
4. **Blank** the stream in place (`obj.write(b"")`), keeping the object's dict.
5. `pdf.save()` → the **skeleton PDF** (original minus image bytes) + a JSON map.

**Restore (`restore_pdf`):**
1. Open the skeleton PDF, walk image XObjects in the **same order**.
2. Write the exact original bytes back with the original filter chain
   (`obj.write(raw, filter=…)`).
3. `pdf.save()` → renders **pixel-identical** to the original (verified by
   rendering pages with pymupdf and comparing pixel hashes).

**Two bugs this design had to solve (documented so they stay fixed):**
- **pikepdf renumbers objects on `save()`** — so images are matched by
  **walk-order index**, not by `objgen` (which changes).
- **Filter chains** — restoring only the first `/Filter` corrupts images encoded
  with a chain (real-world PDFs use `[ASCII85Decode, FlateDecode]`); the **full
  filter list** is captured before blanking and restored.

### PPTX breakdown — ZIP media substitution

A `.pptx` is a ZIP. `pptx_skeleton.store_pptx` keeps the full archive structure
and every entry (order, timestamps, compression), pulling each `ppt/media/*`
image into the ImageStore by reference. `restore_pptx` rewrites the ZIP with the
exact original bytes → **byte-identical** original.

### Putting it back together (reassembly)

`document_skeleton` stores, per document: `kind` (pdf/pptx), the
`skeleton_json` (image/entry map), and for PDF the `skeleton_blob` (the
blanked-PDF bytes). `Store.restore_original(document_id)` reads the skeleton,
pulls each referenced image back from the shared ImageStore via
`get_original()` (lossless), and reassembles — PDF pixel-identical, PPTX
byte-identical. The website's download endpoint uses this path.

### Why it saves storage
- **Pictures are the bulk of PDF/PPTX size.** Extracting them into a shared
  content-addressed store means an image used by N documents (or N slides) is
  stored **once**, not N times.
- **Lossless zlib recompression** shrinks each stored blob further, with exact
  pixels on restore.
- **The skeleton is tiny** — the original file minus its image streams (often
  MBs → KBs). The document costs ≈ skeleton + its *unique* images; shared images
  are amortized across every document that references them.
- Net effect measured on real data: a family of merged report PDFs dropped from
  ~18 MB "stored separately" to ~4 MB actually stored (shared pages/images once),
  with exact restore preserved. Per-version cost is reported with **fair-share
  accounting**: a shared slide of size S used by k versions charges each version
  `S / k`, so the per-version numbers sum to the true stored bytes.
