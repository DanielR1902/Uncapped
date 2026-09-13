"""Request/response pydantic models for the OpenRouter build-analysis call.

Matches spec.md §6.2/§6.4 exactly. All response parsing goes through
BuildAnalysisResponse — a validation failure here is treated the same as a
network failure by llm/client.py (heuristic fallback, never a crash).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ComponentPayload(BaseModel):
    category: str
    name: str
    critical_specs: dict = Field(default_factory=dict)
    extended_specs: dict = Field(default_factory=dict)


class DeterministicPrecheck(BaseModel):
    compatibility_score: float = Field(ge=0, le=100)
    failed_rules: list[str] = Field(default_factory=list)
    baseline_bottleneck_percentage: float = Field(ge=0, le=100)
    bottleneck_direction: Literal["CPU-bound", "GPU-bound", "Balanced"]


class BuildAnalysisRequest(BaseModel):
    workload_profile: str | None = None
    budget_ceiling: float | None = None
    components: list[ComponentPayload]
    deterministic_precheck: DeterministicPrecheck


class SynergyEvaluation(BaseModel):
    overall_score: float = Field(ge=0, le=100)
    breakdown: dict[str, float] = Field(default_factory=dict)
    positive_synergies: list[str] = Field(default_factory=list)
    negative_conflicts: list[str] = Field(default_factory=list)


class ResolutionImpact(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    p1080: str = Field(alias="1080p")
    p1440: str = Field(alias="1440p")
    p4k: str = Field(alias="4K")


class BottleneckAnalysis(BaseModel):
    bottleneck_percentage: float = Field(ge=0, le=100)
    limiting_component: Literal["CPU", "GPU", "None"]
    resolution_impact: ResolutionImpact


class ArchitecturalInsights(BaseModel):
    summary: str
    upgrade_path: list[str] = Field(default_factory=list)
    quirks: list[str] = Field(default_factory=list)


class BuildAnalysisResponse(BaseModel):
    synergy: SynergyEvaluation
    bottleneck: BottleneckAnalysis
    insights: ArchitecturalInsights
    source: Literal["llm", "heuristic"] = "llm"
