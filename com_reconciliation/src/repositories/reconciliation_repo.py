"""Data-access for persisted reconciliation outcomes."""
from __future__ import annotations

from datetime import datetime

from models.database import ReconciliationRecord


class ReconciliationRepository:
    def __init__(self, session_factory):
        self.Session = session_factory

    def save_all(self, records: list) -> int:
        """Persist a batch of reconciliation-result dicts (as produced by
        ReconciliationEngine). Returns the number of rows written."""
        if not records:
            return 0
        with self.Session() as session:
            count = 0
            for r in records:
                date_val = r.get("date")
                if isinstance(date_val, str):
                    try:
                        date_val = datetime.fromisoformat(date_val).date()
                    except ValueError:
                        date_val = None
                session.add(ReconciliationRecord(
                    date=date_val,
                    transaction_id=r.get("transaction_id"),
                    member_id=r.get("member_id"),
                    member_name=r.get("member_name"),
                    branch=r.get("branch"),
                    amount=r.get("amount"),
                    cr_dr=r.get("cr_dr"),
                    payment_type=r.get("payment_type"),
                    status=r.get("status"),
                    matched_on=r.get("matched_on"),
                    issues="; ".join(r.get("issues") or []),
                    expected=r.get("expected"),
                    actual=r.get("actual"),
                ))
                count += 1
            session.commit()
            return count

    def get_history(self, start_date=None, end_date=None, status=None):
        with self.Session() as session:
            q = session.query(ReconciliationRecord)
            if start_date:
                q = q.filter(ReconciliationRecord.date >= start_date)
            if end_date:
                q = q.filter(ReconciliationRecord.date <= end_date)
            if status:
                q = q.filter(ReconciliationRecord.status == status)
            return q.order_by(ReconciliationRecord.date.desc()).all()
