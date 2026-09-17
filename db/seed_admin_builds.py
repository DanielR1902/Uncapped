"""Admin-owned demo builds seeder — 12 realistic builds (Budget/Workload/Free
creation modes) attached to the standing "admin" user (seeded/checked
independently by db/seed_demo.py's `_ensure_admin_user()`), for admin-side
testing and demo purposes. Sibling to db/seed_demo.py, following its exact
conventions (dataclass build plans, idempotency check, a `run_*(force=False)
-> dict` entry point, `if __name__ == "__main__":` block) rather than
inventing a new pattern.

Run directly: `python -m db.seed_admin_builds` (seeds the catalog first if it
isn't already, resolves the existing admin user, then seeds these 12 builds).
Idempotent — skips if the admin user already has builds matching all 12 of
this script's own names, unless force=True (deletes ONLY the builds this
script itself would create, matched by name and scoped to the admin user,
before recreating them — never touches the admin user's other builds from
manual UI use, and never touches db/seed_demo.py's persona users/builds).

Never creates the admin user itself — if it's missing, that means
db/seed_demo.py was never run, and this raises rather than silently creating
a second admin-like account.

Scores are computed directly via engine/compatibility.py + engine/scoring.py,
exactly like db/seed_demo.py — no llm/client.py dependency, no network calls,
so seeding never depends on network availability.
"""
from __future__ import annotations

from dataclasses import dataclass

from db.database import init_db
from db.models import Build
from db.repositories import builds_repo, components_repo, users_repo
from db.seed import run_seed as run_catalog_seed
from engine import scoring, solvers
from engine.compatibility import BuildState, evaluate_build


@dataclass
class BuildPlan:
    name: str
    creation_mode: str  # "Budget" | "Workload" | "Free"
    workload_profile: str | None = None
    tier: str | None = None
    budget_ceiling: float | None = None
    # Slot-aware quantity overrides (e.g. {"Storage": 2}) — None/omitted means
    # every category defaults to quantity 1, exactly like db/seed_demo.py's
    # existing plans (none of which need more than 1 of anything).
    quantities: dict[str, int] | None = None
    # Free mode only: category -> real catalog component id, hand-picked and
    # verified compatible (see module docstring / PR notes for exactly which
    # real catalog rows these are and why). Budget/Workload plans leave this
    # None and go through the solvers instead.
    free_component_ids: dict[str, int] | None = None


BUILD_PLANS: list[BuildPlan] = [
    # --- Budget mode (3): solvers.initialize_budget_build(ceiling) ---
    # $632 is this catalog's true floor (engine.solvers.minimum_possible_build_cost());
    # $700 already lands a clean, fully compatible build with real headroom above
    # that floor (verified: totals $692), so no need to nudge up to $750-800.
    BuildPlan(name="Budget 1080p Starter", creation_mode="Budget", budget_ceiling=700.0),
    BuildPlan(name="Balanced 1440p Build", creation_mode="Budget", budget_ceiling=1300.0),
    BuildPlan(name="Premium 4K Powerhouse", creation_mode="Budget", budget_ceiling=2400.0),

    # --- Workload mode (7): solvers.allocate_workload_baseline(profile, target_tier=tier) ---
    # Real profile/tier combos only (db/models.py WORKLOAD_PROFILES / WORKLOAD_TIERS):
    # remapped from the original (fictional) 7-config request onto the closest real
    # analogs, preserving the original diversity intent.
    BuildPlan(
        name="1080p Entry-Level Gaming Rig",
        creation_mode="Workload",
        workload_profile="Gaming",
        tier="Entry",
    ),
    BuildPlan(
        name="1440p High-Refresh Gaming Build",
        creation_mode="Workload",
        workload_profile="Gaming",
        tier="High",
    ),
    BuildPlan(
        name="4K Enthusiast Gaming Powerhouse",
        creation_mode="Workload",
        workload_profile="Gaming",
        tier="Enthusiast",
    ),
    BuildPlan(
        name="Mid-Tier Video Editing Timeline Rig",
        creation_mode="Workload",
        workload_profile="VideoEditing",
        tier="Mid",
    ),
    BuildPlan(
        # Closest real analog to "Content Creation / 3D Rendering - Pro Tier" —
        # db/seed_demo.py already uses Design for its CAD/3D-modeling persona.
        name="High-Tier 3D Design & Rendering Workstation",
        creation_mode="Workload",
        workload_profile="Design",
        tier="High",
    ),
    BuildPlan(
        # Closest real analog to "Software Engineering & Data Science - Balanced".
        name="Mid-Tier Software Engineering & Data Science Rig",
        creation_mode="Workload",
        workload_profile="Programming",
        tier="Mid",
    ),
    BuildPlan(
        # Closest real analog to "office & productivity, low-key".
        name="Entry-Level Office & Productivity Build",
        creation_mode="Workload",
        workload_profile="General",
        tier="Entry",
    ),

    # --- Free mode (2): hand-assembled from real catalog components ---
    BuildPlan(
        name="High-Performance Water-Cooled Enthusiast Rig",
        creation_mode="Free",
        quantities={"Storage": 2},  # motherboard below has 5 real M.2 slots (resolve_quantity_limit-verified)
        free_component_ids={
            "CPU": 11,  # AMD Ryzen 9 9950X (AM5, top-tier)
            "GPU": 52,  # NVIDIA RTX 5090 (catalog's top-tier GPU)
            "Motherboard": 32,  # ASUS ROG STRIX X670E-E (AM5, DDR5, 5x M.2, ATX)
            "RAM": 74,  # G.Skill Trident Z5 RGB 64GB (2x32GB) DDR5-6000
            "Storage": 82,  # Samsung 990 Pro 2TB NVMe (quantity 2, see above)
            "Case": 120,  # Fractal Design Define 7 (ATX, 420mm max radiator)
            "PSU": 105,  # Corsair HX1200i (1200W ATX — covers the 5090's headroom)
            "Cooler": 135,  # Lian Li Galahad II 360 (AIO, 360mm radiator, AM5-compatible)
        },
    ),
    BuildPlan(
        name="Value-Focused SFF Air-Cooled Build",
        creation_mode="Free",
        free_component_ids={
            "CPU": 5,  # AMD Ryzen 5 7600 (AM5)
            "GPU": 43,  # NVIDIA RTX 4060 (mid-tier, compact 200mm card)
            "Motherboard": 41,  # ASRock B650E PG-ITX/WiFi (AM5, DDR5, ITX)
            "RAM": 68,  # Kingston FURY Beast 32GB (2x16GB) DDR5-5200
            "Storage": 78,  # WD Blue SN580 1TB NVMe
            "Case": 111,  # Cooler Master MasterBox NR200 (ITX, SFX PSU support)
            "PSU": 101,  # Corsair SF600 (SFX)
            "Cooler": 128,  # Noctua NH-L9a-AM5 (low-profile air, not AIO)
        },
    ),
]

