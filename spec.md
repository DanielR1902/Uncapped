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
│   ├── seed_admin_builds.py        # 12 admin-owned demo builds across all 3 modes (§4.5), idempotent
│   ├── seed_community_shares.py    # shares 6 of admin's builds + realistic comments (§4.6), idempotent
│   ├── backfill_workload_tier.py   # one-time backfill of Build.workload_tier for pre-existing rows, idempotent
│   └── repositories/
│       ├── __init__.py
│       ├── users_repo.py
│       ├── components_repo.py
│       ├── builds_repo.py
│       ├── community_repo.py
│       └── drafts_repo.py          # save_draft/get_user_drafts/get_draft/delete_draft (§3.9, §7.9)
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
│   │   ├── community.py
│   │   └── drafts.py                # §7.9 — lists/loads/deletes draft_builds rows
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
| workload_tier | TEXT | NULLABLE, CHECK IN (`Entry`,`Mid`,`High`,`Enthusiast`) — only meaningful for `creation_mode = "Workload"`; NULL for Budget/Free builds and for pre-existing Workload rows persisted before this column existed (backfilled via `db/backfill_workload_tier.py` where the original tier was known) |
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
| quantity | INTEGER | NOT NULL DEFAULT 1 — >1 only valid for `RAM`/`Storage`/peripheral categories |

