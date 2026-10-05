"""Compare replay outputs exactly; metrics are reported separately from data."""
import argparse
import json
from pathlib import Path


def differences(before, after, path="$"):
    if type(before) is not type(after):
        return [f"{path}: type changed"]
    if isinstance(before, dict):
        out = []
        for key in sorted(before.keys() | after.keys()):
            if key not in before or key not in after:
                out.append(f"{path}.{key}: missing key")
            else:
                out.extend(differences(before[key], after[key], f"{path}.{key}"))
        return out
    if isinstance(before, list):
        if len(before) != len(after):
            return [f"{path}: length {len(before)} -> {len(after)}"]
        return [item for i, (left, right) in enumerate(zip(before, after))
                for item in differences(left, right, f"{path}[{i}]")]
    return [] if before == after else [f"{path}: {before!r} -> {after!r}"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    args = parser.parse_args()
    before = json.loads(args.before.read_text(encoding="utf-8"))
    after = json.loads(args.after.read_text(encoding="utf-8"))
    delta = differences(before["data"], after["data"])
    for item in delta:
        print(item)
    print(f"Data differences: {len(delta)}")
    print("Before metrics:", before.get("metrics", {}))
    print("After metrics:", after.get("metrics", {}))
    return int(bool(delta))


if __name__ == "__main__":
    raise SystemExit(main())
