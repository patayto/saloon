# Saloon — Analysis App: Implementation Plan

> **Context:** `saloon/` is a Django 6 / Python 3.13 / SQLite project managed via `uv`. The existing `spotify/` app holds all models and sync logic. Read `CLAUDE.md` before starting any task.

Saloon was built as a personal data platform for a Spotify library of 3,600+ saved tracks and associated playlists. The initial spotify app handles data ingestion and sync — pulling tracks, playlists, audio features, and lyrics from Spotify, ReccoBeats, LRCLib, and Genius into a local SQLite database. The result is a rich, self-contained dataset that Spotify itself never exposes to users in analytical form.
The analysis app is the second phase: turning that raw catalogue into insight. The core motivation is curiosity about patterns in personal music taste that are invisible at the level of individual tracks — how mood and energy shift across months and years, how genre preferences evolve, which playlists are thematically tight versus eclectic, and what tracks share deep sonic or lyrical similarity.
Concretely, the app combines three feature sources that complement each other's blind spots: audio features (structural/sonic properties from ReccoBeats), lyric sentiment (emotional content via VADER and optionally a fine-tuned transformer), and lyric embeddings (dense semantic vectors capturing meaning and theme). These are normalised and composed into per-track feature vectors, which power both the time-series analyses and the vector similarity search.
The priority throughout is keeping everything local and offline — no external API calls after the initial sync, no cloud inference. All transformer models run on-device via sentence-transformers, and vector search runs inside the existing SQLite file via sqlite-vec. This keeps the system fast, private, and self-contained, consistent with the design philosophy of the rest of the project.
>
> **New app name:** `analysis` (i.e. `saloon/analysis/`)
>
> **Execution harness:** tasks are ordered for sequential agentic execution (Claude Code). Each task is self-contained and leaves the codebase in a runnable state.

---

## Task 0 — Bootstrap the `analysis` Django app

Create the app skeleton and wire it into the project.

- `python manage.py startapp analysis` inside `saloon/`
- Register in `INSTALLED_APPS` (`saloon/settings.py`)
- Add a root `analysis/urls.py` and include it in `saloon/urls.py` under `/analysis/`
- Stub `analysis/models.py`, `analysis/views.py`, `analysis/admin.py`
- No migrations yet — models come in the next task

---

## Task 1 — Add core analysis models + migrations

Define the three new models in `analysis/models.py`. All relate back to `spotify.Track` via FK/OneToOne.

### `TrackSentiment` (OneToOne → Track)
Scalar sentiment scores derived from lyrics. Fields: `vader_positive`, `vader_negative`, `vader_neutral`, `vader_compound` (all `FloatField`); `classifier_label` (CharField, blank=True), `classifier_score` (FloatField, null=True), `classifier_model` (CharField, blank=True); `computed_at` (DateTimeField, auto_now=True).

### `LyricEmbedding` (ForeignKey → Track, versioned by model)
Dense vector for lyrics. Fields: `model_name` (CharField), `dimensions` (IntegerField), `embedding` (BinaryField — stores `float32` numpy bytes), `computed_at` (DateTimeField, auto_now=True). `unique_together = [('track', 'model_name')]`.

### `TrackFeatureVector` (ForeignKey → Track, versioned by spec)
Combined/derived vector used for similarity and clustering. Fields: `version` (CharField — human slug, e.g. `"v1-audio-sentiment"`), `feature_spec` (JSONField — records which sources/models were included), `dimensions` (IntegerField), `vector` (BinaryField), `computed_at` (DateTimeField, auto_now=True). `unique_together = [('track', 'version')]`.

- Run `makemigrations analysis` and `migrate`
- Register all three in `analysis/admin.py`

---

## Task 2 — Install dependencies + configure sqlite-vec

Add offline ML/vector dependencies and wire `sqlite-vec` into Django's DB connection.

**Dependencies to add via `uv add`:**
- `vaderSentiment` — rule-based sentiment, no model weights
- `sqlite-vec` — Python package that loads the SQLite extension

**Embedding backend — Ollama (replaces sentence-transformers):**
`sentence-transformers` requires `torch`, which has no wheels for Python 3.13 on Intel Mac (x86_64). Embeddings are produced instead via **Ollama** (`http://localhost:11434`), using its `/api/embed` HTTP endpoint. No Python package needed beyond `requests` (already a project dep). Ollama must be running locally with the target model pulled (`ollama pull <model>`). Default model: `nomic-embed-text` (768 dims). The command should fail clearly if Ollama is unreachable.

