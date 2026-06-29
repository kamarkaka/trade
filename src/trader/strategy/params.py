"""Per-strategy parameter models (design §6/§11).

Optional pydantic models that validate a binding's ``params:`` at config-load time (via the
binding loader), so a bad/typo'd strategy param fails fast and loudly instead of at the first
cycle. A strategy without a model has its params passed through unchanged (backward compatible).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ZScoreRevertParams(BaseModel):
    """Validated params for the zscore_revert strategy."""

    model_config = ConfigDict(frozen=True, extra="forbid")  # typo'd keys are rejected

    lookback: int = Field(default=20, ge=2)
    z_entry: float = Field(default=2.0, gt=0)
    z_exit: float = Field(default=0.5, ge=0)
    lot: int = Field(default=10, gt=0)


# name -> param model. Strategies absent from this map accept params unvalidated.
PARAM_MODELS: dict[str, type[BaseModel]] = {
    "zscore_revert": ZScoreRevertParams,
}


def validate_params(name: str, params: dict[str, Any]) -> dict[str, Any]:
    """Validate + normalize a strategy's params against its model (defaults filled). Strategies
    with no model pass through unchanged. Raises ValueError on invalid params."""
    model = PARAM_MODELS.get(name)
    if model is None:
        return dict(params)
    try:
        return model(**params).model_dump()
    except ValidationError as exc:
        raise ValueError(f"invalid params for strategy {name!r}: {exc}") from exc


__all__ = ["PARAM_MODELS", "ZScoreRevertParams", "validate_params"]
