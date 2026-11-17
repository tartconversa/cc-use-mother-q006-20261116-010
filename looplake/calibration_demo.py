"""本地合成验收场景：失效校准证书、临界日、重叠、通知晚于月结与重新校准。

运行：python -m looplake.calibration_demo
所有数据均为合成，路径使用临时目录，不触碰现有库。
"""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import json

from .fixtures import balanced_batch, seed_dimensions
from .service import LakeService


def _batch(service, batch_id, prefix, raw="10", deposit="10", payout="0.8",
           channel_date="2026-07-15T10:00:00+08:00"):
    rows = balanced_batch(batch_id, prefix, weight=deposit, payout=payout,
                          happened_at=channel_date)
    rows[1]["weight_kg"] = raw
    return [service.ingest(row) for row in rows]


def run_demo():
    with TemporaryDirectory() as folder:
        service = LakeService(Path(folder) / "lake.sqlite3")
        seed_dimensions(service)

        # 2026-07 的证书系数 1.05，巡检在 8 月才通知撤销。
        service.put_calibration({"device_id": "scale-1", "certificate_id": "cert-jul",
                                 "valid_from": "2026-07-01", "valid_to": "2026-07-31",
                                 "factor": "1.05", "recorded_at": "2026-07-01T00:00:00Z"})
        first = _batch(service, "batch-1", "a")
        preview_before = service.preview("2026-07")
        release = service.publish("2026-07-final", "2026-07", preview_before["fingerprint"])
        frozen = service.releases.get(release["release_id"])

        service.revoke_calibration("scale-1", "cert-jul", "巡检发现系数失效",
                                   "2026-08-05T09:00:00Z")
        revoked_impact = service.calibration_impact("scale-1", "2026-07-01", "2026-07-31")

        # 重新校准：同区间补发系数 1.0 的新证书，通知晚于月结。
        service.put_calibration({"device_id": "scale-1", "certificate_id": "cert-aug",
                                 "valid_from": "2026-07-01", "valid_to": "2026-07-31",
                                 "factor": "1.0", "recorded_at": "2026-08-06T09:00:00Z"})
        reissued = service.calibration_impact("scale-1", "2026-07-01", "2026-07-31")
        correction = service.calibrate(first[1]["fact_id"], calibration_id=2)
        current = service.preview("2026-07")
        second = service.publish("2026-07-final-v2", "2026-07", current["fingerprint"])

        # 临界日：证书首日读数会被标记但不阻断。
        service.put_calibration({"device_id": "scale-1", "certificate_id": "cert-aug-real",
                                 "valid_from": "2026-08-01", "valid_to": "2026-08-31",
                                 "factor": "1.0"})
        boundary = _batch(service, "batch-2", "boundary", raw="5", deposit="5", payout="0.4",
                          channel_date="2026-08-01T08:00:00+08:00")
        boundary_status = service.batch_status("batch-2")

        # 证书区间重叠：两张证书都覆盖 9 月，按 certificate_id 顺序给出候选。
        service.put_calibration({"device_id": "scale-1", "certificate_id": "cert-sep-a",
                                 "valid_from": "2026-09-01", "valid_to": "2026-09-30",
                                 "factor": "1.0"})
        service.put_calibration({"device_id": "scale-1", "certificate_id": "cert-sep-b",
                                 "valid_from": "2026-09-15", "valid_to": "2026-10-15",
                                 "factor": "1.02"})
        overlap = service.calibration.effective("scale-1", "2026-09-20")
        overlap_state = service.calibration.state("scale-1")

        return {
            "normalized_fact_ids": [row["fact_id"] for row in first],
            "frozen_release": {"id": frozen["id"], "fingerprint": frozen["fingerprint"],
                               "weigh_in_kg": frozen["content"]["reconciliation"]["batches"][0]["weigh_in_kg"]},
            "revoked_impact": {
                "readings": revoked_impact["readings"],
                "batch_issue_codes": [x["code"] for x in revoked_impact["batches"][0]["issues"]],
                "month_eligible_for_release": revoked_impact["month_eligible_for_release"]},
            "reissued_impact": {
                "proposed_weight_kg": reissued["readings"][0]["proposed_weight_kg"],
                "delta_kg": reissued["readings"][0]["delta_kg"],
                "notice_after_month_close": [x["certificate_id"] for x
                                             in reissued["calibration_notice_after_month_close"]],
                "batch_issue_codes": [x["code"] for x in reissued["batches"][0]["issues"]]},
            "correction": correction,
            "fact_after_correction": {
                "raw_weight_kg": service.store.fact(first[1]["fact_id"])["raw_weight_kg"],
                "booked_weight_kg": service.store.fact(first[1]["fact_id"])["booked_weight_kg"],
                "weight_kg": service.store.fact(first[1]["fact_id"])["weight_kg"]},
            "new_release": {"id": second["release_id"], "created": second["created"]},
            "frozen_unchanged": (service.releases.get(release["release_id"])["fingerprint"]
                                 == frozen["fingerprint"]),
            "boundary_fact_ids": [row["fact_id"] for row in boundary],
            "boundary_flagged": boundary_status["calibration_boundary_fact_ids"],
            "boundary_batch_ready": boundary_status["ready"],
            "overlap": {"certificate_id": overlap["certificate_id"],
                        "ambiguous": overlap["ambiguous"],
                        "candidates": [x["certificate_id"] for x in overlap["candidates"]],
                        "pairs": overlap_state["overlapping_certificates"]},
        }


if __name__ == "__main__":
    print(json.dumps(run_demo(), ensure_ascii=False, sort_keys=True, indent=2))