**sqlite-vec wiring (`saloon/settings.py`):**
Connect `sqlite-vec` at DB connection time via Django's `connection_created` signal (see prior conversation for the pattern). The extension must be loaded before any `vec0` virtual table is used.

---

## Task 3 — Management command: `compute_sentiment`

Backfill `TrackSentiment` for all saved tracks that have lyrics but no sentiment row yet.

- Location: `analysis/management/commands/compute_sentiment.py`
- Source data: `spotify.TrackLyrics.plain_lyrics` — skip tracks where `instrumental=True` or lyrics are empty
- Use VADER (`vaderSentiment.SentenceAnalyzer`) — no model download, fast
- Upsert pattern (same as existing commands in `spotify/management/commands/`)
- Report: saved N, skipped N (no lyrics), skipped N (instrumental)
- Wire into `spotify/pipeline.py`'s `enrich_tracks()` as an optional third phase (off by default; enable via flag)

---

## Task 4 — Management command: `compute_lyric_embeddings`

Backfill `LyricEmbedding` for all tracks with lyrics, for a configurable model.

- Location: `analysis/management/commands/compute_lyric_embeddings.py`
- CLI arg: `--model` (default: `nomic-embed-text`); `--ollama-url` (default: `http://localhost:11434`)
- Embedding backend: **Ollama** via `POST /api/embed` — pass `{"model": model, "input": [text, ...]}` for batch requests; response is `{"embeddings": [[...], ...]}`. No Python package beyond `requests`.
- Lyrics longer than the model's context window should be chunked by verse (split on blank lines) and mean-pooled
- Serialize: `numpy.array(embedding, dtype='float32').tobytes()` into `LyricEmbedding.embedding`
- Skip tracks that already have a `LyricEmbedding` for the requested model
- Fail clearly with a descriptive error if Ollama is unreachable or the model is not pulled
- Report: saved N, skipped N (existing), skipped N (no lyrics)

---

## Task 5 — Management command: `build_feature_vectors`

Construct `TrackFeatureVector` by normalizing and concatenating features from existing tables.

- Location: `analysis/management/commands/build_feature_vectors.py`
- CLI args: `--version` (required slug), `--include-audio` (bool, default on), `--include-sentiment` (bool, default on), `--lyric-model` (optional — if provided, appends the lyric embedding for that model)
- Normalization: audio features scaled to [0,1] using min/max across the library; sentiment scores are already in [-1,1] / [0,1]; lyric embeddings are already unit-normalized by sentence-transformers
- Writes `feature_spec` JSON recording which sources were used
- Skip tracks missing any requested source (log them)
- Report: built N, skipped N (missing sources)

---

## Task 6 — Build the `vec_tracks` virtual table + KNN query helper

Create and maintain the `sqlite-vec` virtual table for similarity search.

- Location: `analysis/vector_store.py`
- `create_vec_table(version)` — creates `vec_tracks_{version}` as a `vec0` virtual table with the correct dimension count, idempotent
- `populate_vec_table(version)` — bulk-inserts from `TrackFeatureVector` for the given version
- `knn_search(version, query_vector, k=10)` — raw SQL KNN query; returns list of `(track_id, distance)`
- Wire `create_vec_table` + `populate_vec_table` into `build_feature_vectors` command so the index is always current after a build
- Add a management command `rebuild_vec_index --version <v>` for manual rebuilds

---

## Task 7 — Analysis utilities: similarity + clustering

Pure-Python analysis functions in `analysis/analytics.py`, no Django views yet.

**Similarity:**
- `similar_tracks(track_id, version, k=10)` — loads query vector, calls `knn_search`, returns `Track` queryset annotated with `distance`
- `playlist_gap(playlist_id, version, k=5)` — centroid of playlist's feature vectors; find nearest saved-but-not-in-playlist tracks

**Clustering:**
- `cluster_tracks(version, n_clusters=None)` — loads all `TrackFeatureVector` rows for `version` into a numpy matrix; runs KMeans (sklearn); returns `{track_id: cluster_id}` dict. `n_clusters=None` uses elbow method (inertia over k=2..12, pick elbow)
- `playlist_coherence(playlist_id, version)` — intra-playlist variance of feature vectors; returns a scalar

Dependencies: `numpy`, `scikit-learn` (add via `uv add`).

---

## Task 8 — Temporal mood analytics

Time-series aggregation functions in `analysis/analytics.py` (same module, new section).

