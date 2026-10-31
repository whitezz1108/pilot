"""Global academic restyle per STYLE_PROMPT.md (Light Academic Research Deck).

- bg #FAF9F5, palette remap (gray-blue primary, green/orange/red/purple semantics)
- font sizes down: titles 30->22, card titles ~11, body ~9.5-10.5, footer 8.5
- white cards: colored borders -> #E2DFD6 hairline 0.75pt (top color bars stay)
- kicker -> rounded chip, meta line 9.5pt, footer muted 8.5pt
- bottom takeaway line -> #ECF1F7 rounded banner, 10.5pt bold #2F496B
- chart XMLs: same color remap + smaller chart fonts
"""
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Inches, Pt

SRC = r"C:\Users\44977\Desktop\CUAD_MAS_Stage1_Validator_Audit_20261010.pptx"
DST = r"C:\Users\44977\Desktop\CUAD_MAS_Stage1_Validator_Audit_20261010_updated.pptx"

CMAP = {
    "4267D5": "3E5F8A", "16233B": "262B33", "687589": "545D6B",
    "16998B": "4F7D5B", "C38935": "B07A3F", "8364B5": "7B5E8C",
    "B9535A": "A85751", "D7DEE8": "DDD9CE", "666666": "545D6B",
    "D4D4D4": "DDD9CE",
}
SZ = {
    "5400": "3200", "4575": "3200", "3675": "2800", "3450": "2600",
    "3150": "2400", "3000": "2200", "2550": "1700", "2400": "1600",
    "2325": "1500", "2250": "1500", "2175": "1200", "2100": "1200",
    "2025": "1150", "1950": "1150", "1875": "1100", "1800": "1100",
    "1725": "1050", "1650": "1050", "1575": "1000", "1500": "1000",
    "1463": "1000", "1425": "1000", "1350": "950", "1200": "950",
    "1125": "950", "1050": "850",
}
COVER_SZ = {
    "4500": "4000", "3225": "2400", "2175": "1500", "1875": "1300",
    "1800": "1300", "1650": "1150", "1350": "1050", "1125": "900",
}
# slide_idx (0-based) -> takeaway line top (inches); slide 3 (idx 2) rebuilt separately
BANNERS = {
    1: 6.04, 3: 6.47, 4: 6.44, 5: 6.53, 6: 6.49, 7: 6.48, 8: 6.48,
    9: 6.5, 10: 6.5, 11: 6.47, 12: 6.51, 13: 6.5, 14: 6.6, 15: 6.51,
    16: 6.51, 17: 6.51,
}


def set_geom(shape, prst, adj=None):
    from pptx.oxml import parse_xml
    from pptx.oxml.ns import nsdecls
    gd = f'<a:gd name="adj" fmla="val {adj}"/>' if adj is not None else ""
    new = parse_xml(
        f'<a:prstGeom {nsdecls("a")} prst="{prst}"><a:avLst>{gd}</a:avLst></a:prstGeom>'
    )
    spPr = shape._element.spPr
    spPr.replace(spPr.find(qn("a:prstGeom")), new)


def remap_slide_colors(slide):
    for el in slide._element.iter():
        if el.tag == qn("a:srgbClr"):
            v = el.get("val").upper()
            if v in CMAP:
                el.set("val", CMAP[v])


def remap_sizes(slide, size_map):
    for el in slide._element.iter():
        sz = el.get("sz")
        if sz and sz in size_map:
            el.set("sz", size_map[sz])


def iter_shapes(shapes):
    for s in shapes:
        if s.shape_type == 6:
            yield from iter_shapes(s.shapes)
        else:
            yield s


def chip(slide):
    for s in iter_shapes(slide.shapes):
        try:
            t = Emu(s.top).inches
        except Exception:
            continue
        if 0.18 <= t <= 0.36 and s.has_text_frame and s.text_frame.text.strip():
            tf = s.text_frame
            txt = tf.text
            w = sum(0.135 if ord(c) > 0x2E7F else 0.072 for c in txt) + 0.34
            set_geom(s, "roundRect", 50000)
            s.fill.solid()
            s.fill.fore_color.rgb = RGBColor.from_string("ECF1F7")
            s.line.color.rgb = RGBColor.from_string("3E5F8A")
            s.line.width = Pt(0.75)
            s.top, s.height, s.width = Inches(0.22), Inches(0.30), Inches(w)
            tf.vertical_anchor = MSO_ANCHOR.MIDDLE
            tf.margin_left = tf.margin_right = Inches(0.05)
            tf.margin_top = tf.margin_bottom = 0
            for p in tf.paragraphs:
                p.alignment = PP_ALIGN.CENTER
                for r in p.runs:
                    r.font.size = Pt(9.5)
                    r.font.bold = True
                    r.font.color.rgb = RGBColor.from_string("2F496B")
            return


