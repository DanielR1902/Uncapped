"""Demo/mock data seeder — realistic users, builds, and community activity for
testing and demonstration. Separate from db/seed.py (catalog) so catalog
seeding and demo-data seeding are independent, idempotent operations.

Run directly: `python -m db.seed_demo` (seeds the catalog first if it isn't
already, then the demo users/builds/community content, then prints login
credentials). Idempotent — skips if the demo usernames already exist, unless
force=True.

Scores are computed directly via engine/compatibility.py + engine/scoring.py,
not through llm/client.py — mock data has no reason to depend on network
availability or make live OpenRouter calls; heuristic_synergy_score() is the
exact same formula llm/client.py's fallback uses, just called directly here.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete

from auth import service as auth_service
from db.database import get_session, init_db
from db.models import User
from db.repositories import builds_repo, community_repo, users_repo
from db.seed import run_seed as run_catalog_seed
from engine import scoring, solvers
from engine.compatibility import BuildState, evaluate_build

DEMO_PASSWORD = "Password123!"

DEMO_USERS = [
    {"username": "tech_enthusiast", "email": "tech.enthusiast@uncapped.dev", "full_name": "Priya Shah"},
    {"username": "budget_gamer", "email": "budget.gamer@uncapped.dev", "full_name": "Marcus Reed"},
    {"username": "ai_researcher", "email": "ai.researcher@uncapped.dev", "full_name": "Dr. Elena Vasquez"},
    {"username": "cad_pro", "email": "cad.pro@uncapped.dev", "full_name": "Omar Haddad"},
    {"username": "silent_builder", "email": "silent.builder@uncapped.dev", "full_name": "Nina Kowalski"},
]
DEMO_USERNAMES = [u["username"] for u in DEMO_USERS]

# Standing test/admin login — separate from the persona demo dataset above
# (own password, no generated builds), seeded/checked independently so it's
# never affected by force-reseeding the personas.
ADMIN_USER = {"username": "admin", "email": "admin@gmail.com", "full_name": "admin admin"}
ADMIN_PASSWORD = "admin123"


@dataclass
class BuildPlan:
    username: str
    name: str
    creation_mode: str  # "Workload" | "Budget"
    workload_profile: str | None
    tier: str | None
    budget_ceiling: float | None
    publish: bool
    post_title: str | None = None
    post_notes: str | None = None


BUILD_PLANS = [
    BuildPlan(
        username="tech_enthusiast",
        name="1440p High-Refresh Whiteout Build",
        creation_mode="Workload",
        workload_profile="Gaming",
        tier="High",
        budget_ceiling=None,
        publish=True,
        post_title="1440p High-Refresh Whiteout Build",
        post_notes=(
            "Went all-white for this one — high refresh rate was the priority, so I leaned "
            "into a strong GPU/CPU pairing and made sure the cooler had enough headroom to "
            "keep boost clocks steady during long sessions. Compatibility check came back "
            "clean across the board."
        ),
    ),
    BuildPlan(
        username="tech_enthusiast",
        name="4K Timeline Editing Beast",
        creation_mode="Workload",
        workload_profile="VideoEditing",
        tier="Enthusiast",
        budget_ceiling=None,
        publish=False,
    ),
    BuildPlan(
        username="budget_gamer",
        name="Budget 1080p Esports Ripper",
        creation_mode="Budget",
        workload_profile=None,
        tier=None,
        budget_ceiling=700.0,
        publish=True,
        post_title="Budget 1080p Esports Ripper",
        post_notes=(
            "Proof you don't need to spend a fortune to hit high frame rates at 1080p in "
            "competitive titles. Kept the ceiling at $700 and let the budget solver converge "
            "upward from there — no compatibility surprises, and there's still a little "
            "headroom left for a GPU upgrade down the line."
        ),
    ),
    BuildPlan(
        username="ai_researcher",
        name="Deep Learning Local Rig",
        creation_mode="Workload",
        workload_profile="Programming",
        tier="Enthusiast",
        budget_ceiling=None,
        publish=True,
        post_title="Deep Learning Local Rig",
        post_notes=(
            "Built this for local model training and inference experiments — prioritized "
            "VRAM and multi-core throughput over raw gaming benchmarks. Runs fine-tuning "
            "jobs overnight without breaking a sweat."
        ),
    ),
    BuildPlan(
        username="cad_pro",
        name="CAD & 3D Rendering Workstation",
        creation_mode="Workload",
        workload_profile="Design",
        tier="High",
        budget_ceiling=None,
        publish=True,
        post_title="CAD Workstation: Renders Without the Wait",
        post_notes=(
            "Assembly files with a few thousand parts used to bring my old machine to its "
            "knees. This one chews through them and still has enough GPU muscle for "
            "real-time viewport rendering."
        ),
    ),
    BuildPlan(
        username="silent_builder",
        name="Whisper-Quiet Home Office Build",
        creation_mode="Workload",
        workload_profile="General",
        tier="Mid",
        budget_ceiling=None,
        publish=False,
    ),
]

# (post_title, commenter_username, content) — commenters are always someone
# other than the post's author, simulating real community back-and-forth.
COMMENT_PLANS = [
    (
        "1440p High-Refresh Whiteout Build",
        "budget_gamer",
        "Wow, this is gorgeous. How are your temps under load? Thinking about doing a white build myself.",
    ),
    (
        "1440p High-Refresh Whiteout Build",
        "ai_researcher",
        "Nice GPU pick. Did you consider a higher core-count CPU for streaming/encoding headroom, or is this purely gaming-focused?",
    ),
    (
        "1440p High-Refresh Whiteout Build",
        "silent_builder",
        "That cooler looks like it could get loud under sustained load — did you check noise levels at all?",
    ),
    (
        "Budget 1080p Esports Ripper",
        "tech_enthusiast",
        "Solid budget pick! Would bumping the PSU by ~100W give you headroom for a GPU upgrade later?",
    ),
    (
        "Budget 1080p Esports Ripper",
        "cad_pro",
        "What's the total spend here? Looks like great value for 1080p esports titles.",
    ),
    (
        "Budget 1080p Esports Ripper",
        "silent_builder",
        "Does the case have decent airflow? Curious how the stock fans hold up under load.",
    ),
    (
        "Deep Learning Local Rig",
        "tech_enthusiast",
        "How much VRAM are you working with? Wondering if it's enough for the larger local models.",
    ),
    (
        "Deep Learning Local Rig",
        "cad_pro",
        "This looks close to what I'd want for GPU-accelerated rendering too. Any bottleneck concerns at that tier?",
    ),
    (
        "Deep Learning Local Rig",
        "budget_gamer",
        "Overkill for gaming but I bet this thing rips through training jobs.",
    ),
    (
        "CAD Workstation: Renders Without the Wait",
        "ai_researcher",
        "How's the RAM capacity holding up with large assemblies open? Considering something similar for simulation work.",
    ),
    (
        "CAD Workstation: Renders Without the Wait",
        "tech_enthusiast",
        "Did you consider ECC memory for this, or is it not necessary for your workload?",
    ),
    (
        "CAD Workstation: Renders Without the Wait",
        "budget_gamer",
        "Beautiful build. What's the price tag on something like this?",
    ),
]


def _delete_existing_demo_data() -> None:
    """DB-level ON DELETE CASCADE (SQLite FK pragma is enabled in
    db/database.py) takes care of each demo user's builds, build_components,
    community_posts, and community_comments — deleting the User rows is
    enough to cleanly wipe everything they own, including comments they left
    on other demo users' posts."""
    session = get_session()
    try:
        session.execute(delete(User).where(User.username.in_(DEMO_USERNAMES)))
        session.commit()
    finally:
        session.close()


