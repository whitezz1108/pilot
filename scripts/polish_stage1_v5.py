# -*- coding: utf-8 -*-
"""Polish Stage1_Research_Update_20261009_v4.pptx -> v5.

Rules: every run's text stays byte-identical. Only geometry, font sizes,
text-frame insets/anchors, shape lines/fills, and chart styling change.
"""
import copy
from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.dml.color import RGBColor
from pptx.enum.text import MSO_ANCHOR
from lxml import etree

SRC = "outputs/stage1_research_update/Stage1_Research_Update_20261009_v4.pptx"
DST = "outputs/stage1_research_update/Stage1_Research_Update_20261009_v5.pptx"

BORDER = RGBColor(0xB7, 0xC7, 0xC4)
CARD = "FCFBF7"

prs = Presentation(SRC)
S = list(prs.slides)


def shp(slide, sid):
    for s in slide.shapes:
        if s.shape_id == sid:
            return s
    raise KeyError(f"shape {sid} not on slide")


def set_text(slide, sid, L=None, T=None, W=None, H=None, sz=None,
             anchor=None, spc_after=None, align=None):
    s = shp(slide, sid)
    tf = s.text_frame
    if L is not None: s.left = Inches(L)
    if T is not None: s.top = Inches(T)
    if W is not None: s.width = Inches(W)
    if H is not None: s.height = Inches(H)
    if anchor is not None:
        tf.vertical_anchor = MSO_ANCHOR.MIDDLE if anchor == "m" else MSO_ANCHOR.TOP
    tf.margin_left = 0; tf.margin_right = 0
    tf.margin_top = 0; tf.margin_bottom = 0
    tf.word_wrap = True
    if sz is not None:
        for p in tf.paragraphs:
            for r in p.runs:
                r.font.size = Pt(sz)
    if spc_after is not None:
        for p in tf.paragraphs:
            p.space_after = Pt(spc_after)
    if align is not None:
        from pptx.enum.text import PP_ALIGN
        for p in tf.paragraphs:
            p.alignment = {"l": PP_ALIGN.LEFT, "c": PP_ALIGN.CENTER,
                           "r": PP_ALIGN.RIGHT}[align]


def style_card(slide, sid, border_pt=1.0):
    """Card containers keep their fill, get a unified hairline border."""
    s = shp(slide, sid)
    s.line.color.rgb = BORDER
    s.line.width = Pt(border_pt)


def rule_h(slide, sid, pts=1.0):
    s = shp(slide, sid)
    s.height = Pt(pts)  # thickness of the bar = line weight equivalent


def geom(slide, sid, L=None, T=None, W=None, H=None):
    s = shp(slide, sid)
    if L is not None: s.left = Inches(L)
    if T is not None: s.top = Inches(T)
    if W is not None: s.width = Inches(W)
    if H is not None: s.height = Inches(H)


# ============ SLIDE 1 ============
sl = S[0]
set_text(sl, 2, L=0.83, T=0.57, W=14.95, H=0.67, sz=30, anchor="m")
rule_h(sl, 3, 1.0)
set_text(sl, 4, L=0.83, T=1.73, W=14.95, H=0.52, sz=21, anchor="m")
set_text(sl, 5, L=0.83, T=2.38, W=8.33, H=0.46, sz=22, anchor="m")
for c in (6, 9, 12, 15, 18, 34, 37, 43):
    style_card(sl, c)
for b in (7, 10, 13, 16, 19, 35, 38):
    style_card(sl, b)
# flow header texts: centered in band, uniform inset
set_text(sl, 8,  L=1.07, T=3.11, W=1.92, H=0.44, sz=20, anchor="m")
set_text(sl, 11, L=4.04, T=3.11, W=2.64, H=0.44, sz=20, anchor="m")
set_text(sl, 14, L=7.74, T=3.11, W=1.86, H=0.44, sz=20, anchor="m")
set_text(sl, 17, L=10.66, T=3.11, W=1.86, H=0.44, sz=20, anchor="m")
set_text(sl, 20, L=13.57, T=3.11, W=2.02, H=0.44, sz=20, anchor="m")
# 原始合同 body (recenter stack in 3.60..5.90)
set_text(sl, 21, L=1.07, T=3.95, W=1.98, H=0.44, sz=20, anchor="m")
set_text(sl, 22, L=1.07, T=4.55, W=1.98, H=0.42, sz=17, anchor="m")
# Analyst stack
set_text(sl, 23, L=4.04, T=3.85, W=2.67, H=0.42, sz=17, anchor="m")
set_text(sl, 24, L=4.04, T=4.43, W=2.60, H=0.47, sz=21, anchor="m")
geom(sl, 25, L=4.04, T=4.68, W=1.70, H=0.022)          # strike trimmed to text width
set_text(sl, 26, L=4.04, T=5.17, W=2.67, H=0.44, sz=18, anchor="m")
# single-line cells centered on body-zone center 4.75
set_text(sl, 27, L=7.74, T=4.55, W=1.86, H=0.40, sz=20, anchor="m", align="c")
set_text(sl, 28, L=10.66, T=4.55, W=1.86, H=0.40, sz=20, anchor="m", align="c")
set_text(sl, 29, L=13.55, T=4.34, W=2.10, H=0.82, sz=19, anchor="m", align="c")
for a in (30, 31, 32, 33):
    set_text(sl, a, T=4.385, H=0.73, sz=24, anchor="m")