def banner(slide, top_in):
    tgt, best = None, 1e9
    for s in iter_shapes(slide.shapes):
        if not (s.has_text_frame and s.text_frame.text.strip()):
            continue
        t = Emu(s.top).inches
        if abs(t - top_in) < 0.08 and Emu(s.width).inches >= 5:
            if t < best:
                tgt, best = s, t
    if tgt is None:
        return
    set_geom(tgt, "roundRect", 19048)
    tgt.fill.solid()
    tgt.fill.fore_color.rgb = RGBColor.from_string("ECF1F7")
    tgt.line.fill.background()
    tgt.top, tgt.height = Inches(6.35), Inches(0.42)
    # keep clear of a caption sitting right above
    for s in iter_shapes(slide.shapes):
        if s is tgt or not (s.has_text_frame and s.text_frame.text.strip()):
            continue
        btm = Emu(s.top).inches + Emu(s.height).inches
        if 5.8 < btm <= 6.36 + 0.05 and Emu(s.width).inches >= 5:
            tgt.top = Inches(btm + 0.07)
    tf = tgt.text_frame
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    tf.margin_left, tf.margin_right = Inches(0.15), Inches(0.1)
    tf.margin_top = tf.margin_bottom = 0
    for p in tf.paragraphs:
        for r in p.runs:
            r.font.size = Pt(10.5)
            r.font.bold = True
            r.font.color.rgb = RGBColor.from_string("2F496B")


def neutralize_borders(slide):
    for s in iter_shapes(slide.shapes):
        try:
            if min(Emu(s.width).inches, Emu(s.height).inches) < 0.45:
                continue
            if s.fill.type != 1 or str(s.fill.fore_color.rgb) != "FFFFFF":
                continue
            if s.line.fill.type != 1:
                continue
            s.line.color.rgb = RGBColor.from_string("E2DFD6")
            s.line.width = Pt(0.75)
        except Exception:
            continue


def header_footer(slide):
    for s in iter_shapes(slide.shapes):
        try:
            t = Emu(s.top).inches
        except Exception:
            continue
        if s.has_text_frame and s.text_frame.text.strip():
            if 1.2 <= t <= 1.4:
                for p in s.text_frame.paragraphs:
                    for r in p.runs:
                        r.font.size = Pt(9.5)
            elif t >= 6.95:
                for p in s.text_frame.paragraphs:
                    for r in p.runs:
                        r.font.size = Pt(8.5)
                        r.font.color.rgb = RGBColor.from_string("8A929E")


def patch_charts(prs):
    for part in prs.part.package.iter_parts():
        name = str(part.partname)
        if "/charts/chart" not in name or not name.endswith(".xml"):
            continue
        for el in part._element.iter():
            if el.tag == qn("a:srgbClr"):
                v = (el.get("val") or "").upper()
                if v in CMAP:
                    el.set("val", CMAP[v])
            sz = el.get("sz")
            if sz and sz in SZ:
                el.set("sz", SZ[sz])


def main():
    prs = Presentation(SRC)
    for i, slide in enumerate(prs.slides):
        slide.background.fill.solid()
        slide.background.fill.fore_color.rgb = RGBColor.from_string("FAF9F5")
        remap_slide_colors(slide)
        remap_sizes(slide, COVER_SZ if i == 0 else SZ)
        if i == 0:
            continue
        chip(slide)
        header_footer(slide)
        neutralize_borders(slide)
        if i in BANNERS:
            banner(slide, BANNERS[i])
    # slide 4: Validator/Gate card boxes pop with evidence-green tint
    s4 = prs.slides[3]
    for s in iter_shapes(s4.shapes):
        # identify by geometry: cards at x=5.61 and x=9.18, y=3.3
        try:
            w = Emu(s.width).inches
        except Exception:
            continue
        if (
            abs(Emu(s.top).inches - 3.3) < 0.05
            and abs(Emu(s.height).inches - 1.04) < 0.05
            and w < 1.7
            and (abs(Emu(s.left).inches - 5.61) < 0.05 or abs(Emu(s.left).inches - 9.18) < 0.05)
        ):
            s.fill.solid()
            s.fill.fore_color.rgb = RGBColor.from_string("ECF3EE")
            s.line.color.rgb = RGBColor.from_string("4F7D5B")
            s.line.width = Pt(0.75)
    # drop the thin rule directly above slide-4 banner (redundant under a banner)
    for s in list(iter_shapes(s4.shapes)):
        try:
            if abs(Emu(s.top).inches - 6.36) < 0.03 and Emu(s.height).inches < 0.05:
                s._element.getparent().remove(s._element)
        except Exception:
            pass
    patch_charts(prs)
    prs.save(DST)
    print("saved", DST)


if __name__ == "__main__":
    main()
