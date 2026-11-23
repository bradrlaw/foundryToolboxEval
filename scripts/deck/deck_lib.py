"""Design helpers for the overview deck, built with python-pptx.

Canvas 960x540 pt; every element is an explicit shape with explicit fonts and colors (no placeholders).
Text markup: **bold** (ink) and `code` (Consolas). Paragraphs are strings or (string, overrides) tuples.
"""
import re

from lxml import etree
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Pt

BLUE, PURPLE, TEAL, ORANGE = "0078D4", "7454B7", "14B8A6", "D98E04"
TEXT = {BLUE: BLUE, PURPLE: PURPLE, TEAL: "0E8F82", ORANGE: "B26A00"}
INK, BODY, DARK, MUTED = "0B1730", "33465C", "263B53", "5C6B7C"
LINE, CARD, PANEL, WHITE = "C7D9E8", "F7FAFD", "EAF3FB", "FFFFFF"
RECT, ROUND, CHEVRON = MSO_SHAPE.RECTANGLE, MSO_SHAPE.ROUNDED_RECTANGLE, MSO_SHAPE.CHEVRON
FOOTER = "Foundry Toolbox Eval  |  github.com/bradrlaw/foundryToolboxEval"
MARK = re.compile(r"(\*\*.+?\*\*|`[^`]+`)")
ALIGN = {"l": PP_ALIGN.LEFT, "c": PP_ALIGN.CENTER, "r": PP_ALIGN.RIGHT}
ANCHOR = {"t": MSO_ANCHOR.TOP, "m": MSO_ANCHOR.MIDDLE, "b": MSO_ANCHOR.BOTTOM}


def E(pt):
    return Emu(int(round(pt * 12700)))


def new_deck():
    prs = Presentation()
    f = 12192000 / prs.slide_width
    prs.slide_width, prs.slide_height = Emu(12192000), Emu(6858000)
    for part in (prs.slide_master, *prs.slide_layouts):  # widen the 4:3 template's placeholders
        for off in part._element.iter(qn("a:off")):
            off.set("x", str(int(int(off.get("x")) * f)))
        for ext in part._element.iter(qn("a:ext")):
            if ext.get("cx") is not None:
                ext.set("cx", str(int(int(ext.get("cx")) * f)))
    return prs


def new_slide(prs, notes=None):
    s = prs.slides.add_slide(prs.slide_layouts[6])
    if notes:
        s.notes_slide.notes_text_frame.text = notes
    return s


def save(prs, path):
    cp = prs.core_properties
    cp.title, cp.subject = "Foundry Toolbox Eval", "Project overview"
    cp.author = cp.last_modified_by = cp.keywords = cp.comments = cp.category = ""
    cp.revision = 1
    prs.save(path)


def _plain(shape):
    style = shape._element.find(qn("p:style"))
    if style is not None:
        shape._element.remove(style)
    return shape


def box(slide, x, y, w, h, fill=None, line=None, lw=1.0, shape=RECT, adj=None):
    s = _plain(slide.shapes.add_shape(shape, E(x), E(y), E(w), E(h)))
    if fill:
        s.fill.solid()
        s.fill.fore_color.rgb = RGBColor.from_string(fill)
    else:
        s.fill.background()
    if line:
        s.line.color.rgb = RGBColor.from_string(line)
        s.line.width = Pt(lw)
    else:
        s.line.fill.background()
    if adj is not None:
        s.adjustments[0] = adj
    return s


def conn(slide, x1, y1, x2, y2, color=BLUE, w=1.5):
    c = _plain(slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, E(x1), E(y1), E(x2), E(y2)))
    c.line.color.rgb = RGBColor.from_string(color)
    c.line.width = Pt(w)
    return c


def _bullet(para, indent):
    pPr = para._p.get_or_add_pPr()
    pPr.set("marL", str(int(E(indent))))
    pPr.set("indent", str(-int(E(indent))))
    etree.SubElement(pPr, qn("a:buFont"), typeface="Arial")
    etree.SubElement(pPr, qn("a:buChar"), char="\u2022")


