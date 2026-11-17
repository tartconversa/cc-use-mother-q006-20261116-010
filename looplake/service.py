from __future__ import annotations

from .batch import BatchImport
from .calibration import Calibration
from .correction import Correction, Review
from .dimensions import Dimensions
from .landing import Landing
from .ledger import Ledger
from .lineage import Lineage
from .mart import Mart
from .quality import Quality
from .reconcile import Reconciliation
from .release import Release
from .store import Store
from .settlement import Settlement
from .inventory import Inventory
from .query import Query
from .snapshot import SnapshotAudit


class LakeService:
    def __init__(self, database_path):
        self.store = Store(database_path)
        self.dimensions = Dimensions(self.store)
        self.landing = Landing(self.store, self.dimensions)
        self.calibration = Calibration(self.store)
        self.ledger = Ledger(self.store, self.dimensions, self.landing, self.calibration)
        self.inventory = Inventory(self.ledger)
        self.query = Query(self.ledger)
        self.settlement = Settlement(self.store)
        self.reconciliation = Reconciliation(self.ledger, self.settlement)
        self.mart = Mart(self.ledger, self.reconciliation)
        self.releases = Release(self.store, self.mart, self.reconciliation, self.inventory)
        self.snapshots = SnapshotAudit(self.store)
        self.correction = Correction(self.store)
        self.review = Review(self.store, self.calibration)
        self.quality = Quality(self.store, self.landing, self.ledger,
                               self.reconciliation, self.mart)
        self.lineage = Lineage(self.store, self.ledger, self.reconciliation,
                               self.mart, self.releases, self.calibration)
        self.batches = BatchImport(self.ledger)

    def register_source(self, source, channel, expected_version=None):
        return self.dimensions.register_source(source, channel, expected_version)

    def register_dimension(self, data, expected_version=None):
        return self.dimensions.put(data, expected_version)

    def ingest(self, data):
        return self.ledger.ingest(data)

    def put_calibration(self, data):
        return self.calibration.put(data)

    def revoke_calibration(self, device_id, certificate_id, reason=None, notified_at=None):
        return self.calibration.revoke(device_id, certificate_id, reason, notified_at)

    def calibrate(self, fact_id, new_weight=None, calibration_id=None, note=None):
        return self.calibration.correct(fact_id, new_weight, calibration_id, note)

    def calibration_impact(self, device_id, start, end, material_id=None, tolerance_kg="0.5"):
        return self.calibration.impact(device_id, start, end, material_id, tolerance_kg)

    def retry(self, ingestion_id):
        return self.ledger.normalize(ingestion_id)

    def batch_status(self, batch_id):
        return self.reconciliation.batch(batch_id)

    def month_status(self, month):
        return self.quality.month(month)

    def month_report(self, month):
        return self.mart.month(month)

    def preview(self, month):
        return self.releases.preview(month)

    def publish(self, name, month, fingerprint):
        return self.releases.publish(name, month, fingerprint)
