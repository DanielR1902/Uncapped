# Uncapped — Technical Specification

Status: v1.0 draft, derived from `intent.txt` and the supplementary detailing document.
This spec is the source of truth for implementation. Any behavior not described here or in `intent.txt` is out of scope until this document is updated.

---

## 1. System Overview & Philosophy

Uncapped is a Streamlit application that helps a user assemble a compatible, budget-aware PC build. It combines:

- **Deterministic engineering rules** (socket matching, TDP/PSU headroom, physical clearance, form-factor fit) — these are hard constraints, never overridden by the LLM.
- **LLM-derived judgment** (synergy score, bottleneck narrative, architectural insights) via OpenRouter — soft, explanatory, advisory.
- **Three build entry points** (Budget Constrained, Workload Profile, Free Custom) that all converge on the same underlying component catalog and compatibility engine.
- **Persistence and community sharing** so builds outlive a session and can be published, forked, and discussed.

### 1.1 Guiding principles
1. Compatibility is never LLM-gated. If the deterministic engine says a pairing is invalid, no LLM output can make it valid.
2. The LLM is stateless per call. It receives a fully-formed snapshot (selected components + deterministic pre-check results) and returns structured JSON — it never queries the database itself.
3. Every build mutation (component added/removed/swapped) re-runs the deterministic engine synchronously and the LLM layer asynchronously/on-demand (see §6.5 for debounce/cache rules).
4. UI code contains no SQL and no compatibility math. It only calls into `db.repositories`, `engine`, `auth`, and `llm`.

---

## 2. Architecture

### 2.1 Tech stack

| Concern | Choice | Notes |
|---|---|---|
| Language | Python 3.11+ | type hints required on public functions |
| UI | Streamlit ≥ 1.38 | multipage via a session-state router, not native `st.Page` navigation, so the auth-gated landing screen can control what's reachable |
| DB (local) | SQLite 3 via SQLAlchemy Core | file at `db/uncapped.db` |
| DB (cloud) | PostgreSQL via the same SQLAlchemy engine | swap `DATABASE_URL`; no raw SQLite-only syntax in `schema.sql`-equivalent DDL |
| Password hashing | `bcrypt` (package used directly, no `passlib` wrapper) | never store plaintext; never log password fields |
| LLM provider | OpenRouter (OpenAI-compatible `/chat/completions`) | model id from `OPENROUTER_MODEL` env var, no hardcoded model string in code |
| HTTP client | `httpx` with explicit timeout | used only inside `llm/client.py` |
| Config | `.env` via `python-dotenv` | `.env.example` checked in, `.env` gitignored |
| Testing | `pytest` | `engine/` and `db/` must have unit tests; `llm/` tested with mocked HTTP |
| Lint/format | `ruff` + `black` | enforced pre-commit (not required to build, but standard) |

### 2.2 Directory structure

```
FinalProjectUncapped/
├── CLAUDE.md
├── spec.md
├── intent.txt
├── app.py                      # Streamlit entrypoint — router only
├── requirements.txt
├── .env.example
├── .streamlit/
│   └── config.toml             # theme (see §7.6)
├── auth/
│   ├── CLAUDE.md
│   ├── __init__.py
│   ├── models.py                # User dataclass
│   ├── service.py                # register(), authenticate(), validate_unique()
│   └── session.py                # st.session_state read/write helpers for auth
├── engine/
│   ├── CLAUDE.md
│   ├── __init__.py
│   ├── compatibility.py          # deterministic rule checks + evaluate_build() audit report
│   ├── solvers.py                 # all 3 creation-mode engines: budget, workload, free-custom filter
│   └── scoring.py                 # compatibility % (delegated), value/ratio index, bottleneck baseline
├── db/
│   ├── CLAUDE.md
│   ├── __init__.py
│   ├── ddl.sql                    # canonical DDL (§3)
│   ├── database.py                # engine/session factory, migrations bootstrap
│   ├── seed_data.py                # catalog + workload-mapping literals (§4), no execution logic
│   ├── seed.py                     # runner: loads seed_data.py into the DB, idempotent
│   ├── seed_demo.py                # mock users/builds/community content for demos (§4.4), idempotent
│   └── repositories/
│       ├── __init__.py
│       ├── users_repo.py
│       ├── components_repo.py
│       ├── builds_repo.py
│       └── community_repo.py
├── ui/
│   ├── CLAUDE.md
│   ├── __init__.py
│   ├── router.py                   # page dispatch on st.session_state["page"]
│   ├── state.py                     # session-state keys/defaults + build_draft <-> BuildState bridge
│   ├── theme.py                     # color/typography constants + CSS injection
│   ├── format.py                    # tiny shared display-formatting helpers (e.g. "VideoEditing" -> "Video Editing")
│   ├── views/
│   │   ├── landing.py
│   │   ├── create_build.py
│   │   ├── my_builds.py
│   │   └── community.py
│   └── components/
│       ├── auth_modal.py
│       ├── build_card.py
│       └── part_picker.py
├── llm/
│   ├── CLAUDE.md
│   ├── __init__.py
│   ├── client.py                    # OpenRouter HTTP wrapper
│   ├── prompts.py                    # prompt templates (§6)
│   ├── schemas.py                     # pydantic request/response models
│   └── cache.py                       # build-state hash → cached response
└── tests/
    ├── engine/
    ├── db/
    └── llm/
```

### 2.3 Module dependency direction

```
ui  →  auth, engine, db.repositories, llm
llm →  engine (for deterministic pre-check payload), db.repositories (cache table only)
engine → db.repositories (read-only catalog access)
auth → db.repositories (users_repo only)
db  →  (nothing above it)
```

`engine` never imports `llm` or `ui`. `db` never imports anything outside itself. This keeps the compatibility engine independently testable and keeps the LLM layer swappable.

---

## 3. Database Schema

Portable DDL (SQLite-compatible, PostgreSQL-compatible — no `AUTOINCREMENT`-only or SQLite-only pragmas beyond `INTEGER PRIMARY KEY`).

### 3.1 `users`

