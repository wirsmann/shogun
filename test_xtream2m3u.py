import unittest
from unittest.mock import patch

import Xtream2m3u


class TidyGroupTests(unittest.TestCase):
    def test_replaces_country_prefixes_in_group_titles(self):
        examples = {
            "CA| GENERAL EN": "CANADA GENERAL",
            "US| SPORTS": "USA SPORTS",
            "UK| NEWS": "UK NEWS",
        }
        for original, expected in examples.items():
            with self.subTest(original=original):
                self.assertEqual(Xtream2m3u.tidy_group(original), expected)

    def test_custom_replacement_can_be_added(self):
        with patch.dict(Xtream2m3u.GROUP_REPLACEMENTS, {"NZ|": "NEW ZEALAND"}):
            self.assertEqual(Xtream2m3u.tidy_group("NZ| SPORTS"), "NEW ZEALAND SPORTS")


if __name__ == "__main__":
    unittest.main()
