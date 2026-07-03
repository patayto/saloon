from django.test import TestCase

from analysis.management.commands.compute_track_tags import (
    ALLOWED,
    MAX_TAGS_PER_AXIS,
    TAG_SCORE_THRESHOLD,
    _validate,
)


class ValidateTagsTests(TestCase):
    def test_threshold_and_unknown_tags(self):
        raw = {
            "mood": {"melancholic": 0.9, "dreamy": 0.4, "not_a_tag": 0.99},
            "theme": {"loss": TAG_SCORE_THRESHOLD},
        }
        out = _validate(raw)
        self.assertEqual(out["mood"], {"melancholic": 0.9})
        self.assertEqual(out["theme"], {"loss": TAG_SCORE_THRESHOLD})

    def test_missing_and_malformed_axes(self):
        out = _validate({"mood": ["melancholic"], "scene": None})
        self.assertEqual(out, {axis: {} for axis in ALLOWED})

    def test_malformed_scores_dropped(self):
        out = _validate({"mood": {"melancholic": "high", "dark": 0.8}})
        self.assertEqual(out["mood"], {"dark": 0.8})

    def test_cap_keeps_top_scores(self):
        vals = list(ALLOWED["mood"])[: MAX_TAGS_PER_AXIS + 2]
        raw = {"mood": {t: 0.6 + i * 0.01 for i, t in enumerate(vals)}}
        out = _validate(raw)
        self.assertEqual(len(out["mood"]), MAX_TAGS_PER_AXIS)
        self.assertNotIn(vals[0], out["mood"])  # lowest two scores dropped
        self.assertNotIn(vals[1], out["mood"])
