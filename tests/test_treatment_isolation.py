"""Gate 2.5, section 3: the treatment is one sentence and nothing else.

The governance condition is a *prompt-level* manipulation. That makes the prompt
itself an experimental instrument, and an instrument that varies in more than
one place measures more than one thing. These tests diff the rendered prompts
across the three main conditions and assert that the only lines that move are
the pre-registered permission sentence and the tool surface that follows from
the same flag.

Two things are checked, and they are different claims:

* **The permission sentence is the pre-registered one.** A0V0, A1V0 and A1V1
  each render the exact wording the design fixed for them, so a later edit to
  ``render.py`` cannot quietly reword the treatment.
* **Nothing else moves.** The role, the policy, the task, the output schema and
  the general rules are byte-identical across conditions, and the two role
  prompts carry a byte-identical ``# SOURCE TOOLS`` section -- so the two roles
  differ in their *inputs*, never in what the condition gives them.

The diff is computed by normalising the governance block out and comparing the
remainder as bytes, rather than by comparing line sets: a set comparison would
miss a line that changed its position, and position is part of what the model
reads.
"""

from __future__ import annotations

import re

import pytest

import sample_case
from pilot01.prompts import load_compliance_prompt, load_manager_prompt
from pilot01.source.tools import SOURCE_TOOL_NAMES
from pilot01.workflow.nodes.compliance import build_compliance_messages
from pilot01.workflow.nodes.manager import build_manager_messages
from pilot01.workflow.nodes.render import (
    MAY_NOT_SEARCH,
    MAY_SEARCH,
    MUST_VERIFY,
    PERMISSIONS,
    render_governance_block,
)
from pilot01.workflow.nodes.tool_loop import render_tool_surface
from pilot01.workflow.views import build_compliance_view, build_manager_view

MAIN_CONDITIONS = ("A0V0", "A1V0", "A1V1")

#: The pre-registered permission each main condition must render, verbatim.
EXPECTED_PERMISSION = {
    "A0V0": MAY_NOT_SEARCH,
    "A1V0": MAY_SEARCH,
    "A1V1": MUST_VERIFY,
}

TOOL_SURFACE_HEADER = "You may use these source tools:"
NO_TOOL_SURFACE = "No source tools are available to you in this workflow."


def rendered(condition_id: str, make_state, *, role: str) -> tuple[str, str]:
    """The (system, user) text a role sees under one condition."""
    state = make_state(condition_id=condition_id)
    if role == "manager":
        messages = build_manager_messages(build_manager_view(state))
    else:
        state.manager_output = sample_case.manager_output()
        messages = build_compliance_messages(build_compliance_view(state))
    system = next(m.content for m in messages if m.role == "system")
    user = next(m.content for m in messages if m.role == "user")
    return system, user


def strip_governance(user_text: str) -> str:
    """The user message with the governance block removed, byte for byte.

    The block runs from its ``GOVERNANCE`` heading to the end of the message,
    because everything the condition varies lives inside it.
    """
    marker = "\nGOVERNANCE\n"
    head, separator, _ = user_text.partition(marker)
    assert separator, "the user message carries no governance block"
    return head


# --------------------------------------------------------------------------
# The pre-registered wording
# --------------------------------------------------------------------------


@pytest.mark.parametrize("condition_id", MAIN_CONDITIONS)
@pytest.mark.parametrize("role", ["manager", "compliance"])
def test_each_condition_renders_its_pre_registered_permission(
    condition_id, role, make_state
):
    _, user = rendered(condition_id, make_state, role=role)
    expected = EXPECTED_PERMISSION[condition_id]
    assert expected in user
    for other_id, other in EXPECTED_PERMISSION.items():
        if other_id != condition_id:
            assert other not in user, f"{condition_id} also renders {other_id}'s sentence"


