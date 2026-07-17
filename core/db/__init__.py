import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from peewee import (
    SqliteDatabase, Model, AutoField, CharField, TextField,
    IntegerField, FloatField, BooleanField, DateTimeField,
    ForeignKeyField, DateField,
)
from core.settings import database_path

database = SqliteDatabase(
    database_path(),
    pragmas={
        "foreign_keys": 1,
        "journal_mode": "wal",
        "busy_timeout": 5_000,
    },
)
SCHEMA_VERSION = 2


def _utcnow_naive():
    return datetime.now(timezone.utc).replace(tzinfo=None)

HORIZONS = [
    ("1h",  "1 Hour"),
    ("3h",  "3 Hours"),
    ("12h", "12 Hours"),
    ("1d",  "1 Day"),
    ("3d",  "3 Days"),
    ("1w",  "1 Week"),
    ("2w",  "2 Weeks"),
    ("1m",  "1 Month"),
    ("3m",  "3 Months"),
    ("6m",  "6 Months"),
]
HORIZON_CODES = [code for code, _ in HORIZONS]
HORIZON_LABELS = {code: label for code, label in HORIZONS}


class BaseModel(Model):
    class Meta:
        database = database


class Event(BaseModel):
    id = AutoField()
    title = CharField()
    date = DateField()
    location_name = CharField(null=True)
    lat = FloatField(null=True)
    lon = FloatField(null=True)
    category = CharField()        # seismic|military|political|cyber|economic|other
    subcategory = CharField(null=True)
    classification = CharField(default="speculation")  # speculation|reported|confirmed
    significance = IntegerField(default=5)  # 1-10
    summary = TextField(null=True)
    full_analysis = TextField(null=True)
    confidence_score = FloatField(null=True)
    created_at = DateTimeField(default=_utcnow_naive)
    updated_at = DateTimeField(default=_utcnow_naive)


class Actor(BaseModel):
    id = AutoField()
    name = CharField()
    type = CharField()            # state|non-state|individual|organization
    country = CharField(null=True)
    motivation = TextField(null=True)
    capability_tier = IntegerField(default=3)  # 1-5
    behavior_notes = TextField(null=True)


class EventActor(BaseModel):
    id = AutoField()
    event = ForeignKeyField(Event, backref="event_actors")
    actor = ForeignKeyField(Actor, backref="event_actors")
    role = CharField()            # perpetrator|victim|observer|beneficiary|mediator


class Evidence(BaseModel):
    id = AutoField()
    event = ForeignKeyField(Event, backref="evidence")
    type = CharField()
    description = TextField()
    source_url = CharField(null=True)
    confidence = CharField(default="medium")  # low|medium|high
    created_at = DateTimeField(default=_utcnow_naive)


class TechnicalAnalysis(BaseModel):
    id = AutoField()
    event = ForeignKeyField(Event, backref="technical_analyses")
    category_type = CharField()   # seismic|military|cyber|chemical|nuclear
    fields_json = TextField()     # JSON blob of type-specific fields
    notes = TextField(null=True)


class Narrative(BaseModel):
    id = AutoField()
    event = ForeignKeyField(Event, backref="narratives")
    narrative_type = CharField()  # official_state|opposition|western_intel|eastern_intel|social_media|independent
    content = TextField()
    credibility_score = IntegerField(default=5)  # 1-10
    disinfo_flag = CharField(null=True)  # fabricated|misleading|unverified|credible


class NarrativeSource(BaseModel):
    id = AutoField()
    narrative = ForeignKeyField(Narrative, backref="sources")
    url = CharField()
    title = CharField(null=True)
    published_at = DateTimeField(null=True)
    reliability_score = IntegerField(default=3)  # 1-5


class EscalationState(BaseModel):
    id = AutoField()
    region = CharField()
    current_level = IntegerField()  # 1-10
    level_description = TextField(null=True)
    triggers_for_next = TextField(null=True)
    de_escalation_pathways = TextField(null=True)
    assessed_at = DateTimeField(default=_utcnow_naive)
    event = ForeignKeyField(Event, backref="escalation_states", null=True)


