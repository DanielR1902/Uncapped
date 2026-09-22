"""Session-state key definitions/defaults, and the build_draft <-> BuildState
bridge (spec.md §7.1). `router.py` owns page dispatch; this module owns state
shape, so every view reads/writes session state the same way.
"""
from __future__ import annotations

import copy

import streamlit as st
from streamlit.errors import StreamlitWidgetAlreadyInstantiatedError

from db.models import Component
from db.repositories import components_repo
from engine.compatibility import BuildState, resolve_quantity_limit

DEFAULT_SORT_CRITERIA = "Cost"

_DEFAULTS = {
    "page": "landing",
    "auth_user": None,
    "auth_mode": None,
    "auth_error": {},
    "create_mode": None,
    "build_draft": None,  # set lazily via new_build_draft() once a mode is chosen
    "build_draft_analysis": None,
    "sort_criteria": DEFAULT_SORT_CRITERIA,
    "previous_builds_filter": {"workload_profile": None, "sort": "date"},
    "selected_post_id": None,
    "fork_source_build_id": None,
    "stretch_applied_keys": set(),  # per-cache-key lock for the advisory's one-time stretch-upgrade apply button
    "concierge_messages": [],  # sidebar AI Concierge chat history: [{"role": "user"|"assistant", "content": str}, ...]
    "has_unsaved_build_changes": False,  # True once build_draft has any pick the user hasn't saved (build or draft) yet
    "concierge_last_saved_build": None,  # {"build_id": int, "name": str} for the most recent Concierge `save_build`
    # action applied this session, or None if none has happened yet. The ONLY way `ui/components/chat_assistant.py`
    # can later resolve which real database build a follow-up `publish_build` action (arriving on a LATER chat
    # turn, once the user confirms "yes, publish it") should act on — the LLM itself never sees/invents a build id.
    # Deliberately a NEW, distinctly-named key rather than reusing the retired `concierge_pending_action` key (that
    # one was dead code from an unrelated, now-retired confirm-before-apply mechanism). Plain `None` default needs
    # no `copy.deepcopy` (see init_session_state's docstring) — only ever wholesale-reassigned, never mutated in place.
}


def init_session_state() -> None:
    """`_DEFAULTS`' dict/set-valued entries (`auth_error`, `previous_builds_filter`,
    `stretch_applied_keys`) are module-level objects created once at import
    time — assigning them directly would hand every Streamlit session (every
    concurrent user, in a real multi-session server process) a reference to
    the SAME mutable object. `auth_error`/`previous_builds_filter` happen to
    always be wholesale-reassigned elsewhere rather than mutated in place, so
    that latent bug never surfaced for them, but `stretch_applied_keys.add(...)`
    genuinely mutates in place — without this copy, one user applying a
    stretch upgrade would silently lock the button for every other session
    too. `copy.deepcopy` on every default (not just the mutable ones) is
    simplest and harmless for the immutable ones (str/None)."""
    for key, default in _DEFAULTS.items():
        if key not in st.session_state:
            st.session_state[key] = copy.deepcopy(default)


def navigate_to_page(target_page: str) -> None:
    """Single source of truth for a plain page transition (`st.session_state
    ["page"] = target_page`), used by every real navigation entry point
    (`app.py`'s sidebar buttons, `ui/components/chat_assistant.py`'s
    Concierge `navigate` action) instead of each writing `page` directly.

    The one thing this centralizes beyond the raw assignment: navigating TO
    `"community"` always clears `selected_post_id` first. Without this, a
    user viewing one specific community post's thread (`selected_post_id`
    set) who then navigates to `"community"` from anywhere else would land
    back on `community.py::render()`, which checks `selected_post_id` at the
    very top of its own function and re-shows the SAME stale thread instead
    of the feed — a real, confirmed bug this function exists to fix at every
    call site at once rather than requiring each one to remember it."""
    if target_page == "community":
        st.session_state["selected_post_id"] = None
    st.session_state["page"] = target_page


