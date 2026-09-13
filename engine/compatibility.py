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


def run_all_checks(build_state: BuildState) -> list[RuleResult]:
    results = []
    for rule in _RULES:
        result = rule(build_state)
        if result is not None:
            results.append(result)
    return results


def evaluate_build(build_state: BuildState) -> CompatibilityReport:
    results = run_all_checks(build_state)
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