| Column | Type | Constraints |
|---|---|---|
| id | INTEGER | PK |
| username | TEXT | UNIQUE, NOT NULL |
| email | TEXT | UNIQUE, NOT NULL |
| password_hash | TEXT | NOT NULL |
| full_name | TEXT | NOT NULL |
| created_at | TIMESTAMP | NOT NULL, DEFAULT CURRENT_TIMESTAMP |

Indexes: `UNIQUE(username)`, `UNIQUE(email)` (both already unique-indexed by the constraint; no additional secondary index needed since lookups are always by one of these two columns).

### 3.2 `components`

| Column | Type | Constraints |
|---|---|---|
| id | INTEGER | PK |
| category | TEXT | NOT NULL, CHECK IN (`CPU`,`Motherboard`,`GPU`,`RAM`,`Storage`,`PSU`,`Case`,`Cooler`,`NetworkCard`,`SoundCard`,`OpticalDrive`) |
| name | TEXT | NOT NULL |
| brand | TEXT | NOT NULL |
| price_usd | REAL | NOT NULL, CHECK (price_usd > 0) |
| socket | TEXT | NULLABLE — CPU/Motherboard/Cooler |
| ram_type | TEXT | NULLABLE — RAM/Motherboard, e.g. `DDR4`, `DDR5` |
| tdp_watts | INTEGER | NULLABLE — CPU/GPU |
| wattage_capacity | INTEGER | NULLABLE — PSU only |
| form_factor | TEXT | NULLABLE — Motherboard (`ATX`,`mATX`,`ITX`), PSU (`ATX`,`SFX`), Case (supported form factors, see 3.2.1) |
| capacity_gb | INTEGER | NULLABLE — RAM, Storage |
| interface | TEXT | NULLABLE — Storage (`NVMe`,`SATA`), GPU (`PCIe4.0x16`, etc.) |
| max_gpu_length_mm | INTEGER | NULLABLE — Case only |
| max_cooler_height_mm | INTEGER | NULLABLE — Case only |
| psu_form_factor_support | TEXT | NULLABLE — Case only, e.g. `ATX,SFX` |
| chipset | TEXT | NULLABLE — Motherboard only |
| benchmark_score | INTEGER | NULLABLE — CPU/GPU, normalized 0–100 performance tier used by bottleneck baseline (§5.4) |
| specs_json | TEXT (JSON) | NOT NULL DEFAULT `'{}'` — extended/display-only specs (core count, clock speeds, RGB, wattage draw curve, etc.) that are not compatibility-critical |
| image_url | TEXT | NULLABLE |
| created_at | TIMESTAMP | NOT NULL DEFAULT CURRENT_TIMESTAMP |

3.2.1 `form_factor` on `Case` rows stores a **comma-separated list** of motherboard form factors the case accepts, largest-first (e.g. `ATX,mATX,ITX`). Compatibility check: `motherboard.form_factor IN case.form_factor.split(',')`.

