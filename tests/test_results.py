"""evals/results.py: run files."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from evals import results


class WriteRunTests(unittest.TestCase):
    def test_runs_in_the_same_second_get_unique_names_in_run_order(self):
        t0 = datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)
        t1 = datetime(2026, 11, 7, 12, 0, 1, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(results, "datetime") as clock, \
                mock.patch.object(results.time, "sleep"):
            clock.now.side_effect = [t0, t0, t1]
            folder = Path(tmp) / "runs"
            first = results.write_run(folder, lambda stamp: {"timestamp": stamp, "run": 1})
            second = results.write_run(folder, lambda stamp: {"timestamp": stamp, "run": 2})
            self.assertEqual([first.name, second.name], ["run-20261107T120000Z.json", "run-20261107T120001Z.json"])
            self.assertEqual(sorted(folder.iterdir()), [first, second])
            self.assertEqual(json.loads(first.read_text(encoding="utf-8")), {"timestamp": "20261107T120000Z", "run": 1})
            self.assertEqual(json.loads(second.read_text(encoding="utf-8")), {"timestamp": "20261107T120001Z", "run": 2})


if __name__ == "__main__":
    unittest.main()
