"""Release / delivery policy profiles (strict | balanced | prototype)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ai_company.config import AppConfig, JudgeSection, RepairLoopSection


@dataclass(frozen=True)
class PolicyProfile:
    name: str
    delivery_mode: str
    auto_fire_stubborn_supervisor: bool
    description: str


PROFILES: dict[str, PolicyProfile] = {
    "strict": PolicyProfile(
        name="strict",
        delivery_mode="strict",
        auto_fire_stubborn_supervisor=False,
        description="All required review FAILs block; no auto-fire",
    ),
    "balanced": PolicyProfile(
        name="balanced",
        delivery_mode="strict",
        auto_fire_stubborn_supervisor=False,
        description="Strict gates with optional later soft residual tracking",
    ),
    "prototype": PolicyProfile(
        name="prototype",
        delivery_mode="ship_first",
        auto_fire_stubborn_supervisor=True,
        description="Ship-first: non-security FAILs soft-pass; may fire stubborn supervisors",
    ),
}


def resolve_policy_name(config: AppConfig) -> str:
    raw = (config.judge.release_policy or config.judge.delivery_mode or "strict").strip().lower()
    if raw in {"ship_first", "prototype", "demo"}:
        return "prototype"
    if raw in PROFILES:
        return raw
    return "strict"


def apply_policy_overrides(
    judge: JudgeSection,
    repair: RepairLoopSection,
    *,
    policy: str | None = None,
    delivery_mode: str | None = None,
) -> tuple[JudgeSection, RepairLoopSection]:
    """Return copies with CLI/profile overrides applied (does not mutate YAML on disk)."""
    name = (policy or "").strip().lower() or None
    mode = (delivery_mode or "").strip().lower() or None
    if name in {"ship_first", "demo"}:
        name = "prototype"
    if mode in {"ship_first", "prototype"} and not name:
        name = "prototype"
    if mode == "strict" and not name:
        name = "strict"

    profile = PROFILES.get(name or resolve_policy_name_from_sections(judge))
    if profile is None:
        profile = PROFILES["strict"]

    j = judge.model_copy(
        update={
            "delivery_mode": mode or profile.delivery_mode,
            "release_policy": profile.name,
        }
    )
    r = repair.model_copy(
        update={
            "auto_fire_stubborn_supervisor": profile.auto_fire_stubborn_supervisor
            if name
            else repair.auto_fire_stubborn_supervisor,
        }
    )
    if mode == "strict":
        j.delivery_mode = "strict"
        r.auto_fire_stubborn_supervisor = False
        j.release_policy = "strict"
    elif mode in {"ship_first", "prototype"}:
        j.delivery_mode = "ship_first"
        j.release_policy = "prototype"
        if name == "prototype":
            r.auto_fire_stubborn_supervisor = True
    return j, r


def resolve_policy_name_from_sections(judge: JudgeSection) -> str:
    raw = (judge.release_policy or judge.delivery_mode or "strict").strip().lower()
    if raw in {"ship_first", "prototype", "demo"}:
        return "prototype"
    if raw in PROFILES:
        return raw
    return "strict"


def policy_snapshot(config: AppConfig) -> dict[str, Any]:
    name = resolve_policy_name(config)
    profile = PROFILES[name]
    return {
        "policy": name,
        "delivery_mode": config.judge.delivery_mode,
        "auto_fire_stubborn_supervisor": config.repair_loop.auto_fire_stubborn_supervisor,
        "description": profile.description,
    }
