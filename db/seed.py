"""Seeding routine — loads db/seed_data.py literals into the database.

Run directly: `python -m db.seed` (creates tables if needed, then seeds).
Idempotent: skips seeding if the components table is already populated,
unless force=True.
"""
from __future__ import annotations

import json

from db.database import get_session, init_db
from db.models import Component, WorkloadMapping
from db.seed_data import ALL_COMPONENTS


def run_seed(force: bool = False) -> dict[str, int]:
    init_db()
    session = get_session()
    try:
        existing = session.query(Component).count()
        if existing > 0 and not force:
            return {
                "components": existing,
                "workload_mappings": session.query(WorkloadMapping).count(),
                "skipped": True,
            }

        if force:
            session.query(WorkloadMapping).delete()
            session.query(Component).delete()

        component_count = 0
        mapping_count = 0
        for part in ALL_COMPONENTS:
            workloads = part["workloads"]
            row = Component(
                category=part["category"],
                name=part["name"],
                brand=part["brand"],
                price_usd=part["price_usd"],
                socket=part["socket"],
                ram_type=part["ram_type"],
                tdp_watts=part["tdp_watts"],
                wattage_capacity=part["wattage_capacity"],
                form_factor=part["form_factor"],
                capacity_gb=part["capacity_gb"],
                interface=part["interface"],
                max_gpu_length_mm=part["max_gpu_length_mm"],
                max_cooler_height_mm=part["max_cooler_height_mm"],
                psu_form_factor_support=part["psu_form_factor_support"],
                chipset=part["chipset"],
                benchmark_score=part["benchmark_score"],
                specs_json=json.dumps(part["specs_json"]),
            )
            session.add(row)
            session.flush()  # populate row.id for the workload_mappings FK
            component_count += 1

            for profile, tier, weight in workloads:
                session.add(
                    WorkloadMapping(
                        component_id=row.id,
                        workload_profile=profile,
                        tier=tier,
                        weight=weight,
                    )
                )
                mapping_count += 1

        session.commit()
        return {
            "components": component_count,
            "workload_mappings": mapping_count,
            "skipped": False,
        }
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    summary = run_seed()
    if summary["skipped"]:
        print(
            f"Catalog already seeded ({summary['components']} components, "
            f"{summary['workload_mappings']} workload mappings) — skipped. "
            "Pass force=True to run_seed() to reseed."
        )
    else:
        print(
            f"Seeded {summary['components']} components and "
            f"{summary['workload_mappings']} workload mappings."
        )