# bottom cards
set_text(sl, 36, L=1.07, T=6.34, W=6.85, H=0.44, sz=20, anchor="m")
set_text(sl, 39, L=8.78, T=6.34, W=6.85, H=0.44, sz=20, anchor="m")
set_text(sl, 40, L=1.07, T=7.08, W=6.76, H=0.42, sz=19, anchor="m")
set_text(sl, 41, L=8.78, T=6.93, W=2.99, H=0.45, sz=18, anchor="m")
set_text(sl, 42, L=8.78, T=7.29, W=6.75, H=0.42, sz=18, anchor="m")
# status strip
geom(sl, 43, T=7.99)
set_text(sl, 44, L=1.07, T=8.13, W=14.28, H=0.42, sz=18, anchor="m")

# ============ SLIDE 2 ============
sl = S[1]
set_text(sl, 2, L=0.83, T=0.57, W=14.95, H=0.67, sz=30, anchor="m")
rule_h(sl, 3, 1.0)
set_text(sl, 4, L=0.83, T=1.73, W=14.95, H=0.52, sz=21, anchor="m")
for c in (5, 9, 13, 17, 23, 29):
    style_card(sl, c)
for b in (10, 14, 18, 24):
    style_card(sl, b)
set_text(sl, 6, L=1.07, T=2.60, W=14.06, H=0.42, sz=20, anchor="m")
set_text(sl, 7, L=1.07, T=3.06, W=14.06, H=0.35, sz=17, anchor="m")
set_text(sl, 8, L=0.83, T=3.70, W=13.96, H=0.48, sz=22, anchor="m")
set_text(sl, 11, L=1.07, T=4.48, W=3.22, H=0.44, sz=20, anchor="m")
set_text(sl, 12, L=1.07, T=5.16, W=3.22, H=0.40, sz=19, anchor="m")
geom(sl, 13, H=1.55)
set_text(sl, 15, L=1.07, T=6.08, W=3.22, H=0.44, sz=20, anchor="m")
set_text(sl, 16, L=1.07, T=6.71, W=3.22, H=0.73, sz=19, anchor="t", spc_after=6)
set_text(sl, 19, L=5.60, T=4.48, W=5.61, H=0.44, sz=20, anchor="m")
set_text(sl, 20, L=5.60, T=5.63, W=5.61, H=1.30, sz=19, anchor="m", spc_after=6)
set_text(sl, 25, L=12.43, T=4.48, W=3.21, H=0.44, sz=20, anchor="m")
set_text(sl, 26, L=12.43, T=5.50, W=3.21, H=0.45, sz=19, anchor="m")
set_text(sl, 27, L=12.43, T=6.30, W=3.21, H=0.45, sz=19, anchor="m")
set_text(sl, 21, L=4.81, T=4.73, W=0.68, H=0.73, sz=24, anchor="m")
set_text(sl, 22, L=4.81, T=6.44, W=0.68, H=0.73, sz=24, anchor="m")
set_text(sl, 28, L=11.49, T=5.64, W=0.66, H=0.73, sz=24, anchor="m")
set_text(sl, 30, L=1.07, T=8.06, W=9.00, H=0.44, sz=19, anchor="m")
set_text(sl, 31, L=10.40, T=8.06, W=5.21, H=0.44, sz=19, anchor="m", align="r")

