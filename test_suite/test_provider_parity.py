import json
import unittest
from pathlib import Path

from provider_replay import replay


class ProviderParityTests(unittest.TestCase):
    def test_current_outputs_match_frozen_before_change_outputs(self):
        baseline = json.loads((Path(__file__).parent / "fixtures/provider_baseline.json").read_text(encoding="utf-8"))
        current = replay()
        self.assertEqual(set(baseline["data"]), set(current["data"]))
        for provider in baseline["data"]:
            with self.subTest(provider=provider):
                self.assertEqual(baseline["data"][provider], current["data"][provider])
        self.assertEqual(baseline["metrics"]["USSSA"]["loads"], 2)
        self.assertEqual(current["metrics"]["USSSA"]["loads"], 1)
        self.assertEqual(baseline["metrics"]["PerfectGame"]["loads"], 4)
        self.assertEqual(current["metrics"]["PerfectGame"]["loads"], 2)
        self.assertEqual(baseline["metrics"]["PST"]["geocode_lookups"], 3)
        self.assertEqual(current["metrics"]["PST"]["geocode_lookups"], 1)


if __name__ == "__main__":
    unittest.main()
