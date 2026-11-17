import tempfile
import unittest
from pathlib import Path

from looplake.fixtures import balanced_batch, event, seed_dimensions
from looplake.model import LakeError
from looplake.service import LakeService


class CalibrationBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = LakeService(Path(self.temp.name) / "lake.sqlite3")
        seed_dimensions(self.service)

    def put(self, rows):
        return [self.service.ingest(row) for row in rows]

    def certificate(self, certificate_id="cert-1", start="2026-07-01", end="2026-07-31",
                    factor="1.05", device="scale-1", recorded_at=None):
        data = {"device_id": device, "certificate_id": certificate_id,
                "valid_from": start, "valid_to": end, "factor": factor}
        if recorded_at is not None:
            data["recorded_at"] = recorded_at
        return self.service.put_calibration(data)

    def scale_batch(self, batch_id="batch-1", prefix="a", deposit="10", raw="10"):
        rows = balanced_batch(batch_id, prefix, weight=deposit)
        rows[1]["weight_kg"] = raw
        return self.put(rows)


class CertificateTests(CalibrationBase):
    def test_certificate_required_fields_and_device(self):
        with self.assertRaises(LakeError) as failure:
            self.certificate(device="missing")
        self.assertEqual(failure.exception.code, "unknown_device")
        for invalid in ({"factor": "0"}, {"factor": "-1"}, {"factor": "NaN"},
                        {"valid_from": "2026-07-31", "valid_to": "2026-07-01"},
                        {"certificate_id": "bad id"}):
            data = {"device_id": "scale-1", "certificate_id": "c1",
                    "valid_from": "2026-07-01", "valid_to": "2026-07-31", "factor": "1"}
            data.update(invalid)
            with self.subTest(invalid=invalid), self.assertRaises(LakeError):
                self.service.put_calibration(data)

    def test_certificate_conflict_and_idempotence(self):
        first = self.certificate()
        self.assertEqual(first["state"], "valid")
        self.assertEqual(self.certificate()["calibration_id"], first["calibration_id"])
        with self.assertRaises(LakeError) as failure:
            self.certificate(factor="1.1")
        self.assertEqual(failure.exception.code, "calibration_conflict")

    def test_boundary_days_are_inclusive_and_flagged(self):
        self.certificate(start="2026-07-15", end="2026-07-16")
        self.put([event("weigh_in", "on-start", weight="1",
                        happened_at="2026-07-15T00:00:00Z"),
                  event("weigh_in", "on-end", weight="1",
                        happened_at="2026-07-16T23:59:59Z")])
        state = self.service.calibration.state("scale-1")
        self.assertEqual(state["readings"], 2)
        first = self.service.batch_status("batch-1")
        self.assertEqual(len(first["calibration_boundary_fact_ids"]), 2)
        self.assertEqual(first["calibration_boundary_fact_ids"],
                         sorted(row["id"] for row in
                                self.service.store.facts("WHERE device_id='scale-1'")))

    def test_reading_carries_raw_and_booked_weight(self):
        self.certificate(factor="1.05")
        outcomes = self.scale_batch(raw="10")
        weigh_in = outcomes[1]["fact_id"]
        fact = self.service.store.fact(weigh_in)
        self.assertEqual(fact["raw_weight_kg"], "10")
        self.assertEqual(fact["booked_weight_kg"], "10.5")
        self.assertEqual(fact["weight_kg"], "10.5")
        self.assertEqual(fact["calibration"]["certificate_id"], "cert-1")

    def test_device_without_certificates_is_untouched(self):
        outcomes = self.scale_batch(raw="10")
        fact = self.service.store.fact(outcomes[1]["fact_id"])
        self.assertEqual(fact["weight_kg"], "10")
        self.assertEqual(fact["calibration"], {})
        self.assertTrue(self.service.batch_status("batch-1")["ready"])

    def test_gap_between_certificates_is_unverified(self):
        self.certificate(start="2026-07-01", end="2026-07-10")
        outcomes = self.service.ingest(event("weigh_in", "outside", weight="10",
                                             happened_at="2026-07-15T10:00:00+08:00"))
        self.assertEqual(outcomes["state"], "normalized")
        fact = self.service.store.fact(outcomes["fact_id"])
        self.assertEqual(fact["weight_kg"], "10")
        issues = self.service.batch_status("batch-1")["issues"]
        codes = [issue["code"] for issue in issues]
        self.assertIn("calibration_unverified", codes)
        issue = next(x for x in issues if x["code"] == "calibration_unverified")
        self.assertEqual(issue["fact_ids"], [outcomes["fact_id"]])


