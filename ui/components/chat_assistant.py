"""Sidebar AI Concierge & Site Navigator widget (spec.md §7.7 / §6.7).

Renders a chat-style expander in the authenticated sidebar backed by
`llm.concierge.get_concierge_response`. This module pre-fetches the full
catalog and community feed (the only two things `llm/concierge.py` itself
is forbidden from querying) and hands them to the LLM on every turn. It owns
no compatibility/scoring math and no SQL beyond calling the two allowed
repository read functions.

`load_build`/`modify_build`/`navigate`/`open_community_build` actions
returned by a turn are applied IMMEDIATELY, with no confirmation click —
`load_build`/`modify_build`/`navigate` only ever mutate the ephemeral,
uncommitted `build_draft`/`page` session state, nothing is persisted to the
database until the user later explicitly clicks "Save build" in
`ui/views/create_build.py`'s normal flow, so there is nothing here that isn't
already fully visible and re-editable the instant it lands.
`open_community_build` similarly only sets `page`/`selected_post_id` — no
database write at all, it just deep-links to an ALREADY-shared post's
existing thread view.

A `navigate` action leaving `create_build` performs NO database write of any
kind — see BUILDER TEARDOWN below. This is the CURRENT of several designs
this exact concern has gone through: an original design let `navigate`
auto-stash the active build via a `save_as_draft` field; that was replaced
by an explicit "Save Draft or Discard?" confirmation dialog
(`ui/components/nav_guard.py`); that was replaced by a silent, unconditional
auto-save-on-exit with a flash banner; ALL of that has since been removed
entirely by a later, explicit product decision — the "Save as draft"
checkbox in `ui/views/create_build.py`'s manual Save UI (§7.4 step 9) is now
the single, explicit source of truth for creating a draft, and leaving the
builder without using it simply discards the in-progress build, the same
tradeoff a plain "close the tab" would have.

`save_build`/`publish_build` are different: `save_build` is deliberately NOT
zero-click by default — the Concierge must first ask the user for both a
`name` and a `destination` ("draft" or "build") and wait for their answer
across one or more turns before this action is ever returned (see
`llm.schemas.ConciergeSaveBuildAction`'s docstring and `llm/concierge.py`'s
SYSTEM_PROMPT intent 7) — UNLESS the user's own message already
unambiguously supplied both up front, the FAST-TRACK path, in which case it
fires on that very first turn instead; this module only ever receives the
action once the model has determined (via either path) that it has both
pieces. Once it does arrive, THIS module still applies it immediately with
no separate confirmation click of its own — the "confirmation" already
happened conversationally (or was never needed, on the fast-track path,
since the user stated their full intent unprompted). `destination ==
"draft"` writes to the real `draft_builds` table via `drafts_repo.save_draft`
(there is no `saved_builds` table anywhere in this app); `destination ==
"build"` mirrors `ui/views/create_build.py::_save_actions`'s own real
`builds_repo.create_build` call exactly, and additionally applies
`builds_repo.set_public` + `community_repo.create_post` in the SAME turn
when the action's `publish_immediately` field is `true` (only ever set when
the user's OWN message that triggered the save ALSO explicitly asked to
publish in the same breath — never inferred) — skipping the separate
"would you like to publish?" round-trip entirely for that case.
`publish_build` (the separate, later action reachable after an ORDINARY
"build"-destination save) mirrors that same `community_repo.create_post`
call. `save_build`'s `source` field (`"studio"` by default, or `"community"`)
picks WHICH build's components/quantities/scores actually get persisted:
`"studio"` reads `st.session_state["build_draft"]` exactly as described
above; `"community"` instead reads a specific, ALREADY-shared
`CommunityPost`'s own `Build` row (`action["source_post_id"]`, zero-
hallucination-guarded by `llm/concierge.py` against `community_summary`,
exactly like `open_community_build`/`load_saved_build`) — for a request like
"save the build I'm looking at to my drafts" made while viewing it on the
Community page, distinct from `load_saved_build` in that it does NOT touch
`build_draft`/`page`/`has_unsaved_build_changes` at all: it is a headless
clone straight into the user's own drafts/builds, not "let me edit this in
the Studio first," so any of the user's own actually-in-progress Studio work
is left completely untouched. This is acceptable specifically
because it's the user's own explicit, direct request acting on their own
account's own data — the same trust boundary as them clicking "Save
build"/"Share to Community" themselves. The multi-turn "save (name +
destination) -> would you like to publish? -> want to add a description?"
conversation is resolved entirely by `llm.concierge`'s own prompt reading
`conversation_history` (the same mechanism already used for the budget
guardrail) — this module tracks exactly one new piece of real state the
model cannot know on its own: WHICH database build id a "build"-destination
`save_build` action just created, in
`st.session_state["concierge_last_saved_build"]` (`{"build_id": int, "name":
str}` or `None`), read back by a later `publish_build` action. A
"draft"-destination save never sets this key — a draft has no publish path,
so there is nothing for a later turn to resolve. `has_unsaved_build_changes`
is reset to `False` on a successful `save_build` of EITHER destination,
matching the manual save flow — but, unlike that flow, `build_draft`/
`create_mode`/`page` are deliberately left untouched: a save happening
mid-chat conversation forcibly yanking the user over to `my_builds` (and
losing the in-progress chat context right as the Concierge is about to ask a
publish follow-up question) would be more disruptive than useful here.

A `navigate` action's remaining optional fields are handled here too:
`filters` (only ever meaningful for a `"community"` destination) is staged
into the one-shot `st.session_state["pending_community_filters"]` key for
`ui/views/community.py::_apply_pending_community_filters` to translate into
its own real widget keys on its next render (this module never writes those
widget keys directly — it doesn't have the live feed data needed to resolve
a requested filter into one of their real option strings). `navigate_to`
accepts `"landing"`/`"create_build"`/`"my_builds"`/`"community"`/`"drafts"`.

BUILDER TEARDOWN (spec.md §7.9): a `navigate` action leaving `create_build`
for a DIFFERENT page calls `ui.state.teardown_builder()` first — the exact
same function `app.py`'s sidebar nav buttons and Logout call, and
`ui/views/create_build.py`'s "⬅ Change mode" button. That function
unconditionally resets `create_mode`/`build_draft`/`build_draft_analysis`/
`has_unsaved_build_changes` — no database write, no confirmation click, no
dialog, no flash message. It is always safe to call even when there's
nothing unsaved.

`reset_mode` (optional bool on the action, only meaningful when
`navigate_to == "create_build"`) exposes the EXISTING "⬅ Change mode" button
(`ui/views/create_build.py`) via chat — the SAME `teardown_builder()` call,
landing back on the mode-selection screen.

An `open_community_build` action ALSO calls `teardown_builder()` first when
leaving `create_build` (deep-linking away from the builder is still leaving
it), then deep-links straight to one specific, already-shared community
post's thread view: it sets `st.session_state["page"] = "community"` and
`st.session_state["selected_post_id"] = action["post_id"]` — `post_id` is
already guaranteed real by `llm/concierge.py`'s own zero-hallucination guard
(cross-checked against the exact `community_summary` this module sent it),
and `ui/views/community.py::render()` already checks `selected_post_id` at
the very top of its own function, so no further wiring is needed in that
module for this to land directly on the post's thread view on the next
render.

A `load_saved_build` action loads an EXISTING, already-persisted draft,
previously-saved build, or community post's build directly into Build
Studio for editing — distinct from `load_build` (a brand NEW build from
named catalog parts) and from `open_community_build` above (a READ-ONLY
thread view). `_drafts_summary()`/`_previous_builds_summary()` are two more
pre-fetched, per-call summaries (same shape/precedent as `_catalog_summary`/
`_community_summary`) giving the model the current user's own real
`draft_builds`/`builds` rows to resolve `source`/`id` against — cross-checked
by `llm/concierge.py::_validate_action` the same way `open_community_build`'s
`post_id` already is. `current_page`/`viewed_post_id` (the real
`st.session_state["page"]`/`["selected_post_id"]`, the latter only sent when
on the `"community"` page) are also handed to the model on every call so it
can resolve page-relative phrasing ("load this draft", "edit this build")
without the user restating a name. Applying it reuses `ui.state.
load_components_into_new_draft` — the SAME shared helper `ui/views/
drafts.py`'s "Load into Builder", `community.py::_fork_into_studio`, and
`my_builds.py::_clone_into_studio` all use — and, like every other "enter
the studio" transition, calls `ui.state.teardown_builder()` first when
already on `create_build` (spec.md §7.9, no database write) so an unsaved
in-progress build is never silently mixed with what's being loaded.

`_advisory_context` supplies the Concierge with a pre-fetched, synthesizable
`get_build_advisory` result (`llm/concierge.py` cannot call `llm.advisory`
or touch the database itself, per its import boundary) — but only when the
user's message actually looks like an optimization/analysis ask (a cheap,
local keyword check, `_looks_like_analysis_request`), so a real advisory
LLM call isn't made on every single chat turn regardless of relevance. It
reuses `st.session_state["advisory_cache"]` (the exact same cache + cache-key
shape as `create_build.py::_advisory_controls`) so a user who already clicked
"✨ Get AI Analysis & Upgrade Path" gets an instant, free cache hit here.

RTL/BiDi RENDERING FOR HEBREW MESSAGES: `llm/concierge.py`'s SYSTEM_PROMPT
already replies in Hebrew (and other languages) reasonably well on its own —
what Streamlit does NOT do on its own is align/direction-style that reply as
right-to-left prose; the Unicode Bidi Algorithm shapes characters correctly
regardless, but block-level `text-align`/`direction` still defaults to LTR.
`_looks_like_hebrew` (a plain `֐`-`׿` codepoint check, no new
dependency) gates a different rendering path in `render_concierge_widget`'s
message loop: instead of the plain `st.markdown(sanitize_markdown(...))` call
used for every other message, a Hebrew-containing message is HTML-escaped
(`html.escape`, stdlib) and wrapped in a small `<div style="direction: rtl;
text-align: right; unicode-bidi: plaintext;">...</div>` rendered via
`st.markdown(..., unsafe_allow_html=True)`. Applied to EITHER role (`"user"`
or `"assistant"`) whose content contains Hebrew, not assistant-only — a
Hebrew-typing user's own message would otherwise sit LTR-aligned directly
above/below an RTL-styled assistant reply in the same thread, which reads as
visibly broken; symmetry is the point, not an asymmetric assistant-only
treatment.

SECURITY: the content wrapped here is LLM-generated (or user-typed) text —
untrusted from this module's perspective — so `unsafe_allow_html=True` is a
capability this module did not need before. The `html.escape()` call is what
makes that safe: it runs BEFORE the string is interpolated into the wrapper
`<div>`, so only the outer wrapper tags this module itself writes are live,
trusted HTML; whatever the message content contains (accidentally or via a
future prompt-injection-style attack through a compromised API response) is
always rendered as inert escaped text, never as a live tag/script. Verified
empirically (a throwaway local Streamlit script, not committed): a message
like `<img src=x onerror=alert(1)>` renders as the literal escaped text, not
a live tag. Also verified empirically that `sanitize_markdown`'s `$`->`USD `
defense is NOT needed on this path and is deliberately NOT applied to it: a
`$...$` pair inside a `<div>...</div>` block handed to `st.markdown` with
`unsafe_allow_html=True` sits inside a raw HTML block, which Streamlit's
markdown parser does not run inline markdown/math processing over at all — a
literal `$5`/`$6` came through unmangled in that throwaway script, whereas
the exact same text passed to a plain `st.markdown(...)` call (no wrapper,
the existing non-Hebrew path) visibly triggered the KaTeX/math-mode bug
`sanitize_markdown` exists to prevent. Running `sanitize_markdown` on the
Hebrew/RTL path anyway would be harmless but pointless (it would rewrite a
"$" to "USD " that was never going to be mangled in the first place); it is
skipped here specifically so the escaped text stays a faithful, literal
rendering of what the model/user actually wrote.

AUTHORITATIVE TOTAL-COST LINE: `llm/concierge.py` has no `engine`/`db` access
and, confirmed via a live (non-mocked) reproduction, is unreliable at summing
many real line-item prices into a build's aggregate cost — it has been
observed returning a `reply` that simply parrots the user's REQUESTED budget
figure back as if it were the resulting build's actual total. Because a wrong
total shown to the user is a correctness problem (not a style/wording one),
`_apply_concierge_action` returns the real total cost (via
`ui.state.build_total_cost`, the same single source of truth used by
`create_build.py`'s summary header) whenever a `load_build`/`modify_build`
action successfully results in a non-empty `build_state`, and
`render_concierge_widget` APPENDS a short, clearly-separated, deterministic
confirmation line carrying that number to the assistant's message — it never
edits/removes whatever the model's own `reply` text said (fragile string
surgery on arbitrary, possibly non-English LLM prose risks mangling grammar
for no real gain); `llm/concierge.py`'s `SYSTEM_PROMPT` separately instructs
the model to stop stating a competing aggregate total at all, so the two
numbers visibly disagreeing should become rare in practice, but the
Python-appended line is the one always guaranteed correct regardless. No such
line is appended for `navigate`/`save_build`/`publish_build` (no build total
is meaningful there) or when the action no-ops (no active draft / empty
`build_state`).
"""
from __future__ import annotations

