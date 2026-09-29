"""Explicit budget changes in chat (spec.md §6.7.4): lowering rebalances DOWN
to fit, raising rebalances UP into the headroom, and the change is reported
truthfully — never "no component changes" while the build violates the ceiling."""
from __future__ import annotations

import pytest
from streamlit.testing.v1 import AppTest

from db.repositories import components_repo
from engine import solvers
from tests.test_ui_smoke import (  # noqa: F401  (autouse fixtures + helpers)
    APP_PATH,
    _no_autoseed_on_boot,
    _no_live_llm_calls,
    _register,
    seeded_db,
)
from ui import state


def _total(draft):
    return state.build_total_cost(state.resolve_build_state(draft), draft.get("quantities", {}))


def _boot(name, build_budget, stale_ceiling, monkeypatch, action):
    import ui.components.chat_assistant as chat_assistant_module

    selection = solvers.initialize_budget_build(build_budget, fill_peripherals_with_surplus=True)

    def _fake(user_message, conversation_history, catalog_summary, community_summary, **kwargs):
        return {"reply": "Done.", "action": action, "source": "llm"}

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake)
    at = AppTest.from_file(str(APP_PATH), default_timeout=90)
    at.run()
    _register(at, name, f"{name}@example.com", name.title())
    at.session_state["build_draft"] = {
        "name": "", "creation_mode": "Budget", "workload_profile": None, "tier": "Mid",
        "budget_ceiling": stale_ceiling,
        "components": {c: comp.id for c, comp in selection.items()},
        "quantities": {},
    }
    at.session_state["create_mode"] = "Budget"
    at.session_state["page"] = "create_build"
    at.run()
    return at


def test_fit_build_to_ceiling_engine(seeded_db):
    build = solvers.initialize_budget_build(4000, fill_peripherals_with_surplus=True)
    assert sum(c.price_usd for c in build.values()) > 3000
    fitted, quantities = solvers.fit_build_to_ceiling(build, 3000.0)
    assert sum(c.price_usd * quantities.get(cat, 1) for cat, c in fitted.items()) <= 3000.0
    assert solvers.build_metrics(fitted, quantities)[0] == 0  # still compatible
    assert fitted != build


@pytest.mark.parametrize("routed_action", [None, {"type": "use_remaining_budget"}])
def test_lowering_budget_rebalances_build_to_fit(seeded_db, monkeypatch, routed_action):
    at = _boot("lowerbudget1", 4000, 4000.0, monkeypatch, routed_action)
    before = dict(at.session_state["build_draft"]["components"])
    assert _total(at.session_state["build_draft"]) > 3000

    at.get_by_key("concierge_chat_input").set_value(
        "im lowering your budget to 3k. make the necessary adjustments"
    ).run()

    assert not at.exception
    draft = at.session_state["build_draft"]
    assert draft["budget_ceiling"] == 3000.0
    assert at.get_by_key("budget_ceiling_input").value == 3000.0
    assert _total(draft) <= 3000.0
    assert draft["components"] != before
    reply = at.session_state["concierge_messages"][-1]["content"]
    assert "Changes:" in reply and "no component changes" not in reply
    assert "replaced" in reply or "removed" in reply
    assert "Still over budget" not in reply


def test_raising_budget_upgrades_into_headroom(seeded_db, monkeypatch):
    at = _boot("raisebudget1", 2030, 2030.0, monkeypatch, None)
    before_total = _total(at.session_state["build_draft"])

    at.get_by_key("concierge_chat_input").set_value("upping budget to 5k. make all possible upgrades").run()

    assert not at.exception
    draft = at.session_state["build_draft"]
    assert draft["budget_ceiling"] == 5000.0
    assert at.get_by_key("budget_ceiling_input").value == 5000.0
    assert before_total < _total(draft) <= 5000.0
    reply = at.session_state["concierge_messages"][-1]["content"]
    assert "Changes:" in reply and "replaced" in reply


def test_additive_peripheral_over_budget_is_still_refused_untouched(seeded_db, monkeypatch):
    """The budget-change backstop must not swallow a pure addition: an
    over-ceiling accessory is refused and every existing part is untouched."""
    monitor = max(components_repo.get_by_category("Monitor"), key=lambda c: c.price_usd)
    selection = solvers.initialize_budget_build(1800.0)
    at = _boot("addperiph1", 1800, sum(c.price_usd for c in selection.values()), monkeypatch,
               {"type": "modify_build", "components": {"Monitor": monitor.id}, "quantities": {}})
    at.session_state["build_draft"]["components"] = {c: comp.id for c, comp in selection.items()}
    at.session_state["build_draft"]["budget_ceiling"] = sum(c.price_usd for c in selection.values())
    before = dict(at.session_state["build_draft"]["components"])

    at.get_by_key("concierge_chat_input").set_value("add a monitor").run()

    draft = at.session_state["build_draft"]
    assert draft["components"] == before
    reply = at.session_state["concierge_messages"][-1]["content"]
    assert "exceeds your budget ceiling" in reply and "Please raise the budget first" in reply
