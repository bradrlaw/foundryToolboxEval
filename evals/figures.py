"""Deterministic grounding check: the figures in a reply that match no number a tool returned.

A figure is a number in the reply with its currency, scale and percent sign, such as $84.12, 1,250,000, 12.4 billion,
-2.3% or 17.3x. It is grounded if it matches a number in a tool result of the conversation within the precision the
reply wrote it to: $12.4 billion matches 12,400,000,000, and 84 matches 84.12. Signs are ignored, since replies often
put them in words (down 2.3%); a percentage also matches its fraction (42% and 0.42), and a scaled figure its unscaled
number (12.4 billion and 12.4). Thousands can be grouped with commas or spaces, as in 1 250 000; a number below a
million grouped with an ordinary space, such as 5 100, reads as two numbers unless the space is a no-break or thin one.

Not figures: numbers inside words and identifiers (C1001, Q3, 3rd), dates and times, years from 1900 to 2100, and
whole numbers without a currency, scale, percent sign or decimals that are below 10, such as counts and list markers,
or part of a compound such as 52-week.

Known limits: a figure computed from tool results, such as a monthly value from an annual one, is flagged, and so are
made-up examples, a user's figure the reply repeats to correct or reject it, and numbers that state no data, as in
Dynamics 365, ISO 8601, slide 14 or a 30-second segment. A claim without a number, such as calling an old quote current
or giving one industry's figure for another, isn't seen.
"""
from __future__ import annotations

import json
import re
from typing import Iterable, NamedTuple

SCALES = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mm": 1e6, "mn": 1e6, "million": 1e6, "b": 1e9, "bn": 1e9,
          "billion": 1e9, "t": 1e12, "tn": 1e12, "trillion": 1e12}
_MONTH = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|"
          r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?")
# Dates and times, removed before figures are read.
_DATES = re.compile("|".join([
    r"\b\d{4}-\d{1,2}-\d{1,2}(?:[T ]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?\s?(?:Z|UTC|GMT|[+-]\d{2}:?\d{2})?)?",
    r"\b\d{1,2}/\d{1,2}/\d{2,4}\b",
    r"\b\d{1,2}:\d{2}(?::\d{2})?(?:\s?(?:[ap]\.?m\.?|UTC|GMT|Z)(?![\w]))?",
    rf"\b{_MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?(?![\w])",
    rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTH}(?:,?\s+\d{{4}})?(?![\w])",
]), re.I)
_SCALE = r"(?:thousand|million|billion|trillion|mm|mn|bn|tn|k|m|b|t)(?![\w])"
# Whole digits: grouped by thousands with commas, with no-break or thin spaces, or with ordinary spaces from a
# million up (1 250 000), since 5 100 can be two numbers; else ungrouped.
_DIGITS = r"\d{1,3}(?:,\d{3})+|\d{1,3}(?:[\u00a0\u202f\u2009]\d{3})+(?!\d)|\d{1,3}(?:[ ]\d{3}){2,}(?!\d)|\d+"
_SEPARATORS = re.compile(r"[,\s]")
_FIGURE = re.compile(rf"""
    (?<![\w.])(?<!\d,)                               # not inside a word, an identifier or a longer number
    (?P<currency>(?:US)?\$|€|£|(?:USD|EUR|GBP)\s?)?
    (?P<number>(?:{_DIGITS})(?:\.\d+)?|\.\d+)
    (?:\s?(?P<scale>{_SCALE}))?
    (?:\s?(?P<percent>%|percent(?![\w])|per\s?cent(?![\w])|pct(?![\w])))?
    x?(?![\w]|\.\d)
""", re.I | re.X)
_NUMBER = re.compile(rf"(?<![\w.])(?<!\d,)((?:{_DIGITS})(?:\.\d+)?)(?:\s?({_SCALE}))?", re.I)


class Figure(NamedTuple):
    text: str  # as the reply wrote it
    value: float  # unsigned, scaled
    tolerance: float  # half a unit of the last digit the reply wrote, scaled
    scale: float
    percent: bool


def figures(text: str) -> list[Figure]:
    """The figures in `text`, in order."""
    out = []
    text = _DATES.sub(" ", text)
    for m in _FIGURE.finditer(text):
        digits = _SEPARATORS.sub("", m["number"])
        decimals = len(digits.partition(".")[2])
        if not (m["currency"] or m["scale"] or m["percent"] or decimals or digits != m["number"]):
            whole = int(digits)
            # A count, a list marker, a year, or part of a compound such as 52-week or 24-hour.
            if whole < 10 or 1900 <= whole <= 2100 or re.match(r"-[A-Za-z]", text[m.end():m.end() + 2]):
                continue
        scale = SCALES[m["scale"].lower()] if m["scale"] else 1.0
        out.append(Figure(m[0].strip(), float(digits) * scale, 0.5 * 10 ** -decimals * scale, scale,
                          bool(m["percent"])))
    return out


def _collect(value: object, found: set[float]) -> None:
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (int, float)):
        found.add(abs(float(value)))
    elif isinstance(value, dict):
        for v in value.values():
            _collect(v, found)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _collect(v, found)
    elif isinstance(value, str):
        text = value.strip()
        if text[:1] in ("{", "["):
            try:
                _collect(json.loads(text), found)
                return
            except ValueError:
                pass
        for m in _NUMBER.finditer(_DATES.sub(" ", text)):
            number = float(_SEPARATORS.sub("", m[1]))
            found.add(number)
            if m[2]:
                found.add(number * SCALES[m[2].lower()])


def numbers(sources: Iterable[object]) -> set[float]:
    """Every number in `sources`, unsigned: tool results as JSON text, other text, or parsed JSON. In text, a number
    written with a scale, such as 12.4 billion, counts both scaled and as written; dates and numbers inside words and
    identifiers don't count."""
    found: set[float] = set()
    for source in sources:
        _collect(source, found)
    return found


def grounded(figure: Figure, known: set[float]) -> bool:
    """Whether `figure` matches one of the `known` numbers within its precision."""
    options = [(figure.value, figure.tolerance)]
    if figure.scale != 1:
        options.append((figure.value / figure.scale, figure.tolerance / figure.scale))
    if figure.percent:
        options += [(v / 100, t / 100) for v, t in options]
    return any(abs(k - v) <= t * (1 + 1e-9) for k in known for v, t in options)


def ungrounded(reply: str, sources: Iterable[object]) -> list[str]:
    """The figures in `reply`, as written and without repeats, that match no number in `sources`."""
    known = numbers(sources)
    return list(dict.fromkeys(f.text for f in figures(reply) if not grounded(f, known)))
