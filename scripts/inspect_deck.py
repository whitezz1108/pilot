"""Inventory slides 2-4 of the validator audit deck: shapes, geometry, fills, lines, text."""
import sys
from pptx import Presentation
from pptx.util import Emu

PATH = r"C:\Users\44977\Desktop\CUAD_MAS_Stage1_Validator_Audit_20261010.pptx"

def emu2in(v):
    return round(Emu(v).inches, 2) if v is not None else None

def run_color(r):
    try:
        if r.font.color and r.font.color.type is not None:
            t = str(r.font.color.type)
            if "SCHEME" in t:
                return "theme:" + str(r.font.color.theme_color)
            return str(r.font.color.rgb)
    except Exception:
        pass
    return None

def dump_shape(s, indent=0):
    pad = " " * indent
    st = str(s.shape_type)
    try:
        pos = f"L={emu2in(s.left)} T={emu2in(s.top)} W={emu2in(s.width)} H={emu2in(s.height)}"
    except Exception:
        pos = "no-pos"
    line = f'{pad}[{s.shape_id}] {st} "{s.name}" {pos}'
    try:
        if s.fill.type is not None:
            fc = ""
            try:
                fc = str(s.fill.fore_color.rgb)
            except Exception:
                try:
                    fc = "theme:" + str(s.fill.fore_color.theme_color)
                except Exception:
                    fc = "?"
            line += f" fill={str(s.fill.type)}:{fc}"
    except Exception:
        pass
    try:
        if s.line.fill.type is not None:
            try:
                lc = str(s.line.color.rgb)
            except Exception:
                lc = "theme?"
            line += f" line={lc} w={s.line.width}"
    except Exception:
        pass
    # autoshape type
    try:
        from pptx.oxml.ns import qn
        prst = s._element.find(".//" + qn("a:prstGeom"))
        if prst is not None:
            line += f" geom={prst.get('prst')}"
    except Exception:
        pass
    print(line)
    if s.shape_type == 6:
        for sub in s.shapes:
            dump_shape(sub, indent + 2)
    elif s.has_text_frame:
        for p in s.text_frame.paragraphs:
            if p.text.strip():
                sizes = [
                    (
                        r.font.size.pt if r.font.size else None,
                        r.font.bold,
                        run_color(r),
                        r.font.name,
                    )
                    for r in p.runs
                ]
                print(f"{pad}   TEXT: {p.text!r} align={p.alignment} {sizes}")

def main():
    prs = Presentation(PATH)
    idxs = [int(x) for x in sys.argv[1:]] or [1, 2, 3]
    for idx in idxs:
        slide = prs.slides[idx]
        print("=" * 100)
        print(f"SLIDE {idx + 1}  layout={slide.slide_layout.name}")
        for s in slide.shapes:
            dump_shape(s)

if __name__ == "__main__":
    main()
