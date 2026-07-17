import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from analysis.schema import validate_llm_payload

_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "leaf_to_intelligence.txt"
MAX_PROMPT_SOURCE_CHARS = 50_000


def load_prompt_contract() -> tuple[str, str]:
    """Load the exact prompt text and its SHA-256 identifier together."""
    template = _PROMPT_PATH.read_text()
    return template, hashlib.sha256(template.encode("utf-8")).hexdigest()


def prompt_version() -> str:
    """Return a stable identifier for the exact prompt contract in use."""
    return load_prompt_contract()[1]


@dataclass
class LLMResult:
    event_title: str
    event_category: str
    classification: str
    significance: int
    summary: str
    actors: list = field(default_factory=list)
    evidence: list = field(default_factory=list)
    narratives: list = field(default_factory=list)
    escalation_region: str = ""
    escalation_level: int = 1
    technical: dict = field(default_factory=dict)
    impact: dict = field(default_factory=dict)
    scenarios: list = field(default_factory=list)


def _call_claude(prompt: str) -> dict:
    # Routed through the local Claude Code CLI (no API key) — see analysis/claude_cli.py
    from analysis.claude_cli import call_claude_json
    return call_claude_json(prompt)


def process_leaf(leaf: dict, *, prompt_template: str | None = None) -> LLMResult:
    template = prompt_template if prompt_template is not None else load_prompt_contract()[0]
    raw_text = str(leaf.get("raw_text", ""))
    if len(raw_text) > MAX_PROMPT_SOURCE_CHARS:
        raw_text = (
            raw_text[:MAX_PROMPT_SOURCE_CHARS]
            + "\n[TRUNCATED: source record exceeded the analysis input limit]"
        )
    prompt = template.format(
        raw_text=raw_text,
        source=str(leaf.get("source", "unknown"))[:200],
        type=str(leaf.get("type", "unknown"))[:100],
        location_name=str(leaf.get("location_name") or "unknown")[:500],
        canonical_url=str(leaf.get("canonical_url") or "unavailable")[:2_000],
    )
    return LLMResult(**validate_llm_payload(_call_claude(prompt)))
