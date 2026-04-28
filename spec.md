# Pliny — Personal Knowledge Base Aggregator Spec

## Overview

Pliny is a single-user knowledge management system that ingests anything (text, files, images, links, audio, video) via API or Telegram bot, processes it through an async pipeline, and stores it across specialized backends optimized for different query patterns. Named for Pliny the Elder, who tried to write down the entire natural world; the system is designed to scale with the user's knowledge over time, with all derived data rebuildable from canonical storage.

## Goals

- One endpoint accepts anything; the pipeline figures out what to do with it.
- Backend-first: the data layer is the product. UI is minimal and disposable.
- Everything derived is rebuildable. Embedding models, prompts, and graph schemas will change; the system must accommodate that without data loss.
- Designed for one user but architected so multi-user is a future addition, not a rewrite.

## Non-goals (v1)

- Auth beyond a single API key.
- Multi-user or sharing.
- Discord bot.
- Real-time collaboration or live editing of items.
- Mobile app.
- Algorithmic discovery feed ("reel"). The v1 frontend is search + filters only.

---

## Architectural Principles

1. **Postgres is the source of truth.** Neo4j, the vector tables, and RustFS are projections. Anything in them must be rebuildable from Postgres + raw files.
2. **Every processing stage is idempotent and resumable.** A worker can be killed at any point; restarting picks up where it left off.
3. **Versioning is mandatory.** Every derived artifact records which model/prompt/code version produced it. Reprocessing finds anything below the current version.
4. **Full overwrite on reprocess.** History is a separate feature, not a foundation. Old derived data is replaced when reprocessed (with one exception: embeddings, which are keyed by model so multiple can coexist during migration).
5. **Rebuild paths exist from day one.** The "drop and rebuild Neo4j from Postgres" command is built before there's meaningful data in either.

---

## Privacy / Data Flow

All extracted text, summaries, entity prompts, and image bytes are sent to OpenAI's hosted API. This is acceptable for v1's single-user scope. If privacy requirements change, the LLM abstraction (see below) is the swap point — the rest of the architecture is provider-agnostic.

---

## Stack

| Layer | Choice | Rationale |
|---|---|---|
| API | FastAPI (Python) | Async-friendly, fast iteration, ecosystem fit for LLM/embedding work |
| Canonical store | Postgres 15+ with `pgvector` | Source of truth, full-text search (tsvector), vector search (HNSW), `NULLS NOT DISTINCT` unique constraints |
| Graph store | Neo4j | Entity relationships, multi-hop traversal, future centrality/community detection |
| Object store | RustFS | S3-compatible, holds raw bytes and derived binary artifacts |
| LLM | OpenAI (`gpt-4o-mini` default for chat + vision) | Cheap, capable, single provider in v1; thin wrapper enables later swap |
| Embeddings | `text-embedding-3-small` (1536-dim) | Good quality/cost ratio. Model name + version stored on every row. |
| Browser automation | Playwright + SingleFile | Renders JS-heavy pages, produces self-contained archival HTML |
| Bot | Telegram | Native handling of photos, files, voice, links; cleaner bot API than Discord |
| Queue | Postgres-backed (`processing_jobs` table + `LISTEN/NOTIFY`) | No new infrastructure for v1; replaceable later |
| Frontend | React (minimal) | Search + filter sidebar; not the focus |

---

## LLM Provider Abstraction

A single Python module exposes the LLM surface used by the rest of the codebase:

```python
class LLM:
    def chat(messages, *, model, response_format=None, temperature=0) -> ChatResponse
    def embed(texts: list[str], *, model) -> list[Vector]
    def vision(image_bytes, prompt, *, model, response_format=None) -> ChatResponse
```

The v1 implementation wraps the OpenAI Python SDK directly. Call sites pass an explicit model name; the wrapper does not pick models. Prompt and model versions are recorded on the relevant derived rows so any change is detectable for reprocessing. Swapping providers in the future means writing one alternative implementation behind this interface; nothing else in the codebase imports the OpenAI SDK.

**Model pinning.** Snapshots are pinned, not aliases: `gpt-4o-mini-2024-07-18`, `text-embedding-3-small` (fixed snapshot). Bumping a model snapshot is a real version bump and triggers reprocess.

**Rate limiting.** The wrapper enforces a token bucket (RPM + TPM) before every call, with conservative Tier-1 defaults (~60 RPM / ~90k TPM equivalent), overridable via `OPENAI_RPM` / `OPENAI_TPM`. 429 responses parse `Retry-After` and feed into the standard job retry. A daily $ cap acts as a circuit breaker, persisted in a Postgres `llm_spend_daily (date pk, usd_spent numeric)` row updated transactionally per call (estimate = tokens × pinned price). Pre-call check raises `CostCapExceeded`, treated as a retryable error scheduled past midnight UTC; in-flight calls finish.

**Prompts.** Prompts live as Python constants per stage (e.g. `prompts/summarize.py` exporting `PROMPT` and `VERSION`). Editing the constant and bumping `VERSION` is the only way to change a prompt; git is the audit trail.

---

## Data Model (Postgres)

