"""Versioned prompt files.

Experimental prompts are version-controlled artifacts, not string literals
buried in Python. They live in ``prompts/<id>_<version>.md`` so that a change to
wording is a reviewable diff, and so that the exact text used by a run can be
recovered from the version identifier recorded on every model call.

A template is a plain Markdown file with ``{{placeholder}}`` slots. Rendering is
strict in both directions: a template that declares a placeholder nobody
supplies fails, and a call that supplies a value the template does not declare
fails. Nothing is silently dropped.

The version identifier that reaches the raw-call log is ``<id>_<version>`` --
for example ``manager_v1`` -- and, for a repaired call,
``manager_v1+repair_v1``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "MANAGER_PROMPT_ID",
    "COMPLIANCE_PROMPT_ID",
    "REPAIR_PROMPT_ID",
    "V1",
    "V2",
    "V3",
    "CURRENT_VERSION",
    "PROMPTS_DIR_ENV",
    "PromptError",
    "PromptTemplate",
    "prompts_dir",
    "load_prompt",
    "load_manager_prompt",
    "load_compliance_prompt",
    "load_repair_prompt",
]

MANAGER_PROMPT_ID = "manager"
COMPLIANCE_PROMPT_ID = "compliance"
REPAIR_PROMPT_ID = "repair"
V1 = "v1"
V2 = "v2"
V3 = "v3"

CURRENT_VERSION = V3
"""The version the pipeline runs.

``v1`` asked for a single contract-level ``clause_status`` and offered no way to
say "not determined", so an agent that could not tell whether a target clause
existed had to choose between ``present`` and ``absent`` -- and the choice was
recorded as though it were an assessment. ``v2`` asks per target category, adds
``unknown``/``REVIEW`` as first-class answers, and separates evidence the agent
opened itself from evidence it inherited.

``v3`` keeps the v2 output shape and makes two procedures explicit: silence in
the handed-off material is not evidence of absence, and a V1 node searches for
both policy targets before answering. The v1 and v2 files are retained so a
recorded prompt version still identifies the text that produced it.
"""

PROMPTS_DIR_ENV = "PILOT01_PROMPT_DIR"

_PLACEHOLDER = re.compile(r"\{\{\s*([a-z_][a-z0-9_]*)\s*\}\}")


class PromptError(ValueError):
    """A prompt file is missing, malformed, or rendered with the wrong values."""


def prompts_dir() -> Path:
    """Locate the ``prompts/`` directory.

    Honours ``PILOT01_PROMPT_DIR``; otherwise resolves relative to the
    repository layout (``<root>/prompts``).
    """
    override = os.environ.get(PROMPTS_DIR_ENV)
    path = Path(override) if override else Path(__file__).resolve().parents[2] / "prompts"
    if not path.is_dir():
        raise FileNotFoundError(
            f"prompts directory not found at {path!r}; set {PROMPTS_DIR_ENV} to override"
        )
    return path


class PromptTemplate(BaseModel):
    """One versioned prompt file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt_id: str
    version: str
    path: Path
    text: str
    placeholders: tuple[str, ...] = Field(
        description="Every placeholder the file declares, in first-appearance order."
    )

    @property
    def ref(self) -> str:
        """The version identifier recorded on a model call, e.g. ``manager_v1``."""
        return f"{self.prompt_id}_{self.version}"

    def render(self, **values: str) -> str:
        """Substitute declared placeholders.

        Raises :class:`PromptError` if a declared placeholder is not supplied or
        an undeclared value is passed. Substitution is single-pass, so a
        ``{{...}}`` sequence inside a supplied value (a model's own previous
        response, for instance) is emitted verbatim rather than interpreted.
        """
        missing = [name for name in self.placeholders if name not in values]
        if missing:
            raise PromptError(
                f"prompt {self.ref!r} declares placeholder(s) {missing} that were "
                f"not supplied; supplied: {sorted(values)}"
            )
        unknown = sorted(set(values) - set(self.placeholders))
        if unknown:
            raise PromptError(
                f"prompt {self.ref!r} was given value(s) {unknown} it does not "
                f"declare; declared: {list(self.placeholders)}"
            )
        return _PLACEHOLDER.sub(lambda match: values[match.group(1)], self.text)


def load_prompt(
    prompt_id: str,
    version: str = CURRENT_VERSION,
    *,
    directory: Path | None = None,
) -> PromptTemplate:
    """Load and parse one prompt file."""
    path = (directory or prompts_dir()) / f"{prompt_id}_{version}.md"
    if not path.is_file():
        raise PromptError(f"prompt file not found: {path}")
    text = path.read_text(encoding="utf-8")

    seen: list[str] = []
    for match in _PLACEHOLDER.finditer(text):
        name = match.group(1)
        if name not in seen:
            seen.append(name)
    return PromptTemplate(
        prompt_id=prompt_id,
        version=version,
        path=path,
        text=text,
        placeholders=tuple(seen),
    )


def load_manager_prompt(version: str = CURRENT_VERSION) -> PromptTemplate:
    """The Manager prompt. A Gate-2 development prompt, not a frozen one."""
    return load_prompt(MANAGER_PROMPT_ID, version)


def load_compliance_prompt(version: str = CURRENT_VERSION) -> PromptTemplate:
    """The Compliance prompt. A Gate-2 development prompt, not a frozen one."""
    return load_prompt(COMPLIANCE_PROMPT_ID, version)


def load_repair_prompt(version: str = CURRENT_VERSION) -> PromptTemplate:
    """The format-repair prompt. Shared by both roles."""
    return load_prompt(REPAIR_PROMPT_ID, version)
