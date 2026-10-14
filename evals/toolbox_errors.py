"""Recognize toolbox errors caused by a tool source (the MCP server behind a toolbox connection).

A toolbox reports a failing tool source as a JSON-RPC error (HTTP 200). Shapes observed:

  tools/list  -32006 or -32007  "tools/list failed for 1 tool source(s), succeeded for 0 tool source(s) {"errors":
              [{"name":"sample","type":"mcp","error":{"code":"CONSENT_REQUIRED","message":"<consent link>"}}]}"
              Upstream failures use the same format with codes such as "HTTP_401" or "HTTP_400".
  tools/call  -32006  the message is the consent link; data {"grpcStatus": "FailedPrecondition", "detail": "<link>"}
  tools/call  -32002  "Authentication failed when connecting to the MCP server: ..."; data grpcStatus "Unauthenticated"
  tools/call  -32602  "Connection resolution failed for '<connection>' ..."

The Responses API reports the same tools/list failure as HTTP 400 with "McpProtocolException encountered while
enumerating tools: ..." and the {"errors": [...]} list inside the message.

Consent links are bearer-like: whoever opens one can bind their own account to the user's connection. They
appear only in `consent` and on the console (announce_consent), never in `detail`, results or telemetry.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from typing import Iterable

CONSENT_REQUIRED = "CONSENT_REQUIRED"
TOOL_SOURCE_FAILED = -32006
UPSTREAM_GRPC = frozenset({"Unauthenticated", "PermissionDenied", "Unavailable", "DeadlineExceeded"})
CONSENT_DETAIL = "user consent required"
# Agent abort reasons caused by a tool source before the model ran: there is no reply to score or judge.
AGENT_NOT_RUN = frozenset({"consent_required", "tool_listing_failed"})

_ERRORS_START = re.compile(r'\{\s*"errors"\s*:')
_UPSTREAM_TEXT = re.compile(r"tool source\(s\)|connecting to the MCP server|Connection resolution failed", re.I)
_URL = re.compile(r"https://[^\s\"'<>]+")

_announced: set[str] = set()


@dataclass(frozen=True)
class SourceError:
    name: str
    code: str
    message: str

    @property
    def consent(self) -> bool:
        return self.code == CONSENT_REQUIRED

    def __str__(self) -> str:
        return f"tool source '{self.name}': " + (CONSENT_DETAIL if self.consent else f"{self.code}: {self.message}")


@dataclass(frozen=True)
class SourceFailure:
    outcome: str  # consent_required or upstream_error
    detail: str  # safe to record: consent links are left out
    consent: tuple[tuple[str, str], ...] = ()  # (tool source name, or "" if unknown; consent link)


def source_errors(text: str) -> list[SourceError]:
    """The per-source errors embedded in a toolbox error message; [] if there are none or they don't parse."""
    m = _ERRORS_START.search(text)
    if not m:
        return []
    try:
        doc, _ = json.JSONDecoder().raw_decode(text, m.start())
    except ValueError:
        return []
    out = []
    for entry in doc.get("errors") or []:
        if isinstance(entry, dict):
            err = entry.get("error") if isinstance(entry.get("error"), dict) else {}
            out.append(SourceError(str(entry.get("name") or "?"), str(err.get("code") or "?"),
                                   str(err.get("message") or "")))
    return out


def _link(text: str) -> str:
    m = _URL.search(text)
    return m[0] if m else text.strip()


def classify(message: str, data: object = None, code: int | None = None) -> SourceFailure | None:
    """Classify a toolbox error message (plus JSON-RPC data and code, if any).

    Returns None when the error is not about a tool source, e.g. an unknown tool or invalid arguments.
    """
    errors = source_errors(message)
    if errors:
        consent = tuple((e.name, _link(e.message)) for e in errors if e.consent)
        return SourceFailure("consent_required" if consent else "upstream_error", "; ".join(map(str, errors)), consent)
    if CONSENT_REQUIRED in message:  # an error list that did not parse; never echo it
        return SourceFailure("consent_required", CONSENT_DETAIL, tuple(("", u) for u in _URL.findall(message)))
    data = data if isinstance(data, dict) else {}
    grpc = data.get("grpcStatus")
    detail = data.get("detail") if grpc == "FailedPrecondition" or code == TOOL_SOURCE_FAILED else None
    # Only consent errors carry a bare link as the message; treating any such message as one keeps
    # an unrecognized variant out of results instead of recording the link.
    bare = [s.strip() for s in (message, detail) if isinstance(s, str) and _URL.fullmatch(s.strip())]
    if bare:
        return SourceFailure("consent_required", CONSENT_DETAIL + " for a tool source", (("", bare[0]),))
    if grpc in UPSTREAM_GRPC or _UPSTREAM_TEXT.search(message):
        return SourceFailure("upstream_error", message)
    return None


def announce_consent(consent: Iterable[tuple[str, str]], user: str | None = None) -> None:
    """Print consent links to stderr, the only place they appear, once per tool source per process."""
    for source, link in consent:
        if not link or source in _announced:
            continue
        _announced.add(source)
        label = f"Tool source '{source}'" if source else "A tool source"
        print(f"\n{label} uses OAuth identity passthrough and needs a one-time consent. Open this link in a "
              f"browser signed in as {user or 'the test user'}, approve, then rerun:\n  {link}\n"
              "Don't share the link: whoever completes the sign-in binds their account to your connection.\n",
              file=sys.stderr)
