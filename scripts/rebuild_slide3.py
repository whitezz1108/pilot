"""Rebuild slide 3 (6-node pipeline) in the reference-image structure.

Deletes the old flowchart region and redraws: numbered badge + lane-colored title,
hairline separator, bold lead line, dot bullets, tinted A/V sub-card (01),
three decision pills (05), lane-colored triangle arrows, top annotation rule
with ticks, bottom bracket + takeaway banner.
"""
import copy

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Inches, Pt

PATH = r"C:\Users\44977\Desktop\CUAD_MAS_Stage1_Validator_Audit_20261010_updated.pptx"

BLUE, ORANGE, GREEN, PURPLE = "3E5F8A", "B07A3F", "4F7D5B", "7B5E8C"
NAVY, MUTED, BORDER, SEP = "262B33", "8A929E", "E2DFD6", "DDD9CE"
RED, R_TINT, G_TINT, P_TINT, B_TINT, O_TINT = "A85751", "F8ECEB", "ECF3EE", "F4EFF8", "ECF1F7", "F8F0E5"
FACE = "Microsoft YaHei"

LANES = [BLUE, ORANGE, BLUE, BLUE, PURPLE, PURPLE]
NUMS = ["00", "01", "02", "03", "04", "05"]
TITLES = ["原文 / 样本", "实验分配", "Analyst", "Manager", "Compliance", "Decision"]
LEADS = ["40 份合同", "E0：5 Claims", "加载冻结 Memo", "独立 LLM", "独立 LLM", "非第三个 LLM"]
BULLETS = [
    ["32 个阳性", "COC 16 / TFC 16", "8 个 Sentinel", "两目标均 absent"],
    ["E1：删至 4 条", "Sentinel 仅 E0"],
    ["离线构造", "不调用 LLM", "不作合同判断", "只传选定 Memo"],
    ["接收 Memo", "判定两类条款", "可独立查原文", "结构化 Handoff"],
    ["仅接收 Handoff", "可独立查原文", "最终审查与核验", "输出最终决定"],
    [],
]
PILLS = [
    ("ESCALATE", "任一 present", RED, R_TINT),
    ("ACCEPT", "两项 absent", GREEN, G_TINT),
    ("REVIEW", "无 present 且 unknown", PURPLE, P_TINT),
]
SUB_ROWS = [("A0V0", "无原文"), ("A1V0", "可选核验"), ("A1V1", "强制核验")]

X0, CW, GAP, CY, CH = 0.62, 1.82, 0.234, 2.58, 3.32
XS = [X0 + i * (CW + GAP) for i in range(6)]


def ea_font(run):
    rPr = run._r.get_or_add_rPr()
    e = rPr.find(qn("a:ea"))
    if e is None:
        e = rPr.makeelement(qn("a:ea"), {})
        latin = rPr.find(qn("a:latin"))
        (latin.addnext if latin is not None else rPr.append)(e)
    e.set("typeface", FACE)


def style_run(r, size, color, bold=False, italic=False):
    r.font.name = FACE
    r.font.size = Pt(size)
    r.font.bold = bold
    r.font.italic = italic
    r.font.color.rgb = RGBColor.from_string(color)
    ea_font(r)


def add_tb(slide, x, y, w, h, wrap=True):
    tb = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = tb.text_frame
    tf.word_wrap = wrap
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    return tb, tf


def add_rect(slide, x, y, w, h, fill, prst=MSO_SHAPE.RECTANGLE, adj=None, line=None, line_w=None):
    sp = slide.shapes.add_shape(prst, Inches(x), Inches(y), Inches(w), Inches(h))
    sp.fill.solid()
    sp.fill.fore_color.rgb = RGBColor.from_string(fill)
    if line:
        sp.line.color.rgb = RGBColor.from_string(line)
        sp.line.width = Pt(line_w or 0.75)
    else:
        sp.line.fill.background()
    sp.shadow.inherit = False
    if adj is not None and prst == MSO_SHAPE.ROUNDED_RECTANGLE:
        try:
            sp.adjustments[0] = adj / min(w, h)
        except Exception:
            pass
    return sp


