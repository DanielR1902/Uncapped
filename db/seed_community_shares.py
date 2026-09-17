"""Shares a deliberate, fixed subset of admin's 12 seeded builds
(db/seed_admin_builds.py) to the community feed, with realistic comments from
the existing demo persona users (db/seed_demo.py). Sibling to those two
files, following their exact conventions (idempotency check, a
`run_*(force=False) -> dict` entry point, `if __name__ == "__main__":` block)
rather than inventing a new pattern.

Run directly: `python -m db.seed_community_shares` (ensures the catalog,
persona users, and admin's 12 builds all exist first via db.seed_demo /
db.seed_admin_builds, then shares 6 of those builds and attaches comments).

Build selection is a fixed, explicit list rather than `random.sample` — a
seed script should be reproducible/idempotent-checkable, not different every
run — spanning all 3 creation modes: 1 Budget, 3 Workload (different
profiles), and both Free-mode builds (there are only 2, and they're the most
visually distinctive, so both are included).

Comments are generated from each build's REAL persisted component/score data
(CPU/GPU/cooler/etc. names, total_cost, bottleneck_percentage + direction
recomputed via engine.scoring) rather than hardcoded guesses — this also
means the comments stay accurate even for the Budget/Workload builds whose
exact solver-picked parts aren't known ahead of time.

Idempotent — for each selected build, skips creating a second community_posts
row if one already exists (checked via community_repo.get_feed(), not a raw
query), and never re-adds comments to a post that already has them, even
under force=True (so re-running never piles up duplicate comments). Never
creates the admin user or the 12 admin builds itself; raises if either
dependency is missing after attempting to seed them.
"""
from __future__ import annotations

from db.database import init_db
from db.models import Component
from db.repositories import builds_repo, community_repo, users_repo
from db.seed_admin_builds import PLAN_NAMES, run_admin_builds_seed
from db.seed_demo import run_demo_seed
from engine.scoring import bottleneck_percentage_baseline

# Fixed, deliberate spread across all 3 creation modes (see module docstring).
# Names must be exact matches from db.seed_admin_builds.PLAN_NAMES.
SHARE_BUILD_NAMES: list[str] = [
    "Balanced 1440p Build",  # Budget
    "1440p High-Refresh Gaming Build",  # Workload / Gaming
    "Mid-Tier Video Editing Timeline Rig",  # Workload / VideoEditing
    "Mid-Tier Software Engineering & Data Science Rig",  # Workload / Programming
    "High-Performance Water-Cooled Enthusiast Rig",  # Free
    "Value-Focused SFF Air-Cooled Build",  # Free
]
assert all(name in PLAN_NAMES for name in SHARE_BUILD_NAMES)

AUTHOR_NOTES: dict[str, str] = {
    "Balanced 1440p Build": (
        "Aimed for the sweet spot between price and performance -- didn't want to chase 4K, "
        "just wanted something that stays smooth at 1440p without blowing the budget. "
        "Compatibility came back clean, and there's still a bit of headroom under the ceiling."
    ),
    "1440p High-Refresh Gaming Build": (
        "Built this one purely for high-refresh 1440p gaming -- let the workload allocator do "
        "its thing for the 'High' tier and it came out really well balanced. No compatibility "
        "hiccups at all."
    ),
    "Mid-Tier Video Editing Timeline Rig": (
        "Needed something that could scrub 4K timelines without stuttering, so I leaned on the "
        "workload allocator's video-editing profile at the Mid tier. Handles multi-track edits "
        "a lot better than what I had before."
    ),
    "Mid-Tier Software Engineering & Data Science Rig": (
        "Wanted a rig that could handle compiling, running containers, and the occasional local "
        "model training without choking. The Programming profile at Mid tier landed on a nicely "
        "balanced spec."
    ),
    "High-Performance Water-Cooled Enthusiast Rig": (
        "Went all-out on this one -- top-tier CPU and GPU with a 360mm AIO to keep everything "
        "cool under sustained load. Doubled up on NVMe storage for game installs and scratch "
        "space. No regrets."
    ),
    "Value-Focused SFF Air-Cooled Build": (
        "Wanted something small enough to tuck away but still capable -- went full ITX with a "
        "quiet air cooler instead of an AIO since space was tight. Great value for what it can do."
    ),
}


