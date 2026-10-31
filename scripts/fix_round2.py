"""Round-2 fixes from visual QA:
1. slide 3: bottom bracket ticks must hit the two LLM nodes (Manager 03, Compliance 04)
2. all banner slides: delete the old full-width thin rules now colliding with banner corners
"""
from pptx import Presentation
from pptx.util import Emu

PATH = r"C:\Users\44977\Desktop\CUAD_MAS_Stage1_Validator_Audit_20261010_updated.pptx"
prs = Presentation(PATH)

# 1-based slide -> shape ids of stale rules (verified by scan)
STALE = {
    5: [9], 6: [38], 7: [9], 8: [40], 9: [20], 10: [14], 11: [31],
    12: [17], 13: [29], 14: [11], 15: [12], 16: [22], 17: [23], 18: [21],
}
for sidx, ids in STALE.items():
    slide = prs.slides[sidx - 1]
    for sh in list(slide.shapes):
        if sh.shape_id in ids:
            sh._element.getparent().remove(sh._element)

# slide 3 ticks: currently at XS[2]=4.738 and XS[3]=6.792 center-x of connectors;
# connectors are the two upward orange ones between rule (6.06) and cards (5.94)
s3 = prs.slides[2]
CW = 1.82
XS = [0.62 + i * (CW + 0.234) for i in range(6)]
want = [XS[3] + CW / 2, XS[4] + CW / 2]  # 7.692, 9.746
ticks = [
    sh for sh in s3.shapes
    if sh.shape_type == 9  # connector... (LINE cxnSp reports as LINE? check both)
    or getattr(sh, "element", None) is not None and sh._element.tag.endswith("}cxnSp")
]
# safer: match by geometry among no-text shapes near the bracket
moved = 0
for sh in s3.shapes:
    try:
        l, t = Emu(sh.left).inches, Emu(sh.top).inches
        w, h = Emu(sh.width).inches, Emu(sh.height).inches
    except Exception:
        continue
    has_txt = sh.has_text_frame and sh.text_frame.text.strip()
    if not has_txt and abs(t - 5.94) < 0.03 and h < 0.2 and w < 0.05 and l < 9.0:
        target = want[moved] if moved < len(want) else None
        if target:
            sh.left = Emu(int(target * 914400))
            moved += 1
print("ticks moved:", moved)
prs.save(PATH)