# ============ SLIDE 3 ============
sl = S[2]
set_text(sl, 2, L=0.83, T=0.57, W=14.95, H=0.67, sz=30, anchor="m")
rule_h(sl, 3, 1.0)
set_text(sl, 4, L=0.83, T=1.73, W=14.95, H=0.52, sz=21, anchor="m")
for c in (5, 13, 24):
    style_card(sl, c)
for b in (6, 14):
    style_card(sl, b)
set_text(sl, 7, L=1.07, T=2.46, W=10.21, H=0.44, sz=20, anchor="m")
set_text(sl, 9, L=1.07, T=7.41, W=1.56, H=0.39, sz=17, anchor="m")
set_text(sl, 10, L=2.37, T=7.41, W=1.67, H=0.39, sz=18, anchor="m", align="c")
set_text(sl, 11, L=5.52, T=7.41, W=1.67, H=0.39, sz=18, anchor="m", align="c")
set_text(sl, 12, L=8.67, T=7.41, W=1.67, H=0.39, sz=18, anchor="m", align="c")
set_text(sl, 15, L=11.99, T=2.46, W=3.62, H=0.44, sz=20, anchor="m")
geom(sl, 16, T=3.30, H=2.10)   # stat card 1
set_text(sl, 17, L=12.21, T=3.46, W=3.18, H=0.39, sz=19, anchor="m")
set_text(sl, 18, L=12.21, T=3.95, W=3.08, H=0.68, sz=34, anchor="m")
set_text(sl, 19, L=12.21, T=4.80, W=3.08, H=0.39, sz=17, anchor="m")
geom(sl, 20, T=5.62, H=2.10)   # stat card 2
set_text(sl, 21, L=12.21, T=5.78, W=3.18, H=0.39, sz=19, anchor="m")
set_text(sl, 22, L=12.21, T=6.27, W=3.08, H=0.68, sz=34, anchor="m")
set_text(sl, 23, L=12.21, T=7.12, W=3.08, H=0.39, sz=17, anchor="m")
set_text(sl, 25, L=1.07, T=8.28, W=14.26, H=0.42, sz=20, anchor="m")

# ---- chart styling (XML level) ----
chart = None
for s in S[2].shapes:
    if str(s.shape_type).startswith("CHART"):
        chart = s.chart
cs = chart._chartSpace
C = "{http://schemas.openxmlformats.org/drawingml/2006/chart}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def make_txpr(size, color, bold=False):
    txPr = etree.fromstring(
        f'<c:txPr xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" '
        f'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        f'<a:bodyPr/><a:lstStyle/><a:p><a:pPr><a:defRPr sz="{int(size*100)}" b="{1 if bold else 0}">'
        f'<a:solidFill><a:srgbClr val="{color}"/></a:solidFill>'
        f'<a:latin typeface="Microsoft YaHei"/><a:ea typeface="Microsoft YaHei"/>'
        f'</a:defRPr></a:pPr><a:endParaRPr lang="zh-CN"/></a:p></c:txPr>')
    return txPr


# gap width / overlap
bc = cs.find(f".//{C}barChart")
gw = bc.find(f"{C}gapWidth")
if gw is not None:
    gw.set("val", "95")
ov = bc.find(f"{C}overlap")
if ov is None:
    ov = etree.SubElement(bc, C + "overlap")
ov.set("val", "-12")

# legend to top, small font
leg = cs.find(f".//{C}legend")
if leg is not None:
    lp = leg.find(f"{C}legendPos")
    if lp is None:
        lp = etree.SubElement(leg, C + "legendPos")
    lp.set("val", "t")
    for old in leg.findall(f"{C}txPr"):
        leg.remove(old)
    leg.append(make_txpr(14, "697C82"))

# value axis: ticks every 20%, small font; gridlines light
for vax in cs.findall(f".//{C}valAx"):
    mu = vax.find(f"{C}majorUnit")
    if mu is None:
        mu = etree.SubElement(vax, C + "majorUnit")
    mu.set("val", "0.2")
    for old in vax.findall(f"{C}txPr"):
        vax.remove(old)
    vax.append(make_txpr(13, "697C82"))
    gr = vax.find(f"{C}majorGridlines")
    if gr is not None:
        sp = gr.find(f"{A}spPr")
        if sp is None:
            sp = etree.SubElement(gr, A + "spPr")
        for ch in list(sp):
            sp.remove(ch)
        etree.SubElement(sp, f"{A}ln").set("w", "9525")
        ln = sp.find(f"{A}ln")
        sf = etree.SubElement(ln, f"{A}solidFill")
        etree.SubElement(sf, f"{A}srgbClr").set("val", "E7E3D9")

