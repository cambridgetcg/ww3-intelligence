import json
from core.db import (
    Event, Actor, EventActor, Evidence, TechnicalAnalysis,
    Narrative, NarrativeSource, EscalationState, ScenarioNode, ImpactAssessment,
)
from analysis.llm_processor import LLMResult


def save_actors(event: Event, result: LLMResult):
    for a in result.actors:
        actor, _ = Actor.get_or_create(
            name=a["name"],
            defaults={
                "type": a.get("type", "state"),
                "country": a.get("country"),
                "capability_tier": a.get("capability_tier", 3),
                "motivation": a.get("motivation"),
            },
        )
        EventActor.get_or_create(
            event=event,
            actor=actor,
            defaults={"role": a.get("role", "observer")},
        )


def save_evidence(event: Event, result: LLMResult, source_url: str | None = None):
    for e in result.evidence:
        Evidence.create(
            event=event,
            type=e.get("type", "unknown"),
            description=e.get("description", ""),
            # Provenance comes only from the captured observation. A model-
            # mentioned URL remains attributed output in AnalysisRun JSON.
            source_url=source_url,
            confidence=e.get("confidence", "medium"),
        )


def save_technical(event: Event, result: LLMResult):
    if result.technical:
        TechnicalAnalysis.create(
            event=event,
            category_type=result.technical.get("category_type", event.category),
            fields_json=json.dumps(result.technical.get("fields", {})),
        )


def save_narratives(
    event: Event,
    result: LLMResult,
    source_url: str | None = None,
    source_title: str | None = None,
    published_at=None,
):
    for n in result.narratives:
        narrative = Narrative.create(
            event=event,
            narrative_type=n.get("type", "independent"),
            content=n.get("content", ""),
            credibility_score=n.get("credibility_score", 5),
            disinfo_flag=n.get("disinfo_flag"),
        )
        if source_url:
            NarrativeSource.create(
                narrative=narrative,
                url=source_url,
                title=source_title,
                published_at=published_at,
            )


def save_escalation(event: Event, result: LLMResult):
    if result.escalation_region:
        EscalationState.create(
            region=result.escalation_region,
            current_level=result.escalation_level,
            event=event,
        )


def save_impact(event: Event, result: LLMResult):
    if result.impact:
        ImpactAssessment.create(event=event, **result.impact)


def save_scenarios(event: Event, scenarios: list, parent=None):
    for s in scenarios:
        node = ScenarioNode.create(
            event=event,
            parent=parent,
            scenario_text=s["text"],
            probability=s.get("probability", 50),
            timeframe=s.get("timeframe"),
            trigger_condition=s.get("trigger"),
            confidence="medium",
        )
        if s.get("children"):
            save_scenarios(event, s["children"], parent=node)


def save_all_branches(
    event: Event,
    result: LLMResult,
    *,
    source_url: str | None = None,
    source_title: str | None = None,
    published_at=None,
):
    save_actors(event, result)
    save_evidence(event, result, source_url=source_url)
    save_technical(event, result)
    save_narratives(
        event,
        result,
        source_url=source_url,
        source_title=source_title,
        published_at=published_at,
    )
    save_escalation(event, result)
    save_impact(event, result)
    save_scenarios(event, result.scenarios)