class ScenarioNode(BaseModel):
    id = AutoField()
    event = ForeignKeyField(Event, backref="scenario_nodes")
    parent = ForeignKeyField("self", backref="children", null=True)
    scenario_text = TextField()
    probability = IntegerField(default=50)  # 0-100
    timeframe = CharField(null=True)
    trigger_condition = TextField(null=True)
    confidence = CharField(default="medium")  # low|medium|high
    resolved = BooleanField(default=False)
    outcome_text = TextField(null=True)
    created_at = DateTimeField(default=_utcnow_naive)


class HistoricalPrecedent(BaseModel):
    id = AutoField()
    event = ForeignKeyField(Event, backref="precedents")
    linked_event = ForeignKeyField(Event, null=True, backref="referenced_by")
    external_ref_url = CharField(null=True)
    title = CharField()
    similarity_score = IntegerField(default=5)  # 1-10
    what_happened = TextField()
    predictive_implication = TextField()


class ImpactAssessment(BaseModel):
    id = AutoField()
    event = ForeignKeyField(Event, backref="impact_assessments")
    military_regional = IntegerField(default=1)
    military_global = IntegerField(default=1)
    economic_regional = IntegerField(default=1)
    economic_global = IntegerField(default=1)
    humanitarian_regional = IntegerField(default=1)
    humanitarian_global = IntegerField(default=1)
    diplomatic_regional = IntegerField(default=1)
    diplomatic_global = IntegerField(default=1)
    assessed_at = DateTimeField(default=_utcnow_naive)


class RawLeaf(BaseModel):
    id = AutoField()
    ts = DateTimeField(default=_utcnow_naive)
    source = CharField()          # usgs|acled|rss|gdelt|un|satellite|manual
    type = CharField()            # seismic|conflict|news|statement|imagery
    raw_text = TextField()
    geo_lat = FloatField(null=True)
    geo_lon = FloatField(null=True)
    location_name = CharField(null=True)
    confidence = CharField(default="medium")
    processed = BooleanField(default=False)
    event = ForeignKeyField(Event, backref="raw_leaves", null=True)


class Observation(BaseModel):
    """Immutable source material captured before analysis."""

    id = AutoField()
    dedupe_key = CharField(unique=True)
    source = CharField()
    source_record_id = TextField(null=True)
    type = CharField()
    raw_text = TextField()
    content_hash = CharField()
    canonical_url = TextField(null=True)
    published_at = DateTimeField(null=True)
    fetched_at = DateTimeField()
    geo_lat = FloatField(null=True)
    geo_lon = FloatField(null=True)
    location_name = CharField(null=True)
    source_confidence = CharField(default="not-assessed")
    synthetic = BooleanField(default=False)
    supersedes = ForeignKeyField("self", backref="revisions", null=True, on_delete="SET NULL")
    created_at = DateTimeField(default=_utcnow_naive)

    def save(self, *args, **kwargs):
        if self.get_id() is not None:
            raise ValueError("observations are immutable; create a new observation instead")
        return super().save(*args, **kwargs)


class AnalysisRun(BaseModel):
    """Mutable processing state kept separate from immutable observations."""

    id = AutoField()
    observation = ForeignKeyField(Observation, backref="analysis_runs")
    event = ForeignKeyField(Event, backref="analysis_runs", null=True)
    analysis_key = CharField(null=True)
    status = CharField(default="pending")
    model_name = CharField(default="unknown")
    prompt_version = CharField(default="unknown")
    code_version = CharField(default="unknown")
    claim_token = CharField(null=True)
    attempt = IntegerField(default=0)
    output_json = TextField(null=True)
    error = TextField(null=True)
    error_stage = CharField(null=True)
    started_at = DateTimeField(null=True)
    completed_at = DateTimeField(null=True)
    created_at = DateTimeField(default=_utcnow_naive)
    updated_at = DateTimeField(default=_utcnow_naive)

    def save(self, *args, **kwargs):
        self.updated_at = _utcnow_naive()
        return super().save(*args, **kwargs)