Constraints: `UNIQUE(build_id, component_id)`.
Indexes: `idx_build_components_build` on (`build_id`).
Application-level invariant (not a DB constraint, enforced in `engine`/`db.repositories.builds_repo`): at most one row per `build_id` for each of the 8 core categories (CPU, Motherboard, GPU, RAM, Storage, PSU, Case, Cooler) — RAM and Storage are the two core categories where `quantity` may represent multiple identical kits/drives (§5.1 rules 9-10, §7.1's `build_draft["quantities"]`), but distinct RAM/storage *models* still get distinct rows. Saving a build (`ui/views/create_build.py::_save_actions`) writes each category's real `ui.state.get_quantity(...)` value here instead of always defaulting to 1 — reloading that quantity back into `build_draft["quantities"]` on Edit/Clone is a known follow-up gap, not yet implemented.

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

### 3.9 `draft_builds`

An in-progress `build_draft` snapshot, saved when the user chooses "Yes, Save Draft" from the unsaved-build-changes navigation dialog (§7.9) rather than committing a fully scored `Build` row via "Save build" (§7.4 step 9). Distinct from `builds`: no scores, no compatibility check, no publish path — just enough to restore the exact in-progress picks later.

| Column | Type | Constraints |
|---|---|---|
| id | INTEGER | PK |
| user_id | INTEGER | NOT NULL, FK → `users(id)` ON DELETE CASCADE |
| name | TEXT | NOT NULL |
| mode | TEXT | NOT NULL, CHECK IN (`Budget`,`Workload`,`Free`) |
| components_json | TEXT (JSON) | NOT NULL — `{category: component_id}`, same shape as `build_draft["components"]` |
| quantities_json | TEXT (JSON) | NOT NULL DEFAULT `'{}'` — `{category: count}`, same shape as `build_draft["quantities"]` |
| created_at | TIMESTAMP | NOT NULL DEFAULT CURRENT_TIMESTAMP |
| updated_at | TIMESTAMP | NOT NULL DEFAULT CURRENT_TIMESTAMP |

Indexes: `idx_draft_builds_user_updated` on (`user_id`, `updated_at`).

A brand-new table — created automatically by `Base.metadata.create_all()` in `init_db()`, no additive `ALTER TABLE` migration needed (unlike `builds.workload_tier`, §3.4).

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

**Catalog tier span** (12 components added in a later round to broaden budget/flagship coverage — corrected note: the catalog's highest-wattage PSU, the Corsair HX1200i at 1200W, was ALREADY sufficient for the highest-TDP CPU+GPU pairing then in the catalog per `engine.compatibility.check_psu_headroom`'s `(cpu_tdp + gpu_tdp + 50) * 1.3` formula — there was no real PSU under-provisioning bug; these PSUs were added purely to broaden the enthusiast/future-proofing tier, not to fix a shortage): PSU now spans 500W–1600W (Seasonic Prime TX-1300, be quiet! Dark Power Pro 13 1500W, Corsair AX1600i added); CPU adds a budget tier (AMD Ryzen 5 5500, Intel Core i3-12100F) and a flagship tier (Intel Core i9-14900KS); GPU adds an older-generation budget tier (NVIDIA RTX 3050, AMD Radeon RX 6600, Intel Arc A580); RAM adds a 128GB kit (Corsair Vengeance 128GB DDR5-5600); Cooler adds a 420mm AIO (NZXT Kraken 420, fitting the existing Fractal Design Define 7's `max_radiator_mm: 420`); Storage adds a PCIe 5.0 NVMe SSD (Crucial T700 2TB, `specs_json.pcie_generation: "5.0"`). Motherboard/Case chipset and size coverage was already broad (B450 through Z890, ITX through full ATX with GPU clearances up to 470mm) and needed no addition.

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

### 4.5 Admin demo builds (`db/seed_admin_builds.py`)

A sibling seeder (same idempotent/`force=True`-reseed conventions as §4.4) that attaches 12 realistic builds to the standing `admin` user seeded by §4.4's `_ensure_admin_user()` — it never creates that user itself, and raises if it's missing rather than creating a second admin-like account. Spans all three creation modes: 3 Budget (`initialize_budget_build` at ~700/1300/2400 USD ceilings), 7 Workload (one or more per each of the 5 real `WORKLOAD_PROFILES`, across a spread of `WORKLOAD_TIERS`, via `allocate_workload_baseline`), and 2 Free-mode builds hand-assembled from specific real catalog component ids rather than a solver (verified compatible via `evaluate_build` before being persisted — an incompatible plan is skipped with a printed warning, never silently written). One Free-mode build exercises the RAM/Storage quantity feature (§5.1 rules 9-10) directly: a motherboard with 5 real M.2 slots, two of the same NVMe drive via `quantities={"Storage": 2}`, checked against `engine.compatibility.resolve_quantity_limit` before being committed rather than assumed. Scores computed the same deterministic, network-free way as §4.4.

### 4.6 Admin community shares (`db/seed_community_shares.py`)

A sibling seeder that shares a fixed, deliberate subset (1 Budget, 3 Workload across different profiles, both Free-mode — 6 of §4.5's 12) of admin's builds to the community feed, via the exact same mechanism `ui/views/my_builds.py`'s own "Share to Community" button uses: `builds_repo.set_public(build_id, True)` + `community_repo.create_post(build_id, admin_id, title, author_notes)` — there is no dedicated "shared build" table, sharing is just the `is_public` flag plus a `community_posts` row pointing at the same `build_id`. Depends on (calls first, idempotently, never duplicates) both §4.4 and §4.5's seeders to ensure the admin user and its 12 builds exist. Comments come from §4.4's 5 existing persona users — never a new, separately-styled set of invented usernames — and are generated from each shared build's REAL persisted component rows and recomputed bottleneck data (never generic filler that could apply to any build), so a comment about "that AIO keeping the 5090 cool" only ever appears on the build that actually has both of those parts. Idempotent per-build (checks the community feed for an existing post before creating a duplicate) and never re-adds comments to an already-commented post even under `force=True`.

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

**Quantity-aware rules (9-10)** — added to support the RAM/Storage quantity multiplier (§7.1's `build_draft["quantities"]`, a lightweight `dict[str, int]` — `BuildState` itself stays exactly one `Component` per category; a full heterogeneous multi-item-per-category model was deliberately rejected as out of scope). Both take an optional second `quantities: dict[str, int] | None` argument (`None`/omitted behaves identically to every category being quantity 1 — fully backward compatible with every pre-existing caller):

9. **RAM capacity**: `quantity("RAM") * modules_per_kit("RAM") <= Motherboard.specs_json.ram_slots AND RAM.capacity_gb * quantity("RAM") <= Motherboard.specs_json.max_ram_gb`. `modules_per_kit` is the RAM component's real stick count, parsed from its `name`'s consistent `"(NxYGB)"` convention (e.g. `"16GB (2x8GB)"` -> 2 modules) via `_ram_kit_module_count` — no structured field for this exists in the catalog, so this is a best-effort name parse, falling back to 1 module only when the pattern isn't found. This replaces an earlier "each selected RAM kit occupies exactly one DIMM slot" simplification: a 4-slot motherboard with a 2-module kit now correctly caps quantity at 2 (2 kits × 2 modules = 4 slots), not 4.
10. **Storage slot capacity**: only fires when the selected Storage's `interface` contains `"NVMe"` (the only interface with a real slot-count constraint in this catalog — non-NVMe/SATA storage has no rule here): `quantity("Storage") <= Motherboard.specs_json.m2_slots`.

`engine.compatibility.resolve_quantity_limit(build_state, category) -> tuple[int | None, str]` is the single source of truth for "what's the real, catalog/motherboard-backed max quantity for this category, and why" — real `ram_slots // modules_per_kit` for RAM, real `m2_slots` for NVMe Storage only, `(None, honest_reason)` for anything else (no Motherboard selected, no RAM/Storage selected yet, or a non-NVMe Storage interface with no real port-count data in this catalog). Both `llm/advisory.py`'s `set_quantity` validation/heuristic and the UI's quantity input consume this one function rather than each recomputing their own slot-count logic.

`ui/state.py::resolve_effective_quantity_limit(build_state, category, quantities, budget_ceiling) -> tuple[int | None, str, str]` layers a BUDGET constraint on top of the physical one above, returning `(effective_max, reason, limit_kind)` where `limit_kind` is `"physical"` | `"budget"` | `"none"` — whichever constraint is tighter wins. This deliberately lives in `ui/`, not `engine/compatibility.py`: `budget_ceiling`/creation mode are UI-session concepts, not hardware compatibility facts, and a build can be perfectly compatible yet unaffordable (this project already keeps `CompatibilityReport` cost-unaware). `budget_ceiling=None` (Workload/Free modes, or Budget mode with no ceiling set) means no financial constraint applies — never a fabricated numeric cap (e.g. no invented "99" ceiling for unlimited modes); the financial half of the calculation holds every OTHER category's current (quantity-scaled) cost fixed and asks whether the remaining budget can afford one more unit of THIS category's currently-selected component, without reserving budget for other still-empty categories (that more elaborate concern belongs to the existing Minimum Reserve Threshold check for picking a brand-new component, §7.4 step 8, not to a quantity increment on an already-selected one).

Quantity deliberately does NOT feed into rule 8 (PSU headroom) — the catalog has no real per-unit wattage data for RAM/Storage, and this project never fabricates numbers to fill a gap; only `total_cost` (§7.1) scales with quantity.

`run_all_checks(build_state, quantities=None) -> list[RuleResult]` runs every applicable rule, rules 9-10 included. `evaluate_build(build_state, quantities=None) -> CompatibilityReport` wraps it into the audit report the rest of the app consumes: `{is_compatible: bool, compatibility_score: float, issues: list[str], results: list[RuleResult]}` — `issues` is just the messages of the failed results, surfaced directly to the UI.

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
- **Never exceeds the ceiling**: guaranteed not by the per-category reservation alone (see `enforce_ceiling` above) but by the repair pass that follows it — the reservation gets the build *close* to the ceiling in one pass; the repair pass is what makes "never exceeds" an actual invariant rather than a best-effort heuristic. As an absolute last resort, `_greedy_fill` checks the total once more after that repair pass; if compatibility constraints chained non-trivially enough across categories that it's still over (the reservation heuristic and `cheapest_fill_cost`'s fixed-order estimate can converge on slightly different totals in this rare case), it re-runs the repair pass a second time treating *every* category — including ones the caller pinned — as adjustable. A pin only ever yields to the ceiling once nothing else is left to trim. And for a from-scratch (unpinned) build specifically, if the ceiling is STILL not met after both repair passes, `_greedy_fill` falls back to `_true_minimum_build()` if that combination fits — `enforce_ceiling` only ever swaps one category at a time holding everything else fixed, so it can get permanently stuck above a ceiling that's only reachable by changing two categories together (this seed catalog's Case+Cooler pair being the concrete example — see the true-minimum-floor bullet below).
- **User can pin any category first** ("doesn't matter which part they start with" — per the detailing doc): the algorithm re-partitions remaining budget across whatever categories are still open, in the same fixed priority order (GPU/CPU first, since they dominate both cost and performance).
- **Dynamic filtering is per-candidate, not per-slot**: after every pin, `part_picker` UI re-queries candidates through `filter_compatible`, then disables any candidate that would leave too little to complete the other open core slots. That reserve (`cheapest_fill_cost` of the other open categories) is recomputed for *each candidate individually* — hypothetically slotting the candidate in first — because which candidate ends up in a slot can itself change what's cheapest elsewhere (an ITX Case forces a pricier SFX PSU than an ATX Case would; a Motherboard's socket changes the cheapest compatible CPU/RAM). A single reserve number computed once per slot, independent of which candidate is being considered, can silently under-count the true reserve for a specific constraining candidate — exactly the deadlock this check exists to prevent. This same Category Max Cap check also applies to the 3 optional peripheral slots (`ui/views/create_build.py` passes `budget_ceiling` into their `render_part_picker` calls too) — but since `CATEGORY_ORDER` never contains a peripheral name, a peripheral slot's own reserve calculation only ever counts the still-empty *core* categories (never another peripheral), matching "peripherals are 100% optional, never part of the reserve requirement," while the peripheral's own price is still checked against the remaining headroom like any other candidate.
- **Infeasible pins get auto-downgraded, not blocked — but a pinned peripheral is a fixed cost, not a downgrade candidate**: `enforce_ceiling`'s repair pass only ever trims categories the solver itself chose — pinned/pre-selected categories are its usual immutable constraint. But if the caller's own pins already exceed the ceiling, or leave less than `cheapest_fill_cost(selection, categories_to_fill)` (the cheapest possible cost to compatibly fill everything still open) for the rest, `_greedy_fill` calls `_downgrade_pinned_until_feasible`: it steps the most expensive *pinned CORE* categories down to progressively cheaper compatible alternatives (highest tier that's still cheaper), one at a time, most-expensive-pinned-first — the same price-descending/progress-flag pattern as `enforce_ceiling` above, just applied to pins instead of the solver's own picks — until it fits, or there's nothing left to downgrade (in which case it proceeds with whatever's cheapest, same graceful-degradation philosophy as an empty selection). A peripheral riding along in `selection` is deliberately excluded from this pass — its price is a fixed, off-the-top deduction from the budget available to the core build, never something this best-effort preservation step swaps out — only the final all-categories-adjustable safety net (see above) may ever touch a peripheral, and only as the absolute last resort once no core category is left to trim. Neither `initialize_budget_build` nor `on_user_pins_component` ever raises for any of this — a hard blocking error was tried and rejected as too much friction. This check (and any downgrade) is skipped entirely when `selection` is empty. In practice, today's UI never exercises this pin-downgrade path at all: `ui/views/create_build.py`'s "Apply budget & generate build" always calls `initialize_budget_build` with an EMPTY selection (see the from-scratch-rebuild bullet above), and manually picking one slot via its own picker (`_part_pickers`'s `on_select`) goes straight through `state.set_component` without re-running the solver or calling `on_user_pins_component` at all — the picker's own Category Max Cap disabling is what keeps a manual pick from ever exceeding the ceiling in the first place, so there's never a "the pin no longer fits" moment for this UI to react to. `_downgrade_pinned_until_feasible` remains exercised and tested at the engine level (a future UI entry point could reintroduce a pin-preserving re-solve and would get this behavior for free), just not reachable from Build Studio today.
- **Ceiling entry and a full from-scratch rebuild are one unified, commit-on-click action**: `_budget_controls`'s `st.number_input` for the ceiling carries no `min_value` — an earlier version set `min_value=minimum_possible_build_cost()` on the widget itself, but Streamlit's native out-of-range validation locks the field in an unrecoverable red "Press Enter to apply" state until the user manually fixes it, which is worse UX than a clean clamp. Instead, a single `"Apply budget & generate build"` button (`_apply_budget_and_generate`, wired as the button's `on_click` callback — not a plain post-widget `if st.button(...)` block, since Streamlit forbids assigning to a widget's own `st.session_state[key]` after that widget has already been instantiated in the same run, and a callback is the only place where overwriting the ceiling field's displayed value is legal) does all of it in one click: reads `st.session_state["budget_ceiling_input"]`, and if it's below `minimum_possible_build_cost()` (the TRUE floor — see below, NOT `cheapest_fill_cost({}, CATEGORY_ORDER)`), overwrites that same session-state key with the floor (so the field visibly snaps to it on the next render) and shows `st.toast("Budget set to minimum viable floor: $X")`; otherwise it takes the entered value as-is. Either way it commits the result to `build_draft["budget_ceiling"]`, **wipes every existing core and peripheral selection**, and calls `initialize_budget_build(ceiling, fill_peripherals_with_surplus=True)` with no seed at all — a deliberate full reset-and-regenerate, not an "adjust what I already have" one, so the very next render shows a fresh build (never a mix of old pins and new picks) with every part-picker's Category Max Cap recalculated against the newly committed ceiling. The *other*, incremental pin-preserving behavior described elsewhere in this section (re-partitioning remaining budget around whatever's already pinned) still applies exactly as before, but only through `on_user_pins_component` — i.e. only when a user changes ONE slot via its own picker, never through this button. `minimum_possible_build_cost()` is an exhaustive search (~2s against this catalog's size) — `ui/views/create_build.py` computes it once and caches the result in `st.session_state["_budget_floor_cost"]` so repeat clicks in the same session don't pay that cost again. `build_draft["budget_ceiling"]` is not live-synced on every rerun as the user types — it only changes when this button is actually clicked, so a part-picker always reflects the last *committed* ceiling, not whatever's mid-edit in the field.
- **Surplus budget auto-fills optional peripherals**: `fill_peripherals_with_surplus=True` (the flag "Apply budget & generate build" always passes) adds a Phase 2 after the normal 8-category fill: `_fill_peripherals_with_surplus` computes `surplus = ceiling - sum(core prices)`, then walks `PERIPHERAL_CATEGORIES` (`NetworkCard`, `SoundCard`, `OpticalDrive`, in that priority order — the only peripheral categories that exist in this catalog) picking the single most expensive still-affordable option per category and decrementing the surplus after each pick; a category is skipped if nothing fits what's left. Deliberately mirrors the core solver's own "spend as much as reasonable on the highest tier that fits" philosophy rather than `scoring.value_index` — with no compatibility rules differentiating peripherals from each other (§5.1 rule 5), `value_index` would degenerate to always preferring the cheapest option, the opposite of what spending a surplus is for. This can only ever reduce the ceiling/total gap, never risk it — every pick is bounds-checked against the shrinking surplus first — and it's opt-in (`fill_peripherals_with_surplus` defaults to `False`), so every OTHER caller of `initialize_budget_build` (the ~15+ existing engine tests included) keeps its exact prior core-only behavior unless it explicitly asks for this.

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
function allocate_workload_baseline(profile, target_tier = "Mid", include_peripherals = False):
    for tier in [Entry, Mid, High, Enthusiast]:  # ALL four, every call, regardless of target_tier
        for category in [CPU, Motherboard, GPU, RAM, Storage, PSU, Case, Cooler] (+ peripherals if opted in):
            candidates = workload_mappings_repo.get(category, profile, tier)
            candidates = filter_compatible(candidates, selection_so_far)
            selection[category] = pick_representative(candidates)  # median price within tier
        if total(selection) <= total(previous_tier):
            selection = escalate_above(selection, floor = total(previous_tier))  # see below
    return selection[target_tier]
```
`target_tier` defaults to `Mid` and is user-adjustable (Entry/Mid/High/Enthusiast) as a secondary control on the Workload mode screen. After the baseline is pre-allocated, the user can swap any part; every swap re-runs `filter_compatible` scoped to compatible + same-or-better tier candidates for the remaining categories, and surfaces compatibility-ranked recommendations (per intent.txt §3 Mode B).

**Strict cross-tier price ordering is a guarantee, not an emergent property**: `Cost(Entry) < Cost(Mid) < Cost(High) < Cost(Enthusiast)` must hold for every profile. It is NOT automatic from `workload_mappings`' tier tags alone — those tags are a curated, per-component "this part suits this workload at this tier" judgment call, not a strict price partition, so a tier's own median-per-category picks can (and, on this project's seed data, did for 4 of 5 profiles before this was enforced) sum to no more, or even less, than a cheaper tier's naive total. `allocate_workload_baseline` computes all 4 tiers in Entry→Mid→High→Enthusiast order on every call — even when only one tier was requested, since the invariant can't be checked or repaired looking at a single tier in isolation — and whenever a tier's own picks don't clear the previous tier's total, `_escalate_workload_tier_above_floor` repairs it: swap that tier's cheapest-relative-to-itself categories (cheapest first, for the most proportionate fix) up to progressively pricier compatible options, trying that SAME tier's own tagged pool first (preserves its curation as much as possible) and only widening to any-tier-for-this-profile, then the full compatible catalog, if the tier's own pool runs out of upgrade headroom before clearing the floor. Stops gracefully (never raises) if even the full catalog has nothing pricier left, matching this module's graceful-degradation philosophy elsewhere (`_greedy_fill` and friends). `include_peripherals=True` folds `PERIPHERAL_CATEGORIES` (`NetworkCard`/`SoundCard`/`OpticalDrive` — the only ones that exist) into each tier's build from the start, so this price guarantee holds with peripherals included too — opt-in, defaulting to `False`, exactly like `initialize_budget_build`'s `fill_peripherals_with_surplus`, so existing callers keep their exact prior core-only behavior. `ui/views/create_build.py`'s "Generate baseline build" always passes `include_peripherals=True`; the analysis auto-trigger (§7.4) fires for a Workload-generated build the same as any other, since it only checks that the 8 core categories are present.

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

### 6.6 Build Advisory (`llm/advisory.py`) — a second, independent OpenRouter feature
Distinct from §6.1–§6.5's synergy/bottleneck/compatibility analysis (a different question — "what should I change" vs "how good is what I have") — kept in its own module rather than folded into `client.py`, matching this package's one-concern-per-file convention.

`get_build_advisory(build_state, mode, current_budget_or_cost, profile=None, bottleneck_info=None, quantities=None) -> dict` follows `client.py::analyze_build`'s exact never-raises, `source`-tagged pattern (its own internal `AdvisoryUnavailableError`, never `LLMUnavailableError`) and returns:
```json
{
  "pros": ["<string>", ...],
  "cons": ["<string>", ...],
  "within_budget": {
    "explanation": "<string>",
    "swaps": [{"action": "swap", "category": "<string>", "replace_with_id": "<int>"}, ...],
    "can_optimize_further": "<bool>"
  },
  "stretch_budget": {
    "explanation": "<string>",
    "actions": [
      {"action": "swap", "category": "<string>", "replace_with_id": "<int>"} |
      {"action": "set_quantity", "category": "RAM"|"Storage", "quantity": "<int>"},
      ...
    ],
    "added_cost_usd": "<float>"
  },
  "source": "llm" | "heuristic"
}
```
All four content fields (`pros`, `cons`, `within_budget`, `stretch_budget`) are REQUIRED (no defaults) on `BuildAdvisoryResponse` — a payload missing any of them, or a nested `WithinBudgetAdvice`/`StretchBudgetAdvice` missing one of ITS fields, fails `pydantic` validation and falls through to the heuristic path, the same "must always be present" precedent `BuildAnalysisResponse`'s nested fields already set (§6.4). `swaps`/`actions` may legitimately be an empty list (no beneficial action exists) — that alone is never a validation failure. **`within_budget.swaps` and `stretch_budget.actions` are two DIFFERENT shapes** — `within_budget` stays a plain list of swaps only (out of scope for the multi-category stretch fix below); `stretch_budget.actions` is a discriminated union (`SwapAction | QuantityAction` in `llm/schemas.py`, tagged by each item's `"action"` field) so a stretch recommendation can propose either swapping a component or increasing RAM/Storage quantity.
- `pros`/`cons`: concise strengths/limitations of the CURRENT build (compatibility status, balance between CPU/GPU, etc.) — short bullet-style strings, rendered as a 2-column colored list in the UI (§7.4 step 6).
- `within_budget`/`stretch_budget` are **machine-executable**, not just descriptive text: `explanation` is the free-text advice, and `within_budget.swaps`/`stretch_budget.actions` are concrete instructions a caller can apply directly to `build_draft` (§7.4 step 6's "Apply" buttons) — following the mode-specific objective below. `within_budget.can_optimize_further` signals whether a further beneficial swap remains after this one; `stretch_budget.added_cost_usd` is the exact numeric total of all its actions' price deltas versus the current picks (never an estimate) — for a `set_quantity` action, that's one more unit at the SAME price as the currently-selected component (the only defensible number for "one more of what's already selected").
- **`stretch_budget` is no longer confined to the single bottleneck category** (a real, fixed bug — the old prompt/heuristic gave up the instant the bottleneck category alone had no headroom, e.g. an already-priciest-compatible CPU, even when a GPU/RAM/Storage/Cooler upgrade was obviously available). Both the LLM prompt and the heuristic fallback now evaluate, in priority order: the bottleneck category, then GPU, then a RAM quantity increment (`set_quantity`, bounded by the motherboard's real `ram_slots`), then a Storage quantity increment (NVMe drives only, bounded by real `m2_slots`), then Cooler — stopping at the first real, catalog-priced option found, and only reporting "no stretch upgrade available" if every one of those was checked and came up empty.
- **Zero-hallucination id/quantity enforcement**: every `replace_with_id` must be a real, currently-valid compatible-candidate id for its stated `category`; every `set_quantity.quantity` must be a positive integer that does not exceed the real motherboard slot count for that category (and `set_quantity` is only ever valid for `"RAM"`/`"Storage"` — any other category is rejected outright, as is a `"Storage"` `set_quantity` when the current Storage pick isn't NVMe, since only NVMe has a real slot-count constraint in this catalog). Enforced by the system prompt AND, authoritatively, by `llm/advisory.py::_validate_advisory_actions`, a post-parse guard that cross-checks every action against fresh `engine.solvers.get_compatible_candidates`/the Motherboard's real `specs_json` and raises (falling to the heuristic path, exactly like a schema-validation failure) if anything doesn't check out. The prompt-level instruction alone is never trusted as sufficient.
- `mode` is one of `"Budget"`/`"Workload"`/`"Free"`; `current_budget_or_cost` is the Budget-mode ceiling when `mode == "Budget"`, otherwise the build's current total cost (Workload/Free have no ceiling — §5.2 vs §5.6). `bottleneck_info`, if omitted, is computed internally via `engine.scoring.bottleneck_percentage_baseline`; when the UI already has a fresh `build_draft_analysis` (§7.1), it passes that result's `bottleneck` field through instead of forcing a redundant recomputation (`_resolve_bottleneck` accepts either shape). `quantities`, if omitted, defaults every category to quantity 1 for the heuristic's reasoning (e.g. "can I add a 2nd RAM kit" needs to know the current count is 1, not fabricate it) — `ui/views/create_build.py` always passes the build's real `build_draft["quantities"]`.
- **Mode-specific optimization objectives** (`llm/advisory.py::SYSTEM_PROMPT`'s "MODE-SPECIFIC OBJECTIVES" section, applied by the model against the payload's `mode`/`workload_profile`/`remaining_budget` fields — and mirrored deterministically in the heuristic fallback's wording):
  - `"Budget"`: optimize strictly within `remaining_budget` (`max(0.0, current_budget_or_cost - sum(component prices))`, computed internally and included in the LLM payload). If `remaining_budget` is ~0 (ceiling already maxed), `within_budget` may only propose a cost-neutral swap or a paired downgrade-to-upgrade reallocation — never a plain upgrade that would exceed the ceiling.
  - `"Workload"`: no ceiling exists; optimize for alignment with `workload_profile` instead — `within_budget` swaps prioritize whichever category matters most for that profile (GPU/VRAM for Gaming/Design, CPU cores/RAM for Programming/Video Editing); `stretch_budget` targets the next tier's typical requirements for that same profile, across whichever category (per the fallthrough above) gives the best real improvement.
  - `"Free"`: no ceiling exists; optimize for bottleneck mitigation and CPU/GPU platform balance — `within_budget` rebalances by downgrading an over-specced component to fund the primary bottleneck; `stretch_budget` targets whichever high-impact category gives the best real improvement within the allowance, per the same fallthrough.
- **Grounded in real catalog data, never invented part names or prices**: the prompt includes, per core category, the current pick plus its immediate cheaper/pricier compatible neighbors by price — fetched via `engine.solvers.get_compatible_candidates`, never `db.repositories.components_repo` directly (this package's allowed-imports boundary, §6 intro; `engine.*` is the only legitimate path to the catalog from here). The system prompt's strict-arithmetic rule requires every stated price delta to exactly equal a real catalog price difference, and requires the model to say so plainly rather than invent an action when no beneficial one exists.
- **No standalone `$` in prices**: the system prompt explicitly instructs the model to write amounts as `"600 USD"`/`"USD 600"`, never a bare `"$600"` — Streamlit's markdown renderer treats a matching pair of `$` characters in one string as inline LaTeX/math (KaTeX), garbling plain-text prices. `ui.format.sanitize_markdown` is a client-side backstop that replaces any stray `$` with `"USD "` before rendering, in case the model ignores the instruction — a shared helper (also guards free-text user content like a community post title combined with an appended price, §7.6), not private to one view.
- **Heuristic fallback** (network-free, on any failure): for `within_budget`, downgrade the OTHER (stronger/over-provisioned) side of CPU/GPU to the cheapest compatible option that's still cheaper than its current pick, freeing budget for the bottlenecked side, PLUS (additive) a paired upgrade swap for the limiting category if a compatible option exists that's affordable within the exact savings just freed up (both land in `swaps`, both catalog-priced, no fabrication). For `stretch_budget`, the priority-ordered fallthrough described above (bottleneck category → GPU → RAM quantity+1 → Storage quantity+1 → Cooler), stopping at the first real option; an empty `actions` list only after all five have been genuinely checked. Every action is real and catalog-priced (or slot-count-bounded for `set_quantity`) and phrases its `explanation` per the same mode-specific objective as the LLM path. `within_budget.can_optimize_further` is computed by applying whichever swaps were found to a hypothetical build and re-running `bottleneck_percentage_baseline` on it — true only if that hypothetical build is still not `"Balanced"`. `stretch_budget.added_cost_usd` is the exact price delta of whichever action won the fallthrough, `0.0` when none exists. `pros`/`cons` are derived from `engine.compatibility.evaluate_build`'s `is_compatible`/`issues` and the bottleneck direction (e.g. "well balanced" vs an imbalance callout), with a neutral truthful fallback line when neither applies.
- **No persistent cache**: unlike `analyze_build`, a successful advisory response is NOT written to `llm_cache` — `llm_cache`'s schema/cache-key scheme is shaped for `analyze_build`'s request/response, and this feature only needs the UI-session-level caching in §7.4 below. Never auto-triggered either (§7.4) — always an explicit click, since it's an on-demand deeper analysis, not part of "what a complete build inherently shows."

### 6.7 Concierge Chat (`llm/concierge.py`) — a third, independent OpenRouter feature
A conversational assistant, distinct from both §6.1–§6.5 (per-build analysis) and §6.6 (per-build advisory) — it answers free-form questions about the catalog and community builds, and can assemble a whole new build from a natural-language request. Kept in its own module for the same one-concern-per-file reason as `advisory.py`.

`get_concierge_response(user_message, conversation_history, catalog_summary, community_summary, current_build_context=None, advisory_context=None) -> dict` follows the exact same never-raises, `source`-tagged, single-shot pattern as `client.py::analyze_build`/`advisory.py::get_build_advisory` (its own internal `ConciergeUnavailableError`, never the other two modules' exceptions) — ONE system prompt + ONE payload + ONE `/chat/completions` call, not genuine multi-turn tool-calling (this project has never used, and does not need, OpenRouter's function-calling API). It returns:
```json
{
  "reply": "<string, at most 2 sentences>",
  "action":
    {"type": "load_build", "components": {"<category>": "<int id>", "..."}, "explanation": "<string>"} |
    {"type": "modify_build", "components": {"<category>": "<int id>", "..."}, "quantities": {"<category>": "<int>", "..."}, "explanation": "<string>"} |
    {"type": "navigate", "navigate_to": "landing" | "create_build" | "my_builds" | "community" | "drafts",
      "filters": {"build_type": "All" | "Budget" | "Workload" | "Free", "max_price": "<float>" | null,
                  "domain": "<string>" | null, "tier": "<string>" | null} | null,
      "reset_mode": "<bool>"} |
    {"type": "save_build", "name": "<string>", "destination": "draft" | "build", "explanation": "<string>"} |
    {"type": "publish_build", "author_notes": "<string>" | null} |
    {"type": "open_community_build", "post_id": "<int>"} |
    null,
  "source": "llm" | "heuristic"
}
```
`navigate`'s `filters` is an optional refinement of intent 5 below (defaults to `null`) — added specifically so a Concierge navigation request can also express a Community filter constraint without needing a second chat round-trip. `navigate`'s `reset_mode` (defaults to `false`) is a second optional refinement, meaningful only with `navigate_to: "create_build"` (see intent 5 below). Leaving `create_build` via `navigate` performs NO database write of any kind (§7.9) — `ui.state.teardown_builder()` only resets session state; an earlier revision of this schema instead carried an LLM-controlled `save_as_draft: bool` field for the same purpose, removed as unreliable (see intent 5 below for the full history of this feature's redesigns). `open_community_build` is intent 8 (below): a deep-link straight to one specific, already-shared community post's thread view, carrying that post's real `post_id` (never a `build_id` — a `CommunityPost` and the `Build` it wraps are different rows with different id sequences).
- **`catalog_summary`/`community_summary` are pre-fetched by the CALLER (`ui/`), never queried by this module.** `llm/` is never allowed to import `db.repositories.components_repo`/`builds_repo`/`community_repo`/`users_repo` directly (§ intro to §6, `llm/CLAUDE.md`'s "Allowed imports") — `ui/` (which *is* allowed to call `db.repositories.*` directly) fetches the full component catalog (155 components across 11 categories — small enough to summarize entirely in one prompt payload, no retrieval/filtering infrastructure needed) and every currently-shared community post (a `CommunityPost` whose `.build.is_public` is true — there is no separate `community_builds` table; §3.6/§3.4) and passes both in as plain arguments, exactly like `advisory.py` receives `build_state` from its caller. `current_build_context` (optional) is a similar caller-assembled snapshot of the user's currently active `build_draft` — `{"mode", "budget_ceiling": float | None, "components": {category: {"id", "name", "price_usd"}}, "quantities": {category: int}}`, or `None` when no draft is in progress — that supports the incremental-modification intent below and the budget guardrail. `advisory_context` (optional) is a caller-fetched `get_build_advisory` (§6.6) result — `{"pros", "cons", "within_budget", "stretch_budget", "source"}`, or `{}` when no advisory has been computed for the active build yet — that supports the optimization/analysis intent below; this module cannot compute it itself (`get_build_advisory` needs a real `BuildState` of ORM rows, which requires database access `llm/` doesn't have), so `ui/components/chat_assistant.py::_advisory_context` pre-fetches it, gated behind a cheap local keyword heuristic (`_looks_like_analysis_request` — "optimi"/"analy"/"upgrade"/"advice"/"recommend"/"improve"/"bottleneck" substrings) so a real advisory LLM call isn't made on every chat turn regardless of relevance, and reusing `st.session_state["advisory_cache"]` (§7.1/§7.4 step 6 — same cache, same cache-key shape) when the user already triggered an equivalent advisory read via the "✨ Get AI Analysis & Upgrade Path" button.
- **Eight intents**, all handled by the same single call and disambiguated by the system prompt reading the user's message:
  1. *Catalog questions* ("What CPUs do you have?") — answered from `catalog_summary` only, real names/prices, `action: null`.
  2. *Community recommendations* ("recommend a gaming build under 2000 USD") — answered from `community_summary` only (filtered/reasoned over by the model, e.g. `workload_profile`/`total_cost`), naming real post titles/prices, `action: null`.
  3. *Build-me requests* ("Build me a PC with an RTX 4070 and Ryzen 5800X3D") — the model fuzzy/substring-matches named parts against real `catalog_summary` entries, fills in the remaining unmentioned core categories (CPU/Motherboard/GPU/RAM/Storage/PSU/Case/Cooler) with sensible real picks, and returns a `load_build` action mapping every filled-in category to a real catalog id (a whole NEW build, replacing any current draft). The model does not run real compatibility checks itself (§5.1's `engine.compatibility` is the only source of pass/fail) — the caller re-validates after this returns, same as any other build assembly path. If a named part genuinely isn't in the catalog, the model says so in `reply` and returns `action: null` rather than inventing a placeholder id. **No aggregate totals rule**: the model must never state an aggregate/total build cost figure in `reply` (confirmed via a live reproduction, it is unreliable at summing many line-item prices and has been observed parroting back the user's REQUESTED budget as if it were the computed total) — it may still cite individual component names/prices straight from `catalog_summary`, but describes the build qualitatively rather than quoting a grand total; `ui/components/chat_assistant.py` appends the real, Python-computed total separately (§7.8).
  4. *Incremental modification requests* ("add a network card and optical drive", "bump my storage to 2") — only meaningful when `current_build_context` shows an active draft; returns a `modify_build` action whose `components` names ONLY the categories being newly added/changed (never repeats unchanged categories — it's a patch, not a replacement) and whose `quantities` names a TOTAL desired count for `"RAM"`/`"Storage"` only (e.g. "add another" on a qty-1 category means `quantities: {"Storage": 2}`, not a delta). With no active draft to modify against, the model says so in `reply` and returns `action: null`. The same no-aggregate-totals rule from intent 3 applies here too — never state the build's new resulting total cost after the patch. **Budget guardrail**: checked before returning a `load_build`/`modify_build` action here — only when `current_build_context["mode"] == "Budget"` and a real `budget_ceiling` figure is present (if absent, the rule is skipped entirely — never guessed); if the proposed action's resulting total (computed from real catalog/context prices only) would exceed the ceiling, the model returns `action: null` instead and replies with the exact template `"This upgrade will exceed your budget by USD {delta}. Would you like to proceed anyway?"`. On the user's next message, the model checks its own immediately-preceding turn in `conversation_history` — only a plain affirmative directly answering THAT specific question (never a "yes" answering something else, e.g. a different confirmation) causes it to return the original action on this later turn. This is a prompt-only mechanism — no new session-state key tracks it (the retired `concierge_pending_action` key is not reused for this); `conversation_history` alone gives the model everything it needs.
  5. *Pure navigation requests* ("take me to Community", "show my previous builds", "take me to drafts", "back to the main feed", "let me select a new mode") — returns `{"type": "navigate", "navigate_to": ...}` with no build mutation; `navigate_to` is constrained to this app's real page keys (`"landing"`/`"create_build"`/`"my_builds"`/`"community"`/`"drafts"` — there is no `"previous_builds"` key; that's only the nav button's label) by the schema's own `Literal` type. Three optional refinements:
     - *Community filters* ("show gaming builds under 2000", "take me to budget builds under 1500") — when `navigate_to == "community"` and the request also names a build-type/domain/tier/price constraint, the model populates `filters` mirroring `ui/views/community.py`'s REAL cascading filter toolbar shape (a mode selectbox, then a mode-specific sub-filter — NOT 3 independent fields): `{"build_type": "All"|"Budget"|"Workload"|"Free", "max_price": float|null, "domain": string|null, "tier": string|null}`, only filling in the field(s) relevant to the chosen `build_type` (`max_price` for `"Budget"`, `domain`/`tier` for `"Workload"`) and leaving the rest `null`. The model does not need to guess the REAL selectable price-step string or exact domain/tier casing — `ui/views/community.py::_apply_pending_community_filters` (§7.6) resolves a requested `max_price` to the smallest real price-step that covers it (or the largest step if the request exceeds all of them) and matches `domain`/`tier` case/whitespace-insensitively against whatever's actually currently shared, falling back to `"All"`/`"All Prices"` on no match — never trusting an LLM-authored string to already be a real selectbox option. `filters` stays `null` for a plain "take me to X" with no constraint, or whenever `navigate_to` isn't `"community"`. Navigating to `"community"` also always clears any currently-open single-post thread view (`selected_post_id`, §7.1/§7.6) so "back to the main feed" genuinely returns to the feed rather than re-showing whatever thread was last open — handled centrally by `ui.state.navigate_to_page` (§7.1), not something the model needs to request explicitly.
     - *Reset mode* (`reset_mode: bool`, only meaningful with `navigate_to == "create_build"`) — for "let me select a new mode"/"choose a different mode"/"reset the builder" phrasing, exposing the EXISTING "⬅ Change mode" button (`ui/views/create_build.py`) via chat rather than a new mechanism: the identical `ui.state.teardown_builder()` call (§7.9), landing back on the Budget/Workload/Free Custom mode-selection screen. A plain "take me to the build studio" request (no explicit mode-reset wording) omits this field (defaults `false`) and just opens whatever's already there.
     - *Never a draft save* — this action carries no field for saving anything, and the model should never mention/promise saving to Drafts in `reply`. Leaving `create_build` via `navigate` performs NO database write of any kind (§7.9) — `ui.state.teardown_builder()` only resets session state. This exact concern has gone through several prior designs, ALL since removed: an LLM-controlled `save_as_draft: bool` field, an explicit "Save Draft or Discard?" confirmation dialog (`ui/components/nav_guard.py`, deleted), and a silent unconditional auto-save-on-exit with a flash banner. The CURRENT, later product decision: the "Save as draft" checkbox in `ui/views/create_build.py`'s manual Save UI (§7.4 step 9) is the single, explicit source of truth for creating a draft — leaving the builder without using it simply discards the in-progress build.
  6. *Optimization/analysis requests* ("analyze my build", "how can I optimize this?", "any upgrade suggestions?") — when `advisory_context` is present and non-empty, the model synthesizes ONLY the single most impactful, actionable point from it (whichever of `within_budget.explanation`, `stretch_budget.explanation`, or the strongest `cons` entry best answers what the user asked) into a plain 1-2 sentence reply — never dumping the raw `pros`/`cons`/`within_budget`/`stretch_budget` structure or describing every item. When `advisory_context` is empty (the keyword heuristic above didn't fire, or no advisory has ever been computed for this build), the model says so plainly and points the user to the real "✨ Get AI Analysis & Upgrade Path" button rather than fabricating advice. Always `action: null` — applying a suggested change is a separate, later, explicit intent-4 request.
  7. *Save & publish requests* ("Save this PC to my list", in English — see the English-only note below) — deliberately NOT zero-click, a reversal of an earlier "fire immediately with an auto-generated name" design: on the FIRST such message, when `current_build_context` shows an active build with at least one component, the model does NOT return an action yet — it asks BOTH "what name would you like to give this build?" and "should I save it as a Draft or a finished Build?" together in `reply`, and returns `action: null`. With no active build, it says so instead and returns `action: null`. On the user's next message, the model reads its own immediately-preceding turn in `conversation_history` (the same discipline as intent 4's budget guardrail) to confirm it just asked that combined question, then extracts a name and a destination ("draft" — "draft"/"in progress"/"wip" wording — or "build" — "finished"/"final"/"save it properly"/"full build" wording) from the reply. If the user supplied BOTH clearly, the model returns the `save_build` action now — `{"type": "save_build", "name": "<the user's exact name>", "destination": "draft"|"build", "explanation": "<string>"}` (the actual persist then happens immediately, via `db.repositories.drafts_repo.save_draft` for `"draft"` or `db.repositories.builds_repo.create_build` for `"build"`, §7.8). If the user answered only ONE part, the model does not guess the other — it asks specifically for the still-missing piece, returns `action: null` again, and repeats the same "read your last turn" discipline on the following message. Once `save_build` fires: for `destination == "build"`, the SAME turn's `reply` confirms the save using the real name just given ("Saved as '<name>'! Would you like to publish it to the Community as well?") and opens the publish follow-up flow below; for `destination == "draft"`, the reply simply confirms the draft was saved under that name and the flow ends there — a draft has no publish path anywhere in this app's real architecture (`community_repo.create_post`/`builds_repo.set_public` only ever operate on a real `Build` row), so the model must NOT ask about publishing after a draft save. The publish follow-up flow itself (reachable only after a `"build"`-destination save) is unchanged: resolved EXACTLY like intent 4's budget guardrail — by the model reading its own immediately-preceding turn in `conversation_history`, never inventing new session-state confirmation machinery: a "no" answering the publish question ends the flow (`action: null`, build stays private); a "yes" answering it asks whether to add an introductory description (`action: null`, still gathering info); a "no" answering the description question returns `{"type": "publish_build", "author_notes": null}`; a "yes" answering it asks for the actual text (`action: null`); and the very next message, once the model's own last turn asked for that text, IS treated as the description and returns `{"type": "publish_build", "author_notes": "<verbatim text>"}`. A bare yes/no answering some OTHER question (e.g. the budget guardrail's own question) must never be misread as advancing this flow — only when the model's own immediately-prior turn was specifically that exact question. `publish_build` carries no build id (the model cannot know one) — the caller resolves which build via `st.session_state["concierge_last_saved_build"]` (§7.1/§7.8), set only by a `"build"`-destination `save_build` action applied earlier in the same session (a `"draft"`-destination save never sets it).
  8. *Deep-link to a specific community build* ("open the build we just submitted", "show build Weekend Gaming Rig", "open my Gaming Rig from community") — distinct from intent 2 (COMMUNITY RECOMMENDATIONS, which only describes/recommends posts in `reply` without navigating): the user wants to be taken directly to ONE specific, already-shared post's own thread view. The model resolves which post against `community_summary`'s real entries (fuzzy/substring, case-insensitive title match for a named build; for a vague recency phrase with no name given, only if `community_summary` actually carries a usable ordering/timestamp signal — otherwise the model says plainly it cannot determine "most recent" and asks the user to name the build, rather than guessing) and returns `{"type": "open_community_build", "post_id": <real post_id>}` — `post_id`, never `build_id`: a `CommunityPost` and the `Build` it wraps are two different rows with two different id sequences (`db/models.py::CommunityPost` has its own `id` plus a separate `build_id` FK), and `community_summary` already gives the model both for every post, so `post_id` is the zero-extra-lookup field that matches `ui/views/community.py::render()`'s real `selected_post_id`-keyed routing exactly. If nothing in `community_summary` matches, the model says so in `reply` and returns `action: null` — never inventing a `post_id`. **Zero-hallucination enforcement**: `llm/concierge.py::_validate_action` cross-checks `post_id` against the real `"post_id"` values in the `community_summary` given for that call, raising `ConciergeUnavailableError` (-> heuristic) on any mismatch, the same precedent as every other action type's id guard. Applied by `ui/components/chat_assistant.py::_apply_concierge_action` with no database write at all: `st.session_state["page"] = "community"` and `st.session_state["selected_post_id"] = action["post_id"]` — `ui/views/community.py::render()` already checks `selected_post_id` at the very top of its own function (§7.6), so no changes were needed in that module for this to land on the post's thread view on the very next render.
- **English-only rule** (supersedes an earlier multilingual/Hebrew-reply design): the system prompt now instructs the model to communicate ONLY in English — a message written in any other language gets a polite, concise English reply stating that the assistant currently only operates in English, with `action: null`; the model does not attempt to fulfill (even partially) a non-English request. This is prompt-only, matching this feature's existing "style/UX concern, not a correctness one" precedent for the conciseness/advisory-synthesis rules — there is no Python-side language-detection gate rejecting a non-English message before it reaches the model (the model itself both recognizes the language and produces the refusal). §7.8 still documents `_render_chat_message`/`_looks_like_hebrew`'s RTL/BiDi rendering, which remains in place for a Hebrew-typing user's OWN message (rendering doesn't care about the model's reply-language policy) even though a real assistant reply will no longer itself be Hebrew.
- **Build-intent disambiguation**: intent 3 (BUILD-ME REQUESTS) explicitly claims any "build/assemble/put together/configure/create a PC" phrasing — with or without named parts or a stated budget, e.g. a bare "build a gaming rig" — never intent 2 (COMMUNITY RECOMMENDATIONS) or intent 5 (PURE NAVIGATION); the model must not redirect to Community or merely recommend an existing shared build in response to a build-me request. Intent 2 is reserved for a request that explicitly asks to browse/see/get a recommendation FROM the existing community feed rather than asking the Concierge to build one.
- **Zero-hallucination id/shape enforcement is authoritative in Python, not just prompt wording**: after `ConciergeResponse.model_validate(raw)` succeeds (which alone already rejects an invalid `navigate_to`, or an invalid `filters.build_type`, via their `Literal` types), `llm/concierge.py::_validate_action` re-checks every `(category, id)` pair in a `load_build`/`modify_build` action's `components` against the real `catalog_summary` it was given, every key in a `modify_build`'s `quantities` against the `"RAM"`/`"Storage"`-only constraint, and an `open_community_build` action's `post_id` against the real `"post_id"` values in the `community_summary` it was given — raising `ConciergeUnavailableError` (falling to the heuristic) on any violation, the same "never trust the LLM's stated data without cross-checking" precedent as `advisory.py::_validate_advisory_actions`. A `modify_build`'s requested quantity NUMBER itself is not clamped here (this module has no `engine`/`db` access to check real slot/budget limits) — the caller re-derives the real achievable ceiling via `ui.state.resolve_effective_quantity_limit` (the same function the manual quantity stepper uses) before ever applying it. `save_build`/`publish_build` carry no LLM-supplied catalog ids or categories at all (they act on `current_build_context`, already-known-real caller data, an optional free-text `author_notes`, and — for `save_build` — the user's own literal `name`/`destination` answers), so `_validate_action` has nothing new to cross-check for either — it treats them as a pass-through, the same as `navigate`. `save_build`'s `name`/`destination` fields ARE enforced at a different layer, though: both are REQUIRED (no default) on `ConciergeSaveBuildAction` itself, so a response that tries to return this action before the model has actually gathered both values from the user fails Pydantic validation outright (falling to the heuristic, exactly like a hallucinated id would) — a stronger, structural guarantee of "the model must have asked first" than a prompt instruction alone. `navigate`'s `filters.max_price`/`domain`/`tier` are likewise NOT cross-checked here: unlike `load_build`/`modify_build`'s catalog ids, they don't reference a fixed catalog at all — the real valid values are "whichever workload domains/tiers/price steps happen to be currently shared," live data only `ui/views/community.py` computes at render time, so there is nothing fixed here for this module to hallucination-check against; `ui/views/community.py::_apply_pending_community_filters` (§7.6) is what resolves a mismatched request gracefully instead. `navigate` carries no `save_as_draft`-style field at all — leaving `create_build` via `navigate` performs no database write of any kind (§7.9), decided entirely in Python (a pure session-state reset), never by the model or this schema, so there is nothing for either the prompt or this guard to reason about for it. The budget-guardrail reply template and the advisory-synthesis conciseness rule (intent 6) are both prompt-only, like the general conciseness rule below — a soft UX guardrail, not a correctness problem, so there is no Python-side enforcement of either.
- **Strict brevity is a prompt-only rule, not a runtime rejection**: the system prompt's "STRICT BREVITY RULE" instructs "exactly 1 or 2 short sentences, state only what you did or the exact answer, then stop" — with explicit BAD/GOOD examples forbidding hardware-history or generic-build-philosophy tangents (e.g. never explain why sound cards/optical drives are uncommon; just report what was added). Unlike the zero-hallucination checks above (a wrong id is a correctness problem), a slightly-longer-than-ideal reply is a style preference, and rejecting an otherwise-good answer over sentence count would trade real usefulness for cosmetics, so there is no Python-side length enforcement. The rule explicitly scopes itself to the `reply` field only — a real, confirmed regression showed the model occasionally over-applying it to an action's internal `explanation` field too, omitting it entirely and failing Pydantic validation (which requires it with no default) for an otherwise perfectly valid action. Fixed on both sides: the prompt now explicitly says brevity does not apply to `explanation`, AND (the authoritative fix, since a wrong/missing field is a correctness problem) `explanation` is now optional (`= ""`) on every Concierge action type — it has no functional consumer anywhere in `ui/` in the first place, only `reply` is ever shown to the user.
- **Component-id shape coercion (`_coerce_component_id_shapes`)**: a real, confirmed bug — live reproduction of a second-turn "upgrade the storage/RAM" request (with `advisory_context` also present) showed the model sometimes echoes `current_build_context["components"]`'s own `{"id", "name", "price_usd"}` object shape back into its OWN `load_build`/`modify_build` action's `components` field, which must be the plainer `{category: <int id>}`. Pydantic correctly rejects the malformed shape (falling back to the heuristic reply), which is safe but means a valid, simple request silently fails to apply. Since the real id is still recoverable from inside the malformed object, `get_concierge_response` coerces `{"id": <int>, ...}` values down to the bare id BEFORE `ConciergeResponse.model_validate` runs, rather than relying solely on a prompt-level "SHAPE WARNING" (also added, to reduce how often this happens in the first place) to prevent it — matching this module's existing "Python corrects what the LLM sometimes gets wrong" precedent for correctness issues (quantity clamping, price grounding) rather than a style preference.
- **Bounded conversation history**: `ui/components/chat_assistant.py::render_concierge_widget` caps the `conversation_history` list sent to the LLM to the last `_MAX_HISTORY_MESSAGES = 4` entries (never the full session's chat log) — bounds token cost on a long-running conversation without affecting what's actually displayed (`st.session_state["concierge_messages"]` itself is never trimmed). The budget-guardrail/save-publish multi-turn flows only ever need the model's own immediately-preceding turn, always well within this window regardless of total conversation length.
- **Unexpected (non-anticipated) exceptions get their full traceback logged to stderr** before falling back to the same heuristic, in addition to (not instead of) the already-informative one-line messages the anticipated failure modes (bad HTTP status, timeout, etc.) already print — so a genuine bug is never silently invisible to whoever's running the app, without ever crashing the user's chat session.
- **No standalone `$` in prices**: same rule and reasoning as §6.6 (Streamlit KaTeX/math-mode on a matching `$...$` pair) — the system prompt instructs "600 USD"/"USD 600", never a bare "$600".
- **Heuristic fallback** (network-free, on any failure — missing API key/model config, timeout, connection error, non-2xx/429 response, schema-validation failure, or a hallucinated `load_build` action): `{"reply": "I'm having trouble reaching the AI assistant right now. Try browsing the catalog or community pages directly, or ask again in a moment.", "action": null, "source": "heuristic"}`. Unlike `client.py`/`advisory.py`, there is no deterministic non-LLM way to "answer a hardware question" — the honest heuristic here is admitting degraded service, not faking a fake answer.
- **No persistent cache**: like `advisory.py`, a successful concierge response is not written to `llm_cache` (its schema/cache-key scheme is shaped for `analyze_build`'s request/response, and a free-form chat message has no stable cache key the way a build-state fingerprint does).

---

## 7. Streamlit UI State Machine

### 7.1 Session state keys

| Key | Type | Purpose |
|---|---|---|
| `page` | str | `landing` \| `create_build` \| `my_builds` \| `community` \| `drafts`. Every real transition into this key goes through `ui.state.navigate_to_page(target_page)` rather than a raw assignment — its one added behavior is clearing `selected_post_id` whenever `target_page == "community"`, so a stale open thread never re-appears after navigating away and back to the feed. |
| `auth_user` | dict \| None | sanitized session payload (id, username, email, full_name) — see `auth.service.to_session_payload` |
| `auth_mode` | str \| None | `login` \| `register` \| None — controls inline modal visibility |
| `auth_error` | dict | field-level validation errors for red-highlighting (§7.3) |
| `create_mode` | str \| None | `Budget` \| `Workload` \| `Free` \| None |
| `build_draft` | dict | `{"name", "creation_mode", "workload_profile", "tier", "budget_ceiling", "components": {category: component_id}, "quantities": {category: count}}` — see `ui/state.py::new_build_draft`. `quantities` defaults to `{}` (meaning quantity 1 everywhere); only `"RAM"`/`"Storage"` ever get a UI control to raise it, via `ui/state.py::get_quantity`/`set_quantity` (`set_quantity` clamps to a minimum of 1 and, like `set_component`, invalidates `build_draft_analysis`). `remove_component` also clears that category's stored quantity, so a re-filled slot starts back at 1x rather than inheriting a stale multiplier. |
| `build_draft_analysis` | dict \| None | last `BuildAnalysisResponse.model_dump()` for the current draft (`["source"]` is `"llm"` or `"heuristic"`) |
| `advisory_cache` | dict | `{(mode, workload_profile, sorted (category, component_id) pairs, round(current_budget_or_cost, 2)): get_build_advisory(...) result}` — §6.6/§7.4; a plain in-session dict, not persisted to `llm_cache`. `workload_profile` is in the key so switching profile with an otherwise-identical build/mode/cost gets its own entry instead of reusing stale advice. |
| `stretch_applied_keys` | set | Cache keys (same tuple shape as `advisory_cache`'s keys) for which the "🚀 Apply Stretch Upgrade (One-Time)" button (§7.4 step 6) has already been used — a per-build lock, not a single global flag, so a *different* build can still use its own one-time stretch upgrade. |
| `sort_criteria` | str | `Cost` \| `Compatibility` \| `ValueIndex` — for part-picker lists; no user-facing control sets this anymore (the Build Studio sort control was replaced by "Reset All Fields", §7.4 step 6), so it stays at its `Cost` default in practice |
| `previous_builds_filter` | dict | `{workload_profile, sort}` for the dashboard |
| `selected_post_id` | int \| None | community thread currently open |
| `fork_source_build_id` | int \| None | set when "Fork/Customize" seeds a new `create_build` draft |
| `concierge_messages` | list | Sidebar AI Concierge widget (`ui/components/chat_assistant.py`, backed by §6.7) chat history: `[{"role": "user" \| "assistant", "content": str}, ...]` |
| `has_unsaved_build_changes` | bool | §7.9 — `True` once the active `build_draft` has any pick made via `ui.state.set_component`/`remove_component`/`set_quantity` since the last successful "Save build" (§7.4 step 9) or a resolved unsaved-changes dialog. Gates the sidebar navigation interception; never set directly outside those three mutators and the two flows that clear it. |
| `concierge_last_saved_build` | dict \| None | §6.7 intent 7/§7.8 — `{"build_id": int, "name": str}` for the most recent Concierge `save_build` action applied THIS session with `destination == "build"`, or `None`. A `destination == "draft"` save never sets this key (a draft has no publish path). The one piece of real state the LLM cannot know on its own; a later `publish_build` action (which carries no build id itself) resolves which build to publish through this key. Owned by `ui/components/chat_assistant.py`; a distinctly-named key, deliberately not a reuse of the retired `concierge_pending_action` key from an unrelated, now-removed confirm-before-apply mechanism. |
| `pending_community_filters` | dict \| None | §6.7 intent 5/§7.6/§7.8 — a one-shot staging payload (`{"build_type", "max_price", "domain", "tier"}`) set by `ui/components/chat_assistant.py::_apply_concierge_action` when a Concierge `navigate` action targeting `"community"` carries a `filters` payload. Not in `_DEFAULTS` — it only exists once a Concierge navigation-with-filters action has actually fired. Consumed (popped, never merely read) by `ui/views/community.py::_apply_pending_community_filters` on that page's very next render, which translates it into the REAL `community_mode_filter`/`community_price_filter`/`community_domain_filter`/`community_tier_filter` widget keys using live feed data — a deliberately DIFFERENT key from all four of those, since `chat_assistant.py` cannot resolve a requested filter into one of their real option strings itself (only `community.py`, at render time, has the live price-step/domain/tier data needed to do that safely). |

`ui/state.py` owns this table: it defines the defaults, `init_session_state()` (called once from `app.py`), `navigate_to_page(target_page)` (the single source of truth for a plain page transition), `teardown_builder()` (the single source of truth for leaving Build Studio — a pure session-state reset, no database write, §7.9), and the bridge functions between `build_draft` (plain, session-state-safe dict of category -> component **id**) and a `BuildState` (category -> full `Component` row) that `engine/`/`llm/` actually operate on — `resolve_build_state(build_draft)`, `set_component(build_draft, category, component)`, `remove_component(build_draft, category)`.

### 7.2 Page routing (`ui/router.py`)
A single dispatcher reads `st.session_state["page"]` and calls the matching `ui/views/*.py::render()` function. No native Streamlit multipage file-based routing is used, because the landing page must gate navigation behind `auth_user` (unauthenticated users can only ever be shown `landing`, regardless of what they click).

```
if not st.session_state.auth_user and st.session_state.page != "landing":
    st.session_state.page = "landing"   # hard gate, enforced every render
```

`ui/router.py` itself has no Concierge-specific logic — a `load_build`/`modify_build`/`navigate`/`open_community_build` action is applied immediately, inline, by the widget itself (`ui/components/chat_assistant.py::_apply_concierge_action`, §7.8) the instant a chat turn produces one, with no confirmation click and no deferred router-level consumption step. This mutates only the ephemeral, uncommitted `build_draft` — nothing is written to the database until the user later explicitly clicks "Save build" in the normal `create_build.py` flow — so the action is fully visible and re-editable the instant it lands. (An earlier design routed this through a two-signal confirm-first flow — `concierge_pending_action`/`concierge_confirmed_action`, consumed by a dedicated `router.py` function — that has since been retired in favor of this simpler, immediate-apply flow; neither key nor that function exist anymore.) Once on `create_build`, a concierge-loaded or -modified draft gets the exact same automatic compatibility/synergy/bottleneck treatment (§7.4) as any hand-built Free-mode draft — that flow reads generically from `build_draft`/`build_state` regardless of how the components got pinned.

### 7.3 Auth modal flow (`ui/components/auth_modal.py`)
- Rendered inline on the landing page (no navigation) when `auth_mode` is set.
- Register form: live per-keystroke duplicate flags via `auth.service.is_username_taken`/`is_email_taken` (Streamlit reruns the script on every widget change, so this is genuinely live, not just on-submit) rendered as a red tag under the field; on submit, `auth.service.register(...)` re-validates everything (including uniqueness) and raises `ValidationError` with the same field->message shape on failure — no page reload, same Streamlit rerun.
- Login form: single identifier input (username OR email) + password; `auth.service.authenticate(identifier, password)` tries username match first, then email.
- On success: `auth.session.log_in(user)` sets `auth_user` (via `to_session_payload` — never the password hash) and resets `auth_mode` to `None`. Login/register buttons are replaced by a sidebar (`app.py`): user badge, then the four page-nav buttons (Create New PC / Previous Builds / View Drafts / Community — needed once the user is past `landing`, since that's the only view with its own nav buttons), then a `Logout` button pushed toward the bottom via a CSS flex spacer. Clicking a nav button while `has_unsaved_build_changes` is `True` and the current page is `create_build` does not navigate immediately — see §7.9.

### 7.4 Create Build flow (`ui/views/create_build.py`)
1. Mode selector (Budget / Workload / Free) if `create_mode` is `None`.
2. **Budget mode**: number input for ceiling + a single "Apply budget & generate build" button (commits the ceiling and runs `engine.solvers.initialize_budget_build()` in one click — see the "Ceiling entry and build generation are one unified, commit-on-click action" key property below) → part-picker grid, one row per category, each showing the current pin and an expander with the filtered, sorted candidate list.
3. **Workload mode**: profile selector + tier selector → `engine.solvers.allocate_workload_baseline()` → same part-picker grid.
4. **Free mode**: empty `build_draft`, all categories open immediately, part-picker shows the full catalog filtered only by compatibility against whatever is already pinned (`engine.solvers.get_compatible_candidates`).
5. A summary header (`_summary_header` in `create_build.py`) sits above the part-picker grid and stays current on every pick, rendered as four clean numeric metric cards — Total Cost, Compatibility, Synergy, Bottleneck — with no badge row and no button beneath them (an earlier revision had a "Compatible"/"N issue(s)" badge, an "AI Engine"/"Heuristic Baseline" source badge, and a manual `"🔮 Analyze"` button here; all three were deliberately removed for a cleaner banner — a conscious trade-off, not an oversight: there is no longer any visual signal for whether Synergy/Bottleneck came from a real LLM call or the heuristic fallback, and no way to manually retry a heuristic result without changing a component). The underlying analysis is unaffected by that removal — it's still a call to `llm.client.analyze_build(...)`, **auto-triggered** (`_maybe_auto_analyze`, called in `render()` right before `_summary_header`) the instant a build first becomes complete — all 8 core categories filled, in ANY mode: Budget's "Apply budget & generate build", Workload's "Generate baseline build", or finishing the last manual pick in Free mode — so real numbers show on the very first render, no click required or possible. Since `analyze_build` never raises, the auto-trigger always succeeds; only for an incomplete build (nothing to auto-analyze yet) do the Synergy/Bottleneck cards show a cheap local heuristic (`scoring.live_bottleneck_and_synergy`) instead, or "—" if fewer than 2 components are picked. Any component change invalidates the last analysis (`ui/state.py::set_component`/`remove_component` clear `build_draft_analysis`) — for a build that stays complete after the change (e.g. swapping one CPU for another), the auto-trigger immediately recomputes a fresh result on that same rerun, so the cards never show stale numbers for a build that's since changed. There is no separate lower analysis section — the numeric synergy/bottleneck figures live only in this banner, and the LLM response's qualitative text (summary/synergies/conflicts/upgrade path) is not displayed anywhere in the UI, only its `synergy_score`/`bottleneck_percentage` are persisted on save. Compatibility issues (`report.issues`) still render as a list of danger-tagged lines below the metric cards when present — only the top-line "Compatible"/"N issue(s)" badge itself was removed, not the detailed issue messages. True CSS `position: sticky` for this header was attempted and found not to engage under Streamlit's flex/overflow DOM layout (verified live) — it's a prominent card at the top of the page, not a scroll-pinned one.
6. Directly below the summary header, a full-width `"✨ Get AI Analysis & Upgrade Path"` button (`_advisory_controls`, disabled until at least 2 components are picked — same gating value the old manual synergy/bottleneck "Analyze" button used) calls `llm.advisory.get_build_advisory` (§6.6) on click, across all three modes. Never auto-triggered (unlike `analyze_build`) — it's an on-demand deeper read, not part of what a complete build inherently shows. Result is cached in `st.session_state["advisory_cache"]` (§7.1) keyed by `(mode, workload_profile, sorted (category, component_id) pairs, round(current_budget_or_cost, 2))`, recomputed fresh from the CURRENT build on every render (not a remembered "last clicked" slot) — so a cache hit displays instantly with no re-click, and the moment the build/mode/cost/profile actually changes, the new key simply has no entry yet and the stale result silently stops showing, rather than lingering for a build it no longer describes. The call also passes `bottleneck_info` straight from `build_draft_analysis["bottleneck"]` when a fresh one already exists (§7.1), instead of making `get_build_advisory` recompute it internally. When a cached result exists, it renders in `st.expander("💡 AI Build Advisory & Recommendations", expanded=True)`: a `st.caption` naming the source ("AI-generated" vs "local heuristic estimate"), then a 2-column `st.columns(2)` Pros & Cons section — `:green[✔ Pros & Strengths]` bullets from `pros` on the left, `:red[✖ Cons & Limitations]` bullets from `cons` on the right, each line passed through `ui.format.sanitize_markdown` — followed by an `st.divider()`, then two `st.tabs` — "⚖️ In-Budget Optimization & Balance" (renders the sanitized `within_budget.explanation` via `st.markdown`) and a dynamically-labeled "🚀 Stretch Budget Upgrades (+X USD)" where X is `current_budget_or_cost * 10%` rounded to the nearest 10 (renders the sanitized `stretch_budget.explanation`).

Each tab also has an **Apply** button that acts on the advisory's machine-executable `swaps` (§6.6) directly against `build_draft`, disabled when that tab's `swaps` list is empty:
- Tab 1's `"⚡ Apply In-Budget Optimization"` (`btn_apply_in_budget`) resolves each swap's `replace_with_id` via `db.repositories.components_repo.get_by_id` and pins it via `ui.state.set_component` (`create_build.py::_apply_swaps`), then — if `within_budget.can_optimize_further` is true — automatically re-queries `get_build_advisory` against the newly-swapped build and repeats, up to a hard cap of **3 rounds** (`_MAX_AUTO_OPTIMIZE_ROUNDS`) in one synchronous loop within the same click. This cap is a deliberate product/safety decision, not from the original ask: an unbounded "loop until confirmed optimized" risks either genuine non-termination (if a result never reports `can_optimize_further: False`) or an unbounded number of billed OpenRouter calls from a single click. The loop's final advisory result is cached under a freshly-computed cache key for the build as it now stands, so the expander shows a coherent result afterward rather than a stale one keyed to the pre-swap build.
- Tab 2's `"🚀 Apply Stretch Upgrade (One-Time)"` (`btn_apply_stretch`) applies `stretch_budget.actions` once via `_apply_stretch_actions` (a `"swap"` item resolves `replace_with_id` and pins it exactly like `within_budget`'s swaps; a `"set_quantity"` item calls `ui.state.set_quantity(build_draft, category, quantity)` directly — no catalog lookup needed, it's a count on the already-selected component), then locks itself for that specific `cache_key` via `st.session_state["stretch_applied_keys"]` (§7.1) — a per-build lock (clicking it again for THIS build is disabled with a caption explaining why) rather than a single session-wide flag, so a different build can still use its own one-time stretch upgrade. `stretch_applied_keys`' default (like every other dict/set-valued entry in `ui/state.py::_DEFAULTS`) is `copy.deepcopy`'d in `init_session_state()`, not assigned by reference — a real bug existed here at one point (a shared mutable default would otherwise leak one user's applied-stretch lock to every other concurrent session in the same server process) and is fixed at the source, not worked around per-callsite.
7. Right below that sits a single `"🔄 Reset All Fields"` button (`_reset_controls`) — replaces the old per-category sort control entirely. Clicking it reinitializes `build_draft` from scratch (`ui/state.py::new_build_draft`) for the current mode, clears `build_draft_analysis`, and pops the mode-specific widgets' own `st.session_state` entries (`budget_ceiling_input`, `budget_unlimited_input`, `workload_profile_input`, `workload_tier_input`) so they fall back to their clean defaults on the next render rather than keeping whatever the user last typed. There is no user-facing sort control anymore — `ui/components/part_picker.py`'s `sort_candidates` always sorts by `"Cost"` (`st.session_state["sort_criteria"]`'s unchanged default) internally.
8. Each of the 8 core slots renders as its own card (`ui/components/part_picker.py`) in a 2-column grid: a `st.badge` reading "Selected" (green) or "Empty" (gray), the current pick's name/price/key-specs if any, a `st.popover` drawer for changing it, and — next to the popover trigger, not inside it — a compact `"✕"` clear button (`on_remove`) that empties the slot in one click with no drawer to open first; it only renders once something is selected. (An older design put this as a "Remove {category}" button at the bottom of the popover drawer itself — that's been replaced by this slot-level button, so the drawer now contains only the candidate list.) Candidate cards inside the popover show price, a per-category "at a glance" spec line (socket/TDP/form-factor/dimensions, whichever apply), and a Value/Ratio Index score.

   For `"RAM"`/`"Storage"` only, a filled slot also shows a compact `st.number_input("Qty", ...)` stepper (the price caption switches to `price × qty = total` once qty > 1), whose bound comes from `ui.state.resolve_effective_quantity_limit(build_state, category, quantities, budget_ceiling)` — never a locally-duplicated calculation, and never just the physical half alone. Three caption/behavior cases, keyed by that function's `limit_kind`:
   - `"physical"`: real motherboard data bounds this category tighter than (or absent) any budget constraint — the widget's `max_value` is that real number and a caption states it plainly (`"ℹ️ Motherboard limit: up to N..."` / `"...limit reached..."` once hit).
   - `"budget"`: an active Budget-mode ceiling is the tighter constraint (holding every other category's cost fixed, one more unit of this category's current pick would exceed it) — the widget's `max_value` stops the increment before it happens (not a post-hoc warning), with `"⚠️ Budget ceiling reached for additional units."` once hit, or `"💰 Budget allows up to N..."` while headroom remains. Outside Budget mode (`budget_ceiling=None`, i.e. Workload/Free), this constraint never applies regardless of cost.
   - `"none"`: neither constraint has real data (this only arises when there's no real physical data AND no budget ceiling either) — the widget falls back to a UI-only safety cap (4) with a caption that explicitly does NOT claim it came from the motherboard.

   A stale widget value that would now exceed a newly-shrunk effective limit (a real limit can shrink from either side — swapping to a higher-modules-per-kit RAM stick, or the current build simply getting more expensive elsewhere) is clamped in `st.session_state` before the widget re-renders, since a keyed Streamlit widget ignores a fresh `value=` argument on every rerun after its first and would otherwise raise a raw range exception instead of silently re-clamping. `ui.state.set_quantity`/`remove_component` (via `_sync_qty_widget_key`) now also keep the stepper's own `qty_{category}` session-state key directly in lockstep with every quantity change, regardless of caller — the AI Concierge's `modify_build` handling and the advisory's stretch-upgrade `set_quantity` action included — closing a previously-known gap where such an out-of-band write had no effect on what the stepper actually displayed. The one wrinkle: Streamlit forbids writing to (or popping) a widget's session-state key once that widget has already been instantiated in the CURRENT script run — `_sync_qty_widget_key` silently no-ops on exactly that exception, which only ever arises from a context where the sync would have been redundant (the stepper's own on-change callback, which already set the same value itself) or premature (a later fresh render will apply it safely instead). In Budget mode with an active ceiling, `render_part_picker` receives the raw `budget_ceiling` (not a pre-subtracted number) and computes a **Minimum Reserve Threshold** itself: `Max_Slot_Cost = budget_ceiling - (cost of everything else already selected) - (cheapest compatible cost to fill every OTHER still-empty core category)`. Any candidate priced above that is disabled (not hidden) with a caption showing the exact overage — this is deliberately stricter than "can I afford this one part," since it also protects the *other* empty slots from being priced out before the user even gets to them. Reduces to the simple "ceiling minus spent" check once every other slot is already filled (reserve = 0 then).
9. "Save Build" → name field + a `"Save as draft"` checkbox (`key="save_as_draft_checkbox"`) that is mutually exclusive with publishing — the `"Also publish to Community"` checkbox (and its conditional `"Community post description"` `st.text_area`) is only rendered when `save_as_draft` is unchecked, since a draft is a lightweight `draft_builds` checkpoint (§3.9) with no publish path anywhere in this app's real architecture, the same precedent as the AI Concierge's own `save_build`/`destination: "draft"` branch (§6.7 intent 7). Clicking "💾 Save build":
   - **`save_as_draft` checked**: calls `db.repositories.drafts_repo.save_draft(user_id, name or "Untitled Draft", mode, components, quantities)`, shows a success message, and redirects to `drafts` — the single, explicit way to create a draft in this app (§7.9); leaving Build Studio any other way discards an unsaved build with no database write.
   - **`save_as_draft` unchecked** (unchanged): `db.repositories.builds_repo.create_build()` (and, if publishing, `community_repo.create_post(..., author_notes=community_description or None)` — an empty/untouched text area stores `None`, not `""`, matching a build published with no notes at all) → redirect to `my_builds` with the new build highlighted. `author_notes` is only ever surfaced in the community thread view (`ui/views/community.py::_thread_view`), not the top-level feed list (§7.6).
   - Either branch resets the builder to a clean slate (`create_mode`/`build_draft`/`build_draft_analysis` to `None`, `has_unsaved_build_changes` to `False`) before redirecting.

### 7.5 Previous Builds (`ui/views/my_builds.py`)
- Top filter bar: workload profile groupings (with nested chronological sort) OR global sort (`Cost High→Low`, `Cost Low→High`, `Date`) — mutually exclusive filter modes per intent.txt §5.
- When grouped by workload profile: a header per profile, builds beneath sorted by date descending.
- Otherwise: 3-column card grid (`ui/components/build_card.py`) showing name, total cost, bottleneck %, compatibility %, date, and quick actions (Clone, Edit, Share to Community, Delete).
- **Clone**: duplicates the build row (new `id`, `user_id` unchanged, `name` suffixed " (copy)"), lands on `create_build` in Free mode pre-filled.
- **Edit**: loads the build into `build_draft` and opens `create_build` in whatever `creation_mode` it was originally created under.
- Both Clone and Edit (`_clone_into_studio`, the same underlying function) also set `has_unsaved_build_changes = True` directly — a real, confirmed bug fix, the exact same issue and fix as `community.py::_fork_into_studio` (§7.6): populating `build_draft["components"]` as a plain dict literal bypasses `ui.state.set_component` (the usual place this flag gets set), so without this explicit write, a cloned/edited build silently bypassed §7.9's unsaved-build-changes interception on its next exit.
- **Share to Community**: opens the same publish modal as §7.4 step 7.
- **Delete**: `db.repositories.builds_repo.delete_build(build_id)` — a destructive action, so `build_card.py` requires a Yes/Cancel confirmation step before it fires (`render_build_card(..., confirm_labels={"Delete"})`); once confirmed, the build (and, via cascading FKs, its `build_components` and any `community_posts`/`community_comments`) is gone and the list refreshes immediately (`st.rerun()`).

### 7.6 Community (`ui/views/community.py`)
- Feed of `community_posts`, newest first. Card title inlines the total cost — `st.markdown(sanitize_markdown(f"### {post.title} | ${total_cost:,.2f}"))` — the whole composed string (title + appended price together) run through `ui.format.sanitize_markdown`, since `post.title` is free text the original creator typed with no character restrictions and could itself contain a `$`, which combined with the appended price's `$` would form the exact matching pair that triggers Streamlit's KaTeX/math-mode rendering. The subtitle caption no longer repeats the cost (it lives only in the title now): `"{creation_mode} build · {date}"` for Budget/Free builds; for a Workload build with both `workload_profile` and `workload_tier` set, `"Workload build · {domain} · Tier: {tier} · {date}"` where `domain` is `ui.format.humanize_profile(workload_profile)` — falls back to the plain format for any Workload build still missing `workload_tier` (e.g. a legacy row predating this column).
- **Filter toolbar**, above the feed, computed entirely from the already-fetched `community_repo.get_feed()` result (no separate query): a `"Filter by Build Type"` selectbox (`"All"`/`"Budget"`/`"Workload"`/`"Free"`) with a mode-specific cascading sub-filter —
  - `"Budget"`: a `"Max Price Limit"` selectbox, options `"All Prices"` plus a real price-step list starting at `$1,000` and stepping by `$500` up to the smallest step that covers the actual most expensive shared Budget build (e.g. costs up to $1,202 produce `$1,000`, `$1,500`) — never rendered if no Budget builds are currently shared.
  - `"Workload"`: two side-by-side selectboxes, `"Domain"` (`"All"` plus the real, `humanize_profile`'d, currently-shared workload profiles) and `"Tier"` (`"All"` plus the real currently-shared tiers, sorted in `Entry`→`Mid`→`High`→`Enthusiast` order, not alphabetically) — a legacy Workload post still missing `workload_profile`/`workload_tier` is excluded by a non-`"All"` selection on that axis but still appears when both stay `"All"`.
  - `"All"`/`"Free"`: no sub-filter.
  - An empty result after filtering shows `st.info("No community builds match the selected filters.")`, kept distinct from the separate "no builds shared yet at all" empty state.
  - **Concierge-driven pre-population**: `render()` calls `_apply_pending_community_filters(posts)` before instantiating the mode/price/domain/tier selectboxes above, which pops the one-shot `st.session_state["pending_community_filters"]` key (§7.1/§7.8) — set by a Concierge `navigate` action's `filters` payload (§6.7 intent 5) — and, if present, writes real, currently-valid values directly into `community_mode_filter`/`community_price_filter`/`community_domain_filter`/`community_tier_filter` so the widgets below pick them up as their current value on this same render. A requested `max_price` resolves to the smallest real price step that covers it (or the largest step if the request exceeds all of them, via the same `_budget_price_steps` helper the widget itself uses); a requested `domain`/`tier` matches case/whitespace-insensitively against `_workload_domains`/`_workload_tiers` (also shared helpers), falling back to `"All"`/`"All Prices"` on no match — never writing a value outside a keyed selectbox's own `options`, which would otherwise raise `StreamlitAPIException`.
- Thread view: full spec list (reuses `build_card`'s detail layout), cost breakdown, `author_notes`, and `community_comments` thread with a comment box (auth required to post). Each component line shows a `(x{N})` badge directly after the category label when `BuildComponent.quantity > 1` (only ever meaningful for RAM/Storage — every other category is implicitly 1x, no badge) — e.g. `Storage (x2): WD Black SN850X 2TB ($318.00)` — with the displayed price being the real total for that many units (`unit price × quantity`), not just one unit's price. `quantity` was already persisted and used functionally elsewhere (forking preserves it via `builds_repo.BuildComponentInput`) but was never previously surfaced in this display.
- **Fork/Customize**: sets `fork_source_build_id`, navigates to `create_build` with the source build's components pre-loaded into `build_draft` as a fresh, editable, unsaved draft. Also sets `has_unsaved_build_changes = True` directly (a real, confirmed bug fix — `_fork_into_studio` writes `build_draft["components"]` as a plain dict literal rather than replaying picks through `ui.state.set_component`, the usual place this flag gets set, so without this explicit write a forked build silently bypassed §7.9's unsaved-build-changes interception entirely).
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
- **Locked sidebar** (`inject_css()`): the sidebar's native collapse control is hidden (`[data-testid="stSidebarCollapseButton"] { display: none !important; }` — no tag qualifier, since this element is actually a `<div>`, confirmed live, despite its testid name suggesting a `<button>`) and its width is pinned to 320px. Locking the width requires overriding `width` itself, not just `min-width`/`max-width`: confirmed live that Streamlit's own draggable-resize logic writes a plain (non-`!important`) inline `width: <Npx>` on `section[data-testid="stSidebar"]`, and `min-width`/`max-width` alone — even `!important` — did not clamp the rendered box without an accompanying `!important` `width` override in the stylesheet rule beating that non-important inline value. All three properties are set together, `!important`, on that same selector.

### 7.8 AI Concierge sidebar widget (`ui/components/chat_assistant.py`)
A `st.expander("💬 AI Concierge & Site Navigator", expanded=False)` in `app.py`'s sidebar, rendered ONLY when `auth_user` is set — positioned between the 3 page-nav buttons and the CSS flex-spacer that pushes Logout to the bottom, so Logout stays pinned below it regardless of chat content length.

- **Context assembly** (this widget owns all of it — `llm/concierge.py` never queries anything itself, §6.7): `_catalog_summary()` (the full 155-component catalog), `_community_summary()` (every currently-shared post via `community_repo.get_feed()`), and `_current_build_context()` (a snapshot of `st.session_state["build_draft"]`, including its raw `budget_ceiling` — `None` when no mode has been chosen yet or it resolves to zero real components) are all rebuilt fresh on every single message send, not cached. `_advisory_context(user_input)` is the fourth: gated behind `_looks_like_analysis_request` (a cheap local keyword check, never a second LLM call just to classify intent) so a real `get_build_advisory` call only happens when the message actually looks like an optimization/analysis ask AND at least 2 components are picked; it reuses `st.session_state["advisory_cache"]` under the exact same cache-key shape as `create_build.py::_advisory_controls` (§7.1/§7.4 step 6), so it's a free cache hit whenever an equivalent advisory already exists, and a fresh (billed) call otherwise — returns `None` when the heuristic doesn't fire or there's no build to analyze, which `get_concierge_response` treats the same as an empty `{}`.
- **Message history**: `st.session_state["concierge_messages"]` (§7.1) is rendered in FULL (never sliced/dropped — an earlier round briefly tried a display-only `[-4:]` slice that made older turns permanently unreachable in the UI; reversed) inside a `st.container(height=380)` — the fixed height plus native `overflow-y: auto` scrolling is what keeps a long conversation from pushing the sidebar's nav buttons/Logout off-screen (roughly the last ~4 bubbles fit in the visible viewport at once), while scrolling up within that same container reaches every earlier turn. Each message renders via `st.chat_message`/`_render_chat_message` (a thin dispatcher, formerly a bare `st.markdown(sanitize_markdown(...))` call inlined at the call site); a new turn is sent via `st.chat_input`. This is unrelated to `_MAX_HISTORY_MESSAGES = 4`, which independently caps only what's sent to the LLM as `conversation_history` (bounding token cost) — the full, untouched `concierge_messages` list is always what's actually displayed.
- **RTL/BiDi rendering for Hebrew messages** (`_render_chat_message`, `_looks_like_hebrew`): kept in place for a Hebrew-typing user's OWN chat message even after §6.7's English-only rule — the model no longer replies in Hebrew itself, but a user is still free to type in Hebrew (they'll get an English "I only operate in English" reply back), and their own message still deserves correct RTL rendering rather than sitting oddly LTR-aligned. Streamlit's default `st.markdown` rendering shapes Hebrew characters correctly (the Unicode Bidi Algorithm handles that automatically) but does not right-align/RTL-direction the block itself, which reads as visibly wrong for Hebrew prose. `_looks_like_hebrew(text)` is a plain `֐`-`׿` Hebrew-Unicode-block codepoint check (a stdlib `re` search, no new dependency). When a message (either role — `"user"` or `"assistant"`, applied symmetrically so a Hebrew-typing user's own message doesn't sit oddly LTR-aligned next to an RTL-styled assistant reply in the same thread) contains Hebrew, `_render_chat_message` HTML-escapes its content (`html.escape`, stdlib) and wraps it in `<div style="direction: rtl; text-align: right; unicode-bidi: plaintext;">...</div>`, rendered via `st.markdown(..., unsafe_allow_html=True)`; a non-Hebrew message renders exactly as before (`st.markdown(sanitize_markdown(...))`, no wrapper, no escaping change). **Security**: the wrapped content is LLM-generated (or user-typed) text — untrusted from this module's perspective — so `unsafe_allow_html=True` is a new capability this feature introduces; escaping BEFORE interpolating into the wrapper `<div>` is what keeps it safe — only the static wrapper tags this function writes are ever live HTML, the message content itself is always inert escaped text, never a rendered tag/script, even if it happens to contain something that looks like markup. Verified empirically (a throwaway local script, not committed) that a message like `<img src=x onerror=alert(1)>` renders as literal escaped text, never a live tag, and that `sanitize_markdown`'s `$`->`USD ` KaTeX/math-mode defense is unneeded (and deliberately not applied) on this path specifically: a `$...$` pair inside a raw HTML block passed to `st.markdown(..., unsafe_allow_html=True)` is not run through Streamlit's inline markdown/math processing at all, unlike the plain non-wrapped path where the same text visibly triggers the bug `sanitize_markdown` exists to prevent.
- **Zero-click action application (`load_build`/`modify_build`/`navigate`/`open_community_build`/`publish_build`) — `save_build` is the deliberate exception**: `_apply_concierge_action` applies whichever action a turn produces IMMEDIATELY, in the same script run that received it, followed by an unconditional `st.rerun()` — no confirmation button, no deferred consumption elsewhere (an earlier design routed this through `ui/router.py` behind a confirm click; that mechanism has been fully retired, §7.2). `load_build`/`modify_build`/`navigate` are safe to apply zero-click because they only ever mutate the ephemeral, uncommitted `build_draft` — nothing reaches the database until the user separately, explicitly clicks "Save build" in the normal `create_build.py` flow (§7.4 step 9), so an AI-driven edit is always fully visible and trivially correctable via the ordinary part-picker UI the instant it lands. `open_community_build` is equally safe zero-click: it performs no database write at all, only setting `page`/`selected_post_id` to deep-link to an ALREADY-shared post's own existing thread view. `save_build` USED to fire zero-click too (on the very first "save this build" message, with an auto-generated name, always targeting the real `builds` table) — that design has been intentionally reversed (§6.7 intent 7): the model now asks for and waits on a name + destination across one or more turns BEFORE ever returning the action, so by the time `_apply_concierge_action` ever sees a `save_build` action, the "confirmation" has already happened conversationally; this module still applies it immediately with no SEPARATE Python-level confirmation step of its own.
  - `navigate`: routes through `ui.state.navigate_to_page` (§7.1), the single helper every real page-transition entry point now shares, rather than writing `st.session_state["page"]` directly — one of the 5 real page keys (landing/create_build/my_builds/community/drafts). **Builder teardown, no database write** (§7.9): when leaving `create_build` for a DIFFERENT page, this handler calls `ui.state.teardown_builder()` first — the SAME function `app.py`'s sidebar nav buttons/Logout and `ui/views/create_build.py`'s "⬅ Change mode" button call. That function unconditionally resets `create_mode`/`build_draft`/`build_draft_analysis`/`has_unsaved_build_changes` — no database write, no confirmation dialog, no flash message. **Reset mode**: a `reset_mode: true` field (only meaningful with `navigate_to == "create_build"`) mirrors the "⬅ Change mode" button via the identical `teardown_builder()` call, landing back on the mode-selector. One further optional refinement (§6.7 intent 5): a non-`null` `filters` payload is staged into `st.session_state["pending_community_filters"]` (§7.1) for `ui/views/community.py::_apply_pending_community_filters` (§7.6) to translate into real widget values on its next render — this module never writes `community_mode_filter`/etc. directly, since it has no access to the live feed data needed to resolve a request into one of their real options.
  - `open_community_build`: also calls `ui.state.teardown_builder()` first when leaving `create_build` (§7.9) — deep-linking away from the builder is still leaving it — then sets `st.session_state["page"] = "community"` and `st.session_state["selected_post_id"] = action["post_id"]`. `post_id` is already guaranteed real by `llm/concierge.py::_validate_action`'s zero-hallucination cross-check against the exact `community_summary` this widget sent it (§6.7 intent 8); `ui/views/community.py::render()` already checks `selected_post_id` at the very top of its own function (§7.6) and resolves it into `_thread_view(post)` on this same next render, so no changes were needed in that module to support this action.
  - `load_build`: always starts a fresh `"Free"`-mode `build_draft` (same shape as `community.py::_fork_into_studio`), regardless of what was previously active — a deliberate full replacement, per its own definition (§6.7 intent 3).
  - `modify_build`: patches the EXISTING `build_draft` in place (or starts a fresh Free draft only in the defensive/should-be-rare case of one arriving with no active draft to patch) — every named component id is resolved via `components_repo.get_by_id` (skipped, never crashed on, if somehow unresolvable — every id is already zero-hallucination-guarded by `llm/concierge.py` itself, so this is belt-and-suspenders); every requested quantity is re-clamped through `ui.state.resolve_effective_quantity_limit` (the SAME function the manual quantity stepper uses, §7.4 step 8) before being written, so a concierge-driven "bump storage to 2" can never exceed what the stepper itself would ever allow — a request that exceeds the real physical/budget limit is silently clamped down to that real limit, not rejected outright.
  - `save_build`/`publish_build` (§6.7 intent 7) — the two exceptions that DO write to the database, once the action actually fires: judged safe specifically because each is the user's own explicit, direct request acting on their own account's own data, the same trust boundary as them clicking "Save build"/"Share to Community" themselves. `save_build` is destination-aware, branching on `action["destination"]` (a real, required field on the action, per §6.7's schema note — always the user's own literal choice, never guessed): `"draft"` calls `db.repositories.drafts_repo.save_draft(...)` (mirroring `_auto_stash_current_build_as_draft`'s component/quantity extraction shape) using `action["name"]` (the user's own literal answer — `_default_saved_build_name()`'s old `"Concierge Build – {date} {time}"` timestamp default is now only a last-resort defensive fallback for a malformed/missing name, never the primary path) and does NOT touch `concierge_last_saved_build` (nothing to publish for a draft); `"build"` calls `db.repositories.builds_repo.create_build(...)` using the exact same argument shape as `create_build.py::_save_actions` and the same user-given `name`, always privately (`is_public=False`; publishing is a separate, later action), then stores `{"build_id", "name"}` in `st.session_state["concierge_last_saved_build"]` (§7.1). Both branches reset `has_unsaved_build_changes` to `False` and deliberately leave `build_draft`/`create_mode`/`page` untouched (unlike the manual flow's redirect to `my_builds`), since forcibly navigating away mid-chat would be more disruptive than useful. `publish_build` reads the build id back out of `concierge_last_saved_build` (never from the action itself — the model cannot know it) and, if present, calls `builds_repo.set_public(build_id, True)` + `community_repo.create_post(build_id, user_id, title, author_notes)` using the saved build's own name as `title`; a `publish_build` arriving with nothing saved this session (`concierge_last_saved_build` empty — always true after a `"draft"`-destination save, since that branch never sets it) is a no-op, not a crash.
- Once `page` is set to `"create_build"` (by either `load_build` or `modify_build`), the mutated draft gets the exact same automatic compatibility/synergy/bottleneck treatment as any hand-built build — including the Optional Peripherals expander (§7.4 step 9), which reads from the same `build_draft`/`build_state` and needs no dedicated glue code to reflect a concierge-added `NetworkCard`/`SoundCard`/`OpticalDrive`.
- **Authoritative total-cost line**: `llm/concierge.py` has no `engine`/`db` access and — confirmed via a live, non-mocked reproduction — is unreliable at summing many real line-item prices into a build's aggregate cost (observed once returning a `reply` that simply echoed the user's REQUESTED budget figure back as the claimed resulting total). Because a wrong total shown to the user is a correctness problem, not a wording one, `_apply_concierge_action` returns the real total cost (via `ui.state.build_total_cost`, the same source of truth as `create_build.py`'s summary header) whenever a `load_build`/`modify_build` action lands against a non-empty `build_state`, and `render_concierge_widget` APPENDS a short, clearly-separated line (`"\n\n**Total: USD {real_total:,.2f}**"`, run through `sanitize_markdown` like the rest of the message) to that turn's assistant message — computed AFTER the action is applied, so it reflects the final, clamped build state. This deliberately APPENDS rather than edits/replaces whatever the model's own `reply` text said (string-surgery on arbitrary, possibly non-English LLM prose risks mangling grammar for no real correctness gain); §6.7's no-aggregate-totals prompt rule separately reduces how often the model states a competing number in the first place, so the two should rarely visibly disagree in practice — but the appended line is the one always guaranteed correct. No such line is appended for `navigate`/`save_build`/`publish_build` (no build total is meaningful there) or when the action resolves to an empty `build_state`.

### 7.9 Drafts page & leaving Build Studio (`ui/views/drafts.py`, `ui.state.teardown_builder`)

A build a user has started but not yet fully saved can be checkpointed as a lightweight `draft_builds` row (§3.9) two ways: the explicit "Save as draft" checkbox in `ui/views/create_build.py`'s manual Save UI (§7.4 step 9), or the user's own explicit "save this build" Concierge chat request (§6.7 intent 7/§7.8, `save_build` with `destination: "draft"`). Leaving Build Studio through any OTHER exit vector — a sidebar button, "⬅ Change mode", or a Concierge `navigate`/`open_community_build`/`reset_mode` action — performs NO database write at all: an unsaved, unchecked build is simply discarded, the same tradeoff a plain "close the tab" would have.

**This is the CURRENT of four designs this exact concern has gone through, each a deliberate, explicit product decision — not bugs, not oversights:**
1. *First*: a Concierge `navigate` action could carry an LLM-controlled `save_as_draft: bool` field, silently auto-stashing the build via `_auto_stash_current_build_as_draft()`. Removed — the model deciding whether to save was unreliable and unnecessary.
2. *Second*: an explicit `st.dialog("Unsaved Build")` confirmation modal (`ui/components/nav_guard.py`, deleted) asked the user "Save Draft or Discard?" on every exit with unsaved changes, via a stashed `pending_navigation_target` session-state key.
3. *Third*: no dialog, no user choice at all — every real exit with an unsaved, non-empty build silently auto-saved it under a computed name (`f"Computer draft | cost - USD {total_cost:,.2f} | date - {date}"`) and staged a flash banner (`st.session_state["_draft_saved_banner"]`, rendered by `app.py` as a top-of-page `st.success(...)`).
4. *Current*: design 3 REMOVED ENTIRELY (both the silent database write and the flash banner) by a later, explicit product decision — once the explicit "Save as draft" checkbox existed as a real, working, single source of truth for creating a draft, a silent background write on every exit was judged more surprising than useful, and the risk of accumulating unwanted phantom drafts outweighed the "never lose work" benefit. `_draft_saved_banner` is no longer a real session-state key anywhere in this app.

- **Tracking**: `ui/state.py::set_component`/`remove_component`/`set_quantity` each set `st.session_state["has_unsaved_build_changes"] = True` alongside their existing `_invalidate_analysis()` call — the same three call sites already exercised by every part-pick, slot-clear, and quantity change (manual or AI-Concierge/advisory-driven, since those all route through the same three functions). It is reset to `False` on a successful "Save build" (§7.4 step 9, either destination) and by `ui.state.teardown_builder()` below. This flag is tracked for its own informational value (and exercised directly by tests) but, per the current design, does not gate whether `teardown_builder()` performs a write — it never performs one.
- **`ui.state.teardown_builder()`** (§7.1) is the single function every real exit vector calls, immediately before its own transition: unconditionally resets `create_mode`/`build_draft`/`build_draft_analysis` to `None` and `has_unsaved_build_changes` to `False` — so `ui/views/create_build.py`'s own `create_mode is None or build_draft is None` gate (§7.4 step 1) shows the mode-selection screen the next time Build Studio is entered, never a leftover build. Does NOT itself change `page` or log out — every caller does that immediately afterward, since what "leaving" means differs per entry point. Called by: `app.py`'s sidebar nav buttons (when `page == "create_build"` and the target differs) and Logout button (when `page == "create_build"`); `ui/views/create_build.py`'s "⬅ Change mode" button (unconditionally); and `ui/components/chat_assistant.py`'s Concierge `navigate` (leaving `create_build` for a different page, or with `reset_mode: true` targeting `create_build`) and `open_community_build` (deep-linking away from the builder) handlers.
- **Drafts page** (`ui/views/drafts.py`, page key `"drafts"`, gated like the other three pages): lists the current user's `draft_builds` (`drafts_repo.get_user_drafts`, newest-`updated_at`-first) as plain `st.container(border=True)` cards — name, mode, a `f"{len(components)} part(s) selected"` count (deliberately not framed as "of 8 core parts," since a draft's `components_json` may include optional peripheral categories too), and the last-saved timestamp. Two actions per card:
  - **Load into Builder**: starts a fresh `state.new_build_draft(draft.mode)`, then replays the draft's stored `components`/`quantities` through `state.set_component`/`set_quantity` (resolving each component id via `components_repo.get_by_id`, silently skipping one that's since left the catalog — same defensive precedent as `ui.state.resolve_build_state`) — the same shape `community.py::_fork_into_studio` uses for forking a community build — then sets `page = "create_build"`.
  - **Delete**: `drafts_repo.delete_draft(draft_id)`, gated behind the same local Yes/Cancel confirm-badge pattern `ui/components/build_card.py` established for its own `confirm_labels` mechanism (adapted locally here since a draft card isn't a `render_build_card` call).

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
