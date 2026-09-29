"""Pure-additive Concierge mutations + Python-computed diff line
(spec.md §6.7 intent 4 / §7.8). Unit tests for the deterministic helpers in
ui/components/chat_assistant.py plus AppTest end-to-end checks that "add a
monitor" preserves every existing part and yields total = previous + price."""
from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from db import database
from db.repositories import components_repo
from db.seed import run_seed
from engine import solvers
from ui import state
from ui.components import chat_assistant as ca

APP_PATH = Path(__file__).resolve().parent.parent / "app.py"


@pytest.fixture(autouse=True)
def _isolation(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    import db.seed_mass_content as seed_mass_content_module

    monkeypatch.setattr(seed_mass_content_module, "seed_if_empty", lambda: None)


@pytest.fixture()
def seeded_db(tmp_path):
    database.configure(f"sqlite:///{tmp_path / 'additive.db'}")
    database.init_db()
    run_seed()
    yield
    database.get_engine().dispose()


def _register(at: AppTest, username: str) -> None:
    at.get_by_key("open_register").click().run()
    at.get_by_key("register_username").input(username)
    at.get_by_key("register_email").input(f"{username}@example.com")
    at.get_by_key("register_full_name").input(username.title())
    at.get_by_key("register_password").input("Passw0rd!")
    at.get_by_key("register_submit").click()
    at.run()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "message,expected",
    [
        ("add a monitor", True),
        ("Please add a keyboard and a mouse", True),
        ("add another 2TB drive", True),
        ("add a monitor under 300 dollars", True),
        ("remove the monitor", False),
        ("replace my monitor", False),
        ("build me a new PC with a monitor", False),
        ("upgrade my GPU", False),
        ("what monitors do you have?", False),
    ],
)
def test_is_additive_request(message, expected):
    assert ca._is_additive_request(message) is expected


def test_enforce_rewrites_load_build_into_peripheral_only_patch():
    draft = {"components": {"CPU": 1, "GPU": 2}, "quantities": {}}
    action = {
        "type": "load_build",
        "components": {"CPU": 9, "GPU": 8, "Monitor": 156},
        "budget_cap_usd": 1500.0,
        "explanation": "x",
    }
    patched, strict, note = ca._enforce_additive_action(action, "add a monitor", draft)
    assert strict is True
    assert patched["type"] == "modify_build"
    assert patched["components"] == {"Monitor": 156}
    assert "budget_cap_usd" not in patched
    assert note


def test_enforce_leaves_non_additive_and_empty_build_alone():
    action = {"type": "load_build", "components": {"CPU": 9}, "budget_cap_usd": 1500.0}
    assert ca._enforce_additive_action(action, "build me a gaming pc for 1500", {"components": {"CPU": 1}}) == (
        action, False, None,
    )
    assert ca._enforce_additive_action(action, "add a monitor", {"components": {}}) == (action, False, None)


def test_describe_changes_reports_added_replaced_removed_and_quantity(seeded_db):
    monitor = components_repo.get_by_category("Monitor")[0]
    gpus = components_repo.get_by_category("GPU")[:2]
    mouse = components_repo.get_by_category("Mouse")[0]
    text = ca._describe_changes(
        {"GPU": gpus[0].id, "Mouse": mouse.id, "Storage": 5},
        {"Storage": 1},
        {"GPU": gpus[1].id, "Monitor": monitor.id, "Storage": 5},
        {"Storage": 2},
    )
    assert f"added Monitor: {monitor.name}" in text
    assert f"replaced GPU: {gpus[0].name} -> {gpus[1].name}" in text
    assert f"removed Mouse: {mouse.name}" in text
    assert "Storage quantity 1 -> 2" in text
    assert ca._describe_changes({"GPU": 1}, {}, {"GPU": 1}, {}) == "no component changes"


# ---------------------------------------------------------------------------
# AppTest end to end
# ---------------------------------------------------------------------------
def _boot(seeded, name, mode="Free", ceiling=None, headroom=0.0):
    selection = solvers.initialize_budget_build(1800.0)
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, name)
    components = {category: component.id for category, component in selection.items()}
    prev_total = sum(c.price_usd for c in selection.values())
    at.session_state["build_draft"] = {
        "name": "",
        "creation_mode": mode,
        "workload_profile": None,
        "tier": "Mid",
        "budget_ceiling": (prev_total + headroom) if ceiling else None,
        "components": dict(components),
        "quantities": {},
    }
    at.session_state["page"] = "create_build"
    return at, components, prev_total


