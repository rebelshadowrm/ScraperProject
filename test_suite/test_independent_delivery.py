import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from common.delivery import DeliveryStore
from common.storage import save_json_atomic
from scripts.sync_destinations import run


def entry(sport="baseball", *, provider="FixtureScraper", name="Fall Classic", **extra):
    return dict(scraper=provider, sport=sport, url="https://example.org/events/42",
        processed=True, export_status="exported", event_data={
            "Tournament-name": name, "Start-date": "10/20/2099", "End-date": "10/21/2099",
            "State-FullName": "Texas", "Website-url": "https://example.org/events/42",
            "Age-groups.1": "12u", "Entry-fee.1": "$450"}, **extra)


def response(body, status=200):
    result = Mock(status_code=status)
    result.iter_content.return_value = [json.dumps(body).encode()]
    return result


def committed(**kwargs):
    return dict(kwargs["json"], status="committed")


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.db"
        self.store = DeliveryStore(self.path)
        self.addCleanup(lambda: self.store.close())
        self.ledger = {"events": {"FixtureScraper|42": entry(),
                                  "FixtureScraper|softball|42": entry("softball")}}

    def send(self, destination="baseball_connected", side_effect=None):
        session = Mock()
        session.post.side_effect = side_effect or (lambda *a, **kw: response(committed(**kw)))
        self.store.send(destination, "https://example.org/import", "test-token", session=session)
        return session

    def test_excel_processed_status_cannot_suppress_website_and_sports_are_independent(self):
        self.store.ingest(self.ledger)
        session = self.send()
        self.assertEqual(session.post.call_args.kwargs["json"]["sport"], "baseball")
        self.assertEqual(self.store.summary()["baseball_connected"]["delivered"], 1)
        self.assertEqual(self.store.summary()["softball_connected"]["pending"], 1)
        self.ledger["events"]["FixtureScraper|42"].update(processed=False, export_status="pending")
        self.store.ingest(self.ledger)
        self.assertEqual(self.store.summary()["baseball_connected"]["delivered"], 1)
        self.send("softball_connected")
        self.assertEqual(self.store.summary()["softball_connected"]["delivered"], 1)

    def test_changed_revision_requeues_only_affected_destination_and_replay_is_stable(self):
        self.store.ingest(self.ledger)
        first = self.send().post.call_args.kwargs["json"]
        self.ledger["events"]["FixtureScraper|42"]["event_data"]["Entry-fee.1"] = "$500"
        self.store.ingest(self.ledger)
        second = self.send().post.call_args.kwargs["json"]
        self.assertEqual(second["sequence"], 2)
        self.assertNotEqual(first["revision"], second["revision"])
        self.store.requeue("baseball_connected")
        replay = self.send().post.call_args.kwargs["json"]
        self.assertEqual(replay, second)
        self.assertEqual(self.store.summary()["softball_connected"]["pending"], 1)

    def test_local_export_does_not_acknowledge(self):
        self.store.ingest(self.ledger)
        self.store.export("baseball_connected", Path(self.temp.name) / "export.json")
        self.assertEqual(self.store.summary()["baseball_connected"]["pending"], 1)

    def test_receipts_cannot_be_reused_at_another_receiver(self):
        self.store.ingest(self.ledger)
        self.send()
        session = Mock()
        with self.assertRaises(ValueError):
            self.store.send("baseball_connected", "https://other.example.org/import", "token", session=session)
        session.post.assert_not_called()
        self.assertEqual(self.store.summary()["baseball_connected"]["delivered"], 1)

    def test_revision_history_preserves_reverted_content_without_rewinding_delivery(self):
        key = "FixtureScraper|42"
        self.store.ingest(self.ledger)
        first = self.send().post.call_args.kwargs["json"]
        self.ledger["events"][key]["event_data"]["Entry-fee.1"] = "$500"
        self.store.ingest(self.ledger)
        self.send()
        # Restoring older source content is a new desired version, never a replay
        # of its old receipt, and does not mark Excel exported again.
        self.ledger["events"][key]["event_data"]["Entry-fee.1"] = "$450"
        self.store.ingest(self.ledger)
        restored = self.store.envelope(self.store.pending("baseball_connected")[0])
        self.assertEqual(restored["revision"], first["revision"])
        self.assertEqual(restored["sequence"], 3)
        self.assertNotEqual(restored["idempotency_key"], first["idempotency_key"])
        versions = self.store.history(key)
        self.assertEqual([v["sequence"] for v in versions], [1, 2, 3])
        self.assertEqual([v["payload"]["fields"]["Entry-fee.1"] for v in versions], ["$450", "$500", "$450"])
        self.assertTrue(self.ledger["events"][key]["processed"])
        self.assertEqual(len(self.store.history("FixtureScraper|softball|42")), 1)
        self.send()
        self.store.requeue("baseball_connected")
        self.send()
        self.assertEqual(len(self.store.history(key)), 3)

    def test_revision_history_migration_preserves_existing_receipts(self):
        self.store.ingest(self.ledger)
        self.send()
        original = self.store.summary()
        with self.store.transaction():
            self.store.db.execute("DROP TABLE event_revisions")
        self.store.close()
        self.store = DeliveryStore(self.path)
        self.assertEqual(self.store.summary(), original)
        versions = self.store.history("FixtureScraper|42")
        self.assertEqual(len(versions), 1)
        self.assertIsNone(versions[0]["recorded_at"])
        self.store.ingest(self.ledger)
        self.assertEqual(len(self.store.history("FixtureScraper|42")), 1)

    def test_history_failure_rolls_back_payload_and_outbox_together(self):
        self.store.ingest(self.ledger)
        self.send()
        with self.store.transaction():
            self.store.db.execute("""CREATE TRIGGER reject_history BEFORE INSERT ON event_revisions
                BEGIN SELECT RAISE(ABORT, 'injected history failure'); END""")
        self.ledger["events"]["FixtureScraper|42"]["event_data"]["Entry-fee.1"] = "$999"
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.ingest(self.ledger)
        self.assertEqual(self.store.summary()["baseball_connected"]["delivered"], 1)
        self.assertEqual(self.store.db.execute("SELECT sequence FROM events WHERE event_id='FixtureScraper|42'").fetchone()[0], 1)
        self.assertEqual(len(self.store.history("FixtureScraper|42")), 1)

    def test_registered_additional_sport_has_its_own_identity_history_and_queue(self):
        from common.identity import event_identity
        from common.delivery import DESTINATIONS
        scraper = type("FixtureScraper", (), {"SPORT": "tennis", "get_event_identity": lambda self, url: "42"})()
        with patch("common.sport.SPORTS", {"baseball", "softball", "tennis"}), \
                patch.dict(DESTINATIONS, {"tennis_connected": "tennis"}):
            key = event_identity(scraper, "https://example.org/events/42")
            self.assertEqual(key, "FixtureScraper|tennis|42")
            self.ledger["events"][key] = entry("tennis")
            self.store.ingest(self.ledger)
            body = self.store.envelope(self.store.pending("tennis_connected")[0])
            self.assertEqual(body["source_id"], "42")
            self.assertEqual(body["sport"], "tennis")
            self.send("tennis_connected")
            summary = self.store.summary()
            self.assertEqual(summary["tennis_connected"]["delivered"], 1)
            self.assertEqual(summary["baseball_connected"]["pending"], 1)
            self.assertEqual(summary["softball_connected"]["pending"], 1)
            self.assertEqual(len(self.store.history(key)), 1)

    def test_softball_requires_scoped_identity_and_source_id_is_not_scope_prefix(self):
        self.store.ingest(self.ledger)
        body=self.store.envelope(self.store.pending('softball_connected')[0])
        self.assertEqual(body['source_id'],'42')
        ambiguous={'events':{'FixtureScraper|unscoped':entry('softball')}}
        self.assertEqual(self.store.ingest(ambiguous)['quarantined'],['FixtureScraper|unscoped'])

    def test_insecure_http_is_available_only_for_explicit_literal_loopback_test(self):
        self.store.ingest(self.ledger)
        session=Mock()
        session.post.side_effect=lambda *args,**kwargs:response(committed(**kwargs))
        for endpoint in ['http://127.0.0.1/import','http://example.org/import','http://127.0.0.1.attacker.test/import']:
            with self.assertRaises(ValueError):self.store.send('softball_connected',endpoint,'fixture-token',session=session)
        with self.assertRaises(ValueError):self.store.send('softball_connected','http://127.0.0.1.attacker.test/import','fixture-token',session=session,allow_loopback=True)
        self.store.send('softball_connected','http://127.0.0.1/import','fixture-token',session=session,allow_loopback=True)
        self.assertEqual(self.store.summary()['softball_connected']['delivered'],1)

    def test_http_success_without_exact_durable_ack_remains_unknown(self):
        for mutation in (lambda body: {}, lambda body: dict(body, status="accepted"),
                         lambda body: dict(body, status="committed", destination="softball_connected"),
                         lambda body: dict(body, status="committed", revision="old"),
                         lambda body: dict(body, status="committed", sequence=0),
                         lambda body: dict(body, status="committed", sequence=True),
                         lambda body: dict(body, status="committed", idempotency_key="wrong")):
            with self.subTest(mutation=mutation):
                self.store.ingest(self.ledger)
                self.store.requeue("baseball_connected")
                self.send(side_effect=lambda *a, **kw: response(mutation(kw["json"])))
                self.assertEqual(self.store.summary()["baseball_connected"]["unknown"], 1)

    def test_unknown_timeout_survives_restart_and_exact_replay(self):
        self.store.ingest(self.ledger)
        session = self.send(side_effect=requests.Timeout("do not log credentials"))
        body = session.post.call_args.kwargs["json"]
        self.assertEqual(self.store.pending("baseball_connected"), [])
        self.store.close()
        self.store = DeliveryStore(self.path)
        self.store.requeue("baseball_connected")
        replay = self.send().post.call_args.kwargs["json"]
        self.assertEqual(body, replay)
        self.assertEqual(self.store.summary()["baseball_connected"]["delivered"], 1)

    def test_interruption_after_request_staging_is_retryable(self):
        self.store.ingest(self.ledger)
        with self.assertRaises(KeyboardInterrupt):
            self.send(side_effect=KeyboardInterrupt())
        self.assertEqual(self.store.summary()["baseball_connected"]["unknown"], 1)
        self.store.requeue("baseball_connected")
        self.send()
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM attempts WHERE finished_at IS NULL").fetchone()[0], 1)
        self.assertEqual(self.store.summary()["baseball_connected"]["delivered"], 1)

    def test_stale_ack_cannot_acknowledge_newer_desired_revision(self):
        self.store.ingest(self.ledger)
        def during_request(*args, **kwargs):
            self.ledger["events"]["FixtureScraper|42"]["event_data"]["Entry-fee.1"] = "$999"
            self.store.ingest(self.ledger)
            return response(committed(**kwargs))
        self.send(side_effect=during_request)
        self.assertEqual(self.store.summary()["baseball_connected"]["pending"], 1)

    def test_provider_identity_collisions_never_merge(self):
        self.ledger["events"]["OtherScraper|42"] = entry(provider="OtherScraper")
        self.store.ingest(self.ledger)
        self.assertEqual(len(self.store.pending("baseball_connected")), 2)

    def test_explicit_deletion_and_expiry_withdraw_but_absence_does_not(self):
        self.store.ingest(self.ledger)
        self.send()
        self.store.ingest({"events": {}})
        self.assertEqual(self.store.summary()["baseball_connected"]["delivered"], 1)
        self.ledger["events"]["FixtureScraper|42"]["is_deleted"] = True
        self.store.ingest(self.ledger)
        body = self.send().post.call_args.kwargs["json"]
        self.assertEqual(body["operation"], "withdraw")
        self.assertEqual(body["sequence"], 2)
        self.ledger["events"]["FixtureScraper|softball|42"]["event_data"].update({"Start-date": "01/01/2001", "End-date": "01/02/2001"})
        self.store.ingest(self.ledger)
        self.assertEqual(self.store.envelope(self.store.pending("softball_connected")[0])["operation"], "withdraw")

    def test_ambiguous_legacy_sport_is_quarantined(self):
        del self.ledger["events"]["FixtureScraper|42"]["sport"]
        self.assertIn("FixtureScraper|42", self.store.ingest(self.ledger)["quarantined"])

    def test_mixed_feed_requires_source_sport_evidence_even_after_excel_export(self):
        key = "PlayPPSScraper|42"
        record = entry(provider="PlayPPSScraper")
        ledger = {"events": {key: record}}
        self.assertEqual(self.store.ingest(ledger)["quarantined"], [key])
        self.assertTrue(record["processed"])
        record.update(source_sport_id=1, sport_verified=True)
        self.store.ingest(ledger)
        self.assertEqual(len(self.store.pending("baseball_connected")), 1)
        record.update(sport_verified=False, source_sport_id=2)
        self.store.ingest(ledger)
        self.assertEqual(self.store.pending("baseball_connected"), [])
        self.store.requeue("baseball_connected")
        self.assertEqual(self.store.pending("baseball_connected"), [])
        self.assertEqual(self.store.summary()["baseball_connected"]["quarantined"], 1)
        record.update(sport_verified=True, source_sport_id=1)
        self.store.ingest(ledger)
        self.assertEqual(len(self.store.pending("baseball_connected")), 1)

    def test_fastpitch_url_cannot_be_stamped_into_baseball_delivery(self):
        record = self.ledger["events"]["FixtureScraper|42"]
        record.update(url="https://baseball.example.org/events/fastpitch-classic", sport_verified=True)
        self.assertIn("FixtureScraper|42", self.store.ingest(self.ledger)["quarantined"])

    def test_endpoint_errors_are_isolated_and_tokens_are_not_reported(self):
        ledger = Path(self.temp.name) / "events.json"
        save_json_atomic(self.ledger, ledger)
        state = Path(self.temp.name) / "separate.db"
        config = {key: {"enabled": True, "endpoint": "https://example.org/import", "token_env": key.upper()}
                  for key in ("baseball_connected", "softball_connected")}
        def send(store, destination, *args, **kwargs):
            if destination == "baseball_connected":
                raise ValueError("secret-value")
            self.assertEqual(destination, "softball_connected")
        with patch.object(DeliveryStore, "send", autospec=True, side_effect=send) as method:
            report = run(ledger=ledger, state=state, config=config)
        self.assertEqual(method.call_count, 2)
        self.assertNotIn("secret-value", json.dumps(report))
        self.assertEqual(report["status"], "partial")

    def test_dry_run_writes_nothing(self):
        root = Path(self.temp.name) / "nonexistent"
        report = run(ledger=root / "events.json", state=root / "state.db", dry_run=True)
        self.assertEqual(report["network_requests"], 0)
        self.assertFalse(root.exists())

    def test_selected_destination_status_excludes_other_destinations_pending_work(self):
        ledger = Path(self.temp.name) / "events.json"
        save_json_atomic(self.ledger, ledger)
        state = Path(self.temp.name) / "selected.db"
        def send(store, destination, *args, **kwargs):
            with store.transaction():
                store.db.execute("UPDATE deliveries SET status='delivered' WHERE destination=?", (destination,))
        with patch.object(DeliveryStore, "send", autospec=True, side_effect=send):
            report = run(ledger=ledger, state=state, destination="baseball_connected",
                         config={"baseball_connected": {"enabled": True, "endpoint": "https://example.org/import", "token_env": "TOKEN"}})
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["destinations"]["softball_connected"]["pending"], 1)

    def test_acknowledgement_checkpoint_failure_replays_same_key(self):
        self.store.ingest(self.ledger)
        self.store.db.execute("""CREATE TRIGGER reject_ack BEFORE UPDATE ON deliveries
            WHEN NEW.status='delivered' BEGIN SELECT RAISE(ABORT,'checkpoint unavailable'); END""")
        with self.assertRaises(Exception):
            self.send()
        self.assertEqual(self.store.summary()["baseball_connected"]["unknown"], 1)
        self.store.db.execute("DROP TRIGGER reject_ack")
        self.store.requeue("baseball_connected")
        self.send()
        self.assertEqual(self.store.summary()["baseball_connected"]["delivered"], 1)


if __name__ == "__main__":
    unittest.main()
