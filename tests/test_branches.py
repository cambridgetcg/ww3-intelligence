# tests/test_branches.py
from analysis.branches import save_all_branches
from analysis.llm_processor import LLMResult
from core.db import (
    Event, Actor, EventActor, Evidence, Narrative,
    EscalationState, TechnicalAnalysis, ScenarioNode, ImpactAssessment,
)


def _make_event():
    return Event.create(
        title="Test Event", date="2026-03-03",
        category="seismic", classification="speculation", significance=5,
    )


def _make_full_result():
    return LLMResult(
        event_title="Test",
        event_category="seismic",
        classification="speculation",
        significance=5,
        summary="test",
        actors=[
            {"name": "Testland", "type": "state", "country": "TL",
             "role": "observer", "capability_tier": 3}
        ],
        evidence=[
            {"type": "seismic_data", "description": "M4.3, depth 2.1km", "confidence": "high"}
        ],
        narratives=[
            {"type": "official_state", "content": "Natural earthquake", "credibility_score": 4},
            {"type": "independent", "content": "Alternative synthetic explanation", "credibility_score": 7},
        ],
        escalation_region="Test Region",
        escalation_level=5,
        technical={"category_type": "seismic", "fields": {"depth_km": 2.1, "magnitude": 4.3}},
        impact={
            "military_regional": 6, "military_global": 4,
            "economic_regional": 3, "economic_global": 2,
            "humanitarian_regional": 2, "humanitarian_global": 1,
            "diplomatic_regional": 7, "diplomatic_global": 6,
        },
        scenarios=[
            {
                "text": "Synthetic industrial cause confirmed",
                "probability": 35,
                "timeframe": "2 weeks",
                "trigger": "Inspection denied",
                "children": [
                    {"text": "UN sanctions", "probability": 70, "timeframe": "1 week"},
                    {"text": "Military response", "probability": 30, "timeframe": "1 month"},
                ],
            },
            {
                "text": "Natural seismic event",
                "probability": 65,
                "timeframe": "1 week",
                "trigger": "Inspection completed",
                "children": [],
            },
        ],
    )


def test_save_all_branches_creates_actor():
    event = _make_event()
    save_all_branches(event, _make_full_result())
    assert Actor.select().count() == 1
    assert Actor.get().name == "Testland"
    assert EventActor.select().count() == 1
    assert EventActor.get().role == "observer"


def test_save_all_branches_creates_evidence():
    event = _make_event()
    save_all_branches(event, _make_full_result())
    assert Evidence.select().count() == 1
    assert Evidence.get().confidence == "high"


def test_save_all_branches_creates_narratives():
    event = _make_event()
    save_all_branches(event, _make_full_result())
    assert Narrative.select().count() == 2
    types = {n.narrative_type for n in Narrative.select()}
    assert "official_state" in types
    assert "independent" in types


def test_save_all_branches_creates_escalation():
    event = _make_event()
    save_all_branches(event, _make_full_result())
    assert EscalationState.select().count() == 1
    state = EscalationState.get()
    assert state.current_level == 5
    assert state.region == "Test Region"


def test_save_all_branches_creates_technical():
    event = _make_event()
    save_all_branches(event, _make_full_result())
    assert TechnicalAnalysis.select().count() == 1
    tech = TechnicalAnalysis.get()
    assert tech.category_type == "seismic"
    assert "depth_km" in tech.fields_json


def test_save_all_branches_creates_impact():
    event = _make_event()
    save_all_branches(event, _make_full_result())
    assert ImpactAssessment.select().count() == 1
    imp = ImpactAssessment.get()
    assert imp.military_regional == 6
    assert imp.diplomatic_regional == 7


def test_save_all_branches_creates_scenario_tree():
    event = _make_event()
    save_all_branches(event, _make_full_result())
    all_nodes = list(ScenarioNode.select())
    assert len(all_nodes) == 4  # 2 root + 2 children
    root_nodes = [n for n in all_nodes if n.parent_id is None]
    assert len(root_nodes) == 2
    child_nodes = [n for n in all_nodes if n.parent_id is not None]
    assert len(child_nodes) == 2


def test_deduplicates_actors():
    event = _make_event()
    result = _make_full_result()
    save_all_branches(event, result)
    # Save again — actor should not be duplicated
    save_all_branches(event, result)
    assert Actor.select().count() == 1


def test_empty_result_creates_nothing():
    event = _make_event()
    empty_result = LLMResult(
        event_title="Empty", event_category="other",
        classification="speculation", significance=1, summary="none",
    )
    save_all_branches(event, empty_result)
    assert Actor.select().count() == 0
    assert ScenarioNode.select().count() == 0
    assert Narrative.select().count() == 0
