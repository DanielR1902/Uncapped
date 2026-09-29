# Uncapped — Spec-Driven PC Hardware Architecture Engine

![Streamlit](https://img.shields.io/badge/Streamlit-1.38%2B-FF4B4B?logo=streamlit&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![SQLite](https://img.shields.io/badge/Database-SQLite%20%2F%20PostgreSQL-003B57?logo=sqlite&logoColor=white)
![OpenRouter](https://img.shields.io/badge/AI-OpenRouter-00e5ff)
![Tests](https://img.shields.io/badge/tests-660%2B%20passing-00f090)

> Deterministic compatibility, budget-safe build solving, and AI-assisted optimization for every PC build.

---

## 1. Overview

**Uncapped** is a PC configuration platform built with **Spec-Driven Development (SDD)**. Instead of letting a user (or an LLM) guess whether a set of parts works together, every build is checked by a deterministic rules engine before it is ever presented as valid:

- CPU↔Motherboard socket, RAM type, Case/Motherboard/PSU form-factor fit
- Cooler socket support and physical clearance (GPU length, cooler height)
- PSU wattage headroom against real component TDP
- RAM/Storage slot and module capacity, including multi-unit quantities

**Compatibility is never LLM-gated.** The LLM (via OpenRouter) explains, converses and proposes; only the engine decides pass/fail, scores synergy/bottleneck, and enforces budgets. Every AI feature degrades to a deterministic heuristic (tagged `source: "heuristic"`) when the model is unavailable, so the app is fully usable offline.

`intent.txt` holds the original product intent, `spec.md` is the always-current source of truth, and each package (`ui/`, `engine/`, `db/`, `llm/`, `auth/`) has a scoped `CLAUDE.md` describing its responsibilities and import boundaries. Behavior changes update `spec.md` in the same change.

## 2. Features

### Build Studio
- **Build modes** — *Budget* (a ceiling; the solver spends it on the highest-tier compatible parts), *Workload* (tiered baselines for Gaming, Video Editing, etc.), *Free* (hand-pick anything compatible), and *Concierge-assisted* drafting (build or edit through chat; the result lands in the same Studio and follows the same rules).
- **Live telemetry** — compatibility, synergy score and CPU/GPU bottleneck are computed deterministically as you pick; power headroom is shown against the same constants the compatibility rules use.
- **Hard budget ceiling** — with "No limit" unchecked, the ceiling is never exceeded. In the part picker, a part that would push the total over is disabled ("Exceeds budget ceiling") and is re-checked when clicked (rejected with a toast, build untouched). RAM/Storage quantities are counted. Nothing is silently downgraded to make room.
- **Advisory panel** ("✨ Get AI Analysis & Upgrade Path") with two optimization tracks:
  - **In-Budget Optimization** — swaps that improve the build within the current ceiling. In Budget mode, unused headroom under the *current* ceiling is offered as higher-tier CPU/GPU upgrades.
  - **Stretch Budget Upgrades** — beyond the ceiling, limited to core parts: **CPU, GPU, and atomic CPU + Motherboard platform upgrades** (a stronger CPU on a new socket together with the cheapest compatible board, evaluated as one unit). RAM capacity, Storage, Case and Cooler stay manual.
- **Monotonic Improvement Gating** — every candidate (LLM-proposed or heuristic) is simulated on a copy of the build and accepted only if compatibility warnings are zero (or fewer), synergy does not drop, bottleneck does not rise, and at least one strictly improves (a higher CPU+GPU performance tier also counts when the other two are unchanged). Degrading, lateral, duplicate, no-op and incompatible swaps are discarded and the explanation is regenerated to match, so the advisory never suggests a swap the button can't perform, never loops, and never regresses.
- **Mutual exclusion & depletion states** — identical in every mode and driven by the same rule:
  - In-Budget swaps available → In-Budget active, Stretch disabled ("Cannot apply stretch upgrades while in-budget optimizations are still available.").
  - In-Budget depleted → In-Budget disabled ("…already fully optimized for this budget. Consider stretching the budget."), Stretch active if it has valid improvements.
  - Both depleted → both disabled ("Increasing budget will not yield further performance or synergy improvements with available parts.").
  - Stale advisories are invalidated on every build or ceiling change, and a proposal for a part already selected is pruned.
- **Budget ceiling input** is a standard keyed widget that stays in sync with the build (including programmatic updates from chat) without Streamlit state-conflict warnings.

### AI Concierge (chat assistant)
- **Natural-language actions** — build a PC (a stated budget such as "3k" or "3000 USD" is verified against what you typed and becomes the exact ceiling), patch the active build, fix warnings, optimize the bottleneck (in-place swaps first, then an automatic full-platform rebuild if targets aren't reachable), navigate, and browse/load/save/publish builds. Every catalog id the model returns is re-validated against the real catalog before use.
- **Budget changes with active rebalancing** — "lower the budget to 3k" downgrades the least-harmful parts until the build fits; "raise it to 5k, make upgrades" spends the new headroom on real upgrades. The ceiling, the input box and the advisory state update together, and the reply reports the actual changes and new total (or that it is still over budget) — never "no changes" while violating the ceiling.
- **Pure additive attachments** — "add a monitor" (or any peripheral/extra) only touches that slot; all other parts are verified unchanged. If the addition would exceed the ceiling it is refused whole ("Adding this … would bring the total to …, which exceeds your budget ceiling of …. Please raise the budget first."), never squeezed in by downgrading other parts.
- **Truthful replies** — a Python-computed "Changes:" line (added / replaced / removed parts, quantity changes, cost delta) accompanies every mutation, so model prose can't misreport what happened.
- **Resilient LLM I/O** — requests set an explicit `max_tokens`; malformed, non-JSON or truncated responses (`finish_reason: length`) are retried with exponential backoff, then passed through a conservative JSON repair that must still pass schema and zero-hallucination validation, and only then fall back to the deterministic reply. Network calls also retry transient connection/DNS failures.
- **Save & Publish state machine** — save as a *Draft* or a *finished Build* (name + destination), then optionally publish to the Community. After the save, every step is resolved deterministically in Python: "yes" moves to the tag question (never repeats the first prompt); choose **Rate My Build**, **Looking for Help**, or **opt out** ("neither"/"no tag"/"skip" publishes untagged); then add a description — type your own, answer "no" for none, or ask the assistant to write one ("write a random description"), which generates a concise 1–2 sentence description grounded in the build's parts instead of publishing your prompt.

### Catalog & Community
- 8 core categories (CPU, Motherboard, GPU, RAM, Storage, PSU, Case, Cooler) and 7 budget-aware peripheral categories (Monitor, Keyboard, Mouse, Headset, Network Card, Sound Card, Optical Drive), spanning multiple hardware generations.
- Community feed with independent **Build Type** and **Tag** filters, threaded nested comments, and a seeded demo dataset (70 builds, 30 published posts) for immediate exploration.
- Multi-currency display (USD / EUR / NIS); stored prices and budgets are always USD.
- "Blueprint / Steel-Navy" theme from a single token module (`ui/theme.py`).

## 3. Architecture & Tech Stack

| Layer | Choice | Notes |
|---|---|---|
| Frontend | **Streamlit** ≥ 1.38 | Session-state router (`ui/router.py`), not native `st.Page`, so the landing page can hard-gate on auth. |
| Database | **SQLite** locally / **PostgreSQL** in the cloud | SQLAlchemy Core; swap `DATABASE_URL`. No SQLite-only SQL, no string-interpolated queries. |
| Auth | **bcrypt** | Used directly; passwords are never logged or stored in plaintext. |
| AI / LLM | **OpenRouter** via **httpx** | Isolated in `llm/`; model id from `OPENROUTER_MODEL`. |
| Validation | **pydantic** | Structured LLM output schemas. |
| Testing | **pytest** | See §5. |
| Methodology | **Spec-Driven Development** | `intent.txt` → `spec.md` → per-package `CLAUDE.md`. |

**Module boundaries are load-bearing:**

```
ui     → auth, engine, db.repositories, llm
llm    → engine, db.repositories (llm_cache only)
engine → db.repositories (read-only)
auth   → db.repositories (users_repo only)
db     → nothing above it
```

`engine/` is pure Python (no Streamlit, network or LLM imports) and holds all compatibility, scoring and solver math — budget solving, headroom upgrades, budget-fitting, and the monotonic gate primitives. `llm/` interprets and proposes but never decides compatibility; `ui/` contains no SQL or scoring math.

## 4. Local Installation & Setup

```bash
# 1. Create and activate a virtual environment (Windows shown; use `source venv/bin/activate` on macOS/Linux)
python -m venv venv
venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment variables in a .env file at the project root:
#    DATABASE_URL=sqlite:///db/uncapped.db     (optional; defaults to the local SQLite file)
#    OPENROUTER_API_KEY=your_openrouter_key
#    OPENROUTER_MODEL=google/gemini-2.5-flash
#    The app runs fully without an OpenRouter key: AI features fall back to deterministic heuristics.

# 4. Launch
streamlit run app.py
```

The app opens at `http://localhost:8501`. On first run it creates the schema and seeds the catalog automatically. Never commit `.env`.

## 5. Testing

```bash
pytest                                  # full suite (660+ tests, ~10 minutes)
pytest tests/test_advisory_gate.py      # a single file for fast feedback
```

The suite covers the compatibility engine and solvers, the monotonic advisory gate and stretch/platform-upgrade scope, budget synchronization, hard-ceiling enforcement and rebalancing, pure additive mutations and change diffing, LLM response resilience (truncation, retry, repair), the Save & Publish state machine, and button states across every build mode. `engine/` and `db/` tests are pure unit tests, `llm/` tests mock the HTTP layer (no live OpenRouter calls), and UI behavior is verified with Streamlit's `AppTest` harness.

---

*See [`spec.md`](spec.md) for the full technical specification and [`intent.txt`](intent.txt) for the original product intent.*
