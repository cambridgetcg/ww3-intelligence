# tests/test_analysis.py
from unittest.mock import patch
from analysis.llm_processor import process_leaf, LLMResult


def test_process_leaf_returns_llm_result():
    raw_leaf = {
        "source": "usgs",
        "type": "seismic",
        "raw_text": '{"mag": 4.3, "place": "Test Region", "depth_km": 2.1}',
        "location_name": "Test Region",
    }
    mock_response = {
        "event_title": "Synthetic Seismic M4.3",
        "event_category": "seismic",
        "classification": "speculation",
        "significance": 8,
        "summary": "Synthetic M4.3 seismic event used to exercise the schema.",
        "actors": [
            {"name": "Testland", "type": "state", "country": "TL", "role": "observer", "capability_tier": 3}
        ],
        "evidence": [
            {"type": "seismic_data", "description": "M4.3, depth 2.1km", "confidence": "high"}
        ],
        "narratives": [
            {"type": "official_state", "content": "Natural earthquake", "credibility_score": 4},
            {"type": "independent", "content": "Alternative synthetic explanation", "credibility_score": 7}
        ],
        "escalation_region": "Test Region",
        "escalation_level": 5,
        "technical": {
            "category_type": "seismic",
            "fields": {"depth_km": 2.1, "magnitude": 4.3, "nearest_facility": "Facility A"}
        },
        "impact": {
            "military_regional": 6, "military_global": 4,
            "economic_regional": 3, "economic_global": 2,
            "humanitarian_regional": 2, "humanitarian_global": 1,
            "diplomatic_regional": 7, "diplomatic_global": 6
        },
        "scenarios": [
            {
                "text": "Synthetic industrial cause confirmed",
                "probability": 35,
                "timeframe": "1-2 weeks",
                "trigger": "Independent inspection request denied",
                "children": [
                    {"text": "UN Security Council emergency session", "probability": 70, "timeframe": "1 week"},
                    {"text": "Unilateral military response", "probability": 20, "timeframe": "2-4 weeks"},
                ]
            },
            {
                "text": "Natural seismic event confirmed",
                "probability": 45,
                "timeframe": "1 week",
                "trigger": "Independent inspectors confirm a natural cause",
                "children": []
            },
            {
                "text": "Industrial accident at underground facility",
                "probability": 20,
                "timeframe": "2 weeks",
                "trigger": "Satellite imagery shows surface damage",
                "children": []
            }
        ]
    }

    with patch("analysis.llm_processor._call_claude", return_value=mock_response):
        result = process_leaf(raw_leaf)

    assert isinstance(result, LLMResult)
    assert result.event_title == "Synthetic Seismic M4.3"
    assert result.event_category == "seismic"
    assert result.significance == 8
    assert len(result.actors) == 1
    assert result.actors[0]["name"] == "Testland"
    assert len(result.scenarios) == 3
    assert result.escalation_level == 5
    assert result.escalation_region == "Test Region"


def test_process_leaf_handles_missing_optional_fields():
    raw_leaf = {
        "source": "manual",
        "type": "news",
        "raw_text": "Brief news item",
        "location_name": None,
    }
    minimal_response = {
        "event_title": "Test Event",
        "event_category": "political",
        "classification": "speculation",
        "significance": 3,
        "summary": "A brief event.",
    }

    with patch("analysis.llm_processor._call_claude", return_value=minimal_response):
        result = process_leaf(raw_leaf)

    assert result.actors == []
    assert result.scenarios == []
    assert result.escalation_level == 1
    assert result.impact == {}
