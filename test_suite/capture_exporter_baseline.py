"""Explicit capture from a frozen pre-change tree, never from candidate defaults."""
import argparse
import hashlib
import json
import sys
from pathlib import Path
from unittest.mock import patch

from provider_replay import ROOT, module
from test_play365_exporters import ExporterTests

parser = argparse.ArgumentParser()
parser.add_argument("--source-root", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
if args.source_root.resolve() == (ROOT / "scrapers").resolve():
    parser.error("Capture must use the frozen before-change source")
base = module("play365_base", args.source_root)
result = {"provenance": {"play365_base.py": hashlib.sha256((args.source_root / "play365_base.py").read_bytes()).hexdigest()}}
for path in sorted((args.source_root / "play365").glob("*_scraper.py")):
    scope = {"__name__": f"baseline_{path.stem}"}
    with patch.dict(sys.modules, {"scrapers.play365_base": base}):
        exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), scope)
    cls = next(value for value in scope.values() if isinstance(value, type) and value.__module__ == scope["__name__"])
    scraper = cls()
    for kind in ("manual", "selenium"):
        replay = ExporterTests().execute(scraper, kind)
        if "dump" not in replay:
            raise RuntimeError(replay)
        result[f"{scraper.SCRAPER_NAME}:{kind}"] = {"pages": replay["dump"]["pages"], "seed_params": replay["dump"]["seed_params"]}
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(f"Captured {len(result) - 1} exporter baselines")
