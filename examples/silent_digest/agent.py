"""A daily digest agent with the most common bug there is: it swallows a failure."""

import json
import os
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
FEED = HERE / "feed.json"
OUT = HERE / "out" / "digest.md"


def load_feed():
    try:
        return json.loads(FEED.read_text(encoding="utf-8"))["items"]
    except Exception:
        # The bug. A dead source is indistinguishable from a quiet day.
        return []


def main():
    items = load_feed()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# Daily digest - {datetime.now():%Y-%m-%d %H:%M:%S}", ""]
    lines += [f"- {item}" for item in items]
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")

    claim = {"status": "ok", "items": len(items), "file": str(OUT.name)}
    claim_path = os.environ.get("VIGIL_CLAIM")
    if claim_path:
        Path(claim_path).write_text(json.dumps(claim), encoding="utf-8")
    print(f"digest written with {len(items)} items")
    return 0  # exit 0, every single time


if __name__ == "__main__":
    raise SystemExit(main())