```sql
-- Canonical items
items (
  id                  uuid primary key,
  user_id             uuid,                     -- nullable in v1; v2 backfills + adds auth
  type                text not null,            -- text, url, image, pdf, audio, video, file
  captured_at         timestamptz not null default now(),  -- first capture
  content_hash        text not null,            -- see "Hashing" below
  canonical_url       text,                     -- url items only
  raw_ref             text,                     -- RustFS key of canonical raw bytes
  title               text,
  summary             text,
  metadata            jsonb,                    -- type-specific Pydantic-validated shape

  -- Per-stage versions. Reprocess query: WHERE <stage>_version < $current.
  -- items.status is intentionally absent — derive from these versions plus
  -- processing_jobs (see "Item Status" below).
  snapshot_version    int not null default 0,   -- url items only; 0 for non-url
  extract_version     int not null default 0,
  summarize_version   int not null default 0,
  chunk_version       int not null default 0,
  embed_version       int not null default 0,
  entities_version    int not null default 0,
  graph_sync_version  int not null default 0,

  unique (content_hash)
)

-- Hashing:
--   url items: content_hash = sha256(canonical_url); raw_ref points to SingleFile HTML
--               under raw/<sha256-of-html-bytes>.
--   all others: content_hash = sha256(raw bytes); raw_ref = raw/<content_hash>.

-- Every ingestion of an existing item appends a row here (e.g. same article forwarded twice).
-- A single ingest can produce multiple items sharing one source_ref (e.g. a Telegram
-- message containing text + a URL splits into a text item and a URL item).
item_sources (
  id           uuid primary key,
  item_id      uuid not null references items(id) on delete cascade,
  source       text not null,             -- telegram, api, ...
  source_ref   text,                      -- telegram message id, api request id, etc.
  captured_at  timestamptz not null default now()
)
-- Partial unique index for idempotent retries:
--   create unique index item_sources_source_ref_uniq
--     on item_sources (source, source_ref) where source_ref is not null;
-- Same Telegram message id retried after a network blip is a no-op.

-- Survivor pointers after a snapshot merge (see "URL Archival > Redirect collision").
-- A bot-issued item_id may resolve post-snapshot to a pre-existing item; we keep a
-- forwarding row so /items/:id and bot polling silently follow the merge.
item_redirects (
  from_id    uuid primary key,            -- the now-deleted item_id originally returned to the client
  to_id      uuid not null references items(id) on delete cascade,
  merged_at  timestamptz not null default now(),
  reason     text not null                -- 'redirect_collision' in v1
)

-- Extracted text (separated so re-extraction doesn't churn items)
content (
  item_id            uuid primary key references items(id) on delete cascade,
  extracted_text     text,
  language           text,
  extraction_method  text,
  extract_version    int not null,
  extracted_at       timestamptz not null default now(),
  tsv                tsvector             -- GIN index for BM25
)

-- Chunked text for retrieval
chunks (
  id              uuid primary key,
  item_id         uuid not null references items(id) on delete cascade,
  chunk_index     int not null,
  text            text not null,
  token_count     int not null,
  chunker_version int not null,
  unique (item_id, chunk_index, chunker_version)
)

-- Embeddings: one table per active vector dimension. Naming: embeddings_<dim>.
-- Migrating to a model with a different dimension creates a new table; the old
-- one is dropped after validation. Within a single table, multiple
-- (model_name, model_version) pairs can coexist on the same chunk/item.
embeddings_1536 (
  id            uuid primary key,
  item_id       uuid not null references items(id) on delete cascade,
  chunk_id      uuid references chunks(id) on delete cascade,  -- null = summary-level
  granularity   text not null,                  -- 'chunk' or 'summary'
  model_name    text not null,
  model_version text not null,
  vector        vector(1536) not null,
  embedded_at   timestamptz not null default now(),
  unique nulls not distinct
    (granularity, item_id, chunk_id, model_name, model_version)
)
-- HNSW index on each per-dimension table's `vector` column.

-- Entities (people, places, orgs, concepts, works)
entities (
  id              uuid primary key,
  canonical_name  text not null,
  type            text not null,
  aliases         jsonb,                  -- array of strings
  unique (canonical_name, type)
)

-- Item-to-entity mentions
item_entities (
  item_id           uuid references items(id) on delete cascade,
  entity_id         uuid references entities(id) on delete cascade,
  mention_text      text,
  confidence        real,
  entities_version  int not null,
  primary key (item_id, entity_id)
)

-- Flat tag system, separate from entities. Tags are free-form in v1.
tags (
  id   uuid primary key,
  name text not null unique
)
-- v1 only emits LLM tags; user-edited tags are out of scope. Reprocess wipes all rows.
-- The source column will return when a tag-edit UI ships.
item_tags (
  item_id uuid not null references items(id) on delete cascade,
  tag_id  uuid not null references tags(id) on delete cascade,
  primary key (item_id, tag_id)
)

-- Pipeline state and queue
processing_jobs (
  id              uuid primary key,
  item_id         uuid not null references items(id) on delete cascade,
  stage           text not null,         -- ingest, snapshot, wayback_fallback,
                                         -- extract, summarize, chunk, embed,
                                         -- entities, graph_sync
  pool            text not null,         -- 'fast' or 'slow'
  status          text not null,         -- pending, running, done, failed
  attempts        int not null default 0,
  error           text,
  next_attempt_at timestamptz,           -- backoff scheduling
  started_at      timestamptz,
  finished_at     timestamptz,
  claim_token     uuid,                  -- set on claim; final UPDATE checks equality
                                         -- so a reprocess-mid-flight clobber no-ops
  unique (item_id, stage)                -- one row per (item, stage), updated in place
)
```

### Indexes

- GIN on `content.tsv` for BM25.
- **Partial HNSW per `(granularity, model_name)`** on each `embeddings_<dim>` table:
  ```sql
  CREATE INDEX embeddings_1536_chunk_<model> ON embeddings_1536 USING hnsw (vector vector_cosine_ops)
    WHERE granularity = 'chunk'   AND model_name = '<model>';
  CREATE INDEX embeddings_1536_summary_<model> ON embeddings_1536 USING hnsw (vector vector_cosine_ops)
    WHERE granularity = 'summary' AND model_name = '<model>';
  ```
  One pair of indexes per active model. Filter-recall stays at 1.0 because each query targets a single partial index.
- B-tree on `items.captured_at` and on `items.<stage>_version` columns used by reprocess queries.
- B-tree on `processing_jobs (status, pool, next_attempt_at)` for worker claim queries.
- B-tree on `item_sources (item_id, captured_at)` for source history lookups.
- B-tree on `items.canonical_url` (partial, where `canonical_url is not null`) for URL lookups.
- B-tree on `items.user_id` (partial, where not null) — unused in v1, in place for v2.

