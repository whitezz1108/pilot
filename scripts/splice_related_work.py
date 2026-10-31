"""Rebuild v2's related-work pages 19-20 in the academic style system and splice
them into the delivered deck, replacing its page 19 (deck grows 20 -> 21 pages).
Content is copied verbatim from the v2 deck (text + figures)."""
import re
import shutil

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Inches, Pt

DECK = r"C:\Users\44977\Desktop\CUAD_MAS_Stage1_Validator_Audit_20261010_updated.pptx"
BACKUP = r"C:\Users\44977\Desktop\CUAD_MAS_Stage1_Validator_Audit_20261010_backup.pptx"
V2 = r"C:\Users\44977\Desktop\pilot01\outputs\research_update_20261010\deliverables\CUAD_MAS_Stage1_Validator_Audit_20261010_v2.pptx"

BLUE, ORANGE, PURPLE = "3E5F8A", "B07A3F", "7B5E8C"
NAVY, GRAY, MUTED, BORDER, SEP = "262B33", "545D6B", "8A929E", "E2DFD6", "DDD9CE"
CHIP_BG = "ECF1F7"
FACE = "Microsoft YaHei"

PAGES = [
    {
        "lane": BLUE, "chip_tint": CHIP_BG,
        "chip": "相关研究 · 2026 · 法律仿真",
        "title": "AgentMediation：用可控仿真研究法律调解",
        "meta": "Junjie Chen, Haitao Li, Minghao Qin et al. · 清华大学等 · AAAI 2026",
        "en": "Simulating Dispute Mediation with LLM-Based Agents for Legal Research",
        "src": "Source: Chen et al., Fig. 1, PDF p. 2 · 论文原图",
        "c1_title": "研究设计（pp. 1–2）",
        "c1_lead": "330 起真实民事纠纷，14,000+ 次模拟",
        "c1_body": [
            "5 阶段调解：初步沟通、陈述、方案生成、讨价还价与结束",
            "操纵变量：当事人冲突模式、纠纷原因、调解员是否拥有外部知识",
            "评价：调解结果与方案质量",
        ],
        "c2_title": "验证依据与本项目启发",
        "c2_body": "专家回应及专家重建过程用于对照（pp. 5、7），原始调解对话未公开。",
        "c2_pill_lead": "可借鉴：",
        "c2_pill": "将真实任务材料、可控过程变量和评价规则分别定义，再检验仿真效度。",
        "pic_idx": 18,
    },
    {
        "lane": ORANGE, "chip_tint": "F8F0E5",
        "chip": "相关研究 · 2026 · 故障评测",
        "title": "Agent Error Simulator：受控故障注入与恢复评估",
        "meta": "Keith Bateman, Jaime Cernuda Garcia, Luke Logan et al. · Illinois Tech / Argonne · IEEE eScience 2026",
        "en": "Agent Error Simulator: Fault Injection for Testing Recovery in Agentic Workflows",
        "src": "Source: Bateman et al., Fig. 1, PDF p. 4 · 论文原图",
        "c1_title": "故障注入设计（pp. 4–5）",
        "c1_lead": "透明 HTTP 代理拦截模型响应",
        "c1_body": [
            "注入位置：Planner / Worker / Aggregator",
            "故障类型：格式、逻辑、工具调用",
        ],
        "c1_note": ["图中 × 表示故障，○ 表示下一轮修正", "代理机制见正文 §III-D（p. 4）"],
        "c2_title": "评价依据与本项目定位",
        "c2_body": "数值任务具有已知真值，协调器独立重算最终误差，避免依赖模型自报结果。",
        "c2_pill_lead": "对我们：",
        "c2_pill": "故障注入已有先例，候选贡献需进一步落实到法律证据恢复的独立评价。",
        "pic_idx": 19,
    },
]


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


def para_sp(p, after=5, ln=112):
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


def add_tb(slide, x, y, w, h):
    tb = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = tb.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    return tf


def add_rect(slide, x, y, w, h, fill, prst=MSO_SHAPE.RECTANGLE, adj=None, line=None, line_w=None):
    sp = slide.shapes.add_shape(prst, Inches(x), Inches(y), Inches(w), Inches(h))
    if fill is None:
        sp.fill.background()
    else:
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


