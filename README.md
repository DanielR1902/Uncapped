# Uncapped — Autonomous Spec-Driven PC Hardware Architecture Engine

![Streamlit](https://img.shields.io/badge/Streamlit-1.38%2B-FF4B4B?logo=streamlit&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![SQLite](https://img.shields.io/badge/Database-SQLite%20%2F%20PostgreSQL-003B57?logo=sqlite&logoColor=white)
![OpenRouter](https://img.shields.io/badge/AI-OpenRouter-00e5ff)
![UI](https://img.shields.io/badge/UI-Blueprint%20%2F%20Steel--Navy%20Cyberpunk-0c1724)
![Status](https://img.shields.io/badge/status-SYSTEM%20OPERATIONAL-00f090)

> **`// SYSTEM OPERATIONAL`** — deterministic compatibility, live budget solving, and AI-assisted synergy scoring for every PC build.

---

## 1. Project Vision & Overview

**Uncapped** is an intelligent, zero-bottleneck PC configuration platform built with **Spec-Driven Development (SDD)**. Rather than letting a user (or an LLM) guess whether a set of parts actually works together, every build is validated against a deterministic rules engine *before* it's ever presented as viable:

- Socket and form-factor compatibility (CPU↔Motherboard, Case↔Motherboard, Case↔PSU)
- Cooler socket support and physical case clearance (GPU length, cooler height)
- PSU wattage headroom against real component TDP draw
- RAM/Storage slot and module-count capacity, including multi-unit quantities

Compatibility is **never LLM-gated** — if the deterministic engine rejects a pairing, no amount of AI persuasion can make it valid. The LLM layer (via OpenRouter) is reserved for what it's actually good at: explaining *why* a build performs the way it does, estimating synergy/bottleneck characteristics, and holding a natural-language conversation about the user's hardware — never for the pass/fail verdict itself.

This repository is developed under **Spec-Driven Development**: `intent.txt` captures original product intent, `spec.md` is the exhaustive, always-current source of technical truth, and a scoped `CLAUDE.md` in every subpackage (`ui/`, `engine/`, `db/`, `llm/`, `auth/`) documents that package's responsibilities and hard import boundaries. Code changes are expected to keep the spec in sync, not drift from it.

## 2. Core Features

### 🔧 Deterministic Compatibility Engine
- Zero-hallucination rule set (`engine/compatibility.py`): CPU/Motherboard socket matching, RAM type/slot capacity, Storage slot capacity (real NVMe `m2_slots`), GPU/cooler physical case clearance, PSU wattage headroom, and Case/PSU/Motherboard form-factor fit.
- Every rule is pure, synchronous Python — no network calls, no LLM involvement, fully unit-testable.
- A live **compatibility score**, **bottleneck percentage**, and **synergy score** are computed deterministically for every build; the LLM only narrates and contextualizes those numbers.

### 🧩 Unified Hardware & Peripheral Catalog
- 8 core component categories: CPU, Motherboard, GPU, RAM, Storage, PSU, Case, Cooler.
- 7 unified peripheral categories, all fillable and budget-aware: Monitor, Keyboard, Mouse, Headset, Network Card, Sound Card, Optical Drive.
- Hardware spans multiple generations (Intel 12th–14th Gen and Core Ultra, AMD Ryzen 5000/7000/9000; NVIDIA RTX 30/40/50-series and AMD RX 6000/7000/9000-series GPUs) so every one of the three build modes has real headroom to work with at any budget.

### 🤖 AI Hardware Concierge
- Conversational, natural-language build assistant (`llm/concierge.py`) that can assemble a brand-new build, patch the *currently active* draft (add/swap/remove components and adjust RAM/Storage quantities in a single request — e.g. "add a network card and bump storage to 2"), navigate the app, save/publish builds, and answer plain hardware questions — all grounded in a zero-hallucination guard that re-validates every catalog id the model returns against the real, live catalog before it's ever applied.
- **Save → Publish pipeline**: an explicit, confirmed save (name + destination: draft or permanent build) with an optional same-turn publish straight to the Community feed, mirroring the manual Build Studio "Save" + "Share" flow exactly.
- **Network resilience**: every OpenRouter call site (`llm/client.py`, `llm/advisory.py`, `llm/concierge.py`) uses a 25-second request timeout and a transparent, fixed-delay retry (up to 3 attempts, 1 second apart) for transient cold-start connection/DNS failures — a genuine non-2xx response or schema failure still falls straight through to the deterministic heuristic fallback, never silently retried.
- Every AI feature (build analysis, the upgrade-path advisory, and the Concierge itself) **never raises** — a network or model failure always degrades to a deterministic, engine-computed heuristic response tagged `source: "heuristic"`, so the app is fully usable even with zero AI connectivity.

### 🌐 Cyberpunk UI & Community Exchange
- Custom "Blueprint / Steel-Navy" high-tech visual theme (`ui/theme.py`) — glowing cyan accents, glass-panel cards, monospace telemetry chips, and a locked 320px sidebar — applied consistently across every page via a single shared token module (no hardcoded colors in view code).
- **Dynamic dual-filter Community feed**: an explicit "Filter by" selector switches the feed between filtering by **Build Type** (Budget/Workload/Free, with cascading price/domain/tier sub-filters) or by **Tag** (`Rate My Build` / `Looking for Help`) — the two axes are independent and never combined.
- Threaded, nested community discussions with a visual reply-chain connector border, supporting multi-level replies while keeping chronological ordering at every depth.
- A seeded demo dataset ships with 70 diverse builds across 6 users (15 admin-owned, 55 spread across 5 persona accounts), 30 published Community posts (balanced Rate My Build / Looking for Help tags), and realistic multi-tier nested comment threads — useful for exploring the feed and filters immediately after a fresh install.

## 3. Architecture & Tech Stack

| Layer | Choice | Notes |
|---|---|---|
| Frontend | **Streamlit** ≥ 1.38 | Session-state-driven router (`ui/router.py`), not native `st.Page` navigation, so the auth-gated landing page can hard-control what's reachable. Custom CSS injected once via `ui/theme.py`. |
| Database | **SQLite** locally (`db/uncapped.db`) / **PostgreSQL** in the cloud | Same SQLAlchemy Core engine either way — swap `DATABASE_URL`, no SQLite-only SQL syntax anywhere in the schema. |
| Auth | **bcrypt** | Used directly (no `passlib` wrapper); passwords are never logged or stored in plaintext. |
| AI / LLM | **OpenRouter** (`google/gemini-2.5-flash`-class models, OpenAI-compatible `/chat/completions`) via **httpx** | Isolated entirely inside `llm/`; model id comes from `OPENROUTER_MODEL`, never hardcoded. Every call gracefully falls back to a deterministic heuristic on failure. |
| Testing | **pytest** | `engine/`/`db/` get pure unit tests; `llm/` tests mock the HTTP layer (no live network calls in CI); UI behavior is verified with `streamlit.testing.v1.AppTest` smoke tests. |
| Lint / format | **ruff** + **black** | |
| Methodology | **Spec-Driven Development (SDD)** | `intent.txt` → `spec.md` → scoped per-package `CLAUDE.md` files. Any new behavior updates `spec.md` in the same change. |

**Module boundaries are load-bearing** — enforced by convention and documented per-package:

```
ui   → auth, engine, db.repositories, llm
llm  → engine, db.repositories (llm_cache table only)
engine → db.repositories (read-only)
auth → db.repositories (users_repo only)
db   → nothing above it
```

`engine/` is pure Python with zero Streamlit/network/LLM imports — it stays unit-testable without mocking anything external. `db/` contains no business logic, only schema, connection management, seeding, and parameterized repository queries — no raw SQL string interpolation anywhere in the project.

## 4. Local Installation & Setup (Windows)

```bash
# 1. Clone the repository, then create and activate a virtual environment
python -m venv venv
venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment variables — create a .env file in the project root:
#    DATABASE_URL=sqlite:///db/uncapped.db
#    OPENROUTER_API_KEY=your_openrouter_key_here
#    OPENROUTER_MODEL=google/gemini-2.5-flash
#    (DATABASE_URL is optional locally — it defaults to the local SQLite file if unset.
#    The app still runs fully without an OpenRouter key: every AI feature degrades to
#    its deterministic heuristic fallback instead of failing.)

# 4. Launch the application
streamlit run app.py
```

The app opens at `http://localhost:8501`. On first run it initializes the local SQLite schema automatically — no manual migration step is required.

## 5. Testing & Quality Assurance

```bash
# Run the full test suite
pytest

# Run a single test file (faster feedback while iterating on one package)
pytest tests/test_engine.py
pytest tests/test_llm.py
pytest tests/test_ui_smoke.py
```

- `engine/` and `db/` tests run against pure Python / an in-memory SQLite database — no external dependencies.
- `llm/` tests mock the HTTP layer entirely (`llm/client.py`, `llm/advisory.py`, `llm/concierge.py`) — the suite never makes a live OpenRouter call.
- UI behavior is exercised with Streamlit's own `AppTest` harness for scripted, deterministic regression coverage of the router, Build Studio, Community feed, and AI Concierge.

---

*Built as a Spec-Driven Development exercise — see [`spec.md`](spec.md) for the full technical specification and [`intent.txt`](intent.txt) for the original product intent.*
