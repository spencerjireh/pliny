# Pliny — User Stories

Stories are written from the perspective of the single user (referred to as "I"). They drive what v1 must deliver and how each capability is verified.

Format: `As a <role>, I want <capability> so that <outcome>.` Each story has acceptance checks and the spec sections it touches.

---

## Capture

### S1 — Drop a link from anywhere
As the user, I want to forward a URL to the Telegram bot and receive an immediate acknowledgement so that I can keep moving and trust the link is captured.

Acceptance:
- Bot replies within 2s with `Captured. <item_id>`. Ingest does no network calls (URL hashing is purely syntactic), so the SLA holds even on slow origins.
- An `items` row exists with `type='url'`, `canonical_url` set to the syntactically-canonicalized URL, `content_hash = sha256(canonical_url)`.
- An `item_sources` row records `source='telegram'` plus the message ID.
- If the same syntactically-canonical URL is forwarded again, no new `items` row is created; a second `item_sources` row is appended.
- If `snapshot` later resolves the URL to a pre-existing item (different submitted URLs, same final URL), the new item is merged into the survivor: `item_redirects` row inserted, `item_sources` moved over, new item cascade-deleted. The bot's status poller observes `{ redirect_to: <surviving_id> }`, switches its tracked id, and edits the original message to `Captured (already had this). <surviving_id>`.

Touches: `Telegram Bot`, `URL Archival > Pipeline`, `URL Archival > Redirect collision`, `Deduplication`, `items`, `item_sources`, `item_redirects`.

### S2 — Drop a photo with text in it
As the user, I want to send a screenshot to the bot so that the text and a description of the image are searchable.

Acceptance:
- `items.type='image'`, raw bytes stored under `raw/<sha256>`.
- After `extract`, `content.extracted_text` contains both OCR'd text and a short caption.
- `tsv` indexes both; the screenshot is findable by phrases visible in it.
- If a near-duplicate already exists (pHash Hamming ≤ 5), `metadata.possible_duplicate_of` is set on the new item.

Touches: `Pipeline > extract`, `Embedding Strategy`, `Deduplication`.

### S3 — Drop a PDF
As the user, I want to upload a PDF via the API and have it chunked, embedded, and searchable so that I can later find passages by query.

Acceptance:
- `POST /items` accepts multipart up to 25 MB.
- pymupdf extracts text; if extracted token count is below a threshold, the on-demand vision fallback renders pages and runs vision OCR.
- Long PDFs cap at 1000 chunks; `metadata.chunk_overflow` records the truncation.
- Chunk-level passages are returned as `matching_chunks` on `/search` results.

Touches: `Pipeline > extract, chunk, embed`, `Embedding Strategy`.

### S4 — Drop a forwarded message
As the user, I want forwarded Telegram messages to preserve the original sender so that I can later filter by who said what.

Acceptance:
- `items.metadata.forwarded_from = { sender, original_chat, forwarded_at }`.
- Visible in `GET /items/:id`.
- Reachable via search filters when (eventually) I add an entity for the sender.

Touches: `Telegram Bot`, `items.metadata`.

### S5 — Drop a YouTube link or podcast episode
As the user, I want to forward a YouTube/Vimeo/Spotify/Apple-Podcasts/SoundCloud link and have the bot capture useful metadata so that I can find it later by title, channel, or description even though v1 doesn't transcribe.

Acceptance:
- Bot ack identical to S1 (under 2s).
- After `snapshot` runs, `items.type` is `video` for video hosts, `audio` for podcast/audio hosts (mutated from the initial `url`).
- `items.metadata` has `media_host`, `duration_seconds`, `channel`/`show`, `published_at`, `thumbnail_url` when the source exposes them; `derived/<id>/thumbnail.jpg` is cached locally.
- After `extract`, `content.extracted_text` contains title + description + channel/show + (when public) caption text. The item is searchable by phrases from any of these.
- `summarize`, `chunk`, `embed`, `entities`, `graph_sync` all run normally on the assembled text. No transcription is attempted.
- When Whisper-style transcription lands later, bumping `extract_version` and reprocessing backfills these items in place — metadata stays, transcript appends.

Touches: `Pipeline > extract`, `URL Archival > Pipeline (snapshot classifier)`, `items.metadata Discipline`, `Open Decisions`.

### S5b — Drop a voice note or video file
As the user, I want voice notes and direct video uploads to be accepted with at least probe metadata so that they're catalogued and re-processable when transcription ships.

Acceptance:
- `items.type='audio'` (voice note) or `video` (video file), raw bytes stored.
- `extract` runs ffprobe and writes `derived/<id>/probe.json`; `items.metadata` gets `duration_seconds`, container, codec.
- `content.extracted_text` is whatever tags the file carries (ID3 title/artist/album for audio, container metadata for video). Often thin; that's expected.
- Item is browseable in the UI and findable by date / source / duration filters.
- When transcription ships, `extract_version` bump backfills these without re-uploading.