def _generate_build_state(plan: BuildPlan) -> BuildState:
    if plan.creation_mode == "Budget":
        return solvers.initialize_budget_build(plan.budget_ceiling)
    return solvers.allocate_workload_baseline(plan.workload_profile, target_tier=plan.tier)


def _create_build(plan: BuildPlan, user_id: int):
    build_state = _generate_build_state(plan)
    report = evaluate_build(build_state)
    if not report.is_compatible:
        print(f"  WARNING: '{plan.name}' has compatibility issues: {report.issues}")

    total_cost = sum(component.price_usd for component in build_state.values())
    bottleneck_pct, _direction = scoring.bottleneck_percentage_baseline(build_state)
    synergy_score = scoring.heuristic_synergy_score(report.compatibility_score, bottleneck_pct)

    return builds_repo.create_build(
        user_id=user_id,
        name=plan.name,
        creation_mode=plan.creation_mode,
        components=[builds_repo.BuildComponentInput(component_id=c.id) for c in build_state.values()],
        total_cost=total_cost,
        compatibility_score=report.compatibility_score,
        workload_profile=plan.workload_profile,
        budget_ceiling=plan.budget_ceiling,
        synergy_score=synergy_score,
        bottleneck_percentage=bottleneck_pct,
        is_public=plan.publish,
    )


