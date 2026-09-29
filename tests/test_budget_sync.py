"""Budget ceiling synchronization + dynamic in-budget thresholds
(spec.md §6.6.3 dynamic thresholds, §6.7.2): the Concierge's programmatic
ceiling update reaches both the draft and the keyed `budget_ceiling_input`
widget without Streamlit's Session State API warning, real headroom under the
CURRENT ceiling is spent, and the in-budget advisory never claims "already
optimized" while a higher-tier compatible part fits."""
from __future__ import annotations

import logging

from streamlit.testing.v1 import AppTest

from engine import solvers
from llm import advisory
from tests.test_ui_smoke import (  # noqa: F401  (autouse fixtures + helpers)
    APP_PATH,
    _no_autoseed_on_boot,
    _no_live_llm_calls,
    _register,
    seeded_db,
)


def test_spend_headroom_upgrades_uses_room_and_respects_ceiling(seeded_db):
    build = solvers.initialize_budget_build(2030)
    cost = sum(c.price_usd for c in build.values())
    upgraded = solvers.spend_headroom_upgrades(build, 4000.0)
    assert sum(c.price_usd for c in upgraded.values()) <= 4000.0
    assert upgraded != build, "a build far below its ceiling must be upgradable"
    assert solvers.build_performance(upgraded) > solvers.build_performance(build)
    before, after = solvers.build_metrics(build), solvers.build_metrics(upgraded)
    assert after[0] == 0 and after[1] >= before[1] - 1e-6 and after[2] <= before[2] + 1e-6
    assert sum(c.price_usd for c in build.values()) == cost  # input not mutated
    assert solvers.spend_headroom_upgrades(build, cost) == build  # no headroom -> unchanged


def test_in_budget_advisory_offers_headroom_upgrades_not_already_optimized(seeded_db):
    from db.repositories import components_repo

    build = solvers.initialize_budget_build(2030)
    result = advisory.get_build_advisory(build, "Budget", 4000.0)
    swaps = result["within_budget"]["swaps"]
    assert swaps, result["within_budget"]["explanation"]
    assert "already optimal" not in result["within_budget"]["explanation"].lower()
    final = dict(build)
    for swap in swaps:
        final[swap["category"]] = components_repo.get_by_id(swap["replace_with_id"])
    assert sum(c.price_usd for c in final.values()) <= 4000.0
    assert solvers.build_performance(final) > solvers.build_performance(build)


def test_concierge_budget_raise_applies_upgrades_without_widget_warnings(seeded_db, monkeypatch, caplog):
    import ui.components.chat_assistant as chat_assistant_module
    from db.repositories import components_repo

    selection = solvers.initialize_budget_build(2030)
    before_total = sum(c.price_usd for c in selection.values())

    def _fake_response(user_message, conversation_history, catalog_summary, community_summary, **kwargs):
        return {
            "reply": "Using your new budget on the best upgrades that fit.",
            "action": {"type": "use_remaining_budget", "budget_cap_usd": 4000.0, "stated_total_budget": True},
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)

    at = AppTest.from_file(str(APP_PATH), default_timeout=60)
    with caplog.at_level(logging.WARNING):
        at.run()
        _register(at, "budgetup2", "budgetup2@example.com", "Budget Up Two")
        at.session_state["build_draft"] = {
            "name": "", "creation_mode": "Budget", "workload_profile": None, "tier": "Mid",
            "budget_ceiling": 2030.0,
            "components": {category: component.id for category, component in selection.items()},
            "quantities": {},
        }
        at.session_state["create_mode"] = "Budget"
        at.session_state["page"] = "create_build"
        at.run()
        at.get_by_key("concierge_chat_input").set_value(
            "im upping your budget to 4k usd. make all upgrades you can"
        ).run()
        at.run()

    assert not at.exception
    draft = at.session_state["build_draft"]
    assert draft["budget_ceiling"] == 4000.0
    assert at.get_by_key("budget_ceiling_input").value == 4000.0
    after_total = sum(components_repo.get_by_id(cid).price_usd for cid in draft["components"].values())
    assert before_total < after_total <= 4000.0
    assert not [
        r for r in caplog.records
        if "Session State API" in r.getMessage() or "default value" in r.getMessage()
    ]


def test_budget_widget_renders_without_value_conflict_warning(seeded_db, caplog):
    at = AppTest.from_file(str(APP_PATH), default_timeout=60)
    with caplog.at_level(logging.WARNING):
        at.run()
        _register(at, "budgetwarn1", "budgetwarn1@example.com", "Budget Warn One")
        at.session_state["build_draft"] = {
            "name": "", "creation_mode": "Budget", "workload_profile": None, "tier": "Mid",
            "budget_ceiling": 2500.0, "components": {}, "quantities": {},
        }
        at.session_state["create_mode"] = "Budget"
        at.session_state["page"] = "create_build"
        at.run()
    assert not at.exception
    assert at.get_by_key("budget_ceiling_input").value == 2500.0
    assert not [r for r in caplog.records if "Session State API" in r.getMessage()]
