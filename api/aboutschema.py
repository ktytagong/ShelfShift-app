"""Published count invariants (11-AI-SPEC §4b).

Phase 10 groups are Safety, Assistant, Reading with p/a/f/x and failures. The
public projection normalizes them to assistant/reading/safety, maps p/f/x/a to
passed/failed/not_applicable/applicable_total, counts recorded failed items as
raw failures and explicitly caught failure contexts as safeguard catches.
Explicit categories already use the §4b order; they are never silently sorted.
"""
from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class PublishedEvalCategorySummary(BaseModel):
    model_config = ConfigDict(extra='forbid')
    category: Literal['assistant','reading','safety']
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    not_applicable: int = Field(ge=0)
    applicable_total: int = Field(ge=0)
    raw_model_failures: int = Field(ge=0)
    safeguard_catches: int = Field(ge=0)
    failure_notes: list[str] = Field(default_factory=list)

    @model_validator(mode='after')
    def _validate_counts(self) -> 'PublishedEvalCategorySummary':
        if self.passed+self.failed != self.applicable_total:
            raise ValueError('passed + failed must equal applicable_total')
        if self.raw_model_failures+self.safeguard_catches < self.failed:
            raise ValueError('raw_model_failures + safeguard_catches must account for failed items')
        return self


class UnpublishedEvalNotice(BaseModel):
    model_config = ConfigDict(extra='forbid')
    run_date: str
    reason: str


class PublishedEvalSummary(BaseModel):
    model_config = ConfigDict(extra='forbid')
    label: Literal['Preliminary evaluation · 10 scenarios × 3 runs'] = 'Preliminary evaluation · 10 scenarios × 3 runs'
    eval_id: str
    model_id: str
    assistant_version: str
    dataset_version: str
    engine_version: str
    build_commit: str
    reference_fingerprint: str
    published_at: str
    version_match: bool
    differing_fields: list[str] = Field(default_factory=list)
    latest_unpublished: UnpublishedEvalNotice | None = None
    scenarios_count: Literal[10] = 10
    runs_per_scenario: Literal[3] = 3
    safety_gate_passed: bool
    categories: list[PublishedEvalCategorySummary]

    @model_validator(mode='after')
    def _validate_categories(self) -> 'PublishedEvalSummary':
        if [c.category for c in self.categories] != ['assistant','reading','safety']:
            raise ValueError("categories must be unique and ordered as ['assistant', 'reading', 'safety']")
        return self


def validate_categories(summary: dict) -> bool:
    try:
        if 'categories' in summary:
            categories = summary['categories']
        else:
            groups = summary['groups']
            if [g['name'] for g in groups] != ['Safety','Assistant','Reading']:
                return False
            by_name={g['name'].lower():g for g in groups}
            categories=[]
            for name in ('assistant','reading','safety'):
                g=by_name[name]; failures=g['failures']
                if len(failures) != g['f']:
                    return False
                categories.append(dict(category=name,passed=g['p'],failed=g['f'],not_applicable=g['x'],
                  applicable_total=g['a'],raw_model_failures=len(failures),
                  safeguard_catches=sum(f.get('context') in ('Attempted, not executed','Generation failed · protection passed') for f in failures),
                  failure_notes=[f['note'] for f in failures if f.get('note')]))
        payload={key:summary[key] for key in PublishedEvalSummary.model_fields if key in summary}
        payload['categories']=categories
        payload['safety_gate_passed']=summary.get('safety_gate_passed', bool(summary.get('safety')) and
          all(g['result']=='PASS' for g in summary.get('safety',[])))
        PublishedEvalSummary.model_validate(payload)
        return True
    except (ValidationError, KeyError, TypeError, ValueError):
        return False
