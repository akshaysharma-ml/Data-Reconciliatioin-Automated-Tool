"""Data-access for the transactions table (bank/UPI statement rows)."""
from __future__ import annotations

import hashlib
from typing import Optional

from models.database import Transaction


def _dedup_key(row: dict) -> str:
    """Fingerprint used to skip a row already loaded (e.g. the same file
    uploaded twice, or the same transaction present in two source files)."""
    parts = [
        str(row.get("source") or ""),
        str(row.get("transaction_id") or ""),
        str(row.get("value_date") or ""),
        str(row.get("transaction_amount") or ""),
        ",".join(sorted(row.get("member_ids") or [])),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


class TransactionRepository:
    def __init__(self, session_factory):
        self.Session = session_factory

    def insert_if_new(self, row: dict) -> bool:
        """Insert one parsed transaction row. Returns True if inserted,
        False if it was a duplicate (skipped)."""
        key = _dedup_key(row)
        with self.Session() as session:
            existing = session.query(Transaction).filter(Transaction.dedup_key == key).first()
            if existing is not None:
                return False
            txn = Transaction(
                transaction_id=row.get("transaction_id"),
                value_date=row.get("value_date"),
                description=row.get("description"),
                cr_dr=row.get("cr_dr"),
                transaction_amount=row.get("transaction_amount"),
                available_balance=row.get("available_balance"),
                name=row.get("name"),
                member_id_list=",".join(row.get("member_ids") or []),
                branch=row.get("branch"),
                transaction_type=row.get("transaction_type"),
                source=row.get("source"),
                source_file=row.get("source_file"),
                dedup_key=key,
            )
            session.add(txn)
            session.commit()
            return True

    def get_all(self):
        with self.Session() as session:
            return session.query(Transaction).all()

    def get_by_date(self, target_date):
        with self.Session() as session:
            return session.query(Transaction).filter(Transaction.value_date == target_date).all()

    def distinct_dates(self):
        with self.Session() as session:
            rows = (
                session.query(Transaction.value_date)
                .filter(Transaction.value_date.isnot(None))
                .distinct()
                .all()
            )
            return sorted({r[0] for r in rows if r[0] is not None})