import html
import json
import re
import sys
import traceback
from datetime import datetime

import streamlit as st

from auth.session import current_user
from db.models import COMPONENT_CATEGORIES
from db.repositories import builds_repo, community_repo, components_repo, drafts_repo
from engine import solvers
from engine.compatibility import evaluate_build
from engine.scoring import live_bottleneck_and_synergy
from llm.advisory import get_build_advisory
from llm.client import analyze_build
from llm.concierge import get_concierge_response
from ui import state
from ui.format import CURRENCY_RATES, format_currency, sanitize_markdown

# A couple of cheap, generically-useful key specs per component, when present
# — matching the compact style of llm/advisory.py's own _component_summary,
# not a full spec dump.
_SUMMARY_SPEC_FIELDS = ("socket", "ram_type", "capacity_gb", "interface")

# optimize_bottleneck's own default target ceiling when the user's request
# didn't state an explicit one (e.g. a bare "optimize the bottleneck") —
# matches the same "roughly 10-12%" threshold llm/concierge.py's SYSTEM_PROMPT
# already uses to decide whether this action is even offered in the first
# place, so the applied target and the trigger condition stay consistent.
_DEFAULT_BOTTLENECK_TARGET = 10.0

# Bounded retry cap for optimize_bottleneck's own internal verification loop
# (re-check the live bottleneck after each applied upgrade, try once more if
# still above target) — never unbounded: a build that's genuinely peaked at
# both CPU and GPU tiers must degrade gracefully (stop, return whatever was
# reached) rather than looping forever or racking up unbounded LLM calls.
_MAX_BOTTLENECK_OPTIMIZE_ATTEMPTS = 3

# Bounded retry cap for use_remaining_budget's own internal spend-down loop —
# higher than the bottleneck loop's cap since fully using a larger headroom
# realistically spans several priority tiers (RAM, then Storage, then Cooler)
# rather than stopping once a single percentage target is met.
_MAX_BUDGET_UTILIZATION_ATTEMPTS = 5

# Caps how much prior conversation gets resent to the LLM on every turn —
# without this, `conversation_history` grows unboundedly with the whole
# session's chat log, which just wastes tokens/cost on older, less relevant
# turns (a long-running conversation still needs the model to read its own
# LAST turn for the budget-guardrail/save-publish multi-turn flows above,
# which this window comfortably covers). Does not affect what's actually
# displayed in the chat UI — `st.session_state["concierge_messages"]` itself
# is never trimmed, only the copy handed to the LLM as `conversation_history`.
_MAX_HISTORY_MESSAGES = 4

# Cheap, local (non-LLM) intent gate for pre-fetching an advisory read —
# bounds cost by never triggering a second real LLM round-trip just to
# classify intent.
_ANALYSIS_KEYWORDS = (
    "optimi",  # optimize / optimise / optimization / optimisation
    "analy",  # analyze / analyse / analysis
    "upgrade",
    "advice",
    "recommend",
    "improve",
    "bottleneck",
    "budget",  # "how much can you add without going over budget?" — a real,
    # confirmed gap: this phrasing has no "upgrade"/"optimi"/etc. substring at
    # all, so advisory_context was never pre-fetched for it, leaving the
    # BUDGET-LEEWAY UPGRADE REQUESTS carve-out (llm/concierge.py SYSTEM_PROMPT
    # intent 4) with nothing to translate into a real modify_build action.
    "headroom",
    "remaining",
)


def _looks_like_analysis_request(user_message: str) -> bool:
    lowered = user_message.lower()
    return any(keyword in lowered for keyword in _ANALYSIS_KEYWORDS)


# The standard Hebrew Unicode block. A plain codepoint-range check is enough
# to detect "this message contains Hebrew" for RTL styling purposes — no
# external language-detection dependency needed (see this module's docstring's
# "RTL/BiDi RENDERING FOR HEBREW MESSAGES" section for why this exists and
# what it gates).
_HEBREW_RE = re.compile(r"[֐-׿]")


def _looks_like_hebrew(text: str) -> bool:
    return bool(_HEBREW_RE.search(text))


def _render_chat_message(content: str) -> None:
    """Renders one chat bubble's content, with RTL/BiDi styling for a message
    that contains Hebrew (either role — see this module's docstring). The
    non-Hebrew path is byte-for-byte the pre-existing behavior: never
    HTML-escaped, never wrapped, `sanitize_markdown` still applied exactly as
    before.

    The Hebrew path HTML-escapes `content` BEFORE interpolating it into the
    wrapper `<div>` — this is the load-bearing safety step. `content` is
    LLM-generated (or user-typed) text, untrusted from this module's
    perspective; escaping it first means only the static wrapper tags this
    function writes are ever live HTML, and whatever the message text
    contains is always inert, escaped text, never a rendered tag/script, even
    though this call site now passes `unsafe_allow_html=True` (a capability
    this module did not need before this feature). `sanitize_markdown`'s
    `$`->`USD ` KaTeX/math-mode defense is deliberately NOT applied on this
    path — verified empirically that a `$...$` pair inside a raw HTML block
    handed to `st.markdown(..., unsafe_allow_html=True)` is not run through
    Streamlit's inline markdown/math processing at all, unlike the plain
    non-wrapped path, so there is nothing here for that defense to guard
    against."""
    if _looks_like_hebrew(content):
        escaped = html.escape(content)
        wrapped = (
            '<div style="direction: rtl; text-align: right; unicode-bidi: plaintext;">'
            f"{escaped}</div>"
        )
        st.markdown(wrapped, unsafe_allow_html=True)
    else:
        st.markdown(sanitize_markdown(content))


def _active_currency() -> str:
    return st.session_state.get("selected_currency", "USD")


def _catalog_summary() -> list[dict]:
    """Full catalog (~143 real components) — compact enough to embed whole in
    one concierge payload every message. `price_usd` stays the real, never-
    converted USD price (this is what `_validate_action`'s zero-hallucination
    guard and any internal budget-guardrail arithmetic reason over — it must
    stay in one consistent, real currency). `display_price` is a SEPARATE,
    already-converted-and-formatted string in the user's active currency
    (ui/format.py) — the ONLY field the model is instructed to quote when
    stating a catalog price aloud, so it never performs currency-conversion
    arithmetic itself (the same "don't trust the model with number-crunching"
    precedent as the NO AGGREGATE TOTALS RULE, extended to cover conversion
    math too)."""
    currency = _active_currency()
    summary: list[dict] = []
    for category in COMPONENT_CATEGORIES:
        for component in components_repo.get_by_category(category):
            entry = {
                "id": component.id,
                "category": category,
                "name": component.name,
                "price_usd": component.price_usd,
                "display_price": format_currency(component.price_usd, currency),
            }
            for field in _SUMMARY_SPEC_FIELDS:
                value = getattr(component, field, None)
                if value is not None:
                    entry[field] = value
            summary.append(entry)
    return summary


