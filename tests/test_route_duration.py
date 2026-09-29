from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from collection_logistics.clock import FrozenClock, arrival_after_minutes
from collection_logistics.errors import InvalidState, ValidationFailed
from collection_logistics.service import CollectionLogisticsService
from collection_logistics.storage import connect


def build_service(clock=None):
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = CollectionLogisticsService(
        connection,
        clock or FrozenClock(datetime(2026, 9, 24, 23, 40, tzinfo=timezone.utc)),
    )
    for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
        service.create_user(user_id, user_id, role)
    service.create_facility("plan", {"center_id": "c1", "name": "东保护站", "kind": "patrol-station", "timezone": "Asia/Shanghai", "capacity_units": "10"})
    service.create_facility("plan", {"center_id": "c2", "name": "西接应点", "kind": "patrol-station", "timezone": "Asia/Shanghai", "capacity_units": "10"})
    return service, connection


def route_payload(corridor_id: str, minutes: object) -> dict[str, object]:
    return {
        "corridor_id": corridor_id,
        "origin_center_id": "c1",
        "destination_center_id": "c2",
        "preservation_resource_kind": "ambulance",
        "hourly_capacity": "5",
        "delay_basis_points": 0,
        "duration_minutes": minutes,
    }


class ArrivalClockTests(unittest.TestCase):
    def test_minutes_added_to_timezone_aware_departure(self) -> None:
        departed = datetime(2026, 9, 24, 23, 40, tzinfo=timezone.utc)
        arrival = arrival_after_minutes(departed, 45)
        self.assertEqual(arrival, datetime(2026, 9, 25, 0, 25, tzinfo=timezone.utc))

    def test_spring_forward_gap_is_deterministic(self) -> None:
        departed = datetime(2026, 3, 8, 1, 45, tzinfo=ZoneInfo("America/New_York"))
        arrival = arrival_after_minutes(departed, 45)
        self.assertEqual(arrival.astimezone(timezone.utc), datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc))
        self.assertEqual(
            arrival.astimezone(ZoneInfo("America/New_York")).isoformat(),
            "2026-03-08T03:30:00-04:00",
        )

    def test_fall_back_overlap_is_deterministic(self) -> None:
        departed = datetime(2026, 11, 1, 0, 45, fold=0, tzinfo=ZoneInfo("America/New_York"))
        arrival = arrival_after_minutes(departed, 45)
        self.assertEqual(arrival.astimezone(timezone.utc), datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc))

    def test_naive_departure_rejected(self) -> None:
        with self.assertRaises(ValueError):
            arrival_after_minutes(datetime(2026, 9, 24, 8, 0), 45)


class DurationRegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service, self.connection = build_service()

    def tearDown(self) -> None:
        self.connection.close()

    def test_zero_negative_excessive_and_non_integer_rejected(self) -> None:
        for bad in (0, -45, 4321, True, "45", 45.0, None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationFailed):
                    self.service.create_route("plan", route_payload(f"bad-{bad!r}", bad))

    def test_boundary_minutes_accepted(self) -> None:
        view = self.service.create_route("plan", route_payload("longest", 4320))
        self.assertEqual(view["duration_minutes"], 4320)
        self.assertEqual(view["duration_unit"], "minute")
        self.assertEqual(view["review_status"], "confirmed")
        self.assertFalse(view["duration_pending_review"])


class DeploymentArrivalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service, self.connection = build_service()
        self.service.create_route("plan", route_payload("r1", 45))
        self.service.submit_dispatch("dispatch", {
            "dispatch_id": "d1", "corridor_id": "r1", "specimen_event_id": "s1",
            "duty_date": "2026-09-25", "requested_units": "2", "priority": 10, "idempotency_key": "k1",
        })
        self.service.allocate("dispatch", "r1", "2026-09-25")
        self.service.add_inventory_lot("dispatch", {
            "preservation_resource_lot_id": "lot1", "center_id": "c1",
            "preservation_resource_kind": "ambulance", "grade": "A",
            "quantity_units": "5", "unit_cost_cny": "1", "received_at": "2026-09-24T00:00:00Z",
        })

    def tearDown(self) -> None:
        self.connection.close()

    def test_expected_arrival_uses_minutes_not_hours(self) -> None:
        deployment = self.service.dispatch_deployment("dispatch", "dep1", "d1", "lot1", 2)
        self.assertEqual(deployment["departed_at"], "2026-09-24T23:40:00Z")
        self.assertEqual(deployment["expected_arrival"], "2026-09-25T00:25:00Z")
        self.assertEqual(deployment["duration_minutes"], 45)
        self.assertEqual(deployment["duration_unit"], "minute")