Indexes:
- `idx_components_category` on (`category`)
- `idx_components_price` on (`category`, `price_usd`)
- `idx_components_socket` on (`socket`) WHERE socket IS NOT NULL (or plain index if the DB target doesn't support partial indexes — PostgreSQL does, SQLite does; if targeting strict portability, use a plain index)

### 3.3 `workload_mappings`

| Column | Type | Constraints |
|---|---|---|
| id | INTEGER | PK |
| component_id | INTEGER | NOT NULL, FK → `components(id)` ON DELETE CASCADE |
| workload_profile | TEXT | NOT NULL, CHECK IN (`General`,`Gaming`,`VideoEditing`,`Design`,`Programming`) |
| tier | TEXT | NOT NULL, CHECK IN (`Entry`,`Mid`,`High`,`Enthusiast`) |
| weight | REAL | NOT NULL, DEFAULT 1.0 — solver priority weight within the profile |

Constraints: `UNIQUE(component_id, workload_profile)`.
Indexes: `idx_workload_profile` on (`workload_profile`, `tier`).

### 3.4 `builds`

| Column | Type | Constraints |
|---|---|---|
| id | INTEGER | PK |
| user_id | INTEGER | NOT NULL, FK → `users(id)` ON DELETE CASCADE |
| name | TEXT | NOT NULL |
| creation_mode | TEXT | NOT NULL, CHECK IN (`Budget`,`Workload`,`Free`) |
| workload_profile | TEXT | NULLABLE, CHECK IN (`General`,`Gaming`,`VideoEditing`,`Design`,`Programming`) |
| budget_ceiling | REAL | NULLABLE |
| total_cost | REAL | NOT NULL |
| synergy_score | REAL | NULLABLE — 0–100, from LLM layer |
| bottleneck_percentage | REAL | NULLABLE — 0–100 |
| compatibility_score | REAL | NOT NULL — 0–100, deterministic (§5.3) |
| is_public | BOOLEAN | NOT NULL DEFAULT FALSE |
| forked_from_post_id | INTEGER | NULLABLE, FK → `community_posts(id)` ON DELETE SET NULL |
| created_at | TIMESTAMP | NOT NULL DEFAULT CURRENT_TIMESTAMP |
| updated_at | TIMESTAMP | NOT NULL DEFAULT CURRENT_TIMESTAMP |

Indexes: `idx_builds_user` on (`user_id`, `created_at`), `idx_builds_public` on (`is_public`, `created_at`).

### 3.5 `build_components` (join table)

| Column | Type | Constraints |
|---|---|---|
| id | INTEGER | PK |
| build_id | INTEGER | NOT NULL, FK → `builds(id)` ON DELETE CASCADE |
| component_id | INTEGER | NOT NULL, FK → `components(id)` |
| category | TEXT | NOT NULL — denormalized copy of `components.category` for cheap per-category queries |
| quantity | INTEGER | NOT NULL DEFAULT 1 — >1 only valid for `Storage`/peripheral categories |

Constraints: `UNIQUE(build_id, component_id)`.
Indexes: `idx_build_components_build` on (`build_id`).
Application-level invariant (not a DB constraint, enforced in `engine`/`db.repositories.builds_repo`): at most one row per `build_id` for each of the 8 core categories (CPU, Motherboard, GPU, RAM, Storage, PSU, Case, Cooler) — Storage is the one core category where `quantity` may represent multiple identical drives, but distinct storage *models* still get distinct rows.

### 3.6 `community_posts`

| Column | Type | Constraints |
|---|---|---|
| id | INTEGER | PK |
| build_id | INTEGER | NOT NULL, FK → `builds(id)` ON DELETE CASCADE |
| user_id | INTEGER | NOT NULL, FK → `users(id)` ON DELETE CASCADE |
| title | TEXT | NOT NULL |
| author_notes | TEXT | NULLABLE |
| created_at | TIMESTAMP | NOT NULL DEFAULT CURRENT_TIMESTAMP |

Indexes: `idx_community_posts_build` on (`build_id`), `idx_community_posts_created` on (`created_at`).

### 3.7 `community_comments`

| Column | Type | Constraints |
|---|---|---|
| id | INTEGER | PK |
| post_id | INTEGER | NOT NULL, FK → `community_posts(id)` ON DELETE CASCADE |
| user_id | INTEGER | NOT NULL, FK → `users(id)` ON DELETE CASCADE |
| content | TEXT | NOT NULL |
| created_at | TIMESTAMP | NOT NULL DEFAULT CURRENT_TIMESTAMP |

Indexes: `idx_comments_post` on (`post_id`, `created_at`).

### 3.8 `llm_cache` (supporting table for §6.5)

| Column | Type | Constraints |
|---|---|---|
| cache_key | TEXT | PK — sha256 of sorted component IDs + workload_profile + prompt_type |
| synergy_score | REAL | NOT NULL — denormalized from response_json.synergy.overall_score, for cheap SQL-level queries |
| bottleneck_percentage | REAL | NOT NULL — denormalized from response_json.bottleneck.bottleneck_percentage |
| response_json | TEXT (JSON) | NOT NULL — the full serialized `BuildAnalysisResponse` (synergy + bottleneck + insights) |
| created_at | TIMESTAMP | NOT NULL DEFAULT CURRENT_TIMESTAMP |

No FK — this is a pure memoization table, safe to truncate at any time without data loss elsewhere.

---

## 4. Component Catalog Seeding Plan

Seed data lives in `db/seed_data.py` as plain Python literals (not a spreadsheet import) so it's diffable and typed; `db/seed.py` is the runner that loads those literals into the database (idempotent — re-running it skips insertion if the catalog is already populated, unless a `force` re-seed is requested). Target volume: **15–25 rows per core category**, **6–10 per peripheral category**, spanning Entry → Enthusiast tiers so every workload profile and budget range has viable options.

### 4.1 Category attribute matrix (compatibility-critical fields only; extended specs go in `specs_json`)

| Category | Critical fields | Example `specs_json` extras |
|---|---|---|
| CPU | `socket`, `tdp_watts`, `benchmark_score` | core_count, thread_count, base_clock_ghz, boost_clock_ghz, integrated_graphics |
| Motherboard | `socket`, `ram_type`, `form_factor`, `chipset` | ram_slots, max_ram_gb, pcie_version, m2_slots, wifi_onboard |
| GPU | `tdp_watts`, `benchmark_score`, `interface` | vram_gb, length_mm (also compared against Case), boost_clock_mhz |
| RAM | `ram_type`, `capacity_gb` | module_count, speed_mhz, cas_latency |
| Storage | `interface`, `capacity_gb` | read_speed_mbps, write_speed_mbps, form_factor(2.5in/M.2-2280) |
| PSU | `wattage_capacity`, `form_factor` | efficiency_rating (80+ Bronze/Gold/Platinum), modular(Y/N) |
| Case | `form_factor` (accepted MB form factors), `max_gpu_length_mm`, `max_cooler_height_mm`, `psu_form_factor_support` | fan_mounts, side_panel_type, max_radiator_mm (largest AIO radiator the case accepts — see §5.1 rule 7) |
| Cooler | `socket` (comma-list of supported sockets stored same convention as Case.form_factor) | cooler type (air/AIO), radiator_size_mm, noise_level_dba |
| NetworkCard | none (compat = PCIe slot presence, assumed universal) | interface_speed, wifi_standard |
| SoundCard | none | channels, dac_quality |
| OpticalDrive | none | disc_types_supported |

### 4.2 Realistic seed examples (illustrative subset — full list in `db/seed_data.py`)

**CPU**
- AMD Ryzen 5 5600 — socket `AM4`, tdp 65W, benchmark_score 58, price $109
- AMD Ryzen 7 7800X3D — socket `AM5`, tdp 120W, benchmark_score 88, price $359
- Intel Core i5-13400F — socket `LGA1700`, tdp 65W, benchmark_score 62, price $199
- Intel Core i9-14900K — socket `LGA1700`, tdp 125W, benchmark_score 97, price $549

**GPU**
- NVIDIA RTX 4060 — tdp 115W, benchmark_score 55, interface `PCIe4.0x8`, length 200mm, price $299
- NVIDIA RTX 4070 Super — tdp 220W, benchmark_score 74, length 267mm, price $599
- NVIDIA RTX 4090 — tdp 450W, benchmark_score 100, length 336mm, price $1599
- AMD Radeon RX 7600 — tdp 165W, benchmark_score 50, length 204mm, price $269

**Motherboard**
- MSI B550M PRO-VDH — socket `AM4`, ram_type `DDR4`, form_factor `mATX`, chipset `B550`, price $85
- ASUS ROG STRIX X670E-E — socket `AM5`, ram_type `DDR5`, form_factor `ATX`, chipset `X670E`, price $479
- ASRock B760M Pro RS — socket `LGA1700`, ram_type `DDR4`, form_factor `mATX`, chipset `B760`, price $109

**RAM**
- Corsair Vengeance 16GB (2x8GB) DDR4-3200 — price $42
- G.Skill Trident Z5 32GB (2x16GB) DDR5-6000 — price $129

**Storage**
- Kingston NV2 500GB NVMe — interface `NVMe`, capacity 500, price $34
- Samsung 990 Pro 2TB NVMe — interface `NVMe`, capacity 2000, price $149
- Seagate Barracuda 2TB — interface `SATA`, capacity 2000, price $54

**PSU**
- Corsair CV550 — wattage 550, form_factor `ATX`, price $54
- EVGA SuperNOVA 850 GT — wattage 850, form_factor `ATX`, price $129

**Case**
- NZXT H510 — form_factor `ATX,mATX,ITX`, max_gpu_length_mm 381, max_cooler_height_mm 165, psu_form_factor_support `ATX`, price $79
- Cooler Master NR200 — form_factor `ITX`, max_gpu_length_mm 330, max_cooler_height_mm 155, psu_form_factor_support `SFX`, price $99

**Cooler**
- Cooler Master Hyper 212 — socket `AM4,AM5,LGA1700,LGA1200`, height 159mm, price $35
- NZXT Kraken 280 (AIO) — socket `AM4,AM5,LGA1700`, radiator 280mm, price $159

**Peripherals**
- TP-Link Archer TX3000E (NetworkCard) — price $40
- Creative Sound Blaster Z (SoundCard) — price $80
- LG WH16NS40 (OpticalDrive, Blu-ray writer) — price $55

### 4.3 Workload tier seeding rule

For each `(component, workload_profile)` pair that makes sense, insert a `workload_mappings` row with a `tier`. A component may map to multiple profiles (e.g., a mid-tier GPU maps to both `Gaming` Mid-tier and `Design` Mid-tier). Tier bands per profile are authored by hand in `seed_data.py`, not derived — this is curated data, not computed.

### 4.4 Demo/mock data (`db/seed_demo.py`)

Separate from catalog seeding — populates realistic users, builds, and community activity for demos and manual testing, idempotently (checks whether the demo usernames already exist; `force=True` wipes and reseeds via cascading deletes on the `users` rows). Runs the catalog seed first if it isn't already present, so it works standalone against a fresh `uncapped.db`.

- **Users**: 5 persona accounts (`tech_enthusiast`, `budget_gamer`, `ai_researcher`, `cad_pro`, `silent_builder`) with realistic emails/full names, registered through `auth.service.register` (so passwords are hashed exactly the way real signups are) sharing one documented password — plus a standing `admin` login (`admin@gmail.com` / `admin123`) with no generated builds, checked for existence independently of the persona dataset's own idempotency/force-reseed logic.
- **Builds**: 1-2 per user generated through the real solvers (`engine.solvers.allocate_workload_baseline` / `initialize_budget_build`), so every build is compatible by construction rather than hand-assembled. Scores (`compatibility_score`, `synergy_score`, `bottleneck_percentage`) are computed via `engine/compatibility.py` + `engine/scoring.py` directly — deliberately *not* through `llm/client.py`, so seeding is deterministic and never depends on network/API availability. `engine.scoring.heuristic_synergy_score(compatibility_score, bottleneck_percentage)` is the same formula `llm/client.py`'s fallback uses, factored out so it's defined in exactly one place.
- **Community**: some of those builds are published with an engaging title + author notes (`community_posts`); other demo users leave 2-3 realistic comments each (thermals/pricing/part-recommendation questions) — always from someone other than the post's author.

---

## 5. Algorithmic Specification

### 5.1 Deterministic compatibility rules (`engine/compatibility.py`)

Each rule is a pure function `(build_state) -> RuleResult{passed: bool, message: str}`. Rules only fire when both sides of the check are present in the current build state (partial builds are always valid — a rule about GPU/Case clearance doesn't fire until both a GPU and a Case are selected).

1. **Socket match**: `CPU.socket == Motherboard.socket`
2. **RAM type match**: `Motherboard.ram_type == RAM.ram_type`
3. **Cooler socket support**: `CPU.socket IN Cooler.socket.split(',')`
4. **Case ↔ Motherboard form factor**: `Motherboard.form_factor IN Case.form_factor.split(',')`
5. **Case ↔ PSU form factor**: `PSU.form_factor IN Case.psu_form_factor_support.split(',')`
6. **GPU clearance**: `GPU.specs_json.length_mm <= Case.max_gpu_length_mm`
7. **Cooler clearance**: for air coolers, `Cooler.specs_json.height_mm <= Case.max_cooler_height_mm`. For AIO (liquid) coolers, height doesn't apply — instead `Cooler.specs_json.radiator_size_mm <= Case.specs_json.max_radiator_mm` (a dedicated extended-specs field on Case, distinct from the air-cooler height column, since the two are physically unrelated dimensions).
8. **PSU headroom**: `(CPU.tdp_watts + GPU.tdp_watts + 50_system_baseline_watts) * 1.3 <= PSU.wattage_capacity`
   - The `1.3` safety multiplier and `50W` baseline are named constants in `compatibility.py`, not magic numbers.

`run_all_checks(build_state) -> list[RuleResult]` runs every applicable rule. `evaluate_build(build_state) -> CompatibilityReport` wraps it into the audit report the rest of the app consumes: `{is_compatible: bool, compatibility_score: float, issues: list[str], results: list[RuleResult]}` — `issues` is just the messages of the failed results, surfaced directly to the UI.

### 5.2 Budget-Constrained Greedy Solver (`engine/solvers.py`)

Implements the "converge from the top" approach: find the most expensive fully-compatible build that still fits the ceiling, then let the user swap parts from there with the remaining budget dynamically re-partitioned.

```
function initialize_budget_build(ceiling, category_order = [GPU, CPU, Motherboard, RAM, Storage, Case, PSU, Cooler]):
    remaining = ceiling
    selection = {}
    for category in category_order:
        candidates = components_repo.get_by_category(category)
        candidates = filter_compatible(candidates, selection)          # apply engine.compatibility so far
        # reserve enough budget for the cheapest compatible option in every category not yet chosen
        future_min_cost = sum(min_price(cat, selection) for cat in category_order if cat not chosen yet, excluding current)
        affordable = [c for c in candidates if c.price_usd <= remaining - future_min_cost]
        if affordable is empty:
            affordable = [cheapest compatible candidate]  # degrade gracefully, never leave a category unfilled
        chosen = max(affordable, key = lambda c: c.price_usd)
        selection[category] = chosen
        remaining -= chosen.price_usd
    return enforce_ceiling(selection, category_order, ceiling)   # repair pass, see below

function on_user_pins_component(selection, pinned_category, pinned_component, ceiling):
    selection[pinned_category] = pinned_component
    spent = sum(price of pinned/selected so far)
    remaining_categories = all categories not yet pinned by the user
    remaining_budget = ceiling - spent
    re-run the same max-convergence loop over remaining_categories only,
    with candidates pre-filtered by compatibility against every already-pinned component
    return enforce_ceiling(selection, remaining_categories, ceiling)   # never touches pinned_category

function enforce_ceiling(selection, adjustable_categories, ceiling):
    # The per-category reservation above is a heuristic, not a proof: PSU headroom
    # depends jointly on CPU AND GPU, but they're chosen one at a time, so the
    # reservation computed while choosing GPU can't yet know CPU's contribution
    # (and vice versa) — the loop can still land over budget once PSU is priced
    # for real. This closes the gap deterministically: while over budget, repeatedly
    # swap the most expensive still-adjustable category to its own cheapest
    # compatible alternative (holding everything else fixed) until it fits, or no
    # further trim is possible. Categories the caller pinned are never touched.
    ...
```

Key properties:
- **Category order matters**: `Case` is resolved before `PSU` (not the intuitive order) because PSU's ATX/SFX form-factor rule (§5.1 rule 5) needs a fixed Case to check against — choosing PSU first can strand the build with an SFX unit once Case's turn comes and no compatible Case is left (SFX PSUs only pair with the catalog's ITX-only cases).
- **Never exceeds the ceiling**: guaranteed not by the per-category reservation alone (see `enforce_ceiling` above) but by the repair pass that follows it — the reservation gets the build *close* to the ceiling in one pass; the repair pass is what makes "never exceeds" an actual invariant rather than a best-effort heuristic. As an absolute last resort, `_greedy_fill` checks the total once more after that repair pass; if compatibility constraints chained non-trivially enough across categories that it's still over (the reservation heuristic and `cheapest_fill_cost`'s fixed-order estimate can converge on slightly different totals in this rare case), it re-runs the repair pass a second time treating *every* category — including ones the caller pinned — as adjustable. A pin only ever yields to the ceiling once nothing else is left to trim.
- **User can pin any category first** ("doesn't matter which part they start with" — per the detailing doc): the algorithm re-partitions remaining budget across whatever categories are still open, in the same fixed priority order (GPU/CPU first, since they dominate both cost and performance).
- **Dynamic filtering is per-candidate, not per-slot**: after every pin, `part_picker` UI re-queries candidates through `filter_compatible`, then disables any candidate that would leave too little to complete the other open core slots. That reserve (`cheapest_fill_cost` of the other open categories) is recomputed for *each candidate individually* — hypothetically slotting the candidate in first — because which candidate ends up in a slot can itself change what's cheapest elsewhere (an ITX Case forces a pricier SFX PSU than an ATX Case would; a Motherboard's socket changes the cheapest compatible CPU/RAM). A single reserve number computed once per slot, independent of which candidate is being considered, can silently under-count the true reserve for a specific constraining candidate — exactly the deadlock this check exists to prevent.
- **Infeasible pins get auto-downgraded, not blocked**: `enforce_ceiling`'s repair pass only ever trims categories the solver itself chose — pinned/pre-selected categories are its usual immutable constraint. But if the caller's own pins already exceed the ceiling, or leave less than `cheapest_fill_cost(selection, categories_to_fill)` (the cheapest possible cost to compatibly fill everything still open) for the rest, `_greedy_fill` calls `_downgrade_pinned_until_feasible`: it steps the most expensive *pinned* categories down to progressively cheaper compatible alternatives (highest tier that's still cheaper), one at a time, most-expensive-pinned-first — the same price-descending/progress-flag pattern as `enforce_ceiling` above, just applied to pins instead of the solver's own picks — until it fits, or there's nothing left to downgrade (in which case it proceeds with whatever's cheapest, same graceful-degradation philosophy as an empty selection). Neither `initialize_budget_build` nor `on_user_pins_component` ever raises for this — a hard blocking error was tried and rejected as too much friction. This check (and any downgrade) is skipped entirely when `selection` is empty. `ui/views/create_build.py`'s "Generate starting build" detects a downgrade by comparing its seed against the result per category, and shows a non-blocking `st.toast(...)` (not an error) when something was adjusted.
- **The Budget ceiling input can't go below the true minimum**: `minimum_possible_build_cost()` (`= cheapest_fill_cost({}, CATEGORY_ORDER)`) is the cheapest a complete, compatible 8-part build can ever be — no ceiling below it is satisfiable, full stop. `ui/views/create_build.py`'s `_budget_controls` sets this as the ceiling `st.number_input`'s `min_value` (so the widget itself won't accept lower) and, as a defensive second layer, clamps any value that still comes back below it and shows `st.toast("Budget adjusted to $X (the minimum viable cost for a complete compatible build).")`. The ceiling is written to `build_draft["budget_ceiling"]` unconditionally on every rerun (not gated behind a button), so every part-picker's Category Max Cap recalculates the instant the ceiling changes — no separate reactivity plumbing needed beyond Streamlit's own per-widget rerun.