def _community_summary() -> list[dict]:
    """`total_cost` stays real USD (unconverted); `display_total_cost` is the
    pre-converted, pre-formatted string for the model to quote — same
    rationale as `_catalog_summary`'s `display_price`."""
    currency = _active_currency()
    posts = community_repo.get_feed()
    return [
        {
            "post_id": post.id,
            "build_id": post.build_id,
            "title": post.title,
            "creation_mode": post.build.creation_mode,
            "workload_profile": post.build.workload_profile,
            "workload_tier": post.build.workload_tier,
            "total_cost": post.build.total_cost,
            "display_total_cost": format_currency(post.build.total_cost, currency),
            "author_notes": post.author_notes,
        }
        for post in posts
    ]


def _drafts_summary() -> list[dict]:
    """The current user's own `draft_builds` rows — real ids the Concierge's
    `load_saved_build` action (`source == "draft"`) is cross-checked against
    by `llm/concierge.py::_validate_action`, the same zero-hallucination
    precedent as `_community_summary()`'s `post_id`."""
    user = current_user()
    if user is None:
        return []
    return [
        {"draft_id": draft.id, "name": draft.name, "mode": draft.mode}
        for draft in drafts_repo.get_user_drafts(user["id"])
    ]


def _previous_builds_summary() -> list[dict]:
    """The current user's own real, finished `builds` rows (§3.4) — real ids
    the Concierge's `load_saved_build` action (`source == "build"`) is
    cross-checked against, same precedent as `_drafts_summary()` above."""
    user = current_user()
    if user is None:
        return []
    return [
        {"build_id": build.id, "name": build.name, "creation_mode": build.creation_mode}
        for build in builds_repo.get_builds_for_user(user["id"])
    ]


def _current_build_context() -> dict | None:
    """Snapshot of the user's currently active build draft, handed to the
    LLM so it can support incremental `modify_build` requests. `None` when
    there's no draft in progress at all (mode not chosen yet) or it resolves
    to no real components (e.g. every previously-picked id has since been
    removed from the catalog).

    `price_usd`/`budget_ceiling` stay real, unconverted USD (the budget
    guardrail's own internal arithmetic, and anything `_validate_action`
    might reason over, must stay in one consistent real currency). Each
    component's `display_price`, and the top-level `formatted_total`
    (`ui.state.build_total_cost` run through `ui.format.format_currency` —
    the SAME authoritative total `render_concierge_widget` itself later
    appends to the reply), are the pre-converted strings the model is
    instructed to quote instead — this is what finally lets a bare "what's
    my current total?" question be answered correctly without the model
    ever summing/converting anything itself (see SYSTEM_PROMPT's CURRENT
    BUILD TOTAL-COST QUESTIONS intent).

    `compatibility_issues`/`bottleneck` (spec.md §6.7 intents 11/12, "Fix
    Warnings"/"Optimize Bottleneck") are the SAME real, deterministic data
    `create_build.py`'s own HUD shows — `engine.compatibility.evaluate_build`
    and, for bottleneck, `build_draft_analysis` (the LLM/heuristic
    synergy/bottleneck read already computed for a complete build) when
    present, else the same `engine.scoring.live_bottleneck_and_synergy`
    estimate the HUD falls back to for an incomplete one — never a second,
    independently-computed copy of either. `bottleneck["direction"]` is
    normalized to the SAME `"CPU-bound"|"GPU-bound"|"Balanced"` vocabulary
    either source uses (`build_draft_analysis`'s own `limiting_component` is
    `"CPU"|"GPU"|"None"` instead, so `"None"` maps to `"Balanced"` and
    `"CPU"/"GPU"` gets `"-bound"` appended) so the SYSTEM_PROMPT only ever
    has to reason about one shape regardless of which source produced it."""
    build_draft = st.session_state.get("build_draft")
    if not build_draft:
        return None
    build_state = state.resolve_build_state(build_draft)
    if not build_state:
        return None
    currency = _active_currency()
    quantities = build_draft.get("quantities", {})

    report = evaluate_build(build_state, quantities)

    analysis = st.session_state.get("build_draft_analysis")
    if analysis:
        limiting = analysis["bottleneck"]["limiting_component"]
        bottleneck = {
            "percentage": analysis["bottleneck"]["bottleneck_percentage"],
            "direction": "Balanced" if limiting == "None" else f"{limiting}-bound",
        }
    else:
        live = live_bottleneck_and_synergy(build_state)
        bottleneck = {"percentage": live[1], "direction": live[2]} if live is not None else None

    return {
        "mode": build_draft.get("creation_mode"),
        "budget_ceiling": build_draft.get("budget_ceiling"),
        "components": {
            category: {
                "id": component.id,
                "name": component.name,
                "price_usd": component.price_usd,
                "display_price": format_currency(component.price_usd, currency),
            }
            for category, component in build_state.items()
        },
        "quantities": quantities,
        "formatted_total": format_currency(state.build_total_cost(build_state, quantities), currency),
        "compatibility_issues": report.issues,
        "bottleneck": bottleneck,
    }


def _advisory_context(user_message: str) -> dict | None:
    """Pre-fetched `get_build_advisory` result for the Concierge to
    synthesize, gated behind `_looks_like_analysis_request` so a real
    (LLM-backed) advisory call only happens when the user's message actually
    signals an optimization/analysis intent. Reuses the same cache + cache
    key shape as `create_build.py::_advisory_controls` — see that function's
    docstring for the key's exact shape and invalidation philosophy."""
    if not _looks_like_analysis_request(user_message):
        return None
    build_draft = st.session_state.get("build_draft")
    if not build_draft:
        return None
    build_state = state.resolve_build_state(build_draft)
    if len(build_state) < 2:
        return None

    mode = build_draft.get("creation_mode")
    current_budget_or_cost = build_draft.get("budget_ceiling") if mode == "Budget" else None
    if not current_budget_or_cost:
        current_budget_or_cost = state.build_total_cost(build_state, build_draft.get("quantities", {}))

    cache_key = (
        mode,
        build_draft.get("workload_profile"),
        tuple(sorted((category, component.id) for category, component in build_state.items())),
        round(current_budget_or_cost, 2),
    )
    cache = st.session_state.setdefault("advisory_cache", {})
    if cache_key not in cache:
        cache[cache_key] = get_build_advisory(
            build_state,
            mode,
            current_budget_or_cost,
            profile=build_draft.get("workload_profile"),
            bottleneck_info=(st.session_state.get("build_draft_analysis") or {}).get("bottleneck"),
            quantities=build_draft.get("quantities", {}),
        )
    return cache[cache_key]


def _default_saved_build_name() -> str:
    """LAST-RESORT DEFENSIVE FALLBACK ONLY — no longer the primary path.

    Under the current interactive-first `save_build` design, the model is
    REQUIRED (by `llm.schemas.ConciergeSaveBuildAction`'s now-required `name`
    field) to have already asked the user for a real name before this action
    can ever be returned — the model should always arrive with a real,
    user-chosen `name`. This helper only exists as a belt-and-suspenders
    fallback for the case where a malformed/heuristic response somehow
    reaches `_apply_concierge_action` with an empty/missing `name` anyway
    (Pydantic validation should already have rejected that upstream, but
    defensive code here costs nothing and matches this function's own
    existing defensive-guard style everywhere else). A timestamped default is
    unambiguous and never collides with a real prior build."""
    return f"Concierge Build – {datetime.now().strftime('%Y-%m-%d %H:%M')}"


def _sync_authoritative_analysis(build_draft: dict, build_state: dict) -> tuple[float, float, str] | None:
    """GROUND-TRUTH BINDING (a real, confirmed bug this fixes): a live
    reproduction showed the Concierge's own reply text stating a
    Synergy/Bottleneck reading that visibly disagreed with the Build
    Studio's HUD moments later — not because the model invented a number
    from nothing (the RIGOROUS TELEMETRY REPLY FORMAT already stops that),
    but because this module's own telemetry line and `create_build.py`'s
    `_maybe_auto_analyze` were computing TWO INDEPENDENT readings for the
    same build: this line used the plain deterministic
    `live_bottleneck_and_synergy` heuristic, while the HUD (once its own
    lazy analysis finished) shows an LLM-REFINED reading that spec.md §6.3
    explicitly allows to differ from that same heuristic baseline by up to
    +/-10 percentage points. Two real, legitimate numbers — just not the
    SAME one, and showing both is what created the mismatch.

    This closes the gap by computing (or reusing an `llm_cache` hit for) the
    EXACT SAME analysis `_maybe_auto_analyze` would — `llm.client.
    analyze_build`, the SAME call, same arguments — for a build with all 8
    core categories filled, and storing it into
    `st.session_state["build_draft_analysis"]` right here. The HUD's own
    next render then finds an analysis already on file and skips
    recomputing one of its own (the exact "no analysis on file yet" check
    `_maybe_auto_analyze` already gates on) — so the chat message and the
    HUD are now reading the SAME stored result, not two independent
    computations of what should be one number. Falls back to the plain
    heuristic (matching `_maybe_auto_analyze`'s OWN fallback for an
    incomplete build) when fewer than all 8 core categories are filled —
    `analyze_build` requires a complete build, and the HUD wouldn't show a
    refined reading for an incomplete one either. Returns `None` when there
    are fewer than 2 components (nothing meaningful to report either way).

    Returns `(synergy, bottleneck_percentage, direction)`, `direction`
    normalized to `"CPU-bound"|"GPU-bound"|"Balanced"` regardless of source —
    the same normalization `_current_build_context()` already applies to
    this exact same ambiguity."""
    if set(solvers.CATEGORY_ORDER).issubset(build_state.keys()):
        with st.spinner("Analyzing build..."):
            response = analyze_build(
                build_state,
                workload_profile=build_draft.get("workload_profile"),
                budget_ceiling=build_draft.get("budget_ceiling"),
            )
        st.session_state["build_draft_analysis"] = response.model_dump()
        limiting = response.bottleneck.limiting_component
        direction = "Balanced" if limiting == "None" else f"{limiting}-bound"
        return response.synergy.overall_score, response.bottleneck.bottleneck_percentage, direction
    if len(build_state) >= 2:
        return live_bottleneck_and_synergy(build_state)
    return None