**Data source:** `SavedTrack.added_at` joined to `AudioFeatures` and `TrackSentiment`.

- `mood_timeline(granularity='month')` — returns a list of `{period, mean_energy, mean_valence, mean_vader_compound, track_count}` dicts, ordered by period. Granularity: `'week'` | `'month'` | `'quarter'`
- `russell_circumplex_by_period(granularity='month')` — same but returns `{period, mean_energy, mean_valence}` for the 2D mood plane
- `genre_timeline(granularity='quarter')` — explodes `Artist.genres` JSON; returns `{period: {genre: count}}` dict for stacked area charts

All should be pure queryset + Python — no pandas dependency unless the data volume makes it necessary.

---

## Task 9 — `analysis` tab: UI scaffolding + mood timeline chart

Add an Analysis tab to the existing `library.html` UI (matches the pattern of existing Library / Audio Features / Playlists tabs).

- New tab entry in `library.html`'s `TABS` array: `"analysis"`
- Lazy-loads `analysis/partials/analysis_overview.html` on first activation
- View: `analysis_overview` in `analysis/views.py` — calls `mood_timeline()`, passes data as JSON to template
- Chart: Chart.js line chart (already available via CDN in `base.html`) showing rolling mean valence + energy over time, dual-axis
- Template: `analysis/templates/analysis/partials/analysis_overview.html`

---

## Task 10 — Genre timeline + Russell circumplex views

Two additional HTMX partials within the Analysis tab.

- `genre_timeline` partial: stacked area chart of top-10 genres over time (Chart.js)
- `mood_scatter` partial: 2D scatter plot of all tracks on the Russell circumplex (energy Y, valence X), coloured by `SavedTrack.added_at` year — renders with Chart.js scatter type
- Both lazy-load on demand via HTMX buttons within the Analysis tab
- URLs: `/analysis/genre-timeline/`, `/analysis/mood-scatter/`

---

## Task 11 — Similarity search UI

Per-track "Find Similar" button wired into the existing track detail modal.

- Add a "Find Similar" button to `spotify/templates/spotify/partials/track_detail.html` (only shown if a `TrackFeatureVector` exists for the track)
- POST to `/analysis/tracks/<id>/similar/?version=<v>&k=10`
- View returns a partial `analysis/partials/similar_tracks.html` — a compact list of 10 tracks with distance scores, each row clickable (reuses existing modal open pattern)
- If no `TrackFeatureVector` exists for any version, show a "Run analysis pipeline first" message

---

## Task 12 — Playlist coherence + gap analysis UI

Surface clustering insights on the playlist detail page.

- On `spotify/templates/spotify/playlist_detail.html`, add a collapsible "Analysis" section (below the track list)
- Coherence score: badge showing intra-playlist variance percentile across all playlists (low = tight, high = eclectic)
- Gap analysis: "Tracks you might have missed" — top 5 results from `playlist_gap()`, shown as a compact card list
- HTMX lazy-load via GET `/analysis/playlists/<id>/insights/`
- Only renders if `TrackFeatureVector` rows exist; otherwise shows a prompt to run the pipeline

---

## Task 13 — Analysis pipeline management command + UI trigger

Wrap Tasks 3–6 into a single end-to-end pipeline command, and add a UI trigger.

- `analysis/management/commands/run_analysis_pipeline.py` — runs in order: `compute_sentiment` → `compute_lyric_embeddings` → `build_feature_vectors` → `rebuild_vec_index`; accepts `--version`, `--lyric-model` args
- Background job pattern: same `_sync_jobs` / daemon thread / poll pattern used by existing sync commands in `spotify/views.py`
- UI: "Run Analysis Pipeline" button in the Analysis tab header; starts job, registers with existing `registerJob()` sync panel in `library.html`
- URLs: `/analysis/pipeline/run/` POST, `/analysis/pipeline/status/<job_id>/` GET

---

## Notes for agents

- Always run `migrate` after model changes
- Always update `CLAUDE.md` after changes with a concise set of edits necessary to keep the file in sync with the current project and systems operations. 
- Keep all new Python in `analysis/`; do not modify `spotify/models.py` or `spotify/importers.py`
- The only permitted change to `spotify/pipeline.py` is appending an optional call to the sentiment command (Task 3)
- Use `uv add <package>` not `pip install`
- Test each management command with `--help` and a dry run against the live DB before marking a task done
- The existing `SavedTrack.added_at` field is the temporal anchor for all time-series work; playlist tracks without a `SavedTrack` row are excluded from library-level analyses