class Prediction(BaseModel):
    id = AutoField()
    event = ForeignKeyField(Event, backref="predictions")
    scenario_node = ForeignKeyField(ScenarioNode, backref="predictions", null=True)
    probability = IntegerField()  # 0-100
    confidence = CharField(default="medium")
    timeframe = CharField(null=True)
    horizon = CharField(null=True)  # one of HORIZON_CODES, or None for legacy/custom
    reasoning = TextField()
    created_at = DateTimeField(default=_utcnow_naive)
    resolved_at = DateTimeField(null=True)
    outcome = TextField(null=True)
    brier_score = FloatField(null=True)


class EventLink(BaseModel):
    id = AutoField()
    event_a = ForeignKeyField(Event, backref="links_as_a")
    event_b = ForeignKeyField(Event, backref="links_as_b")
    relationship = CharField()    # caused_by|preceded_by|escalated_to|related_to
    notes = TextField(null=True)


class GeoContext(BaseModel):
    id = AutoField()
    event = ForeignKeyField(Event, backref="geo_contexts")
    alliances = TextField(null=True)
    treaties = TextField(null=True)
    sanctions = TextField(null=True)
    key_dates = TextField(null=True)
    strategic_interests = TextField(null=True)


ALL_MODELS = [
    Event, Actor, EventActor, Evidence, TechnicalAnalysis,
    Narrative, NarrativeSource, EscalationState, ScenarioNode,
    HistoricalPrecedent, ImpactAssessment, RawLeaf, Observation,
    AnalysisRun, Prediction, EventLink, GeoContext,
]


def _migrate_prediction_horizon():
    """Add horizon column to prediction table if missing (idempotent)."""
    cursor = database.execute_sql("PRAGMA table_info(prediction)")
    columns = {row[1] for row in cursor.fetchall()}
    if "horizon" not in columns:
        database.execute_sql("ALTER TABLE prediction ADD COLUMN horizon VARCHAR(255) NULL")


def _table_columns(table: str) -> set[str]:
    cursor = database.execute_sql(f'PRAGMA table_info("{table}")')
    return {row[1] for row in cursor.fetchall()}