Touches: `Pipeline > extract`, `Telegram Bot`, `Storage Layout (probe.json)`.

### S6 — Unsupported file type
As the user, I want to upload a `.docx` or `.epub` via the API and have it accepted as a `file` item so that the bytes are preserved and I can find it later by metadata, even before extraction lands for that mime.

Acceptance:
- `POST /items` multipart accepts arbitrary mimes; `items.type='file'`.
- Raw bytes stored; `metadata.mime` recorded.
- `extract` runs through the standard retry budget and lands in `failed` with `error='no_handler'`. Downstream stages do not enqueue.
- Item is browseable by metadata in the UI; `GET /items/:id` returns it normally.
- When the extractor for that mime ships later, bumping `extract_version` and reprocessing picks it up — bytes are unchanged.
- The Telegram bot does the same: a `.docx` document upload becomes a `file`-type item; the bot ack confirms capture even though extraction won't run in v1.

Touches: `items` enum, `Pipeline > extract`, `Telegram Bot`.

---

## Search and browse

### S7 — Recall a half-remembered passage
As the user, I want to type a fuzzy phrase into search and get the right item back so that I find things I read weeks ago.

Acceptance:
- `GET /search?q=...` returns items ordered by RRF (BM25 ∪ summary vector ∪ chunk vector), k=60 equal weights.
- Result includes `matching_chunks` with `ts_headline` highlights.
- An item with both summary and chunk hits ranks above an item with only one hit.

Touches: `Search`, `Embedding Strategy`.

### S8 — Browse recent captures
As the user, I want to open the app with no query and see what I've recently dropped in so that I can scan my last week.

Acceptance:
- `GET /search` (no `q`) returns items by `captured_at desc`.
- Cursor pagination is stable under concurrent ingest (browse-mode cursor encodes `(captured_at, id)`).
- Filters (type, date range, tag, entity) compose with browse-mode.

Touches: `Search`, cursor format.

### S9 — Filter by tag and entity
As the user, I want to combine "tag = X" with "entity = Y" so that I can find items at the intersection.

Acceptance:
- Repeated `?tag=` is OR within the facet; different facets AND together.
- Same for `?entity=`.
- v1 only emits LLM-generated tags (no user-edited tags yet — see Open Decisions); the filter operates over whatever rows exist in `item_tags`.

Touches: `Search > Filters`, `tags`, `item_tags`.

### S10 — Find probable duplicates
As the user, I want to see image items the system thinks are near-duplicates so that I can clean them up.

Acceptance:
- `GET /search?possible_duplicate=true` returns items with `metadata.possible_duplicate_of` set.
- Each card fetches the linked candidate via `GET /items/:id` and renders it inline as a compact mini-card alongside the current item, so both are visible without navigation.
- I can `DELETE` either item from the inline view.

Touches: `Deduplication`, `Search > Filters`, `Frontend (v1)`, `DELETE /items/:id`.

---

## Processing transparency

### S11 — Know whether an item is done
As the user, I want to check an item's processing status per stage so that I know whether search will find it yet.

Acceptance:
- `GET /items/:id/status` returns each applicable stage's `{status, version, attempts, error}` plus a derived `overall` status. The `stages` object only contains stages that apply to the item's type — URL items include `snapshot`, non-URL items omit it.
- For a merged-away id, the response is `200 { redirect_to: <surviving_id> }` with no `stages` field.
- The Telegram bot uses this endpoint to know when to edit the message with the summary, and to follow merges.

Touches: `Item Status`, `API Surface`, `Telegram Bot`, `URL Archival > Redirect collision`.

### S12 — A snapshot fails on a dead URL
As the user, I want the system to fall back to the Wayback Machine when live capture fails so that link rot is partially survivable.

Acceptance:
- On `snapshot` exhausting retries, a `wayback_fallback` job is enqueued instead of `extract`.
- If Wayback has the URL, archived HTML is stored at `raw/<hash>`; `metadata.archive_source = 'wayback'`; `extract` resumes.
- If Wayback has nothing, `wayback_fallback` is `failed`; downstream stages stay unenqueued; the user can re-snapshot manually later.

Touches: `URL Archival > Wayback fallback stage`.

### S13 — Surface failures
As the user, I want to see jobs that went to `failed` state so that I can decide whether to retry or accept the loss.

Acceptance:
- `GET /admin/jobs?status=failed` lists them with `stage`, `item_id`, `error`, `attempts`.
- `POST /admin/jobs/:id/retry` resets one job.
- `POST /admin/items/:id/reprocess?stage=X` resets one stage on one item without touching version constants.

