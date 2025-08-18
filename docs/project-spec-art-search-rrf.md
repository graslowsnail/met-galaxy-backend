# Project Spec: Art Search with Dual Embeddings + RRF Fusion

## One-liner
Enable multimodal search across an art collection by combining image and metadata embeddings with Reciprocal Rank Fusion (RRF) for high‑quality, user‑friendly results.

---

## Problem & Goals
- Users want to find artworks by text query (e.g., “gold cross”, “American Romanticism painting”).
- Users want “similar artworks” recommendations when clicking on an image.
- Metadata can be incomplete; fallback to image-based semantic matching is essential.
- Fuse text-based and image-based rankings to get robust, flexible results.

## Non-Goals (v1)
- No fine-tuning of CLIP or custom models.
- No user personalization / recommender system.
- No live model training pipeline (batch embedding only).

## Primary Users & Jobs-to-be-Done
- **Casual visitor:** searches “Japanese art” → sees relevant pieces.
- **Scholar:** clicks one painting → sees visually similar works.
- **Curator:** tests broad/bespoke queries → verifies coverage.

## Assumptions & Constraints
- **Backend:** Postgres + pgvector.
- **Embeddings:** CLIP (512‑dim `img_vec`), text embedding model (1,024–1,408‑dim `txt_vec`; final dim set at migration time).
- **Data:** Metadata fields vary in quality; enrichment may be added later.
- **Performance:** MVP target latency < 1s for top‑50 on ≤1M items.

## Success Metrics
- ≥80% of test queries return “plausibly correct” top‑5.
- Median query latency < 800ms.
- ≥95% of artworks have both `img_vec` and `txt_vec`.
- Engagement: ≥50% of users click through from results grid.

---

## Scope
**In scope**
- Batch embedding generation for images + metadata.
- Store vectors in pgvector.
- Dual search (text→`txt_vec`, text→`img_vec` via CLIP text tower).
- RRF fusion of ranked lists.
- API endpoints for text search and image similarity.
- Simple grid-based UI for results.

**Out of scope (for now)**
- Advanced rerankers (cross-encoders).
- Automatic captioning/tagging.
- Multi-lingual queries.
- Personalization.

## Open Questions (and plan)
- **Text embedding model?** OpenAI vs local HF (cost vs quality) → run 100‑query eval.
- **RRF weights / k?** Equal vs skewed; pick via small relevance test set.
- **Auto-enrichment?** Add color/material tags later if coverage gaps persist.

---

## High-Level Solution
**System overview**
- Precompute and store `img_vec` and `txt_vec` per artwork.
- On a text query, run two searches:
  1) text→`txt_vec` (metadata semantic search)  
  2) text→`img_vec` (CLIP text tower → image space)
- Fuse the two ranked lists with RRF; return fused results.
- “View similar” runs image→`img_vec` only.

**RRF formula**
For document `d` over result lists `L`, RRF score is:
\[
\text{RRF}(d)=\sum_{i \in L}\frac{1}{k + \text{rank}_i(d)}
\]
- Default **k = 60** (tunable); higher `k` reduces tail influence.
- Optionally scale per‑list contribution (e.g., `w_text`, `w_image`) via:  
  \[
  \text{RRF}_w(d)=\sum_{i \in L} w_i \cdot \frac{1}{k + \text{rank}_i(d)}
  \]

**Architecture sketch (textual)**
```
User Query
  → API
    → Embed text twice (text model; CLIP text tower)
    → Search txt_vec index (pgvector, cosine/HNSW)
    → Search img_vec index (pgvector, cosine/HNSW)
    → RRF fusion
    → Ranked results → UI grid
Image click ("similar")
  → API → search img_vec with image embedding → ranked results
```

---

## Data Model
**Artwork (logical)**
- `id` (int, PK)
- `objectId` (int, external ref)
- `title` (text)
- `artist` (text)
- `date` (text)
- `medium` (text)
- `department` (text)
- `culture` (text)
- `img_vec` (vector[512]) — CLIP image embedding
- `txt_vec` (vector[TXT_DIM]) — metadata text embedding (set dim at migration time)
- `objectUrl` (text)
- `primaryImage` (text URL)