# category axis font
for cax in cs.findall(f".//{C}catAx"):
    for old in cax.findall(f"{C}txPr"):
        cax.remove(old)
    cax.append(make_txpr(15, "102D3E"))

# data labels: neutral ink, smaller
for dl in cs.findall(f".//{C}dLbls"):
    for old in dl.findall(f"{C}txPr"):
        dl.remove(old)
    dl.append(make_txpr(13, "102D3E"))

# ============ SLIDE 4 ============
sl = S[3]
set_text(sl, 2, L=0.83, T=0.57, W=14.95, H=0.67, sz=30, anchor="m")
rule_h(sl, 3, 1.0)
set_text(sl, 4, L=0.83, T=1.73, W=14.95, H=0.52, sz=21, anchor="m")
# header row
set_text(sl, 5, L=1.05, T=2.47, W=2.59, H=0.40, sz=17, anchor="m")
set_text(sl, 6, L=4.11, T=2.47, W=1.84, H=0.40, sz=17, anchor="m")
set_text(sl, 7, L=6.56, T=2.47, W=2.60, H=0.40, sz=17, anchor="m")
set_text(sl, 8, L=10.00, T=2.47, W=2.60, H=0.40, sz=17, anchor="m")
set_text(sl, 9, L=13.65, T=2.47, W=2.02, H=0.40, sz=17, anchor="m")
for c in (10, 11, 23, 24, 36, 37, 49):
    style_card(sl, c)
# Row A
set_text(sl, 12, L=1.05, T=3.10, W=2.59, H=0.42, sz=19, anchor="m")
set_text(sl, 13, L=1.05, T=3.58, W=2.59, H=0.35, sz=17, anchor="m")
set_text(sl, 14, L=1.05, T=4.03, W=2.59, H=0.35, sz=15, anchor="m")
set_text(sl, 15, L=4.11, T=3.31, W=1.84, H=0.40, sz=19, anchor="m")
set_text(sl, 17, L=6.56, T=3.10, W=2.65, H=0.84, sz=19, anchor="t", spc_after=6)
set_text(sl, 19, L=10.00, T=3.31, W=2.60, H=0.40, sz=19, anchor="m")
set_text(sl, 21, L=13.65, T=3.31, W=2.02, H=0.40, sz=19, anchor="m")
set_text(sl, 16, L=5.97, T=3.145, W=0.57, H=0.73, sz=22, anchor="m")
set_text(sl, 18, L=9.35, T=3.145, W=0.65, H=0.73, sz=22, anchor="m")
set_text(sl, 20, L=12.80, T=3.145, W=0.68, H=0.73, sz=22, anchor="m")
set_text(sl, 22, L=4.11, T=4.07, W=11.50, H=0.38, sz=17, anchor="m")
# Row B
set_text(sl, 25, L=1.05, T=4.83, W=2.59, H=0.42, sz=19, anchor="m")
set_text(sl, 26, L=1.05, T=5.31, W=2.59, H=0.35, sz=17, anchor="m")
set_text(sl, 27, L=1.05, T=5.76, W=2.59, H=0.35, sz=15, anchor="m")
set_text(sl, 28, L=4.11, T=5.04, W=1.84, H=0.40, sz=19, anchor="m")
set_text(sl, 30, L=6.56, T=5.04, W=2.65, H=0.40, sz=19, anchor="m")
set_text(sl, 32, L=10.00, T=5.04, W=2.60, H=0.40, sz=19, anchor="m")
set_text(sl, 34, L=13.65, T=5.04, W=2.02, H=0.40, sz=19, anchor="m")
set_text(sl, 29, L=5.97, T=4.875, W=0.57, H=0.73, sz=22, anchor="m")
set_text(sl, 31, L=9.35, T=4.875, W=0.65, H=0.73, sz=22, anchor="m")
set_text(sl, 33, L=12.80, T=4.875, W=0.68, H=0.73, sz=22, anchor="m")
set_text(sl, 35, L=4.11, T=5.80, W=11.50, H=0.38, sz=17, anchor="m")
# Row C
set_text(sl, 38, L=1.05, T=6.56, W=2.59, H=0.42, sz=19, anchor="m")
set_text(sl, 39, L=1.05, T=7.04, W=2.59, H=0.35, sz=17, anchor="m")
set_text(sl, 40, L=1.05, T=7.49, W=2.59, H=0.35, sz=15, anchor="m")
set_text(sl, 41, L=4.11, T=6.77, W=1.84, H=0.40, sz=19, anchor="m")
set_text(sl, 43, L=6.56, T=6.56, W=2.65, H=0.84, sz=19, anchor="t", spc_after=6)
set_text(sl, 45, L=10.00, T=6.56, W=2.60, H=0.84, sz=19, anchor="t", spc_after=6)
set_text(sl, 47, L=13.65, T=6.77, W=2.02, H=0.40, sz=19, anchor="m")
set_text(sl, 42, L=5.97, T=6.605, W=0.57, H=0.73, sz=22, anchor="m")
set_text(sl, 44, L=9.35, T=6.605, W=0.65, H=0.73, sz=22, anchor="m")
set_text(sl, 46, L=12.80, T=6.605, W=0.68, H=0.73, sz=22, anchor="m")
set_text(sl, 48, L=4.11, T=7.53, W=11.50, H=0.38, sz=17, anchor="m")
set_text(sl, 50, L=1.07, T=8.30, W=14.26, H=0.42, sz=20, anchor="m")

