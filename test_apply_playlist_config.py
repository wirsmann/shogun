import json
import tempfile
import unittest
from pathlib import Path

from apply_playlist_config import ConfigError, apply_config, stable_channel_id


def edit(match, **changes):
    return {"id": stable_channel_id(match), "match": match, "set": changes}


def identity(occurrence=0, epg_source="", tvg_id="channel.test"):
    return {
        "tvg_id": tvg_id,
        "tvg_name": "Test Channel",
        "group_title": "Test Group",
        "title": "Test Channel",
        "epg_source": epg_source,
        "occurrence": occurrence,
    }


class ApplyPlaylistConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.playlist = self.root / "wes.m3u"
        self.config = self.root / "edits.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def write_config(self, channels, playlist="wes.m3u"):
        self.config.write_text(
            json.dumps({"version": 1, "playlist": playlist, "channels": channels}),
            encoding="utf-8",
        )

    def test_updates_only_selected_duplicate_and_preserves_line_endings(self):
        header = (
            '#EXTINF:-1 tvg-id="channel.test" tvg-name="Test Channel" '
            'group-title="Test Group",Test Channel\r\n'
        )
        self.playlist.write_bytes(
            ("#EXTM3U\r\n" + header + "https://one.invalid/stream\r\n" +
             header + "https://two.invalid/stream\r\n").encode("utf-8")
        )
        self.write_config([
            edit(identity(1), name="Edited Channel", tvg_id="guide.test", epg_source="segment-ab12")
        ])

        self.assertEqual(apply_config(self.playlist, self.config), (1, 0))
        result = self.playlist.read_bytes().decode("utf-8")
        lines = result.splitlines()
        self.assertIn('tvg-name="Test Channel"', lines[1])
        self.assertIn('tvg-name="Edited Channel"', lines[3])
        self.assertIn('tvg-id="guide.test"', lines[3])
        self.assertIn('epg-source="segment-ab12"', lines[3])
        self.assertIn("channel-studio-id=", lines[3])
        self.assertEqual(lines[2], "https://one.invalid/stream")
        self.assertEqual(lines[4], "https://two.invalid/stream")
        self.assertTrue(result.endswith("\r\n"))

    def test_can_clear_epg_id_and_restore_primary_source(self):
        self.playlist.write_text(
            '#EXTM3U\n#EXTINF:-1 tvg-id="guide.test" tvg-name="Test Channel" '
            'group-title="Test Group" epg-source="segment-ab12",Test Channel\n'
            "https://stream.invalid/live\n",
            encoding="utf-8",
        )
        self.write_config([
            edit(identity(epg_source="segment-ab12", tvg_id="guide.test"), tvg_id="", epg_source="epgstrong")
        ])

        apply_config(self.playlist, self.config)
        result = self.playlist.read_text(encoding="utf-8")
        self.assertIn('tvg-id=""', result)
        self.assertNotIn("epg-source=", result)

    def test_removes_only_selected_duplicate_and_its_channel_directives(self):
        header = (
            '#EXTINF:-1 tvg-id="channel.test" tvg-name="Test Channel" '
            'group-title="Test Group",Test Channel\r\n'
        )
        self.playlist.write_bytes(
            ("#EXTM3U\r\n" + header + "#EXTVLCOPT:option=first\r\n" +
             "https://one.invalid/stream\r\n" + header +
             "#EXTVLCOPT:option=second\r\nhttps://two.invalid/stream\r\n").encode("utf-8")
        )
        match = identity(1)
        self.write_config([{
            "id": stable_channel_id(match),
            "match": match,
            "set": {},
            "remove": True,
        }])

        self.assertEqual(apply_config(self.playlist, self.config), (1, 0))
        result = self.playlist.read_bytes().decode("utf-8")
        self.assertEqual(result.count("#EXTINF:"), 1)
        self.assertIn("#EXTVLCOPT:option=first", result)
        self.assertNotIn("#EXTVLCOPT:option=second", result)
        self.assertIn("https://one.invalid/stream", result)
        self.assertNotIn("https://two.invalid/stream", result)
        self.assertTrue(result.endswith("\r\n"))

    def test_rejects_invalid_channel_removal_flag(self):
        self.playlist.write_text(
            '#EXTM3U\n#EXTINF:-1 tvg-id="channel.test" tvg-name="Test Channel" '
            'group-title="Test Group",Test Channel\nhttps://stream.invalid/live\n',
            encoding="utf-8",
        )
        match = identity()
        self.write_config([{
            "id": stable_channel_id(match),
            "match": match,
            "set": {},
            "remove": "true",
        }])

        with self.assertRaises(ConfigError):
            apply_config(self.playlist, self.config)

    def test_rejects_removal_when_channel_has_no_stream_url(self):
        original = (
            '#EXTM3U\n#EXTINF:-1 tvg-id="channel.test" tvg-name="Test Channel" '
            'group-title="Test Group",Test Channel\n'
            '#EXTINF:-1 tvg-id="next.test" tvg-name="Next Channel" '
            'group-title="Test Group",Next Channel\nhttps://stream.invalid/next\n'
        )
        self.playlist.write_text(original, encoding="utf-8")
        match = identity()
        self.write_config([{
            "id": stable_channel_id(match),
            "match": match,
            "set": {},
            "remove": True,
        }])

        with self.assertRaises(ConfigError):
            apply_config(self.playlist, self.config)
        self.assertEqual(self.playlist.read_text(encoding="utf-8"), original)

    def test_rejects_stale_channel_match_without_modifying_playlist(self):
        original = (
            '#EXTM3U\n#EXTINF:-1 tvg-id="different" tvg-name="Other" '
            'group-title="Test Group",Other\nhttps://stream.invalid/live\n'
        )
        self.playlist.write_text(original, encoding="utf-8")
        self.write_config([edit(identity(), name="Edited Channel")])

        with self.assertRaises(ConfigError):
            apply_config(self.playlist, self.config)
        self.assertEqual(self.playlist.read_text(encoding="utf-8"), original)

    def test_wildcard_config_applies_matches_and_reports_channels_missing_from_playlist(self):
        self.playlist = self.root / "josh.m3u"
        self.playlist.write_text(
            '#EXTM3U\n#EXTINF:-1 tvg-id="channel.test" tvg-name="Test Channel" '
            'group-title="Test Group",Test Channel\nhttps://stream.invalid/live\n',
            encoding="utf-8",
        )
        self.write_config([
            edit(identity(), name="Edited Channel"),
            edit(identity(tvg_id="missing.channel"), name="Not Present"),
        ], playlist="*")

        self.assertEqual(apply_config(self.playlist, self.config), (1, 1))
        result = self.playlist.read_text(encoding="utf-8")
        self.assertIn('tvg-name="Edited Channel"', result)
        self.assertNotIn("Not Present", result)


if __name__ == "__main__":
    unittest.main()