def teardown_builder() -> None:
    """Leaving Build Studio (spec.md §7.9) — called by every real exit vector
    (`app.py`'s sidebar nav buttons and Logout button, `ui/views/create_build.py`'s
    "⬅ Change mode" button, `ui/components/chat_assistant.py`'s Concierge
    `navigate`/`open_community_build`/`reset_mode` handling) BEFORE the target
    transition itself (a page change, a mode reset, or `log_out()`).

    Per an explicit, later product decision, this performs NO database write
    of any kind — it unconditionally resets `create_mode`/`build_draft`/
    `build_draft_analysis` to `None` and `has_unsaved_build_changes` to
    `False`, so `ui/views/create_build.py`'s own `create_mode is None or
    build_draft is None` gate (§7.4 step 1) shows the mode-selection screen
    the next time Build Studio is entered, never a leftover build. An
    in-progress, unsaved build that hasn't been explicitly checkpointed via
    the "Save as draft" checkbox (§7.4 step 9) or a full "Save build" is
    simply discarded — the same tradeoff a plain "close the tab" would have.
    Does NOT itself change `st.session_state["page"]` or log out — the
    caller does that immediately afterward, since what "leaving" means
    differs per entry point (a page key, or `auth.session.log_out()`).

    This function previously (two designs ago) silently auto-saved an
    unsaved build to `draft_builds` on exit and staged a flash banner
    (`st.session_state["_draft_saved_banner"]`, rendered by `app.py`) — both
    REMOVED ENTIRELY by a later, explicit product decision: the "Save as
    draft" checkbox in the manual Save UI is now the single, explicit source
    of truth for creating a draft, and a silent background write on every
    exit was judged more surprising than useful once that explicit checkbox
    existed. See spec.md §7.9 for the full history of this feature's
    redesigns."""
    st.session_state["create_mode"] = None
    st.session_state["build_draft"] = None
    st.session_state["build_draft_analysis"] = None
    st.session_state["has_unsaved_build_changes"] = False


def new_build_draft(creation_mode: str | None = None) -> dict:
    return {
        "name": "",
        "creation_mode": creation_mode,
        "workload_profile": None,
        "tier": "Mid",
        "budget_ceiling": None,
        "components": {},  # category -> component_id
        "quantities": {},  # category -> count (meaningful only for RAM/Storage, default 1)
    }


def load_components_into_new_draft(
    mode: str, components: dict[str, int], quantities: dict[str, int] | None = None, name: str = ""
) -> dict:
    """The shared shape every "load an existing build/draft into the studio"
    entry point needs: starts a fresh `new_build_draft(mode)`, then replays
    `components`/`quantities` through `set_component`/`set_quantity` —
    resolving each component id via `components_repo.get_by_id`, silently
    skipping one that's since left the catalog (same defensive precedent as
    `resolve_build_state`) — rather than writing `draft["components"]` as a
    raw dict literal. Using the real setters (not a direct write) is what
    correctly marks the result as having unsaved changes for free
    (`has_unsaved_build_changes = True`, set internally by `set_component`),
    since every source this loads from (a draft, a previously-saved build, a
    community post) is itself a real, persisted row — the copy landing in
    the studio is a fresh, uncommitted edit of it, not yet saved on its own.
    Does not itself touch `st.session_state` beyond that — the caller still
    assigns the result to `st.session_state["build_draft"]` and sets
    `create_mode`/`page` itself, since what "loading" means (view, edit,
    fork, clone) differs per caller."""
    draft = new_build_draft(mode)
    draft["name"] = name
    for category, component_id in components.items():
        component = components_repo.get_by_id(component_id)
        if component is not None:
            set_component(draft, category, component)
    for category, quantity in (quantities or {}).items():
        set_quantity(draft, category, quantity)
    return draft