def _ensure_admin_user() -> int:
    """Standing admin/test login. Independent idempotency check from the demo
    personas below — "if already present, do not duplicate" — so it's
    unaffected by force-reseeding the persona dataset."""
    existing = users_repo.get_by_username(ADMIN_USER["username"])
    if existing is not None:
        return existing.id
    user = auth_service.register(
        username=ADMIN_USER["username"],
        password=ADMIN_PASSWORD,
        email=ADMIN_USER["email"],
        full_name=ADMIN_USER["full_name"],
    )
    return user.id


def run_demo_seed(force: bool = False) -> dict:
    init_db()
    run_catalog_seed()  # idempotent; ensures components exist for builds to reference
    _ensure_admin_user()

    already_seeded = users_repo.get_by_username(DEMO_USERNAMES[0]) is not None
    if already_seeded and not force:
        return {
            "skipped": True,
            "users": len(DEMO_USERNAMES),
            "builds": 0,
            "posts": 0,
            "comments": 0,
        }

    if force:
        _delete_existing_demo_data()

    user_ids: dict[str, int] = {}
    for entry in DEMO_USERS:
        user = auth_service.register(
            username=entry["username"],
            password=DEMO_PASSWORD,
            email=entry["email"],
            full_name=entry["full_name"],
        )
        user_ids[entry["username"]] = user.id

    posts_by_title: dict[str, object] = {}
    build_count = 0
    for plan in BUILD_PLANS:
        build = _create_build(plan, user_ids[plan.username])
        build_count += 1
        if plan.publish and plan.post_title:
            post = community_repo.create_post(build.id, user_ids[plan.username], plan.post_title, plan.post_notes)
            posts_by_title[plan.post_title] = post

    comment_count = 0
    for post_title, commenter_username, content in COMMENT_PLANS:
        post = posts_by_title[post_title]
        community_repo.add_comment(post.id, user_ids[commenter_username], content)
        comment_count += 1

    return {
        "skipped": False,
        "users": len(user_ids),
        "builds": build_count,
        "posts": len(posts_by_title),
        "comments": comment_count,
    }


def _print_credentials() -> None:
    print("\nAdmin login:")
    print(f"  username: {ADMIN_USER['username']:<16} email: {ADMIN_USER['email']:<28} password: {ADMIN_PASSWORD}")

    print("\nDemo login credentials (all share the same password):")
    print(f"  password: {DEMO_PASSWORD}")
    for entry in DEMO_USERS:
        print(f"  username: {entry['username']:<16} email: {entry['email']}")


if __name__ == "__main__":
    summary = run_demo_seed()
    if summary["skipped"]:
        print("Demo data already present — skipped. Pass force=True to run_demo_seed() to reseed.")
    else:
        print(
            f"Seeded {summary['users']} users, {summary['builds']} builds, "
            f"{summary['posts']} community posts, {summary['comments']} comments."
        )
    _print_credentials()