---

## items.metadata Discipline

`items.metadata` is jsonb but **not free-form**. Each `items.type` has a Pydantic model in `schemas/metadata.py`; writers go through it. Common keys:

| Key | Set by | Notes |
|---|---|---|
| `forwarded_from` | telegram bot | `{ sender, original_chat, forwarded_at }` for forwarded messages |
| `source_url` | bot/api | When the original capture had a URL hint |
| `og` | snapshot | OpenGraph tags scraped from the page |
| `final_url` | snapshot | Post-redirect URL |
| `archive_source` | wayback fallback | `'wayback'`; `archive_timestamp` paired |
| `possible_duplicate_of` | image dedup | UUID of the candidate near-duplicate |
| `chunk_overflow` / `original_chunk_count` | chunker | Set when the 1000-chunk cap truncated |
| `paywalled` / `bot_challenge` | snapshot | Boolean flags for capture quality |
| `exif` | image extract | Selected EXIF fields (no GPS by default) |
| `media_host` | snapshot | For audio/video URLs: `youtube`, `vimeo`, `spotify`, `apple_podcasts`, `soundcloud`, ... |
| `duration_seconds` | snapshot / extract | Audio/video items |
| `channel` / `show` / `published_at` | snapshot | Media metadata when the source exposes it |
| `captions_available` | snapshot | Boolean; `true` when public captions were fetched into `extracted_text` |
| `thumbnail_url` | snapshot | Cached locally to `derived/<id>/thumbnail.jpg` |
| `mime` | ingest | For `file` items and direct media uploads, the detected mime type |
| `redirect_resolved_to` | snapshot | (rare) For diagnostics when merge skipped, points to the canonical-URL twin |

Readers tolerate missing keys. Adding a field means: add to the per-type model, ship, populate. Removing a field means: write a migration to clear it. Search filters that need a metadata key go through a typed accessor, not raw jsonb path queries scattered through the codebase.

## Storage Layout (RustFS)

Originals are immutable. Derived artifacts live under `derived/<item_id>/` and can be wiped and regenerated freely.

```
raw/<sha256-of-bytes>                   # canonical raw bytes:
                                        #   non-url items: the original payload
                                        #   url items:     the SingleFile HTML, or
                                        #                  the platform metadata JSON for media URLs
derived/<item_id>/screenshot.png        # full-page screenshot for HTML URLs
derived/<item_id>/extracted.txt         # cleaned text (also in Postgres; here for debugging)
derived/<item_id>/metadata.json         # fetch metadata, OG tags, final URL after redirects
derived/<item_id>/page_<n>.png          # rendered PDF pages, if needed
derived/<item_id>/thumbnail.jpg         # video/audio cover or poster, when the source provides one
derived/<item_id>/probe.json            # ffprobe output for direct audio/video uploads
```

For URL items, `items.content_hash` (the dedup key) is the SHA-256 of the canonical URL, while `items.raw_ref` points to `raw/<sha256-of-html-bytes>` — they are different hashes by construction. Re-snapshotting an existing URL overwrites the bytes at `raw_ref` (or writes a new one and updates `raw_ref`); the item identity is stable.

A `BlobStore` interface (`put`, `get`, `exists`, `url_for`) wraps RustFS so the implementation is swappable.

---

## Neo4j Schema

Neo4j stores only what Postgres can't query well: entity-to-entity relationships.

```
(:Item {id, title, captured_at, type})
(:Entity {id, canonical_name, type})

(:Item)-[:MENTIONS {confidence}]->(:Entity)
(:Entity)-[:RELATED_TO {weight, source}]->(:Entity)
```

- `MENTIONS` is a direct edge from extraction. Maintained incrementally on ingest.
- `RELATED_TO` edges come from two sources:
  - **Co-occurrence**: entities mentioned in the same item get a weighted edge. Computed in full rebuild.
  - **LLM-inferred**: during entity extraction, the model is asked for explicit relationships ("X authored Y", "A works at B"). Tagged with `source: 'llm'`.
- Edge `source` field is mandatory so they can be filtered or recomputed independently.

### Sync Strategy

**Incremental on ingest** for nodes and `MENTIONS` edges (the `graph_sync` pipeline stage). New items show up in the graph within seconds of ingestion.

**Full rebuild on demand** for `RELATED_TO` edges, schema changes, and recomputing co-occurrence weights globally. The rebuild script:

1. Reads `items`, `entities`, `item_entities` from Postgres.
2. Drops and recreates Neo4j.
3. Recomputes co-occurrence edges and weights.
4. Re-runs LLM-inferred relationships if extraction version has bumped.

The rebuild is the safety net for any drift, schema mistake, or extraction improvement.

---

## Processing Pipeline

Each stage is a row in `processing_jobs`. Workers poll for `status='pending' AND next_attempt_at <= now()` filtered by their pool, claim the row with `SELECT ... FOR UPDATE SKIP LOCKED`, run the stage, mark it done, enqueue the next stage. Failures bump `attempts`, set `error`, and schedule the next retry.

