"""Render a deck through PowerPoint COM: export PNGs and report text that overflows its box.

Usage: python scripts/deck/verify_deck.py <deck.pptx> <out-dir> [--no-export]
Never quits a PowerPoint instance that was already running.
"""
import os
import subprocess
import sys

import win32com.client


def main():
    path, out = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2])
    export = "--no-export" not in sys.argv
    os.makedirs(out, exist_ok=True)
    running = "POWERPNT.EXE" in subprocess.run(["tasklist", "/FI", "IMAGENAME eq POWERPNT.EXE"],
                                               capture_output=True, text=True).stdout.upper()
    app = win32com.client.Dispatch("PowerPoint.Application")
    pres = app.Presentations.Open(path, -1, 0, 0)  # ReadOnly, not Untitled, no window
    issues = 0
    try:
        for n in range(1, pres.Slides.Count + 1):
            sl = pres.Slides(n)
            if export:
                sl.Export(os.path.join(out, "slide%02d.png" % n), "PNG", 1600, 900)
            for i in range(1, sl.Shapes.Count + 1):
                sh = sl.Shapes(i)
                if not sh.HasTextFrame or not sh.TextFrame.HasText:
                    continue
                tr = sh.TextFrame.TextRange
                bh, bw = tr.BoundHeight, tr.BoundWidth
                size = tr.Font.Size if tr.Font.Size > 0 else tr.Paragraphs(1).Font.Size
                lines = tr.Lines(1, 999).Count if hasattr(tr.Lines(1, 999), "Count") else 0
                over = bh > sh.Height + 1 or bw > sh.Width + 1
                wrapped_title = size >= 20 and bh > size * 1.6 and sh.Height < size * 2.5
                if over or wrapped_title:
                    issues += 1
                    print("slide %d: %s h %.1f/%.1f w %.1f/%.1f lines %s size %s: %r" % (
                        n, "OVERFLOW" if over else "WRAPPED", bh, sh.Height, bw, sh.Width, lines, size,
                        tr.Text.replace("\r", " / ")[:70]))
        print("slides: %d, issues: %d" % (pres.Slides.Count, issues))
    finally:
        pres.Close()
        if not running and app.Presentations.Count == 0:
            app.Quit()


if __name__ == "__main__":
    main()