def resolve_build_state(build_draft: dict | None) -> BuildState:
    """Fetch full Component rows for whatever's pinned in build_draft. Missing
    or since-removed component ids are silently skipped rather than raising —
    a stale pick shouldn't crash the whole build view."""
    build_state: BuildState = {}
    if not build_draft:
        return build_state
    for category, component_id in build_draft.get("components", {}).items():
        component = components_repo.get_by_id(component_id)
        if component is not None:
            build_state[category] = component
    return build_state


def _sync_qty_widget_key(category: str, value: int | None) -> None:
    """Best-effort sync of `ui/components/part_picker.py`'s Qty stepper
    widget key (`qty_{category}`) to `value` (or removes it entirely when
    `value` is `None`) — `None` when a slot is emptied, an int when a
    quantity is set. Streamlit forbids writing to (or popping) a widget's
    session-state key AFTER that widget has already been instantiated in the
    CURRENT script run (`StreamlitWidgetAlreadyInstantiatedError`) — this
    happens for real whenever `set_quantity`/`remove_component` is called
    from the stepper's OWN on-change callback (the widget already rendered
    earlier in this exact run, using the just-typed value) or from
    create_build.py code that runs after the part-picker grid (e.g. the
    stretch-upgrade advisory's `set_quantity` action). In the on-change case
    the write would be redundant anyway (the widget's own interaction
    already put the same value there); in the after-the-grid case, skipping
    it here is safe too — `build_draft["quantities"]` is already correct,
    and the widget's own pre-render clamp/sync logic reconciles it against
    the real value on the very next fresh render, before that key's widget
    has been instantiated in THAT run. Silently swallowing this specific,
    well-understood exception is what makes the sync possible at all for
    the OTHER (safe) call sites — e.g. the AI Concierge applying a
    `modify_build` quantity change, which runs before `router.render()`
    reaches the widget at all — without also having to duplicate this
    caller-context check at every call site."""
    key = f"qty_{category}"
    try:
        if value is None:
            st.session_state.pop(key, None)
        else:
            st.session_state[key] = value
    except StreamlitWidgetAlreadyInstantiatedError:
        pass


def set_component(build_draft: dict, category: str, component: Component) -> None:
    build_draft.setdefault("components", {})[category] = component.id
    _invalidate_analysis()
    st.session_state["has_unsaved_build_changes"] = True


def remove_component(build_draft: dict, category: str) -> None:
    build_draft.get("components", {}).pop(category, None)
    # A freshly-emptied slot starts back at quantity 1 if it's ever re-filled,
    # rather than inheriting a stale multiplier from whatever was there before.
    build_draft.get("quantities", {}).pop(category, None)
    # `ui/components/part_picker.py`'s Qty stepper is a keyed widget
    # (`qty_{category}`) that only ever honors a fresh `value=` argument the
    # very first time that key is created — every rerun after that, it
    # renders from whatever's already cached in st.session_state[key],
    # regardless of what this function just wrote to build_draft. Popping
    # the stale widget key here means a re-filled slot's stepper gets to
    # honor `value=` again on its next render, rather than silently
    # resurrecting whatever quantity the PREVIOUS component in this slot
    # happened to have. See `_sync_qty_widget_key`'s own docstring for why
    # this pop is wrapped defensively.
    _sync_qty_widget_key(category, None)
    _invalidate_analysis()
    st.session_state["has_unsaved_build_changes"] = True


def get_quantity(build_draft: dict, category: str) -> int:
    return build_draft.get("quantities", {}).get(category, 1)