**DDL (pgvector)** — set `TXT_DIM` to match chosen text model
```sql
CREATE EXTENSION IF NOT EXISTS vector;

-- Base table
CREATE TABLE IF NOT EXISTS artworks (
  id BIGSERIAL PRIMARY KEY,
  object_id BIGINT,
  title TEXT,
  artist TEXT,
  date_text TEXT,
  medium TEXT,
  department TEXT,
  culture TEXT,
  object_url TEXT,
  primary_image TEXT,
  img_vec vector(512),
  txt_vec vector(/* TXT_DIM e.g., 1024, 1344, 1408 */),
  created_at TIMESTAMPTZ DEFAULT now()
);

-- HNSW indexes (cosine distance recommended)
-- Tune m / ef_construction per dataset scale
CREATE INDEX IF NOT EXISTS idx_artworks_img_vec_hnsw
ON artworks USING hnsw (img_vec vector_cosine_ops)
WITH (m = 16, ef_construction = 64);

CREATE INDEX IF NOT EXISTS idx_artworks_txt_vec_hnsw
ON artworks USING hnsw (txt_vec vector_cosine_ops)
WITH (m = 16, ef_construction = 64);
```

---

## API (Contracts)

### `GET /search`
Search by text; runs dual retrieval + RRF fusion.
- **Query params**
  - `q` (string, required) — user query
  - `k` (int, optional, default `50`) — max results
  - `w_text` (float, optional, default `1.0`), `w_image` (float, optional, default `1.0`)
  - `k_rrf` (int, optional, default `60`)
- **Response (200)**
```jsonc
{
  "query": "japanese art",
  "results": [
    {
      "id": 123,
      "title": "Ukiyo-e print",
      "artist": "Hokusai",
      "primaryImage": "https://...",
      "objectUrl": "https://...",
      "score": 0.1342,
      "subscores": { "text_rank": 3, "image_rank": 11 }
    }
  ],
  "timing_ms": { "embed": 25, "txt_search": 120, "img_search": 130, "fusion": 1, "total": 280 }
}
```

### `GET /similar`
Nearest neighbors in `img_vec` space.
- **Query params**
  - `id` (int, required) — artwork id
  - `k` (int, optional, default `24`)
- **Response (200)**
```jsonc
{
  "id": 123,
  "similar": [
    { "id": 456, "title": "Wave Study", "primaryImage": "https://...", "score": 0.873 }
  ]
}
```

**Notes**
- Use cosine distance (lower is closer) but return an **affinity score** (e.g., `1 - dist`) for UX.
- Include per‑stage timings for ops dashboards.

---

## Phased Plan

### Phase 0 — Discovery & Planning (2–3 days)
**Steps**
- Decide text model (OpenAI vs HF) and dimension.
- Estimate embedding cost/throughput; define batch sizing.
- Finalize pgvector schema + index ops.

**Deliverables**
- Decision doc, schema migration.

**Acceptance**
- Chosen models; migration applied.

### Phase 1 — MVP (2 weeks)
**Steps**
- Build batch embedding pipeline (images + metadata).
- Populate pgvector with `img_vec` + `txt_vec` (≥95% coverage).
- Implement `/search` (dual retrieval + RRF) and `/similar`.
- Minimal grid UI (image, title, artist).

**Deliverables**
- Running demo on seed dataset (≈10k).

**Acceptance**
- Query “Japanese art” returns plausible results.
- Clicking an image returns visually similar items.
- P50 latency < 1s on 10k items.

### Phase 2 — Beta/Polish (2–3 weeks)
**Steps**
- Create 100‑query relevance set; annotate top‑10.
- Tune `w_image`, `w_text`, and `k_rrf` via grid search.
- UX improvements (facets, hover preview, skeletons).

**Deliverables**
- Beta release; evaluation report.

**Acceptance**
- ≥80% top‑5 plausibility on test set.