def _read_cached_analysis(build_state) -> tuple[float, float, str] | None:
    """The read-only counterpart to `_sync_authoritative_analysis`, used for
    the "BEFORE" snapshot only: the pre-mutation build already has whatever
    analysis was last synced for it sitting in `st.session_state[
    "build_draft_analysis"]` (kept in lockstep by that same function after
    every prior build-mutating turn) — re-reading it here, rather than
    independently recomputing a fresh heuristic estimate, is what closes a
    second real instance of the exact desync `_sync_authoritative_analysis`
    exists to fix: without this, a "before" reading computed via the plain
    heuristic could itself disagree with the LLM-refined number the HUD (and
    the PREVIOUS turn's own telemetry line) already showed for that same
    build, one turn ago. Falls back to the live heuristic only when nothing
    has been cached yet (e.g. the very first build-mutating turn of a
    session)."""
    analysis = st.session_state.get("build_draft_analysis")
    if analysis:
        limiting = analysis["bottleneck"]["limiting_component"]
        direction = "Balanced" if limiting == "None" else f"{limiting}-bound"
        return analysis["synergy"]["overall_score"], analysis["bottleneck"]["bottleneck_percentage"], direction
    if len(build_state) >= 2:
        return live_bottleneck_and_synergy(build_state)
    return None


