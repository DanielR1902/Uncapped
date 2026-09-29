"""(1) The Concierge publish flow, resolved deterministically in Python
(spec.md §6.7.5): "yes" moves to the tag question (never repeats the first
question), a tag may be declined ("neither"/"no tag"/"skip" -> untagged), and
the description step publishes with an empty description on "no", a GENERATED
one on "write a random description", or the user's own typed text.
(2) The In-Budget / Stretch button states and red messages are identical in
every build mode."""
from __future__ import annotations

import pytest
from streamlit.testing.v1 import AppTest

from db.repositories import community_repo, components_repo
from engine import solvers
from llm import concierge
from tests.test_ui_smoke import (  # noqa: F401  (autouse fixtures + helpers)
    APP_PATH,
    _no_autoseed_on_boot,
    _no_live_llm_calls,
    _register,
    seeded_db,
)

DESCRIPTION_QUESTION = (
    "Tagged as 'Rate My Build'! Would you like to add a description or notes? "
    "(Type your description, or type 'no' to publish without one)"
)
HISTORY = [{"role": "assistant", "content": DESCRIPTION_QUESTION}]
PUBLISH_QUESTION = "Saved as 'X'! Would you like to publish it to the Community as well?"
TAG_QUESTION = "Would you like to tag this community post as 'Rate My Build' or 'Looking for Help'?"


# ------------------------------------------------------- description step
@pytest.mark.parametrize("answer", ["no", "No", "no thanks", "none", "skip", "nope"])
def test_decline_publishes_without_description(answer):
    result = concierge.resolve_publish_description_turn(answer, HISTORY)
    assert result["action"] == {"type": "publish_build", "author_notes": None, "flair": "Rate My Build"}


def test_other_text_becomes_the_description():
    result = concierge.resolve_publish_description_turn("Silent 1440p rig for editing", HISTORY)
    assert result["action"]["author_notes"] == "Silent 1440p rig for editing"
    result = concierge.resolve_publish_description_turn("A high-end 4K editing rig", HISTORY)
    assert result["action"]["author_notes"] == "A high-end 4K editing rig"
    # a description that merely STARTS with "no" is still a description
    result = concierge.resolve_publish_description_turn("no RGB, whisper quiet, 1440p", HISTORY)
    assert result["action"]["author_notes"] == "no RGB, whisper quiet, 1440p"


def test_cancel_keeps_private():
    assert concierge.resolve_publish_description_turn("cancel", HISTORY)["action"] is None


@pytest.mark.parametrize(
    "ask",
    ["write a random description", "generate one", "write one for me", "auto", "you write it", "surprise me"],
)
def test_ai_write_request_generates_and_never_publishes_the_prompt(monkeypatch, ask):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)  # deterministic fallback path
    context = {"components": {"CPU": {"name": "AMD Ryzen 7 5800X3D"}, "GPU": {"name": "NVIDIA RTX 4070"}}}
    result = concierge.resolve_publish_flow_turn(ask, HISTORY, context)
    notes = result["action"]["author_notes"]
    assert notes and notes != ask
    assert "Ryzen 7 5800X3D" in notes and "RTX 4070" in notes
    assert result["action"]["flair"] == "Rate My Build"


def test_ai_write_request_uses_llm_text_when_available(monkeypatch):
    from tests.test_advisory import _fake_openrouter_response

    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("OPENROUTER_MODEL", "m")
    monkeypatch.setattr(
        concierge.httpx, "post",
        lambda *a, **k: _fake_openrouter_response({"description": "A quiet 1440p editing rig. Costs $1,000."}),
    )
    result = concierge.resolve_publish_flow_turn("write a random description", HISTORY, {"components": {}})
    assert result["action"]["author_notes"] == "A quiet 1440p editing rig. Costs USD 1,000."


def test_only_applies_right_after_a_publish_flow_question():
    other = [{"role": "assistant", "content": "Here are some GPUs."}]
    assert concierge.resolve_publish_flow_turn("no", other) is None
    assert concierge.resolve_publish_flow_turn("no", []) is None
    assert concierge.resolve_publish_description_turn("no", [{"role": "assistant", "content": PUBLISH_QUESTION}]) is None


