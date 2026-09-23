"""Building the Gate-4 development set once, for the tests that inspect it.

Building it is not free -- the near-duplicate sketch alone hashes every word
shingle of 510 contracts -- and every test in the Gate-4 files inspects the same
twelve cases. So it is built once per session and shared, which also has the
useful property that every test is looking at *one* development set rather than
at its own separately-built copy that could differ.

Nothing here calls a model. The whole build is a pure function of the frozen
CUAD file and the declared seed, which is what makes it safe to do at import
time of a test module and what makes the assertions below statements about the
artifact the experiment actually ships.
"""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from pilot01.config import load_policy_v1  # noqa: E402
from pilot01.experiment.development import (  # noqa: E402
    build_development_set,
    build_memo_plans,
    load_annotations,
    near_duplicate_groups,
    select_development_cases,
)
from pilot01.source import build_document  # noqa: E402

ARTIFACT_DIR = REPO_ROOT / "outputs" / "development"
"""Where ``scripts/build_development_cases.py`` writes the shipped artifacts.

The tests read the *build*, not this directory: an artifact that was written by
an older revision of the code is not evidence about the current one. The
directory is used only to check that the build script's own output is
consistent with what the modules produce.
"""


@lru_cache(maxsize=1)
def annotations():
    return load_annotations()


@lru_cache(maxsize=1)
def policy_config():
    return load_policy_v1()


@lru_cache(maxsize=1)
def policy():
    return policy_config().to_policy()


@lru_cache(maxsize=1)
def documents():
    return {
        contract.contract_id: build_document(contract.contract_id, contract.context)
        for contract in annotations().contracts
    }


@lru_cache(maxsize=1)
def paragraph_spans():
    return {
        contract_id: [(p.start_char, p.end_char) for p in document.paragraphs]
        for contract_id, document in documents().items()
    }


@lru_cache(maxsize=1)
def clause_spans():
    return {
        contract.contract_id: {
            clause.category: clause.spans for clause in contract.clauses
        }
        for contract in annotations().contracts
    }


@lru_cache(maxsize=1)
def duplicate_of():
    return near_duplicate_groups(
        {contract.contract_id: contract.context for contract in annotations().contracts}
    )


@lru_cache(maxsize=1)
def selection():
    return select_development_cases(
        annotations(),
        policy_targets=policy().target_clause_categories,
        policy_version=policy().policy_version,
        paragraph_spans=paragraph_spans(),
        duplicate_of=duplicate_of(),
    )


@lru_cache(maxsize=1)
def memo_plans():
    return build_memo_plans(
        selection().cases,
        documents=documents(),
        clause_spans=clause_spans(),
        seed=selection().seed,
    )


@lru_cache(maxsize=1)
def case_set():
    return build_development_set(
        annotations=annotations(),
        selection=selection(),
        plans=memo_plans(),
        documents=documents(),
        policy=policy_config(),
    )


@lru_cache(maxsize=1)
def registry():
    return case_set().registry


@lru_cache(maxsize=1)
def positive_records():
    return tuple(record for record in case_set().cases if not record.is_negative_sentinel)


@lru_cache(maxsize=1)
def sentinel_records():
    return tuple(record for record in case_set().cases if record.is_negative_sentinel)


POSITIVE_CASE_IDS = (
    "DEV-POS-COC-0496",
    "DEV-POS-COC-0188",
    "DEV-POS-COC-0286",
    "DEV-POS-COC-0371",
    "DEV-POS-TFC-0436",
    "DEV-POS-TFC-0334",
    "DEV-POS-TFC-0011",
    "DEV-POS-TFC-0212",
)
"""The eight positive case ids the declared seed produces.

Pinned rather than derived, so that a change to the exclusion criteria or the
diversity function shows up as a failing test with the old ids in it, instead of
as a development set that quietly became a different sample. If a change is
intended, these constants are what has to be updated -- deliberately, in a diff
someone reviews.
"""

SENTINEL_CASE_IDS = (
    "DEV-SENT-COC-0123",
    "DEV-SENT-TFC-0395",
    "DEV-SENT-COC-0204",
    "DEV-SENT-TFC-0405",
)
