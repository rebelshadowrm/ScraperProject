import json
import shutil
import subprocess
import unittest
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from provider_replay import ROOT, module
from scrapers.play365.fivetool_scraper import FiveToolPlay365Scraper
from scrapers.play365.fivetoolyouth_scraper import FiveToolYouthScraper


NODE = shutil.which("node")


def pages_without_csrf(pages):
    # Credentials changed intentionally; all event data and other acquisition
    # parameters must continue matching the frozen before-change fixture.
    result = []
    for page in pages:
        parsed = urlsplit(page['url'])
        params = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key != '_token']
        result.append({**page, 'url': urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(params), parsed.fragment))})
    return result


@unittest.skipUnless(NODE, "Node is required for generated exporter execution")
class ExporterTests(unittest.TestCase):
    def execute(self, scraper, kind, mode="normal"):
        script = scraper._build_export_script() if kind == "manual" else scraper._build_selenium_export_script()
        request = {"script": script, "kind": kind, "mode": mode, "domain": scraper.DOMAIN}
        result = subprocess.run([NODE, str(Path(__file__).with_name("exporter_replay.js"))],
                                input=json.dumps(request), text=True, capture_output=True, timeout=15, check=True)
        return json.loads(result.stdout)

    def test_exported_pages_match_frozen_exporter_for_get_and_post_configs(self):
        # Baseline source is needed only to regenerate provenance-backed exporter goldens.
        golden = json.loads((Path(__file__).parent / "fixtures/exporter_baseline.json").read_text(encoding="utf-8"))
        from scrapers.play365 import fivetool_scraper, fivetoolyouth_scraper, play24sports_scraper, playpps_scraper
        from scrapers.play365 import playrbi_scraper, prospectwire_scraper, twodsportshighschool_scraper, twodsportsyouth_scraper
        classes = [fivetool_scraper.FiveToolPlay365Scraper, fivetoolyouth_scraper.FiveToolYouthScraper,
                   play24sports_scraper.Play24SportsScraper, playpps_scraper.PlayPPSScraper,
                   playrbi_scraper.PlayRBIScraper, prospectwire_scraper.ProspectWireScraper,
                   twodsportshighschool_scraper.TwoDSportsHighSchoolScraper, twodsportsyouth_scraper.TwoDSportsYouthScraper]
        for cls in classes:
            for kind in ("manual", "selenium"):
                with self.subTest(provider=cls.__name__, kind=kind):
                    scraper = cls()
                    result = self.execute(scraper, kind)
                    self.assertNotIn("error", result)
                    self.assertEqual(pages_without_csrf(result["dump"]["pages"]),
                                     pages_without_csrf(golden[f"{scraper.SCRAPER_NAME}:{kind}"]["pages"]))
                    self.assertTrue(result['requestDetails'])
                    self.assertTrue(all(r['method'] == 'POST' and r['token'] == 'fixture-token' for r in result['requestDetails']))
                    self.assertEqual(result["dump"]["seed_params"], golden[f"{scraper.SCRAPER_NAME}:{kind}"]["seed_params"])
                    scraper._validate_complete_dump(result["dump"])

    def test_exporters_fail_without_successful_dump_on_partial_malformed_repeated_or_hung_source(self):
        for kind in ("manual", "selenium"):
            for mode in ("malformed", "repeat", "middle-failure", "hung"):
                with self.subTest(kind=kind, mode=mode):
                    result = self.execute(FiveToolYouthScraper(), kind, mode)
                    self.assertNotIn("dump", result)
                    self.assertLessEqual(result["requests"], 201)
                    if kind == "selenium":
                        self.assertFalse(result["callback"]["ok"])
                    else:
                        self.assertIn("error", result)

    def test_exporters_allow_verified_empty(self):
        for kind in ("manual", "selenium"):
            scraper = FiveToolPlay365Scraper()
            result = self.execute(scraper, kind, "empty")
            scraper._validate_complete_dump(result["dump"])
            self.assertEqual(result["dump"]["pages"], [])


if __name__ == "__main__":
    unittest.main()