class RevocationTests(CalibrationBase):
    def test_revoke_blocks_batch_and_is_idempotent(self):
        self.certificate(factor="1.05")
        self.scale_batch(raw="10")
        self.assertTrue(self.service.batch_status("batch-1")["ready"])
        revoked = self.service.revoke_calibration("scale-1", "cert-1", "巡检失效",
                                                  "2026-08-05T10:00:00Z")
        self.assertFalse(revoked["already_revoked"])
        self.assertEqual(revoked["state"], "revoked")
        again = self.service.revoke_calibration("scale-1", "cert-1")
        self.assertTrue(again["already_revoked"])
        issues = self.service.batch_status("batch-1")["issues"]
        self.assertIn("calibration_revoked", [x["code"] for x in issues])
        self.assertEqual(next(x for x in issues if x["code"] == "calibration_revoked")["fact_ids"], [2])
        self.assertFalse(self.service.preview("2026-07")["ready"])

    def test_revoke_unknown_certificate(self):
        with self.assertRaises(LakeError) as failure:
            self.service.revoke_calibration("scale-1", "nope")
        self.assertEqual(failure.exception.code, "unknown_calibration")
        self.assertEqual(failure.exception.status, 404)

    def test_impact_is_read_only_and_recomputes(self):
        self.certificate(factor="1.05")
        outcomes = self.scale_batch(raw="10")
        self.service.revoke_calibration("scale-1", "cert-1", "巡检失效")
        before = self.service.store.fact(outcomes[1]["fact_id"])
        impact = self.service.calibration_impact("scale-1", "2026-07-01", "2026-07-31")
        after = self.service.store.fact(outcomes[1]["fact_id"])
        self.assertEqual(before["weight_kg"], after["weight_kg"])
        self.assertFalse(impact["historical_release_modified"])
        self.assertTrue(impact["read_only"])
        reading = impact["readings"][0]
        self.assertEqual(reading["raw_weight_kg"], "10")
        self.assertEqual(reading["booked_weight_kg"], "10.5")
        self.assertIsNone(reading["proposed_weight_kg"])
        self.assertEqual(reading["basis"], "uncalibrated")
        self.assertFalse(impact["month_eligible_for_release"])
        self.assertEqual(impact["batches"][0]["issues"][0]["code"], "calibration_unavailable")

    def test_reissue_recomputes_proposed_weight(self):
        self.certificate(factor="1.05")
        self.scale_batch(raw="10")
        self.service.revoke_calibration("scale-1", "cert-1", "巡检失效")
        self.certificate("cert-2", factor="1", recorded_at="2026-08-06T09:00:00Z")
        impact = self.service.calibration_impact("scale-1", "2026-07-01", "2026-07-31")
        reading = impact["readings"][0]
        self.assertEqual(reading["certificate_id"], "cert-2")
        self.assertEqual(reading["proposed_weight_kg"], "10")
        self.assertEqual(reading["delta_kg"], "-0.5")
        self.assertTrue(reading["notice_after_month_close"])
        self.assertEqual([x["code"] for x in impact["batches"][0]["issues"]],
                         ["calibration_basis_changed"])

    def test_recalibration_correction_is_append_only(self):
        self.certificate(factor="1.05")
        outcomes = self.scale_batch(raw="10")
        fact_id = outcomes[1]["fact_id"]
        self.service.revoke_calibration("scale-1", "cert-1", "巡检失效")
        cert = self.certificate("cert-2", factor="1")
        correction = self.service.calibrate(fact_id, calibration_id=cert["calibration_id"])
        self.assertTrue(correction["changed"])
        self.assertEqual(correction["new_weight_kg"], "10")
        fact = self.service.store.fact(fact_id)
        self.assertEqual(fact["booked_weight_kg"], "10.5")
        self.assertEqual(fact["raw_weight_kg"], "10")
        self.assertEqual(fact["weight_kg"], "10")
        history = self.service.correction.history(fact_id)
        self.assertEqual(len(history["corrections"]), 1)
        self.assertEqual(history["corrections"][0]["certificate_id"], "cert-2")
        self.assertTrue(self.service.batch_status("batch-1")["ready"])
        again = self.service.calibrate(fact_id, calibration_id=cert["calibration_id"])
        self.assertFalse(again["changed"])

    def test_correction_rejects_wrong_certificate_and_period(self):
        outcomes = self.scale_batch(raw="10")
        fact_id = outcomes[1]["fact_id"]
        self.certificate("aug", start="2026-08-01", end="2026-08-31")
        with self.assertRaises(LakeError) as failure:
            self.service.calibrate(fact_id, calibration_id=1)
        self.assertEqual(failure.exception.code, "calibration_period")
        self.certificate("other", device="box-2")
        with self.assertRaises(LakeError) as failure:
            self.service.calibrate(fact_id, calibration_id=2)
        self.assertEqual(failure.exception.code, "calibration_device")
        with self.assertRaises(LakeError) as failure:
            self.service.calibrate(outcomes[0]["fact_id"], calibration_id=None, new_weight="9")
        self.assertEqual(failure.exception.code, "not_a_scale_reading")
        self.certificate("ok", factor="1.05")
        with self.assertRaises(LakeError) as failure:
            self.service.calibrate(fact_id, "12", calibration_id=3)
        self.assertEqual(failure.exception.code, "calibration_mismatch")

    def test_manual_correction_requires_weight(self):
        outcomes = self.scale_batch()
        with self.assertRaises(LakeError) as failure:
            self.service.calibrate(outcomes[1]["fact_id"])
        self.assertEqual(failure.exception.code, "missing_correction_basis")
        corrected = self.service.calibrate(outcomes[1]["fact_id"], "9.5", note="现场复称")
        self.assertEqual(corrected["new_weight_kg"], "9.5")
        self.assertEqual(self.service.store.fact(outcomes[1]["fact_id"])["weight_kg"], "9.5")


