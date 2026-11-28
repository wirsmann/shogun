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


class HiddenGroupTests(unittest.TestCase):
    def test_hides_matching_group_substrings_case_insensitively(self):
        group = "USA TELEMUNDO NETWORK ᴴᴰ/ᴿᴬᵂ ⁶⁰ᶠᵖˢ"
        self.assertTrue(Xtream2m3u.hidden_group(group))
        self.assertTrue(Xtream2m3u.hidden_group("usa telemundo network"))
        self.assertFalse(Xtream2m3u.hidden_group("USA NEWS"))

    def test_additional_substrings_can_be_configured(self):
        with patch.object(Xtream2m3u, "HIDDEN_GROUP_SUBSTRINGS",
                          ("TELEMUNDO NETWORK", "LOCAL SPORTS")):
            self.assertTrue(Xtream2m3u.hidden_group("US LOCAL SPORTS HD"))


if __name__ == "__main__":
    unittest.main()