| Stage | Pool | Input | Output | Notes |
|---|---|---|---|---|
| `ingest` | fast | Raw payload | Hash, dedup check, raw written to RustFS, `items` row(s), `item_sources` row(s) | Synchronous from API. Telegram messages with text + URL split into two items here, sharing one `source_ref`. No network calls (URL hashing uses the as-submitted, normalized form; redirect resolution happens in `snapshot`). |
| `snapshot` | slow | URL items only | Per-classifier output (see URL Archival): SingleFile HTML for HTML pages, fetched PDF for PDFs, platform metadata JSON for recognized media hosts, ffprobe for direct media. Always sets `items.canonical_url` to the redirect-resolved URL and may merge into a pre-existing item. | Runs **before** `extract` for URL items so extraction reads stable bytes. |
| `extract` | fast | Item | `content.extracted_text` | Type-specific. `text` items: identity. `url`-typed HTML: trafilatura on the SingleFile HTML. `pdf`: pymupdf with vision fallback when extracted token count < threshold. `image`: `gpt-4o-mini` vision returns OCR'd text + caption (both stored). `audio` / `video`: pulls title, description, channel/show, captions when public, ID3/EXIF/ffprobe tags as available — no transcription. `file`: fails with `error='no_handler'`. |
| `summarize` | fast | Extracted text | `items.title`, `items.summary`, tag rows (with `source='llm'`) | LLM call, prompt versioned |
| `chunk` | fast | Extracted text | `chunks` rows | Fixed-size with overlap (512 tokens, 64 overlap). Tunable. |
| `embed` | fast | Chunks + summary | Rows in `embeddings_<dim>` | Skip chunks for short docs (see Embedding Strategy) |
| `entities` | fast | Extracted text + summary | `entities` and `item_entities` | LLM extraction with relationship hints |
| `graph_sync` | fast | Item + entities | Neo4j upserts (Item node, Entity nodes, MENTIONS edges) | Incremental |

For non-URL items, `snapshot` is skipped and `extract` runs on the original bytes. Stage ordering for a URL item is: `ingest → snapshot → extract → summarize → chunk → embed → entities → graph_sync`. For everything else, drop `snapshot`. `summarize`, `chunk`, and `embed` can be partially parallelized once `extract` finishes (`embed` needs both `chunk` and `summarize` outputs); `entities` and `graph_sync` follow.

For `file` items and any item whose `extract` lacks a handler, the stage fails with `error='no_handler'` after the standard retry budget. Downstream stages do not enqueue. When a handler ships later, bumping `extract_version` and reprocessing picks the items back up automatically — bytes are preserved.

### Pipeline DAG and stage enqueue

There is no central orchestrator. Each worker, on successful completion of a stage, performs an "enqueue downstream" step inside the same transaction that marks its own job done:

1. For each potential downstream stage, check whether all prereqs are met by reading `items.<prereq>_version >= current_constant` and (for fan-in stages like `embed` and `graph_sync`) confirming every prereq stage is satisfied.
2. For each stage whose prereqs are now met and which has no existing non-terminal `processing_jobs` row, insert a `pending` row.
3. NOTIFY the appropriate channel (`job_pool_fast` or `job_pool_slow`) so a listening worker wakes immediately.

Concurrent workers (e.g. `chunk` and `summarize` both finishing at once and both trying to enqueue `embed`) race on the unique `(item_id, stage)` constraint; one INSERT wins, the other catches the unique violation and ignores it.

### Claim and reprocess race

Workers claim with:

```sql
UPDATE processing_jobs
   SET status = 'running',
       started_at = now(),
       attempts = attempts + 1,
       claim_token = gen_random_uuid()
 WHERE id = $id AND status = 'pending'
RETURNING claim_token;
```

The worker carries `claim_token` for the duration of the stage. Final UPDATE (mark done / failed) is gated on `WHERE claim_token = $token`. `/admin/reprocess` and the stale-job sweeper both NULL `claim_token` when they reset a row, so any stale worker's final UPDATE matches zero rows and silently no-ops.

### Worker Pools

- **Fast pool**: handles every stage except `snapshot`. Configurable concurrency (default 4 workers). Stages in this pool are CPU/IO-light or LLM-bound and complete in seconds.
- **Slow pool**: handles `snapshot` only. Default 2 workers. Each snapshot can take 10–30 seconds (Playwright + SingleFile + screenshot), and we don't want it blocking fast-path processing.

Both pools poll the same `processing_jobs` table; the `pool` column on each row directs work. Pools scale independently.

### Failure Handling

- Each stage has `attempts` capped at **5**. Retry delays use exponential backoff with jitter (1s, 4s, 16s, 64s, 256s).
- On the 6th failure, `status='failed'`, `error` holds the last error, and `next_attempt_at` is cleared. The job stays put — no automatic dead-letter movement, no further retries.
- The item itself remains in whatever partial state it reached; downstream stages do not enqueue. (Exception: `snapshot` failures kick off `wayback_fallback`; see URL Archival.)
- Failed jobs are surfaced via `GET /admin/jobs?status=failed` and reset via `POST /admin/jobs/:id/retry` (clears `attempts`, sets `status='pending'`, NULLs `claim_token`).

### Stale-job sweeper

A periodic task (every 60s) resets jobs whose worker died mid-stage:

```sql
UPDATE processing_jobs
   SET status = 'pending', claim_token = NULL,
       next_attempt_at = now()
 WHERE status = 'running'
   AND started_at < now() - interval '15 minutes';  -- stage_timeout, configurable
```

A SIGTERM'd worker also unparks its own claim before exiting (`SET status='pending', claim_token = NULL`) so deploys recover instantly without waiting on the sweeper. The sweeper exists for hard kills, OOMs, and crashed processes.

### Item Status (derived)

`items.status` is **not a column**. The `/items/:id/status` endpoint computes status from `items.<stage>_version` plus open `processing_jobs` rows:

- `failed` if any job for the item is in `status='failed'`.
- `processing` if any job is `pending` or `running`.
- `ready` if every applicable stage version equals its current code constant.
- `pending` otherwise (e.g. just-ingested, no stage rows yet).

### Reprocessing

Every stage has a version constant in code. Bumping a constant marks all items with `<stage>_version < $current` as needing reprocessing for that stage only.

```
POST /admin/reprocess?stage=summarize
```

The endpoint resets matching `processing_jobs` rows (`status='pending'`, `attempts=0`, `next_attempt_at=now()`); workers pick them up. On stage success, the worker updates `items.<stage>_version` to the current constant. Old derived data is fully overwritten.

For embeddings specifically: a new model creates new rows alongside the old (same dimension) or in a new per-dimension table. Once the new model is validated, old rows are dropped in a separate sweep.

---

## Embedding Strategy

**Multi-granularity: chunk + summary.**