@pytest.mark.parametrize("condition_id", MAIN_CONDITIONS)
def test_the_permission_rendered_is_the_table_entry_for_the_flags(condition_id, make_state):
    """The sentence is selected by the flags, and the flags come from the
    condition -- so the rendered permission cannot disagree with the enforced
    one."""
    state = make_state(condition_id=condition_id)
    view = build_manager_view(state)
    expected = PERMISSIONS[(view.source_access, view.verification_required)]
    _, user = rendered(condition_id, make_state, role="manager")
    assert expected in user


def test_the_three_conditions_render_three_distinct_permissions(make_state):
    rendered_permissions = {
        condition_id: EXPECTED_PERMISSION[condition_id] for condition_id in MAIN_CONDITIONS
    }
    assert len(set(rendered_permissions.values())) == 3
    for condition_id in MAIN_CONDITIONS:
        _, user = rendered(condition_id, make_state, role="manager")
        assert user.count(rendered_permissions[condition_id]) == 1


# --------------------------------------------------------------------------
# Nothing else moves
# --------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["manager", "compliance"])
def test_the_system_message_is_byte_identical_across_conditions(role, make_state):
    """The treatment lives in the user turn, so the role prompt is untouched.

    This is the strongest form of the claim: not "the differences are small" but
    "there are none".
    """
    texts = {c: rendered(c, make_state, role=role)[0] for c in MAIN_CONDITIONS}
    assert len(set(texts.values())) == 1, {
        c: t[:200] for c, t in texts.items()
    }


@pytest.mark.parametrize("role", ["manager", "compliance"])
def test_everything_outside_the_governance_block_is_byte_identical(role, make_state):
    """The case, the policy, the memo (or handoff) and the task do not move."""
    stripped = {
        c: strip_governance(rendered(c, make_state, role=role)[1])
        for c in MAIN_CONDITIONS
    }
    assert len(set(stripped.values())) == 1, {
        c: t[:200] for c, t in stripped.items()
    }


@pytest.mark.parametrize("role", ["manager", "compliance"])
def test_the_governance_block_decomposes_into_flags_permission_and_surface(
    role, make_state
):
    """Whatever the block says, it says it in that order and nothing else.

    The surface is longer in A1 than in A0 -- two tool signatures and an envelope
    against one refusal line -- so the blocks are not the same length and should
    not be. What must hold is that the block *is* its four parts in order, with
    no fifth thing appended: a line count difference is only legitimate if the
    extra lines are the advertised surface itself.
    """
    for condition_id in MAIN_CONDITIONS:
        view = build_manager_view(make_state(condition_id=condition_id))
        surface = render_tool_surface(
            names=SOURCE_TOOL_NAMES, available=view.source_access
        )
        block = render_governance_block(
            source_access=view.source_access,
            verification_required=view.verification_required,
            tool_surface=surface,
        )
        lines = block.splitlines()
        assert lines[0] == "GOVERNANCE"
        assert lines[1] == f"source_access: {str(view.source_access).lower()}"
        assert (
            lines[2] == f"verification_required: {str(view.verification_required).lower()}"
        )
        assert lines[3] == ""
        assert lines[4] == EXPECTED_PERMISSION[condition_id]
        assert lines[5] == ""
        assert "\n".join(lines[6:]) == surface
        # And no extra prose between the permission and the surface.
        assert len(lines) == 6 + len(surface.splitlines())


@pytest.mark.parametrize("role", ["manager", "compliance"])
def test_the_prefix_before_the_governance_block_is_byte_identical(role, make_state):
    """The case, the policy, the memo (or handoff): identical, in the same order.

    This is what makes the block's own variation safe to interpret -- everything
    the model reads *before* the permission is the same text under every
    condition, so a behavioural difference cannot come from the material.
    """
    prefixes = {
        c: rendered(c, make_state, role=role)[1].partition("\nGOVERNANCE\n")[0]
        for c in MAIN_CONDITIONS
    }
    assert len(set(prefixes.values())) == 1
    # ...and the prefix is non-trivial, so the assertion above is not vacuous.
    assert len(prefixes["A0V0"]) > 200


