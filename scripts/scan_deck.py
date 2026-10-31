"""Compact full-deck scan: font sizes in use, bottom-region shapes, kickers, charts, backgrounds."""
from pptx import Presentation
from pptx.util import Emu

PATH = r"C:\Users\44977\Desktop\CUAD_MAS_Stage1_Validator_Audit_20261010.pptx"
prs = Presentation(PATH)

def inch(v):
    return round(Emu(v).inches, 2)

for i, slide in enumerate(prs.slides):
    sizes = {}
    bottoms = []
    kicker = None
    charts = 0
    tables = 0
    pics = 0
    n_sp = 0

    def walk(shapes):
        global charts, tables, pics, n_sp, kicker
        for s in shapes:
            n_sp += 1
            if s.shape_type == 6:
                walk(s.shapes)
                continue
            if s.has_chart if hasattr(s, "has_chart") else False:
                charts += 1
            if s.shape_type == 19:
                tables += 1
            if s.shape_type == 13:
                pics += 1
            if s.has_text_frame:
                for p in s.text_frame.paragraphs:
                    for r in p.runs:
                        if r.font.size:
                            k = r.font.size.pt
                            sizes.setdefault(k, p.text[:24])
                t = inch(s.top) if s.top is not None else -1
                txt = s.text_frame.text.replace("\n", " / ")[:60]
                if 0.1 < t < 0.45 and txt.strip() and kicker is None:
                    kicker = txt
                if t > 5.85 and txt.strip():
                    bottoms.append(f"    y={t} sz={sizes_at(s)} {txt!r}")

    def sizes_at(s):
        return sorted({r.font.size.pt for p in s.text_frame.paragraphs for r in p.runs if r.font.size})

    walk(slide.shapes)
    print(f"--- SLIDE {i+1}: shapes={n_sp} charts={charts} tables={tables} pics={pics}")
    print(f"    kicker: {kicker!r}")
    print(f"    sizes: {sorted(sizes.items(), key=lambda kv: -kv[0])}")
    if bottoms:
        print("    bottom:")
        for b in bottoms:
            print(b)
