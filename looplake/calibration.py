from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
import re

from .model import LakeError, calendar_day, key, now, required
from .store import Store


SCALE_EVENT_TYPES = {"weigh_in", "weigh_out"}


def positive_factor(value, name: str = "factor") -> str:
    """校准系数：保留 6 位小数，允许 0.000001..1000000。"""
    if isinstance(value, bool) or value is None:
        raise LakeError("invalid_" + name, f"{name} must be numeric")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise LakeError("invalid_" + name, f"{name} must be numeric") from exc
    if not number.is_finite() or number <= 0 or number > Decimal("1000000"):
        raise LakeError("invalid_" + name, f"{name} must be a positive calibration factor")
    if abs(number.as_tuple().exponent) > 6:
        raise LakeError("invalid_" + name, f"{name} exceeds supported precision")
    return format(number.normalize(), "f")


def scaled(weight: str | None, factor: str) -> str:
    """原始读数 × 系数 = 核算净重，定点十进制。"""
    result = (Decimal(weight or "0") * Decimal(factor)).quantize(Decimal("0.000001"))
    return format(result.normalize(), "f")


@dataclass(frozen=True)
class CalibrationInput:
    device_id: str
    certificate_id: str
    valid_from: str
    valid_to: str
    factor: str
    recorded_at: str | None

    @classmethod
    def parse(cls, data: dict) -> "CalibrationInput":
        if not isinstance(data, dict):
            raise LakeError("invalid_calibration", "calibration must be an object")
        device_id = key(data.get("device_id"), "device_id")
        certificate_id = required(data.get("certificate_id"), "certificate_id", 120)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]*", certificate_id):
            raise LakeError("invalid_certificate_id", "certificate_id must be a stable token")
        start = calendar_day(data.get("valid_from"), "valid_from")
        end = calendar_day(data.get("valid_to"), "valid_to")
        if end < start:
            raise LakeError("invalid_calibration_period", "valid_to must not precede valid_from")
        recorded_at = data.get("recorded_at")
        if recorded_at is not None:
            from .model import instant
            recorded_at = instant(recorded_at, "recorded_at")
        return cls(device_id, certificate_id, start, end,
                   positive_factor(data.get("factor")), recorded_at)


