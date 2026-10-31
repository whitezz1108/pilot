"""Locate only whitespace-equivalent quotes, returning original source bytes."""
import re


def unique_whitespace_quote(document, quote):
    tokens = quote.split()
    if not tokens:
        raise ValueError("empty quote")
    pattern = re.compile(r"(?<!\S)" + r"\s+".join(re.escape(token) for token in tokens) + r"(?!\S)")
    matches = [(p.paragraph_id, m.start(), m.end(), p.text[m.start():m.end()])
               for p in document.paragraphs for m in pattern.finditer(p.text)]
    if len(matches) != 1:
        raise ValueError("whitespace-only quote does not have one unique source match")
    pid, start, end, original = matches[0]
    if original.split() != tokens:
        raise ValueError("non-whitespace source change")
    return {"paragraph_id":pid,"start_in_paragraph":start,"end_in_paragraph":end,"exact_quote":original}
