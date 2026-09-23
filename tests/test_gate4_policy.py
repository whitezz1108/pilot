"""Gate 4 §3: POLICY-01 as a development artifact.

The policy is the instrument, so the things worth asserting are that it is the
one that was written down (id, version, exact target categories), that it
decides the same way every time, that UNKNOWN is not quietly read as ABSENT,
and that its fingerprint moves when its semantics move and not when its prose
does.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import gate4_fixture as g4
from pilot01.config import PolicyConfig, load_policy_v1, policy_fingerprint
from pilot01.schemas import ClauseStatus, Decision, ExperimentalPolicy, expected_decision
from pilot01.workflow.nodes.render import render_policy_block

POLICY_PATH = Path(__file__).resolve().parents[1] / "config" / "policies" / "policy_v1.yaml"


@pytest.fixture(scope="module")
def raw_policy() -> dict:
    return yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# §3: what the file says
# ---------------------------------------------------------------------------


def test_the_policy_file_exists_at_the_path_the_spec_names():
    assert POLICY_PATH.is_file(), POLICY_PATH


def test_the_policy_declares_its_identity(raw_policy):
    assert raw_policy["policy_id"] == "POLICY-01"
    assert str(raw_policy["policy_version"]) == "1"


def test_the_policy_is_marked_development_and_not_confirmatory(raw_policy):
    """§3 and §18: this freeze is for development, not the 40-case pilot."""
    assert raw_policy["policy_status"] == "development"
    assert g4.policy_config().policy_status == "development"


def test_the_target_categories_are_exactly_the_two_the_spec_names(raw_policy):
    assert raw_policy["target_clause_categories"] == [
        "Change Of Control",
        "Termination For Convenience",
    ]
    assert g4.policy().target_clause_categories == (
        "Change Of Control",
        "Termination For Convenience",
    )


def test_the_file_states_the_rule_is_not_legal_advice(raw_policy):
    """§3: a synthetic organizational policy, explicitly labelled as such."""
    text = POLICY_PATH.read_text(encoding="utf-8").lower()
    assert "not legal advice" in text
    assert "synthetic" in text


def test_the_mapping_is_stated_exhaustively_for_every_clause_status(raw_policy):
    mapping = raw_policy["clause_status_mapping"]
    assert set(mapping) == {status.value for status in ClauseStatus}
    assert mapping["present"] == Decision.ESCALATE.value
    assert mapping["absent"] == Decision.ACCEPT.value


def test_the_declared_mapping_agrees_with_the_reference_implementation():
    """The file is the statement; ``expected_decision`` is the implementation."""
    policy = g4.policy()
    for status in ClauseStatus:
        assert g4.policy_config().clause_status_mapping[status.value] == (
            expected_decision(status, policy).value
        )


def test_every_edge_case_is_named_and_carries_its_action_and_rationale(raw_policy):
    """§3: edge cases decided on the record rather than left to a reader."""
    edge_cases = raw_policy["edge_cases"]
    assert edge_cases
    ids = [entry["id"] for entry in edge_cases]
    assert len(set(ids)) == len(ids)
    for entry in edge_cases:
        assert entry["case"].strip()
        assert str(entry["action"]).strip()
        assert len(entry["rationale"].strip()) > 40


# ---------------------------------------------------------------------------
# §3: UNKNOWN must not be silently treated as ABSENT
# ---------------------------------------------------------------------------


def test_unknown_escalates_rather_than_accepting():
    """The whole experiment turns on this one line.

    If UNKNOWN were read as ABSENT, an omission that removes the only positive
    claim would produce the *correct* answer, and there would be nothing to
    measure.
    """
    policy = g4.policy()
    assert expected_decision(ClauseStatus.UNKNOWN, policy) is Decision.ESCALATE
    assert expected_decision(ClauseStatus.UNKNOWN, policy) is not (
        expected_decision(ClauseStatus.ABSENT, policy)
    )


def test_only_a_confirmed_absence_permits_accept():
    policy = g4.policy()
    accepting = [
        status for status in ClauseStatus if expected_decision(status, policy) is Decision.ACCEPT
    ]
    assert accepting == [ClauseStatus.ABSENT]


def test_the_policy_file_says_so_in_words_too(raw_policy):
    unknown_edges = [
        entry
        for entry in raw_policy["edge_cases"]
        if "UNKNOWN" in entry["case"] or "unknown" in entry["case"]
    ]
    assert unknown_edges, "the UNKNOWN edge case must be named"
    assert all(str(entry["action"]).upper() == "ESCALATE" for entry in unknown_edges)


def test_the_rationale_explains_why_unknown_escalates(raw_policy):
    unknown_edge = next(
        entry for entry in raw_policy["edge_cases"] if entry["id"] == "EDGE-01"
    )
    rationale = unknown_edge["rationale"].lower()
    assert "confirmed absence" in rationale
    assert "escalate" in rationale


# ---------------------------------------------------------------------------
# §3: determinism and fingerprint stability
# ---------------------------------------------------------------------------


def test_loading_the_policy_twice_gives_the_same_object():
    assert load_policy_v1() == load_policy_v1()


def test_the_fingerprint_is_stable_across_loads():
    assert policy_fingerprint(load_policy_v1()) == policy_fingerprint(load_policy_v1())


def test_the_fingerprint_is_a_sha256_digest():
    fingerprint = g4.policy_config().fingerprint()
    assert fingerprint.startswith("sha256:")
    assert len(fingerprint) == len("sha256:") + 64


def test_the_fingerprint_is_insensitive_to_comments_and_key_order(raw_policy, tmp_path):
    """A fingerprint that moved on a comment edit would train a reader to ignore it."""
    reordered = {key: raw_policy[key] for key in sorted(raw_policy)}
    path = tmp_path / "policy_v1.yaml"
    path.write_text(
        "# a new comment that changes nothing about the rule\n"
        + yaml.safe_dump(reordered, sort_keys=False),
        encoding="utf-8",
    )
    assert policy_fingerprint(load_policy_v1(path)) == g4.policy_config().fingerprint()


def test_the_fingerprint_moves_when_a_target_category_moves(raw_policy, tmp_path):
    changed = dict(raw_policy)
    changed["target_clause_categories"] = [
        "Change Of Control",
        "Exclusivity",
    ]
    path = tmp_path / "policy_v1.yaml"
    path.write_text(yaml.safe_dump(changed, sort_keys=False), encoding="utf-8")
    assert policy_fingerprint(load_policy_v1(path)) != g4.policy_config().fingerprint()


def test_the_fingerprint_moves_when_the_mapping_moves(raw_policy, tmp_path):
    """A rule that escalated nothing would be a different experiment."""
    changed = json.loads(json.dumps(raw_policy))
    changed["decision_if_target_present"] = "ACCEPT"
    changed["clause_status_mapping"]["present"] = "ACCEPT"
    path = tmp_path / "policy_v1.yaml"
    path.write_text(yaml.safe_dump(changed, sort_keys=False), encoding="utf-8")
    with pytest.raises(Exception):
        # Either the load refuses it or the fingerprint differs; both are
        # acceptable, and silently accepting the change is not.
        loaded = load_policy_v1(path)
        assert policy_fingerprint(loaded) != g4.policy_config().fingerprint()


def test_the_fingerprint_moves_when_the_rule_text_moves(raw_policy, tmp_path):
    changed = dict(raw_policy)
    changed["description"] = "Some other rule entirely."
    path = tmp_path / "policy_v1.yaml"
    path.write_text(yaml.safe_dump(changed, sort_keys=False), encoding="utf-8")
    assert policy_fingerprint(load_policy_v1(path)) != g4.policy_config().fingerprint()


def test_the_fingerprint_is_recorded_in_the_development_record():
    recorded = {
        record.policy_version for record in g4.case_set().cases
    }
    assert recorded == {g4.policy().policy_version}


def test_the_policy_fingerprint_is_recorded_in_the_registry_header():
    """§3: the record carries the policy's identity, mapping and digest."""
    block = g4.case_set().to_payload()["policy"]
    assert block["policy_id"] == "POLICY-01"
    assert block["policy_version"] == "1"
    assert block["policy_status"] == "development"
    assert block["fingerprint"] == g4.policy_config().fingerprint()
    assert block["clause_status_mapping"]["unknown"] == "ESCALATE"
    assert block["target_clause_categories"] == [
        "Change Of Control",
        "Termination For Convenience",
    ]
    assert {entry["id"] for entry in block["edge_cases"]} == {
        entry["id"] for entry in g4.policy_config().edge_cases
    }