### 5.2.1 Free Custom filtering (Mode C) (`engine/solvers.py`)

`get_compatible_candidates(category, build_state) -> list[Component]` is `filter_compatible` exposed directly: given whatever is already selected, return only the catalog rows for one category that keep the whole build compatible. `get_all_compatible_candidates(build_state) -> dict[str, list[Component]]` runs that for every core category at once, for rendering all part-pickers together. This is the entire Mode C engine — Free Custom Build has no budget/workload pre-allocation, just this filter re-run on every selection change.

### 5.3 Compatibility Score (%) (`engine/scoring.py`)

```
compatibility_score(build_state) = 100 * (checks_passed / checks_applicable)
```
where `checks_applicable` is the subset of the 8 rules in §5.1 that have both required components present. A build with only a CPU selected has 0 applicable checks and a score of 100 (nothing to fail yet); the score only degrades as failing pairings are introduced, and the UI must surface *which* rule failed via `RuleResult.message`. `scoring.compatibility_score` is a thin delegator to `compatibility.evaluate_build(...).compatibility_score` — the formula is owned by `compatibility.py` (§5.1) and not duplicated.

### 5.4 Bottleneck percentage (baseline, pre-LLM)

```
bottleneck_percentage(build_state) =
    abs(cpu.benchmark_score - gpu.benchmark_score) / max(cpu.benchmark_score, gpu.benchmark_score) * 100
```
Direction (`CPU-bound` vs `GPU-bound`) is derived from which score is lower. This baseline is always computed synchronously and shown immediately; the LLM layer (§6) may refine the number by up to ±10 percentage points and must always attach a narrative explanation when it does.