def add_conn(slide, x1, y1, x2, y2, color, w_pt, head=("triangle", "med", "med")):
    c = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(x1), Inches(y1), Inches(x2), Inches(y2))
    c.line.color.rgb = RGBColor.from_string(color)
    c.line.width = Pt(w_pt)
    ln = c.line._get_or_add_ln()
    tail = ln.makeelement(qn("a:tailEnd"), {"type": head[0], "w": head[1], "len": head[2]})
    ln.append(tail)
    c.shadow.inherit = False
    return c


def set_bullet(p, color, size, spc_after=10, hang=0.12):
    from pptx.oxml import parse_xml
    from pptx.oxml.ns import nsdecls
    marL = int(hang * 914400)
    pPr = parse_xml(
        f'<a:pPr {nsdecls("a")} marL="{marL}" indent="-{marL}">'
        f'<a:lnSpc><a:spcPct val="112000"/></a:lnSpc>'
        f'<a:spcAft><a:spcPts val="{spc_after * 100}"/></a:spcAft>'
        f'<a:buClr><a:srgbClr val="{color}"/></a:buClr>'
        f'<a:buSzPct val="80000"/><a:buFont typeface="Arial"/><a:buChar char="•"/></a:pPr>'
    )
    old = p._p.find(qn("a:pPr"))
    if old is not None:
        p._p.remove(old)
    p._p.insert(0, pPr)


def para_sp(p, after=4, ln=112):
    from pptx.oxml import parse_xml
    from pptx.oxml.ns import nsdecls
    pPr = parse_xml(
        f'<a:pPr {nsdecls("a")}><a:lnSpc><a:spcPct val="{ln * 1000}"/></a:lnSpc>'
        f'<a:spcAft><a:spcPts val="{after * 100}"/></a:spcAft></a:pPr>'
    )
    old = p._p.find(qn("a:pPr"))
    if old is not None:
        p._p.remove(old)
    p._p.insert(0, pPr)