class Calibration:
    """地磅设备校准证书与称重读数的可追溯处理。

    事实保留原始读数 raw_weight_kg，weight_kg 为入账净重；校准证书覆盖
    [valid_from, valid_to] 闭区间。临界日（首日/末日）不阻断核算，但会被
    标记并随批次质量报告暴露。证书撤销、补发或更正都不会覆盖历史称重：
    更正写入 weight_corrections（追加证据），当前视图取最新更正，发布版
    保持冻结内容。校准通知（recorded_at）晚于月结时只影响后续重估资格。
    """

    def __init__(self, store: Store):
        self.store = store

    # ---------- 证书生命周期 ----------
    def put(self, data: dict):
        cal = CalibrationInput.parse(data)
        with self.store.write() as conn:
            device = self.store.one(conn, "SELECT id FROM dimension_versions WHERE kind='device' AND external_id=? ORDER BY version LIMIT 1",
                                    (cal.device_id,))
            if device is None:
                raise LakeError("unknown_device", "calibration device must be registered", 409)
            existing = self.store.one(conn, "SELECT * FROM calibrations WHERE device_id=? AND certificate_id=?",
                                      (cal.device_id, cal.certificate_id))
            if existing:
                if (existing["valid_from"], existing["valid_to"], existing["factor"]) == \
                        (cal.valid_from, cal.valid_to, cal.factor):
                    return self._view(existing)
                raise LakeError("calibration_conflict", "certificate already recorded with different content", 409)
            overlaps = self.store.all(conn, "SELECT * FROM calibrations WHERE device_id=? AND state='valid' AND valid_from<=? AND valid_to>=? ORDER BY certificate_id",
                                      (cal.device_id, cal.valid_to, cal.valid_from))
            cursor = conn.execute("INSERT INTO calibrations(device_id,certificate_id,valid_from,valid_to,factor,state,recorded_at,created_at) VALUES(?,?,?,?,?, 'valid', ?, datetime('now'))",
                                  (cal.device_id, cal.certificate_id, cal.valid_from, cal.valid_to,
                                   cal.factor, cal.recorded_at or now()))
            self.store.audit(conn, "calibration_recorded", cal.device_id,
                             {"calibration_id": cursor.lastrowid, "certificate_id": cal.certificate_id,
                              "range": [cal.valid_from, cal.valid_to], "factor": cal.factor,
                              "overlap": [x["certificate_id"] for x in overlaps],
                              "recorded_at": cal.recorded_at})
            if overlaps:
                self.store.audit(conn, "calibration_overlap_detected", cal.device_id,
                                 {"certificate_id": cal.certificate_id,
                                  "overlaps": [x["certificate_id"] for x in overlaps],
                                  "range": [cal.valid_from, cal.valid_to],
                                  "resolution": "lowest certificate_id is used for accounting"})
            row = self.store.one(conn, "SELECT * FROM calibrations WHERE id=?", (cursor.lastrowid,))
        return self._view(row)

    def revoke(self, device_id: str, certificate_id: str, reason: str | None = None,
               notified_at: str | None = None):
        device_id = key(device_id, "device_id")
        certificate_id = required(certificate_id, "certificate_id", 120)
        with self.store.write() as conn:
            row = self.store.one(conn, "SELECT * FROM calibrations WHERE device_id=? AND certificate_id=?",
                                 (device_id, certificate_id))
            if row is None:
                raise LakeError("unknown_calibration", "calibration certificate not found", 404)
            if row["state"] == "revoked":
                return {**self._view(row), "already_revoked": True}
            conn.execute("UPDATE calibrations SET state='revoked', reason=?, revoked_at=? WHERE id=?",
                         (reason, notified_at or now(), row["id"]))
            self.store.audit(conn, "calibration_revoked", device_id,
                             {"calibration_id": row["id"], "certificate_id": certificate_id,
                              "reason": reason, "notified_at": notified_at})
        return {**self._view(self._row(row["id"])), "already_revoked": False}

    def certificates(self, device_id: str):
        device_id = key(device_id, "device_id")
        with self.store.connect() as conn:
            rows = self.store.all(conn, "SELECT * FROM calibrations WHERE device_id=? ORDER BY valid_from,certificate_id",
                                  (device_id,))
        return [self._view(row) for row in rows]

    def effective(self, device_id: str, day: str) -> dict | None:
        """某日生效的校准证书。重叠时按证书 ID 排序取第一张作为核算依据，
        并在返回中暴露全部候选与歧义标记。"""
        device_id = key(device_id, "device_id")
        day = calendar_day(day, "day")
        rows = self._effective_rows(device_id, day)
        if not rows:
            with self.store.connect() as conn:
                registered = self.store.one(conn, "SELECT COUNT(*) AS n FROM calibrations WHERE device_id=?",
                                            (device_id,))["n"]
            if registered:
                return {"certificate_id": None, "candidates": [], "ambiguous": False,
                        "boundary_day": False, "uncalibrated": True,
                        "calibration_id": None, "factor": "1"}
            return None
        result = self._view(rows[0])
        result["candidates"] = [self._view(row) for row in rows]
        result["ambiguous"] = len(rows) > 1
        result["boundary_day"] = day in (rows[0]["valid_from"], rows[0]["valid_to"])
        return result

    def pending_notice(self, device_id: str, month: str) -> list[str]:
        """校准通知（recorded_at）晚于月结的证书：不参与当月核算依据判断。"""
        return [row["certificate_id"] for row in self.certificates(device_id)
                if row["recorded_at"][:7] > month]

    def _effective_rows(self, device_id: str, day: str):
        with self.store.connect() as conn:
            return self.store.all(conn, "SELECT * FROM calibrations WHERE device_id=? AND state='valid' AND valid_from<=? AND valid_to>=? ORDER BY certificate_id",
                                  (device_id, day, day))

    def state(self, device_id: str):
        device_id = key(device_id, "device_id")
        certs = self.certificates(device_id)
        with self.store.connect() as conn:
            rows = self.store.all(conn, "SELECT id,device_id,day,event_type,raw_weight_kg,weight_kg FROM facts WHERE device_id=? AND event_type IN ('weigh_in','weigh_out') ORDER BY day,id",
                                  (device_id,))
            corrections = self.store.all(conn, "SELECT DISTINCT fact_id FROM weight_corrections WHERE device_id=?",
                                         (device_id,))
        uncalibrated = []
        for row in rows:
            effective = self.effective(device_id, row["day"])
            if effective is None or effective.get("uncalibrated"):
                uncalibrated.append(row["id"])
        revoked = [c for c in certs if c["state"] == "revoked"]
        affected = [row["id"] for row in rows
                    if any(c["valid_from"] <= row["day"] <= c["valid_to"] for c in revoked)]
        boundary = [row["id"] for row in rows
                    if (self.effective(device_id, row["day"]) or {}).get("boundary_day")]
        return {"device_id": device_id, "certificates": certs,
                "valid_certificate_ids": [c["certificate_id"] for c in certs if c["state"] == "valid"],
                "revoked_certificate_ids": sorted(c["certificate_id"] for c in revoked),
                "overlapping_certificates": self._overlaps(certs),
                "readings": len(rows), "uncalibrated_fact_ids": uncalibrated,
                "boundary_fact_ids": boundary,
                "affected_fact_ids": sorted(set(affected)),
                "corrected_fact_ids": sorted({row["fact_id"] for row in corrections})}

    @staticmethod
    def _overlaps(certs):
        valid = [c for c in certs if c["state"] == "valid"]
        pairs = []
        for index, left in enumerate(valid):
            for right in valid[index + 1:]:
                if left["valid_from"] <= right["valid_to"] and right["valid_from"] <= left["valid_to"]:
                    pairs.append([left["certificate_id"], right["certificate_id"]])
        return pairs

    # ---------- 事实读数与更正 ----------
    def annotate(self, conn, dimension_rows: dict, day: str) -> dict | None:
        """称重事实归一化时调用；返回入账依据的证书快照。

        设备从未登记任何证书时不干预（保持原始读数与既有行为）；一旦设备
        纳入校准管理，落在证书区间之外即标记 uncalibrated。"""
        if "device" not in dimension_rows:
            return None
        device_id = dimension_rows["device"]["external_id"]
        registered = self.store.one(conn, "SELECT COUNT(*) AS n FROM calibrations WHERE device_id=?",
                                   (device_id,))["n"]
        if not registered:
            return None
        rows = self.store.all(conn, "SELECT * FROM calibrations WHERE device_id=? AND state='valid' AND valid_from<=? AND valid_to>=? ORDER BY certificate_id",
                              (device_id, day, day))
        if not rows:
            return {"certificate_id": None, "calibration_id": None, "factor": "1",
                    "boundary_day": False, "ambiguous": False,
                    "uncalibrated": True, "candidates": []}
        chosen = rows[0]
        return {"certificate_id": chosen["certificate_id"],
                "calibration_id": chosen["id"],
                "factor": chosen["factor"],
                "boundary_day": day in (chosen["valid_from"], chosen["valid_to"]),
                "ambiguous": len(rows) > 1,
                "uncalibrated": False,
                "candidates": [row["certificate_id"] for row in rows]}

    def facts_affected(self, device_id: str, valid_from: str, valid_to: str,
                       material_id: str | None = None):
        device_id = key(device_id, "device_id")
        start, end = calendar_day(valid_from, "valid_from"), calendar_day(valid_to, "valid_to")
        args, sql = [device_id, start, end], "WHERE device_id=? AND event_type IN ('weigh_in','weigh_out') AND day>=? AND day<=?"
        if material_id is not None:
            sql += " AND material_id=?"
            args.append(key(material_id, "material_id"))
        return self.store.facts(sql, tuple(args))

    def correct(self, fact_id: int, new_weight=None, calibration_id: int | None = None,
                note: str | None = None):
        if isinstance(fact_id, bool) or not isinstance(fact_id, int) or fact_id <= 0:
            raise LakeError("invalid_fact_id", "fact_id must be a positive integer")
        if new_weight is None and calibration_id is None:
            raise LakeError("missing_correction_basis", "new_weight_kg or calibration_id is required")
        with self.store.write() as conn:
            fact = self.store.one(conn, "SELECT * FROM facts WHERE id=?", (fact_id,))
            if fact is None:
                raise LakeError("unknown_fact", "fact not found", 404)
            if fact["event_type"] not in SCALE_EVENT_TYPES:
                raise LakeError("not_a_scale_reading", "only scale facts can be corrected", 409)
            cal = None
            if calibration_id is not None:
                cal = self.store.one(conn, "SELECT * FROM calibrations WHERE id=?", (calibration_id,))
                if cal is None:
                    raise LakeError("unknown_calibration", "calibration certificate not found", 404)
                if cal["device_id"] != fact["device_id"]:
                    raise LakeError("calibration_device", "certificate does not belong to reading device", 409)
                if not cal["valid_from"] <= fact["day"] <= cal["valid_to"]:
                    raise LakeError("calibration_period", "certificate does not cover the reading day", 409)
                basis = "recalibration"
                reason = note or f"recalibrated with certificate {cal['certificate_id']}"
                boundary = 1 if fact["day"] in (cal["valid_from"], cal["valid_to"]) else 0
                basis_record = {"certificate_id": cal["certificate_id"], "factor": cal["factor"]}
                derived = scaled(fact.get("raw_weight_kg") or fact["weight_kg"], cal["factor"])
                if new_weight is None:
                    new_weight = derived
                elif Decimal(str(new_weight)) != Decimal(derived):
                    raise LakeError("calibration_mismatch",
                                    f"new_weight_kg does not equal raw × certificate factor ({derived})", 409)
            else:
                basis, reason, boundary = "manual_adjustment", note or "manual correction", 0
                basis_record = {}
            try:
                new = Decimal(str(new_weight))
            except InvalidOperation as exc:
                raise LakeError("invalid_new_weight", "new_weight_kg must be numeric") from exc
            if not new.is_finite() or new <= 0 or abs(new.as_tuple().exponent) > 6:
                raise LakeError("invalid_new_weight", "new_weight_kg must be a positive reading")
            duplicate = self.store.one(conn, "SELECT id FROM weight_corrections WHERE fact_id=? AND calibration_id IS ? AND new_weight_kg=?",
                                       (fact_id, None if cal is None else cal["id"], format(new, "f")))
            if duplicate:
                return {"fact_id": fact_id, "changed": False, "correction_id": duplicate["id"]}
            booked = self.store.one(conn, "SELECT new_weight_kg FROM weight_corrections WHERE fact_id=? ORDER BY id DESC LIMIT 1",
                                    (fact_id,))
            current = booked["new_weight_kg"] if booked else fact["weight_kg"]
            cursor = conn.execute("INSERT INTO weight_corrections(fact_id,device_id,certificate_id,calibration_id,raw_weight_kg,old_weight_kg,new_weight_kg,basis,boundary_day,note,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,datetime('now'))",
                                  (fact_id, fact["device_id"],
                                   None if cal is None else cal["certificate_id"],
                                   None if cal is None else cal["id"],
                                   fact["raw_weight_kg"] or fact["weight_kg"], current,
                                   format(new, "f"), basis, boundary, reason))
            # 只追加更正证据，不 UPDATE facts：历史原始读数与入账净重保持原样。
            self.store.audit(conn, "weight_corrected", str(fact_id),
                             {"correction_id": cursor.lastrowid, "old_weight_kg": current,
                              "new_weight_kg": format(new, "f"), "basis": basis,
                              "boundary_day": bool(boundary), **basis_record})
        return {"fact_id": fact_id, "changed": True, "correction_id": cursor.lastrowid,
                "old_weight_kg": current, "new_weight_kg": format(new, "f"),
                "basis": basis, "boundary_day": bool(boundary)}

    def impact(self, device_id: str, valid_from: str, valid_to: str,
               material_id: str | None = None, tolerance_kg: str = "0.5"):
        """只读重估：按当前有效证书重算受影响称重，重新判断批次守恒、
        站点库存与月结资格；不回写事实，也不修改历史发布版。"""
        from .correction import Review
        return Review(self.store, self).run(device_id, valid_from, valid_to,
                                            material_id, tolerance_kg)

    def _row(self, calibration_id: int):
        with self.store.connect() as conn:
            return self.store.one(conn, "SELECT * FROM calibrations WHERE id=?", (calibration_id,))

    @staticmethod
    def _view(row: dict) -> dict:
        return {"calibration_id": row["id"], "device_id": row["device_id"],
                "certificate_id": row["certificate_id"],
                "valid_from": row["valid_from"], "valid_to": row["valid_to"],
                "factor": row["factor"], "state": row["state"],
                "reason": row["reason"], "recorded_at": row["recorded_at"],
                "revoked_at": row["revoked_at"]}