### 5.5 Value/Ratio Index

Used for the "Value/Ratio Index" sort criterion on part-picker lists, computed per category (not per full build) so components can be compared against peers in the same slot:

```
value_index(component, category_candidates) =
    normalized_compat = component.compatibility_contribution / max(compat contribution across category_candidates)
    normalized_price  = component.price_usd / max(price across category_candidates)
    value_index = (normalized_compat / normalized_price) * 100   # higher is better
```
`compatibility_contribution` for a candidate is computed by hypothetically slotting it into the current build state and re-running §5.3 — i.e., "if I picked this part, what would my score become."

### 5.6 Workload Profile allocation (`engine/solvers.py`)

```
function allocate_workload_baseline(profile, target_tier = "Mid"):
    for category in [CPU, Motherboard, GPU, RAM, Storage, PSU, Case, Cooler]:
        candidates = workload_mappings_repo.get(category, profile, tier = target_tier)
        candidates = filter_compatible(candidates, selection_so_far)
        selection[category] = pick_representative(candidates)  # e.g. median price within tier
    return selection
```
`target_tier` defaults to `Mid` and is user-adjustable (Entry/Mid/High/Enthusiast) as a secondary control on the Workload mode screen. After the baseline is pre-allocated, the user can swap any part; every swap re-runs `filter_compatible` scoped to compatible + same-or-better tier candidates for the remaining categories, and surfaces compatibility-ranked recommendations (per intent.txt §3 Mode B).

