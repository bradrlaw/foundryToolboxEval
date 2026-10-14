import os
import unittest
from unittest import mock

from evals import telemetry


class TelemetryNoopTests(unittest.TestCase):
    def test_init_without_connection_string_does_not_export(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("APPLICATIONINSIGHTS_CONNECTION_STRING", None)
            self.assertFalse(telemetry.init("unit-test"))
        telemetry.flush()

    def test_span_context_manager_accepts_attributes(self):
        with telemetry.span("unit.span", **{"eval.passed": True}) as s:
            s.set_attribute("k", "v")


if __name__ == "__main__":
    unittest.main()