def _patch_llm(monkeypatch, action):
    def _fake(user_message, conversation_history, catalog_summary, community_summary, **kwargs):
        return {"reply": "No components were downgraded.", "action": action, "source": "llm"}

    monkeypatch.setattr(ca, "get_concierge_response", _fake)


def _quantity_aware_total(draft):
    build_state = state.resolve_build_state(draft)
    return state.build_total_cost(build_state, draft.get("quantities", {}))


def test_add_monitor_as_load_build_with_budget_cap_is_applied_as_pure_patch(seeded_db, monkeypatch):
    at, before, prev_total = _boot(seeded_db, "addmon1", mode="Budget", ceiling=True, headroom=5000.0)
    monitor = components_repo.get_by_category("Monitor")[0]
    _patch_llm(
        monkeypatch,
        {
            "type": "load_build",
            "components": {"Monitor": monitor.id},
            "budget_cap_usd": prev_total,  # the exact regression: a budget re-solve
            "explanation": "x",
        },
    )

    at.get_by_key("concierge_chat_input").set_value("add a monitor").run()

    assert not at.exception
    draft = at.session_state["build_draft"]
    for category, component_id in before.items():
        assert draft["components"][category] == component_id
    assert draft["components"]["Monitor"] == monitor.id
    assert set(draft["components"]) == set(before) | {"Monitor"}
    assert _quantity_aware_total(draft) == pytest.approx(prev_total + monitor.price_usd)
    reply = at.session_state["concierge_messages"][-1]["content"]
    assert f"added Monitor: {monitor.name}" in reply
    assert "replaced" not in reply and "removed" not in reply


def test_add_monitor_modify_build_with_extra_core_swaps_drops_the_core_changes(seeded_db, monkeypatch):
    at, before, prev_total = _boot(seeded_db, "addmon2")
    monitor = components_repo.get_by_category("Monitor")[0]
    cheaper_gpu = min(components_repo.get_by_category("GPU"), key=lambda c: c.price_usd)
    _patch_llm(
        monkeypatch,
        {
            "type": "modify_build",
            "components": {"Monitor": monitor.id, "GPU": cheaper_gpu.id},
            "quantities": {},
            "remove_categories": ["Cooler"],
            "explanation": "x",
        },
    )

    at.get_by_key("concierge_chat_input").set_value("add a monitor").run()

    assert not at.exception
    draft = at.session_state["build_draft"]
    for category, component_id in before.items():
        assert draft["components"][category] == component_id
    assert draft["components"]["Monitor"] == monitor.id
    assert _quantity_aware_total(draft) == pytest.approx(prev_total + monitor.price_usd)


def test_additive_over_ceiling_is_rolled_back_until_user_confirms(seeded_db, monkeypatch):
    at, before, prev_total = _boot(seeded_db, "addmon3", mode="Budget", ceiling=True, headroom=0.0)
    monitor = components_repo.get_by_category("Monitor")[0]
    _patch_llm(
        monkeypatch,
        {"type": "modify_build", "components": {"Monitor": monitor.id}, "quantities": {}, "explanation": "x"},
    )

    at.get_by_key("concierge_chat_input").set_value("add a monitor").run()
    draft = at.session_state["build_draft"]
    assert "Monitor" not in draft["components"]
    assert draft["components"] == before
    assert "exceed your budget" in at.session_state["concierge_messages"][-1]["content"]

    # The guardrail's own question, then a plain "yes": now it applies, still
    # without touching any other part.
    at.session_state["concierge_messages"] = at.session_state["concierge_messages"] + [
        {"role": "assistant", "content": "This upgrade will exceed your budget by USD 10.00. Would you like to proceed anyway?"},
    ]
    at.get_by_key("concierge_chat_input").set_value("yes").run()
    draft = at.session_state["build_draft"]
    assert draft["components"]["Monitor"] == monitor.id
    for category, component_id in before.items():
        assert draft["components"][category] == component_id


def test_replace_reply_includes_python_changes_line_and_cost(seeded_db, monkeypatch):
    at, before, prev_total = _boot(seeded_db, "diffline")
    gpus = sorted(components_repo.get_by_category("GPU"), key=lambda c: c.price_usd)
    old_gpu = components_repo.get_by_id(before["GPU"])
    new_gpu = next(g for g in gpus if g.id != old_gpu.id)
    _patch_llm(
        monkeypatch,
        {"type": "modify_build", "components": {"GPU": new_gpu.id}, "quantities": {}, "explanation": "x"},
    )

    at.get_by_key("concierge_chat_input").set_value("swap my GPU please").run()

    reply = at.session_state["concierge_messages"][-1]["content"]
    assert f"Changes: replaced GPU: {old_gpu.name} -> {new_gpu.name}" in reply
    assert "(cost " in reply