---

## 6. LLM Integration Layer (OpenRouter)

### 6.1 Client (`llm/client.py`)
- Base URL: `https://openrouter.ai/api/v1/chat/completions`
- Auth: `Authorization: Bearer {OPENROUTER_API_KEY}` from env
- Model: `OPENROUTER_MODEL` env var (no hardcoded default committed to source; `.env.example` documents the variable and links to OpenRouter's model list)
- Timeout: 15s connect+read.
- Structured output: request `response_format: {"type": "json_object"}` and instruct the exact JSON shape in the system prompt (see §6.4); parse defensively via `pydantic`.
- **`analyze_build(...)` never raises for an expected failure mode** (missing API key, timeout, connection error, non-2xx/429 response, or a response that fails schema validation) — internally these all raise `LLMUnavailableError`, which `analyze_build` catches and converts into a heuristic `BuildAnalysisResponse` computed entirely from `engine/scoring.py` + `engine/compatibility.py` (no network), with `source="heuristic"` so the UI can show "AI insight unavailable — showing an estimate" without needing to catch an exception itself. `LLMUnavailableError` stays importable for tests and for any caller that wants to distinguish the failure explicitly, but the default call path always returns a usable response object — the app must never crash because OpenRouter is unreachable.
- Only a successful, schema-valid LLM response is written to `llm_cache` — a heuristic fallback is recomputed on demand each time rather than cached, so a later real call isn't permanently shadowed by a stale estimate.

### 6.2 Request payload shape (`llm/schemas.py::BuildAnalysisRequest`)
```json
{
  "workload_profile": "Gaming | General | VideoEditing | Design | Programming | null",
  "budget_ceiling": 1500.0,
  "components": [
    {"category": "CPU", "name": "...", "critical_specs": {...}, "extended_specs": {...}},
    ...
  ],
  "deterministic_precheck": {
    "compatibility_score": 92.5,
    "failed_rules": ["PSU headroom: 620W required, 550W supplied"],
    "baseline_bottleneck_percentage": 18.2,
    "bottleneck_direction": "CPU-bound"
  }
}
```
The LLM never re-derives compatibility — it is handed the deterministic result and asked to interpret/contextualize it, never to override the pass/fail.

### 6.3 Prompt schemas

**a) Synergy score prompt** — asks the model to rate 0–100 how well the *specific combination* of parts works together beyond raw compatibility (e.g., a 4090 paired with a budget PSU that technically clears headroom but leaves no room for upgrades; a workload mismatch like a Design build with a gaming-oriented low-VRAM GPU).

**b) Bottleneck analysis prompt** — given `baseline_bottleneck_percentage` and `bottleneck_direction`, asks for a refined percentage (bounded to baseline ± 10) plus a 1–2 sentence plain-language explanation of what is actually being bottlenecked (e.g., "1% lows in CPU-bound titles").

**c) Insights prompt** — asks for 2–4 short bullet insights: upgrade suggestions, notable strengths, workload fit commentary.

These three are combined into **one** `/chat/completions` call per build-state change (one system prompt instructing the model to return all three sections in a single JSON object, per `BuildAnalysisResponse`) to minimize latency and cost — not three separate round-trips.

