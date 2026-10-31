# -*- coding: utf-8 -*-
"""Restyle Stage1 v5 -> v6 to match FINAL_MAS_ARCHITECTURE_EVOLUTION reference:
warm paper bg FAF9F5, white rounded cards with soft shadows (no borders),
category tint header bands (blue/green/red/orange), charcoal ink 262B33,
navy 2F496B emphasis, mid-blue 3E5F8A. Text stays byte-identical.
"""
from pptx import Presentation
from pptx.util import Pt
from pptx.dml.color import RGBColor
from lxml import etree

SRC = "outputs/stage1_research_update/Stage1_Research_Update_20261009_v5.pptx"
DST = "outputs/stage1_research_update/Stage1_Research_Update_20261009_v6.pptx"

A = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
ns = {"a": A, "p": P}

BG = "FAF9F5"
INK = "262B33"
NAVY = "2F496B"
MIDBLUE = "3E5F8A"
GREY = "545D6B"
PAGE = "8A929E"
T_BLUE, T_GREEN, T_RED, T_ORANGE = "ECF1F7", "ECF3EE", "F8ECEB", "F8F0E5"
WARM_LINE = "E2DFD6"
RED_DEEP = "A94F45"

# ---- classification tables: (slide_idx0, shape_id) -> role
TINT = {  # tint-filled, no border, no shadow
    # S3 right panel header = red (错误 ACCEPT); stat cards tinted
    (2, 14): T_RED, (2, 16): T_BLUE, (2, 20): T_RED, (2, 24): T_RED,
    # S4 trace label cards: A green / B red / C orange
    (3, 11): T_GREEN, (3, 24): T_RED, (3, 37): T_ORANGE,
    # S5 chip columns blue; right stat boxes blue/red/orange
    (4, 6): T_BLUE, (4, 12): T_BLUE, (4, 18): T_BLUE,
    (4, 10): T_BLUE, (4, 16): T_RED, (4, 22): T_ORANGE,
    # S6 chain endpoint 治理干预 green
    (5, 14): T_GREEN,
    # bottom banners / status strips
    (0, 43): T_ORANGE, (1, 29): T_GREEN, (3, 49): T_GREEN, (6, 25): T_GREEN,
}
WHITE_CARD = {  # white rounded card + shadow, no border
    (0, 6), (0, 9), (0, 12), (0, 15), (0, 18), (0, 34), (0, 37),
    (1, 5), (1, 9), (1, 13), (1, 17), (1, 23),
    (2, 5), (2, 13),
    (3, 10), (3, 23), (3, 36),
    (4, 5), (4, 11), (4, 17),
    (5, 5), (5, 15), (5, 19), (5, 24), (5, 8), (5, 10), (5, 12),
    (6, 5), (6, 12), (6, 19),
}
BAND = {  # header bands: default tint blue unless overridden in TINT/BAND
    (0, 7), (0, 10), (0, 13), (0, 16), (0, 19), (0, 35), (0, 38),
    (1, 10), (1, 14), (1, 18), (1, 24),
    (2, 6), (2, 14),
    (5, 6), (5, 16), (5, 20), (5, 25),
    (6, 6), (6, 13), (6, 20),
}
BAND = {k: T_BLUE for k in BAND}  # TINT keys take precedence in the branch below

TEXT_COLOR = {  # per-shape overrides applied after global map
    (2, 25): INK,          # S3 red banner text -> charcoal
    (3, 34): RED_DEEP,     # ACCEPT -> deep red
    (3, 50): INK,          # S4 green banner text -> charcoal
    (4, 26): RED_DEEP,     # ≠ -> deep red
    (6, 26): INK,          # S7 green banner text -> charcoal
    (0, 45): PAGE, (1, 32): PAGE, (2, 26): PAGE, (3, 51): PAGE,
    (4, 29): PAGE, (5, 28): PAGE, (6, 27): PAGE,  # page numbers
}
COLOR_MAP = {"102D3E": INK, "477E78": NAVY, "697C82": GREY}

prs = Presentation(SRC)
slides = list(prs.slides)


def spPr_of(s):
    return getattr(s._element, "spPr", None)


def set_fill(spPr, hexval):
    for tag in ("solidFill", "noFill", "gradFill", "blipFill", "pattFill"):
        for el in spPr.findall(f"{{{A}}}{tag}"):
            spPr.remove(el)
    fill = etree.SubElement(spPr, f"{{{A}}}solidFill")
    etree.SubElement(fill, f"{{{A}}}srgbClr").set("val", hexval)
    # keep schema order: fill must come before ln/effectLst — reinsert right after prstGeom
    spPr.remove(fill)
    geom = spPr.find(f"{{{A}}}prstGeom")
    if geom is not None:
        geom.addnext(fill)
    else:
        spPr.insert(0, fill)


def no_line(spPr):
    ln = spPr.find(f"{{{A}}}ln")
    if ln is None:
        ln = etree.SubElement(spPr, f"{{{A}}}ln")
    for ch in list(ln):
        ln.remove(ch)
    etree.SubElement(ln, f"{{{A}}}noFill")


def round_corners(spPr, radius_in=0.075):
    geom = spPr.find(f"{{{A}}}prstGeom")
    if geom is None:
        return
    geom.set("prst", "roundRect")
    av = geom.find(f"{{{A}}}avLst")
    if av is None:
        av = etree.SubElement(geom, f"{{{A}}}avLst")
    for gd in list(av):
        av.remove(gd)
    etree.SubElement(av, f"{{{A}}}gd").set("name", "adj")
    av[0].set("fmla", "val 1")  # placeholder, set below with real adj