# ============ SLIDE 5 ============
sl = S[4]
set_text(sl, 2, L=0.83, T=0.57, W=14.95, H=0.67, sz=30, anchor="m")
rule_h(sl, 3, 1.0)
set_text(sl, 4, L=0.83, T=1.73, W=14.95, H=0.52, sz=21, anchor="m")
for c in (5, 11, 17):
    style_card(sl, c)
set_text(sl, 7, L=1.03, T=3.15, W=0.57, H=0.55, sz=24, anchor="m")
set_text(sl, 13, L=1.03, T=5.20, W=0.57, H=0.55, sz=24, anchor="m")
set_text(sl, 19, L=1.03, T=7.25, W=0.57, H=0.55, sz=24, anchor="m")
set_text(sl, 8, L=2.07, T=2.68, W=9.67, H=0.54, sz=23, anchor="m")
set_text(sl, 14, L=2.07, T=4.73, W=9.67, H=0.54, sz=23, anchor="m")
set_text(sl, 20, L=2.07, T=6.78, W=9.67, H=0.54, sz=23, anchor="m")
set_text(sl, 9, L=2.07, T=3.35, W=9.67, H=0.79, sz=19, anchor="t", spc_after=6)
set_text(sl, 15, L=2.07, T=5.41, W=9.67, H=0.79, sz=19, anchor="t", spc_after=6)
set_text(sl, 21, L=2.07, T=7.46, W=9.67, H=0.79, sz=19, anchor="t", spc_after=6)
# right stat stacks, vertically centered in their boxes
set_text(sl, 23, L=12.23, T=2.82, W=3.40, H=0.54, sz=27, anchor="m")
set_text(sl, 24, L=12.23, T=3.56, W=3.40, H=0.40, sz=18, anchor="m")
set_text(sl, 25, L=12.23, T=4.74, W=3.40, H=0.42, sz=19, anchor="m")
set_text(sl, 26, L=12.23, T=5.16, W=3.40, H=0.51, sz=24, anchor="m")
set_text(sl, 27, L=12.23, T=5.71, W=3.40, H=0.42, sz=20, anchor="m")
set_text(sl, 28, L=12.23, T=7.22, W=3.40, H=0.53, sz=23, anchor="m")

# ============ SLIDE 6 ============
sl = S[5]
set_text(sl, 2, L=0.83, T=0.57, W=14.95, H=0.67, sz=30, anchor="m")
rule_h(sl, 3, 1.0)
set_text(sl, 4, L=0.83, T=1.73, W=14.95, H=0.52, sz=21, anchor="m")
for c in (5, 15, 19, 24):
    style_card(sl, c)
for b in (6, 16, 20, 25):
    style_card(sl, b)
set_text(sl, 7, L=1.07, T=2.51, W=4.35, H=0.44, sz=20, anchor="m")
# chain nodes become boxed (fill + hairline) like the card grammar
chain = {8: 3.47, 10: 4.79, 12: 6.11, 14: 7.43}
for sid, t in chain.items():
    s = shp(sl, sid)
    s.fill.solid(); s.fill.fore_color.rgb = RGBColor(0xFC, 0xFB, 0xF7)
    s.line.color.rgb = BORDER; s.line.width = Pt(1)
    set_text(sl, sid, L=1.09, T=t, W=4.25, H=0.62, sz=23, anchor="m", align="c")