class OverlapAndNoticeTests(CalibrationBase):
    def test_overlap_is_flagged_and_resolved_deterministically(self):
        self.certificate("cert-b", factor="1.05")
        self.certificate("cert-a", factor="1.02")
        rows = self.service.calibration.certificates("scale-1")
        self.assertEqual([row["certificate_id"] for row in rows], ["cert-a", "cert-b"])
        effective = self.service.calibration.effective("scale-1", "2026-07-15")
        self.assertTrue(effective["ambiguous"])
        self.assertEqual(effective["certificate_id"], "cert-a")
        self.assertEqual([c["certificate_id"] for c in effective["candidates"]],
                         ["cert-a", "cert-b"])
        self.scale_batch(raw="10")
        issues = self.service.batch_status("batch-1")["issues"]
        self.assertIn("calibration_overlap", [x["code"] for x in issues])
        state = self.service.calibration.state("scale-1")
        self.assertEqual(state["overlapping_certificates"], [["cert-a", "cert-b"]])
        self.assertFalse(self.service.preview("2026-07")["ready"])

    def test_notice_after_month_close_is_traceable(self):
        self.certificate(factor="1.05", recorded_at="2026-08-05T00:00:00Z")
        self.scale_batch(raw="10")
        impact = self.service.calibration_impact("scale-1", "2026-07-01", "2026-07-31")
        notice = impact["calibration_notice_after_month_close"]
        self.assertEqual(len(notice), 1)
        self.assertEqual(notice[0]["certificate_id"], "cert-1")
        self.assertEqual(self.service.calibration.pending_notice("scale-1", "2026-07"),
                         ["cert-1"])

    def test_impact_scopes_to_material_and_range(self):
        self.certificate(factor="1.05")
        self.scale_batch(raw="10")
        self.put([event("weigh_in", "glass-in", material_id="glass", weight="4")])
        all_rows = self.service.calibration_impact("scale-1", "2026-07-01", "2026-07-31")
        self.assertEqual(len(all_rows["readings"]), 2)
        scoped = self.service.calibration_impact("scale-1", "2026-07-01", "2026-07-31",
                                                 material_id="glass")
        self.assertEqual(len(scoped["readings"]), 1)
        self.assertEqual(scoped["readings"][0]["event_type"], "weigh_in")
        with self.assertRaises(LakeError) as failure:
            self.service.calibration_impact("scale-1", "2026-07-31", "2026-07-01")
        self.assertEqual(failure.exception.code, "invalid_range")


