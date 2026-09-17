"""Deterministic compatibility rules — the only source of pass/fail for a build.

Pure Python: no Streamlit, no network, no writes to the database (see
engine/CLAUDE.md). Every rule function takes the current build_state (a dict
mapping category name -> selected Component) and returns a RuleResult, or None
if the rule doesn't apply yet (one or both required components aren't picked).

Rules implement spec.md §5.1 (1-8). Rule 7 (cooler clearance) is split into an
air-cooler height check and an AIO radiator check — see spec.md's note there for
why they compare against different Case fields.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable

from db.models import Component

BuildState = dict[str, Component]

# Named constants — never inline these multipliers/baselines (engine/CLAUDE.md).
PSU_HEADROOM_MULTIPLIER = 1.3
SYSTEM_BASELINE_WATTS = 50


@dataclass(frozen=True)
class RuleResult:
    rule: str
    passed: bool
    message: str


@dataclass
class CompatibilityReport:
    is_compatible: bool
    compatibility_score: float
    issues: list[str] = field(default_factory=list)
    results: list[RuleResult] = field(default_factory=list)


def _specs(component: Component | None) -> dict:
    if component is None or not component.specs_json:
        return {}
    return json.loads(component.specs_json)


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _ram_kit_module_count(component: Component) -> int:
    """Parses "(NxYGB)" from a RAM component's name to get its real
    modules-per-kit (e.g. "16GB (2x8GB)" -> 2) — no structured field exists
    for this in the catalog, so this is a best-effort name parse matching
    the catalog's consistent naming convention, with a safe fallback of 1
    module if the pattern isn't found (never fabricates a number beyond
    what's observably true of the name string)."""
    if component.name:
        match = re.search(r"\((\d+)\s*x\s*\d+\s*GB\)", component.name, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return 1


def check_cpu_motherboard_socket(build_state: BuildState) -> RuleResult | None:
    cpu = build_state.get("CPU")
    motherboard = build_state.get("Motherboard")
    if cpu is None or motherboard is None:
        return None
    passed = bool(cpu.socket) and cpu.socket == motherboard.socket
    message = (
        f"CPU socket ({cpu.socket}) matches motherboard socket ({motherboard.socket})."
        if passed
        else f"Socket mismatch: CPU is {cpu.socket}, motherboard is {motherboard.socket}."
    )
    return RuleResult("cpu_motherboard_socket", passed, message)


def check_ram_motherboard_type(build_state: BuildState) -> RuleResult | None:
    motherboard = build_state.get("Motherboard")
    ram = build_state.get("RAM")
    if motherboard is None or ram is None:
        return None
    passed = bool(ram.ram_type) and ram.ram_type == motherboard.ram_type
    message = (
        f"RAM type ({ram.ram_type}) matches motherboard-supported type ({motherboard.ram_type})."
        if passed
        else f"RAM type mismatch: RAM is {ram.ram_type}, motherboard supports {motherboard.ram_type}."
    )
    return RuleResult("ram_motherboard_type", passed, message)


def check_cooler_socket_support(build_state: BuildState) -> RuleResult | None:
    cpu = build_state.get("CPU")
    cooler = build_state.get("Cooler")
    if cpu is None or cooler is None:
        return None
    supported = _split_csv(cooler.socket)
    passed = bool(cpu.socket) and cpu.socket in supported
    message = (
        f"Cooler supports CPU socket {cpu.socket}."
        if passed
        else f"Cooler does not support CPU socket {cpu.socket} (supports: {', '.join(supported) or 'none listed'})."
    )
    return RuleResult("cooler_socket_support", passed, message)


def check_case_motherboard_form_factor(build_state: BuildState) -> RuleResult | None:
    case = build_state.get("Case")
    motherboard = build_state.get("Motherboard")
    if case is None or motherboard is None:
        return None
    accepted = _split_csv(case.form_factor)
    passed = bool(motherboard.form_factor) and motherboard.form_factor in accepted
    message = (
        f"Case accepts motherboard form factor {motherboard.form_factor}."
        if passed
        else f"Case does not accept motherboard form factor {motherboard.form_factor} "
        f"(accepts: {', '.join(accepted) or 'none listed'})."
    )
    return RuleResult("case_motherboard_form_factor", passed, message)


def check_case_psu_form_factor(build_state: BuildState) -> RuleResult | None:
    case = build_state.get("Case")
    psu = build_state.get("PSU")
    if case is None or psu is None:
        return None
    accepted = _split_csv(case.psu_form_factor_support)
    passed = bool(psu.form_factor) and psu.form_factor in accepted
    message = (
        f"Case accepts PSU form factor {psu.form_factor}."
        if passed
        else f"Case does not accept PSU form factor {psu.form_factor} "
        f"(accepts: {', '.join(accepted) or 'none listed'})."
    )
    return RuleResult("case_psu_form_factor", passed, message)


def check_gpu_case_clearance(build_state: BuildState) -> RuleResult | None:
    case = build_state.get("Case")
    gpu = build_state.get("GPU")
    if case is None or gpu is None:
        return None
    gpu_length = _specs(gpu).get("length_mm")
    if gpu_length is None or case.max_gpu_length_mm is None:
        return None
    passed = gpu_length <= case.max_gpu_length_mm
    message = (
        f"GPU length ({gpu_length}mm) fits case clearance ({case.max_gpu_length_mm}mm)."
        if passed
        else f"GPU is too long: {gpu_length}mm vs. case clearance of {case.max_gpu_length_mm}mm."
    )
    return RuleResult("gpu_case_clearance", passed, message)


def check_cooler_case_clearance(build_state: BuildState) -> RuleResult | None:
    case = build_state.get("Case")
    cooler = build_state.get("Cooler")
    if case is None or cooler is None:
        return None

    cooler_specs = _specs(cooler)
    cooler_type = cooler_specs.get("cooler_type", "Air")

    if cooler_type == "AIO":
        radiator_mm = cooler_specs.get("radiator_size_mm")
        max_radiator_mm = _specs(case).get("max_radiator_mm")
        if radiator_mm is None or max_radiator_mm is None:
            return None
        passed = radiator_mm <= max_radiator_mm
        message = (
            f"{radiator_mm}mm radiator fits case's radiator mount ({max_radiator_mm}mm max)."
            if passed
            else f"Radiator too large: {radiator_mm}mm vs. case's {max_radiator_mm}mm max radiator mount."
        )
        return RuleResult("cooler_case_clearance", passed, message)

    height_mm = cooler_specs.get("height_mm")
    if height_mm is None or case.max_cooler_height_mm is None:
        return None
    passed = height_mm <= case.max_cooler_height_mm
    message = (
        f"Cooler height ({height_mm}mm) fits case clearance ({case.max_cooler_height_mm}mm)."
        if passed
        else f"Cooler too tall: {height_mm}mm vs. case clearance of {case.max_cooler_height_mm}mm."
    )
    return RuleResult("cooler_case_clearance", passed, message)


def check_psu_headroom(build_state: BuildState) -> RuleResult | None:
    cpu = build_state.get("CPU")
    psu = build_state.get("PSU")
    if cpu is None or psu is None:
        return None
    gpu = build_state.get("GPU")

    cpu_tdp = cpu.tdp_watts or 0
    gpu_tdp = (gpu.tdp_watts or 0) if gpu is not None else 0
    required_watts = (cpu_tdp + gpu_tdp + SYSTEM_BASELINE_WATTS) * PSU_HEADROOM_MULTIPLIER

    if psu.wattage_capacity is None:
        return None
    passed = psu.wattage_capacity >= required_watts
    message = (
        f"PSU capacity ({psu.wattage_capacity}W) covers estimated draw with headroom "
        f"(requires >= {required_watts:.0f}W)."
        if passed
        else f"PSU under-provisioned: {psu.wattage_capacity}W supplied, "
        f"requires >= {required_watts:.0f}W ({PSU_HEADROOM_MULTIPLIER}x headroom over "
        f"{cpu_tdp + gpu_tdp + SYSTEM_BASELINE_WATTS}W estimated draw)."
    )
    return RuleResult("psu_headroom", passed, message)


def check_ram_capacity(build_state: BuildState, quantities: dict[str, int] | None) -> RuleResult | None:
    motherboard = build_state.get("Motherboard")
    ram = build_state.get("RAM")
    if motherboard is None or ram is None:
        return None
    mobo_specs = _specs(motherboard)
    ram_slots = mobo_specs.get("ram_slots")
    max_ram_gb = mobo_specs.get("max_ram_gb")
    if ram_slots is None or max_ram_gb is None or ram.capacity_gb is None:
        return None
    qty = (quantities or {}).get("RAM", 1)
    total_capacity = ram.capacity_gb * qty
    # Real per-module math: a kit's name encodes its actual stick count
    # ("16GB (2x8GB)" -> 2 modules), so the true physical slot cost of `qty`
    # kits is qty * modules-per-kit, not qty itself (see
    # _ram_kit_module_count's docstring for why this is a name parse rather
    # than a structured field).
    kit_modules = _ram_kit_module_count(ram)
    total_modules_used = qty * kit_modules
    passed = total_modules_used <= ram_slots and total_capacity <= max_ram_gb
    message = (
        f"{qty}x {ram.capacity_gb}GB RAM kit(s) ({kit_modules} module(s) per kit, "
        f"{total_modules_used} module(s) total, {total_capacity}GB total) fits "
        f"motherboard's {ram_slots} RAM slot(s) and {max_ram_gb}GB max capacity."
        if passed
        else f"RAM capacity mismatch: {qty}x {ram.capacity_gb}GB kit(s) ({kit_modules} module(s) per "
        f"kit, {total_modules_used} module(s) total, {total_capacity}GB total) exceeds motherboard's "
        f"{ram_slots} RAM slot(s) / {max_ram_gb}GB max capacity."
    )
    return RuleResult("ram_capacity", passed, message)


def resolve_quantity_limit(build_state: BuildState, category: str) -> tuple[int | None, str]:
    """Single source of truth for "what's the real, catalog/motherboard-
    backed max quantity for this category, and why" — used by the quantity-
    aware rules above, by llm/advisory.py (replacing its own duplicate slot-
    count logic), and by the UI for a quantity input's max_value/caption.

    Returns (max_allowed, reason). max_allowed is None when no real
    motherboard/catalog data exists to bound this category's quantity —
    callers must NOT fabricate a fallback number in that case (a UI layer
    may apply its own clearly-labeled conservative display cap, but that is
    a UI convenience, never claimed as a real hardware-derived limit)."""
    if category not in ("RAM", "Storage"):
        return None, ""
    motherboard = build_state.get("Motherboard")
    if motherboard is None:
        return None, "Select a Motherboard to see this category's real slot limit."
    specs = _specs(motherboard)

    if category == "RAM":
        ram = build_state.get("RAM")
        ram_slots = specs.get("ram_slots")
        if ram is None or ram_slots is None:
            return None, "No RAM selected yet, or this motherboard has no listed DIMM slot count."
        kit_modules = _ram_kit_module_count(ram)
        max_qty = max(1, ram_slots // kit_modules)
        return max_qty, (
            f"This motherboard supports up to {max_qty} kit(s) of this type "
            f"({ram_slots} DIMM slots total, {kit_modules} module(s) per kit)."
        )

    # Storage
    storage = build_state.get("Storage")
    if storage is None:
        return None, "No Storage selected yet."
    if storage.interface and "NVMe" in storage.interface:
        m2_slots = specs.get("m2_slots")
        if m2_slots is None:
            return None, "This motherboard has no listed M.2 slot count."
        return m2_slots, f"This motherboard supports up to {m2_slots} M.2 NVMe drive(s)."
    return None, "No motherboard-specific port-count data exists for this drive's interface in this catalog."


def check_storage_slot_capacity(build_state: BuildState, quantities: dict[str, int] | None) -> RuleResult | None:
    motherboard = build_state.get("Motherboard")
    storage = build_state.get("Storage")
    if motherboard is None or storage is None:
        return None
    m2_slots = _specs(motherboard).get("m2_slots")
    if m2_slots is None or not storage.interface or "NVMe" not in storage.interface:
        return None  # only M.2/NVMe drives have a real slot-count constraint in this catalog
    qty = (quantities or {}).get("Storage", 1)
    passed = qty <= m2_slots
    message = (
        f"{qty}x NVMe drive(s) fits motherboard's {m2_slots} M.2 slot(s)."
        if passed
        else f"Storage slot mismatch: {qty}x NVMe drive(s) exceeds motherboard's {m2_slots} M.2 slot(s)."
    )
    return RuleResult("storage_slot_capacity", passed, message)


_RULES: tuple[Callable[[BuildState], RuleResult | None], ...] = (
    check_cpu_motherboard_socket,
    check_ram_motherboard_type,
    check_cooler_socket_support,
    check_case_motherboard_form_factor,
    check_case_psu_form_factor,
    check_gpu_case_clearance,
    check_cooler_case_clearance,
    check_psu_headroom,
)

_QUANTITY_RULES: tuple[Callable[[BuildState, dict[str, int] | None], RuleResult | None], ...] = (
    check_ram_capacity,
    check_storage_slot_capacity,
)


def run_all_checks(build_state: BuildState, quantities: dict[str, int] | None = None) -> list[RuleResult]:
    results = []
    for rule in _RULES:
        result = rule(build_state)
        if result is not None:
            results.append(result)
    for quantity_rule in _QUANTITY_RULES:
        result = quantity_rule(build_state, quantities)
        if result is not None:
            results.append(result)
    return results


def evaluate_build(build_state: BuildState, quantities: dict[str, int] | None = None) -> CompatibilityReport:
    results = run_all_checks(build_state, quantities)
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    score = 100.0 if total == 0 else 100.0 * passed / total
    issues = [r.message for r in results if not r.passed]
    return CompatibilityReport(
        is_compatible=all(r.passed for r in results),
        compatibility_score=score,
        issues=issues,
        results=results,
    )
