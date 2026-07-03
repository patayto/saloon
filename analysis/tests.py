import time
from unittest.mock import patch

import requests
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from analysis.management.commands.backfill_promoted_tags import merge_tag
from analysis.management.commands.compute_track_tags import (
    ALLOWED,
    MAX_TAGS_PER_AXIS,
    TAG_SCORE_THRESHOLD,
    VOCAB_HASH,
    _call_api_with_retry,
    _cooldowns,
    _validate,
)
from analysis.models import TagSuggestion, TrackTags
from spotify.models import Album, Track


class ValidateTagsTests(TestCase):
    def test_threshold_and_unknown_tags(self):
        raw = {
            "mood": {"melancholic": 0.9, "dreamy": 0.4, "not_a_tag": 0.99},
            "theme": {"loss": TAG_SCORE_THRESHOLD},
        }
        out, strays = _validate(raw)
        self.assertEqual(out["mood"], {"melancholic": 0.9})
        self.assertEqual(out["theme"], {"loss": TAG_SCORE_THRESHOLD})
        self.assertEqual(strays["mood"], {"not_a_tag": 0.99})

    def test_missing_and_malformed_axes(self):
        out, strays = _validate({"mood": ["melancholic"], "scene": None})
        self.assertEqual(out, {axis: {} for axis in ALLOWED})
        self.assertEqual(strays, {})

    def test_malformed_scores_dropped(self):
        out, strays = _validate({"mood": {"melancholic": "high", "dark": 0.8}})
        self.assertEqual(out["mood"], {"dark": 0.8})
        self.assertEqual(strays, {})

    def test_cap_keeps_top_scores(self):
        vals = list(ALLOWED["mood"])[: MAX_TAGS_PER_AXIS + 2]
        raw = {"mood": {t: 0.6 + i * 0.01 for i, t in enumerate(vals)}}
        out, _ = _validate(raw)
        self.assertEqual(len(out["mood"]), MAX_TAGS_PER_AXIS)
        self.assertNotIn(vals[0], out["mood"])  # lowest two scores dropped
        self.assertNotIn(vals[1], out["mood"])

    def test_stray_capture_and_normalization(self):
        raw = {
            "mood": {
                "melancholic": 0.9,
                "vulnerability": 0.8,
                "a very long sentence about the song": 0.9,
                "fleeting": 0.4,
                "Slow Burn": 0.7,
            },
            "scene": {"Late Night": 0.8},
        }
        out, strays = _validate(raw)
        self.assertEqual(out["mood"], {"melancholic": 0.9})
        self.assertEqual(strays["mood"], {"vulnerability": 0.8, "slow_burn": 0.7})
        # normalized "Late Night" matches allowed late_night → kept, not a stray
        self.assertEqual(out["scene"], {"late_night": 0.8})
        self.assertNotIn("scene", strays)


class CallApiWithRetryTests(TestCase):
    def setUp(self):
        _cooldowns.clear()

    def _http_error(self, code, headers=None):
        resp = requests.Response()
        resp.status_code = code
        resp.headers.update(headers or {})
        return requests.exceptions.HTTPError(response=resp)

    def test_429_moves_to_next_model_and_sets_cooldown(self):
        calls = []

        def fake_call(msg, model, url):
            calls.append(model)
            if model == "a":
                raise self._http_error(429, {"Retry-After": "30"})
            return {"mood": {}}

        with patch(
            "analysis.management.commands.compute_track_tags._call_api", fake_call
        ):
            result = _call_api_with_retry("msg", ["a", "b"], "url")
        self.assertEqual(result, {"mood": {}})
        self.assertEqual(calls, ["a", "b"])
        self.assertIn("a", _cooldowns)

    def test_cooled_model_skipped(self):
        _cooldowns["a"] = time.time() + 60
        calls = []

        def fake_call(msg, model, url):
            calls.append(model)
            return {}

        with patch(
            "analysis.management.commands.compute_track_tags._call_api", fake_call
        ):
            _call_api_with_retry("msg", ["a", "b"], "url")
        self.assertEqual(calls, ["b"])

    def test_long_retry_after_fails_fast(self):
        def fake_call(msg, model, url):
            raise self._http_error(429, {"Retry-After": "3600"})

        with patch(
            "analysis.management.commands.compute_track_tags._call_api", fake_call
        ):
            start = time.time()
            with self.assertRaises(CommandError):
                _call_api_with_retry("msg", ["a", "b"], "url")
            self.assertLess(time.time() - start, 5)


class MergeTagTests(TestCase):
    def test_below_threshold_skipped(self):
        tags = {"mood": {"dark": 0.8}}
        self.assertFalse(merge_tag(tags, "mood", "vulnerability", 0.5))
        self.assertEqual(tags["mood"], {"dark": 0.8})

    def test_already_present_skipped(self):
        tags = {"mood": {"dark": 0.8}}
        self.assertFalse(merge_tag(tags, "mood", "dark", 0.9))
        self.assertEqual(tags["mood"], {"dark": 0.8})

    def test_list_format_skipped(self):
        tags = {"mood": ["dark"]}
        self.assertFalse(merge_tag(tags, "mood", "vulnerability", 0.9))
        self.assertEqual(tags["mood"], ["dark"])

    def test_merge_and_cap_drops_lowest(self):
        tags = {"mood": {f"t{i}": 0.7 + i * 0.01 for i in range(MAX_TAGS_PER_AXIS)}}
        self.assertTrue(merge_tag(tags, "mood", "vulnerability", 0.9))
        self.assertEqual(len(tags["mood"]), MAX_TAGS_PER_AXIS)
        self.assertIn("vulnerability", tags["mood"])
        self.assertNotIn("t0", tags["mood"])  # lowest score dropped


class BackfillPromotedTagsTests(TestCase):
    def setUp(self):
        album = Album.objects.create(
            id="al1", name="A", album_type="album", uri="u", href="h",
            release_date="2020", release_date_precision="year", total_tracks=1,
        )
        self.track = Track.objects.create(
            id="tr1", name="T", uri="u", href="h", album=album,
            duration_ms=1000, track_number=1,
        )
        self.row = TrackTags.objects.create(
            track=self.track, model_name="m", tags={"theme": {"love": 0.9}}
        )
        TagSuggestion.objects.create(
            track=self.track, axis="theme", tag="vulnerability", score=0.8, model_name="m"
        )

    def test_promotion_round_trip(self):
        promoted = dict(ALLOWED)
        promoted["theme"] = ALLOWED["theme"] + ("vulnerability",)
        with patch(
            "analysis.management.commands.backfill_promoted_tags.ALLOWED", promoted
        ):
            call_command("backfill_promoted_tags")
            self.row.refresh_from_db()
            self.assertEqual(self.row.tags["theme"], {"love": 0.9, "vulnerability": 0.8})
            self.assertEqual(self.row.vocab_hash, VOCAB_HASH)
            self.assertFalse(TagSuggestion.objects.exists())  # consumed
            call_command("backfill_promoted_tags")  # idempotent no-op
            self.row.refresh_from_db()
            self.assertEqual(self.row.tags["theme"], {"love": 0.9, "vulnerability": 0.8})

    def test_dry_run_writes_nothing(self):
        promoted = dict(ALLOWED)
        promoted["theme"] = ALLOWED["theme"] + ("vulnerability",)
        with patch(
            "analysis.management.commands.backfill_promoted_tags.ALLOWED", promoted
        ):
            call_command("backfill_promoted_tags", "--dry-run")
        self.row.refresh_from_db()
        self.assertEqual(self.row.tags["theme"], {"love": 0.9})
        self.assertTrue(TagSuggestion.objects.exists())