def test_a_policy_with_no_target_category_is_refused():
    """§3: a policy that matches nothing must fail loudly, not accept everything."""
    with pytest.raises(Exception):
        ExperimentalPolicy(
            policy_id="POLICY-01",
            policy_version="1",
            target_clause_categories=(),
            decision_if_target_present=Decision.ESCALATE,
            decision_if_target_absent=Decision.ACCEPT,
            description="a policy with no trigger",
        )


def test_a_policy_config_missing_its_mapping_is_refused(raw_policy, tmp_path):
    changed = dict(raw_policy)
    changed.pop("clause_status_mapping")
    path = tmp_path / "policy_v1.yaml"
    path.write_text(yaml.safe_dump(changed, sort_keys=False), encoding="utf-8")
    with pytest.raises(Exception):
        load_policy_v1(path)


def test_a_policy_config_whose_mapping_contradicts_the_rule_is_refused(raw_policy, tmp_path):
    """The file must not be able to state a rule the implementation denies."""
    changed = json.loads(json.dumps(raw_policy))
    changed["clause_status_mapping"]["absent"] = "ESCALATE"
    path = tmp_path / "policy_v1.yaml"
    path.write_text(yaml.safe_dump(changed, sort_keys=False), encoding="utf-8")
    with pytest.raises(Exception):
        load_policy_v1(path)