def set_round_adj(s, radius_in=0.075):
    spPr = spPr_of(s)
    if spPr is None:
        return
    geom = spPr.find(f"{{{A}}}prstGeom")
    if geom is None or geom.get("prst") != "roundRect":
        return
    min_in = min(s.width, s.height) / 914400
    adj = max(2000, min(50000, int(radius_in / min_in * 100000)))
    av = geom.find(f"{{{A}}}avLst")
    av[0].set("fmla", f"val {adj}")


def add_shadow(spPr):
    for el in spPr.findall(f"{{{A}}}effectLst"):
        spPr.remove(el)
    eff = etree.fromstring(
        f'<a:effectLst xmlns:a="{A}"><a:outerShdw blurRad="114300" dist="25400" '
        f'dir="5400000" rotWithShape="0"><a:srgbClr val="262B33">'
        f'<a:alpha val="7000"/></a:srgbClr></a:outerShdw></a:effectLst>')
    spPr.append(eff)


# 1) backgrounds + delete teal title rule (id3 on every slide)
for sl in slides:
    bg = sl._element.find(f"{{{P}}}bg")
    if bg is not None:
        for c in bg.findall(f".//{{{A}}}srgbClr"):
            c.set("val", BG)
    for s in list(sl.shapes):
        spPr = spPr_of(s)
        if spPr is None:
            continue
        sf = spPr.find(f"{{{A}}}solidFill/{{{A}}}srgbClr")
        if s.shape_id == 3 and sf is not None and sf.get("val") == "477E78":
            s._element.getparent().remove(s._element)

# 2) shape restyle by class
for idx, sl in enumerate(slides):
    for s in sl.shapes:
        spPr = spPr_of(s)
        if spPr is None:
            continue
        key = (idx, s.shape_id)
        if key in TINT:
            set_fill(spPr, TINT[key]); no_line(spPr)
            round_corners(spPr); set_round_adj(s, 0.075)
        elif key in WHITE_CARD:
            set_fill(spPr, "FFFFFF"); no_line(spPr)
            round_corners(spPr); set_round_adj(s, 0.075)
            add_shadow(spPr)
        elif key in BAND:
            set_fill(spPr, BAND[key]); no_line(spPr)
            round_corners(spPr); set_round_adj(s, 0.075)
        else:
            sf = spPr.find(f"{{{A}}}solidFill/{{{A}}}srgbClr")
            cur = sf.get("val") if sf is not None else None
            if cur == "B7C7C4":                       # S7 dividers -> warm line
                sf.set("val", WARM_LINE)
            elif cur == "102D3E":                     # S1 strike -> deep red
                sf.set("val", RED_DEEP)

# 3) text colors
for idx, sl in enumerate(slides):
    for s in sl.shapes:
        if not getattr(s, "has_text_frame", False) or not s.has_text_frame:
            continue
        for para in s.text_frame.paragraphs:
            for r in para.runs:
                try:
                    cur = str(r.font.color.rgb)
                except Exception:
                    cur = None
                if cur in COLOR_MAP:
                    r.font.color.rgb = RGBColor.from_string(COLOR_MAP[cur])
        ov = TEXT_COLOR.get((idx, s.shape_id))
        if ov:
            for para in s.text_frame.paragraphs:
                for r in para.runs:
                    r.font.color.rgb = RGBColor.from_string(ov)

# 4) chart recolor
for s in slides[2].shapes:
    if str(s.shape_type).startswith("CHART"):
        cs = s.chart._chartSpace
        C = "{http://schemas.openxmlformats.org/drawingml/2006/chart}"
        # series colors
        sers = cs.findall(f".//{C}ser")
        cols = [NAVY, MIDBLUE]
        for i, ser in enumerate(sers):
            sp = ser.find(f"{C}spPr")
            if sp is None:
                sp = etree.SubElement(ser, C + "spPr")
            for ch in list(sp):
                sp.remove(ch)
            fill = etree.SubElement(sp, f"{{{A}}}solidFill")
            etree.SubElement(fill, f"{{{A}}}srgbClr").set("val", cols[i % 2])
        # recolor text fragments
        for el in cs.iter(f"{{{A}}}srgbClr"):
            v = el.get("val")
            if v == "477E78":
                el.set("val", NAVY)
            elif v == "102D3E":
                el.set("val", INK)
            elif v == "697C82":
                el.set("val", GREY)
            elif v == "E7E3D9":
                el.set("val", WARM_LINE)

prs.save(DST)
print("saved", DST)

# verification: text identity vs v4 (source of truth)
def all_texts(path):
    p = Presentation(path)
    return [(i + 1, s.shape_id, para.text) for i, sl in enumerate(p.slides)
            for s in sl.shapes
            if getattr(s, "has_text_frame", False) and s.has_text_frame
            for para in s.text_frame.paragraphs]

orig = all_texts("outputs/stage1_research_update/Stage1_Research_Update_20261009_v4.pptx")
new = all_texts(DST)
# v4 had no deleted shapes with text (rule id3 had none) so counts must match
diff = [(a, b) for a, b in zip(orig, new) if a != b]
print("TEXT-IDENTITY vs v4:", "PASS" if not diff and len(orig) == len(new)
      else f"FAIL count {len(orig)}->{len(new)} diff {diff[:3]}")
