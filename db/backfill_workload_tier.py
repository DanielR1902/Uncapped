"""One-time, idempotent backfill: db/models.py's Build.workload_tier column
was added after db/seed_admin_builds.py and db/seed_demo.py had already
persisted their Workload-mode builds, so those existing rows have
workload_tier=NULL even though the real tier each was generated with is
still known from those seed files' own BuildPlan.tier values (read here as
reference data only — this script never re-runs the seeders or touches
their BUILD_PLANS/generation logic).

Safe to run multiple times: only updates rows where workload_tier IS NULL,
so it never overwrites a real value. Goes entirely through the
db.repositories layer (never raw SQL), per db/CLAUDE.md.
"""
from __future__ import annotations

from db.database import init_db
from db.repositories import builds_repo, users_repo

# (username, build name) -> real tier, taken directly from each seed file's
# own BuildPlan.tier for that build (see db/seed_admin_builds.py and
# db/seed_demo.py for the source of truth).
KNOWN_TIERS: dict[tuple[str, str], str] = {
    # db/seed_admin_builds.py (owned by the standing "admin" user)
    ("admin", "1080p Entry-Level Gaming Rig"): "Entry",
    ("admin", "1440p High-Refresh Gaming Build"): "High",
    ("admin", "4K Enthusiast Gaming Powerhouse"): "Enthusiast",
    ("admin", "Mid-Tier Video Editing Timeline Rig"): "Mid",
    ("admin", "High-Tier 3D Design & Rendering Workstation"): "High",
    ("admin", "Mid-Tier Software Engineering & Data Science Rig"): "Mid",
    ("admin", "Entry-Level Office & Productivity Build"): "Entry",
    # db/seed_demo.py (owned by their respective persona users)
    ("tech_enthusiast", "1440p High-Refresh Whiteout Build"): "High",
    ("tech_enthusiast", "4K Timeline Editing Beast"): "Enthusiast",
    ("ai_researcher", "Deep Learning Local Rig"): "Enthusiast",
    ("cad_pro", "CAD & 3D Rendering Workstation"): "High",
    ("silent_builder", "Whisper-Quiet Home Office Build"): "Mid",
}


def run_backfill() -> int:
    """Returns the number of rows actually updated."""
    init_db()

    updated = 0
    usernames = {username for username, _ in KNOWN_TIERS}
    for username in usernames:
        user = users_repo.get_by_username(username)
        if user is None:
            continue  # that persona/admin user doesn't exist in this DB yet — skip, not an error
        builds = builds_repo.get_builds_for_user(user.id)
        for build in builds:
            tier = KNOWN_TIERS.get((username, build.name))
            if tier is None:
                continue
            if build.workload_tier is not None:
                continue  # already set (real value) — never overwrite
            builds_repo.set_workload_tier(build.id, tier)
            updated += 1

    return updated


if __name__ == "__main__":
    count = run_backfill()
    print(f"Backfilled workload_tier on {count} existing build row(s).")
