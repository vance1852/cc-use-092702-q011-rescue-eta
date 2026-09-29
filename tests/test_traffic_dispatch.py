from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from collection_logistics.api import JsonApplication
from collection_logistics.clock import FrozenClock, arrival_after_minutes, utc_text
from collection_logistics.errors import Conflict, Forbidden, InvalidState, ValidationFailed
from collection_logistics.models import MAX_RESPONSE_MINUTES
from collection_logistics.planning import AllocationRequest, RiskPoint, allocate_capacity, latest_streak
from collection_logistics.service import CollectionLogisticsService
from collection_logistics.risk import DemandBucket, inventory_coverage, mark_to_risk, traffic_gap


class PlanningTests(unittest.TestCase):
    def test_latest_down_streak_uses_first_close_as_base(self) -> None:
        streak = latest_streak([
            RiskPoint("2026-09-18", Decimal("108")),
            RiskPoint("2026-09-19", Decimal("105")),
            RiskPoint("2026-09-20", Decimal("102")),
            RiskPoint("2026-09-21", Decimal("98")),
        ])
        self.assertEqual(streak.direction, "down")
        self.assertEqual(streak.sessions, 4)
        self.assertEqual(streak.start_date, "2026-09-18")
        self.assertEqual(streak.end_close, Decimal("98"))

    def test_allocation_is_stable_and_does_not_exceed_capacity(self) -> None:
        rows = allocate_capacity(Decimal("100"), [
            AllocationRequest("later", Decimal("80"), 20, "2026-09-24T09:00:00Z"),
            AllocationRequest("first", Decimal("70"), 10, "2026-09-24T10:00:00Z"),
        ])
        self.assertEqual(rows[0]["dispatch_id"], "first")
        self.assertEqual(rows[0]["allocated_units"], "70.000")
        self.assertEqual(rows[1]["allocated_units"], "30.000")

    def test_inventory_coverage_and_traffic_gap(self) -> None:
        coverage = inventory_coverage(
            [{"center_id": "receiving-vault", "preservation_resource_kind": "tow-truck", "available_units": "250"}],
            [DemandBucket("receiving-vault", "tow-truck", Decimal("100"), Decimal("20"))],
        )
        self.assertEqual(coverage[0]["coverage_days"], "2.30")
        self.assertTrue(coverage[0]["below_three_days"])
        gap = traffic_gap(
            opening_inventory=Decimal("100"),
            confirmed_inbound=Decimal("30"),
            forecast_demand=Decimal("120"),
            protected_reserve=Decimal("40"),
        )
        self.assertEqual(gap["traffic_gap"], "30.000")

    def test_mark_to_risk_groups_deterministically(self) -> None:
        result = mark_to_risk(
            [{"position_id": "p1", "risk_index": "HUMIDITY", "quantity_units": "100", "baseline_value": "105"}],
            {"HUMIDITY": Decimal("98")},
        )
        self.assertEqual(result["unrealized_pnl_cny"], "-700.00")


class CollectionLogisticsServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = CollectionLogisticsService(self.connection, self.clock)
        for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
            self.service.create_user(user_id, user_id, role)
        self.service.create_facility("plan", {"center_id": "collection-east", "name": "北部标本事件保藏中心", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_units": "500000"})
        self.service.create_facility("plan", {"center_id": "receiving-vault-b", "name": "沿海终端", "kind": "receiving-vault", "timezone": "Asia/Shanghai", "capacity_units": "800000"})
        self.service.create_route("plan", {"corridor_id": "transfer-east-1", "origin_center_id": "collection-east", "destination_center_id": "receiving-vault-b", "preservation_resource_kind": "preservation-box", "hourly_capacity": "100000", "delay_basis_points": 25, "response_minutes": 36})

    def tearDown(self) -> None:
        self.connection.close()

    def risk_record(self, day: int, close: str) -> dict[str, object]:
        return self.service.record_risk_record("plan", {"risk_index": "HUMIDITY", "duty_date": f"2026-09-{day}", "index_value": close, "source_revision": f"r-{day}", "observed_at": f"2026-09-{day}T21:00:00Z"})

    def test_risk_record_revisions_preserve_history(self) -> None:
        first = self.risk_record(23, "98")
        second = self.service.record_risk_record("plan", {"risk_index": "HUMIDITY", "duty_date": "2026-09-23", "index_value": "97.8", "source_revision": "r-23-corrected", "observed_at": "2026-09-23T22:00:00Z"})
        self.assertNotEqual(first["risk_record_id"], second["risk_record_id"])
        rows = self.connection.execute("SELECT * FROM risk_index_risk_records ORDER BY risk_record_id").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["supersedes_risk_record_id"], rows[0]["risk_record_id"])

    def test_dispatch_request_replay_and_payload_conflict(self) -> None:
        payload = {"dispatch_id": "nom-1", "corridor_id": "transfer-east-1", "specimen_event_id": "herbarium-room", "duty_date": "2026-09-25", "requested_units": "80000", "priority": 10, "idempotency_key": "key-1"}
        first = self.service.submit_dispatch("dispatch", payload)
        self.assertEqual(first, self.service.submit_dispatch("dispatch", payload))
        changed = dict(payload, requested_units="81000")
        with self.assertRaises(Conflict):
            self.service.submit_dispatch("dispatch", changed)

    def test_outage_reduces_allocation_and_deployment_consumes_inventory(self) -> None:
        self.service.announce_restriction("risk", "transfer-east-1", "2026-09-25T00:00:00Z", "2026-09-25T23:59:59Z", "50", "检修")
        for number, requested, priority in ((1, "40000", 10), (2, "30000", 20)):
            self.service.submit_dispatch("dispatch", {"dispatch_id": f"nom-{number}", "corridor_id": "transfer-east-1", "specimen_event_id": f"specimen_event-{number}", "duty_date": "2026-09-25", "requested_units": requested, "priority": priority, "idempotency_key": f"key-{number}"})
        allocation = self.service.allocate("dispatch", "transfer-east-1", "2026-09-25")
        self.assertEqual(allocation["available_units"], "50000.000")
        self.assertEqual(allocation["allocations"][1]["allocated_units"], "10000.000")
        self.service.add_inventory_lot("dispatch", {"preservation_resource_lot_id": "lot-1", "center_id": "collection-east", "preservation_resource_kind": "preservation-box", "grade": "HUMIDITY", "quantity_units": "60000", "unit_cost_cny": "91", "received_at": "2026-09-24T06:00:00Z"})
        deployment = self.service.dispatch_deployment("dispatch", "deployment-1", "nom-1", "lot-1", 2)
        self.assertEqual(deployment["deployed_units"], "40000.000")
        self.assertEqual(self.service.inventory_lot("lot-1")["available_units"], "20000.000")

    def test_scenario_is_approved_and_replayed_by_input(self) -> None:
        self.risk_record(23, "98")
        self.service.add_inventory_lot("dispatch", {"preservation_resource_lot_id": "lot-1", "center_id": "collection-east", "preservation_resource_kind": "preservation-box", "grade": "HUMIDITY", "quantity_units": "60000", "unit_cost_cny": "91", "received_at": "2026-09-24T06:00:00Z"})
        self.service.create_scenario("plan", {"scenario_id": "restart", "name": "库房环境恢复", "risk_index_drop_percent": "9", "route_capacity_changes": {"transfer-east-1": "20"}, "demand_changes": {"collection-east:preservation-box": "-5"}})
        with self.assertRaises(Forbidden):
            self.service.approve_scenario("plan", "restart", 1)
        self.service.approve_scenario("risk", "restart", 1)
        first = self.service.run_scenario("plan", "restart", "2026-09-23")
        second = self.service.run_scenario("plan", "restart", "2026-09-23")
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["run_id"], second["run_id"])

    def test_audit_chain_detects_tampering(self) -> None:
        self.assertTrue(self.service.audit_chain("audit")["valid"])
        self.connection.execute("UPDATE traffic_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("audit")["valid"])

    def test_api_exposes_browser_free_boundary(self) -> None:
        app = JsonApplication(self.service)
        self.assertEqual(app.handle("GET", "/health").status, 200)
        response = app.handle("GET", "/risk_records/summary/HUMIDITY", {"X-Actor-Id": "plan"})
        self.assertEqual(response.status, 404)
        self.assertEqual(response.body["error"]["code"], "not_found")


class ResponseMinutesRegistrationTests(unittest.TestCase):
    """登记口径：response_minutes 一律为分钟，写入前排除零值、负值和超长值。"""

    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.service = CollectionLogisticsService(
            self.connection, FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        )
        self.service.create_user("plan", "plan", "planner")
        self.service.create_facility("plan", {"center_id": "c-a", "name": "甲站", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_units": "100"})
        self.service.create_facility("plan", {"center_id": "c-b", "name": "乙站", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_units": "100"})

    def tearDown(self) -> None:
        self.connection.close()

    def route_payload(self, corridor_id: str, response_minutes: object) -> dict[str, object]:
        return {
            "corridor_id": corridor_id,
            "origin_center_id": "c-a",
            "destination_center_id": "c-b",
            "preservation_resource_kind": "ambulance",
            "hourly_capacity": "10",
            "delay_basis_points": 0,
            "response_minutes": response_minutes,
        }

    def test_rejects_zero_negative_and_unreasonably_long_durations(self) -> None:
        for index, bad in enumerate((0, -45, MAX_RESPONSE_MINUTES + 1, True, 45.5, "45")):
            with self.assertRaises(ValidationFailed, msg=f"response_minutes={bad!r} 应被拒绝"):
                self.service.create_route("plan", self.route_payload(f"r-{index}", bad))

    def test_accepts_minutes_and_marks_unit_explicitly(self) -> None:
        route = self.service.create_route("plan", self.route_payload("r-ok", 45))
        self.assertEqual(route["response_minutes"], 45)
        self.assertEqual(route["duration_unit"], "minutes")
        self.assertEqual(route["response_minutes_unit"], "minutes")
        self.assertTrue(route["duration_confirmed"])
        self.assertFalse(route["duration_pending_confirmation"])
        boundary = self.service.create_route("plan", self.route_payload("r-max", MAX_RESPONSE_MINUTES))
        self.assertEqual(boundary["response_minutes"], MAX_RESPONSE_MINUTES)


class ArrivalAfterMinutesTests(unittest.TestCase):
    """到达时刻：从带时区的实际出发时间增加分钟，跨日与夏令时结果确定。"""

    def test_forty_five_minutes_stays_forty_five_minutes(self) -> None:
        departed = datetime(2026, 9, 28, 22, 15, tzinfo=timezone.utc)
        self.assertEqual(utc_text(arrival_after_minutes(departed, 45)), "2026-09-28T23:00:00Z")

    def test_crosses_midnight_into_next_day(self) -> None:
        departed = datetime(2026, 9, 30, 23, 50, tzinfo=timezone.utc)
        self.assertEqual(utc_text(arrival_after_minutes(departed, 45)), "2026-10-01T00:35:00Z")

    def test_dst_spring_forward_is_deterministic(self) -> None:
        # 纽约 2026-03-08 02:00 拨快一小时；按绝对时刻加 45 分钟结果唯一。
        departed = datetime(2026, 3, 8, 1, 30, tzinfo=ZoneInfo("America/New_York"))
        self.assertEqual(utc_text(arrival_after_minutes(departed, 45)), "2026-03-08T07:15:00Z")

    def test_dst_fall_back_is_deterministic(self) -> None:
        # 纽约 2026-11-01 02:00 拨回一小时；按绝对时刻加 120 分钟结果唯一。
        departed = datetime(2026, 11, 1, 1, 30, tzinfo=ZoneInfo("America/New_York"))
        self.assertEqual(utc_text(arrival_after_minutes(departed, 120)), "2026-11-01T07:30:00Z")

    def test_rejects_naive_departure_and_non_positive_minutes(self) -> None:
        with self.assertRaises(ValueError):
            arrival_after_minutes(datetime(2026, 9, 28, 22, 15), 45)
        with self.assertRaises(ValueError):
            arrival_after_minutes(datetime(2026, 9, 28, 22, 15, tzinfo=timezone.utc), 0)


class LegacyDurationMigrationTests(unittest.TestCase):
    """无法证明单位的旧数据：迁移后待人工确认，确认前不得自动参与调度。"""

    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        # 模拟升级前的旧库：路线表没有单位溯源列。
        self.connection.executescript(
            """
            CREATE TABLE traffic_users (
                user_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                role TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE response_centers (
                center_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                timezone TEXT NOT NULL,
                capacity_units TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE road_corridors (
                corridor_id TEXT PRIMARY KEY,
                origin_center_id TEXT NOT NULL,
                destination_center_id TEXT NOT NULL,
                preservation_resource_kind TEXT NOT NULL,
                hourly_capacity TEXT NOT NULL,
                delay_basis_points INTEGER NOT NULL,
                response_minutes INTEGER NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1,
                state TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL
            );
            INSERT INTO traffic_users VALUES('plan','plan','planner',1,'2026-09-01T00:00:00Z');
            INSERT INTO traffic_users VALUES('dispatch','dispatch','dispatcher',1,'2026-09-01T00:00:00Z');
            INSERT INTO response_centers VALUES('c-a','甲站','storage','Asia/Shanghai','100',1,'2026-09-01T00:00:00Z');
            INSERT INTO response_centers VALUES('c-b','乙站','storage','Asia/Shanghai','100',1,'2026-09-01T00:00:00Z');
            INSERT INTO road_corridors(corridor_id,origin_center_id,destination_center_id,
                preservation_resource_kind,hourly_capacity,delay_basis_points,response_minutes,created_at)
            VALUES('legacy-1','c-a','c-b','ambulance','10',0,45,'2026-09-01T00:00:00Z');
            """
        )
        self.service = CollectionLogisticsService(
            self.connection, FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        )

    def tearDown(self) -> None:
        self.connection.close()

    def test_migration_marks_legacy_routes_pending_confirmation(self) -> None:
        route = self.service.route("legacy-1")
        self.assertEqual(route["duration_unit"], "minutes_legacy_unknown")
        self.assertTrue(route["duration_pending_confirmation"])
        self.assertFalse(route["duration_confirmed"])

    def test_legacy_route_is_excluded_from_dispatch_until_confirmed(self) -> None:
        payload = {"dispatch_id": "nom-legacy", "corridor_id": "legacy-1", "specimen_event_id": "ward-1", "duty_date": "2026-09-25", "requested_units": "5", "priority": 10, "idempotency_key": "key-legacy"}
        with self.assertRaises(InvalidState):
            self.service.submit_dispatch("dispatch", payload)
        with self.assertRaises(InvalidState):
            self.service.allocate("dispatch", "legacy-1", "2026-09-25")

    def test_manual_confirmation_restores_dispatch_and_can_correct_value(self) -> None:
        confirmed = self.service.confirm_route_duration("plan", "legacy-1", 45)
        self.assertEqual(confirmed["duration_unit"], "minutes")
        self.assertTrue(confirmed["duration_confirmed"])
        self.assertEqual(confirmed["response_minutes"], 45)
        with self.assertRaises(Conflict):
            self.service.confirm_route_duration("plan", "legacy-1")
        submitted = self.service.submit_dispatch("dispatch", {"dispatch_id": "nom-ok", "corridor_id": "legacy-1", "specimen_event_id": "ward-1", "duty_date": "2026-09-25", "requested_units": "5", "priority": 10, "idempotency_key": "key-ok"})
        self.assertEqual(submitted["state"], "submitted")

    def test_confirmation_rejects_invalid_replacement_values(self) -> None:
        for bad in (0, -1, MAX_RESPONSE_MINUTES + 1, 45.5, "45"):
            with self.assertRaises(ValidationFailed, msg=f"response_minutes={bad!r} 应被拒绝"):
                self.service.confirm_route_duration("plan", "legacy-1", bad)
        # 校验失败不得改变待确认状态。
        self.assertTrue(self.service.route("legacy-1")["duration_pending_confirmation"])


class DurationInterpretationConsistencyTests(unittest.TestCase):
    """路线接口、任务执行结果、审计摘要和历史读取必须使用同一分钟解释。"""

    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 28, 22, 15, tzinfo=timezone.utc))
        self.service = CollectionLogisticsService(self.connection, self.clock)
        for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("audit", "auditor")):
            self.service.create_user(user_id, user_id, role)
        self.service.create_facility("plan", {"center_id": "c-a", "name": "甲站", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_units": "100"})
        self.service.create_facility("plan", {"center_id": "c-b", "name": "乙站", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_units": "100"})
        self.service.create_route("plan", {"corridor_id": "r-45", "origin_center_id": "c-a", "destination_center_id": "c-b", "preservation_resource_kind": "ambulance", "hourly_capacity": "100", "delay_basis_points": 0, "response_minutes": 45})
        self.service.add_inventory_lot("dispatch", {"preservation_resource_lot_id": "lot-1", "center_id": "c-a", "preservation_resource_kind": "ambulance", "grade": "A", "quantity_units": "100", "unit_cost_cny": "1", "received_at": "2026-09-28T00:00:00Z"})
        self.service.submit_dispatch("dispatch", {"dispatch_id": "nom-1", "corridor_id": "r-45", "specimen_event_id": "ward-1", "duty_date": "2026-09-28", "requested_units": "10", "priority": 10, "idempotency_key": "key-1"})
        self.service.allocate("dispatch", "r-45", "2026-09-28")

    def tearDown(self) -> None:
        self.connection.close()

    def test_deployment_result_uses_minutes_not_hours(self) -> None:
        deployment = self.service.dispatch_deployment("dispatch", "dep-1", "nom-1", "lot-1", 2)
        # 22:15 UTC 出发，45 分钟后到达，而不是 45 小时后。
        self.assertEqual(deployment["expected_arrival"], "2026-09-28T23:00:00Z")
        self.assertEqual(deployment["response_minutes"], 45)
        self.assertEqual(deployment["response_minutes_unit"], "minutes")

    def test_history_read_matches_dispatch_result(self) -> None:
        deployment = self.service.dispatch_deployment("dispatch", "dep-1", "nom-1", "lot-1", 2)
        history = self.service.deployment("dep-1")
        self.assertEqual(history["expected_arrival"], deployment["expected_arrival"])
        self.assertEqual(history["response_minutes"], 45)
        self.assertEqual(history["response_minutes_unit"], "minutes")
        self.assertTrue(history["duration_confirmed"])

    def test_audit_summary_uses_same_minute_interpretation(self) -> None:
        self.service.dispatch_deployment("dispatch", "dep-1", "nom-1", "lot-1", 2)
        summary = self.service.audit_summary("audit")
        self.assertEqual(summary["duration_unit"], "minutes")
        dispatched = [e for e in summary["duration_events"] if e["event_type"] == "deployment.dispatched"]
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(dispatched[0]["response_minutes"], 45)
        self.assertEqual(dispatched[0]["response_minutes_unit"], "minutes")
        self.assertGreaterEqual(summary["counts"]["route.created"], 1)

    def test_api_exposes_confirmation_history_and_summary(self) -> None:
        app = JsonApplication(self.service)
        headers = {"X-Actor-Id": "plan"}
        # 新登记路线已是分钟口径，确认接口应拒绝重复确认。
        duplicate = app.handle("POST", "/road_corridors/r-45/confirm_duration", headers, b"{}")
        self.assertEqual(duplicate.status, 409)
        self.service.dispatch_deployment("dispatch", "dep-1", "nom-1", "lot-1", 2)
        history = app.handle("GET", "/deployments/history/dep-1", headers)
        self.assertEqual(history.status, 200)
        self.assertEqual(history.body["expected_arrival"], "2026-09-28T23:00:00Z")
        self.assertEqual(history.body["response_minutes_unit"], "minutes")
        summary = app.handle("GET", "/audit/summary", {"X-Actor-Id": "audit"})
        self.assertEqual(summary.status, 200)
        self.assertEqual(summary.body["duration_unit"], "minutes")


if __name__ == "__main__":
    unittest.main()