set_text(sl, 9, L=2.91, T=4.155, W=0.62, H=0.57, sz=28, anchor="m")
set_text(sl, 11, L=2.91, T=5.475, W=0.62, H=0.57, sz=28, anchor="m")
set_text(sl, 13, L=2.91, T=6.795, W=0.62, H=0.57, sz=28, anchor="m")
set_text(sl, 17, L=6.27, T=2.51, W=9.35, H=0.44, sz=20, anchor="m")
set_text(sl, 18, L=6.27, T=3.24, W=9.31, H=0.72, sz=23, anchor="m")
set_text(sl, 21, L=6.27, T=4.47, W=9.35, H=0.44, sz=20, anchor="m")
set_text(sl, 22, L=6.27, T=5.17, W=9.31, H=0.54, sz=23, anchor="m")
set_text(sl, 23, L=6.27, T=5.80, W=9.31, H=0.45, sz=18, anchor="m")
set_text(sl, 26, L=6.27, T=6.66, W=9.35, H=0.44, sz=20, anchor="m")
set_text(sl, 27, L=6.27, T=7.40, W=9.31, H=0.98, sz=23, anchor="t", spc_after=6)

# ============ SLIDE 7 ============
sl = S[6]
set_text(sl, 2, L=0.83, T=0.57, W=14.95, H=0.67, sz=30, anchor="m")
rule_h(sl, 3, 1.0)
set_text(sl, 4, L=0.83, T=1.73, W=14.95, H=0.52, sz=21, anchor="m")
for c in (5, 12, 19, 25):
    style_card(sl, c)
for b in (6, 13, 20):
    style_card(sl, b)
set_text(sl, 7, L=1.07, T=2.83, W=4.14, H=0.57, sz=20, anchor="m")
set_text(sl, 14, L=6.28, T=2.83, W=4.14, H=0.57, sz=20, anchor="m")
set_text(sl, 21, L=11.49, T=2.83, W=4.14, H=0.57, sz=20, anchor="m")
set_text(sl, 8, L=1.07, T=3.70, W=4.08, H=0.47, sz=17, anchor="m")
set_text(sl, 15, L=6.28, T=3.70, W=4.08, H=0.47, sz=17, anchor="m")
set_text(sl, 22, L=11.49, T=3.70, W=4.08, H=0.47, sz=17, anchor="m")
for d in (9, 16, 23):
    s = shp(sl, d)
    s.fill.solid(); s.fill.fore_color.rgb = BORDER
    geom(sl, d, H=0.014)
set_text(sl, 10, L=1.07, T=4.76, W=4.08, H=1.81, sz=19, anchor="t", spc_after=8)
set_text(sl, 17, L=6.28, T=4.76, W=4.08, H=1.81, sz=19, anchor="t", spc_after=8)
set_text(sl, 24, L=11.49, T=4.76, W=4.08, H=1.81, sz=19, anchor="t", spc_after=8)
set_text(sl, 11, L=5.42, T=4.60, W=0.60, H=0.73, sz=24, anchor="m")
set_text(sl, 18, L=10.64, T=4.60, W=0.60, H=0.73, sz=24, anchor="m")
set_text(sl, 26, L=1.07, T=7.91, W=14.30, H=0.51, sz=23, anchor="m")

prs.save(DST)
print("saved", DST)

# ================= VERIFICATION =================
def all_texts(path):
    p = Presentation(path)
    out = []
    for i, sl_ in enumerate(p.slides):
        for s in sl_.shapes:
            if getattr(s, "has_text_frame", False) and s.has_text_frame:
                for para in s.text_frame.paragraphs:
                    out.append((i + 1, s.shape_id, para.text))
    return out

orig, new = all_texts(SRC), all_texts(DST)
assert len(orig) == len(new), f"para count changed {len(orig)} -> {len(new)}"
diff = [(a, b) for a, b in zip(orig, new) if a != b]
print("TEXT-IDENTITY:", "PASS (all runs byte-identical)" if not diff else f"FAIL {diff[:5]}")

# bounds check
p2 = Presentation(DST)
W, H = p2.slide_width, p2.slide_height
bad = []
for i, sl_ in enumerate(p2.slides):
    for s in sl_.shapes:
        if s.left is None:
            continue
        if s.left < -10000 or s.top < -10000 or s.left + s.width > W + 10000 or s.top + s.height > H + 10000:
            bad.append((i + 1, s.shape_id))
print("BOUNDS:", "PASS" if not bad else f"FAIL {bad}")