Touches: `Failure Handling`, `API Surface`.

### S14 — Recover from a crashed worker
As the user, I want jobs stuck in `running` to recover automatically so that a crashed worker doesn't strand items.

Acceptance:
- The stale-job sweeper resets `running` jobs whose `started_at` is older than 15 min back to `pending` and NULLs `claim_token`.
- A worker that does come back from a half-completed stage performs its final UPDATE with the old `claim_token` and silently no-ops.

Touches: `Stale-job sweeper`, `Claim and reprocess race`.

---

## Reprocessing and migrations

### S15 — Bump the summarize prompt
As the user, I want to improve the summarize prompt and reprocess every item with the new version so that older items benefit from the better prompt.

Acceptance:
- Edit `prompts/summarize.py` (constant + `VERSION`).
- `POST /admin/reprocess?stage=summarize` resets every item with `summarize_version < $current` to `pending`.
- Workers process them; `items.summarize_version` lands at the new value on success.
- All `item_tags` rows for each reprocessed item are wiped and re-emitted (v1 has no user-tag distinction).

Touches: `Reprocessing`, `Versioning`, `item_tags`.

### S16 — Migrate to a new embedding model
As the user, I want to roll out a new embedding model alongside the old so that I can validate before cutting over.

Acceptance:
- New model writes new rows in `embeddings_1536` (or in a new `embeddings_<dim>` table if dimension differs).
- New partial HNSW indexes built per `(granularity, new_model_name)`.
- `CURRENT_EMBEDDING_MODEL` flag still points at old model; queries unchanged.
- After validation, flip the env var; old rows dropped in a separate sweep.

Touches: `Embedding Strategy`, `Indexes`, `CURRENT_EMBEDDING_MODEL`.

### S17 — Fix entity drift with a graph rebuild
As the user, I want to wipe and rebuild Neo4j from Postgres so that I can recover from any drift, schema mistake, or extraction improvement.

Acceptance:
- `POST /admin/rebuild_graph` drops Neo4j, recreates Item/Entity nodes from Postgres, recomputes co-occurrence edges, re-runs LLM-inferred edges if `entities_version` has bumped.
- Operation is idempotent; safe to run multiple times.

Touches: `Neo4j Schema > Sync Strategy`.

### S18 — Merge duplicate entities
As the user, I want to merge two entities when the naive matcher created duplicates so that mentions roll up correctly.

Acceptance:
- `POST /entities/merge { from_id, into_id }` updates `item_entities` to point at `into_id`.
- `graph_sync` is triggered for affected items; the old `Entity` node is removed, edges re-pointed.

Touches: `Entity Canonicalization`.

---

## Hygiene

### S19 — Delete a regretted capture
As the user, I want to delete an item completely so that mistaken captures (private screenshots, accidental forwards) actually go away.

Acceptance:
- `DELETE /items/:id` cascades: Postgres rows in dependent tables (FK cascade), all RustFS objects under `derived/<id>/`, the canonical raw object at `items.raw_ref` (unique by construction since `content_hash` is unique), `item_redirects` rows pointing at the id, and the Neo4j Item node + edges.
- Subsequent `GET /items/:id` returns 404; search no longer surfaces it.

Touches: `API Surface > DELETE /items/:id`.

### S20 — Auth on the frontend
As the user, I want to enter my API key once in the browser and have requests work for the session so that I don't reauth constantly.

Acceptance:
- First load prompts for the key; stored in `localStorage`.
- Every fetch attaches `Authorization: Bearer <key>`.
- A 401 clears storage and re-prompts.

Touches: `Frontend (v1)`.

---

## Out of scope (v1, named so they don't sneak in)

- Multi-user, sharing, ACLs (schema is shaped for v2; v1 leaves `user_id` NULL).
- Algorithmic discovery feed / "reel".
- On-this-day or scheduled resurfacing.
- Item editing UI (delete-only in v1).
- Discord bot.
- Mobile app.
- Real-time collaboration.

---

## Coverage map

| Spec section | Stories |
|---|---|
| Telegram Bot | S1, S2, S4, S5, S5b, S6, S11 |
| URL Archival | S1, S5, S12 |
| URL Archival > Redirect collision | S1, S11 |
| Pipeline (extract / chunk / embed / etc.) | S2, S3, S5, S5b, S6 |
| Search | S7, S8, S9, S10 |
| Item Status & failure handling | S6, S11, S13, S14 |
| Reprocessing & versioning | S5, S5b, S6, S15, S16 |
| Neo4j sync & entities | S17, S18 |
| Frontend & API surface | S8, S10, S19, S20 |
| Deduplication | S2, S10 |