- **Chunk embeddings**: 512-token chunks with 64-token overlap, tokenized via `tiktoken cl100k_base` (matches `text-embedding-3-small`'s tokenizer). Used for precise passage retrieval.
- **Summary embeddings**: the LLM-generated summary embedded as one vector. Represents "what is this item about" much better than averaging a long document. Used for item-level thematic search.

**Short-doc shortcut**: if the chunker produces exactly one chunk for a document, skip chunk embeddings and store only a summary embedding. The summary already represents the document at this scale. This sidesteps the off-by-one debate around the 512-token boundary — the chunker's count is the source of truth.

**Long-doc cap**: at most **1000 chunks per item**. Beyond that the chunker truncates and writes `metadata.chunk_overflow = true` plus `metadata.original_chunk_count` so the item is reviewable. 1000 chunks ≈ a 600-page book; anything longer should probably be split before ingestion.

Raw whole-document embeddings are explicitly **not** used. They blur multi-topic documents into uninformative vectors. The summary already does the compression.

Embeddings are keyed by `(granularity, item_id, chunk_id, model_name, model_version)` so multiple embedding models can coexist during migrations. Different vector dimensions live in separate `embeddings_<dim>` tables.

**Active query model.** Retrieval uses the model named by the `CURRENT_EMBEDDING_MODEL` env var. Backfills target this same name. Migration is: stand up new model alongside, validate, flip the flag, drop old rows in a separate sweep.

---

## Search

Hybrid retrieval fuses three ranked lists via reciprocal rank fusion (RRF, k=60, equal weights):

1. **BM25** over `content.tsv` (Postgres `to_tsquery` against the GIN index).
2. **Summary vector** ANN: query embedded with `CURRENT_EMBEDDING_MODEL`, searched against the summary partial HNSW.
3. **Chunk vector** ANN: same query embedding, searched against the chunk partial HNSW.

Each list contributes `1 / (k + rank)` per item; chunk hits aggregate up to their item.

### Behavior

- **Default return: items.** Each result is one item card with title, summary, and metadata.
- **Chunks on drill-down.** Each item result includes its top-N matching chunks as a nested field. UI hides them until the user expands the card.
- **Item ranking** is the RRF score above; chunk RRF contributions are summed per item then folded into the item-level score.
- **Item-level deduplication.** Multiple matching chunks from one item collapse to one card with multiple highlighted passages on expansion. Otherwise long documents dominate.
- **Highlights.** `matching_chunks[].highlights` is produced by Postgres `ts_headline` over the chunk text using the same `tsquery` as the BM25 arm.

### Filters

All optional, all combinable. Repeating a filter is OR within that facet; different facets AND together.

| Param | Meaning |
|---|---|
| `q` | Free-text query; if absent, the endpoint returns recent items by `captured_at desc` filtered by the rest |
| `type` | Item type (`text`, `url`, `image`, `pdf`, `audio`, `video`, `file`) |
| `from` / `to` | ISO date range on `captured_at` |
| `tag` | Tag name (repeatable) |
| `entity` | Entity id (repeatable) |
| `possible_duplicate` | If `true`, only items with `metadata.possible_duplicate_of` set |
| `limit` | Default 20, max 100 |
| `cursor` | Opaque pagination token |

### Cursor format

Base64-encoded JSON with a mode prefix:

- Query mode (`q` present): `{"mode":"q","score":<float>,"id":"<uuid>"}`. Pagination is by `(score desc, id desc)`.
- Browse mode (`q` absent): `{"mode":"b","captured_at":"<iso>","id":"<uuid>"}`. Pagination is by `(captured_at desc, id desc)`.

Servers reject cursors whose mode mismatches the request.

### Endpoint shape

```
GET /search?q=...&type=...&from=...&to=...&tag=...&entity=...&limit=20
→ {
    items: [
      {
        id, title, summary, type, captured_at,
        score,                                  -- omitted when q is absent
        matching_chunks: [{ chunk_id, text, score, highlights }, ...]
      }
    ],
    next_cursor: "..."
  }
```

---

## URL Archival

Aggressive archival because the web rots.

### Pipeline (URL items)

1. At `ingest`: canonicalize the URL **without resolving redirects** — lowercase scheme/host, drop default ports, drop fragment, strip tracking params, sort remaining params, strip trailing slash. Set `items.canonical_url` and `items.content_hash = sha256(canonical_url)`. If a row with this hash already exists, append to `item_sources` and stop. Ingest does **no network calls** so the bot ack stays fast.
2. `snapshot`: resolve redirects via HEAD/GET, then **classify the resolved URL**:
   - **HTML page** (default): launch Playwright, load page, wait for network idle + small delay. Capture three artifacts:
     - **SingleFile HTML**: self-contained, all CSS/JS/images/fonts inlined. Stored as the canonical raw bytes at `raw/<sha256-of-html-bytes>`; `items.raw_ref` updated.
     - **Full-page screenshot** (`page.screenshot({fullPage: true})`), PNG, in `derived/<item_id>/screenshot.png`.
     - **Metadata** (title, OG tags, final URL, fetch timestamp) in `derived/<item_id>/metadata.json`.
   - **PDF** (`Content-Type: application/pdf`): skip Playwright, fetch the PDF into `raw/`. Item type stays `url`; PDF extraction runs in step 4.
   - **Recognized media host** (YouTube, Vimeo, Spotify, Apple Podcasts, SoundCloud, etc.): skip Playwright. Fetch oEmbed / page metadata / RSS entry. Persist the metadata JSON as the canonical raw bytes at `raw/<sha256-of-json-bytes>`; cache thumbnail to `derived/<id>/thumbnail.jpg`. **Mutate `items.type` to `audio` or `video`** based on host classification. The recognized-host list lives in `media_hosts.py` next to the canonicalization list.
   - **Direct media `Content-Type`** (`audio/*`, `video/*`): fetch headers, run ffprobe, persist the probe output. Set `items.type` to `audio`/`video`. Same metadata-only treatment as a recognized host.
3. After classification, `snapshot` always sets `items.canonical_url` to the redirect-resolved URL and recomputes `content_hash`. If the new hash collides with another existing item, snapshot **merges** — see "Redirect collision" below.
4. `extract`: dispatch by item type — trafilatura on SingleFile HTML for `url`, pymupdf (with vision fallback) for PDFs, vision OCR + caption for `image`, metadata-text assembly for `audio`/`video` (title + description + show/channel + captions when present).

### Failure modes

- **Paywalls / login walls**: capture what's visible, flag in metadata.
- **Bot detection (Cloudflare etc.)**: snapshot may be the challenge page; accepted.
- **Dead links**: handled by the `wayback_fallback` stage (below).
- **Timeouts**: 30 seconds, retry with longer timeout via the standard backoff. After cap, snapshot job is `failed` and a `wayback_fallback` job is enqueued.

The `snapshot` stage's slow pool isolates this latency from the rest of the pipeline.

### Redirect collision

Because ingest does not resolve redirects, two submitted URLs may hash to different items at ingest but resolve to the same canonical URL during `snapshot`. The colliding case is handled inside the snapshot transaction:

1. Compute `new_hash = sha256(resolved_canonical_url)`. If equal to the item's existing `content_hash`, no-op and continue.
2. If `new_hash` matches another `items.content_hash`, treat that other item as the **survivor** (it was captured first by `captured_at`):
   - Move every `item_sources` row from the current item over to the survivor.
   - Insert `item_redirects (from_id = current.id, to_id = survivor.id, reason = 'redirect_collision')`.
   - Cascade-delete the current item (this also drops its `processing_jobs` rows; this worker's own row is the in-flight one — its final UPDATE no-ops because the row is gone).
   - If the survivor doesn't yet have a `snapshot` artifact, transfer this snapshot's outputs (`raw_ref`, screenshot, metadata.json, thumbnail) to the survivor and bump its `snapshot_version`. Otherwise discard.
3. If no collision, update the current item's `canonical_url` and `content_hash` to the resolved values and continue normally.

`GET /items/:id` and `GET /items/:id/status` consult `item_redirects` on miss and respond with a 308-style payload `{ redirect_to: <to_id> }` (HTTP 200 with explicit body, not an HTTP redirect, so the bot can render a clear "merged" message). Bot polling treats this as the signal to switch its tracked id and edit the user-facing message.

### Wayback fallback stage

When `snapshot` exits in `failed` status (5 attempts exhausted), the stage's enqueue step inserts a `wayback_fallback` job on the slow pool instead of an `extract` job. The fallback worker:

1. Queries the Wayback Machine availability API for the canonical URL.
2. If a snapshot exists, fetches the closest archived HTML, writes it to `raw/<sha256-of-html-bytes>`, updates `items.raw_ref`, and tags `metadata.archive_source = 'wayback'` plus the wayback timestamp.
3. Enqueues `extract` so the rest of the pipeline proceeds normally.
4. If no archived snapshot exists, the `wayback_fallback` job goes to `failed` and downstream stages remain unenqueued. The user can re-snapshot later via the admin reprocess endpoint.

### URL canonicalization rules

Two phases. Ingest runs the **purely-syntactic** rules synchronously (no network); `snapshot` adds redirect resolution before re-canonicalizing and (if needed) merging.

Syntactic phase, applied in order:

1. Lowercase scheme and host.
2. Drop default ports (80 for http, 443 for https).
3. Drop fragment (`#...`).
4. Strip tracking query params: `utm_source`, `utm_medium`, `utm_campaign`, `utm_term`, `utm_content`, `fbclid`, `gclid`, `mc_eid`, `mc_cid`, `ref`, `ref_src`, `igshid`, `_ga`, `yclid`, `ymclid`. Easy to extend; list lives in `canonicalize.py`.
5. Sort remaining query params alphabetically.
6. Strip trailing slash on path (except for root `/`).

Network phase, in `snapshot`:

7. Resolve redirects via HEAD/GET, then re-apply rules 1–6 to the final URL.
8. Update `items.canonical_url` and recompute `items.content_hash`. On hash collision with an existing item, run the merge in "Redirect collision" above.

---

## Entity Canonicalization

**v1: naive matching.** Lowercase exact match against canonical names and aliases. New extraction either matches or creates.

Rationale: at single-user scale, duplicates are noticeable and merge-able manually. This builds intuition for actual failure modes. Premature canonicalization (embedding-based or LLM-based) is expensive and hard to debug.

Migration path: when a smarter resolver is added later, run it once across existing entities as a merge job. No structural changes needed.

Admin endpoint: `POST /entities/merge { from_id, into_id }` — updates `item_entities`, then triggers `graph_sync` for affected items.

---

## Deduplication

Three layers:

1. **Exact dedup at ingest.**
   - URL items: by canonical URL hash. Refetching a URL returns the existing item; an `item_sources` row is appended; nothing is reprocessed.
   - All other items: by SHA-256 of raw bytes. Identical bytes return the existing item with an appended `item_sources` row.
2. **URL canonicalization** before fetch (strip tracking params, resolve redirects, normalize). This is what makes layer 1 effective for URLs.
3. **Perceptual hash** for images. Soft-flag as "possibly duplicate" rather than auto-reject. Implementation: on image ingest, compute a pHash; if it matches an existing image's pHash within Hamming distance ≤ 5, set `metadata.possible_duplicate_of = <other_item_id>` on the new item. The user reviews via `/search?possible_duplicate=true` and can `DELETE` or merge manually.

Semantic similarity is **not** used for dedup at ingest time. Too much false-positive risk. Similar items are surfaced as related in search/graph but always stored separately.

---

## Telegram Bot

Thin adapter. Posts to the ingestion API and replies with the item ID immediately, then edits the message with the summary once `summarize` completes.

Transport: **long polling** (no public URL needed). Authorization is a hardcoded allowlist of Telegram user IDs (env var). Requests from anyone else are dropped silently.

**Summary callback.** The bot keeps an in-memory map of `(item_id → message_ref)`. After ingest, a per-item poller hits `GET /items/:id/status` every 3 seconds until `stages.summarize.status == "done"` (or `failed`), then edits the original message with the title + summary. If the status response is `{ redirect_to: <other_id> }`, the poller swaps the tracked id (no new message), notes the merge in the message edit, and continues polling against the survivor. Polling state is in-memory only; if the bot restarts before summarize completes, the message stays as-is.

### Capture types

- Text-only message → text item.
- Forwarded message → same item type as it would be otherwise, with `items.metadata.forwarded_from = { sender, original_chat, forwarded_at }`.
- Photo → image item.
- Document/file → image / pdf by mime when known; `audio` for `audio/*`, `video` for `video/*`; otherwise `file` type. Bot accepts the upload either way.
- Voice note → audio item (metadata-only extraction in v1; ffprobe runs, no transcription).
- Video → video item (metadata-only extraction in v1).
- Message containing one or more URLs → **multi-item ingest**: each URL becomes its own URL item (snapshot resolves the type to `url`/`audio`/`video` per classifier), and any non-empty surrounding text becomes a separate text item. All items produced by one Telegram message share the same `item_sources.source_ref` so they're easy to relate. Forward metadata, when present, attaches to every item produced from that message.

### Bot ack timing

Ingest is synchronous and contains no network calls (URL hashing is purely syntactic; redirect resolution moved to `snapshot`). The bot's `Captured. <item_id>` reply lands well under 2 seconds even on slow origins. If `snapshot` later merges the item into a pre-existing one, the bot's status poller observes the `redirect_to` payload, switches its tracked id, and edits the original message to `Captured (already had this). <surviving_item_id>`.

### Commands (post-v1)

- `/search <query>` — returns top items
- `/recent` — last N items

The bot does not try to be a full UI. It's a capture tool plus quick lookup.

---

## Frontend (v1)

A minimal React app with a single screen:

- **Auth**: on first load, prompt for the API key; store in `localStorage`. Every request adds `Authorization: Bearer <key>`. A 401 clears storage and re-prompts.
- **Search bar** at the top.
- **Filter sidebar**: type, date range, tags, entities, "possible duplicates only" toggle. Filters update the result list as they change.
- **Results**: item cards (title, summary, type, captured_at, score when a query is present). Expanding a card reveals the top matching chunks with `ts_headline` highlights. Each card has a delete affordance backed by `DELETE /items/:id`.
- **Duplicate cards**: when an item has `metadata.possible_duplicate_of`, the card fetches the linked candidate (`GET /items/:id`) and renders it inline as a compact mini-card alongside, so both items are visible without navigation. Either side can be deleted from the inline view.

Empty query and no filters returns recent items ordered by `captured_at desc`, paginated. Search and browse are the same view; the difference is whether `q` is set.

That's the whole frontend for v1. No reel, no discovery, no on-this-day, no edit.

---

## API Surface (v1)

Single API key in `Authorization: Bearer <key>` header, validated via `hmac.compare_digest` against `API_KEY` env. No per-user, no scopes. Multipart ingest cap: **25 MB** (matches the eventual hosted-whisper input limit). All routes mount under `/v1/` so v2 can break shapes without coordinating clients in lockstep.

| Method | Path | Description |
|---|---|---|
| POST | `/items` | Ingest. JSON or multipart. Returns 202 with `item_id` (or a list of `item_id`s when one ingest produces multiple items, e.g. a Telegram message with text + URL). |
| GET | `/items/:id` | Full item with content, chunks, entities, tags, sources. If `:id` is a merged-away id, returns `200 { redirect_to: <surviving_id> }` instead of the item body. |
| GET | `/items/:id/status` | Per-stage status (see shape below). Same `redirect_to` payload on merged ids. |
| DELETE | `/items/:id` | Cascade delete: Postgres rows in dependent tables (FK cascade), `item_redirects` rows pointing at the id, all RustFS objects under `derived/<id>/`, the canonical raw object at `items.raw_ref` (unique by construction since `content_hash` is unique), and the Neo4j Item node + edges. |
| GET | `/search` | Hybrid search with filters; also serves browse-by-recency when `q` is absent. |
| POST | `/entities/merge` | Admin: merge two entities. |
| GET | `/admin/jobs` | Admin: list jobs by status. |
| POST | `/admin/jobs/:id/retry` | Admin: reset a failed job (clears `attempts`, NULLs `claim_token`, sets `pending`). |
| POST | `/admin/reprocess` | Admin: bulk reset of a stage for all items below the current version. |
| POST | `/admin/items/:id/reprocess` | Admin: reset one stage on one item (`?stage=summarize`). Doesn't bump version constants. |
| POST | `/admin/rebuild_graph` | Admin: drop and rebuild Neo4j from Postgres. |
| GET | `/healthz` | Liveness. Returns 200 when API process is up. |
| GET | `/metrics` | Prometheus exposition: queue depth by pool, jobs by status, stage lag, error counts. |

### `GET /items/:id/status` response

The `stages` object contains **only the stages applicable to this item's type**. URL items include `snapshot`; non-URL items omit it. The `extract` key is always present (even for `file` items, where it will reach `failed` with `error='no_handler'`).

URL item example:

```json
{
  "id": "<uuid>",
  "stages": {
    "snapshot":   { "status": "done",     "version": 1, "attempts": 1, "error": null },
    "extract":    { "status": "done",     "version": 2, "attempts": 1, "error": null },
    "summarize":  { "status": "running",  "version": 3, "attempts": 1, "error": null },
    "chunk":      { "status": "pending",  "version": 0, "attempts": 0, "error": null },
    "embed":      { "status": "pending",  "version": 0, "attempts": 0, "error": null },
    "entities":   { "status": "pending",  "version": 0, "attempts": 0, "error": null },
    "graph_sync": { "status": "pending",  "version": 0, "attempts": 0, "error": null }
  },
  "overall": "processing"
}
```

For a merged-away id, the response is `200 { "redirect_to": "<surviving_id>" }` with no `stages` field.

`overall` is the derived item-level status (`pending`, `processing`, `ready`, `failed`).

---

## Versioning

Every stage has a version constant in code. Every derived row records the version that produced it; `items.<stage>_version` records the latest successful version per item.

| Stage | Version dimension | Why bump |
|---|---|---|
| `extract` | `extraction_method` + version | New PDF library, better OCR, better vision prompt |
| `summarize` | `prompt_version` | Improved prompt, model swap |
| `chunk` | `chunker_version` | Different chunk size or strategy |
| `embed` | `model_name` + `model_version` | New embedding model |
| `entities` | `extraction_version` | Better entity prompt, schema change |
| `graph_sync` | `graph_schema_version` | New edge type, weight formula change |

Reprocess flow: bump constant, run `POST /admin/reprocess?stage=<stage>`, worker picks up everything below the new version.

---

## Operations

See `BUILD.md` for toolchain pins, repo layout, env vars, and CI shape. Spec-relevant ops:

- **Migrations**: Alembic. Every schema change is a named revision; rollbacks are first-class.
- **Local dev = prod recipe**: `docker compose up` brings up Postgres+pgvector, Neo4j, RustFS, the API process, fast and slow worker pools, and the bot. Production uses the same compose file under Coolify (which adds TLS, reverse proxy, env-var management, scheduled backups).
- **Process model**: api, fast-worker, slow-worker, and bot are separate processes / containers, each an entrypoint of the single `pliny` package. Worker pools run one process per pool with N async tasks inside (defaults: fast=4, slow=2).
- **Logging**: structured JSON to stdout. Every line includes `stage`, `item_id`, `attempt`, `latency_ms`, `error` where applicable. Shape is Sentry/Prometheus-consumable; no tracker wired in v1.
- **Tests**: end-to-end suite in CI using `testcontainers` for Postgres+pgvector, Neo4j, and RustFS, with fixture items covering each item type and each stage. OpenAI is stubbed at the LLM-wrapper boundary in CI; a separate "live" suite exercises real OpenAI in a nightly run.
- **NOTIFY channels**: one per pool — `job_pool_fast`, `job_pool_slow`. Workers `LISTEN` their channel; enqueuers `NOTIFY` after insert.
- **OpenAPI sync**: CI regenerates `frontend/src/api/types.ts` from the running API's `/openapi.json` and fails on diff, so frontend types never drift from FastAPI.

## Backup

Backup wiring is deferred for v1 — Coolify's scheduled `pg_dump` plus RustFS bucket replication get turned on once captured volume justifies the risk. The storage layout is designed for it from day one:

- **Postgres**: nightly `pg_dump` to cold object storage (Coolify-managed once enabled).
- **RustFS**: bucket replication (S3-compatible) to cold storage; `raw/` is critical, `derived/` is rebuildable.
- **Neo4j**: not backed up. Recovery is `POST /admin/rebuild_graph` against a restored Postgres.

## Open Decisions / Deferred

- Tag taxonomy: free-form vs constrained vocabulary. Starting free-form, may consolidate later.
- User-edited tags: deferred. v1 only emits `llm` tags; the `item_tags.source` column returns when an editing UI ships.
- Whether to embed entity names for similarity-based canonicalization in v2.
- Audio/video deep understanding (Whisper transcription, frame analysis, scene detection): deferred. v1 indexes platform metadata only — title, description, channel/show, public captions, ID3/EXIF/ffprobe tags. Transcription lands as an `extract_version` bump that backfills existing audio/video items in place; metadata stays, transcript appends.
- Replacing the Postgres-backed queue with a real broker (Redis/NATS/etc.) once contention or scale demands it.
- Frontend polish (sort options, saved filter sets, on-this-day, reel).

---

## v1 Build Order

1. **[done]** Alembic + Postgres schema (items, item_sources, item_redirects, content, chunks, embeddings_1536, entities, item_entities, tags, item_tags, processing_jobs). `BlobStore` interface (filesystem impl first, then RustFS).
2. **[done]** FastAPI scaffolding, `/items` ingest endpoint with **syntactic-only** URL canonicalization + dedup + `item_sources`. `hmac.compare_digest` API-key dependency. `/healthz`. Multi-item ingest from a single source_ref (text + URL splitting).
3. **[done]** Worker loop with two pools, claim_token, NOTIFY/LISTEN, exponential backoff, and the stale-job sweeper. Status endpoint computes from versions + jobs and honors `item_redirects`.
4. **[done]** `extract` stage for text and URLs (initially without Playwright snapshot — fetch raw HTML directly). Image OCR + caption via vision; perceptual hash computed in the same stage so `metadata.possible_duplicate_of` is populated from day one.
5. **[done]** `summarize` and `chunk` stages.
6. **[done]** `embed` stage with pgvector and partial HNSW indexes per `(granularity, model_name)`.
7. **[done]** `/search` endpoint with hybrid RRF retrieval, mode-specific cursor, `ts_headline` highlights, filters (including `possible_duplicate=true`).
8. `entities` stage and Neo4j wiring (driver + connection from env).
9. `graph_sync` stage and `rebuild_graph` admin endpoint.
10. `snapshot` stage on the slow pool: redirect resolution + classifier (HTML / PDF / recognized media host / direct media). Playwright + SingleFile + screenshot for HTML; metadata fetchers + ffprobe for media. Snapshot-time merge into `item_redirects` on hash collision. Flip URL `extract` to read SingleFile HTML. Add `wayback_fallback` stage.
11. Telegram bot (long polling + `/status` polling for summary callback + redirect-following on merge).
12. React viewer (search bar + filter sidebar + results + delete affordance + localStorage auth + duplicate-card inline render of the linked candidate).
13. `/metrics`, reprocess admin endpoints, end-to-end CI suite, reprocessing flow validation.
