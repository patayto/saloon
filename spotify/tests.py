import tempfile
from unittest import mock

import requests
from django.test import SimpleTestCase

from spotify.audio_features.reccobeats import _MAX_429_RETRIES, ReccoBeatsProvider

_FEATURES = {"acousticness": 0.1, "danceability": 0.2, "energy": 0.3, "tempo": 120.0}


def _resp(status, json_data=None, headers=None):
    resp = mock.Mock()
    resp.status_code = status
    resp.headers = headers or {}
    resp.json.return_value = json_data or {}
    if status >= 400:
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError(str(status))
    else:
        resp.raise_for_status.return_value = None
    return resp


class FetchFromFile429RetryTests(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".mp3")
        self.addCleanup(self.tmp.close)
        self.provider = ReccoBeatsProvider()

    def test_retries_on_429_then_succeeds(self):
        responses = [_resp(429), _resp(429), _resp(200, _FEATURES)]
        with mock.patch("spotify.audio_features.reccobeats.requests.post", side_effect=responses) as post, \
             mock.patch("spotify.audio_features.reccobeats.time.sleep") as sleep:
            result = self.provider.fetch_from_file(self.tmp.name, "t1")
        self.assertIsNotNone(result)
        self.assertEqual(result.tempo, 120.0)
        self.assertEqual(post.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_gives_up_after_max_retries(self):
        responses = [_resp(429)] * (_MAX_429_RETRIES + 1)
        with mock.patch("spotify.audio_features.reccobeats.requests.post", side_effect=responses) as post, \
             mock.patch("spotify.audio_features.reccobeats.time.sleep"):
            result = self.provider.fetch_from_file(self.tmp.name, "t1")
        self.assertIsNone(result)
        self.assertEqual(post.call_count, _MAX_429_RETRIES + 1)

    def test_honours_retry_after_header(self):
        responses = [_resp(429, headers={"Retry-After": "7"}), _resp(200, _FEATURES)]
        with mock.patch("spotify.audio_features.reccobeats.requests.post", side_effect=responses), \
             mock.patch("spotify.audio_features.reccobeats.time.sleep") as sleep:
            self.provider.fetch_from_file(self.tmp.name, "t1")
        sleep.assert_called_once_with(7.0)