LEGACY_SCHEMA = """
CREATE TABLE traffic_users(user_id TEXT PRIMARY KEY,display_name TEXT NOT NULL,role TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL);
CREATE TABLE response_centers(center_id TEXT PRIMARY KEY,name TEXT NOT NULL,kind TEXT NOT NULL,
    timezone TEXT NOT NULL,capacity_units TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL);
CREATE TABLE road_corridors(corridor_id TEXT PRIMARY KEY,origin_center_id TEXT NOT NULL,
    destination_center_id TEXT NOT NULL,preservation_resource_kind TEXT NOT NULL,hourly_capacity TEXT NOT NULL,
    delay_basis_points INTEGER NOT NULL,response_minutes INTEGER NOT NULL,revision INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'active',created_at TEXT NOT NULL);
INSERT INTO traffic_users VALUES('plan','plan','planner',1,'2026-01-01T00:00:00Z');
INSERT INTO traffic_users VALUES('dispatch','dispatch','dispatcher',1,'2026-01-01T00:00:00Z');
INSERT INTO traffic_users VALUES('risk','risk','risk',1,'2026-01-01T00:00:00Z');
INSERT INTO traffic_users VALUES('audit','audit','auditor',1,'2026-01-01T00:00:00Z');
INSERT INTO response_centers VALUES('c1','东保护站','patrol-station','Asia/Shanghai','10',1,'2026-01-01T00:00:00Z');
INSERT INTO response_centers VALUES('c2','西接应点','patrol-station','Asia/Shanghai','10',1,'2026-01-01T00:00:00Z');
INSERT INTO road_corridors VALUES('old','c1','c2','ambulance','5',0,45,1,'active','2026-01-01T00:00:00Z');
"""


class LegacyDurationMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "legacy.sqlite3"
        seeded = sqlite3.connect(self.db_path)
        seeded.executescript(LEGACY_SCHEMA)
        seeded.commit()
        seeded.close()
        self.connection = connect(self.db_path)
        self.service = CollectionLogisticsService(
            self.connection, FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        )

    def tearDown(self) -> None:
        self.connection.close()
        self.temp.cleanup()

    def test_legacy_route_marked_pending_review(self) -> None:
        view = self.service.route("old")
        self.assertEqual(view["duration_unit"], "unknown")
        self.assertEqual(view["review_status"], "pending_review")
        self.assertTrue(view["duration_pending_review"])
        self.assertIsNone(view["duration_minutes"])
        self.assertEqual(view["legacy_duration_value"], 45)
        self.assertEqual(self.service.routes_pending_review("dispatch")["count"], 1)

    def test_pending_route_blocked_from_scheduling(self) -> None:
        with self.assertRaises(InvalidState):
            self.service.submit_dispatch("dispatch", {
                "dispatch_id": "d9", "corridor_id": "old", "specimen_event_id": "s9",
                "duty_date": "2026-09-25", "requested_units": "1", "priority": 10, "idempotency_key": "k9",
            })
        with self.assertRaises(InvalidState):
            self.service.allocate("dispatch", "old", "2026-09-25")

    def test_manual_confirmation_restores_scheduling(self) -> None:
        confirmed = self.service.confirm_route_duration(
            "plan", "old", {"duration_minutes": 45, "note": "夜巡核实为 45 分钟"}
        )
        self.assertEqual(confirmed["duration_minutes"], 45)
        self.assertEqual(confirmed["duration_unit"], "minute")
        self.assertEqual(confirmed["review_status"], "confirmed")
        self.assertEqual(self.service.routes_pending_review("dispatch")["count"], 0)
        self.service.submit_dispatch("dispatch", {
            "dispatch_id": "d9", "corridor_id": "old", "specimen_event_id": "s9",
            "duty_date": "2026-09-25", "requested_units": "1", "priority": 10, "idempotency_key": "k9",
        })

    def test_confirmation_rejects_unreasonable_minutes(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.confirm_route_duration("plan", "old", {"duration_minutes": 4321})
        with self.assertRaises(ValidationFailed):
            self.service.confirm_route_duration("plan", "old", {"duration_minutes": 0})
        # 确认失败后仍处于待确认状态
        self.assertTrue(self.service.route("old")["duration_pending_review"])


if __name__ == "__main__":
    unittest.main()