def test_the_governance_block_varies_only_in_the_permission(make_state):
    """Diff the block itself, with the tool surface excluded."""
    blocks = {}
    for condition_id in MAIN_CONDITIONS:
        state = make_state(condition_id=condition_id)
        view = build_manager_view(state)
        blocks[condition_id] = render_governance_block(
            source_access=view.source_access,
            verification_required=view.verification_required,
        )
    # Identical structure: same number of lines, same headings.
    line_counts = {c: len(b.splitlines()) for c, b in blocks.items()}
    assert len(set(line_counts.values())) == 1, line_counts
    for condition_id, block in blocks.items():
        assert block.startswith("GOVERNANCE\n")
        assert f"source_access: {str(condition_id.startswith('A1')).lower()}" in block
        assert (
            f"verification_required: {str(condition_id.endswith('V1')).lower()}" in block
        )


# --------------------------------------------------------------------------
# The two roles are given the same condition
# --------------------------------------------------------------------------


@pytest.mark.parametrize("condition_id", MAIN_CONDITIONS)
def test_the_two_roles_are_offered_the_same_surface(condition_id, make_state):
    """Same tools, same wording. The roles differ in their inputs, not their
    permissions -- otherwise the condition would not be a property of the run."""
    _, manager_user = rendered(condition_id, make_state, role="manager")
    _, compliance_user = rendered(condition_id, make_state, role="compliance")
    for marker in (
        EXPECTED_PERMISSION[condition_id],
        TOOL_SURFACE_HEADER,
        NO_TOOL_SURFACE,
        "- `search_contract(query)`",
        "- `open_source_span(paragraph_id)`",
        "tool_request",
    ):
        assert (marker in manager_user) == (marker in compliance_user), marker


def test_a0_advertises_no_tool_surface_at_all(make_state):
    """A0 is not "tools you may not use" -- the surface is not offered."""
    _, user = rendered("A0V0", make_state, role="manager")
    assert NO_TOOL_SURFACE in user
    assert TOOL_SURFACE_HEADER not in user
    assert "search_contract" not in user.replace(
        # The refusal text names the tools it is refusing; that is the only
        # place an A0 prompt is allowed to mention them.
        NO_TOOL_SURFACE,
        "",
    )


@pytest.mark.parametrize("condition_id", ["A1V0", "A1V1"])
def test_a1_advertises_exactly_two_tools(condition_id, make_state):
    _, user = rendered(condition_id, make_state, role="manager")
    assert TOOL_SURFACE_HEADER in user
    assert "- `search_contract(query)`" in user
    assert "- `open_source_span(paragraph_id)`" in user
    assert NO_TOOL_SURFACE not in user


# --------------------------------------------------------------------------
# The treatment text never names the experiment
# --------------------------------------------------------------------------


CONDITION_TOKEN_RE = re.compile(r"\b(?:A0V0|A1V0|A1V1|A0V1|A0|A1|V0|V1|E0|E1)\b")

TREATMENT_LEAKS = (
    "hypothesis",
    "expected direction",
    "expected result",
    "expected answer",
    "supposed to fail",
    "should fail",
    "gold",
    "target location",
    "error condition",
    "treatment",
    "omission",
    "omitted",
    "condition",
)


@pytest.mark.parametrize("condition_id", MAIN_CONDITIONS)
@pytest.mark.parametrize("role", ["manager", "compliance"])
def test_the_rendered_prompt_never_names_the_condition(condition_id, role, make_state):
    system, user = rendered(condition_id, make_state, role=role)
    for text in (system, user):
        assert not CONDITION_TOKEN_RE.search(text), CONDITION_TOKEN_RE.findall(text)
        lowered = text.lower()
        for leak in TREATMENT_LEAKS:
            assert leak not in lowered, f"{condition_id} prompt mentions {leak!r}"


