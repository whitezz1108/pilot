"""Fix slide 3 defects from visual QA: stack node titles (number line + navy
name line, uniform 17.25pt) so nothing wraps mid-word; widen the bottom amber
rule so its right end meets the tick at card 04's center."""
import copy

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.util import Pt

PATH = r"C:\Users\44977\Desktop\CUAD_MAS_Stage1_Validator_Audit_20261010_updated.pptx"
NAVY = "16233B"

TITLES = {
    19: ("00", "原文 / 样本", "4267D5"),
    21: ("01", "实验分配", "C38935"),
    23: ("02", "Analyst", "4267D5"),
    25: ("03", "Manager", "4267D5"),
    27: ("04", "Compliance", "8364B5"),
    29: ("05", "Decision", "8364B5"),
}

prs = Presentation(PATH)
s = prs.slides[2]

for sid, (num, name, color) in TITLES.items():
    shp = next(x for x in s.shapes if x.shape_id == sid)
    tf = shp.text_frame
    p1 = tf.paragraphs[0]
    # keep only the first run (number), drop the rest
    for r in p1.runs[1:]:
        r._r.getparent().remove(r._r)
    # drop any extra paragraphs beyond the first
    for p in tf.paragraphs[1:]:
        p._p.getparent().remove(p._p)
    run1 = p1.runs[0]
    run1.text = num
    run1.font.size = Pt(17.25)
    run1.font.color.rgb = RGBColor.from_string(color)
    # second paragraph: name, same formatting but navy
    p2el = copy.deepcopy(p1._p)
    p1._p.addnext(p2el)
    from pptx.text.text import _Paragraph

    p2 = _Paragraph(p2el, tf)
    for r in p2.runs[1:]:
        r._r.getparent().remove(r._r)
    run2 = p2.runs[0]
    run2.text = name
    run2.font.size = Pt(17.25)
    run2.font.color.rgb = RGBColor.from_string(NAVY)

# widen bottom amber rule (shape 41) so its right end reaches the tick at x=9.74"
rule = next(x for x in s.shapes if x.shape_id == 41)
rule.width = __import__("pptx.util", fromlist=["Emu"]).Emu(int(9.74 * 914400) - rule.left)

prs.save(PATH)
print("fixed")
for sid in TITLES:
    shp = next(x for x in prs.slides[2].shapes if x.shape_id == sid)
    print(sid, [(p.runs[0].text, p.runs[0].font.size.pt) for p in shp.text_frame.paragraphs if p.runs])
rule = next(x for x in prs.slides[2].shapes if x.shape_id == 41)
print("rule right edge:", (rule.left + rule.width) / 914400)