def _parts(build) -> dict[str, Component]:
    return {bc.category: bc.component for bc in build.components}


def _comments_balanced_1440p(parts, cost, pct, direction) -> list[tuple[str, str]]:
    return [
        (
            "budget_gamer",
            f"Nice pairing for 1440p -- the {parts['GPU'].name} and {parts['CPU'].name} come "
            f"out to a {direction.lower()} split of only {pct:.1f}%, so you're not leaving much "
            f"performance on the table for the price.",
        ),
        (
            "tech_enthusiast",
            f"${cost:.0f} all-in for this spec is solid value. How's the {parts['PSU'].name} "
            f"handling headroom if you want to upgrade the GPU down the line?",
        ),
        (
            "silent_builder",
            f"{parts['Case'].name} with the {parts['Cooler'].name} -- quiet setup? Curious "
            f"about idle noise on that combo.",
        ),
    ]


def _comments_1440p_gaming(parts, cost, pct, direction) -> list[tuple[str, str]]:
    return [
        (
            "budget_gamer",
            f"The {parts['GPU'].name} should crush 1440p high-refresh. What's the "
            f"{parts['CPU'].name} doing for 1% lows in CPU-heavy titles?",
        ),
        (
            "ai_researcher",
            f"Bottleneck's sitting at {pct:.1f}% ({direction}) -- pretty tight pairing for a "
            f"gaming build. Nice work.",
        ),
        (
            "cad_pro",
            f"${cost:.0f} total -- how's the thermal headroom on the {parts['Cooler'].name} "
            f"under sustained load?",
        ),
    ]


def _comments_video_editing(parts, cost, pct, direction) -> list[tuple[str, str]]:
    return [
        (
            "tech_enthusiast",
            f"For timeline scrubbing, {parts['RAM'].name} should help a ton with caching. "
            f"How's export time on 4K footage?",
        ),
        (
            "ai_researcher",
            f"{parts['CPU'].name} paired with {parts['GPU'].name} comes out {direction} at "
            f"{pct:.1f}% -- reasonable for editing workloads where raw core count matters more "
            f"than GPU headroom.",
        ),
        (
            "silent_builder",
            f"What's the {parts['Storage'].name} looking like for scratch-disk speed? NVMe, I hope.",
        ),
    ]


def _comments_swe_ds(parts, cost, pct, direction) -> list[tuple[str, str]]:
    return [
        (
            "ai_researcher",
            f"Solid core count on the {parts['CPU'].name} for compiling and parallel workloads. "
            f"How much headroom does the {parts['RAM'].name} give you for containers/VMs?",
        ),
        (
            "tech_enthusiast",
            f"${cost:.0f} is reasonable for a dev rig. Any issues running a few VMs at once "
            f"alongside a build job?",
        ),
        (
            "budget_gamer",
            f"Would this double as a decent gaming machine with the {parts['GPU'].name}, or is "
            f"it purely for dev work?",
        ),
    ]


def _comments_watercooled_enthusiast(parts, cost, pct, direction) -> list[tuple[str, str]]:
    return [
        (
            "tech_enthusiast",
            f"That {parts['Cooler'].name} should keep the {parts['CPU'].name} nice and cool "
            f"next to that {parts['GPU'].name} monster -- bottleneck's basically nonexistent "
            f"at {pct:.1f}%, great synergy.",
        ),
        (
            "budget_gamer",
            f"Running two {parts['Storage'].name} drives is a nice touch for game installs plus "
            f"scratch space. What's the damage on a build like this -- ${cost:.0f}?",
        ),
        (
            "cad_pro",
            f"{parts['PSU'].name} makes sense for headroom with that {parts['GPU'].name} -- "
            f"should hold up fine even under transient spikes.",
        ),
    ]