### 6.4 Response schema (`llm/schemas.py::BuildAnalysisResponse`)
Each of the three prompt sections in §6.3 gets its own nested `pydantic` model rather than a flat set of fields:
```json
{
  "synergy": {
    "overall_score": 0-100,
    "breakdown": {"<aspect>": 0-100, "...": "..."},
    "positive_synergies": ["string", "..."],
    "negative_conflicts": ["string", "..."]
  },
  "bottleneck": {
    "bottleneck_percentage": 0-100,
    "limiting_component": "CPU | GPU | None",
    "resolution_impact": {"1080p": "string", "1440p": "string", "4K": "string"}
  },
  "insights": {
    "summary": "string",
    "upgrade_path": ["string", "..."],
    "quirks": ["string", "..."]
  },
  "source": "llm | heuristic"
}
```
`SynergyEvaluation.overall_score`/`breakdown` values and `BottleneckAnalysis.bottleneck_percentage` are range-validated (0-100) via `pydantic`; `limiting_component` is a strict `Literal["CPU", "GPU", "None"]`. Any field out of range, missing, or otherwise failing validation is treated the same as a network failure (§6.1) — it produces the heuristic fallback, not a crash. `source` defaults to `"llm"` and is forced to `"heuristic"` on the fallback path regardless of what a (still successfully-parsed) partial LLM payload might have set it to.

