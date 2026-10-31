# -*- coding: utf-8 -*-
"""v6 -> v7: rounder & flashier while keeping the reference palette.
Full-pill banners, navy circular number badges, solid pill status tags,
bigger card radius, stronger soft shadows, bolder chart. Text untouched.
"""
from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from lxml import etree

SRC = "outputs/stage1_research_update/Stage1_Research_Update_20261009_v6.pptx"
DST = "outputs/stage1_research_update/Stage1_Research_Update_20261009_v7.pptx"

A = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
NAVY, RED_DEEP, T_BLUE, T_RED = "2F496B", "A94F45", "ECF1F7", "F8ECEB"
WHITE = "FFFFFF"

prs = Presentation(SRC)
slides = list(prs.slides)


def shp(sl, sid):
    for s in sl.shapes:
        if s.shape_id == sid:
            return s
    raise KeyError(sid)


def spPr_of(s):
    return getattr(s._element, "spPr", None)


FULL_PILL = {(0, 43), (1, 29), (2, 24), (3, 49), (6, 25)}          # banners
CIRCLES = {(4, 6): 3.085, (4, 12): 5.135, (4, 18): 7.185}          # chip -> badge
NAVY_SOLID = {(5, 14)}                                              # chain endpoint
WHITE_CARDS = {                                                     # keep shadow set
    (0, 6), (0, 9), (0, 12), (0, 15), (0, 18), (0, 34), (0, 37),
    (1, 5), (1, 9), (1, 13), (1, 17), (1, 23),
    (2, 5), (2, 13),
    (3, 10), (3, 23), (3, 36),
    (4, 5), (4, 11), (4, 17),
    (5, 5), (5, 15), (5, 19), (5, 24), (5, 8), (5, 10), (5, 12),
    (6, 5), (6, 12), (6, 19),
}


def set_round(s, radius_in):
    spPr = spPr_of(s)
    geom = spPr.find(f"{{{A}}}prstGeom")
    if geom is None or geom.get("prst") != "roundRect":
        return
    min_in = min(s.width, s.height) / 914400
    adj = max(2000, min(50000, int(radius_in / min_in * 100000)))
    geom.find(f"{{{A}}}avLst")[0].set("fmla", f"val {adj}")


def set_full_pill(s):
    spPr = spPr_of(s)
    geom = spPr.find(f"{{{A}}}prstGeom")
    geom.find(f"{{{A}}}avLst")[0].set("fmla", "val 50000")


def add_shadow(spPr, blur=152400, dist=38100, alpha=9000):
    for el in spPr.findall(f"{{{A}}}effectLst"):
        spPr.remove(el)
    spPr.append(etree.fromstring(
        f'<a:effectLst xmlns:a="{A}"><a:outerShdw blurRad="{blur}" dist="{dist}" '
        f'dir="5400000" rotWithShape="0"><a:srgbClr val="262B33">'
        f'<a:alpha val="{alpha}"/></a:srgbClr></a:outerShdw></a:effectLst>'))


# 1) radius pass
for idx, sl in enumerate(slides):
    for s in sl.shapes:
        spPr = spPr_of(s)
        if spPr is None:
            continue
        key = (idx, s.shape_id)
        if key in FULL_PILL:
            set_full_pill(s)
        elif key in CIRCLES:
            s.left, s.top = Inches(0.975), Inches(CIRCLES[key])
            s.width = s.height = Inches(0.68)
            geom = spPr.find(f"{{{A}}}prstGeom")
            geom.set("prst", "ellipse")
            for av in geom.findall(f"{{{A}}}avLst"):
                geom.remove(av)
            etree.SubElement(geom, f"{{{A}}}avLst")
        elif key in NAVY_SOLID:
            set_round(s, 0.16)
            fill = spPr.find(f"{{{A}}}solidFill/{{{A}}}srgbClr")
            fill.set("val", NAVY)
        else:
            set_round(s, 0.16)
        if key in WHITE_CARDS:
            add_shadow(spPr_of(s))

# 2) badge numbers: white, centered in circle
for sid, t in ((7, 3.15), (13, 5.20), (19, 7.25)):
    s = shp(slides[4], sid)
    s.left, s.width = Inches(0.975), Inches(0.68)
    s.top, s.height = Inches(t), Inches(0.55)
    tf = s.text_frame
    for p in tf.paragraphs:
        p.alignment = PP_ALIGN.CENTER
        for r in p.runs:
            r.font.color.rgb = RGBColor.from_string(WHITE)
            r.font.size = Pt(22)