def _apply_concierge_action(action: dict | None) -> float | None:
    """Applies a `load_build`/`modify_build`/`navigate`/`save_build`/
    `publish_build`/`open_community_build`/`fix_warnings`/
    `optimize_bottleneck` action immediately — no confirmation step for any
    of them (see this module's docstring for why `save_build`/
    `publish_build`'s real database writes are still safe to fire
    immediately here). `open_community_build` performs no database
    write at all — it only deep-links to an already-shared post.

    `fix_warnings` (spec.md §6.7 intent 11) and `optimize_bottleneck`
    (intent 12) both mutate the active `build_draft` exactly like
    `modify_build` does (same `state.set_component`/`build_draft_analysis`
    invalidation/`page = "create_build"` shape, same authoritative real-total
    return), but neither ever lets the LLM choose the replacement part:
    `fix_warnings` applies `engine.solvers.resolve_compatibility_issues`'s
    own deterministic, catalog-grounded patch; `optimize_bottleneck` applies
    `llm.advisory.get_build_advisory`'s own already-zero-hallucination-
    validated `stretch_budget.actions` — its dedicated "target the
    bottleneck category directly with a real upgrade" recommendation, NOT
    `within_budget.swaps` (a cost-neutral rebalance that downgrades the
    OTHER side and can return no swap at all once that side has no cheaper
    option left — a real, confirmed dead end for an explicit "fix my
    bottleneck" request). Runs in a small, bounded verification loop
    (`_MAX_BOTTLENECK_OPTIMIZE_ATTEMPTS`): after applying one round of
    stretch actions, the live bottleneck is re-checked against the action's
    own `target_percentage` (or `_DEFAULT_BOTTLENECK_TARGET` when the user
    didn't state one) and another round is fetched/applied if still above
    target and further upgrades exist — this project's own architecture rule
    that compatibility (and, here, the deterministic engine's compute-
    balance read) is never LLM-gated (root CLAUDE.md) applies just as much
    to a Concierge-triggered fix as to a manual one. Both are no-ops
    (return `None`, no session-state write) when there's no active draft, or
    (for `optimize_bottleneck`) fewer than 2 components picked yet.

    Returns the authoritative real total cost (`ui.state.build_total_cost`)
    of the resulting build when a `load_build`/`modify_build` action
    successfully lands against a non-empty `build_state`, so the caller can
    append a deterministic, Python-computed total to the assistant's chat
    reply instead of trusting whatever number (if any) the model's own
    `reply` text stated — see this module's docstring's "AUTHORITATIVE
    TOTAL-COST LINE" section. Returns `None` for every other action type
    (`navigate`/`save_build`/`publish_build`/`open_community_build`, where no
    build total is meaningful) and for any no-op branch below (no active
    draft / build resolves to zero real components).

    `load_build` starts a fresh draft — Free-mode using the model's own
    `components` picks verbatim (mirroring `ui/views/community.py::
    _fork_into_studio`'s shape) when the request carried no budget figure, or
    Budget-mode via `engine.solvers.initialize_budget_build` when the action
    carries a real `budget_cap_usd` (spec.md §6.7 intent 3's HARD CEILING /
    TARGET-ZONE ENFORCEMENT rule — see this function's own inline comments):
    in that case the model's `components` are used only as seed pins for any
    part the user explicitly named, never as the final build, since the
    deterministic solver is what actually guarantees the ceiling is never
    exceeded and converges close to it. `modify_build` patches the existing
    draft in place, leaving every unmentioned category untouched, then — when
    the draft carries a real `budget_ceiling` — re-clamps the FULL patched
    selection back under it via that same solver if the patch pushed the
    total over (the BUDGET HARD-CAP SAFETY NET, Part 2: never trust the
    model's own "this fits" arithmetic alone). Every id in
    `action["components"]` is already guaranteed real by `llm/concierge.py`'s
    own zero-hallucination guard, so the defensive
    `components_repo.get_by_id` re-check here is belt-and-suspenders, not
    load-bearing — mirroring `ui/views/create_build.py::_apply_swaps`'s
    identical defensive pattern. Requested quantities are NOT trusted
    verbatim: `llm/concierge.py` has no `engine`/`db` access to clamp them to
    a real physical/budget limit, so this function re-derives the same
    effective ceiling the manual quantity stepper already enforces
    (`ui.state.resolve_effective_quantity_limit`) before ever writing one.

    `save_build` first resolves WHICH build's data to use via
    `action["source"]` (`"studio"`, the default, or `"community"` — see this
    module's own docstring for the full rationale): `"studio"` reads
    `st.session_state["build_draft"]`; `"community"` instead reads
    `action["source_post_id"]`'s `CommunityPost.build` (zero-hallucination-
    guarded against `community_summary` by `llm/concierge.py`), leaving
    `build_draft`/`page`/`has_unsaved_build_changes` completely untouched
    since nothing about the user's own Studio session is being acted on.
    Either way it then persists using `action["destination"]` (one of
    `"draft"`/`"build"` — always present per the now-required schema field,
    read defensively regardless):
      - `"draft"`: persists the resolved build's components/quantities/mode
        via `drafts_repo.save_draft(...)`, the same way every other real
        persist path in this module does. Does NOT set
        `st.session_state["concierge_last_saved_build"]` — a draft has
        nothing for a later `publish_build` action to resolve against, since
        `community_repo.create_post`/`builds_repo.set_public` only ever
        operate on a real `Build` row.
      - `"build"`: persists via the exact same `builds_repo.create_build(...)`
        call shape as `ui/views/create_build.py::_save_actions` (read that
        function first if editing this). For `source == "studio"`,
        `compatibility_score` comes from a fresh `evaluate_build` call and
        `synergy_score`/`bottleneck_percentage` from `build_draft_analysis`
        when present, exactly like the manual flow; for `source ==
        "community"`, all four scores (plus `total_cost`) are copied directly
        from the community post's own already-evaluated `Build` row instead
        of being recomputed — that build was already scored once when it was
        first saved, so reusing its real, persisted numbers is both simpler
        and more accurate than re-deriving them. It always saves privately
        (`is_public=False`) — publishing is a deliberately separate, later
        `publish_build` action/turn, never bundled into the same write. Sets
        `st.session_state["concierge_last_saved_build"]`, since the publish
        flow needs it.
    Both branches use `action["name"]` (the user's own literal answer to the
    Concierge's name question) rather than any auto-generated name —
    `_default_saved_build_name()` is called only as a last-resort defensive
    fallback if `name` somehow arrives empty/missing despite the schema
    requiring it. No-ops (does nothing, does not crash) when there is no
    active draft/resolvable community post, or it resolves to zero real
    components — should be rare, since the SYSTEM_PROMPT tells the model not
    to return this action against nothing, but a defensive guard costs
    nothing. `build_draft`/`create_mode`/`page` are deliberately left
    untouched for BOTH sources (see this module's docstring); only
    `has_unsaved_build_changes` is reset in both destination branches, and
    only for `source == "studio"` — a `"community"` source never set it in
    the first place, so there is nothing of the user's own Studio session to
    reset, matching the manual flow's own post-save state reset.

    `publish_build` resolves WHICH build to publish via
    `st.session_state["concierge_last_saved_build"]` (set by a prior
    `save_build` application in this same session) rather than any id on the
    action itself (the LLM never sees/invents one) — a no-op, not a crash,
    when that key is empty (e.g. this action somehow arrives with nothing
    having been saved this session). Otherwise mirrors
    `_save_actions`'s own `builds_repo.set_public` + `community_repo.
    create_post(build_id, user_id, title, author_notes)` pair exactly, using
    the saved build's own name as `title` (the same sensible default
    `_save_actions` uses: the build's own saved name)."""
    if action is None:
        return None
    action_type = action.get("type")

    if action_type == "navigate":
        page = action.get("navigate_to")
        if page not in ("landing", "create_build", "my_builds", "community", "drafts"):
            return None

        # "reset_mode" mirrors the existing "⬅ Change mode" button in
        # ui/views/create_build.py exactly — only meaningful for
        # navigate_to == "create_build". ui.state.teardown_builder resets
        # create_mode/build_draft/build_draft_analysis (no database write —
        # spec.md §7.9), landing back on the mode-selection screen.
        if page == "create_build" and action.get("reset_mode"):
            state.teardown_builder()
            state.navigate_to_page(page)
            return None

        filters = action.get("filters")
        if filters:
            # A distinctly-named, one-shot staging key — NEVER the same as
            # community.py's own real widget keys (community_mode_filter/
            # community_price_filter/community_domain_filter/
            # community_tier_filter). Those depend on live, currently-shared
            # feed data (real price steps, real domains/tiers) that only
            # ui/views/community.py itself computes at render time, so this
            # module cannot resolve a requested filter into one of those real
            # widget values here — it only stages the raw request for
            # community.py::_apply_pending_community_filters to translate,
            # popping this key, on its very next render (one-shot: it must
            # never keep re-applying itself after the user changes a filter
            # manually).
            st.session_state["pending_community_filters"] = filters

        # Leaving create_build for a DIFFERENT page: reset the builder first,
        # mirroring app.py's sidebar buttons exactly (same condition, same
        # ui.state.teardown_builder call — no database write, spec.md §7.9).
        if st.session_state.get("page") == "create_build" and page != "create_build":
            state.teardown_builder()

        state.navigate_to_page(page)
        return None

    if action_type == "open_community_build":
        # Same builder reset as any other navigate away from create_build
        # (spec.md §7.9, no database write) — deep-linking to a community
        # post is still leaving the builder. `post_id` is already guaranteed
        # real by llm/concierge.py's own zero-hallucination guard
        # (cross-checked against the exact community_summary this module sent
        # it), and ui/views/community.py::render() already checks
        # selected_post_id at the very top of its own function, resolving it
        # into the thread view on this same next render with no further glue
        # code needed here beyond setting these two keys.
        if st.session_state.get("page") == "create_build":
            state.teardown_builder()
        st.session_state["page"] = "community"
        st.session_state["selected_post_id"] = action.get("post_id")
        return None

    if action_type == "load_saved_build":
        # Loading an EXISTING draft/saved-build/community-build into the
        # studio for editing — distinct from load_build (a brand new build
        # from named catalog parts) and open_community_build (a read-only
        # thread view). `id` is already guaranteed real by llm/concierge.py's
        # own zero-hallucination guard, cross-checked against whichever of
        # drafts_summary/previous_builds_summary/community_summary this
        # module sent it for the matching `source`. Same builder reset as
        # any other "enter the studio" transition (spec.md §7.9, no database
        # write) before loading the new content, so an unsaved in-progress
        # build is never silently mixed with what's being loaded.
        if st.session_state.get("page") == "create_build":
            state.teardown_builder()

        source = action.get("source")
        item_id = action.get("id")
        new_draft = None

        if source == "draft":
            draft = drafts_repo.get_draft(item_id)
            if draft is not None:
                new_draft = state.load_components_into_new_draft(
                    mode=draft.mode,
                    components=json.loads(draft.components_json),
                    quantities=json.loads(draft.quantities_json),
                    name=draft.name,
                )
                st.session_state["create_mode"] = draft.mode
        elif source == "build":
            build = builds_repo.get_build(item_id)
            if build is not None:
                new_draft = state.load_components_into_new_draft(
                    mode=build.creation_mode,
                    components={bc.category: bc.component_id for bc in build.components},
                    quantities={bc.category: bc.quantity for bc in build.components},
                    name=f"{build.name} (copy)",
                )
                new_draft["workload_profile"] = build.workload_profile
                new_draft["budget_ceiling"] = build.budget_ceiling
                st.session_state["create_mode"] = build.creation_mode
        else:  # "community"
            post = community_repo.get_post(item_id)
            if post is not None:
                build = post.build
                new_draft = state.load_components_into_new_draft(
                    mode=build.creation_mode or "Free",
                    components={bc.category: bc.component_id for bc in build.components},
                    quantities={bc.category: bc.quantity for bc in build.components},
                    name=f"{build.name} (fork)",
                )
                new_draft["workload_profile"] = build.workload_profile
                new_draft["budget_ceiling"] = build.budget_ceiling
                st.session_state["fork_source_build_id"] = build.id
                st.session_state["create_mode"] = build.creation_mode or "Free"

        if new_draft is None:
            # The zero-hallucination guard already rejects an id that never
            # existed in the summary sent this call, so this only fires for
            # the (should be rare) case of a row deleted between that
            # summary being assembled and this action being applied.
            return None

        st.session_state["build_draft"] = new_draft
        st.session_state["build_draft_analysis"] = None
        st.session_state["page"] = "create_build"
        return None

    if action_type == "save_build":
        user = current_user()
        # Defensive fallback only — the schema requires `name`, so a
        # well-formed action always carries the user's own literal answer.
        name = action.get("name") or _default_saved_build_name()
        destination = action.get("destination")

        if action.get("source") == "community":
            # SOURCE RESOLUTION (llm.schemas.ConciergeSaveBuildAction, SYSTEM_PROMPT
            # intent 7): the user asked to save/clone a build they're VIEWING on the
            # Community page, not their own in-progress Studio build — pull the
            # components/quantities/scores straight from that ALREADY-persisted
            # CommunityPost's own Build row instead of `build_draft`. Deliberately
            # does NOT touch `build_draft`/`page`/`has_unsaved_build_changes` at all:
            # unlike `load_saved_build`, this is a headless "clone it into my drafts/
            # builds" request, not "let me edit it in the Studio" — the user's own
            # in-progress Studio work (if any) must be left completely untouched.
            post = community_repo.get_post(action.get("source_post_id"))
            if post is None:
                # The zero-hallucination guard already rejects a source_post_id that
                # never existed in the community_summary sent this call, so this only
                # fires for the (should be rare) case of a post deleted in between.
                return None
            source_build = post.build
            build_state = state.resolve_build_state(
                {"components": {bc.category: bc.component_id for bc in source_build.components}}
            )
            if not build_state:
                return None
            quantities = {bc.category: bc.quantity for bc in source_build.components}
            creation_mode = source_build.creation_mode or "Free"
            workload_profile = source_build.workload_profile
            workload_tier = source_build.workload_tier
            budget_ceiling = source_build.budget_ceiling
            total_cost = source_build.total_cost
            compatibility_score = source_build.compatibility_score
            synergy_score = source_build.synergy_score
            bottleneck_percentage = source_build.bottleneck_percentage
        else:
            build_draft = st.session_state.get("build_draft")
            if not build_draft:
                return None
            build_state = state.resolve_build_state(build_draft)
            if not build_state:
                return None
            quantities = build_draft.get("quantities", {})
            creation_mode = build_draft.get("creation_mode") or "Free"
            workload_profile = build_draft.get("workload_profile")
            workload_tier = build_draft.get("tier") if creation_mode == "Workload" else None
            budget_ceiling = build_draft.get("budget_ceiling")
            total_cost = state.build_total_cost(build_state, quantities)
            compatibility_score = evaluate_build(build_state, quantities).compatibility_score
            analysis = st.session_state.get("build_draft_analysis") or {}
            synergy_score = analysis.get("synergy", {}).get("overall_score")
            bottleneck_percentage = analysis.get("bottleneck", {}).get("bottleneck_percentage")

        if destination == "draft":
            drafts_repo.save_draft(
                user_id=user["id"],
                name=name,
                mode=creation_mode,
                components={category: component.id for category, component in build_state.items()},
                quantities=quantities,
            )
            # Nothing for a later publish_build action to resolve against —
            # a draft has no publish path anywhere in this app's real
            # architecture, so concierge_last_saved_build is deliberately
            # left untouched (never set, never cleared) here.
            if action.get("source") != "community":
                st.session_state["has_unsaved_build_changes"] = False
            return None

        # destination == "build" (or a malformed/missing value — default to
        # the real, finished-build save path, the historical behavior).
        build = builds_repo.create_build(
            user_id=user["id"],
            name=name,
            creation_mode=creation_mode,
            components=[
                builds_repo.BuildComponentInput(component_id=c.id, quantity=quantities.get(cat, 1))
                for cat, c in build_state.items()
            ],
            total_cost=total_cost,
            compatibility_score=compatibility_score,
            workload_profile=workload_profile,
            workload_tier=workload_tier,
            budget_ceiling=budget_ceiling,
            synergy_score=synergy_score,
            bottleneck_percentage=bottleneck_percentage,
            is_public=False,
        )
        st.session_state["concierge_last_saved_build"] = {"build_id": build.id, "name": name}
        if action.get("source") != "community":
            st.session_state["has_unsaved_build_changes"] = False
        # FAST-TRACK refinement (llm.schemas.ConciergeSaveBuildAction): only
        # ever true when the user's OWN message that triggered this save
        # ALSO explicitly asked to publish/share to Community in the same
        # breath — publishes in this SAME turn, mirroring the SAME
        # builds_repo.set_public + community_repo.create_post pair the
        # separate, later `publish_build` action below uses, so the user
        # never has to wait for a second round-trip they already answered.
        if action.get("publish_immediately"):
            builds_repo.set_public(build.id, True)
            community_repo.create_post(
                build.id, user["id"], name, action.get("author_notes"), flair=action.get("flair"),
            )
        return None

    if action_type == "publish_build":
        saved = st.session_state.get("concierge_last_saved_build")
        if not saved:
            return None
        user = current_user()
        title = saved.get("name") or "Untitled build"
        builds_repo.set_public(saved["build_id"], True)
        community_repo.create_post(
            saved["build_id"], user["id"], title, action.get("author_notes"), flair=action.get("flair"),
        )
        return None

    if action_type in ("load_build", "modify_build"):
        # BUDGET HARD-CAP (spec.md §6.7 intent 3's HARD CEILING / TARGET-ZONE
        # ENFORCEMENT rule): only ever set on a `load_build` action, and only
        # when the user's request stated a real budget/ceiling figure. The
        # model's own `components` picks are NOT trusted for this case's
        # arithmetic (the same "compatibility/budget math is never LLM-gated"
        # rule root CLAUDE.md applies to compatibility) — instead, any
        # explicitly-named parts in `components` are used as fixed seed pins,
        # and `engine.solvers.initialize_budget_build` (already tested to
        # both NEVER exceed a ceiling and converge close to — not just
        # comfortably under — it) deterministically fills/downgrades
        # everything else.
        budget_cap_usd = action.get("budget_cap_usd") if action_type == "load_build" else None
        build_draft = st.session_state.get("build_draft")

        if budget_cap_usd:
            seed_selection = {}
            for category, component_id in action.get("components", {}).items():
                component = components_repo.get_by_id(component_id)
                if component is not None:
                    seed_selection[category] = component
            selection = solvers.initialize_budget_build(
                budget_cap_usd, seed_selection=seed_selection or None, fill_peripherals_with_surplus=True,
            )
            build_draft = state.load_components_into_new_draft(
                mode="Budget",
                components={category: component.id for category, component in selection.items()},
            )
            build_draft["budget_ceiling"] = budget_cap_usd
        else:
            # A modify_build with nothing to modify against starts a fresh Free
            # draft too, same as load_build — a defensive fallback for the (should
            # be rare, since the prompt tells the model not to do this) case of a
            # modify_build arriving with no real active draft.
            if action_type == "load_build" or not build_draft:
                build_draft = state.new_build_draft("Free")

            for category, component_id in action.get("components", {}).items():
                component = components_repo.get_by_id(component_id)
                if component is not None:
                    state.set_component(build_draft, category, component)

            # REMOVE_CATEGORIES (root-cause fix for a real, confirmed
            # failure: `components` has no way to express "clear this
            # category," so a request to remove several peripherals in one
            # message previously made the model try `components: {"Sound
            # Card": null}`, which fails Pydantic validation outright and
            # silently degrades the WHOLE response to the generic "having
            # trouble reaching the AI assistant" fallback — see
            # llm/schemas.py::ConciergeModifyBuildAction's own docstring).
            # Applied AFTER `components` above so a category named in BOTH
            # (a contradiction the model should never produce, but isn't
            # trusted not to) ends up cleared, not re-filled — "clearing
            # wins," per that same docstring.
            for category in action.get("remove_categories", []) or []:
                state.remove_component(build_draft, category)

            requested_quantities = action.get("quantities", {})
            if requested_quantities:
                build_state = state.resolve_build_state(build_draft)
                budget_ceiling = build_draft.get("budget_ceiling")
                for category, requested_qty in requested_quantities.items():
                    effective_max, _reason, _kind = state.resolve_effective_quantity_limit(
                        build_state, category, build_draft.get("quantities", {}), budget_ceiling
                    )
                    clamped = min(requested_qty, effective_max) if effective_max is not None else requested_qty
                    state.set_quantity(build_draft, category, max(1, clamped))

            # BUDGET HARD-CAP SAFETY NET (Part 2 — "the AI is strictly
            # prohibited from exceeding the cap by even 1 unit"): a
            # `modify_build` patch (e.g. the BUDGET-LEEWAY UPGRADE REQUESTS
            # carve-out applying `advisory_context.stretch_budget.actions`)
            # is checked against the SYSTEM_PROMPT's own real-arithmetic
            # BUDGET GUARDRAIL RULE, but that's a prompt instruction, not an
            # enforcement — this re-checks the FINAL patched build's real,
            # quantity-aware total in Python and, only if it's still over a
            # real numeric `budget_ceiling`, re-clamps it via the exact same
            # tested downgrade path the fresh-budget-build branch above uses
            # (passing the full current selection as `seed_selection` so
            # every category — not just the one this patch touched — is a
            # candidate for the downgrade, cheapest/least-impactful swaps
            # first). A no-op when there's no ceiling, or the patch is
            # already within it (the overwhelmingly common case).
            budget_ceiling = build_draft.get("budget_ceiling")
            if budget_ceiling:
                current_state = state.resolve_build_state(build_draft)
                current_total = state.build_total_cost(current_state, build_draft.get("quantities", {}))
                if current_state and current_total > budget_ceiling:
                    clamped_state = solvers.initialize_budget_build(budget_ceiling, seed_selection=current_state)
                    for category, component in clamped_state.items():
                        state.set_component(build_draft, category, component)

        st.session_state["build_draft"] = build_draft
        st.session_state["create_mode"] = build_draft.get("creation_mode") or "Free"
        st.session_state["build_draft_analysis"] = None
        st.session_state["page"] = "create_build"

        # Authoritative total, re-derived from the FINAL build_draft (after
        # any component swaps AND any clamped quantity writes above) — never
        # trust a total the model might have stated in its own `reply`. Empty
        # build_state (should be rare/defensive-only, mirroring every other
        # no-op guard in this function) means there is nothing to report.
        final_build_state = state.resolve_build_state(build_draft)
        if not final_build_state:
            return None
        return state.build_total_cost(final_build_state, build_draft.get("quantities", {}))

    if action_type == "fix_warnings":
        build_draft = st.session_state.get("build_draft")
        if not build_draft:
            return None
        build_state = state.resolve_build_state(build_draft)
        if not build_state:
            return None
        quantities = build_draft.get("quantities", {})
        # The only deterministic, catalog-grounded resolver for this — never
        # an LLM-chosen part (spec.md §6.7 intent 11, engine/CLAUDE.md's own
        # "compatibility is never LLM-gated" rule). A no-op patch (already
        # compatible, or nothing in the catalog can resolve it) still falls
        # through to the same real-total return below, matching every other
        # no-op guard in this function.
        patch = solvers.resolve_compatibility_issues(build_state, quantities)
        for category, component_id in patch.items():
            component = components_repo.get_by_id(component_id)
            if component is not None:
                state.set_component(build_draft, category, component)

        st.session_state["build_draft"] = build_draft
        st.session_state["page"] = "create_build"
        st.session_state["build_draft_analysis"] = None

        final_build_state = state.resolve_build_state(build_draft)
        if not final_build_state:
            return None
        return state.build_total_cost(final_build_state, build_draft.get("quantities", {}))

    if action_type == "optimize_bottleneck":
        build_draft = st.session_state.get("build_draft")
        if not build_draft:
            return None
        build_state = state.resolve_build_state(build_draft)
        if len(build_state) < 2:
            return None
        mode = build_draft.get("creation_mode") or "Free"
        quantities = build_draft.get("quantities", {})
        current_budget_or_cost = build_draft.get("budget_ceiling") if mode == "Budget" else None
        if not current_budget_or_cost:
            current_budget_or_cost = state.build_total_cost(build_state, quantities)

        target_percentage = action.get("target_percentage")
        if target_percentage is None:
            target_percentage = _DEFAULT_BOTTLENECK_TARGET

        # BOTTLENECK-TARGETING FIX (root-cause fix for "AI fails to reduce
        # bottleneck" / "hallucinates irrelevant swaps"): `within_budget.
        # swaps` (used here previously) is a cost-neutral REBALANCE — it
        # downgrades the NON-bottlenecked side to fund a modest paired
        # upgrade of the bottlenecked one, and returns NO swap at all once
        # that non-bottlenecked side has no cheaper compatible option left —
        # a real, confirmed dead end that left an explicit "fix my
        # bottleneck" request doing nothing. `stretch_budget.actions` is
        # llm.advisory's own dedicated "target the bottleneck category
        # directly with a real upgrade" recommendation for Free mode's own
        # stated objective (llm/CLAUDE.md) — the correct one to apply here,
        # never the rebalance-only swap. Runs in a bounded retry loop: after
        # applying one round of upgrades, re-check the LIVE bottleneck
        # against `target_percentage` and, if still above it and a further
        # real stretch upgrade exists, fetch and apply one more — capped at
        # _MAX_BOTTLENECK_OPTIMIZE_ATTEMPTS so a build that's genuinely
        # peaked at both CPU and GPU tiers degrades gracefully instead of
        # looping or spamming LLM calls indefinitely.
        for _ in range(_MAX_BOTTLENECK_OPTIMIZE_ATTEMPTS):
            advisory = get_build_advisory(
                build_state, mode, current_budget_or_cost,
                profile=build_draft.get("workload_profile"),
                bottleneck_info=(st.session_state.get("build_draft_analysis") or {}).get("bottleneck"),
                quantities=quantities,
            )
            actions = advisory["stretch_budget"]["actions"]
            if not actions:
                break

            for stretch_action in actions:
                if stretch_action.get("action") == "set_quantity":
                    category = stretch_action.get("category")
                    effective_max, _reason, _kind = state.resolve_effective_quantity_limit(
                        build_state, category, quantities,
                        current_budget_or_cost if mode == "Budget" else None,
                    )
                    requested_qty = stretch_action.get("quantity", 1)
                    clamped_qty = min(requested_qty, effective_max) if effective_max is not None else requested_qty
                    state.set_quantity(build_draft, category, max(1, clamped_qty))
                else:
                    component = components_repo.get_by_id(stretch_action.get("replace_with_id"))
                    if component is not None:
                        state.set_component(build_draft, stretch_action.get("category"), component)

            build_state = state.resolve_build_state(build_draft)
            quantities = build_draft.get("quantities", {})

            # BUDGET HARD-CAP SAFETY NET (Part 2 precedent, applied here too):
            # llm.advisory's own Budget-mode objective already keeps
            # stretch_budget within remaining_budget, but this loop never
            # trusts that alone — if a round of stretch actions somehow still
            # pushed the total over a real ceiling, re-clamp deterministically
            # via the same tested solver before continuing.
            if mode == "Budget":
                new_total = state.build_total_cost(build_state, quantities)
                if new_total > current_budget_or_cost:
                    clamped_state = solvers.initialize_budget_build(
                        current_budget_or_cost, seed_selection=build_state,
                    )
                    for category, component in clamped_state.items():
                        state.set_component(build_draft, category, component)
                    build_state = state.resolve_build_state(build_draft)
                    break

            live = live_bottleneck_and_synergy(build_state) if len(build_state) >= 2 else None
            if live is None or live[1] <= target_percentage:
                break

        st.session_state["build_draft"] = build_draft
        st.session_state["page"] = "create_build"
        st.session_state["build_draft_analysis"] = None

        final_build_state = state.resolve_build_state(build_draft)
        if not final_build_state:
            return None
        return state.build_total_cost(final_build_state, build_draft.get("quantities", {}))

    if action_type == "use_remaining_budget":
        build_draft = st.session_state.get("build_draft")
        if not build_draft:
            return None

        # BUDGET CEILING RESOLUTION: a fresh figure the user stated THIS
        # message (action["budget_cap_usd"]) is ONLY ever adopted when NO
        # real ceiling already exists on the draft (e.g. "you have 13000
        # NIS, upgrade it accordingly" against a build that was never in
        # Budget mode at all) — a real, confirmed gap this fixes: a Free-mode
        # build had no way to engage this multi-tier spend-down loop at all
        # before, even when the user explicitly stated a budget to build
        # toward in the same breath as "upgrade it". When a real ceiling
        # ALREADY exists, `budget_cap_usd` is NEVER trusted to replace it,
        # authoritatively in Python, no matter what the LLM sent (belt-and-
        # suspenders alongside llm/concierge.py's own corrected prompt
        # instruction): a live call was caught setting `budget_cap_usd` to a
        # user's STATED LEEWAY figure ("you have 1,864.68 EUR leeway, use
        # them to upgrade") — describing headroom UNDER an existing €4,000
        # ceiling, not a replacement for it — which overwrote that real
        # ceiling down to barely more than the build's own current cost,
        # making the very first `remaining_headroom` check below negative
        # and breaking the loop immediately with `Delta: +€0.00`, the exact
        # opposite of what "you have leeway" asked for.
        existing_ceiling = build_draft.get("budget_ceiling") if build_draft.get("creation_mode") == "Budget" else None
        stated_ceiling = action.get("budget_cap_usd")
        budget_ceiling = existing_ceiling or stated_ceiling
        if not budget_ceiling:
            return None
        if build_draft.get("creation_mode") != "Budget":
            build_draft["creation_mode"] = "Budget"
            build_draft["budget_ceiling"] = budget_ceiling
        mode = "Budget"
        build_state = state.resolve_build_state(build_draft)
        if not build_state:
            return None
        quantities = build_draft.get("quantities", {})

        # DETERMINISTIC MULTI-TIER UPGRADE (root-cause fix for "AI math
        # hallucinations" — a real, confirmed failure in both directions:
        # falsely rejecting an affordable upgrade, and separately, spending
        # only a small fraction of real remaining headroom). The model never
        # computes "does this still fit?" itself — every round below calls
        # `llm.advisory.get_build_advisory`'s OWN Budget-mode objective (which
        # already keeps every proposal within its own computed
        # `remaining_budget`, per llm/advisory.py's SYSTEM_PROMPT) and applies
        # its `stretch_budget.actions` (the SAME priority-ordered CPU/GPU ->
        # RAM -> Storage -> Cooler chain `optimize_bottleneck` above reuses),
        # re-deriving the REAL remaining headroom in Python after every round
        # via `state.build_total_cost`, never trusting a stated figure.
        # Bounded at `_MAX_BUDGET_UTILIZATION_ATTEMPTS` rounds (more than the
        # bottleneck loop's cap, since fully using a larger headroom
        # realistically spans several tiers — RAM, then Storage, then
        # Cooler — not just one or two) so a build that's already
        # genuinely maxed out (every category peaked) degrades gracefully
        # instead of looping forever.
        for _ in range(_MAX_BUDGET_UTILIZATION_ATTEMPTS):
            current_total = state.build_total_cost(build_state, quantities)
            remaining_headroom = budget_ceiling - current_total
            if remaining_headroom <= 0:
                break

            advisory = get_build_advisory(
                build_state, mode, budget_ceiling,
                profile=build_draft.get("workload_profile"),
                bottleneck_info=(st.session_state.get("build_draft_analysis") or {}).get("bottleneck"),
                quantities=quantities,
            )
            stretch = advisory["stretch_budget"]
            actions = stretch["actions"]
            # No further real upgrade fits anywhere in the priority chain, or
            # advisory proposed something that wouldn't actually spend any
            # more (no progress) — stop rather than loop with no effect.
            if not actions or not stretch.get("added_cost_usd"):
                break

            for stretch_action in actions:
                if stretch_action.get("action") == "set_quantity":
                    category = stretch_action.get("category")
                    effective_max, _reason, _kind = state.resolve_effective_quantity_limit(
                        build_state, category, quantities, budget_ceiling,
                    )
                    requested_qty = stretch_action.get("quantity", 1)
                    clamped_qty = min(requested_qty, effective_max) if effective_max is not None else requested_qty
                    state.set_quantity(build_draft, category, max(1, clamped_qty))
                else:
                    component = components_repo.get_by_id(stretch_action.get("replace_with_id"))
                    if component is not None:
                        state.set_component(build_draft, stretch_action.get("category"), component)

            build_state = state.resolve_build_state(build_draft)
            quantities = build_draft.get("quantities", {})

            # BUDGET HARD-CAP SAFETY NET (Part 2 precedent, applied here too):
            # never trust llm.advisory's own remaining_budget arithmetic
            # alone — re-clamp deterministically if a round somehow still
            # pushed the total over the real ceiling.
            new_total = state.build_total_cost(build_state, quantities)
            if new_total > budget_ceiling:
                clamped_state = solvers.initialize_budget_build(budget_ceiling, seed_selection=build_state)
                for category, component in clamped_state.items():
                    state.set_component(build_draft, category, component)
                build_state = state.resolve_build_state(build_draft)
                break

        st.session_state["build_draft"] = build_draft
        st.session_state["create_mode"] = build_draft.get("creation_mode") or "Free"
        st.session_state["page"] = "create_build"
        st.session_state["build_draft_analysis"] = None

        final_build_state = state.resolve_build_state(build_draft)
        if not final_build_state:
            return None
        return state.build_total_cost(final_build_state, build_draft.get("quantities", {}))

    if action_type == "rebalance_budget":
        # DETERMINISTIC REBALANCE (root-cause fix for "downgrade X and use
        # the money to upgrade Y/Z" — a real, confirmed failure: the model
        # was previously trusted to invent both the specific downgrade part
        # AND the specific upgrade part(s) AND the price arithmetic
        # connecting them, and did so wildly inconsistently — sometimes
        # leaving the named upgrade categories untouched despite real freed
        # cash, sometimes downgrading far more than any upgrade recouped.
        # The Concierge's only job now is CATEGORY recognition (see
        # llm/concierge.py intent 14); engine.solvers.rebalance_budget does
        # the actual, real, catalog-priced downgrade-then-upgrade math.
        build_draft = st.session_state.get("build_draft")
        if not build_draft:
            return None
        build_state = state.resolve_build_state(build_draft)
        downgrade_category = action.get("downgrade_category")
        upgrade_categories = action.get("upgrade_categories") or []
        if not build_state or not downgrade_category or downgrade_category not in build_state:
            return None
        # Free/Workload mode has no real ceiling to respect — treat as
        # "no budget constraint" (every upgrade candidate fits), the same
        # philosophy Free mode's own part-picker already applies elsewhere.
        budget_ceiling = (
            build_draft.get("budget_ceiling") if build_draft.get("creation_mode") == "Budget" else None
        )
        effective_ceiling = budget_ceiling if budget_ceiling else float("inf")
        new_state = solvers.rebalance_budget(
            build_state, downgrade_category, upgrade_categories, effective_ceiling,
        )
        for category, component in new_state.items():
            state.set_component(build_draft, category, component)
        st.session_state["build_draft"] = build_draft
        st.session_state["build_draft_analysis"] = None
        final_build_state = state.resolve_build_state(build_draft)
        if not final_build_state:
            return None
        return state.build_total_cost(final_build_state, build_draft.get("quantities", {}))

    return None


