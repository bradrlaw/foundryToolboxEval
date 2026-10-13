import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals"))
import telemetry  # noqa: E402


class TelemetryNoopTests(unittest.TestCase):
    def test_init_without_connection_string_does_not_export(self):
        os.environ.pop("APPLICATIONINSIGHTS_CONNECTION_STRING", None)
        telemetry.init("unit-test")
        telemetry.flush()

    def test_span_context_manager_accepts_attributes(self):
        with telemetry.span("unit.span", **{"eval.passed": True}) as s:
            s.set_attribute("k", "v")


if __name__ == "__main__":
    unittest.main()