def top_bar(slide, x, y, w, color):
    bar = slide.shapes.add_shape(MSO_SHAPE.ROUND_2_SAME_RECTANGLE, Inches(x), Inches(y), Inches(w), Inches(0.055))
    bar.adjustments[0] = 0.5
    bar.adjustments[1] = 0.0
    bar.fill.solid()
    bar.fill.fore_color.rgb = RGBColor.from_string(color)
    bar.line.fill.background()
    bar.shadow.inherit = False


def delete_slide(prs, idx):
    sld = list(prs.slides._sldIdLst)[idx]
    prs.part.drop_rel(sld.get(qn("r:id")))
    prs.slides._sldIdLst.remove(sld)


def build_page(slide, spec, v2, pic_w_in, pic_h_in):
    lane, tint = spec["lane"], spec["chip_tint"]
    slide.background.fill.solid()
    slide.background.fill.fore_color.rgb = RGBColor.from_string("FAF9F5")

    # chip
    w_chip = sum(0.135 if ord(c) > 0x2E7F else 0.072 for c in spec["chip"]) + 0.34
    chip = add_rect(slide, 0.62, 0.22, w_chip, 0.30, tint, MSO_SHAPE.ROUNDED_RECTANGLE, adj=0.15, line=lane, line_w=0.75)
    ctf = chip.text_frame
    ctf.margin_left = ctf.margin_right = Inches(0.05)
    ctf.margin_top = ctf.margin_bottom = 0
    ctf.vertical_anchor = MSO_ANCHOR.MIDDLE
    cr = ctf.paragraphs[0].add_run()
    cr.text = spec["chip"]
    style_run(cr, 9.5, lane, bold=True)
    ctf.paragraphs[0].alignment = PP_ALIGN.CENTER

    # title / separator / meta / english title
    tf = add_tb(slide, 0.62, 0.62, 12.09, 0.44)
    r = tf.paragraphs[0].add_run()
    r.text = spec["title"]
    style_run(r, 22, NAVY, bold=True)
    add_rect(slide, 0.62, 1.18, 12.09, 0.012, SEP)
    add_rect(slide, 0.62, 1.18, 0.67, 0.03, lane)
    tf = add_tb(slide, 0.62, 1.28, 12.09, 0.24)
    r = tf.paragraphs[0].add_run()
    r.text = spec["meta"]
    style_run(r, 9.5, GRAY)
    tf = add_tb(slide, 0.62, 1.56, 12.09, 0.22)
    r = tf.paragraphs[0].add_run()
    r.text = spec["en"]
    style_run(r, 9.5, MUTED, italic=True)

    # left figure card + picture + source caption
    CX, CY, CW, CH = 0.62, 1.92, 7.15, 4.5
    add_rect(slide, CX, CY, CW, CH, "FFFFFF", MSO_SHAPE.ROUNDED_RECTANGLE, adj=0.06, line=BORDER, line_w=0.75)
    top_bar(slide, CX, CY, CW, lane)
    pic = v2.slides[spec["pic_idx"]].shapes[8].image  # PICTURE shape id 9
    tmp = f"C:\\Users\\44977\\Desktop\\pilot01\\tmp_pic_{spec['pic_idx']}.png"
    with open(tmp, "wb") as f:
        f.write(pic.blob)
    iw, ih = pic.size
    max_w, max_h = CW - 0.34, CH - 0.35
    scale = min(max_w / iw, max_h / ih)
    pw, ph = iw * scale, ih * scale
    slide.shapes.add_picture(tmp, Inches(CX + (CW - pw) / 2), Inches(CY + 0.055 + (CH - 0.055 - ph) / 2 + 0.06),
                             Inches(pw), Inches(ph))
    tf = add_tb(slide, CX, 6.52, CW, 0.24)
    r = tf.paragraphs[0].add_run()
    r.text = spec["src"]
    style_run(r, 8.5, MUTED, italic=True)
    tf.paragraphs[0].alignment = PP_ALIGN.RIGHT

    # right card 1
    R1X, R1Y, R1W, R1H = 7.98, 1.92, 4.92, 2.52
    add_rect(slide, R1X, R1Y, R1W, R1H, "FFFFFF", MSO_SHAPE.ROUNDED_RECTANGLE, adj=0.06, line=BORDER, line_w=0.75)
    top_bar(slide, R1X, R1Y, R1W, lane)
    tf = add_tb(slide, R1X + 0.16, R1Y + 0.14, R1W - 0.32, 0.26)
    r = tf.paragraphs[0].add_run()
    r.text = spec["c1_title"]
    style_run(r, 11, lane, bold=True)
    add_rect(slide, R1X + 0.16, R1Y + 0.48, R1W - 0.32, 0.012, BORDER)
    tf = add_tb(slide, R1X + 0.16, R1Y + 0.62, R1W - 0.32, R1H - 0.75)
    p = tf.paragraphs[0]
    r = p.add_run()
    r.text = spec["c1_lead"]
    style_run(r, 10, NAVY, bold=True)
    para_sp(p)
    for item in spec["c1_body"]:
        p = tf.add_paragraph()
        r = p.add_run()
        r.text = item
        style_run(r, 10, NAVY)
        para_sp(p)
    for item in spec.get("c1_note", []):
        p = tf.add_paragraph()
        r = p.add_run()
        r.text = item
        style_run(r, 9, GRAY)
        para_sp(p, after=2)

    # right card 2
    R2X, R2Y, R2W, R2H = 7.98, 4.60, 4.92, 1.95
    add_rect(slide, R2X, R2Y, R2W, R2H, "FFFFFF", MSO_SHAPE.ROUNDED_RECTANGLE, adj=0.06, line=BORDER, line_w=0.75)
    top_bar(slide, R2X, R2Y, R2W, PURPLE)
    tf = add_tb(slide, R2X + 0.16, R2Y + 0.14, R2W - 0.32, 0.26)
    r = tf.paragraphs[0].add_run()
    r.text = spec["c2_title"]
    style_run(r, 11, PURPLE, bold=True)
    add_rect(slide, R2X + 0.16, R2Y + 0.48, R2W - 0.32, 0.012, BORDER)
    tf = add_tb(slide, R2X + 0.16, R2Y + 0.60, R2W - 0.32, 0.5)
    r = tf.paragraphs[0].add_run()
    r.text = spec["c2_body"]
    style_run(r, 10, NAVY)
    para_sp(p := tf.paragraphs[0], after=4)
    pill = add_rect(slide, R2X + 0.16, R2Y + 1.06, R2W - 0.32, 0.72, "F4EFF8", MSO_SHAPE.ROUNDED_RECTANGLE, adj=0.08)
    ptf = pill.text_frame
    ptf.word_wrap = True
    ptf.margin_left = ptf.margin_right = Inches(0.12)
    ptf.margin_top = ptf.margin_bottom = Inches(0.05)
    ptf.vertical_anchor = MSO_ANCHOR.MIDDLE
    p1 = ptf.paragraphs[0]
    r = p1.add_run()
    r.text = spec["c2_pill_lead"]
    style_run(r, 9.5, PURPLE, bold=True)
    r = p1.add_run()
    r.text = spec["c2_pill"]
    style_run(r, 9.5, NAVY)
    para_sp(p1, after=0)

    # page number
    tf = add_tb(slide, 11.4, 7.04, 1.31, 0.24)
    r = tf.paragraphs[0].add_run()
    r.text = "00 / 21"
    style_run(r, 8.5, MUTED)
    tf.paragraphs[0].alignment = PP_ALIGN.RIGHT


