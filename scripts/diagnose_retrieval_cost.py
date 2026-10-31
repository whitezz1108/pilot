"""Offline lexical query/paging diagnostics; no model calls or semantic-recall claim."""
import argparse
import json
from pathlib import Path

from pilot01.experiment.cases import CaseRegistry
from pilot01.source.index import BM25Index, excerpt, tokenize

# Global definition-based formulations, identical for all contracts; not per-case tuning.
REFORMULATIONS = {"Change Of Control": ("change control", "merger acquisition ownership"),
                  "Termination For Convenience": ("terminate without cause", "termination any reason")}


def diagnose(registry):
    rows = []
    for c in registry.cases:
        if c.is_negative_sentinel:
            continue
        doc = registry.document(c.case_id)
        gold = {p.paragraph_id for p in doc.paragraphs if any(a < p.end_char and p.start_char < b for a, b in c.gold_spans())}
        index = BM25Index(doc.paragraphs)
        for queries, limit, name in (((c.target_category,), 5, "category_top5"), ((c.target_category,), 10, "category_two_pages"),
                                     (REFORMULATIONS.get(c.target_category, (c.target_category,)), 5, "definition_reformulations")):
            hits = [(p, q) for q in queries for p, _ in index.search(q, top_k=limit)]
            ids = [p.paragraph_id for p, _ in hits]
            rows.append({"case_id": c.case_id, "strategy": name, "queries": list(queries),
                         "candidate_gold_paragraph_overlap": bool(set(ids) & gold),
                         "requests_if_all_pages_read": len(queries) * ((limit + 4)//5),
                         "returned_characters": sum(len(excerpt(p.text, tokenize(q), 400)) for p, q in hits),
                         "duplicate_results": len(ids) - len(set(ids)), "unique_candidates": len(set(ids))})
    return {"note": "Existing development diagnostic only. Paragraph overlap, not semantic recall, agent recovery or actual API cost. No best strategy selected automatically.", "rows": rows}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--registry", required=True)
    p.add_argument("--out", required=True)
    args = p.parse_args()
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(diagnose(CaseRegistry.load_json(args.registry)), ensure_ascii=False, indent=2), encoding="utf-8")
