import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from scrapers.play365.fivetoolyouth_scraper import FiveToolYouthScraper
from scrapers.play365_base import Play365ManualFallbackRequired


def reply(payload, text='{}'):
    return MagicMock(text=text, json=lambda: payload)


EVENT = {'url_key': 'classic', 'name': 'Classic', 'state': 'TX',
         'start_date': '2099-07-20', 'end_date': '2099-07-22'}


class Play365ProtocolTests(unittest.TestCase):
    def test_refresh_overwrites_cached_token_before_post_and_closes_session(self):
        scraper, session = FiveToolYouthScraper(), MagicMock()
        session.get.side_effect = [reply({}, '<input type="hidden" name="_token" value="cached-token">'),
                                   reply({'csrf_token': 'fresh-token'})]
        session.post.side_effect = [reply({'eventlist': [EVENT]}), reply({'eventlist': []})]
        with patch('scrapers.play365_base.requests.Session', return_value=session):
            self.assertTrue(scraper._try_load_events_from_api())
        self.assertEqual(session.get.call_args.args[0], scraper.DOMAIN + '/refresh-csrf')
        self.assertEqual(session.post.call_count, 2)
        for call in session.post.call_args_list:
            self.assertEqual(call.kwargs['data']['_token'], 'fresh-token')
            self.assertEqual(call.kwargs['data']['past_events'], 'false')
        self.assertTrue(scraper._all_events_cache)
        self.assertTrue(all(event == EVENT for event in scraper._all_events_cache))
        session.close.assert_called_once()

    def test_missing_refreshed_token_does_not_post_or_cache_empty_success(self):
        scraper, session = FiveToolYouthScraper(), MagicMock()
        session.get.side_effect = [reply({}), reply({})]
        with patch('scrapers.play365_base.requests.Session', return_value=session):
            self.assertFalse(scraper._try_load_events_from_api())
        session.post.assert_not_called()
        self.assertFalse(scraper._events_loaded)
        session.close.assert_called_once()

    def test_partial_post_source_does_not_load_success_cache(self):
        scraper, session = FiveToolYouthScraper(), MagicMock()
        session.get.side_effect = [reply({}), reply({'csrf_token': 'fresh-token'})]
        session.post.side_effect = [reply({'eventlist': [EVENT]}), RuntimeError('later page unavailable')]
        with patch('scrapers.play365_base.requests.Session', return_value=session):
            self.assertFalse(scraper._try_load_events_from_api())
        self.assertFalse(scraper._events_loaded)
        self.assertEqual(scraper._all_events_cache, [])

    def test_verified_fresh_dump_is_usable_without_manual_prompt_after_api_failure(self):
        scraper = FiveToolYouthScraper()
        dump = {'domain': scraper.DOMAIN, 'seed_params': {},
                'completion': {'complete': True, 'completed_states': ['']},
                'pages': [{'state': '', 'page': 1, 'payload': {'eventlist': [EVENT]}}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'dump.json'
            path.write_text(json.dumps(dump), encoding='utf-8')
            with patch.object(scraper, '_try_load_events_from_api', return_value=False), \
                 patch('builtins.input', side_effect=AssertionError('Unattended prompt')):
                links = scraper.collect_event_links(None, str(path) + '?state=TX', allow_manual_fallback=False)
            self.assertEqual(links, [scraper.DOMAIN + '/events/classic'])
            scraper = FiveToolYouthScraper()
            path.write_text('{}', encoding='utf-8')
            with patch.object(scraper, '_try_load_events_from_api', return_value=False), self.assertRaises(Play365ManualFallbackRequired):
                scraper.collect_event_links(None, str(path) + '?state=TX', allow_manual_fallback=False)