def make_analysis_key(
    observation_dedupe_key: str,
    model_name: str,
    prompt_version: str,
    code_version: str,
) -> str:
    encoded = json.dumps(
        [
            "analysis-v1",
            observation_dedupe_key,
            model_name,
            prompt_version,
            code_version,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _backfill_analysis_keys():
    """Give one canonical legacy run per version a lease key; preserve retries."""
    rows = database.execute_sql(
        """
        SELECT ar.id, ar.observation_id, o.dedupe_key,
               ar.model_name, ar.prompt_version, ar.code_version,
               ar.status, ar.event_id, ar.completed_at, ar.created_at,
               ar.analysis_key
        FROM analysisrun AS ar
        JOIN observation AS o ON o.id = ar.observation_id
        ORDER BY ar.id
        """
    ).fetchall()
    groups = {}
    for row in rows:
        identity = (row[1], row[3] or "unknown", row[4] or "unknown", row[5] or "unknown")
        groups.setdefault(identity, []).append(row)

    def priority(row):
        completed_with_event = row[6] == "completed" and row[7] is not None
        return (
            completed_with_event,
            row[6] == "completed",
            row[6] == "running",
            row[8] or "",
            row[9] or "",
            row[0],
        )

    for group_rows in groups.values():
        canonical = max(group_rows, key=priority)
        expected_key = make_analysis_key(
            canonical[2],
            canonical[3] or "unknown",
            canonical[4] or "unknown",
            canonical[5] or "unknown",
        )
        database.execute_sql(
            "UPDATE analysisrun SET analysis_key = NULL "
            "WHERE analysis_key = ? AND id != ?",
            (expected_key, canonical[0]),
        )
        database.execute_sql(
            "UPDATE analysisrun SET analysis_key = ? WHERE id = ?",
            (expected_key, canonical[0]),
        )


def migrate_schema():
    """Apply additive migrations for databases created by earlier prototypes."""
    observation_columns = _table_columns("observation")
    if "supersedes_id" not in observation_columns:
        database.execute_sql(
            "ALTER TABLE observation ADD COLUMN supersedes_id INTEGER NULL "
            "REFERENCES observation(id)"
        )

    run_columns = _table_columns("analysisrun")
    additions = {
        "analysis_key": "VARCHAR(255) NULL",
        "claim_token": "VARCHAR(255) NULL",
        "attempt": "INTEGER NOT NULL DEFAULT 0",
        "error_stage": "VARCHAR(255) NULL",
    }
    for name, declaration in additions.items():
        if name not in run_columns:
            database.execute_sql(
                f'ALTER TABLE analysisrun ADD COLUMN "{name}" {declaration}'
            )
    _backfill_analysis_keys()
    database.execute_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS analysisrun_analysis_key_unique "
        "ON analysisrun(analysis_key)"
    )
    database.execute_sql(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _assert_supported_schema_version():
    current = database.execute_sql("PRAGMA user_version").fetchone()[0]
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema v{current} is newer than supported v{SCHEMA_VERSION}"
        )
    return current


def verify_database_integrity():
    """Fail startup on structural corruption or foreign-key violations."""
    quick_check = database.execute_sql("PRAGMA quick_check").fetchone()
    if quick_check is None or quick_check[0] != "ok":
        raise RuntimeError("SQLite quick_check failed")
    violations = database.execute_sql("PRAGMA foreign_key_check").fetchall()
    if violations:
        tables = ", ".join(sorted({str(row[0]) for row in violations}))
        raise RuntimeError(f"SQLite foreign-key violations found in: {tables}")


def _harden_database_permissions():
    """Keep newly created SQLite state private to the local user."""
    db_path = database.database
    if db_path == ":memory:":
        return
    for candidate in (db_path, f"{db_path}-wal", f"{db_path}-shm"):
        if os.path.exists(candidate):
            os.chmod(candidate, 0o600)


def _prepare_database_file():
    """Create the database file owner-only before SQLite opens it."""
    db_path = database.database
    if db_path == ":memory:":
        return
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    os.close(descriptor)


def install_observation_guards():
    """Enforce append-only observations below the ORM layer."""
    database.execute_sql(
        """
        CREATE TRIGGER IF NOT EXISTS observation_no_update
        BEFORE UPDATE ON observation
        BEGIN
            SELECT RAISE(ABORT, 'observations are immutable');
        END
        """
    )
    database.execute_sql(
        """
        CREATE TRIGGER IF NOT EXISTS observation_no_delete
        BEFORE DELETE ON observation
        BEGIN
            SELECT RAISE(ABORT, 'observations are append-only');
        END
        """
    )


def remove_observation_guards():
    """Remove append-only triggers for controlled schema teardown only."""
    database.execute_sql("DROP TRIGGER IF EXISTS observation_no_update")
    database.execute_sql("DROP TRIGGER IF EXISTS observation_no_delete")


def init_db():
    db_path = database.database
    dir_name = os.path.dirname(db_path)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True, mode=0o700)
    _prepare_database_file()
    database.connect(reuse_if_open=True)
    current_version = _assert_supported_schema_version()
    database.create_tables(ALL_MODELS, safe=True)
    # This structural check is cheap and repairs databases that were stamped
    # v2 by an interrupted/partial migration without repeating data backfills.
    _migrate_prediction_horizon()
    if current_version < SCHEMA_VERSION:
        migrate_schema()
    install_observation_guards()
    verify_database_integrity()
    _harden_database_permissions()
