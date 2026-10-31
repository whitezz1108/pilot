"""Restyle flowcharts on slides 2-4 to match the reference image style.

Keeps content, positions and structure unchanged; only restyles:
- node cards: rect -> roundRect (soft radius), white fill, light blue-gray 1pt border
- colored top bars: rect -> round2SameRect (rounded top corners matching the card),
  slightly taller so bar top radius == card corner radius
- flow arrows: solid triangle arrowheads (med), recolored to the target node's lane color
- slide 3 numbered node titles: colored number prefix + navy name (reference typography)
"""
import copy
import re

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.oxml.ns import qn
from pptx.util import Emu, Pt

SRC = r"C:\Users\44977\Desktop\CUAD_MAS_Stage1_Validator_Audit_20261010.pptx"
DST = r"C:\Users\44977\Desktop\CUAD_MAS_Stage1_Validator_Audit_20261010_updated.pptx"

BLUE, AMBER, TEAL, PURPLE = "4267D5", "C38935", "16998B", "8364B5"
NAVY, BORDER = "16233B", "DCE2EC"


def shape_by_id(slide, sid):
    for s in slide.shapes:
        if s.shape_id == sid:
            return s
    raise KeyError(f"shape {sid} not found")


def set_geom(shape, prst, **adj):
    from pptx.oxml import parse_xml
    from pptx.oxml.ns import nsdecls

    gd = "".join(f'<a:gd name="{k}" fmla="val {v}"/>' for k, v in adj.items())
    new = parse_xml(
        f'<a:prstGeom {nsdecls("a")} prst="{prst}"><a:avLst>{gd}</a:avLst></a:prstGeom>'
    )
    spPr = shape._element.spPr
    spPr.replace(spPr.find(qn("a:prstGeom")), new)


def round_card(shape, radius_in):
    ss = Emu(min(shape.width, shape.height)).inches
    adj = min(int(radius_in / ss * 100000), 49999)
    set_geom(shape, "roundRect", adj=adj)
    shape.fill.solid()
    shape.fill.fore_color.rgb = RGBColor.from_string("FFFFFF")
    shape.line.color.rgb = RGBColor.from_string(BORDER)
    shape.line.width = Pt(1)


def round_bar(shape, height_emu):
    set_geom(shape, "round2SameRect", adj1=50000, adj2=0)
    shape.height = Emu(height_emu)


def style_arrow(shape, color=None, head="triangle", w="med", ln="med"):
    if color:
        shape.line.color.rgb = RGBColor.from_string(color)
    lnEl = shape.line._get_or_add_ln()
    tail = lnEl.find(qn("a:tailEnd"))
    if tail is None:
        tail = lnEl.makeelement(qn("a:tailEnd"), {})
        lnEl.append(tail)
    tail.attrib.update({"type": head, "w": w, "len": ln})


def split_numbered_title(shape):
    """'NN 名称' -> run 'NN ' keeps lane color, run '名称' becomes navy."""
    for para in shape.text_frame.paragraphs:
        runs = para.runs
        if not runs:
            continue
        m = re.match(r"^(\d{2})\s+(.*)$", runs[0].text, re.S)
        if not m:
            continue
        r1 = runs[0]._r
        r2 = copy.deepcopy(r1)
        r1.addnext(r2)
        runs[0].text = m.group(1) + " "
        # second run: same formatting, navy ink
        from pptx.text.text import _Run
        _Run(r2, para).font.color.rgb = RGBColor.from_string(NAVY)
        _Run(r2, para).text = m.group(2)


prs = Presentation(SRC)

# ---------- slide 2 (idx 1): 4-node pipeline ----------
s = prs.slides[1]
for cid in (17, 21, 25, 29):
    round_card(shape_by_id(s, cid), 0.07)
for bid in (18, 22, 26, 30):
    round_bar(shape_by_id(s, bid), Emu(0.14).emu)
for aid, col in ((33, AMBER), (34, BLUE), (35, TEAL)):
    style_arrow(shape_by_id(s, aid), color=col)

# ---------- slide 3 (idx 2): 6-node serial pipeline ----------
s = prs.slides[2]
for cid in (12, 13, 14, 15, 16, 17):
    round_card(shape_by_id(s, cid), 0.07)
for bid in (18, 20, 22, 24, 26, 28):
    round_bar(shape_by_id(s, bid), Emu(0.14).emu)
for aid in (36, 37, 38, 39, 40):
    style_arrow(shape_by_id(s, aid))
# annotation bracket ticks: subtle small heads
for tid in (9, 10, 42, 43):
    style_arrow(shape_by_id(s, tid), head="triangle", w="sm", ln="sm")
for tshp in (19, 21, 23, 25, 27, 29):
    split_numbered_title(shape_by_id(s, tshp))

# ---------- slide 4 (idx 3): before/after rows ----------
s = prs.slides[3]
for cid in (9, 12, 15, 18, 25, 28, 31, 34, 37, 40):
    round_card(shape_by_id(s, cid), 0.06)
for bid in (10, 13, 16, 19, 26, 29, 32, 35, 38, 41):
    round_bar(shape_by_id(s, bid), Emu(0.12).emu)
for aid, col in ((21, BLUE), (22, PURPLE), (23, PURPLE)):
    style_arrow(shape_by_id(s, aid), color=col)
for aid in (43, 44, 45, 46, 47):
    style_arrow(shape_by_id(s, aid))

prs.save(DST)
print("saved", DST)