# ------------------------------------------------------------ earlier steps
def test_yes_to_publish_moves_to_tag_question_and_never_repeats_the_first_question():
    result = concierge.resolve_publish_flow_turn("yes", [{"role": "assistant", "content": PUBLISH_QUESTION}])
    assert result["action"] is None
    assert "'Rate My Build' or 'Looking for Help'" in result["reply"]
    assert "publish it to the Community as well" not in result["reply"]
    declined = concierge.resolve_publish_flow_turn("no", [{"role": "assistant", "content": PUBLISH_QUESTION}])
    assert declined["action"] is None and "stays private" in declined["reply"]


@pytest.mark.parametrize("answer", ["neither", "no tag", "none", "skip", "no"])
def test_declining_a_tag_does_not_force_one(answer):
    result = concierge.resolve_publish_flow_turn(answer, [{"role": "assistant", "content": TAG_QUESTION}])
    assert result["action"] is None
    assert result["reply"].startswith("No tag selected.")
    assert "add a description or notes" in result["reply"]
    assert "Looking for Help'!" not in result["reply"]


def test_choosing_a_tag_moves_to_description_question():
    for answer, tag in (("Rate My Build", "Rate My Build"), ("looking for help please", "Looking for Help")):
        result = concierge.resolve_publish_flow_turn(answer, [{"role": "assistant", "content": TAG_QUESTION}])
        assert result["reply"].startswith(f"Tagged as '{tag}'!")


def test_untagged_description_step_publishes_with_no_flair():
    history = [{"role": "assistant", "content": "No tag selected. Would you like to add a description or notes? (x)"}]
    result = concierge.resolve_publish_flow_turn("no", history)
    assert result["action"] == {"type": "publish_build", "author_notes": None, "flair": None}


# ------------------------------------------------------------- end to end
def _run_flow(monkeypatch, name, tag_answer, description_answer):
    import ui.components.chat_assistant as chat_assistant_module

    real_response = chat_assistant_module.get_concierge_response

    at = AppTest.from_file(str(APP_PATH), default_timeout=60)
    at.run()
    _register(at, name, f"{name}@example.com", name.title())
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("apply_budget_generate").click().run()

    def _fake_save(user_message, conversation_history, catalog_summary, community_summary, **kwargs):
        return {
            "reply": "Saved as 'the promised neverland'! Would you like to publish it to the Community as well?",
            "action": {
                "type": "save_build", "name": "the promised neverland", "destination": "build",
                "explanation": "x",
            },
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_save)
    at.get_by_key("concierge_chat_input").set_value("save this as a final build named the promised neverland").run()
    assert not at.exception and community_repo.get_feed() == []

    # every later turn goes through the REAL entry point with HTTP forbidden:
    # the flow must be resolved without ever consulting a model.
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    def _no_http(*args, **kwargs):
        raise AssertionError("publish flow must not call the LLM")

    monkeypatch.setattr(concierge.httpx, "post", _no_http)
    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", real_response)

    def say(text):
        at.get_by_key("concierge_chat_input").set_value(text).run()
        assert not at.exception
        return at.session_state["concierge_messages"][-1]["content"]

    reply = say("yes")
    assert "'Rate My Build' or 'Looking for Help'" in reply
    assert "publish it to the Community as well" not in reply  # no repeat of the first question
    assert community_repo.get_feed() == []
    reply = say(tag_answer)
    assert "add a description or notes" in reply and community_repo.get_feed() == []
    return at, say(description_answer)


def test_full_flow_no_description_publishes_and_does_not_loop(seeded_db, monkeypatch):
    at, reply = _run_flow(monkeypatch, "pubflow1", "Rate My Build", "no")
    feed = community_repo.get_feed()
    assert len(feed) == 1
    assert feed[0].title == "the promised neverland"
    assert feed[0].flair == "Rate My Build"
    assert not feed[0].author_notes
    assert "successfully published to Community" in reply and "the promised neverland" in reply
    assert "Would you like to publish it to the Community as well?" not in reply


def test_full_flow_no_tag_publishes_untagged(seeded_db, monkeypatch):
    at, reply = _run_flow(monkeypatch, "pubflow2", "neither", "skip")
    feed = community_repo.get_feed()
    assert len(feed) == 1 and feed[0].flair is None and not feed[0].author_notes
    assert "successfully published to Community (no tag)" in reply


