"""Compare two decks through PowerPoint COM: shapes, geometry, text and notes per slide, plus master/layout shapes.

Usage: python scripts/deck/compare.py <a.pptx> <b.pptx>
Prints every difference and the sensitivity label of each deck. Never quits a PowerPoint instance already running.
"""
import os
import subprocess
import sys

import win32com.client


def safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def shape_rows(shapes, prefix=""):
    rows = []
    for i in range(1, shapes.Count + 1):
        sh = shapes.Item(i)
        t = sh.Type
        row = (prefix + str(i), t, safe(lambda: sh.AutoShapeType) if t == 1 else None,
               round(sh.Left, 1), round(sh.Top, 1), round(sh.Width, 1), round(sh.Height, 1),
               safe(lambda: round(sh.Adjustments.Item(1), 3)) if t == 1 and safe(lambda: sh.Adjustments.Count, 0) else None,
               safe(lambda: sh.TextFrame.TextRange.Text) if safe(lambda: sh.HasTextFrame) else None)
        rows.append(row)
        if t == 6:
            rows += shape_rows(sh.GroupItems, prefix + str(i) + ".")
    return rows


def notes(slide):
    shapes = slide.NotesPage.Shapes
    for i in range(1, shapes.Count + 1):
        sh = shapes.Item(i)
        if sh.Type == 14 and sh.PlaceholderFormat.Type == 2:
            return sh.TextFrame.TextRange.Text
    return None


def label(pres):
    info = safe(lambda: pres.SensitivityLabel.GetLabel())
    if info is None:
        return "label: unavailable"
    return "label: %s id=%s method=%s irm=%s" % (safe(lambda: info.LabelName), safe(lambda: info.LabelId),
                                                 safe(lambda: info.AssignmentMethod),
                                                 safe(lambda: pres.Permission.Enabled))


def masters(pres):
    out = {}
    for d in range(1, pres.Designs.Count + 1):
        m = pres.Designs(d).SlideMaster
        out["master%d" % d] = [(sh.Name, sh.Type) for sh in [m.Shapes(i) for i in range(1, m.Shapes.Count + 1)]]
        for k in range(1, m.CustomLayouts.Count + 1):
            lay = m.CustomLayouts(k)
            out["master%d layout%d %s" % (d, k, lay.Name)] = [(lay.Shapes(i).Name, lay.Shapes(i).Type)
                                                            for i in range(1, lay.Shapes.Count + 1)]
    return out


def main():
    a, b = (os.path.abspath(p) for p in sys.argv[1:3])
    running = "POWERPNT.EXE" in subprocess.run(["tasklist", "/FI", "IMAGENAME eq POWERPNT.EXE"],
                                               capture_output=True, text=True).stdout.upper()
    app = win32com.client.Dispatch("PowerPoint.Application")
    pa = app.Presentations.Open(a, -1, 0, 0)
    pb = app.Presentations.Open(b, -1, 0, 0)
    diffs = 0
    try:
        print("A:", label(pa))
        print("B:", label(pb))
        print("slides: A %d, B %d" % (pa.Slides.Count, pb.Slides.Count))
        for n in range(1, min(pa.Slides.Count, pb.Slides.Count) + 1):
            ra, rb = shape_rows(pa.Slides(n).Shapes), shape_rows(pb.Slides(n).Shapes)
            if len(ra) != len(rb):
                diffs += 1
                print("slide %d: %d shapes vs %d" % (n, len(ra), len(rb)))
            for x, y in zip(ra, rb):
                if x != y:
                    diffs += 1
                    print("slide %d shape %s:\n  A %r\n  B %r" % (n, x[0], x[1:], y[1:]))
            na, nb = notes(pa.Slides(n)), notes(pb.Slides(n))
            if (na or "").strip() != (nb or "").strip():
                diffs += 1
                print("slide %d notes differ:\n  A %r\n  B %r" % (n, (na or "")[:120], (nb or "")[:120]))
            la, lb = safe(lambda: pa.Slides(n).CustomLayout.Name), safe(lambda: pb.Slides(n).CustomLayout.Name)
            if la != lb:
                diffs += 1
                print("slide %d layout %r vs %r" % (n, la, lb))
        ma, mb = masters(pa), masters(pb)
        for key in sorted(set(ma) | set(mb)):
            if ma.get(key) != mb.get(key):
                diffs += 1
                print("%s:\n  A %r\n  B %r" % (key, ma.get(key), mb.get(key)))
        print("differences: %d" % diffs)
    finally:
        for p in (pa, pb):
            p.Saved = -1
            p.Close()
        if not running and app.Presentations.Count == 0:
            app.Quit()


if __name__ == "__main__":
    main()
