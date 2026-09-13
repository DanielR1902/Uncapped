"""System prompt + request-payload builder for the combined build-analysis call
(spec.md §6.3 — synergy + bottleneck + insights in one call, not three)."""
from __future__ import annotations

import json

from db.models import Component
from engine import compatibility, scoring
from engine.compatibility import BuildState
from llm.schemas import BuildAnalysisRequest, ComponentPayload, DeterministicPrecheck

SYSTEM_PROMPT = """You are a PC-hardware analyst for Uncapped, an AI-assisted PC configuration platform.

You are given a build's components and a deterministic compatibility pre-check that has ALREADY
determined pass/fail for every hard constraint (socket, wattage, clearance, etc). Never contradict,
re-derive, or override that pass/fail — only interpret and add judgment on top of it.

Respond with STRICT JSON and nothing else (no prose, no markdown fences), matching exactly this shape:
{
  "synergy": {
    "overall_score": <0-100>,
    "breakdown": {"<aspect>": <0-100>, ...},
    "positive_synergies": ["<string>", ...],
    "negative_conflicts": ["<string>", ...]
  },
  "bottleneck": {
    "bottleneck_percentage": <0-100>,
    "limiting_component": "CPU" | "GPU" | "None",
    "resolution_impact": {"1080p": "<string>", "1440p": "<string>", "4K": "<string>"}
  },
  "insights": {
    "summary": "<string>",
    "upgrade_path": ["<string>", ...],
    "quirks": ["<string>", ...]
  }
}

The bottleneck_percentage you return must stay within +/-10 of the supplied
baseline_bottleneck_percentage in deterministic_precheck.
"""

# Compatibility-critical fields worth surfacing to the model when present —
# mirrors the columns engine/compatibility.py actually checks (spec.md §4.1).
_CRITICAL_FIELD_NAMES = (
    "socket",
    "ram_type",
    "tdp_watts",
    "wattage_capacity",
    "form_factor",
    "capacity_gb",
    "interface",
    "max_gpu_length_mm",
    "max_cooler_height_mm",
    "psu_form_factor_support",
    "chipset",
    "benchmark_score",
)


def _component_payload(component: Component) -> ComponentPayload:
    critical = {
        field: getattr(component, field)
        for field in _CRITICAL_FIELD_NAMES
        if getattr(component, field) is not None
    }
    extended = json.loads(component.specs_json) if component.specs_json else {}
    return ComponentPayload(
        category=component.category,
        name=component.name,
        critical_specs=critical,
        extended_specs=extended,
    )


def build_request(
    build_state: BuildState,
    workload_profile: str | None = None,
    budget_ceiling: float | None = None,
) -> BuildAnalysisRequest:
    report = compatibility.evaluate_build(build_state)
    bottleneck_pct, direction = scoring.bottleneck_percentage_baseline(build_state)

    precheck = DeterministicPrecheck(
        compatibility_score=report.compatibility_score,
        failed_rules=list(report.issues),
        baseline_bottleneck_percentage=bottleneck_pct,
        bottleneck_direction=direction,
    )
    components = [_component_payload(component) for component in build_state.values()]

    return BuildAnalysisRequest(
        workload_profile=workload_profile,
        budget_ceiling=budget_ceiling,
        components=components,
        deterministic_precheck=precheck,
    )


def build_messages(request: BuildAnalysisRequest) -> list[dict]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": request.model_dump_json()},
    ]
