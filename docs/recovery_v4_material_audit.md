# Recovery experiment material audit

The new recovery experiment uses a derived copy of the frozen clean80 materials. The original registry and reviews remain intact. Preparation is reproducible through `scripts/prepare_recovery_materials.py`, with transformation version `recovery-materials-v1` and seed `20261109`.

## Source and derivation

The verified source is `C:/Users/44977/Desktop/pilot01/data/pilot01/clean80_v1/case_registry_v2.json`. Its source manifest records AI reviews and `human_review_completed=false`. The derived material is stored in the ignored local directory `outputs/recovery_v4/materials`. The derived manifest records the source registry and manifest hashes and retains hash-bound copies of all inherited review files. The source registry contains inline contract text, so moving the derived directory does not depend on external contract text paths. The absolute source path in the manifest is descriptive provenance.

The source placed both policy categories in the first two claim slots. Its target summaries used generic analyst-status statements, including an unsupported assertion that absent categories had been searched. Neutral summaries contained descriptive prose. These regularities could influence behavior independently of the intended requirement text.

Every original and replacement claim now uses the same summary template, generated only from that claim's existing category and status. The template says that the analyst assesses the category with the recorded status and that source references are listed separately. It makes no claim about a search having occurred. Existing status and source IDs are preserved for every claim. This removes descriptive summary details from neutral claims as well as target claims. Any observed outcome therefore applies to this revised material presentation.

Claim order is permuted using a SHA256 ordering key formed from transformation version, seed, neutral case ID and original claim ID. The transformation does not consult the error arm, category, status or gold labels to select the order. Claim IDs are reassigned in that order. E0 and E1 share the same permutation and IDs. The omitted target and its replacement occupy the same remapped slot.

The script retains the original registry and records each ID mapping, so the transformation can be reconstructed and its original inputs recovered. It refuses to overwrite an existing nonempty output directory. It writes the ready manifest after preparing all material and audit files and then invokes the standard `verify_ready_registry` entry point.

## Completed checks

- Verified source registry and review hashes before preparation, and verified the derived registry through the normal readiness check.
- Audited all 80 cases, comprising 64 positive cases and 16 negative sentinels. The positives include 32 cases of each policy target category.
- Verified unchanged contract text, chunking, policy, source references, statuses, hidden labels, case IDs and original case order.
- Verified that all 64 positive E0/E1 pairs retain the same memo ID, claim IDs and five-claim count, with exactly one changed slot.
- Verified zero overlap between remaining E1 upstream citation IDs and omitted-target gold paragraphs.
- Observed omitted-target positions across slots 1 through 5 of 16, 12, 8, 16 and 12 cases respectively. Position balance is approximate under the frozen seed.
- Four regression tests passed, covering deterministic transformation, paired evidence preservation, normalization of legacy narratives, rejection of residual gold citations, portable hash-bound lineage and refusal to overwrite frozen output. Initial pytest invocations encountered sandbox temporary-directory setup failures. The passing run used a fresh workspace-local temporary directory.

## Limits of the checks

Gold labels are retained from CUAD without new semantic adjudication. Inherited AI reviews cover the original claims, while the derived wording and order receive mechanical checks. The derived manifest continues to report no completed human review.

The missing policy category remains observable in E1 because that absence is part of the experimental manipulation. Neutral identifiers, shared claim counts and permuted slots remove additional cues but cannot conceal the task-relevant content difference. Different category names and statuses can still produce different token lengths.

Paragraph overlap is a structural check. It cannot rule out paraphrases, cross-references, annotation errors or source paragraphs containing relevant facts outside annotated spans. Unexpected live outcomes require inspection of the underlying contract and actual model-visible evidence. Passing these checks does not establish absolute freedom from information leakage or semantic error.