def set_quantity(build_draft: dict, category: str, quantity: int) -> None:
    clamped = max(1, quantity)
    build_draft.setdefault("quantities", {})[category] = clamped
    # Keep the Qty stepper's own widget key (`ui/components/part_picker.py`,
    # `qty_{category}`) in lockstep with the real value this function just
    # wrote. Without this, any OUT-OF-BAND quantity write — this function is
    # called not just from the manual stepper's own callback but also from
    # the AI Concierge's `modify_build` handling and the advisory's
    # stretch-upgrade "set_quantity" action — has no effect on what the
    # stepper actually DISPLAYS: a keyed Streamlit widget only ever honors a
    # fresh `value=` argument the very first time its key is created: every
    # later rerun renders from whatever's already cached in
    # st.session_state[key] and simply ignores `value=` entirely. Writing
    # the key directly here, at the single source of truth for this value,
    # closes that gap for every caller at once rather than requiring each
    # one to remember to do it themselves. See `_sync_qty_widget_key`'s own
    # docstring for why this write is wrapped defensively.
    _sync_qty_widget_key(category, clamped)
    # A quantity change affects cost/compatibility just like a component
    # swap, so the last synergy/bottleneck read must go stale too.
    _invalidate_analysis()
    st.session_state["has_unsaved_build_changes"] = True


def _invalidate_analysis() -> None:
    """Any change to which components are picked makes the last synergy/
    bottleneck analysis stale — clear it so the UI doesn't keep showing scores
    for a build that no longer matches what's on screen (it'll say "select at
    least two components" or need a fresh "Analyze" click instead)."""
    st.session_state["build_draft_analysis"] = None


def build_total_cost(build_state: BuildState, quantities: dict[str, int] | None = None) -> float:
    return sum(component.price_usd * (quantities or {}).get(category, 1) for category, component in build_state.items())


def resolve_effective_quantity_limit(
    build_state: BuildState,
    category: str,
    quantities: dict[str, int],
    budget_ceiling: float | None,
) -> tuple[int | None, str, str]:
    """Combines the real physical slot limit (engine.compatibility.
    resolve_quantity_limit) with a budget-affordability limit, returning
    whichever is tighter. Returns (effective_max, reason, limit_kind) where
    limit_kind is "physical" | "budget" | "none" (neither constraint has
    real data to bound this category — caller applies its own UI-only
    fallback cap in that case, exactly as it already does for the
    physical-only function).

    budget_ceiling=None means no financial constraint applies (Workload/Free
    modes, or Budget mode with no ceiling set yet) — the same convention
    ui/components/part_picker.py already uses when calling render_part_picker
    (it passes build_draft.get("budget_ceiling"), which is None outside
    Budget mode). effective_max is always >= 1 when it is not None.

    The financial calculation: holding every OTHER category's cost fixed at
    its current (quantity-scaled) total, how many units of THIS category's
    currently-selected component can the remaining budget afford? This does
    NOT reserve budget for other still-empty categories (a more elaborate
    concern the existing "Minimum Reserve Threshold" logic in
    ui/components/part_picker.py already handles for NEW component
    selection) — this function only answers "would incrementing this
    quantity, with everything else held fixed, exceed the ceiling."
    """
    physical_max, physical_reason = resolve_quantity_limit(build_state, category)

    financial_max: int | None = None
    financial_reason = ""
    component = build_state.get(category)
    if budget_ceiling is not None and component is not None and component.price_usd > 0:
        current_qty = quantities.get(category, 1)
        total_cost = build_total_cost(build_state, quantities)
        other_components_cost = total_cost - (current_qty * component.price_usd)
        remaining_for_category = budget_ceiling - other_components_cost
        financial_max = max(1, int(remaining_for_category // component.price_usd))
        financial_reason = (
            f"Budget limit reached: cannot afford additional units without exceeding "
            f"{budget_ceiling:,.2f} USD."
        )

    candidates = [v for v in (physical_max, financial_max) if v is not None]
    if not candidates:
        return None, "", "none"

    effective_max = max(1, min(candidates))
    if physical_max is not None and effective_max == physical_max and (
        financial_max is None or physical_max <= financial_max
    ):
        return effective_max, physical_reason, "physical"
    return effective_max, financial_reason, "budget"
