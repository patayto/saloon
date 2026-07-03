# Saloon

A personal Spotify library browser and analyser.

## Features

- Browse and search your saved tracks, with audio features (danceability, energy, valence, tempo, etc.)
- Playlist management with delta sync and staleness detection
- Lyrics via [LRCLib](https://lrclib.net/) → [Genius](https://genius.com/) fallback chain
- Mood timeline and genre analytics with Chart.js visualisation
- Mashup partner suggestions (KNN over audio features + lyric embeddings)
- Mashup tab: compare any two library tracks side by side with a compatibility score (0–100), per-feature diffs, and hover notes based on harmonic rules
- Background sync jobs with live progress panel in the UI
- LLM-generated tags per track across five axes — mood, theme, scene, style, and tempo feel — with confidence scores (via OpenRouter)
- Filter the library by any tag (e.g. mood:melancholic)
- Graph view: group nodes by tag axis to form clusters, or filter to a single tag value
- Mashup view: overlapping tags highlighted in the compatibility column

## Requirements

- [Docker](https://docs.docker.com/get-docker/) + Docker Compose
- A [Spotify app](https://developer.spotify.com/dashboard) (client ID + secret)

## Quick start

```bash
git clone https://github.com/YOUR_USERNAME/saloon.git
cd saloon

cp .env.example .env
# Edit .env — fill in SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET

docker compose up --build
```

Open [http://localhost:8000](http://localhost:8000).

## Configuration

`.env` variables:

| Variable | Required | Description |
|---|---|---|
| `SPOTIFY_CLIENT_ID` | yes | Spotify app client ID |
| `SPOTIFY_CLIENT_SECRET` | yes | Spotify app client secret |
| `SPOTIFY_REDIRECT_URI` | yes | OAuth callback — default `http://localhost:8000/spotify/callback/` |
| `GENIUS_ACCESS_TOKEN` | no | Genius API token for fallback lyrics |
| `OLLAMA_URL` | no | Ollama base URL for lyric embeddings (default: `http://localhost:11434`; use `http://host.docker.internal:11434` in Docker) |
| `OPENROUTER_API_KEY` | no | [OpenRouter](https://openrouter.ai/) API key for track tag generation |
| `SECRET_KEY` | no | Django secret key (a default is provided for local dev; set this in production) |

Add `http://localhost:8000/spotify/callback/` to the **Redirect URIs** list in your Spotify app settings.

## Loading your library

**One-time OAuth login** (required for live sync):

1. Start the app: `docker compose up`
2. Visit [http://localhost:8000/spotify/login/](http://localhost:8000/spotify/login/) and approve access.

Saloon stores a refresh token — you never need to repeat this.

**Sync saved tracks:**
```bash
docker compose exec web .venv/bin/python manage.py sync_saved_tracks
```

Or use the **Sync Library** button in the UI.

## Management commands

All management commands run inside the container:

```bash
docker compose exec web .venv/bin/python manage.py <command>
```

| Command | Description |
|---|---|
| `sync_saved_tracks` | Delta-sync `/me/tracks` from Spotify |
| `fetch_audio_features` | Backfill audio features for all saved tracks |
| `fetch_lyrics` | Backfill lyrics for all saved tracks (LRCLib → Genius) |
| `sync_playlists` | Fast library-level playlist scan (detects stale playlists) |
| `sync_playlist_tracks <id>` | Per-playlist delta sync (adds/removes/reorders tracks) |
| `compute_sentiment` | VADER sentiment backfill for tracks with lyrics |
| `compute_lyric_embeddings` | Ollama lyric embedding backfill (requires Ollama running) |
| `compute_track_tags` | LLM tag backfill via OpenRouter across five axes: mood, theme, scene, style, tempo feel (requires `OPENROUTER_API_KEY`); use `--retry` to cycle through free model fallbacks on 429/5xx; `--refresh-stale` to re-tag tracks tagged under an older vocabulary |
| `backfill_promoted_tags` | Merge recorded out-of-vocabulary tag suggestions into existing track tags after a tag is promoted into the allowed list (no LLM calls) |

## OpenRouter (optional — track tags)

Tags are generated across five axes per track and stored with per-tag confidence scores:

| Axis | What it captures |
|---|---|
| **mood** | Emotional tone — melancholic, euphoric, yearning, triumphant, etc. |
| **theme** | Subject matter — love, loss, identity, political, memory, etc. |
| **scene** | Listening context — late_night, road_trip, study_focus, slow_dance, etc. |
| **style** | Lyrical/vocal delivery — storytelling, confessional, anthemic, poetic, etc. |
| **tempo_feel** | Perceived motion — driving, swaying, laid_back, hypnotic, etc. |

Tags appear in the track detail modal, are filterable in the Library tab (tag dropdown next to the search box), drive group-by clustering and per-tag filtering in the Graph tab, and show overlapping tags in the Mashup compatibility column.

Requires a free [OpenRouter](https://openrouter.ai/) account:

1. Create an API key at [openrouter.ai/keys](https://openrouter.ai/keys)
2. Add `OPENROUTER_API_KEY=<your-key>` to `.env`

Bulk backfill (skips tracks already tagged):

```bash
docker compose exec web .venv/bin/python manage.py compute_track_tags
```

Re-tag the entire library (e.g. after a model or taxonomy upgrade):

```bash
docker compose exec web .venv/bin/python manage.py compute_track_tags --force
```

Or click **Generate Tags** / **Regenerate** in any track's detail modal — tag generation runs in the background and shows live inline progress (model being tried, model index, attempt number) while it works.

Default model: `google/gemma-4-31b-it:free`. Override with `--model <model-id>` — run `--help` for a list of suggested free models. Any OpenRouter model slug is accepted.

Free models can be rate-limited aggressively (sometimes one request per 10 minutes per model). The UI automatically cycles through the full free model list as fallbacks on 429/5xx. Use `--retry` for the same behaviour in the CLI:

```bash
docker compose exec web .venv/bin/python manage.py compute_track_tags --retry
```

On a 429, the command reads the `Retry-After` header and either waits (if ≤ 60 s) or switches to the next model immediately. 5xx errors use exponential backoff starting at 8 s, capped at 60 s.

### Growing the tag vocabulary

The model may propose tags outside the allowed vocabulary; these are recorded per track as suggestions instead of being applied. Audit them in the Django admin (`/admin/analysis/tagsuggestion/`) — the changelist shows a ranked axis/tag/occurrences summary. When a suggestion has appeared often enough, add it to the `ALLOWED` dict in `analysis/management/commands/compute_track_tags.py`, then:

```bash
# Instantly apply recorded suggestions of the newly allowed tag(s) — no LLM calls
docker compose exec web .venv/bin/python manage.py backfill_promoted_tags

# Optionally re-tag tracks still on the old vocabulary via the LLM
docker compose exec web .venv/bin/python manage.py compute_track_tags --refresh-stale
```

Each tag row is stamped with a hash of the vocabulary it was generated under, so `--refresh-stale` only re-tags tracks whose vocabulary is out of date.

## Ollama (optional — lyric embeddings)

Lyric embeddings power the lyrics-similarity column in Mashup Partners. Requires [Ollama](https://ollama.com/) running on your host:

```bash
ollama pull nomic-embed-text
```

Set `OLLAMA_URL=http://host.docker.internal:11434` in your `.env`, then:

```bash
docker compose exec web .venv/bin/python manage.py compute_lyric_embeddings
```

## Database

The app uses SQLite (`db.sqlite3` in the project root). It is bind-mounted into the container — data persists between restarts and is never bundled into the image.

To migrate after pulling updates:

```bash
docker compose exec web .venv/bin/python manage.py migrate
```

## Admin

```bash
docker compose exec web .venv/bin/python manage.py createsuperuser
```

Then visit [http://localhost:8000/admin/](http://localhost:8000/admin/).

## Dev setup (without Docker)

Requires Python 3.13 and [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env   # fill in credentials
.venv/bin/python manage.py migrate
.venv/bin/python manage.py runserver
```
