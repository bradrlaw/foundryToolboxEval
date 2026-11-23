# Overview deck

`build_deck.py` builds the project's overview deck, [docs/foundry-toolbox-eval-overview.pptx](../../docs/foundry-toolbox-eval-overview.pptx),
with python-pptx. To change a slide, edit its function in `slides_*.py` and rebuild. Don't edit the .pptx by hand: the
next build overwrites it.

| File | Purpose |
| --- | --- |
| `build_deck.py` | Builds the deck, by default to `docs\foundry-toolbox-eval-overview.pptx` |
| `slides_a.py`, `slides_b.py`, `slides_c.py` | Slides 1-5, 6-11 and 12-16: one function per slide, speaker notes included |
| `deck_lib.py` | Colors, fonts and layout helpers (cards, panels, header, footer). Canvas 960x540 pt; text takes `**bold**` and `` `code` `` markup |
| `verify_deck.py` | Renders the deck in PowerPoint, exports each slide as a PNG and reports text that overflows its box |
| `compare.py` | Compares two decks slide by slide: shapes, geometry, text, notes, layouts and masters |

## Setup

The deck tools have their own `requirements.txt`; install it into the repo's venv:

```powershell
.\.venv\Scripts\pip install -r scripts\deck\requirements.txt
```

`build_deck.py` runs wherever python-pptx does. `verify_deck.py` and `compare.py` drive PowerPoint through COM, so they
need Windows with PowerPoint. They never quit a PowerPoint instance that was already running.

## Update the deck

```powershell
.\.venv\Scripts\python scripts\deck\build_deck.py
.\.venv\Scripts\python scripts\deck\verify_deck.py docs\foundry-toolbox-eval-overview.pptx $env:TEMP\deck-png
```

Look through the PNGs, then commit the deck together with the script change. `verify_deck.py` reports width overruns of
a few points on wrapped text; those are false positives. A height overflow or a wrapped title is a real problem.

Sizing text: a card's body is `h - 48.9` pt tall and `w - 27.4` pt wide. 12.6 pt Calibri needs about 15.4 pt per line and
5.9 pt per character. To keep two words on the same line, join them with a no-break space (`\u00a0`).

A .pptx doesn't diff, so use `compare.py` to see what a rebuild changed against the committed deck:

```powershell
git archive --format=zip --output=$env:TEMP\deck-old.zip HEAD docs/foundry-toolbox-eval-overview.pptx
Expand-Archive -Force $env:TEMP\deck-old.zip $env:TEMP\deck-old
.\.venv\Scripts\python scripts\deck\compare.py $env:TEMP\deck-old\docs\foundry-toolbox-eval-overview.pptx docs\foundry-toolbox-eval-overview.pptx
```