def text(slide, x, y, w, h, paras, font="Calibri", size=12.6, c=BODY, bold=False, align="l", anchor="m",
         after=0, markup=True, ls=None, bullet=False):
    tb = slide.shapes.add_textbox(E(x), E(y), E(w), E(h))
    tf = tb.text_frame
    tf.word_wrap = True
    tf.auto_size = MSO_AUTO_SIZE.NONE
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    tf.vertical_anchor = ANCHOR[anchor]
    if isinstance(paras, (str, tuple)):
        paras = [paras]
    for i, p in enumerate(paras):
        p, o = p if isinstance(p, tuple) else (p, {})
        para = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        para.alignment = ALIGN[o.get("align", align)]
        if o.get("after", after):
            para.space_after = Pt(o.get("after", after))
        if o.get("before"):
            para.space_before = Pt(o["before"])
        if o.get("ls", ls):
            para.line_spacing = o.get("ls", ls)
        f, sz, col, b = o.get("font", font), o.get("size", size), o.get("c", c), o.get("bold", bold)
        if o.get("bullet", bullet):
            _bullet(para, sz * 0.95)
        mk = o.get("markup", markup)
        for j, line in enumerate(p.split("\n")):
            if j:
                para.add_line_break()
            for part in (MARK.split(line) if mk else [line]):
                if not part:
                    continue
                rf, rs, rc, rb = f, sz, col, b
                if mk and len(part) > 4 and part[:2] == "**" and part[-2:] == "**":
                    part, rb, rc = part[2:-2], True, o.get("bc", INK if col in (BODY, DARK, MUTED) else col)
                elif mk and len(part) > 2 and part[0] == "`" == part[-1]:
                    part, rf, rs, rc = part[1:-1], "Consolas", round(sz * 0.9, 1), INK
                r = para.add_run()
                r.text = part
                r.font.name, r.font.size, r.font.bold = rf, Pt(rs), rb
                r.font.color.rgb = RGBColor.from_string(rc)
    return tb


def topbar(slide):
    box(slide, 0, 0, 960, 10.1, fill=BLUE, line=BLUE)


def header(slide, n, section, title, lead=None):
    topbar(slide)
    text(slide, 39.6, 24.5, 864, 15.8, f"{n:02d}  /  {section.upper()}", font="Aptos", size=10, c=BLUE, bold=True,
         markup=False)
    text(slide, 39.6, 49, 882, 44.6, title, font="Aptos Display", size=27, c=INK, bold=True, markup=False)
    if lead:
        text(slide, 39.6, 99.4, 878.4, 49, lead, size=16.5, c=BODY)


def subtitle(slide, s):
    box(slide, 39.6, 103.5, 37.4, 5, fill=BLUE)
    text(slide, 39.6, 112.5, 878.4, 22.8, s, font="Aptos", size=14, c=MUTED, markup=False)


def footer(slide, page):
    conn(slide, 39.6, 509.8, 918, 509.8, color=LINE, w=1)
    text(slide, 39.6, 515.5, 576, 13, FOOTER, size=8, c=MUTED, markup=False)
    text(slide, 882, 514.1, 36, 14.4, str(page), size=9, c=MUTED, align="r", markup=False)


def card(slide, x, y, w, h, title, body, accent=BLUE, adj=0.042, size=12.6, tsize=17, after=6, fill=CARD):
    box(slide, x, y, w, h, fill=fill, line=LINE, shape=ROUND, adj=adj)
    box(slide, x, y, 5, h, fill=accent)
    text(slide, x + 14.4, y + 11.6, w - 27.4, 25.9, title, size=tsize, c=INK, bold=True, markup=False)
    text(slide, x + 14.4, y + 41.8, w - 27.4, h - 48.9, body, size=size, c=BODY, anchor="t", after=after)


def chevron(slide, x, y, w=32.4, h=25.2, color=BLUE):
    box(slide, x, y, w, h, fill=color, shape=CHEVRON, adj=0.5)


def panel(slide, x, y, w, h, fill=PANEL, adj=0.062):
    box(slide, x, y, w, h, fill=fill, line=LINE, shape=ROUND, adj=adj)


def cards3(slide, items, y=159.8, h=169.9, chevrons=True, **kw):
    for x, (t, b, a) in zip((39.6, 352.1, 663.8), items):
        card(slide, x, y, 255.6, h, t, b, accent=a, **kw)
    if chevrons:
        for x in (304.6, 616.3):
            chevron(slide, x, y + h / 2 - 12.6)


def row(slide, items, y, h, gap=20, chev=None, x0=39.6, total=878.4, **kw):
    """Cards side by side; items are (title, body, accent). chev=(w, h) puts a chevron in each gap."""
    n = len(items)
    w = (total - gap * (n - 1)) / n
    for i, (t, b, a) in enumerate(items):
        x = x0 + i * (w + gap)
        card(slide, x, y, w, h, t, b, accent=a, **kw)
        if chev and i < n - 1:
            chevron(slide, x + w + (gap - chev[0]) / 2, y + h / 2 - chev[1] / 2, *chev)


