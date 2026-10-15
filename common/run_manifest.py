import uuid
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from common.storage import save_json_atomic


class RunManifest:
    @classmethod
    def load_for_repair(cls, path):
        """Read a finished snapshot without changing its durable state."""
        path = Path(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("mode") != "snapshot":
            raise ValueError("Only snapshot runs can be repaired")
        if data.get("test_mode") or Path(data["output_root"]).resolve().is_relative_to(Path("Tests").resolve()):
            raise ValueError("Production repair cannot use isolated test state")
        if data.get("status") not in {"partial", "interrupted"}:
            raise ValueError("Repair requires a finished partial or interrupted run")
        if data.get("run_date") != datetime.now().strftime("%Y-%m-%d"):
            raise ValueError("Start a new snapshot after the run date changes")
        if not isinstance(data.get("providers"), dict) or not isinstance(data.get("locations"), dict):
            raise ValueError("Invalid run coverage metadata")
        obj = cls.__new__(cls)
        obj.path = path
        obj.data = data
        obj.run_id = data["run_id"]
        obj.run_date = data["run_date"]
        obj.output_root = data["output_root"]
        return obj

    def begin_repair(self):
        # Called under the production ledger lock. Keep the failed attempt reviewable.
        stamp = datetime.now().strftime("%H%M%S_%f")
        archive = self.path.with_name(self.path.stem + f".before_repair_{stamp}.json")
        save_json_atomic(self.data, archive)
        self.data.setdefault("repair_attempts", []).append({
            "started_at": datetime.now(timezone.utc).isoformat(),
            "previous_manifest": str(archive), "previous_status": self.data["status"],
        })
        self.data["status"] = "running"
        if os.environ.get("SCRAPER_DAILY_RUN_ID"):
            self.data["coordinator_id"] = os.environ["SCRAPER_DAILY_RUN_ID"]
        self.data.pop("finished_at", None)
        self.save()

    def __init__(self, options, *, test_mode=False, root=None):
        now = datetime.now()
        self.run_date = now.strftime("%Y-%m-%d")
        self.run_id = now.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
        base = Path(root) if root else Path("Tests" if test_mode else "Sheets")
        self.output_root = str(base / "Snapshots" / self.run_id) if options.mode == "snapshot" else str(base)
        self.path = base / "PullRuns" / f"{self.run_id}.json"
        self.data = {"run_id": self.run_id, "run_date": self.run_date, "mode": options.mode, "test_mode": test_mode,
                     "start_date": str(options.start_date or ""), "end_date": str(options.end_date or ""),
                     "output_root": self.output_root, "status": "running", "providers": {}, "locations": {}}
        if os.environ.get("SCRAPER_DAILY_RUN_ID"):
            self.data["coordinator_id"] = os.environ["SCRAPER_DAILY_RUN_ID"]
        self.save()

    def save(self):
        save_json_atomic(self.data, self.path)

    def provider(self, name, status, *, locations=None, error=""):
        previous = self.data["providers"].get(name, {})
        expected = previous.get("locations", []) if locations is None else list(locations)
        self.data["providers"][name] = {"status": status, "locations": expected, "error": error}
        self.save()

    def location(self, provider, location, result):
        self.data["locations"][f"{provider}|{location}"] = dict(result)
        self.save()

    def finish(self, interrupted=False):
        problems = [item["status"] for item in self.data["providers"].values() if item["status"] != "complete"]
        problems += [item["status"] for item in self.data["locations"].values() if item["status"] != "complete"]
        self.data["status"] = "interrupted" if interrupted else "partial" if problems else "complete"
        self.data["finished_at"] = datetime.now(timezone.utc).isoformat()
        self.save()