class ReleaseImmutabilityTests(CalibrationBase):
    def test_frozen_release_survives_revocation_and_reissue(self):
        self.certificate(factor="1.05")
        outcomes = self.scale_batch(raw="10")
        preview = self.service.preview("2026-07")
        self.assertTrue(preview["ready"])
        release = self.service.publish("july-final", "2026-07", preview["fingerprint"])
        frozen_before = self.service.releases.get(release["release_id"])

        self.service.revoke_calibration("scale-1", "cert-1", "巡检失效")
        self.certificate("cert-2", factor="1", recorded_at="2026-08-06T00:00:00Z")
        self.service.calibrate(outcomes[1]["fact_id"], calibration_id=2)

        frozen_after = self.service.releases.get(release["release_id"])
        self.assertEqual(frozen_before["fingerprint"], frozen_after["fingerprint"])
        self.assertEqual(frozen_after["content"]["mart"]["rows"][0]["deposit_kg"], "10")
        self.assertEqual(frozen_after["content"]["reconciliation"]["batches"][0]["weigh_in_kg"], "10.5")

        # 当前视图已重算，并且必须发布新版本而不是改旧版。
        self.assertEqual(self.service.batch_status("batch-1")["weigh_in_kg"], "10")
        current = self.service.preview("2026-07")
        self.assertTrue(current["ready"])
        self.assertNotEqual(current["fingerprint"], preview["fingerprint"])
        with self.assertRaises(LakeError) as failure:
            self.service.publish("july-final", "2026-07", current["fingerprint"])
        self.assertEqual(failure.exception.code, "release_conflict")
        second = self.service.publish("july-final-v2", "2026-07", current["fingerprint"])
        self.assertTrue(second["created"])
        self.assertEqual(self.service.snapshots.releases_for_month("2026-07")["releases"][-1]["name"],
                         "july-final-v2")
        diff = self.service.releases.compare(release["release_id"], second["release_id"])
        self.assertEqual(diff["changes"], [])

    def test_revocation_blocks_publish_until_rebooked(self):
        self.certificate(factor="1.05")
        self.scale_batch(raw="10")
        fingerprint = self.service.preview("2026-07")["fingerprint"]
        self.service.revoke_calibration("scale-1", "cert-1", "巡检失效")
        with self.assertRaises(LakeError) as failure:
            self.service.publish("july", "2026-07", fingerprint)
        self.assertEqual(failure.exception.code, "unresolved_month")
        self.assertEqual(self.service.snapshots.releases_for_month("2026-07")["releases"], [])

    def test_impact_reports_frozen_release_and_inventory(self):
        self.certificate(factor="1.05")
        self.scale_batch(raw="10")
        preview = self.service.preview("2026-07")
        release = self.service.publish("july-final", "2026-07", preview["fingerprint"])
        self.service.revoke_calibration("scale-1", "cert-1", "巡检失效")
        self.certificate("cert-2", factor="1", recorded_at="2026-08-06T00:00:00Z")
        impact = self.service.calibration_impact("scale-1", "2026-07-01", "2026-07-31")
        self.assertEqual([row["id"] for row in impact["frozen_releases"]], [release["release_id"]])
        self.assertEqual(impact["inventory"]["station_materials"][0]["closing_kg"], "10")
        self.assertTrue(impact["inventory"]["ready"])
        self.assertEqual(self.service.inventory.station_day("station-1", "foam", "2026-07-15")["closing_kg"], "10.5")


class ApiCalibrationTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from fastapi.testclient import TestClient
        from looplake.api import create_app
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.client = TestClient(create_app(Path(self.temp.name) / "api.sqlite3"))
        seed_dimensions(self.client.app.state.service)

    def test_api_certificate_correction_flow(self):
        posted = self.client.post("/calibrations", json={
            "device_id": "scale-1", "certificate_id": "cert-api",
            "valid_from": "2026-07-01", "valid_to": "2026-07-31", "factor": "1.05"})
        self.assertEqual(posted.status_code, 200)
        self.assertEqual(self.client.get("/devices/scale-1/calibrations").json()[0]["factor"], "1.05")
        event = self.client.post("/events", json={
            "source": "scale-feed", "external_id": "w-api", "event_type": "weigh_in",
            "happened_at": "2026-07-15T10:00:00+08:00", "batch_id": "batch-api",
            "channel": "scale", "device_id": "scale-1", "station_id": "station-1",
            "material_id": "foam", "weight_kg": "10"}).json()
        fact_id = event["fact_id"]
        self.assertEqual(self.client.get(f"/facts/{fact_id}").json()["raw_weight_kg"], "10")
        revoked = self.client.post("/devices/scale-1/calibrations/cert-api/revoke",
                                   json={"reason": "巡检失效"})
        self.assertEqual(revoked.status_code, 200)
        self.assertEqual(revoked.json()["state"], "revoked")
        impact = self.client.get("/devices/scale-1/calibration-impact",
                                 params={"start": "2026-07-01", "end": "2026-07-31"}).json()
        self.assertFalse(impact["month_eligible_for_release"])
        self.assertEqual(impact["readings"][0]["basis"], "uncalibrated")
        corrected = self.client.post(f"/facts/{fact_id}/corrections",
                                     json={"new_weight_kg": "9.8", "note": "复称"})
        self.assertEqual(corrected.status_code, 200)
        history = self.client.get(f"/facts/{fact_id}/corrections").json()
        self.assertEqual(history["booked_weight_kg"], "10.5")
        self.assertEqual(history["weight_kg"], "9.8")
        self.assertEqual(self.client.post("/calibrations", json={
            "device_id": "missing", "certificate_id": "c", "valid_from": "2026-07-01",
            "valid_to": "2026-07-31", "factor": "1"}).status_code, 409)

    def test_api_impact_rejects_bad_range(self):
        response = self.client.get("/devices/scale-1/calibration-impact",
                                   params={"start": "2026-07-31", "end": "2026-07-01"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "invalid_range")


if __name__ == "__main__":
    unittest.main()
