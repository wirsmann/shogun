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
        self.config = self.root / "wes-edits.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def write_config(self, channels):
        self.config.write_text(
            json.dumps({"version": 1, "playlist": "wes.m3u", "channels": channels}),
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

        self.assertEqual(apply_config(self.playlist, self.config), 1)
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


if __name__ == "__main__":
    unittest.main()
