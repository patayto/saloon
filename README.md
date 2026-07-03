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
- LLM-generated mood / theme / scene tags per track (via OpenRouter)

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
| `compute_track_tags` | LLM mood/theme/scene tag backfill via OpenRouter (requires `OPENROUTER_API_KEY`) |

## OpenRouter (optional — track tags)

Mood, theme, and scene tags appear in the track detail modal. Requires a free [OpenRouter](https://openrouter.ai/) account:

1. Create an API key at [openrouter.ai/keys](https://openrouter.ai/keys)
2. Add `OPENROUTER_API_KEY=<your-key>` to `.env`

Bulk backfill (skips tracks already tagged):

```bash
docker compose exec web .venv/bin/python manage.py compute_track_tags
```

Or click **Generate Tags** in any track's detail modal. Default model: `nvidia/nemotron-3-ultra-550b-a55b:free`. Override with `--model <model-id>`.

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
