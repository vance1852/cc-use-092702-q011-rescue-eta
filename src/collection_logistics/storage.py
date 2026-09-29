"""标本事件快处服务的 SQLite 模式和事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS traffic_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('planner','dispatcher','risk','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_index_risk_records (
    risk_record_id INTEGER PRIMARY KEY AUTOINCREMENT,
    risk_index TEXT NOT NULL,
    duty_date TEXT NOT NULL,
    index_value TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    supersedes_risk_record_id INTEGER REFERENCES risk_index_risk_records(risk_record_id),
    recorded_by TEXT NOT NULL REFERENCES traffic_users(user_id),
    recorded_at TEXT NOT NULL,
    UNIQUE(risk_index, duty_date, source_revision)
);

CREATE INDEX IF NOT EXISTS idx_risk_records_series
ON risk_index_risk_records(risk_index, duty_date, risk_record_id);

CREATE TABLE IF NOT EXISTS response_centers (
    center_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    timezone TEXT NOT NULL,
    capacity_units TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS road_corridors (
    corridor_id TEXT PRIMARY KEY,
    origin_center_id TEXT NOT NULL REFERENCES response_centers(center_id),
    destination_center_id TEXT NOT NULL REFERENCES response_centers(center_id),
    preservation_resource_kind TEXT NOT NULL,
    hourly_capacity TEXT NOT NULL,
    delay_basis_points INTEGER NOT NULL,
    duration_minutes INTEGER,
    legacy_duration_value INTEGER,
    duration_unit TEXT NOT NULL DEFAULT 'minute'
        CHECK(duration_unit IN ('minute','unknown')),
    review_status TEXT NOT NULL DEFAULT 'confirmed'
        CHECK(review_status IN ('confirmed','pending_review')),
    revision INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','suspended','retired')),
    created_at TEXT NOT NULL,
    CHECK(origin_center_id <> destination_center_id),
    CHECK(duration_minutes IS NULL OR (duration_minutes > 0 AND duration_minutes <= 4320)),
    CHECK(
        (duration_unit = 'minute' AND review_status = 'confirmed' AND duration_minutes IS NOT NULL)
        OR
        (duration_unit = 'unknown' AND review_status = 'pending_review' AND duration_minutes IS NULL)
    )
);

CREATE TABLE IF NOT EXISTS corridor_restrictions (
    restriction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    corridor_id TEXT NOT NULL REFERENCES road_corridors(corridor_id),
    starts_at TEXT NOT NULL,
    ends_at TEXT,
    capacity_percent TEXT NOT NULL,
    reason TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'announced' CHECK(state IN ('announced','active','closed','cancelled')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES traffic_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_outages_route_time
ON corridor_restrictions(corridor_id, starts_at, ends_at);

CREATE TABLE IF NOT EXISTS preservation_resource_lots (
    preservation_resource_lot_id TEXT PRIMARY KEY,
    center_id TEXT NOT NULL REFERENCES response_centers(center_id),
    preservation_resource_kind TEXT NOT NULL,
    grade TEXT NOT NULL,
    quantity_units TEXT NOT NULL,
    available_units TEXT NOT NULL,
    unit_cost_cny TEXT NOT NULL,
    received_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES traffic_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_inventory_available
ON preservation_resource_lots(center_id, preservation_resource_kind, received_at);

CREATE TABLE IF NOT EXISTS preservation_resource_adjustments (
    adjustment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    preservation_resource_lot_id TEXT NOT NULL REFERENCES preservation_resource_lots(preservation_resource_lot_id),
    delta_units TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    note TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL REFERENCES traffic_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dispatch_requests (
    dispatch_id TEXT PRIMARY KEY,
    corridor_id TEXT NOT NULL REFERENCES road_corridors(corridor_id),
    specimen_event_id TEXT NOT NULL,
    duty_date TEXT NOT NULL,
    requested_units TEXT NOT NULL,
    allocated_units TEXT NOT NULL DEFAULT '0',
    arrived_units TEXT NOT NULL DEFAULT '0',
    priority INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'submitted'
        CHECK(state IN ('submitted','allocated','in_transit','delivered','cancelled')),
    revision INTEGER NOT NULL DEFAULT 1,
    idempotency_key TEXT NOT NULL UNIQUE,
    submitted_by TEXT NOT NULL REFERENCES traffic_users(user_id),
    submitted_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dispatch_requests_schedule
ON dispatch_requests(corridor_id, duty_date, priority, submitted_at);

CREATE TABLE IF NOT EXISTS dispatch_plans (
    plan_id INTEGER PRIMARY KEY AUTOINCREMENT,
    corridor_id TEXT NOT NULL REFERENCES road_corridors(corridor_id),
    duty_date TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    available_units TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES traffic_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(corridor_id, duty_date, input_sha256)
);

CREATE TABLE IF NOT EXISTS deployments (
    deployment_id TEXT PRIMARY KEY,
    dispatch_id TEXT NOT NULL UNIQUE REFERENCES dispatch_requests(dispatch_id),
    inventory_preservation_resource_lot_id TEXT NOT NULL REFERENCES preservation_resource_lots(preservation_resource_lot_id),
    deployed_units TEXT NOT NULL,
    expected_arrived_units TEXT NOT NULL,
    departed_at TEXT NOT NULL,
    arrived_at TEXT,
    state TEXT NOT NULL DEFAULT 'in_transit' CHECK(state IN ('in_transit','delivered','disputed')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES traffic_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS response_scenarios (
    scenario_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN ('draft','approved','retired')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES traffic_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS response_scenario_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_id TEXT NOT NULL REFERENCES response_scenarios(scenario_id),
    as_of_date TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES traffic_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(scenario_id, as_of_date, input_sha256)
);

CREATE TABLE IF NOT EXISTS traffic_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS traffic_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_traffic_audit_entity
ON traffic_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)
    _migrate_legacy_duration(connection)


def _migrate_legacy_duration(connection: sqlite3.Connection) -> None:
    """把以 response_minutes 存储、无法证明单位的旧路线标为待人工确认。

    旧值原样保留在 legacy_duration_value 中，duration_minutes 置空，调度链路
    因此无法再自动使用这些行；人工确认提供正确的分钟值后才会重新参与调度。
    """
    columns = {row[1] for row in connection.execute("PRAGMA table_info(road_corridors)")}
    if not columns or "response_minutes" not in columns:
        return
    migrated_ddl = """
        CREATE TABLE road_corridors_migrated (
            corridor_id TEXT PRIMARY KEY,
            origin_center_id TEXT NOT NULL REFERENCES response_centers(center_id),
            destination_center_id TEXT NOT NULL REFERENCES response_centers(center_id),
            preservation_resource_kind TEXT NOT NULL,
            hourly_capacity TEXT NOT NULL,
            delay_basis_points INTEGER NOT NULL,
            duration_minutes INTEGER,
            legacy_duration_value INTEGER,
            duration_unit TEXT NOT NULL DEFAULT 'minute'
                CHECK(duration_unit IN ('minute','unknown')),
            review_status TEXT NOT NULL DEFAULT 'confirmed'
                CHECK(review_status IN ('confirmed','pending_review')),
            revision INTEGER NOT NULL DEFAULT 1,
            state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','suspended','retired')),
            created_at TEXT NOT NULL,
            CHECK(origin_center_id <> destination_center_id),
            CHECK(duration_minutes IS NULL OR (duration_minutes > 0 AND duration_minutes <= 4320)),
            CHECK(
                (duration_unit = 'minute' AND review_status = 'confirmed' AND duration_minutes IS NOT NULL)
                OR
                (duration_unit = 'unknown' AND review_status = 'pending_review' AND duration_minutes IS NULL)
            )
        );
    """
    foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    connection.execute("PRAGMA foreign_keys=OFF")
    try:
        connection.executescript(
            migrated_ddl
            + """
        INSERT INTO road_corridors_migrated(
            corridor_id,origin_center_id,destination_center_id,preservation_resource_kind,hourly_capacity,
            delay_basis_points,duration_minutes,legacy_duration_value,duration_unit,review_status,
            revision,state,created_at
        )
        SELECT corridor_id,origin_center_id,destination_center_id,preservation_resource_kind,hourly_capacity,
               delay_basis_points,NULL,response_minutes,'unknown','pending_review',
               revision,state,created_at
        FROM road_corridors;

        DROP TABLE road_corridors;

        ALTER TABLE road_corridors_migrated RENAME TO road_corridors;
        """
        )
    finally:
        connection.execute(f"PRAGMA foreign_keys={'ON' if foreign_keys else 'OFF'}")
        # 改名后让外键约束重新指向现存的 road_corridors 表。
        connection.execute("PRAGMA foreign_key_check")


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def row_dict(row: sqlite3.Row | None) -> dict[str, object] | None:
    return None if row is None else dict(row)