def test_full_flow_generated_description_is_published_not_the_prompt(seeded_db, monkeypatch):
    at, reply = _run_flow(monkeypatch, "pubflow3", "Looking for Help", "write a random description")
    feed = community_repo.get_feed()
    assert len(feed) == 1 and feed[0].flair == "Looking for Help"
    assert feed[0].author_notes and feed[0].author_notes != "write a random description"
    assert "successfully published to Community under 'Looking for Help'" in reply


def test_full_flow_typed_description_is_used_verbatim(seeded_db, monkeypatch):
    at, reply = _run_flow(monkeypatch, "pubflow4", "Rate My Build", "A high-end 4K editing rig")
    assert community_repo.get_feed()[0].author_notes == "A high-end 4K editing rig"


# ------------------------------------------------- button states in every mode
def _advisory(state, swap_id, stretch_id):
    def _fake(build_state, mode, current_budget_or_cost, profile=None, bottleneck_info=None, quantities=None):
        swaps = [{"action": "swap", "category": "CPU", "replace_with_id": swap_id}] if state == "A" else []
        actions = [{"action": "swap", "category": "CPU", "replace_with_id": stretch_id}] if state in ("A", "B") else []
        return {
            "pros": ["p"], "cons": ["c"],
            "within_budget": {"explanation": "wb", "swaps": swaps, "can_optimize_further": False},
            "stretch_budget": {"explanation": "sb", "actions": actions, "added_cost_usd": 10.0 if actions else 0.0},
            "source": "heuristic",
        }

    return _fake


def _boot_mode(mode, name, monkeypatch):
    import ui.components.chat_assistant as chat_assistant_module

    selection = solvers.initialize_budget_build(1800.0)
    components = {c: comp.id for c, comp in selection.items()}
    at = AppTest.from_file(str(APP_PATH), default_timeout=60)
    at.run()
    _register(at, name, f"{name}@example.com", name.title())
    if mode == "AI":
        # a draft created through the Concierge (`load_build`), not a Studio mode button
        def _fake(user_message, conversation_history, catalog_summary, community_summary, **kwargs):
            return {
                "reply": "Built.", "source": "heuristic",
                "action": {"type": "load_build", "components": components, "explanation": "x"},
            }

        monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake)
        at.get_by_key("concierge_chat_input").set_value("build me a gaming pc").run()
    else:
        at.session_state["build_draft"] = {
            "name": "", "creation_mode": mode, "workload_profile": "Gaming" if mode == "Workload" else None,
            "tier": "Mid", "budget_ceiling": 1800.0 if mode == "Budget" else None,
            "components": dict(components), "quantities": {},
        }
        at.session_state["create_mode"] = mode
        at.session_state["page"] = "create_build"
        at.run()
    return at, components


@pytest.mark.parametrize("mode", ["Budget", "Workload", "Free", "AI"])
@pytest.mark.parametrize("state", ["A", "B", "C"])
def test_button_states_identical_in_every_mode(seeded_db, monkeypatch, mode, state):
    import ui.views.create_build as create_build_module

    at, components = _boot_mode(mode, f"btnmode{mode.lower()}{state.lower()}", monkeypatch)
    cpu_id = components["CPU"]
    others = [c.id for c in components_repo.get_by_category("CPU") if c.id != cpu_id][:2]
    monkeypatch.setattr(create_build_module, "get_build_advisory", _advisory(state, others[0], others[1]))
    at.get_by_key("hud_ai_analysis").click().run()
    assert not at.exception

    in_budget, stretch = at.get_by_key("btn_apply_in_budget"), at.get_by_key("btn_apply_stretch")
    text = "\n".join(m.value for m in at.markdown)
    cannot = "Cannot apply stretch upgrades while in-budget optimizations are still available."
    optimized = "The current configuration is already fully optimized for this budget. Consider stretching the budget."
    no_gain = "Increasing budget will not yield further performance or synergy improvements with available parts."

    if state == "A":  # in-budget swaps available
        assert in_budget.disabled is False and stretch.disabled is True
        assert cannot in text and optimized not in text and no_gain not in text
    elif state == "B":  # in-budget depleted, stretch available
        assert in_budget.disabled is True and stretch.disabled is False
        assert optimized in text and cannot not in text and no_gain not in text
    else:  # both depleted
        assert in_budget.disabled is True and stretch.disabled is True
        assert optimized in text and no_gain in text and cannot not in text