def stack(slide, x, y, w, items, h=27, gap=9):
    """Small boxes stacked top to bottom and joined by short connectors; items are (label, color)."""
    for i, (s, col) in enumerate(items):
        yy = y + i * (h + gap)
        if i:
            conn(slide, x + w / 2, yy - gap, x + w / 2, yy, color=col)
        box(slide, x, yy, w, h, fill=WHITE, line=col, lw=1.2, shape=ROUND, adj=0.167)
        text(slide, x + 4, yy, w - 8, h, s, font="Aptos", size=8, c=TEXT.get(col, col), bold=True, align="c",
             markup=False)


def ptitle(slide, x, y, w, s, size=18):
    text(slide, x, y, w, 26, s, size=size, c=INK, bold=True, markup=False)


def highlight(slide, x, y, w, s, h=22.3):
    text(slide, x, y, w, h, [(s, {"bc": BLUE})], size=11.3, c=BLUE, bold=True)


def code(slide, x, y, w, h, label, lines, size=10):
    box(slide, x, y, w, h, fill=WHITE, line=LINE, shape=ROUND, adj=min(0.5, 8 / min(w, h)))
    text(slide, x + 12, y + 9, w - 24, 12, label.upper(), font="Aptos", size=8, c=MUTED, bold=True, markup=False)
    paras = [(ln, {"c": MUTED}) if ln.lstrip().startswith("#") else ln for ln in lines]
    text(slide, x + 12, y + 27, w - 24, h - 34, paras, font="Consolas", size=size, c=INK, anchor="t", markup=False)


def node(slide, x, y, w, h, label=None, name=None, caption=None, kind=BLUE, lw=1.2, fill=CARD, sizes=(8, 11, 9)):
    box(slide, x, y, w, h, fill=fill, line=kind or LINE, lw=lw, shape=ROUND, adj=0.167)
    paras = []
    if label:
        paras.append((label.upper(), {"size": sizes[0], "c": TEXT.get(kind, MUTED), "bold": True, "after": 2}))
    if name:
        paras.append((name, {"size": sizes[1], "c": INK, "bold": True, "after": 1}))
    if caption:
        paras.append((caption, {"size": sizes[2], "c": MUTED}))
    text(slide, x + 5, y + 2, w - 10, h - 4, paras, font="Aptos", align="c", markup=False)


def service(slide, x, y, w, h, label, tag, name, detail, kind, lw=1.8):
    box(slide, x, y, w, h, fill=CARD, line=kind, lw=lw, shape=ROUND, adj=0.167)
    text(slide, x + 14.4, y + 9, 120, 12, label.upper(), font="Aptos", size=8, c=TEXT[kind], bold=True, markup=False)
    text(slide, x + w - 144.4, y + 9, 130, 12, tag.upper(), font="Aptos", size=8, c=MUTED, bold=True, align="r",
         markup=False)
    text(slide, x + 14.4, y + 24, 100, h - 30, name, font="Aptos", size=12, c=INK, bold=True, anchor="t",
         markup=False)
    text(slide, x + w - 154.4, y + 24, 140, h - 30, detail, font="Aptos", size=9, c=BODY, align="r", anchor="t",
         markup=False)


def pill(slide, x, y, w, h, s, color=BLUE, size=9):
    box(slide, x, y, w, h, fill=WHITE, line=LINE, lw=0.5, shape=ROUND, adj=0.5)
    text(slide, x + 4, y, w - 8, h, s, font="Aptos", size=size, c=color, bold=True, align="c", markup=False)


def colhead(slide, x, y, w, s):
    text(slide, x, y, w, 17.3, s.upper(), font="Aptos", size=9, c=MUTED, bold=True, align="c", markup=False)


def tile(slide, x, y, w, h, label, name, caption, color):
    box(slide, x, y, w, h, fill=CARD, line=LINE, shape=ROUND, adj=0.06)
    box(slide, x, y, 5, h, fill=color)
    text(slide, x + 14.4, y + 12, w - 27.4, 12, label.upper(), font="Aptos", size=8.5, c=TEXT[color], bold=True,
         markup=False)
    text(slide, x + 14.4, y + 28, w - 27.4, 20, name, size=15, c=INK, bold=True, markup=False)
    text(slide, x + 14.4, y + 54, w - 27.4, h - 60, caption, size=11.5, c=BODY, anchor="t", markup=False)
