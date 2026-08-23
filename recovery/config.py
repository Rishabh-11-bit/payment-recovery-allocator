"""Config loading. Every limit lives in YAML; nothing here carries a default value.

A missing key is an error, not a silent fallback. If a threshold can be absent
without anyone noticing, it is not really config -- and a panel asking "where
does the attempt cap come from" should get one answer, not two.
"""

from __future__ import annotations

import pathlib
from datetime import time
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

DEFAULT_CONFIG_PATH = pathlib.Path("config/default.yaml")


class _Strict(BaseModel):
    # extra="forbid" so a typo'd key fails loudly instead of silently doing nothing.
    model_config = ConfigDict(extra="forbid", frozen=True)


class PolicyConfig(_Strict):
    version: str


class DatabaseConfig(_Strict):
    path: str


class IngestConfig(_Strict):
    ack_budget_seconds: Annotated[float, Field(gt=0)]
    accepted_events: tuple[str, ...]


class WorkerConfig(_Strict):
    batch_size: Annotated[int, Field(gt=0)]
    claim_timeout_seconds: Annotated[float, Field(gt=0)]
    max_attempts_per_job: Annotated[int, Field(gt=0)]


class LateAuthConfig(_Strict):
    poll_window_days: Annotated[int, Field(ge=0)]
    require_state_refresh: bool


class InfrastructureConfig(_Strict):
    high_offset_days: Annotated[int, Field(ge=1)]
    moderate_offset_days: Annotated[int, Field(ge=1)]


class LiquidityConfig(_Strict):
    min_offset_days: Annotated[int, Field(ge=1)]
    funding_days_of_month: tuple[Annotated[int, Field(ge=1, le=31)], ...]
    max_wait_days: Annotated[int, Field(ge=1)]


class ContactConfig(_Strict):
    default_channel: str
    attention_channel: str
    max_per_case: Annotated[int, Field(ge=0)]


class AllocatorConfig(_Strict):
    """Ordinal knobs only. A probability here would be a bug, not a setting."""

    version: str
    infrastructure: InfrastructureConfig
    liquidity: LiquidityConfig
    contact: ContactConfig

    @field_validator("liquidity")
    @classmethod
    def _later_is_later(cls, value: LiquidityConfig) -> LiquidityConfig:
        if value.max_wait_days < value.min_offset_days:
            raise ValueError("liquidity.max_wait_days is before min_offset_days")
        return value


class RegulatoryConfig(_Strict):
    """Consumed by C4. Recorded from day one so the NPCI constants live in config."""

    attempt_cap: Annotated[int, Field(gt=0)]
    pdn_lead_time_hours: Annotated[int, Field(ge=0)]
    pdn_cutoff_ist: time
    afa_threshold_inr: Annotated[int, Field(gt=0)]
    peak_windows_ist: tuple[tuple[time, time], ...]

    @field_validator("peak_windows_ist")
    @classmethod
    def _ordered(cls, windows: tuple[tuple[time, time], ...]) -> tuple[tuple[time, time], ...]:
        for start, end in windows:
            if start >= end:
                raise ValueError(f"peak window {start}-{end} does not advance")
        return windows


class Config(_Strict):
    policy: PolicyConfig
    allocator: AllocatorConfig
    database: DatabaseConfig
    ingest: IngestConfig
    worker: WorkerConfig
    late_auth: LateAuthConfig
    regulatory: RegulatoryConfig


def load_config(path: pathlib.Path | str = DEFAULT_CONFIG_PATH) -> Config:
    path = pathlib.Path(path)
    if not path.exists():
        raise FileNotFoundError(f"config not found: {path}")
    with path.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    if not isinstance(raw, dict):
        raise ValueError(f"config at {path} is not a mapping")
    return Config.model_validate(raw)
