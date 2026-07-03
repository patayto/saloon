import logging

from django.core.management.base import BaseCommand

from analysis.management.commands.compute_track_tags import (
    ALLOWED,
    MAX_TAGS_PER_AXIS,
    TAG_SCORE_THRESHOLD,
    VOCAB_HASH,
)
from analysis.models import TagSuggestion, TrackTags

logger = logging.getLogger(__name__)


def merge_tag(tags: dict, axis: str, tag: str, score: float) -> bool:
    """Merge one promoted tag into a TrackTags tags dict in place. Returns True if changed."""
    if score < TAG_SCORE_THRESHOLD:
        return False
    vals = tags.get(axis)
    if not isinstance(vals, dict):  # pre-v2 list rows — left for --refresh-stale
        return False
    if tag in vals:
        return False
    vals[tag] = round(float(score), 2)
    if len(vals) > MAX_TAGS_PER_AXIS:
        kept = sorted(vals.items(), key=lambda ts: -ts[1])[:MAX_TAGS_PER_AXIS]
        tags[axis] = dict(kept)
    return True


class Command(BaseCommand):
    help = (
        "Merge recorded TagSuggestion rows whose tag has been added to ALLOWED "
        "into their tracks' TrackTags (no LLM calls). Run after editing the "
        "vocabulary in compute_track_tags.py; then use compute_track_tags "
        "--refresh-stale to re-tag the rest."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be merged/deleted without writing anything.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        merged = 0
        deleted = 0
        tracks_changed = set()

        for axis, allowed in ALLOWED.items():
            promoted = TagSuggestion.objects.filter(axis=axis, tag__in=allowed)
            for sugg in promoted:
                row = TrackTags.objects.filter(
                    track_id=sugg.track_id, model_name=sugg.model_name
                ).first()
                if row is None or not merge_tag(row.tags, axis, sugg.tag, sugg.score):
                    continue
                merged += 1
                tracks_changed.add(row.pk)
                if not dry_run:
                    row.vocab_hash = VOCAB_HASH
                    row.save(update_fields=["tags", "vocab_hash", "computed_at"])
            # Consume all now-allowed suggestions (including sub-threshold ones —
            # _validate would never keep those) so admin stays a pending-review list.
            deleted += promoted.count()
            if not dry_run:
                promoted.delete()

        prefix = "[dry-run] Would merge" if dry_run else "Merged"
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix} {merged} tag(s) into {len(tracks_changed)} track(s); "
                f"{'would delete' if dry_run else 'deleted'} {deleted} suggestion(s)."
            )
        )
        if merged or deleted:
            self.stdout.write(
                "Run `compute_track_tags --refresh-stale` to re-tag remaining "
                "tracks under the new vocabulary."
            )