### Phase 3 — Production Hardening (2 weeks)
**Steps**
- Monitoring (latency, CTR, “no results”), retries, backups.
- Index/DB tuning (HNSW `ef_search`, caching, VACUUM).
- IaC for DB + API; on‑call docs; load test to 50 QPS.

**Deliverables**
- Infra-as-code; runbook; dashboards.

**Acceptance**
- 99.9% uptime target; passes 50 QPS load test.

---

## Work Breakdown

| ID  | Task                                               | Owner | Effort | Depends On | Acceptance |
|-----|----------------------------------------------------|:-----:|:------:|:----------:|-----------|
| T1  | Select text embedding model                        | Eng   |   S    |     —      | Decision doc w/ cost & eval |
| T2  | Schema for artworks + vectors                      | Eng   |   S    |    T1      | Migration applied |
| T3  | Batch embed metadata (txt_vec)                     | Eng   |   M    |    T1      | ≥95% coverage |
| T4  | Batch embed images (img_vec)                       | Eng   |   M    |    T1      | ≥95% coverage |
| T5  | API: `/search` (txt_vec search)                    | Eng   |   S    |    T3      | Returns sorted list |
| T6  | API: `/search` (img_vec via CLIP text tower)       | Eng   |   S    |    T4      | Returns sorted list |
| T7  | RRF fusion module                                  | Eng   |   M    |  T5,T6     | Fuses lists deterministically |
| T8  | API: `/similar`                                    | Eng   |   S    |    T4      | Returns neighbors |
| T9  | Frontend results grid                              | FE    |   M    |  T5–T8     | Displays images+titles |
| T10 | Relevance test set (100 queries)                   | PM    |   M    |    T1      | Annotated cases |
| T11 | RRF weight tuning                                  | Eng   |   S    |  T7,T10    | Eval report produced |
| T12 | Monitoring + metrics                               | DevOps|   M    |  T5–T9     | Dashboards live |

---

## Risks & Mitigations
- **Sparse/bad metadata** → lean on cross‑modal (text→`img_vec`); consider lightweight captions later.
- **Embedding cost** → local CLIP for images; external API for text; batch + cache.
- **Latency** → HNSW indexes; cache embeddings; tune `ef_search`; CDN on images.
- **Storage** → ~1KB/vector ⇒ ~2GB for 1M items (two vectors) — acceptable.

---

## Analytics & Telemetry
- Log: query string, top‑5 IDs, click‑through ID, response timing.
- Dashboards: search latency, CTR, “no‑result” queries, embedding queue depth.
- Privacy: do not log PII; truncate long queries; redact URLs if needed.

---

## Test Plan
- **Unit:** embedding pipeline (non‑null vectors), RRF fusion math.
- **Integration:** end‑to‑end query flow, DB search, fusion.
- **E2E:** user query → UI results; image click → similar results.
- **Data validation:** random 1% embeddings checked for null/NaN; checksum counts.

---

## Ops & Runbook
- Deploy via Docker/K8s; blue/green for API.
- Env vars: DB URL, model keys, `RRF_K`, `W_IMAGE`, `W_TEXT`.
- Secrets in Vault/Secrets Manager.
- Backups: nightly `pg_dump`; weekly restore drill.
- Alerts: latency >2s, error rate >2%, CTR drop >20% d/d.

---

## Timeline & Milestones
- **Week 1:** Phase 0 complete; model decisions + schema.
- **Weeks 2–3:** Phase 1 embeddings + API skeleton + UI.
- **Weeks 4–5:** Phase 2 beta + eval report.
- **Week 6:** Phase 3 production‑ready + load test.

---

## Artifacts & Links
- Repo: `art-search-service`
- DB schema: `/schemas/artworks.sql`
- API docs: `/docs/api.md`
- Mock UI: `/design/search-grid.fig`

---

## Next 3 Actions
1. Decide on text embedding model (OpenAI vs HF local).
2. Write DB migration to add `img_vec (512)` + `txt_vec (TXT_DIM)`.
3. Build batch embedding script for seed dataset (~10k works).
