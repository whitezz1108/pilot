"""Material preparation settings, deliberately independent of ExperimentProfile."""
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict

from ...model.client import PreparationModelParams


class PreparationConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    preparation_version: Literal["1"]
    model: PreparationModelParams


def load_preparation_config(path: Path | None = None) -> PreparationConfig:
    resolved = path or Path(__file__).resolve().parents[4] / "config/material_preparation_v1.yaml"
    return PreparationConfig.model_validate(yaml.safe_load(resolved.read_text(encoding="utf-8")))