def main():
    prs = Presentation(PATH)
    slide = prs.slides[2]
    kill = {8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 20, 22, 24, 26, 28,
            19, 21, 23, 25, 27, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40,
            41, 42, 43, 44}
    for s in list(slide.shapes):
        if s.shape_id in kill:
            s._element.getparent().remove(s._element)

    # ---- top annotation ----
    _, tf = add_tb(slide, 0.62, 2.02, 9.0, 0.26)
    r = tf.paragraphs[0].add_run()
    r.text = "仅 A1 可回溯当前合同；Manager / Compliance 各自访问原文"
    style_run(r, 9.5, BLUE)
    add_rect(slide, 0.62, 2.38, 12.09, 0.014, BLUE)
    add_conn(slide, XS[3] + CW / 2, 2.38, XS[3] + CW / 2, 2.56, BLUE, 1.0, ("triangle", "sm", "sm"))
    add_conn(slide, XS[4] + CW / 2, 2.38, XS[4] + CW / 2, 2.56, PURPLE, 1.0, ("triangle", "sm", "sm"))

    # ---- cards ----
    for i in range(6):
        x = XS[i]
        lane = LANES[i]
        add_rect(slide, x, CY, CW, CH, "FFFFFF", MSO_SHAPE.ROUNDED_RECTANGLE, adj=0.06, line=BORDER, line_w=0.75)
        bar = slide.shapes.add_shape(MSO_SHAPE.ROUND_2_SAME_RECTANGLE, Inches(x), Inches(CY), Inches(CW), Inches(0.055))
        bar.adjustments[0] = 0.5
        bar.adjustments[1] = 0.0
        bar.fill.solid()
        bar.fill.fore_color.rgb = RGBColor.from_string(lane)
        bar.line.fill.background()
        bar.shadow.inherit = False
        # badge + title
        badge = add_rect(slide, x + 0.13, CY + 0.15, 0.27, 0.27, lane, MSO_SHAPE.ROUNDED_RECTANGLE, adj=0.055)
        btf = badge.text_frame
        btf.margin_left = btf.margin_right = btf.margin_top = btf.margin_bottom = 0
        btf.vertical_anchor = MSO_ANCHOR.MIDDLE
        br = btf.paragraphs[0].add_run()
        br.text = NUMS[i]
        style_run(br, 9.5, "FFFFFF", bold=True)
        btf.paragraphs[0].alignment = PP_ALIGN.CENTER
        _, ttf = add_tb(slide, x + 0.48, CY + 0.13, CW - 0.60, 0.30)
        ttf.vertical_anchor = MSO_ANCHOR.MIDDLE
        tr = ttf.paragraphs[0].add_run()
        tr.text = TITLES[i]
        style_run(tr, 11, lane, bold=True)
        # separator
        add_rect(slide, x + 0.13, CY + 0.56, CW - 0.26, 0.012, BORDER)
        # lead
        _, ltf = add_tb(slide, x + 0.13, CY + 0.70, CW - 0.26, 0.24)
        lr = ltf.paragraphs[0].add_run()
        lr.text = LEADS[i]
        style_run(lr, 10.5, NAVY, bold=True)
        # bullets
        if BULLETS[i]:
            _, btf2 = add_tb(slide, x + 0.13, CY + 1.02, CW - 0.24, CH - 1.15)
            for j, item in enumerate(BULLETS[i]):
                p = btf2.paragraphs[0] if j == 0 else btf2.add_paragraph()
                rr = p.add_run()
                rr.text = item
                style_run(rr, 10, NAVY)
                set_bullet(p, lane, 10)

    # card 01: tinted A/V sub-card
    x1 = XS[1]
    add_rect(slide, x1 + 0.13, CY + 1.86, CW - 0.26, 1.32, O_TINT, MSO_SHAPE.ROUNDED_RECTANGLE, adj=0.06)
    _, stf = add_tb(slide, x1 + 0.24, CY + 1.95, CW - 0.48, 0.22)
    sr = stf.paragraphs[0].add_run()
    sr.text = "A / V 治理条件"
    style_run(sr, 8.5, ORANGE, bold=True)
    for k, (code, desc) in enumerate(SUB_ROWS):
        _, rtf = add_tb(slide, x1 + 0.24, CY + 2.22 + k * 0.30, CW - 0.44, 0.26)
        p = rtf.paragraphs[0]
        r1 = p.add_run()
        r1.text = code + "  "
        style_run(r1, 9, NAVY, bold=True)
        r2 = p.add_run()
        r2.text = desc
        style_run(r2, 9, "545D6B")

    # card 05: three decision pills
    x5 = XS[5]
    for k, (name, sub, col, tint) in enumerate(PILLS):
        py = CY + 1.08 + k * 0.70
        add_rect(slide, x5 + 0.13, py, CW - 0.26, 0.58, tint, MSO_SHAPE.ROUNDED_RECTANGLE, adj=0.08)
        _, ptf = add_tb(slide, x5 + 0.24, py + 0.07, CW - 0.46, 0.46)
        p1 = ptf.paragraphs[0]
        r1 = p1.add_run()
        r1.text = name
        style_run(r1, 9.5, col, bold=True)
        p2 = ptf.add_paragraph()
        r2 = p2.add_run()
        r2.text = sub
        style_run(r2, 8.5, "545D6B")

    # ---- arrows between cards (colored by target lane) ----
    for i in range(5):
        ay = CY + CH / 2
        ax = XS[i] + CW + 0.035
        add_conn(slide, ax, ay, ax + GAP - 0.07, ay, LANES[i + 1], 1.75)

    # ---- bottom bracket + banner ----
    add_rect(slide, XS[1] + CW / 2, 6.06, XS[4] + CW / 2 - (XS[1] + CW / 2), 0.014, ORANGE)
    add_conn(slide, XS[2] + CW / 2, 6.06, XS[2] + CW / 2, 5.94, ORANGE, 1.0, ("triangle", "sm", "sm"))
    add_conn(slide, XS[3] + CW / 2, 6.06, XS[3] + CW / 2, 5.94, ORANGE, 1.0, ("triangle", "sm", "sm"))
    bn = add_rect(slide, 0.62, 6.30, 12.09, 0.42, B_TINT, MSO_SHAPE.ROUNDED_RECTANGLE, adj=0.08)
    btf3 = bn.text_frame
    btf3.margin_left, btf3.margin_right = Inches(0.15), Inches(0.1)
    btf3.margin_top = btf3.margin_bottom = 0
    btf3.vertical_anchor = MSO_ANCHOR.MIDDLE
    br2 = btf3.paragraphs[0].add_run()
    br2.text = "A / V 同时控制两个 LLM 节点；Compliance 不接收原始 Analyst Memo"
    style_run(br2, 10.5, "2F496B", bold=True)

    prs.save(PATH)
    print("slide 3 rebuilt; shapes now:", len(prs.slides[2].shapes))


if __name__ == "__main__":
    main()
