"""Run files, shared by both runners."""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path


def write_run(folder: Path, record: Callable[[str], dict]) -> Path:
    """Write record(timestamp) to a new folder/run-<UTC timestamp>.json and return its path.

    Never overwrites an earlier run: if another run already took this second's name, wait for the next second,
    so names stay unique and sort in run order (scripts/report.py picks the last one)."""
    folder.mkdir(parents=True, exist_ok=True)
    while True:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        text = json.dumps(record(stamp), indent=2)
        try:
            with (folder / f"run-{stamp}.json").open("x", encoding="utf-8") as f:
                f.write(text)
            return folder / f"run-{stamp}.json"
        except FileExistsError:
            time.sleep(0.1)
