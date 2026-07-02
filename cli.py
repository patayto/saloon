#!/usr/bin/env python3
"""
Saloon local CLI — development and testing tool.

Commands
--------
  fetch-audio-features   Backfill AudioFeatures for saved tracks via ReccoBeats
  sync                   Delta-sync saved tracks from the Spotify API
  track-preview          Print the 30-second preview URL for a Spotify track

A Markdown run report is appended to run_report.md after each invocation.
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import sys

# Bootstrap Django before any project imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saloon.settings")

import django
django.setup()

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

from cli.reporter import REPORT_FILE, Reporter  # noqa: E402 (after django.setup)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_fetch_audio_features(_args, reporter: Reporter) -> None:
    from spotify.audio_features.reccobeats import ReccoBeatsProvider
    from spotify.models import AudioFeatures, Track

    reporter.attach_logger("spotify.audio_features.reccobeats")
    reporter.begin_log_section()

    existing_ids = set(AudioFeatures.objects.values_list("track_id", flat=True))
    pending_ids = list(
        Track.objects.filter(saved__isnull=False)
        .exclude(id__in=existing_ids)
        .values_list("id", flat=True)
    )

    if not pending_ids:
        reporter.write("No pending tracks — audio features are up to date.")
        print("No pending tracks — audio features are up to date.")
        return

    print(f"{len(pending_ids)} tracks need audio features.")

    provider = ReccoBeatsProvider()
    total_saved = 0

    for batch in provider.fetch_stream(pending_ids):
        to_create = [
            _make_audio_features(af) for af in batch.values()
        ]
        AudioFeatures.objects.bulk_create(to_create, ignore_conflicts=True)
        total_saved += len(to_create)
        print(f"  Saved {len(to_create)} (running total: {total_saved})")

    not_found = len(pending_ids) - total_saved
    reporter.write_summary({
        "Pending tracks": len(pending_ids),
        "Resolved and saved": total_saved,
        "Not found in ReccoBeats": not_found,
    })

    if total_saved == 0:
        print("No audio features found.")
    else:
        print(f"Done. Saved {total_saved} records. {not_found} not found.")


def cmd_track_preview(args, _reporter: Reporter) -> None:
    from spotify.deezer import search_track
    from spotify.models import Track

    try:
        track = Track.objects.prefetch_related("artists").get(id=args.track_id)
    except Track.DoesNotExist:
        print(f"Track {args.track_id} not found in local DB.")
        return

    artist = track.artists.first()
    artist_name = artist.name if artist else ""

    result = search_track(title=track.name, artist=artist_name)
    if result is None:
        print(f"No Deezer match for: {artist_name} — {track.name}")
        return

    print(f"Deezer match: {result.artist} — {result.title} (id={result.id})")
    print(result.preview)

    if args.download:
        import requests
        resp = requests.get(result.preview, timeout=30)
        resp.raise_for_status()
        with open(args.download, "wb") as f:
            f.write(resp.content)
        print(f"Saved to {args.download} ({len(resp.content):,} bytes)")


def cmd_sync(_args, reporter: Reporter) -> None:
    from django.core.management import call_command
    from spotify.models import SavedTrack

    before = SavedTrack.objects.count()
    reporter.write(f"**Saved tracks before sync:** {before}\n")

    buf = io.StringIO()
    try:
        call_command("sync_saved_tracks", stdout=buf)
    except Exception as exc:
        reporter.write(f"\n**Error:** {exc}")
        raise

    output = buf.getvalue().strip()
    after = SavedTrack.objects.count()
    added = after - before

    print(output)

    reporter.write_summary({
        "Tracks before sync": before,
        "Tracks after sync": after,
        "New tracks added": added,
    })

    if output:
        reporter.write("\n**Command output:**\n")
        for line in output.splitlines():
            reporter.write(f"    {line}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_audio_features(af):
    from spotify.models import AudioFeatures
    return AudioFeatures(
        track_id=af.track_id,
        acousticness=af.acousticness,
        danceability=af.danceability,
        energy=af.energy,
        instrumentalness=af.instrumentalness,
        key=af.key,
        liveness=af.liveness,
        loudness=af.loudness,
        mode=af.mode,
        speechiness=af.speechiness,
        tempo=af.tempo,
        time_signature=af.time_signature,
        valence=af.valence,
        analysis_url="",
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Saloon local CLI — development and testing tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("fetch-audio-features", help="Backfill audio features via ReccoBeats")
    sub.add_parser("sync", help="Delta-sync saved tracks from Spotify")

    p_preview = sub.add_parser("track-preview", help="Print (and optionally download) the 30s preview for a track")
    p_preview.add_argument("track_id", help="Spotify track ID")
    p_preview.add_argument("--download", metavar="FILE", help="Save the preview MP3 to FILE")

    args = parser.parse_args()

    print(f"Report → {REPORT_FILE}")

    with Reporter() as reporter:
        reporter.session_start(args.command)
        if args.command == "fetch-audio-features":
            cmd_fetch_audio_features(args, reporter)
        elif args.command == "sync":
            cmd_sync(args, reporter)
        elif args.command == "track-preview":
            cmd_track_preview(args, reporter)
        reporter.session_end()

    print(f"Report → {REPORT_FILE}")


if __name__ == "__main__":
    main()
