"""Transactional canonical revisions and independent website outboxes.

Excel remains owned by events.json and the existing workbook commit path.
This store never reads processed/export_status as website acknowledgements.
"""

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import requests

from common.config import OUTPUT_COLUMNS
from common.sport import payload_revision
from common.validation import is_future_event, validate_event_data

DESTINATIONS = {"baseball_connected": "baseball", "softball_connected": "softball"}
LEGACY_BASEBALL_PROVIDERS = {
    "USSSAScraper", "NCSScraper", "PGScraper", "TripleCrownScraper", "TopGunScraper",
    "PSTScraper", "Game7BaseballScraper", "GrandSlamScraper", "RipkenBaseballScraper",
    "FiveToolPlay365Scraper", "FiveToolYouthScraper", "Play24SportsScraper", "PlayPPSScraper",
    "PlayRBIScraper", "ProspectWireScraper", "TwoDSportsYouthScraper", "TwoDSportsHighSchoolScraper",
}
UNTYPED_PROVIDERS = {
    "PSTScraper", "FiveToolPlay365Scraper", "FiveToolYouthScraper", "Play24SportsScraper",
    "PlayPPSScraper", "PlayRBIScraper", "ProspectWireScraper", "TwoDSportsYouthScraper", "TwoDSportsHighSchoolScraper",
}