PLAN_NAMES: list[str] = [plan.name for plan in BUILD_PLANS]


def _generate_build_state(plan: BuildPlan) -> BuildState:
    if plan.creation_mode == "Budget":
        return solvers.initialize_budget_build(plan.budget_ceiling)
    if plan.creation_mode == "Workload":
        return solvers.allocate_workload_baseline(plan.workload_profile, target_tier=plan.tier)

    # Free mode: hand-picked, verified-compatible real catalog components.
    build_state: BuildState = {}
    for category, component_id in (plan.free_component_ids or {}).items():
        component = components_repo.get_by_id(component_id)
        if component is None:
            raise ValueError(f"'{plan.name}': no component with id={component_id} (category={category})")
        build_state[category] = component
    return build_state


def _build_total_cost(build_state: BuildState, quantities: dict[str, int] | None) -> float:
    """Mirrors ui/state.py::build_total_cost's exact one-line formula. Not
    imported from ui/ (db/ must never depend on ui/, per root CLAUDE.md's
    module-boundary rule) — just replicated inline."""
    return sum(component.price_usd * (quantities or {}).get(category, 1) for category, component in build_state.items())


def _create_build(plan: BuildPlan, user_id: int) -> Build | None:
    build_state = _generate_build_state(plan)
    report = evaluate_build(build_state, plan.quantities)
    if not report.is_compatible:
        print(f"  WARNING: '{plan.name}' has compatibility issues, NOT persisting: {report.issues}")
        return None

    total_cost = _build_total_cost(build_state, plan.quantities)
    bottleneck_pct, _direction = scoring.bottleneck_percentage_baseline(build_state)
    synergy_score = scoring.heuristic_synergy_score(report.compatibility_score, bottleneck_pct)

    components = [
        builds_repo.BuildComponentInput(
            component_id=component.id,
            quantity=(plan.quantities or {}).get(category, 1),
        )
        for category, component in build_state.items()
    ]

    return builds_repo.create_build(
        user_id=user_id,
        name=plan.name,
        creation_mode=plan.creation_mode,
        components=components,
        total_cost=total_cost,
        compatibility_score=report.compatibility_score,
        workload_profile=plan.workload_profile,
        budget_ceiling=plan.budget_ceiling,
        synergy_score=synergy_score,
        bottleneck_percentage=bottleneck_pct,
        is_public=False,
    )


def _existing_admin_build_ids_by_name(user_id: int) -> dict[str, int]:
    builds = builds_repo.get_builds_for_user(user_id)
    return {build.name: build.id for build in builds if build.name in PLAN_NAMES}


def _delete_existing_admin_builds(user_id: int) -> None:
    """force=True path: deletes ONLY this script's own 12 (by name), scoped to
    the admin user — never the admin's other manually-created builds, and
    never db/seed_demo.py's persona users/builds."""
    for build_id in _existing_admin_build_ids_by_name(user_id).values():
        builds_repo.delete_build(build_id)


def run_admin_builds_seed(force: bool = False) -> dict:
    init_db()
    run_catalog_seed()  # idempotent; ensures components exist for builds to reference

    admin = users_repo.get_by_username("admin")
    if admin is None:
        raise RuntimeError(
            "admin user not found. This script only resolves the existing standing "
            "admin login seeded by db/seed_demo.py's _ensure_admin_user() — it never "
            "creates one itself. Run `python -m db.seed_demo` first, then retry."
        )

    existing = _existing_admin_build_ids_by_name(admin.id)
    if len(existing) >= len(PLAN_NAMES) and not force:
        return {"skipped": True, "builds": 0, "skipped_incompatible": []}

    if force and existing:
        _delete_existing_admin_builds(admin.id)
        existing = {}

    build_count = 0
    skipped_incompatible: list[str] = []
    for plan in BUILD_PLANS:
        if plan.name in existing:
            continue  # already present from a prior partial run and not forcing a full wipe
        build = _create_build(plan, admin.id)
        if build is None:
            skipped_incompatible.append(plan.name)
            continue
        build_count += 1

    return {
        "skipped": False,
        "builds": build_count,
        "skipped_incompatible": skipped_incompatible,
    }


if __name__ == "__main__":
    summary = run_admin_builds_seed()
    if summary["skipped"]:
        print("Admin builds already seeded — skipped. Pass force=True to run_admin_builds_seed() to reseed.")
    else:
        print(f"Seeded {summary['builds']} admin builds.")
        if summary["skipped_incompatible"]:
            print(f"  Skipped (compatibility issues, not persisted): {summary['skipped_incompatible']}")