def _comments_sff_air_cooled(parts, cost, pct, direction) -> list[tuple[str, str]]:
    return [
        (
            "silent_builder",
            f"{parts['Cooler'].name} in a case that small -- nice choice for a quiet SFF build. "
            f"Any clearance issues with the {parts['Case'].name}?",
        ),
        (
            "ai_researcher",
            f"{parts['GPU'].name} is a smart pick for a compact build like this -- good "
            f"performance-per-mm. ${cost:.0f} total is great value.",
        ),
        (
            "tech_enthusiast",
            f"{parts['PSU'].name} is the right call for that case. Bottleneck's {pct:.1f}% "
            f"({direction}) -- nicely balanced for the price point.",
        ),
    ]


_COMMENT_GENERATORS = {
    "Balanced 1440p Build": _comments_balanced_1440p,
    "1440p High-Refresh Gaming Build": _comments_1440p_gaming,
    "Mid-Tier Video Editing Timeline Rig": _comments_video_editing,
    "Mid-Tier Software Engineering & Data Science Rig": _comments_swe_ds,
    "High-Performance Water-Cooled Enthusiast Rig": _comments_watercooled_enthusiast,
    "Value-Focused SFF Air-Cooled Build": _comments_sff_air_cooled,
}


def _comments_for(name: str, build) -> list[tuple[str, str]]:
    parts = _parts(build)
    pct, direction = bottleneck_percentage_baseline(parts)
    return _COMMENT_GENERATORS[name](parts, build.total_cost, pct, direction)


def run_community_shares_seed(force: bool = False) -> dict:
    init_db()
    run_demo_seed()  # idempotent; ensures persona users + admin exist
    run_admin_builds_seed()  # idempotent; ensures admin's 12 builds exist

    admin = users_repo.get_by_username("admin")
    if admin is None:
        raise RuntimeError(
            "admin user not found even after run_demo_seed(). Something is wrong with the "
            "demo-seed dependency chain."
        )

    admin_builds = builds_repo.get_builds_for_user(admin.id, builds_repo.BuildsFilter())
    builds_by_name = {b.name: b for b in admin_builds if b.name in SHARE_BUILD_NAMES}
    missing = [name for name in SHARE_BUILD_NAMES if name not in builds_by_name]
    if missing:
        raise RuntimeError(
            f"Expected admin builds not found even after run_admin_builds_seed(): {missing}"
        )

    existing_posts_by_build_id = {post.build_id: post for post in community_repo.get_feed()}

    posts_created = 0
    comments_created = 0
    skipped_builds: list[str] = []

    for name in SHARE_BUILD_NAMES:
        build = builds_by_name[name]
        existing_post = existing_posts_by_build_id.get(build.id)

        if existing_post is not None and not force:
            skipped_builds.append(name)
            continue

        if existing_post is None:
            builds_repo.set_public(build.id, True)
            post = community_repo.create_post(build.id, admin.id, build.name, AUTHOR_NOTES[name])
            posts_created += 1
        else:
            # force=True but this build was already shared in a prior run --
            # reuse the existing post rather than creating a second one.
            post = existing_post

        if existing_post is not None and existing_post.comments:
            # Never re-add comments to a post that already has them, even under
            # force -- avoids piling up duplicates on repeated forced runs.
            continue

        for commenter_username, content in _comments_for(name, build):
            commenter = users_repo.get_by_username(commenter_username)
            community_repo.add_comment(post.id, commenter.id, content)
            comments_created += 1

    return {
        "skipped": False,
        "posts": posts_created,
        "comments": comments_created,
        "already_shared": skipped_builds,
    }


if __name__ == "__main__":
    summary = run_community_shares_seed()
    print(f"Shared {summary['posts']} builds to the community, added {summary['comments']} comments.")
    if summary["already_shared"]:
        print(f"  Already shared (skipped): {summary['already_shared']}")
