"""Opt-in OpenTelemetry export to Azure Application Insights.

Set APPLICATIONINSIGHTS_CONNECTION_STRING in .env to enable. Without it (or without the exporter
package) everything is a no-op, so local runs and offline tests need no Azure access.

  APPLICATIONINSIGHTS_CONNECTION_STRING   App Insights connection string (enables export)
  APPLICATIONINSIGHTS_USE_AAD=1           authenticate ingestion with `az login` (Entra) instead of the
                                          instrumentation key; required if the resource disables local auth
  OTEL_SERVICE_NAME                       overrides the cloud role name (default per entry point)
"""
from __future__ import annotations

import atexit
import contextlib
import os

try:
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    AVAILABLE = True
except ImportError:  # stdlib-only environments: every function below becomes a no-op
    AVAILABLE = False

_initialized = False


class _NoopSpan:
    def set_attribute(self, *_a, **_k):
        pass

    def record_exception(self, *_a, **_k):
        pass


@contextlib.contextmanager
def span(name: str, **attributes):
    """Start an internal span with attributes (no-op if OpenTelemetry is unavailable)."""
    if not AVAILABLE:
        yield _NoopSpan()
        return
    with trace.get_tracer("foundry_toolbox_eval").start_as_current_span(name) as s:
        for k, v in attributes.items():
            s.set_attribute(k, v)
        yield s


def _sdk_provider():
    """Return the SDK TracerProvider currently installed (unwrapping the API proxy), or None."""
    if not AVAILABLE:
        return None
    current = trace.get_tracer_provider()
    for candidate in (current, getattr(current, "_real_provider", None)):
        if isinstance(candidate, TracerProvider):
            return candidate
    return None


def init(service_name: str) -> bool:
    """Install (or extend) the tracer provider with an App Insights exporter. Returns True if exporting."""
    global _initialized
    if not AVAILABLE:
        return False
    conn = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")
    provider = _sdk_provider()
    if provider is None:
        provider = TracerProvider(resource=Resource.create({"service.name": os.environ.get("OTEL_SERVICE_NAME", service_name)}))
        trace.set_tracer_provider(provider)
    if not conn or _initialized:
        return _initialized
    try:
        from azure.monitor.opentelemetry.exporter import AzureMonitorTraceExporter
    except ImportError:
        return False

    kwargs = {"connection_string": conn}
    if os.environ.get("APPLICATIONINSIGHTS_USE_AAD") == "1":
        from azure.identity import AzureCliCredential
        kwargs["credential"] = AzureCliCredential()
    provider.add_span_processor(BatchSpanProcessor(AzureMonitorTraceExporter(**kwargs)))
    atexit.register(flush)
    _initialized = True
    return True


def flush() -> None:
    provider = _sdk_provider()
    if provider is not None:
        provider.force_flush(timeout_millis=15000)
