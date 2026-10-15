"""Anchor the existing CLI and propagate its actual status to the scheduler."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from ui.cli import run_cli

if __name__ == "__main__":
    raise SystemExit(run_cli())
