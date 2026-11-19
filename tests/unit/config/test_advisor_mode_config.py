# -*- coding: utf-8 -*-
"""``AdvisorModeConfig`` defaults, persistence and legacy compatibility."""
from __future__ import annotations

import json

import pytest

from qwenpaw.config.config import (
    AdvisorModeConfig,
    AgentProfileConfig,
    ModelSlotConfig,
)


def test_defaults_are_off_with_followup_on():
    cfg = AdvisorModeConfig()
    assert cfg.enabled is False
    assert cfg.plan_enabled is True
    assert cfg.followup_enabled is True
    assert cfg.on_demand_enabled is True
    assert cfg.max_consults == 32
    assert cfg.advisor_model is None
    assert cfg.worker_model is None
    assert AgentProfileConfig(id="a", name="A").advisor_mode == cfg


def test_round_trips_through_json():
    cfg = AgentProfileConfig(id="a", name="A")
    cfg.advisor_mode.enabled = True
    cfg.advisor_mode.followup_enabled = False
    raw = json.loads(cfg.model_dump_json())
    assert raw["advisor_mode"] == {
        "enabled": True,
        "plan_enabled": True,
        "followup_enabled": False,
        "on_demand_enabled": True,
        "max_consults": 32,
        "recent_messages": 20,
        "intervention": {
            "consecutive_failures": 3,
            "window_size": 10,
            "window_failures": 4,
            "cooldown_steps": 0,
            "max_interventions": 3,
        },
        "privacy": {
            "enabled": False,
            "small_int_max": 10,
            "min_value_chars": 4,
            "shingle_words": 3,
            "format_min_records": 5,
            "reference_pattern": r"\bvar_[A-Za-z0-9_.:\-]+",
        },
        "advisor_model": None,
        "worker_model": None,
        "advisor_thinking": "off",
    }
    back = AgentProfileConfig.model_validate(raw)
    assert back.advisor_mode.enabled is True
    assert back.advisor_mode.followup_enabled is False


def test_privacy_is_off_by_default_and_round_trips():
    cfg = AgentProfileConfig(id="a", name="A")
    assert cfg.advisor_mode.privacy.enabled is False
    cfg.advisor_mode.privacy.enabled = True
    cfg.advisor_mode.privacy.small_int_max = 0
    back = AgentProfileConfig.model_validate(
        json.loads(cfg.model_dump_json()),
    )
    assert back.advisor_mode.privacy.enabled is True
    assert back.advisor_mode.privacy.small_int_max == 0


def test_legacy_advisor_section_without_privacy_loads_with_it_off():
    raw = json.loads(AgentProfileConfig(id="a", name="A").model_dump_json())
    del raw["advisor_mode"]["privacy"]
    back = AgentProfileConfig.model_validate(raw)
    assert back.advisor_mode.privacy.enabled is False


def test_model_overrides_round_trip():
    cfg = AgentProfileConfig(id="a", name="A")
    cfg.advisor_mode.advisor_model = ModelSlotConfig(
        provider_id="big",
        model="b-max",
    )
    raw = json.loads(cfg.model_dump_json())
    assert raw["advisor_mode"]["advisor_model"] == {
        "provider_id": "big",
        "model": "b-max",
    }
    assert raw["advisor_mode"]["worker_model"] is None
    back = AgentProfileConfig.model_validate(raw)
    assert back.advisor_mode.advisor_model.model == "b-max"
    assert back.advisor_mode.worker_model is None


def test_legacy_agent_json_without_the_section_loads_with_defaults():
    """Configs written before Advisor Mode existed must keep loading."""
    legacy = AgentProfileConfig(id="a", name="A").model_dump()
    legacy.pop("advisor_mode")
    cfg = AgentProfileConfig.model_validate(legacy)
    assert cfg.advisor_mode.enabled is False
    assert cfg.advisor_mode.followup_enabled is True


def test_max_consults_cannot_be_negative():
    with pytest.raises(ValueError):
        AdvisorModeConfig(max_consults=-1)


def test_intervention_thresholds_are_validated():
    from qwenpaw.config.config import AdvisorInterventionConfig

    assert AdvisorInterventionConfig().max_interventions == 3
    with pytest.raises(ValueError):
        AdvisorInterventionConfig(consecutive_failures=0)
    legacy = AgentProfileConfig(id="a", name="A").model_dump()
    legacy["advisor_mode"].pop("intervention")
    assert (
        AgentProfileConfig.model_validate(
            legacy,
        ).advisor_mode.intervention.window_size
        == 10
    )