class DeliveryStore:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY, sport TEXT NOT NULL,
                revision TEXT NOT NULL, sequence INTEGER NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS deliveries (
                destination TEXT NOT NULL, event_id TEXT NOT NULL,
                desired_revision TEXT NOT NULL, desired_sequence INTEGER NOT NULL,
                delivered_revision TEXT, delivered_sequence INTEGER,
                status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                retry_at REAL NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
                acknowledged_at REAL, PRIMARY KEY(destination, event_id));
            CREATE TABLE IF NOT EXISTS attempts (
                id INTEGER PRIMARY KEY, destination TEXT NOT NULL, event_id TEXT NOT NULL,
                revision TEXT NOT NULL, sequence INTEGER NOT NULL,
                started_at REAL NOT NULL, finished_at REAL, result TEXT);
            CREATE TABLE IF NOT EXISTS event_revisions (
                event_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                sport TEXT NOT NULL, revision TEXT NOT NULL, payload TEXT NOT NULL,
                recorded_at REAL, PRIMARY KEY(event_id, sequence));
            CREATE TABLE IF NOT EXISTS receiver_targets (
                destination TEXT PRIMARY KEY, endpoint TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS delivery_receipts (
                destination TEXT NOT NULL,event_id TEXT NOT NULL,sequence INTEGER NOT NULL,
                revision TEXT NOT NULL,idempotency_key TEXT NOT NULL,acknowledged_at REAL NOT NULL,
                receipt TEXT NOT NULL,PRIMARY KEY(destination,event_id,sequence));
        """)
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(attempts)')}
        for name,kind in (('http_status','INTEGER'),('failure_kind','TEXT')):
            if name not in columns:self.db.execute(f'ALTER TABLE attempts ADD COLUMN {name} {kind}')
        self.db.commit()
        # Existing stores only retained their latest payload. Preserve that known
        # version without inventing earlier history or its observation time.
        with self.transaction():
            self.db.execute("""INSERT INTO event_revisions
                (event_id,sequence,sport,revision,payload,recorded_at)
                SELECT event_id,sequence,sport,revision,payload,NULL FROM events
                WHERE NOT EXISTS (SELECT 1 FROM event_revisions r
                    WHERE r.event_id=events.event_id AND r.sequence=events.sequence)""")

    def close(self):
        self.db.close()

    def history(self, event_id):
        """Read retained versions; restoring content must create a new sequence.

        This never rewinds acknowledgements or changes the acquisition ledger.
        Legacy stores expose only versions still available at migration time.
        """
        return [dict(row, payload=json.loads(row["payload"])) for row in
                self.db.execute("SELECT * FROM event_revisions WHERE event_id=? ORDER BY sequence", (event_id,))]

    def bind_target(self, destination, endpoint, *, adopt_existing_receipts=False):
        """Keep a destination's acknowledgements scoped to one physical receiver.

        Legacy acknowledgements require explicit operator reconciliation, since
        older stores did not retain which endpoint committed them.
        """
        with self.transaction():
            old = self.db.execute("SELECT endpoint FROM receiver_targets WHERE destination=?", (destination,)).fetchone()
            if old and old["endpoint"] != endpoint:
                raise ValueError("Outbox is bound to another receiver; reconcile instead of reusing acknowledgements")
            if not old:
                acknowledged = self.db.execute("SELECT 1 FROM deliveries WHERE destination=? AND delivered_sequence IS NOT NULL LIMIT 1",
                                               (destination,)).fetchone()
                if acknowledged and not adopt_existing_receipts:
                    raise ValueError("Legacy receiver acknowledgements require explicit target reconciliation")
                self.db.execute("INSERT INTO receiver_targets VALUES (?,?)", (destination, endpoint))

    @contextmanager
    def transaction(self):
        with self.db:
            yield

    def ingest(self, ledger):
        """Reconcile latest payloads even when Excel already exported or failed.

        Missing/ambiguous provenance is reported, never guessed into softball.
        Explicit deletion and expiry retract previously staged events. A missing
        record or incomplete source listing cannot retract a published event.
        """
        result = {"staged": 0, "unchanged": 0, "quarantined": []}
        def quarantine(key):
            result["quarantined"].append(key)
            self.db.execute("UPDATE deliveries SET status='quarantined',last_error='Source provenance requires review' "
                            "WHERE event_id=?", (key,))
        with self.transaction():
            for key, entry in sorted(ledger.get("events", {}).items()):
                raw = entry.get("event_data")
                if not isinstance(raw, dict):
                    continue
                provider = entry.get("scraper", "")
                sport = entry.get("sport")
                if sport=='softball' and entry.get('source_review_required') is True:
                    quarantine(key)
                    continue
                if sport is None and provider in LEGACY_BASEBALL_PROVIDERS and provider not in UNTYPED_PROVIDERS:
                    # Historical project contract, not an inference from title.
                    if "fastpitch" not in entry.get("url", "").lower():
                        sport = "baseball"
                if (sport not in DESTINATIONS.values() or not provider or not key.startswith(provider + "|")
                        or (sport != 'baseball' and not key.startswith(provider+'|'+sport+'|'))
                        or entry.get("sport_verified") is False
                        or (provider in UNTYPED_PROVIDERS and entry.get("sport_verified") is not True)
                        or (sport == "baseball" and any(marker in entry.get("url", "").lower()
                                                       for marker in ("fastpitch", "softball")))):
                    quarantine(key)
                    continue
                old = self.db.execute("SELECT * FROM events WHERE event_id=?", (key,)).fetchone()
                if old and old["sport"] != sport:
                    raise ValueError("Canonical event sport cannot change; use its sport-specific identity")
                row = {field: raw.get(field, "") for field in OUTPUT_COLUMNS}
                valid, _ = validate_event_data(row)
                withdrawn = entry.get("is_deleted", False) or (valid and not is_future_event(row))
                if not valid and not (entry.get("is_deleted") and old):
                    quarantine(key)
                    continue
                if withdrawn and not old:
                    continue
                source_key = key[len(provider+'|'+sport+'|'):] if sport!='baseball' else key.split('|',1)[1]
                body = {
                    "schema_version": 1, "event_id": key, "sport": sport,
                    "provider": provider, "source_id": entry.get("source_event_id") or source_key,
                    "source_url": entry.get("url", ""), "operation": "withdraw" if withdrawn else "upsert",
                    "fields": row, "source_details": {**(raw.get("source_details") or {}), **(entry.get("source_details") or {})},
                }
                try:
                    revision = payload_revision(body)
                except (ValueError, TypeError):
                    quarantine(key)
                    continue
                sequence = old["sequence"] + 1 if old else 1
                if old and old["revision"] == revision:
                    self.db.execute("""UPDATE deliveries SET status=CASE
                        WHEN delivered_sequence=desired_sequence THEN 'delivered' ELSE 'pending' END,
                        retry_at=0,last_error='' WHERE event_id=? AND status='quarantined'""", (key,))
                    result["unchanged"] += 1
                    continue
                self.db.execute("INSERT INTO events VALUES (?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET "
                                "revision=excluded.revision,sequence=excluded.sequence,payload=excluded.payload",
                                (key, sport, revision, sequence, json.dumps(body, ensure_ascii=False)))
                self.db.execute("INSERT INTO event_revisions VALUES (?,?,?,?,?,?)",
                                (key, sequence, sport, revision, json.dumps(body, ensure_ascii=False), time.time()))
                for destination, target_sport in DESTINATIONS.items():
                    if target_sport != sport:
                        continue
                    self.db.execute("""INSERT INTO deliveries
                        (destination,event_id,desired_revision,desired_sequence,status)
                        VALUES (?,?,?,?,'pending') ON CONFLICT(destination,event_id) DO UPDATE SET
                        desired_revision=excluded.desired_revision,desired_sequence=excluded.desired_sequence,
                        status='pending',attempts=0,retry_at=0,last_error=''""",
                        (destination, key, revision, sequence))
                result["staged"] += 1
        return result

    def requeue(self, destination):
        """Receiver rebuild/reconciliation: replay the same revisions and keys."""
        if destination not in DESTINATIONS:
            raise ValueError("Unknown website destination")
        with self.transaction():
            self.db.execute("UPDATE deliveries SET status='pending',retry_at=0 WHERE destination=? AND status!='quarantined'", (destination,))

    def pending(self, destination, *, limit=100, now=None, event_ids=None):
        now = time.time() if now is None else now
        clause=''
        parameters=[destination,now]
        if event_ids is not None:
            selected=sorted(set(event_ids))
            if not selected:return []
            if len(selected)>1000:raise ValueError('Selection exceeds the attempt bound')
            clause=' AND d.event_id IN ('+','.join('?' for _ in selected)+')'
            parameters.extend(selected)
        parameters.append(limit)
        return self.db.execute("""SELECT d.*,e.payload FROM deliveries d JOIN events e USING(event_id)
            WHERE destination=? AND status IN ('pending','failed','unknown','in_flight') AND retry_at<=?
            """+clause+" ORDER BY retry_at,event_id LIMIT ?", parameters).fetchall()

    @staticmethod
    def envelope(item):
        body = json.loads(item["payload"])
        body.update(destination=item["destination"], revision=item["desired_revision"],
                    sequence=item["desired_sequence"])
        body["idempotency_key"] = payload_revision({"destination": item["destination"],
            "event_id": item["event_id"], "revision": item["desired_revision"], "sequence": item["desired_sequence"]})
        return body

    def export(self, destination, path, *, event_ids=None):
        """Local materialization is never an acknowledgement."""
        from common.storage import save_json_atomic
        items = self.db.execute("SELECT d.*,e.payload FROM deliveries d JOIN events e USING(event_id) "
                                "WHERE destination=? AND status!='quarantined' ORDER BY event_id", (destination,)).fetchall()
        save_json_atomic({"schema_version": 1, "destination": destination,
                          "events": [self.envelope(item) for item in items if event_ids is None or item['event_id'] in event_ids]}, path)

    def summary(self):
        result = {destination: {"delivered": 0, "pending": 0, "unknown": 0, "failed": 0, "quarantined": 0}
                  for destination in DESTINATIONS}
        for row in self.db.execute("SELECT destination,status,count(*) AS n FROM deliveries GROUP BY destination,status"):
            # A killed in-flight request may have committed remotely.
            status = "unknown" if row["status"] == "in_flight" else row["status"]
            result[row["destination"]][status] = row["n"]
        return result

    def send(self, destination, endpoint, token, *, limit=100, session=None, allow_loopback=False, event_ids=None, min_interval=0):
        if destination not in DESTINATIONS or not 1 <= limit <= 1000:
            raise ValueError("Invalid destination or attempt limit")
        parsed = urlsplit(endpoint)
        local_http = allow_loopback and parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1','localhost','::1')
        if (parsed.scheme != "https" and not local_http) or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Delivery endpoint must be HTTPS without embedded credentials, query or fragment")
        if not token or "\n" in token or "\r" in token:
            raise ValueError("A nonempty bearer token is required")
        self.bind_target(destination, endpoint)
        owned = session is None
        session = session or requests.Session()
        next_request=0
        try:
            for item in self.pending(destination, limit=limit,event_ids=event_ids):
                delay=next_request-time.monotonic()
                if delay>0:time.sleep(delay)
                next_request=time.monotonic()+min_interval
                body = self.envelope(item)
                started = time.time()
                with self.transaction():
                    cursor = self.db.execute("INSERT INTO attempts(destination,event_id,revision,sequence,started_at) "
                        "VALUES (?,?,?,?,?)", (destination,item["event_id"],item["desired_revision"],item["desired_sequence"],started))
                    attempt_id = cursor.lastrowid
                    self.db.execute("UPDATE deliveries SET status='in_flight',attempts=attempts+1,retry_at=? "
                                    "WHERE destination=? AND event_id=?", (started + 60,destination,item["event_id"]))
                status, error = "unknown", "Unverified response"
                response = None
                ack = None
                http_status = None
                try:
                    response = session.post(endpoint, json=body,
                        headers={"Authorization": "Bearer " + token, "Idempotency-Key": body["idempotency_key"]},
                        timeout=(5, 20), allow_redirects=False, stream=True)
                    http_status=response.status_code
                    if response.status_code == 200:
                        content = bytearray()
                        for chunk in response.iter_content(8192):
                            content.extend(chunk)
                            if len(content) > 1048576 or time.time() - started > 30:
                                raise ValueError("Acknowledgement budget exceeded")
                        ack = json.loads(content)
                        expected = {key: body[key] for key in
                                    ("destination", "event_id", "revision", "sequence", "idempotency_key")}
                        if (isinstance(ack, dict) and ack.get("status") == "committed"
                                and type(ack.get("sequence")) is int
                                and all(ack.get(key) == value for key, value in expected.items())):
                            status, error = "delivered", ""
                    elif response.status_code >= 400:
                        status, error = "failed", f"HTTP {response.status_code}"
                except (requests.RequestException, ValueError, TypeError):
                    # Do not log exception text, response bodies or credential-bearing URLs.
                    status, error = "unknown", "Transport or acknowledgement failure"
                finally:
                    if response is not None:
                        response.close()
                finished = time.time()
                with self.transaction():
                    kind=('authorization' if http_status in (401,403) else 'rejected' if http_status in (409,422)
                          else 'http_error' if status=='failed' else 'unverified_response' if status=='unknown' else None)
                    self.db.execute("UPDATE attempts SET finished_at=?,result=?,http_status=?,failure_kind=? WHERE id=?",
                                    (finished,status,http_status,kind,attempt_id))
                    if status == "delivered":
                        # Retain only correlation and publication fields, never a
                        # reflected source body or arbitrary response diagnostics.
                        receipt={key:ack[key] for key in ('status','destination','event_id','revision','sequence',
                            'idempotency_key','item_id','review_required') if key in ack}
                        self.db.execute('INSERT OR IGNORE INTO delivery_receipts VALUES (?,?,?,?,?,?,?)',
                            (destination,item['event_id'],item['desired_sequence'],item['desired_revision'],
                             body['idempotency_key'],finished,json.dumps(receipt,sort_keys=True)))
                        self.db.execute("""UPDATE deliveries SET delivered_revision=?,delivered_sequence=?,
                            acknowledged_at=?,status='delivered',last_error='',retry_at=0
                            WHERE destination=? AND event_id=? AND desired_revision=? AND desired_sequence=?""",
                            (item["desired_revision"],item["desired_sequence"],finished,destination,item["event_id"],
                             item["desired_revision"],item["desired_sequence"]))
                    else:
                        delay = min(86400, 60 * 2 ** min(item["attempts"], 10))
                        self.db.execute("UPDATE deliveries SET status=?,last_error=?,retry_at=? "
                            "WHERE destination=? AND event_id=? AND desired_sequence=?",
                            (status,error,finished+delay,destination,item["event_id"],item["desired_sequence"]))
                if http_status in (401,403,429):
                    break  # One credential/configuration failure must not churn the backlog.
        finally:
            if owned:
                session.close()
        return self.summary()[destination]