# chain endpoint text -> white
for r in [r for p in shp(slides[5], 14).text_frame.paragraphs for r in p.runs]:
    r.font.color.rgb = RGBColor.from_string(WHITE)

# 3) solid/tint pill tags behind status words --------------------------------
def add_pill(slide_idx, text_id, L, T, W, H, fill, text_color):
    sl = slides[slide_idx]
    txt = shp(sl, text_id)
    txt.left, txt.top, txt.width, txt.height = Inches(L), Inches(T), Inches(W), Inches(H)
    for p in txt.text_frame.paragraphs:
        p.alignment = PP_ALIGN.CENTER
    for r in [r for p in txt.text_frame.paragraphs for r in p.runs]:
        r.font.color.rgb = RGBColor.from_string(text_color)
    # create pill shape, insert BEHIND the text element
    spTree = sl.shapes._spTree
    pill = etree.fromstring(
        f'<p:sp xmlns:p="{P}" xmlns:a="{A}">'
        f'<p:nvSpPr><p:cNvPr id="0" name="pill"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
        f'<p:spPr><a:xfrm><a:off x="{int(L*914400)}" y="{int(T*914400)}"/>'
        f'<a:ext cx="{int(W*914400)}" cy="{int(H*914400)}"/></a:xfrm>'
        f'<a:prstGeom prst="roundRect"><a:avLst><a:gd name="adj" fmla="val 50000"/></a:avLst></a:prstGeom>'
        f'<a:solidFill><a:srgbClr val="{fill}"/></a:solidFill><a:ln><a:noFill/></a:ln></p:spPr>'
        f'<p:txBody><a:bodyPr/><a:lstStyle/><a:p/></p:txBody></p:sp>')
    spTree.insert(list(spTree).index(txt._element), pill)


add_pill(0, 21, 1.33, 3.92, 1.40, 0.54, T_BLUE, NAVY)          # COC / TFC
add_pill(0, 26, 4.335, 5.10, 2.05, 0.54, T_RED, RED_DEEP)      # E1：删除这项事实
add_pill(1, 26, 12.765, 5.42, 2.50, 0.56, T_BLUE, NAVY)        # V0 检查并记录
add_pill(1, 27, 12.765, 6.22, 2.50, 0.56, NAVY, WHITE)         # V1 缺项由 Gate 拦截
add_pill(2, 17, 13.28, 3.42, 1.04, 0.52, T_BLUE, NAVY)         # A1V0
add_pill(2, 21, 13.28, 5.74, 1.04, 0.52, NAVY, WHITE)          # A1V1
add_pill(3, 21, 13.82, 3.23, 1.68, 0.55, NAVY, WHITE)          # ESCALATE (A)
add_pill(3, 34, 13.82, 4.96, 1.68, 0.55, RED_DEEP, WHITE)      # ACCEPT
add_pill(3, 47, 13.82, 6.69, 1.68, 0.55, NAVY, WHITE)          # ESCALATE (C)

# 4) chart: bolder — no gridlines, thicker bars, bold labels
for s in slides[2].shapes:
    if str(s.shape_type).startswith("CHART"):
        cs = s.chart._chartSpace
        C = "{http://schemas.openxmlformats.org/drawingml/2006/chart}"
        bc = cs.find(f".//{C}barChart")
        gw = bc.find(f"{C}gapWidth")
        if gw is not None:
            gw.set("val", "70")
        for vax in cs.findall(f".//{C}valAx"):
            gr = vax.find(f"{C}majorGridlines")
            if gr is not None:
                vax.remove(gr)
        for dl in cs.findall(f".//{C}dLbls"):
            for defRPr in dl.findall(f".//{{{A}}}defRPr"):
                defRPr.set("sz", "1400")
                defRPr.set("b", "1")

prs.save(DST)
print("saved", DST)

# verify text identity vs v4 (non-empty paras)
def all_texts(path):
    p = Presentation(path)
    return [(i+1, s.shape_id, para.text) for i, sl in enumerate(p.slides)
            for s in sl.shapes
            if getattr(s, "has_text_frame", False) and s.has_text_frame
            for para in s.text_frame.paragraphs if para.text.strip()]

orig = all_texts("outputs/stage1_research_update/Stage1_Research_Update_20261009_v4.pptx")
new = all_texts(DST)
diff = [(a, b) for a, b in zip(orig, new) if a[2] != b[2]]
print("TEXT-IDENTITY vs v4:", "PASS" if not diff and len(orig) == len(new)
      else f"FAIL {len(orig)}->{len(new)} {diff[:3]}")