### 6.5 Caching & debounce
- Cache key: `sha256(prompt_type + "|" + workload_profile + "|" + sorted(component_ids))`, stored in `llm_cache` (§3.8). `prompt_type` defaults to the single combined-call type used today (`"build_analysis"`) — it's a parameter on `llm.cache.cache_key(...)` so a future distinct prompt type can share the same cache table without key collisions, not because §6.3's three sections are cached separately today (they aren't — one call, one cache entry, one row).
- On every build-state change, `ui` computes the cache key first and checks `llm_cache` / an in-session dict before calling `llm.client`. Identical component sets across different users/builds share a cache hit.
- UI debounces rapid successive swaps (e.g., 800ms) before triggering an LLM call, so dragging through a part list doesn't fire a request per keystroke.

---

## 7. Streamlit UI State Machine

### 7.1 Session state keys

| Key | Type | Purpose |
|---|---|---|
| `page` | str | `landing` \| `create_build` \| `my_builds` \| `community` |
| `auth_user` | dict \| None | sanitized session payload (id, username, email, full_name) — see `auth.service.to_session_payload` |
| `auth_mode` | str \| None | `login` \| `register` \| None — controls inline modal visibility |
| `auth_error` | dict | field-level validation errors for red-highlighting (§7.3) |
| `create_mode` | str \| None | `Budget` \| `Workload` \| `Free` \| None |
| `build_draft` | dict | `{"name", "creation_mode", "workload_profile", "tier", "budget_ceiling", "components": {category: component_id}}` — see `ui/state.py::new_build_draft` |
| `build_draft_analysis` | dict \| None | last `BuildAnalysisResponse.model_dump()` for the current draft (`["source"]` is `"llm"` or `"heuristic"`) |
| `sort_criteria` | str | `Cost` \| `Compatibility` \| `ValueIndex` — for part-picker lists |
| `previous_builds_filter` | dict | `{workload_profile, sort}` for the dashboard |
| `selected_post_id` | int \| None | community thread currently open |
| `fork_source_build_id` | int \| None | set when "Fork/Customize" seeds a new `create_build` draft |

`ui/state.py` owns this table: it defines the defaults, `init_session_state()` (called once from `app.py`), and the bridge functions between `build_draft` (plain, session-state-safe dict of category -> component **id**) and a `BuildState` (category -> full `Component` row) that `engine/`/`llm/` actually operate on — `resolve_build_state(build_draft)`, `set_component(build_draft, category, component)`, `remove_component(build_draft, category)`.

### 7.2 Page routing (`ui/router.py`)
A single dispatcher reads `st.session_state["page"]` and calls the matching `ui/views/*.py::render()` function. No native Streamlit multipage file-based routing is used, because the landing page must gate navigation behind `auth_user` (unauthenticated users can only ever be shown `landing`, regardless of what they click).

```
if not st.session_state.auth_user and st.session_state.page != "landing":
    st.session_state.page = "landing"   # hard gate, enforced every render
```

### 7.3 Auth modal flow (`ui/components/auth_modal.py`)
- Rendered inline on the landing page (no navigation) when `auth_mode` is set.
- Register form: live per-keystroke duplicate flags via `auth.service.is_username_taken`/`is_email_taken` (Streamlit reruns the script on every widget change, so this is genuinely live, not just on-submit) rendered as a red tag under the field; on submit, `auth.service.register(...)` re-validates everything (including uniqueness) and raises `ValidationError` with the same field->message shape on failure — no page reload, same Streamlit rerun.
- Login form: single identifier input (username OR email) + password; `auth.service.authenticate(identifier, password)` tries username match first, then email.
- On success: `auth.session.log_in(user)` sets `auth_user` (via `to_session_payload` — never the password hash) and resets `auth_mode` to `None`. Login/register buttons are replaced by a sidebar (`app.py`): user badge, then the three page-nav buttons (Create New PC / Previous Builds / Community — needed once the user is past `landing`, since that's the only view with its own nav buttons), then a `Logout` button pushed toward the bottom via a CSS flex spacer.

### 7.4 Create Build flow (`ui/views/create_build.py`)
1. Mode selector (Budget / Workload / Free) if `create_mode` is `None`.
2. **Budget mode**: number input for ceiling → `engine.solvers.initialize_budget_build()` → part-picker grid, one row per category, each showing the current pin and an expander with the filtered, sorted candidate list.
3. **Workload mode**: profile selector + tier selector → `engine.solvers.allocate_workload_baseline()` → same part-picker grid.
4. **Free mode**: empty `build_draft`, all categories open immediately, part-picker shows the full catalog filtered only by compatibility against whatever is already pinned (`engine.solvers.get_compatible_candidates`).
5. A summary header (`_summary_header`, `ui/components/part_picker.py`'s companion in `create_build.py`) sits above the part-picker grid and stays current on every pick: total cost, `engine.compatibility.evaluate_build(...)` (`is_compatible`/`compatibility_score`/`issues`), and — on demand via an explicit "Analyze" click, not automatically on every keystroke — a synergy/bottleneck panel from `llm.client.analyze_build(...)`. Since `analyze_build` never raises, the panel always renders; a `st.badge` reads `"AI Engine"` when `response.source == "llm"` or `"Heuristic Baseline"` when `"heuristic"` (§6.1). Any component change invalidates the last analysis (`ui/state.py::set_component`/`remove_component` clear `build_draft_analysis`) so the panel never shows stale numbers for a build that's since changed. True CSS `position: sticky` for this header was attempted and found not to engage under Streamlit's flex/overflow DOM layout (verified live) — it's a prominent card at the top of the page, not a scroll-pinned one.
6. Each of the 8 core slots renders as its own card (`ui/components/part_picker.py`) in a 2-column grid: a `st.badge` reading "Selected" (green) or "Empty" (gray), the current pick's name/price/key-specs if any, and a `st.popover` drawer for changing it — candidate cards inside show price, a per-category "at a glance" spec line (socket/TDP/form-factor/dimensions, whichever apply), and a Value/Ratio Index score, sorted by the `st.segmented_control` at the top of the page (Cost / Compatibility / ValueIndex). In Budget mode with an active ceiling, `render_part_picker` receives the raw `budget_ceiling` (not a pre-subtracted number) and computes a **Minimum Reserve Threshold** itself: `Max_Slot_Cost = budget_ceiling - (cost of everything else already selected) - (cheapest compatible cost to fill every OTHER still-empty core category)`. Any candidate priced above that is disabled (not hidden) with a caption showing the exact overage — this is deliberately stricter than "can I afford this one part," since it also protects the *other* empty slots from being priced out before the user even gets to them. Reduces to the simple "ceiling minus spent" check once every other slot is already filled (reserve = 0 then).
7. "Save Build" → name field (+ optional "Publish to Community" checkbox) → `db.repositories.builds_repo.create_build()` (and `community_repo.create_post()` if publishing) → redirect to `my_builds` with the new build highlighted.

### 7.5 Previous Builds (`ui/views/my_builds.py`)
- Top filter bar: workload profile groupings (with nested chronological sort) OR global sort (`Cost High→Low`, `Cost Low→High`, `Date`) — mutually exclusive filter modes per intent.txt §5.
- When grouped by workload profile: a header per profile, builds beneath sorted by date descending.
- Otherwise: 3-column card grid (`ui/components/build_card.py`) showing name, total cost, bottleneck %, compatibility %, date, and quick actions (Clone, Edit, Share to Community, Delete).
- **Clone**: duplicates the build row (new `id`, `user_id` unchanged, `name` suffixed " (copy)"), lands on `create_build` in Free mode pre-filled.
- **Edit**: loads the build into `build_draft` and opens `create_build` in whatever `creation_mode` it was originally created under.
- **Share to Community**: opens the same publish modal as §7.4 step 7.
- **Delete**: `db.repositories.builds_repo.delete_build(build_id)` — a destructive action, so `build_card.py` requires a Yes/Cancel confirmation step before it fires (`render_build_card(..., confirm_labels={"Delete"})`); once confirmed, the build (and, via cascading FKs, its `build_components` and any `community_posts`/`community_comments`) is gone and the list refreshes immediately (`st.rerun()`).

### 7.6 Community (`ui/views/community.py`)
- Feed of `community_posts`, newest first, card shows title, author, build summary stats.
- Thread view: full spec list (reuses `build_card`'s detail layout), cost breakdown, `author_notes`, and `community_comments` thread with a comment box (auth required to post).
- **Fork/Customize**: sets `fork_source_build_id`, navigates to `create_build` with the source build's components pre-loaded into `build_draft` as a fresh, editable, unsaved draft.
- **Save to My Builds**: calls `db.repositories.builds_repo.create_build()` with the post's component set under the *current* user's `user_id`, as a new standalone row (no FK back-reference beyond the informational `forked_from_post_id`).

### 7.7 Theme definitions (`.streamlit/config.toml` + `ui/theme.py`)
- Base: dark theme by default (hardware/enthusiast audience convention).
- Palette (defined once in `ui/theme.py`, imported everywhere — no ad-hoc hex codes in view files):
  - `background`: `#0E1117`
  - `surface` (cards): `#161B22`
  - `primary accent`: `#3DDC97` (used for compatibility-positive states, CTAs)
  - `warning accent`: `#F2B134` (bottleneck/near-budget warnings)
  - `danger accent`: `#E5484D` (failed compatibility, duplicate-username/email flags)
  - `text primary`: `#E6EDF3`
  - `text muted`: `#8B949E`
- Typography: system default Streamlit font stack; component names in `font-weight: 600`, specs in muted/regular.

---

## 8. Non-Functional Requirements

- **Security**: passwords hashed with bcrypt (cost factor ≥ 12); no raw SQL string interpolation anywhere (parameterized queries only, enforced in `db.repositories`); `.env` never committed.
- **Portability**: DDL and repository queries must run unmodified against SQLite and PostgreSQL — no SQLite-only functions (e.g., no `datetime('now', ...)`; use application-side UTC timestamps or DB-agnostic `CURRENT_TIMESTAMP`).
- **Resilience**: any LLM failure degrades to deterministic-only scoring; the app must never crash or block a save because OpenRouter is unreachable.
- **Testability**: `engine/` functions are pure (no Streamlit, no network) and unit-testable in isolation; `llm/` tested against mocked HTTP responses, never live API calls in CI.
- **Performance**: part-picker candidate filtering must stay client-perceptibly instant (<200ms) for catalog sizes up to a few hundred rows per category — plain in-memory filtering over `components_repo` results is sufficient at this scale; no need for search infra.

---

## 9. Open Assumptions (flag if incorrect)

1. "Community" moderation/reporting is out of scope for v1 — any authenticated user can post/comment.
2. Multi-currency is out of scope; all prices are USD.
3. `benchmark_score` is a curated, hand-authored 0–100 relative performance number per CPU/GPU (not pulled from a live benchmark API) — acceptable for a course/demo-scale catalog.
4. No real-time multi-user collaboration on a single build; builds are edited by one user at a time.
5. Peripheral categories (NetworkCard, SoundCard, OpticalDrive) have no deterministic compatibility rules beyond being optional add-ons, per intent.txt's framing as "optional peripheral expansion slots."