def render_concierge_widget() -> None:
    """Called from app.py's sidebar, only when a user is authenticated."""
    with st.expander("💬 AI Concierge & Site Navigator", expanded=True):
        # Renders the FULL conversation history (never sliced/dropped here —
        # a deliberate reversal of an earlier round's display-only [-4:]
        # slice, which made older turns permanently unreachable). The fixed
        # height + overflow-y: auto (via st.container(height=...)) is what
        # keeps a long conversation from pushing the sidebar's nav buttons/
        # Logout off-screen — roughly the last ~4 message bubbles fit in the
        # visible 380px viewport at once, but scrolling up within this same
        # container reveals every earlier turn. This still does NOT trim
        # st.session_state["concierge_messages"] itself — the full history
        # stays intact for conversation_history construction below (which
        # does its own independent [-_MAX_HISTORY_MESSAGES:] slice off this
        # same untouched full list, a separate concern: bounding LLM token
        # cost, not display).
        with st.container(height=380):
            for message in st.session_state["concierge_messages"]:
                with st.chat_message(message["role"]):
                    _render_chat_message(message["content"])

        user_input = st.chat_input(
            "Ask about parts, request a build, or navigate (English only)...",
            key="concierge_chat_input",
        )
        if user_input:
            st.session_state["concierge_messages"].append({"role": "user", "content": user_input})
            history = [
                {"role": m["role"], "content": m["content"]}
                for m in st.session_state["concierge_messages"][:-1]
            ][-_MAX_HISTORY_MESSAGES:]
            with st.spinner("Thinking..."):
                result = get_concierge_response(
                    user_input,
                    history,
                    _catalog_summary(),
                    _community_summary(),
                    current_build_context=_current_build_context(),
                    advisory_context=_advisory_context(user_input),
                    drafts_summary=_drafts_summary(),
                    previous_builds_summary=_previous_builds_summary(),
                    current_page=st.session_state.get("page"),
                    viewed_post_id=(
                        st.session_state.get("selected_post_id")
                        if st.session_state.get("page") == "community"
                        else None
                    ),
                    active_currency=_active_currency(),
                    currency_rates=CURRENCY_RATES,
                )
            # RIGOROUS TELEMETRY REPLY FORMAT — snapshot the BEFORE state for
            # the 3 action types that patch an EXISTING build (modify_build/
            # fix_warnings/optimize_bottleneck have a meaningful "before"; a
            # brand-new load_build does not) so the delta-reporting block
            # below can state a real before -> after bottleneck/synergy/cost
            # change — never trusting the model's own arithmetic for any of
            # these numbers, the same discipline as the pre-existing
            # AUTHORITATIVE TOTAL-COST LINE this extends.
            pending_action_type = (result.get("action") or {}).get("type")
            before_build_draft = (
                st.session_state.get("build_draft")
                if pending_action_type in (
                    "modify_build", "fix_warnings", "optimize_bottleneck", "use_remaining_budget",
                    "rebalance_budget",
                )
                else None
            )
            before_build_state = state.resolve_build_state(before_build_draft) if before_build_draft else {}
            before_total = (
                state.build_total_cost(before_build_state, before_build_draft.get("quantities", {}))
                if before_build_state
                else None
            )
            before_live = _read_cached_analysis(before_build_state) if before_build_state else None

            # Apply BEFORE finalizing the assistant message so the real,
            # Python-computed total (if any) is available to append to the
            # SAME message the user sees — see this module's docstring's
            # "AUTHORITATIVE TOTAL-COST LINE" section for why this must never
            # be the number (if any) the model itself typed in `reply`.
            #
            # Unlike llm/concierge.py's own get_concierge_response (which has
            # a broad `except Exception` fallback of its own), this function
            # had NO safety net: any genuinely unexpected failure here (a real
            # bug, a future regression, an unforeseen None somewhere) would
            # propagate uncaught and crash the entire Streamlit rerun with a
            # raw error screen. This mirrors that module's own "never-raise,
            # but never silently swallow either" convention — log the full
            # traceback so a real bug is never silently invisible, then
            # degrade to a plain, honest note on the reply instead of
            # crashing the page. The happy path below (the vast majority of
            # calls) is completely unaffected: this only changes behavior
            # when `_apply_concierge_action` itself raises.
            try:
                real_total = _apply_concierge_action(result.get("action"))
            except Exception:
                traceback.print_exc(file=sys.stderr)
                real_total = None
                result = dict(result)
                result["reply"] = (
                    f"{result['reply']}\n\n_(Note: something went wrong applying this action — "
                    "your build may not have updated as expected.)_"
                )
            # CURRENCY SWITCH (spec.md §6.7/§7.10): a `currency_switch` response
            # field is a SEPARATE, top-level field from `action` — set whenever the
            # user's message explicitly names a currency, whether attached to a
            # budget ("build me a PC for 10000 NIS") or standalone ("switch to
            # NIS"). Only ever a real, already-active-currency-code-shaped value
            # (Pydantic's own Literal type on ConciergeResponse rejects anything
            # else at parse time) — never re-staged when it already matches what's
            # active (a no-op, not an error). Staged into a one-shot key
            # (`app.py::pending_currency_switch`) rather than written directly to
            # `st.session_state["selected_currency"]` here: THIS widget already
            # rendered earlier in the current script pass (it lives above the nav
            # buttons in the sidebar, `app.py`), so writing its own key now would
            # raise StreamlitWidgetAlreadyInstantiatedError — the exact same class
            # of gotcha `ui/components/part_picker.py`'s quantity-stepper works
            # around. The unconditional `st.rerun()` at the end of this block
            # already picks the staged value up on the very next pass, BEFORE that
            # widget re-instantiates, so the sidebar selector and every price on
            # screen switch immediately with no extra plumbing needed.
            currency_switch = result.get("currency_switch")
            target_currency = _active_currency()
            if currency_switch and currency_switch != target_currency:
                st.session_state["pending_currency_switch"] = currency_switch
                target_currency = currency_switch
            # A bare "switch currency" request (no other action, e.g. "switch to
            # NIS" or "I asked it to be in NIS") still gets the authoritative total
            # line restated in the NEWLY active currency, for an existing build —
            # `_apply_concierge_action` only ever computes `real_total` for a
            # `load_build`/`modify_build` action, so this covers the case where
            # `action` is `None` but a build is already active. Deliberately
            # scoped to `action is None` only (never `navigate`/`save_build`/etc.)
            # so those action types keep their own "never gets a total line"
            # guarantee even when combined with a currency switch.
            if real_total is None and result.get("action") is None and currency_switch:
                build_draft = st.session_state.get("build_draft")
                if build_draft:
                    build_state = state.resolve_build_state(build_draft)
                    if build_state:
                        real_total = state.build_total_cost(build_state, build_draft.get("quantities", {}))
            reply_content = result["reply"]
            action_dict = result.get("action") or {}
            applied_action_type = action_dict.get("type")

            # AUTHORITATIVE PUBLISH CONFIRMATION (a real, confirmed bug this
            # fixes): the model's own `reply` text composes the "Build 'X'
            # successfully published..." confirmation itself, and a live
            # reproduction showed it echoing an EXAMPLE build name from deep
            # inside its own SYSTEM_PROMPT ("Beast Rig" — used repeatedly
            # there as an illustrative name) instead of the REAL name the
            # user actually gave, for a build that was in fact saved and
            # published correctly under the right name — the model's stated
            # name was wrong, not the underlying data. The same "never trust
            # what the model typed, restate the Python-computed truth"
            # discipline this module already applies to Total/Bottleneck/
            # Synergy applies here too: whenever a `publish_build` action (or
            # a one-turn `save_build` fast-track with `publish_immediately`
            # AND a `flair` both set) actually completed — signalled by
            # `concierge_last_saved_build` being populated, the SAME
            # real-name/real-id record `_apply_concierge_action` itself just
            # wrote — this REPLACES the model's own reply outright (not an
            # appended line: an outright wrong name in the primary
            # confirmation sentence is actively misleading, unlike a merely
            # missing supplementary total) with the exact 2-line format,
            # substituting the REAL name from that same record (the name
            # actually used in the real `builds_repo.create_build`/
            # `community_repo.create_post` calls) and the REAL flair the
            # action carried — never anything the model composed. Gated on
            # `concierge_last_saved_build` being truthy so a genuine no-op
            # (nothing was actually saved this session) is never overwritten
            # with a fabricated success message either.
            saved_build = st.session_state.get("concierge_last_saved_build")
            if saved_build and (
                (applied_action_type == "publish_build" and action_dict.get("flair"))
                or (
                    applied_action_type == "save_build"
                    and action_dict.get("publish_immediately")
                    and action_dict.get("flair")
                )
            ):
                reply_content = (
                    f"Build '{saved_build['name']}' successfully published to Community "
                    f"under '{action_dict['flair']}'!\nViewable now in Community & Your Posts."
                )

            # RIGOROUS TELEMETRY REPLY FORMAT: for load_build/modify_build/
            # fix_warnings/optimize_bottleneck/use_remaining_budget, replace
            # the plain "Total: ..." line below with a concise, fully
            # Python-computed technical summary — bottleneck/synergy (before
            # -> after for the 4 that patch an EXISTING build, or just the
            # real final reading for a brand-new load_build, which has no
            # "before" to diff against), plus a cost delta (or, again, just
            # the real final total for load_build) — instead of just a bare
            # new total. Currency-safe by construction: every number here is
            # run through `format_currency(..., target_currency)`, the same
            # function every other price in this app already goes through,
            # so a NIS/EUR session can never see a stray "$" — the model's
            # own reply text is never trusted for any of these numbers (the
            # NO TELEMETRY HALLUCINATION RULE, llm/concierge.py SYSTEM_PROMPT,
            # explicitly forbids it from stating one at all — a real,
            # confirmed failure previously showed the model claiming a
            # Synergy/Bottleneck reading that visibly disagreed with the
            # Build Studio's own HUD; this line is the one the user should
            # trust, same "never trust what the model typed, restate the
            # Python-computed truth" discipline the old AUTHORITATIVE
            # TOTAL-COST LINE / AUTHORITATIVE FIX-STATUS LINE this replaces
            # already followed).
            if (
                applied_action_type in (
                    "load_build", "modify_build", "fix_warnings", "optimize_bottleneck", "use_remaining_budget",
                    "rebalance_budget",
                )
                and real_total is not None
            ):
                build_draft = st.session_state.get("build_draft")
                build_state = state.resolve_build_state(build_draft) if build_draft else {}
                lines: list[str] = []
                if applied_action_type == "fix_warnings":
                    remaining = evaluate_build(build_state, (build_draft or {}).get("quantities", {})).issues
                    lines.append(
                        "0 compatibility warnings remaining."
                        if not remaining
                        else f"{len(remaining)} warning(s) still remaining: {remaining[0]}"
                    )
                after_live = _sync_authoritative_analysis(build_draft or {}, build_state)
                if before_live is not None and after_live is not None:
                    lines.append(
                        f"Bottleneck: {before_live[1]:.0f}% -> {after_live[1]:.0f}% ({after_live[2]}) | "
                        f"Synergy: {before_live[0]:.0f} -> {after_live[0]:.0f}"
                    )
                elif before_live is None and after_live is not None:
                    # load_build (a brand-new build has no "before" to diff
                    # against) — the real, Python-computed final reading only,
                    # never anything the model's own reply might have guessed.
                    lines.append(
                        f"Bottleneck: {after_live[1]:.0f}% ({after_live[2]}) | Synergy: {after_live[0]:.0f}"
                    )
                elif applied_action_type == "optimize_bottleneck" and after_live is None:
                    lines.append("Bottleneck unavailable.")
                if before_total is not None:
                    delta = real_total - before_total
                    sign = "+" if delta >= 0 else "-"
                    lines.append(
                        f"Delta: {sign}{format_currency(abs(delta), target_currency)} | "
                        f"Total: {format_currency(real_total, target_currency)}"
                    )
                else:
                    lines.append(f"Total: {format_currency(real_total, target_currency)}")
                reply_content = f"{reply_content}\n\n" + "\n\n".join(f"**{line}**" for line in lines)
            elif real_total is not None:
                reply_content = f"{reply_content}\n\n**Total: {format_currency(real_total, target_currency)}**"
            st.session_state["concierge_messages"].append(
                {"role": "assistant", "content": sanitize_markdown(reply_content)}
            )
            st.rerun()
