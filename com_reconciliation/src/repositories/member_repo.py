"""Data-access for the members table (internal ledger / reference rows)."""
from __future__ import annotations

import hashlib

from models.database import Member


def _dedup_key(row: dict) -> str:
    if row.get("tranx_id"):
        return f"tranx:{row['tranx_id']}"
    parts = [
        str(row.get("member_id") or ""),
        str(row.get("transaction_date") or ""),
        str(row.get("credit") or ""),
        str(row.get("debit") or ""),
        str(row.get("source_file") or ""),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


class MemberRepository:
    def __init__(self, session_factory):
        self.Session = session_factory
        self._seen_keys = set()

    def insert_if_new(self, row: dict) -> bool:
        key = _dedup_key(row)
        with self.Session() as session:
            if row.get("tranx_id"):
                existing = session.query(Member).filter(Member.tranx_id == row["tranx_id"]).first()
                if existing is not None:
                    return False
            elif key in self._seen_keys:
                return False

            member = Member(
                member_id=row.get("member_id"),
                tranx_id=row.get("tranx_id"),
                transaction_number=row.get("transaction_number"),
                member_name=row.get("member_name"),
                branch=row.get("branch"),
                scheme=row.get("scheme"),
                payment_mode=row.get("payment_mode"),
                transaction_type=row.get("transaction_type"),
                credit=row.get("credit"),
                debit=row.get("debit"),
                o_balance=row.get("o_balance"),
                c_balance=row.get("c_balance"),
                transaction_date=row.get("transaction_date"),
                source_file=row.get("source_file"),
            )
            session.add(member)
            session.commit()
            self._seen_keys.add(key)
            return True

    def get_all(self):
        with self.Session() as session:
            return session.query(Member).all()

    def get_by_member_id(self, member_id: str):
        with self.Session() as session:
            return session.query(Member).filter(Member.member_id == member_id).all()