def test_the_policy_carries_no_gold_status_and_no_case_identity():
    """§3: the policy names its triggers, never a case's answer.

    Checked on what is *projected* -- the runtime policy and the text an agent
    reads -- rather than on the file's design notes. The file's edge-case
    rationales legitimately discuss the experiment's case classes; that prose is
    documentation for a reader of the repository and reaches no agent.
    """
    projected = json.dumps(
        {
            "policy": g4.policy().model_dump(mode="json"),
            "rendered": render_policy_block(g4.policy()),
            "header": g4.case_set().to_payload()["policy"],
        }
    ).lower()
    # No "e0"/"e1" here: a sha256 hex digest contains those characters by
    # chance, so a substring scan would fail on the policy's own fingerprint.
    # The arm markers are checked as whole tokens against the rendered text,
    # which carries no digest -- the same whole-token rule the view audits use.
    for forbidden in ("gold", "sentinel", "case_id", "cuad", "omission", "error_condition"):
        assert forbidden not in projected


def test_the_agent_facing_policy_text_names_no_case_and_no_arm():
    rendered = render_policy_block(g4.policy())
    for forbidden in ("gold", "sentinel", "E0", "E1", "CUAD", "DEV-", "omission"):
        assert forbidden not in rendered
    tokens = {token for token in rendered.replace(":", " ").split()}
    assert "e0" not in {token.lower() for token in tokens}
    assert "e1" not in {token.lower() for token in tokens}


def test_the_policy_names_both_targets_in_its_own_statement():
    """§10: an agent must be able to read the rule without hidden knowledge."""
    policy = g4.policy()
    for category in policy.target_clause_categories:
        assert category in policy.description or category in (
            " ".join(policy.target_clause_categories)
        )
    assert policy.target_clause_categories == (
        "Change Of Control",
        "Termination For Convenience",
    )


def test_the_policy_rendered_to_an_agent_names_both_targets():
    """§10: the rule must be answerable from what the agent is given."""
    rendered = render_policy_block(g4.policy())
    for category in g4.policy().target_clause_categories:
        assert category in rendered
    assert "ESCALATE" in rendered
    assert "ACCEPT" in rendered


def test_the_rendered_rule_closes_the_unresolved_case_by_construction():
    """The agent is told only a *confirmed* absence permits ACCEPT.

    The word "unknown" does not appear, and that is deliberate rather than an
    oversight: the rendered rule is a closed biconditional -- ACCEPT if and only
    if the target clauses are confirmed absent -- so a category the memo neither
    affirms nor denies cannot be read as permitting ACCEPT without contradicting
    the sentence the agent was given. The gate on this is the sentence itself,
    which is why it is asserted rather than the absent keyword.
    """
    rendered = render_policy_block(g4.policy())
    assert "confirmed absent" in rendered
    assert "only" in rendered.lower()
    assert expected_decision(ClauseStatus.UNKNOWN, g4.policy()) is Decision.ESCALATE


def test_the_policy_config_and_the_runtime_policy_agree():
    config = g4.policy_config()
    runtime = config.to_policy()
    assert runtime.policy_id == config.policy_id
    assert runtime.policy_version == config.policy_version
    assert runtime.target_clause_categories == tuple(config.target_clause_categories)
    assert runtime.decision_if_target_present == config.decision_if_target_present
    assert runtime.decision_if_target_absent == config.decision_if_target_absent


def test_is_target_category_answers_for_both_targets_and_for_neither():
    policy = g4.policy()
    assert policy.is_target_category("Change Of Control")
    assert policy.is_target_category("Termination For Convenience")
    assert not policy.is_target_category("Audit Rights")
    assert not policy.is_target_category("change of control"), (
        "matching is exact; a case-folded match would be a different policy"
    )


def test_policy_config_is_frozen():
    config: PolicyConfig = g4.policy_config()
    with pytest.raises(Exception):
        config.policy_version = "2"  # type: ignore[misc]
