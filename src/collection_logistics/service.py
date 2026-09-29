"""风险指数、应急资源库存、转运路线和调度申请的事务用例。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .clock import SystemClock, arrival_after_minutes, parse_utc, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import (
    RiskIndexRecord,
    ResponseCenter,
    PreservationResourceLot,
    DispatchRequest,
    RoadCorridor,
    ResponseScenario,
    response_minutes_value,
)
from .planning import (
    AllocationRequest,
    RiskPoint,
    allocate_capacity,
    canonical_json,
    decimal_text,
    delivered_after_loss,
    digest,
    effective_capacity,
    latest_streak,
    moving_average,
    quantize_volume,
    scenario_projection,
    weighted_inventory_cost,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "planner": {"risk_record.write", "catalog.write", "scenario.write", "scenario.run"},
    "dispatcher": {"dispatch_request.write", "allocation.run", "deployment.write", "inventory.write"},
    "risk": {"outage.write", "scenario.approve", "report.read"},
    "auditor": {"report.read", "audit.read"},
}

# 路线响应时长全平台唯一业务口径：分钟。
DURATION_UNIT_MINUTES = "minutes"
# 存量数据无法证明单位：只能展示为待人工确认，禁止自动参与调度。
DURATION_UNIT_LEGACY_UNKNOWN = "minutes_legacy_unknown"


def route_view(row: sqlite3.Row) -> dict[str, Any]:
    """路线读取的统一解释：显式携带时长单位与确认状态。"""
    data = dict(row)
    unit = row["duration_unit"]
    data["duration_unit"] = unit
    data["response_minutes_unit"] = DURATION_UNIT_MINUTES
    data["duration_confirmed"] = unit == DURATION_UNIT_MINUTES
    data["duration_pending_confirmation"] = unit == DURATION_UNIT_LEGACY_UNKNOWN
    return data


class CollectionLogisticsService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM traffic_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM traffic_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO traffic_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO traffic_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    def record_risk_record(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "risk_record.write")
        risk_record = RiskIndexRecord.from_dict(raw)
        previous = self.connection.execute(
            "SELECT risk_record_id,source_revision FROM risk_index_risk_records WHERE risk_index=? AND duty_date=? "
            "ORDER BY risk_record_id DESC LIMIT 1",
            (risk_record.risk_index, risk_record.duty_date),
        ).fetchone()
        if previous is not None and previous["source_revision"] == risk_record.source_revision:
            raise Conflict("同一来源修订已登记")
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO risk_index_risk_records(risk_index,duty_date,index_value,source_revision,observed_at,"
                    "supersedes_risk_record_id,recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        risk_record.risk_index,
                        risk_record.duty_date,
                        decimal_text(risk_record.index_value),
                        risk_record.source_revision,
                        risk_record.observed_at,
                        None if previous is None else previous["risk_record_id"],
                        actor_id,
                        self._now(),
                    ),
                )
                risk_record_id = int(cursor.lastrowid)
                self._audit(
                    "risk_record",
                    str(risk_record_id),
                    "risk_record.recorded",
                    actor_id,
                    {"risk_index": risk_record.risk_index, "duty_date": risk_record.duty_date},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("风险指数版本冲突") from exc
        return {"risk_record_id": risk_record_id, "risk_index": risk_record.risk_index, "duty_date": risk_record.duty_date}

    def risk_summary(self, risk_index: str, sessions: int = 20) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT q.duty_date,q.index_value FROM risk_index_risk_records q "
            "JOIN (SELECT duty_date,max(risk_record_id) risk_record_id FROM risk_index_risk_records "
            "WHERE risk_index=? GROUP BY duty_date) latest ON latest.risk_record_id=q.risk_record_id "
            "ORDER BY q.duty_date DESC LIMIT ?",
            (risk_index.upper(), sessions),
        ).fetchall()
        points = [RiskPoint(row["duty_date"], Decimal(row["index_value"])) for row in rows]
        if not points:
            raise NotFound("没有基准风险指数")
        streak = latest_streak(points)
        average = moving_average(points, min(5, len(points)))
        latest = max(points, key=lambda item: item.duty_date)
        return {
            "risk_index": risk_index.upper(),
            "latest": {"duty_date": latest.duty_date, "index_value": decimal_text(latest.close)},
            "latest_streak": None if streak is None else streak.as_dict(),
            "moving_average": None if average is None else decimal_text(average),
            "evidence_items": len(points),
        }

    def create_facility(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        facility = ResponseCenter.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO response_centers(center_id,name,kind,timezone,capacity_units,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        facility.center_id,
                        facility.name,
                        facility.kind,
                        facility.timezone,
                        decimal_text(facility.capacity_units),
                        self._now(),
                    ),
                )
                self._audit("facility", facility.center_id, "facility.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("设施编号已经存在") from exc
        return dict(raw)

    def create_route(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        route = RoadCorridor.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO road_corridors(corridor_id,origin_center_id,destination_center_id,preservation_resource_kind,hourly_capacity,"
                    "delay_basis_points,response_minutes,duration_unit,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        route.corridor_id,
                        route.origin_center_id,
                        route.destination_center_id,
                        route.preservation_resource_kind,
                        decimal_text(route.hourly_capacity),
                        route.delay_basis_points,
                        route.response_minutes,
                        DURATION_UNIT_MINUTES,
                        self._now(),
                    ),
                )
                self._audit(
                    "route",
                    route.corridor_id,
                    "route.created",
                    actor_id,
                    {**dict(raw), "response_minutes_unit": DURATION_UNIT_MINUTES},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("转运路线编号冲突或设施不存在") from exc
        return self.route(route.corridor_id)

    def route(self, corridor_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM road_corridors WHERE corridor_id=?", (corridor_id,)).fetchone()
        if row is None:
            raise NotFound("转运路线不存在")
        return route_view(row)

    def _require_confirmed_duration(self, route: sqlite3.Row) -> None:
        """无法证明单位的旧路线不得自动参与调度。"""
        if route["duration_unit"] != DURATION_UNIT_MINUTES:
            raise InvalidState(
                f"转运路线 {route['corridor_id']} 的响应时长单位无法从历史数据证明，"
                "已标记为待人工确认，确认前不得调度"
            )

    def confirm_route_duration(
        self,
        actor_id: str,
        corridor_id: str,
        response_minutes: int | None = None,
    ) -> dict[str, Any]:
        """人工确认存量路线的时长确实以分钟登记，也可同时纠正为正确分钟数。"""
        self._require(actor_id, "catalog.write")
        row = self.connection.execute("SELECT * FROM road_corridors WHERE corridor_id=?", (corridor_id,)).fetchone()
        if row is None:
            raise NotFound("转运路线不存在")
        if row["duration_unit"] == DURATION_UNIT_MINUTES:
            raise Conflict("该路线时长单位已是分钟口径，无需人工确认")
        # 无论沿用旧值还是人工纠正，都重新执行登记口径，非法值不得放行。
        minutes = response_minutes_value(
            row["response_minutes"] if response_minutes is None else response_minutes
        )
        confirmed_at = self._now()
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE road_corridors SET duration_unit=?,response_minutes=?,"
                "duration_confirmed_by=?,duration_confirmed_at=?,revision=revision+1 WHERE corridor_id=?",
                (DURATION_UNIT_MINUTES, minutes, actor_id, confirmed_at, corridor_id),
            )
            self._audit(
                "route",
                corridor_id,
                "route.duration_confirmed",
                actor_id,
                {"response_minutes": minutes, "response_minutes_unit": DURATION_UNIT_MINUTES},
            )
        return self.route(corridor_id)

    def announce_restriction(
        self,
        actor_id: str,
        corridor_id: str,
        starts_at: str,
        ends_at: str | None,
        capacity_percent: object,
        reason: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "outage.write")
        self.route(corridor_id)
        try:
            start = parse_utc(starts_at, "starts_at")
            end = None if ends_at is None else parse_utc(ends_at, "ends_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        if end is not None and end <= start:
            raise ValidationFailed("ends_at 必须晚于 starts_at")
        percentage = Decimal(str(capacity_percent))
        if percentage < 0 or percentage > 100:
            raise ValidationFailed("capacity_percent 必须在 0 到 100 之间")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO corridor_restrictions(corridor_id,starts_at,ends_at,capacity_percent,reason,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (corridor_id, utc_text(start), None if end is None else utc_text(end), decimal_text(percentage), reason, actor_id, self._now()),
            )
            restriction_id = int(cursor.lastrowid)
            self._audit("route", corridor_id, "outage.announced", actor_id, {"restriction_id": restriction_id})
        return {"restriction_id": restriction_id, "corridor_id": corridor_id, "state": "announced"}

    def add_inventory_lot(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "inventory.write")
        lot = PreservationResourceLot.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO preservation_resource_lots(preservation_resource_lot_id,center_id,preservation_resource_kind,grade,quantity_units,available_units,"
                    "unit_cost_cny,received_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        lot.preservation_resource_lot_id,
                        lot.center_id,
                        lot.preservation_resource_kind,
                        lot.grade,
                        decimal_text(lot.quantity_units),
                        decimal_text(lot.quantity_units),
                        decimal_text(lot.unit_cost_cny),
                        lot.received_at,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("inventory_lot", lot.preservation_resource_lot_id, "inventory.received", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("应急资源批次冲突或设施不存在") from exc
        return self.inventory_lot(lot.preservation_resource_lot_id)

    def inventory_lot(self, preservation_resource_lot_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM preservation_resource_lots WHERE preservation_resource_lot_id=?", (preservation_resource_lot_id,)).fetchone()
        if row is None:
            raise NotFound("应急资源批次不存在")
        return dict(row)

    def inventory_summary(self, center_id: str, preservation_resource_kind: str) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT * FROM preservation_resource_lots WHERE center_id=? AND preservation_resource_kind=? ORDER BY received_at,preservation_resource_lot_id",
            (center_id, preservation_resource_kind),
        ).fetchall()
        return {"center_id": center_id, "preservation_resource_kind": preservation_resource_kind, **weighted_inventory_cost(rows)}

    def submit_dispatch(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "dispatch_request.write")
        dispatch_request = DispatchRequest.from_dict(raw)
        request_digest = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM traffic_idempotency WHERE scope='dispatch_request' AND idempotency_key=?",
            (dispatch_request.idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同调度申请内容")
            return json.loads(stored["response_json"])
        route = self.connection.execute(
            "SELECT * FROM road_corridors WHERE corridor_id=?",
            (dispatch_request.corridor_id,),
        ).fetchone()
        if route is None:
            raise NotFound("转运路线不存在")
        self._require_confirmed_duration(route)
        if route["state"] != "active":
            raise InvalidState("转运路线当前不可调度申请")
        response = {
            "dispatch_id": dispatch_request.dispatch_id,
            "corridor_id": dispatch_request.corridor_id,
            "state": "submitted",
            "revision": 1,
        }
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO dispatch_requests(dispatch_id,corridor_id,specimen_event_id,duty_date,requested_units,"
                    "priority,idempotency_key,submitted_by,submitted_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        dispatch_request.dispatch_id,
                        dispatch_request.corridor_id,
                        dispatch_request.specimen_event_id,
                        dispatch_request.duty_date,
                        decimal_text(dispatch_request.requested_units),
                        dispatch_request.priority,
                        dispatch_request.idempotency_key,
                        actor_id,
                        self._now(),
                    ),
                )
                self.connection.execute(
                    "INSERT INTO traffic_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                    "VALUES('dispatch_request',?,?,?,?)",
                    (dispatch_request.idempotency_key, request_digest, canonical_json(response), self._now()),
                )
                self._audit("dispatch_request", dispatch_request.dispatch_id, "dispatch_request.submitted", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("调度申请编号或幂等键冲突") from exc
        return response

    def _capacity_for_date(self, route: sqlite3.Row, duty_date: str) -> Decimal:
        start = duty_date + "T00:00:00Z"
        end = duty_date + "T23:59:59Z"
        rows = self.connection.execute(
            "SELECT capacity_percent FROM corridor_restrictions WHERE corridor_id=? AND state IN ('announced','active') "
            "AND starts_at<=? AND (ends_at IS NULL OR ends_at>=?) ORDER BY restriction_id",
            (route["corridor_id"], end, start),
        ).fetchall()
        percentages = [Decimal(row["capacity_percent"]) for row in rows]
        return effective_capacity(Decimal(route["hourly_capacity"]), percentages)

    def allocate(self, actor_id: str, corridor_id: str, duty_date: str) -> dict[str, Any]:
        self._require(actor_id, "allocation.run")
        route = self.connection.execute("SELECT * FROM road_corridors WHERE corridor_id=?", (corridor_id,)).fetchone()
        if route is None:
            raise NotFound("转运路线不存在")
        self._require_confirmed_duration(route)
        dispatch_requests = self.connection.execute(
            "SELECT * FROM dispatch_requests WHERE corridor_id=? AND duty_date=? AND state='submitted' "
            "ORDER BY priority,submitted_at,dispatch_id",
            (corridor_id, duty_date),
        ).fetchall()
        if not dispatch_requests:
            raise InvalidState("没有待分配调度申请")
        requests = [
            AllocationRequest(
                row["dispatch_id"],
                Decimal(row["requested_units"]),
                int(row["priority"]),
                row["submitted_at"],
            )
            for row in dispatch_requests
        ]
        available = self._capacity_for_date(route, duty_date)
        input_value = [dict(row) for row in dispatch_requests]
        input_sha256 = digest({"route": dict(route), "dispatch_requests": input_value, "capacity": str(available)})
        result_rows = allocate_capacity(available, requests)
        result = {
            "corridor_id": corridor_id,
            "duty_date": duty_date,
            "available_units": decimal_text(available),
            "allocations": result_rows,
        }
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO dispatch_plans(corridor_id,duty_date,input_sha256,available_units,result_json,"
                "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (corridor_id, duty_date, input_sha256, decimal_text(available), canonical_json(result), actor_id, self._now()),
            )
            for item in result_rows:
                state = "allocated" if Decimal(item["allocated_units"]) > 0 else "cancelled"
                self.connection.execute(
                    "UPDATE dispatch_requests SET allocated_units=?,state=?,revision=revision+1 "
                    "WHERE dispatch_id=? AND state='submitted'",
                    (item["allocated_units"], state, item["dispatch_id"]),
                )
            plan_id = int(cursor.lastrowid)
            self._audit("route", corridor_id, "allocation.completed", actor_id, {"plan_id": plan_id})
        return {"plan_id": plan_id, **result}

    def dispatch_deployment(
        self,
        actor_id: str,
        deployment_id: str,
        dispatch_id: str,
        preservation_resource_lot_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        self._require(actor_id, "deployment.write")
        dispatch_request = self.connection.execute(
            "SELECT n.*,r.delay_basis_points,r.response_minutes,r.duration_unit,r.origin_center_id FROM dispatch_requests n "
            "JOIN road_corridors r ON r.corridor_id=n.corridor_id WHERE n.dispatch_id=?",
            (dispatch_id,),
        ).fetchone()
        if dispatch_request is None:
            raise NotFound("调度申请不存在")
        self._require_confirmed_duration(dispatch_request)
        if dispatch_request["state"] != "allocated" or dispatch_request["revision"] != expected_revision:
            raise InvalidState("调度申请不是当前可资源到场版本")
        lot = self.connection.execute("SELECT * FROM preservation_resource_lots WHERE preservation_resource_lot_id=?", (preservation_resource_lot_id,)).fetchone()
        if lot is None:
            raise NotFound("应急资源批次不存在")
        allocated = Decimal(dispatch_request["allocated_units"])
        available = Decimal(lot["available_units"])
        if lot["center_id"] != dispatch_request["origin_center_id"] or lot["preservation_resource_kind"] != self.route(dispatch_request["corridor_id"])["preservation_resource_kind"]:
            raise Conflict("应急资源批次与转运路线起点或电源类型不匹配")
        if available < allocated:
            raise Conflict("应急资源库存不足以完成分配")
        expected_delivery = delivered_after_loss(allocated, int(dispatch_request["delay_basis_points"]))
        departed = self.clock.now()
        departed_at = utc_text(departed)
        response_minutes = int(dispatch_request["response_minutes"])
        expected_arrival = utc_text(arrival_after_minutes(departed, response_minutes))
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE preservation_resource_lots SET available_units=?,revision=revision+1 WHERE preservation_resource_lot_id=? AND revision=?",
                (decimal_text(quantize_volume(available - allocated)), preservation_resource_lot_id, lot["revision"]),
            )
            self.connection.execute(
                "UPDATE dispatch_requests SET state='in_transit',revision=revision+1 WHERE dispatch_id=? AND revision=?",
                (dispatch_id, expected_revision),
            )
            self.connection.execute(
                "INSERT INTO deployments(deployment_id,dispatch_id,inventory_preservation_resource_lot_id,deployed_units,"
                "expected_arrived_units,departed_at,expected_arrival_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    deployment_id,
                    dispatch_id,
                    preservation_resource_lot_id,
                    decimal_text(allocated),
                    decimal_text(expected_delivery),
                    departed_at,
                    expected_arrival,
                    actor_id,
                    departed_at,
                ),
            )
            self._audit(
                "deployment",
                deployment_id,
                "deployment.dispatched",
                actor_id,
                {
                    "dispatch_id": dispatch_id,
                    "response_minutes": response_minutes,
                    "response_minutes_unit": DURATION_UNIT_MINUTES,
                    "departed_at": departed_at,
                    "expected_arrival": expected_arrival,
                },
            )
        return {
            "deployment_id": deployment_id,
            "state": "in_transit",
            "deployed_units": decimal_text(allocated),
            "expected_arrived_units": decimal_text(expected_delivery),
            "departed_at": departed_at,
            "response_minutes": response_minutes,
            "response_minutes_unit": DURATION_UNIT_MINUTES,
            "expected_arrival": expected_arrival,
        }

    def create_scenario(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "scenario.write")
        scenario = ResponseScenario.from_dict(raw)
        definition = canonical_json(raw)
        content_sha256 = hashlib.sha256(definition.encode("utf-8")).hexdigest()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO response_scenarios(scenario_id,name,definition_json,content_sha256,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (scenario.scenario_id, scenario.name, definition, content_sha256, actor_id, self._now()),
                )
                self._audit("scenario", scenario.scenario_id, "scenario.created", actor_id, {"sha256": content_sha256})
        except sqlite3.IntegrityError as exc:
            raise Conflict("情景编号或内容已经存在") from exc
        return {"scenario_id": scenario.scenario_id, "state": "draft", "sha256": content_sha256}

    def approve_scenario(self, actor_id: str, scenario_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "scenario.approve")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE response_scenarios SET state='approved',revision=revision+1 "
                "WHERE scenario_id=? AND state='draft' AND revision=?",
                (scenario_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("情景不是当前草稿版本")
            self._audit("scenario", scenario_id, "scenario.approved", actor_id, {})
        return {"scenario_id": scenario_id, "state": "approved", "revision": expected_revision + 1}

    def run_scenario(self, actor_id: str, scenario_id: str, as_of_date: str) -> dict[str, Any]:
        self._require(actor_id, "scenario.run")
        row = self.connection.execute(
            "SELECT * FROM response_scenarios WHERE scenario_id=?", (scenario_id,)
        ).fetchone()
        if row is None:
            raise NotFound("情景不存在")
        if row["state"] != "approved":
            raise InvalidState("只有已批准情景可以运行")
        scenario = ResponseScenario.from_dict(json.loads(row["definition_json"]))
        index_row = self.connection.execute(
            "SELECT index_value FROM risk_index_risk_records WHERE duty_date<=? ORDER BY duty_date DESC,risk_record_id DESC LIMIT 1",
            (as_of_date,),
        ).fetchone()
        if index_row is None:
            raise InvalidState("截止日期没有可用风险指数")
        road_corridors = self.connection.execute("SELECT * FROM road_corridors WHERE state='active' ORDER BY corridor_id").fetchall()
        inventory = self.connection.execute(
            "SELECT center_id,preservation_resource_kind,sum(CAST(available_units AS REAL)) available_units "
            "FROM preservation_resource_lots GROUP BY center_id,preservation_resource_kind ORDER BY center_id,preservation_resource_kind"
        ).fetchall()
        input_value = {
            "scenario_sha256": row["content_sha256"],
            "as_of_date": as_of_date,
            "index": index_row["index_value"],
            "road_corridors": [route_view(item) for item in road_corridors],
            "inventory": [dict(item) for item in inventory],
        }
        input_sha256 = digest(input_value)
        existing = self.connection.execute(
            "SELECT run_id,result_json FROM response_scenario_runs WHERE scenario_id=? AND as_of_date=? AND input_sha256=?",
            (scenario_id, as_of_date, input_sha256),
        ).fetchone()
        if existing is not None:
            return {"run_id": existing["run_id"], **json.loads(existing["result_json"]), "replayed": True}
        result = scenario_projection(
            current_index=Decimal(index_row["index_value"]),
            risk_index_drop_percent=scenario.risk_index_drop_percent,
            road_corridors=road_corridors,
            inventory=inventory,
            route_capacity_changes=scenario.route_capacity_changes,
            demand_changes=scenario.demand_changes,
        )
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO response_scenario_runs(scenario_id,as_of_date,input_sha256,result_json,created_by,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (scenario_id, as_of_date, input_sha256, canonical_json(result), actor_id, self._now()),
            )
            run_id = int(cursor.lastrowid)
            self._audit("scenario", scenario_id, "scenario.executed", actor_id, {"run_id": run_id})
        return {"run_id": run_id, **result, "replayed": False}

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM traffic_audit_events ORDER BY event_id").fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}

    def audit_summary(self, actor_id: str) -> dict[str, Any]:
        """审计摘要：事件计数，以及时长口径相关事件的显式单位清单。"""
        self._require(actor_id, "audit.read")
        rows = self.connection.execute(
            "SELECT event_type, payload_json FROM traffic_audit_events ORDER BY event_id"
        ).fetchall()
        counts: dict[str, int] = {}
        duration_events: list[dict[str, Any]] = []
        for row in rows:
            counts[row["event_type"]] = counts.get(row["event_type"], 0) + 1
            payload = json.loads(row["payload_json"])
            if "response_minutes" in payload:
                # 与路线接口、任务执行结果使用同一解释：分钟。
                duration_events.append(
                    {
                        "event_type": row["event_type"],
                        "response_minutes": payload["response_minutes"],
                        "response_minutes_unit": payload.get(
                            "response_minutes_unit", DURATION_UNIT_LEGACY_UNKNOWN
                        ),
                    }
                )
        return {
            "events": len(rows),
            "counts": dict(sorted(counts.items())),
            "duration_unit": DURATION_UNIT_MINUTES,
            "duration_events": duration_events,
        }

    def deployment(self, deployment_id: str) -> dict[str, Any]:
        """历史读取：部署记录与路线时长口径必须使用同一解释。"""
        row = self.connection.execute(
            "SELECT d.*,r.response_minutes,r.duration_unit FROM deployments d "
            "JOIN dispatch_requests n ON n.dispatch_id=d.dispatch_id "
            "JOIN road_corridors r ON r.corridor_id=n.corridor_id "
            "WHERE d.deployment_id=?",
            (deployment_id,),
        ).fetchone()
        if row is None:
            raise NotFound("任务执行记录不存在")
        data = dict(row)
        unit = row["duration_unit"]
        data["response_minutes_unit"] = DURATION_UNIT_MINUTES
        data["duration_confirmed"] = unit == DURATION_UNIT_MINUTES
        data["duration_pending_confirmation"] = unit == DURATION_UNIT_LEGACY_UNKNOWN
        # 预计到达时刻以发车时落库的值为准；存量记录留空，历史读取不得按当前路线值补算。
        data["expected_arrival"] = row["expected_arrival_at"]
        return data