def renumber(slide, label):
    for sh in slide.shapes:
        if sh.has_text_frame and re.fullmatch(r"\d{2} / \d{2}", sh.text_frame.text.strip()):
            p = sh.text_frame.paragraphs[0]
            runs = p.runs
            runs[0].text = label
            for r in runs[1:]:
                r._r.getparent().remove(r._r)
            return


def main():
    shutil.copyfile(DECK, BACKUP)
    prs = Presentation(DECK)
    v2 = Presentation(V2)

    for spec in PAGES:
        slide = prs.slides.add_slide(prs.slides[1].slide_layout)
        build_page(slide, spec, v2, 0, 0)

    delete_slide(prs, 18)  # old related-work page
    # order now: 1..18, closing, newA, newB -> move newA,newB before closing
    lst = prs.slides._sldIdLst
    ids = list(lst)
    a, b = ids[-2], ids[-1]
    lst.remove(a)
    lst.remove(b)
    lst.insert(18, a)
    lst.insert(19, b)

    for i, slide in enumerate(prs.slides):
        renumber(slide, f"{i + 1:02d} / 21")
    prs.save(DECK)
    print("saved; slides:", len(prs.slides))
    for i, slide in enumerate(prs.slides):
        t = ""
        for sh in slide.shapes:
            if sh.has_text_frame:
                tt = sh.text_frame.text.strip()
                if re.fullmatch(r"\d{2} / \d{2}", tt):
                    t = tt
        print(f"  {i+1:02d} -> {t}")


if __name__ == "__main__":
    main()
