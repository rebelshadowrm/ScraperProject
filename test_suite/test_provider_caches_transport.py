import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests
from common.http import create_get_session
from scrapers.pst_scraper import PSTScraper
from scrapers.game7baseball_scraper import Game7BaseballScraper
from scrapers.play365.fivetoolyouth_scraper import FiveToolYouthScraper
from ui import cli


class CacheTransportTests(unittest.TestCase):
    def test_pst_reuses_geocode_preserves_input_fields_and_retries_failed_lookup(self):
        scraper = PSTScraper()
        location = MagicMock(raw={"address": {"road": "Main St", "city": "Austin", "state": "Texas", "postcode": "78701"}})
        scraper.reverse_func = MagicMock(side_effect=[RuntimeError("temporary"), location])
        self.assertEqual(scraper.reverse_geocode(30.1, -97.1), {})
        event = {"Location": "Named Venue", "Latitude": 30.1, "Longitude": -97.1, "StreetAddress": "Supplied street"}
        before = scraper.build_address_and_venue(event)
        after = scraper.build_address_and_venue(event)
        self.assertEqual(before, after)
        self.assertEqual(before["Address"], "Supplied street")
        self.assertEqual(before["City"], "Austin")
        self.assertEqual(scraper.reverse_func.call_count, 2)
        value = scraper.reverse_geocode(30.1, -97.1)
        value["city"] = "Changed"
        self.assertEqual(scraper.reverse_geocode(30.1, -97.1)["city"], "Austin")

    def test_successful_empty_geocode_is_cached(self):
        scraper = PSTScraper()
        scraper.reverse_func = MagicMock(return_value=None)
        self.assertEqual(scraper.reverse_geocode(1, 2), {})
        self.assertEqual(scraper.reverse_geocode(1, 2), {})
        scraper.reverse_func.assert_called_once()

    def test_play365_repeated_state_requests_reuse_index_and_source_replacement_clears_it(self):
        scraper = FiveToolYouthScraper()
        events = [{"url_key": "tx", "state": "TX", "start_date": "07/20/2099", "end_date": "07/22/2099"},
                  {"url_key": "ca", "state": "CA", "start_date": "07/20/2099", "end_date": "07/22/2099"}]
        scraper._load_events_from_blob({"events": events}, "fixture")
        with patch.object(scraper, "_try_load_events_from_api", side_effect=AssertionError("Cached source refetched")):
            first = scraper.collect_event_links(None, "fixture.json?state=TX")
            self.assertEqual(first, scraper.collect_event_links(None, "fixture.json?state=TX"))
            self.assertIn("ca", scraper.collect_event_links(None, "fixture.json?state=CA")[0])
            scraper._load_events_from_blob({"events": []}, "new fixture")
            self.assertEqual(scraper.collect_event_links(None, "fixture.json?state=TX"), [])

    def test_dump_import_requires_completion_domain_and_contiguous_pages(self):
        scraper = FiveToolYouthScraper()
        good = {"domain": scraper.DOMAIN, "seed_params": {"season_ids": "2099"}, "pages": [],
                "completion": {"complete": True, "completed_states": [""]}}
        scraper._validate_complete_dump(good)
        bad = [dict(good, completion=None), dict(good, domain="https://wrong.example"),
               dict(good, completion={"complete": False, "completed_states": [""]}),
               dict(good, completion={"complete": True, "completed_states": ["TX"]}),
               dict(good, pages=[{"state": "", "page": 2, "payload": {"events": []}}]),
               dict(good, pages=[{"state": "", "page": 1, "payload": {"error": "blocked"}}])]
        for blob in bad:
            with self.subTest(blob=blob), self.assertRaises(ValueError):
                scraper._validate_complete_dump(blob)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dump.json"
            path.write_text(json.dumps(good), encoding="utf-8")
            scraper._load_dump_if_needed(str(path))
            self.assertEqual(scraper.collect_event_links(None, f"{path}?state=TX"), [])

    def test_provider_session_closes_after_cli_failure(self):
        scraper = Game7BaseballScraper()
        with patch.object(scraper.session, "close") as close, \
                patch.object(cli, "_run_selected_scraper_unlocked", side_effect=RuntimeError("fixture")):
            with self.assertRaises(RuntimeError):
                cli._run_selected_scraper(scraper=scraper, test_mode=True)
        close.assert_called_once()

    def test_http_connection_reuse_retry_limits_and_large_retry_after(self):
        calls = []
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def do_GET(self):
                calls.append((self.path, self.client_address))
                count = sum(path == self.path for path, _ in calls)
                status = 503 if self.path == "/retry" and count <= 2 else (429 if self.path == "/slow" else 200)
                self.send_response(status)
                if self.path == "/slow":
                    self.send_header("Retry-After", "3600")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        session = create_get_session()
        origin = f"http://127.0.0.1:{server.server_port}"
        try:
            with patch("urllib3.util.retry.time.sleep"):
                self.assertEqual(session.get(origin + "/retry", timeout=(1, 2)).status_code, 200)
            self.assertEqual(sum(path == "/retry" for path, _ in calls), 3)
            session.get(origin + "/one", timeout=(1, 2))
            session.get(origin + "/two", timeout=(1, 2))
            addresses = [addr for path, addr in calls if path in {"/one", "/two"}]
            self.assertEqual(addresses[0], addresses[1])
            with self.assertRaises(requests.exceptions.RequestException):
                session.get(origin + "/slow", timeout=(1, 2))
            self.assertEqual(sum(path == "/slow" for path, _ in calls), 1)
        finally:
            session.close()
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
