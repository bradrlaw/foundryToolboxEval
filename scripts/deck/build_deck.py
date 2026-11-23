"""Build the Foundry Toolbox Eval overview deck.

Usage: python scripts/deck/build_deck.py [out.pptx]    (default: docs/foundry-toolbox-eval-overview.pptx)
"""
import sys
from pathlib import Path

from deck_lib import new_deck, save
from slides_a import SLIDES as A
from slides_b import SLIDES as B
from slides_c import SLIDES as C

DEFAULT = Path(__file__).resolve().parents[2] / "docs" / "foundry-toolbox-eval-overview.pptx"


def main(path):
    prs = new_deck()
    for page, make in enumerate(A + B + C, 1):
        make(prs, page)
    save(prs, path)
    print(f"wrote {path}: {len(prs.slides)} slides")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else str(DEFAULT))