@pytest.mark.parametrize("sentence", sorted(set(PERMISSIONS.values())))
def test_no_permission_sentence_states_a_reason(sentence):
    """The treatment states what is permitted, never what it is for.

    A permission sentence that explained itself -- "you should check, because
    the memo may be incomplete" -- would hand the model the hypothesis.
    """
    lowered = sentence.lower()
    for leak in TREATMENT_LEAKS + ("because", "since", "in order to", "so that"):
        assert leak not in lowered, f"{sentence!r} states a reason: {leak!r}"
    assert "you must" in lowered or "you may" in lowered or "you do not" in lowered


@pytest.mark.parametrize("condition_id", MAIN_CONDITIONS)
def test_the_treatment_block_is_the_last_thing_the_agent_reads(condition_id, make_state):
    """Position is part of the manipulation: the permission is stated after the
    material it governs, and nothing follows it but the tool surface."""
    _, user = rendered(condition_id, make_state, role="manager")
    head, _, tail = user.partition("\nGOVERNANCE\n")
    assert head
    assert "source_access:" in tail
    assert EXPECTED_PERMISSION[condition_id] in tail


# --------------------------------------------------------------------------
# The prompt files themselves
# --------------------------------------------------------------------------


def prompt_section(text: str, heading: str) -> str:
    match = re.search(rf"^#\s+{heading}\s*$(.*?)(?=^#\s|\Z)", text, re.MULTILINE | re.DOTALL)
    assert match, f"no '# {heading}' section"
    return match.group(1)


def test_the_source_tools_section_is_the_same_for_both_roles():
    """Same tools, described in the same words.

    The only permitted difference is the word naming each role's *upstream
    artifact* -- the Manager holds a memo, Compliance holds a handoff -- because
    that sentence tells the agent what its own upstream ids are. Everything
    else, including the envelope, the limit and the warning that a paragraph id
    is not a source id, is byte-identical: a role must not be told something
    about the tool surface the other role is not.
    """
    manager = prompt_section(load_manager_prompt().text, "SOURCE TOOLS")
    compliance = prompt_section(load_compliance_prompt().text, "SOURCE TOOLS")

    def normalise(text: str) -> str:
        # The two words differ in length, so the hard-wrapped lines fall in
        # different places; collapsing whitespace compares the *text*, which is
        # what the model reads, rather than the wrap points.
        for word in ("memo", "handoff"):
            text = re.sub(rf"\bthe {word}\b", "the upstream artifact", text)
        return re.sub(r"\s+", " ", text).strip()

    assert normalise(manager) == normalise(compliance)
    # ...and the difference really is only that word.
    assert manager != compliance
    assert "the memo used" in manager
    assert "the handoff used" in compliance


def test_the_source_tools_section_names_both_tools_and_the_envelope():
    for template in (load_manager_prompt(), load_compliance_prompt()):
        section = prompt_section(template.text, "SOURCE TOOLS")
        assert "`search_contract`" in section
        assert "`open_source_span`" in section
        assert '"tool_request"' in section
        assert "read-only" in section


def test_neither_role_prompt_hard_codes_a_source_access_state():
    """The prompt must not assert what the run allows; the block states it.

    A prompt that said "you have no access to the contract" in its own voice
    would be an A0 prompt, and the same file could not serve A1. The permission
    belongs to the GOVERNANCE block, which is rendered per run.
    """
    for template in (load_manager_prompt(), load_compliance_prompt()):
        lowered = template.text.lower()
        assert "governance block" in lowered
        for hard_coded in (
            "you do not have access to the source contract",
            "you may use the available source tools",
            "you must verify the relevant policy",
        ):
            assert hard_coded not in lowered, f"{template.ref} hard-codes a condition"


def test_the_prompts_declare_only_the_policy_placeholder():
    """The treatment is rendered into the message, not substituted into the file.

    If a prompt took a permission placeholder, the permission would be part of
    the prompt text rather than part of the block -- and a diff of the two would
    no longer localise the treatment.
    """
    for template in (load_manager_prompt(), load_compliance_prompt()):
        assert template.placeholders == ("policy",)
