from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
import json

from .model import LakeError


class Correction:
    """读数更正的历史视图：更正作为追加证据保存，不覆盖原始事实。"""

    def __init__(self, store):
        self.store = store

    def latest(self, fact_ids: list[int]):
        if not fact_ids:
            return {}
        placeholders = ",".join("?" for _ in fact_ids)
        with self.store.connect() as conn:
            rows = self.store.all(conn,
                                  "SELECT * FROM weight_corrections WHERE fact_id IN ("
                                  + placeholders + ") ORDER BY id", tuple(fact_ids))
        return {row["fact_id"]: row for row in rows}

    def view(self, fact: dict, correction: dict | None = None) -> dict:
        if correction is None:
            correction = self.latest([fact["id"]]).get(fact["id"])
        snapshot = json.loads(fact.get("calibration_json") or "{}")
        return {"fact_id": fact["id"], "batch_id": fact["batch_id"],
                "event_type": fact["event_type"], "day": fact["day"],
                "raw_weight_kg": fact.get("raw_weight_kg") or fact["weight_kg"],
                "booked_weight_kg": fact.get("booked_weight_kg", fact["weight_kg"]),
                "current_weight_kg": fact["weight_kg"],
                "correction_id": None if correction is None else correction["id"],
                "corrected_weight_kg": None if correction is None else correction["new_weight_kg"],
                "boundary_day": bool((correction and correction["boundary_day"])
                                     or (correction is None and snapshot.get("boundary_day"))),
                "certificate_id": (correction["certificate_id"] if correction
                                   else snapshot.get("certificate_id")),
                "basis": None if correction is None else correction["basis"],
                "note": None if correction is None else correction["note"]}

    def facts(self, fact_ids: list[int]):
        latest = self.latest(fact_ids)
        return [self.view(self.store.fact(fact_id), latest.get(fact_id)) for fact_id in fact_ids]

    def history(self, fact_id: int):
        if isinstance(fact_id, bool) or not isinstance(fact_id, int) or fact_id <= 0:
            raise LakeError("invalid_fact_id", "fact_id must be a positive integer")
        fact = self.store.fact(fact_id)
        with self.store.connect() as conn:
            rows = self.store.all(conn, "SELECT * FROM weight_corrections WHERE fact_id=? ORDER BY id",
                                  (fact_id,))
        return {"fact_id": fact_id,
                "raw_weight_kg": fact.get("raw_weight_kg") or fact["weight_kg"],
                "booked_weight_kg": fact.get("booked_weight_kg", fact["weight_kg"]),
                "weight_kg": fact["weight_kg"], "corrections": rows}


def _month_end(month: str) -> date:
    if month.endswith("12"):
        return date(int(month[:4]), 12, 31)
    return date(int(month[:4]), int(month[5:7]) + 1, 1) - timedelta(days=1)


