"""C-005 phase-based validation gates."""

from __future__ import annotations

from ai_company.config import load_config
from ai_company.services.validation import required_review_types


def test_mvp_phase_gate_thins_medium_to_test_security(tmp_env) -> None:
    from ai_company.config import clear_config_cache

    clear_config_cache()
    cfg = load_config()
    types = required_review_types("medium", cfg, phase_gate="mvp")
    assert types == ["test", "security"]


def test_mvp_phase_gate_does_not_expand_low(tmp_env) -> None:
    from ai_company.config import clear_config_cache

    clear_config_cache()
    cfg = load_config()
    types = required_review_types("low", cfg, phase_gate="mvp")
    assert types == ["test"]


def test_release_phase_uses_full_risk_profile(tmp_env) -> None:
    from ai_company.config import clear_config_cache

    clear_config_cache()
    cfg = load_config()
    types = required_review_types("high", cfg, phase_gate="release")
    assert "edge_case" in types
    assert "spec" in types


def test_critical_ignores_mvp_thinning(tmp_env) -> None:
    from ai_company.config import clear_config_cache

    clear_config_cache()
    cfg = load_config()
    types = required_review_types("critical", cfg, phase_gate="mvp")
    assert "architecture" in types