class Review:
    """校准撤销/补发后的逐事实重估报告：只读，不直接改历史发布版。

    对每个受影响称重事实给出原始读数、入账净重、按当前有效证书重算的
    拟议净重与差额；据此重新判断批次守恒、站点库存与月结资格。结果不写回
    事实表，历史发布版保持冻结；如需采纳，走更正（追加证据）并发布新版本。
    """

    def __init__(self, store, calibration):
        self.store, self.calibration = store, calibration

    def _services(self):
        from .dimensions import Dimensions
        from .inventory import Inventory
        from .landing import Landing
        from .ledger import Ledger
        from .reconcile import Reconciliation
        from .settlement import Settlement
        ledger = Ledger(self.store, Dimensions(self.store),
                        Landing(self.store, Dimensions(self.store)), self.calibration)
        return ledger, Reconciliation(ledger, Settlement(self.store)), Inventory(ledger)

    def run(self, device_id: str, valid_from: str, valid_to: str,
            material_id: str | None = None, tolerance_kg: str = "0.5",
            month: str | None = None):
        from .model import calendar_day
        from .calibration import scaled

        valid_from = calendar_day(valid_from, "valid_from")
        valid_to = calendar_day(valid_to, "valid_to")
        if valid_to < valid_from:
            raise LakeError("invalid_range", "valid_to precedes valid_from")
        try:
            tolerance = Decimal(str(tolerance_kg))
        except Exception as exc:
            raise LakeError("invalid_tolerance", "tolerance must be numeric") from exc
        if not tolerance.is_finite() or tolerance < 0 or tolerance > 1000:
            raise LakeError("invalid_tolerance", "tolerance must be 0..1000 kg")
        facts = self.calibration.facts_affected(device_id, valid_from, valid_to, material_id)
        ledger, reconciliation, inventory = self._services()
        corrections = {row["fact_id"]: row for row in corrections_for(self.store, [f["id"] for f in facts])}
        month = month or (facts[0]["day"][:7] if facts else valid_to[:7])
        from .model import calendar_day as _day
        _day(month + "-01", "month")

        readings, proposed = [], {}
        for fact in facts:
            effective = self.calibration.effective(fact["device_id"], fact["day"])
            correction = corrections.get(fact["id"])
            raw = fact.get("raw_weight_kg") or fact["weight_kg"]
            booked = fact.get("booked_weight_kg", fact["weight_kg"])
            proposed_weight = scaled(raw, effective["factor"]) if effective and not effective.get("uncalibrated") else None
            readings.append({
                "fact_id": fact["id"], "batch_id": fact["batch_id"], "day": fact["day"],
                "device_id": fact["device_id"], "event_type": fact["event_type"],
                "raw_weight_kg": raw, "booked_weight_kg": booked,
                "current_weight_kg": fact["weight_kg"],
                "proposed_weight_kg": proposed_weight,
                "certificate_id": None if effective is None else effective["certificate_id"],
                "certificate_unavailable": bool(effective is not None and effective.get("uncalibrated")),
                "certificate_candidates": [] if effective is None else
                [c["certificate_id"] for c in effective["candidates"]],
                "boundary_day": bool(effective and effective["boundary_day"]),
                "ambiguous_certificate": bool(effective and effective["ambiguous"]),
                "already_corrected": correction is not None,
                "notice_after_month_close": bool(effective and effective.get("recorded_at")
                                                 and effective["recorded_at"][:7] > month),
                "delta_kg": None if proposed_weight is None else
                            format(Decimal(proposed_weight) - Decimal(booked), "f"),
                "basis": "uncalibrated" if effective is None or effective.get("uncalibrated")
                else "effective_calibration"})
            if proposed_weight is not None:
                proposed[fact["id"]] = proposed_weight

        affected_batches = sorted({row["batch_id"] for row in readings})
        projections = []
        for batch_id in affected_batches:
            original = ledger.batch(batch_id)
            shadow = []
            for row in original:
                copy = dict(row)
                reading = next((x for x in readings if x["fact_id"] == copy["id"]), None)
                if reading is None:
                    shadow.append(copy)
                    continue
                if reading["basis"] == "uncalibrated":
                    # 无有效证书：由 calibration_unavailable 报告，不虚构依据。
                    copy["calibration"] = {}
                    copy["calibration_json"] = "{}"
                elif copy["id"] in proposed:
                    snapshot = {"certificate_id": reading["certificate_id"],
                                "boundary_day": reading["boundary_day"],
                                "ambiguous": reading["ambiguous_certificate"],
                                # 保留历史入账值：重估只预览，须追加更正才消除 basis_changed。
                                "booked_weight_kg": reading["booked_weight_kg"]}
                    copy["calibration"] = snapshot
                    copy["calibration_json"] = json.dumps(snapshot)
                    copy["weight_kg"] = proposed[copy["id"]]
                shadow.append(copy)
            revaluated = self._evaluate(reconciliation, batch_id, shadow, tolerance)
            in_batch = [x for x in readings if x["batch_id"] == batch_id]
            uncalibrated = [x["fact_id"] for x in in_batch if x["basis"] == "uncalibrated"]
            ambiguous = [x["fact_id"] for x in in_batch if x["ambiguous_certificate"]]
            if uncalibrated:
                revaluated["issues"].append({
                    "code": "calibration_unavailable", "fact_ids": uncalibrated,
                    "detail": "no valid certificate covers the reading; net weight needs manual basis"})
                revaluated["ready"] = False
            if ambiguous:
                revaluated["issues"].append({
                    "code": "calibration_ambiguous", "fact_ids": ambiguous,
                    "detail": "multiple certificates overlap; lowest certificate_id is provisional"})
                revaluated["ready"] = False
            current = reconciliation.batch(batch_id)
            revaluated["ready_now"] = current["ready"]
            revaluated["weight_kg"] = {row["id"]: row["weight_kg"] for row in shadow}
            projections.append(revaluated)

        stations = self._inventory(month, proposed, inventory) if facts else []
        unsupported = [x["fact_id"] for x in readings if x["basis"] == "uncalibrated"]
        ambiguous = [x["fact_id"] for x in readings if x["ambiguous_certificate"]]
        frozen = self._frozen_releases(month)
        inventory_ready = all(row["ready"] for row in stations) and not unsupported
        return {"device_id": device_id, "range": [valid_from, valid_to],
                "material_id": material_id, "month": month,
                "readings": readings,
                "batches": projections,
                "batches_needing_revaluation": [row["batch_id"] for row in projections
                                                if not row["ready"]],
                "inventory": {"month": month, "ready": inventory_ready,
                              "uncalibrated_fact_ids": unsupported,
                              "negative_fact_ids": sorted({fact_id for row in stations
                                                           for fact_id in row["negative_fact_ids"]}),
                              "station_materials": stations},
                "frozen_releases": frozen,
                "month_eligible_for_release": not readings or
                                             (all(row["ready"] for row in projections)
                                              and inventory_ready),
                "calibration_notice_after_month_close": [
                    {"fact_id": x["fact_id"], "certificate_id": x["certificate_id"]}
                    for x in readings if x["notice_after_month_close"]],
                "unresolved_fact_ids": unsupported + ambiguous,
                "historical_release_modified": False,
                "requires_new_release": bool(readings),
                "read_only": True,
                "note": ("no reading matches the device/range; nothing to revalue" if not readings
                         else "重估不写回事实；采纳需追加更正并发布新版本")}

    def _evaluate(self, reconciliation, batch_id, shadow, tolerance):
        try:
            result = reconciliation.batch(batch_id, str(tolerance), facts=shadow)
            return {"batch_id": batch_id, "ready": result["ready"],
                    "issues": result["issues"],
                    "deposit_kg": result["deposit_kg"],
                    "weigh_in_kg": result["weigh_in_kg"],
                    "weigh_out_kg": result["weigh_out_kg"],
                    "calibration_boundary_fact_ids": result["calibration_boundary_fact_ids"]}
        except LakeError as error:
            return {"batch_id": batch_id, "ready": False,
                    "issues": [{"code": error.code, "detail": error.detail}],
                    "deposit_kg": None, "weigh_in_kg": None, "weigh_out_kg": None,
                    "calibration_boundary_fact_ids": []}

    def _inventory(self, month: str, proposed: dict[int, str], inventory):
        end = _month_end(month)
        all_facts = self.store.facts("WHERE day<=?", (end.isoformat(),))
        shadow = []
        for row in all_facts:
            copy = dict(row)
            if copy["id"] in proposed:
                copy["weight_kg"] = proposed[copy["id"]]
            shadow.append(copy)
        keys = sorted({(fact["station_id"], fact["material_id"])
                       for fact in shadow if fact["day"][:7] == month
                       and fact["station_id"] and fact["material_id"]
                       and fact["event_type"] in {"weigh_in", "weigh_out"}})
        return [inventory.station_day(station, material, end.isoformat(), facts=shadow)
                for station, material in keys]

    def _frozen_releases(self, month: str):
        with self.store.connect() as conn:
            return self.store.all(conn, "SELECT id,name,fingerprint,created_at FROM releases WHERE project_month=? ORDER BY id",
                                  (month,))


def corrections_for(store, fact_ids):
    if not fact_ids:
        return []
    placeholders = ",".join("?" for _ in fact_ids)
    with store.connect() as conn:
        return store.all(conn, "SELECT * FROM weight_corrections WHERE id IN (SELECT MAX(id) FROM weight_corrections WHERE fact_id IN ("
                                + placeholders + ") GROUP BY fact_id) ORDER BY id", tuple(fact_ids))
