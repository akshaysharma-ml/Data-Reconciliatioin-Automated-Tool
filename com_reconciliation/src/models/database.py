"""
Database setup + schema.

Two tables carry the reconciliation:

- Member           -> one row per line of the internal transactions-report.xlsx
                      ("reference" data in the Streamlit UI). Keyed by
                      `member_id`, which is that file's `Account No` column.
- Transaction       -> one row per line of an uploaded bank/UPI statement
                      ("source" data in the Streamlit UI). `member_id_list`
                      holds every Policy No/Member ID found in that bank row
                      (some cells contain more than one, split on load).

Match key: Transaction.member_id_list (any entry) == Member.member_id
See utils/parsers.py for where each column is read from, per bank format.
"""
from __future__ import annotations

from datetime import datetime, date as date_cls
from typing import Optional

from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    Float,
    Date,
    DateTime,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import StaticPool

Base = declarative_base()


def _f(value) -> Optional[float]:
    """Safe float conversion; None/NaN/'' -> None instead of raising."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f


def _iso(value) -> Optional[str]:
    """Safe isoformat for date/datetime columns that may be None."""
    if value is None:
        return None
    if isinstance(value, (datetime, date_cls)):
        return value.isoformat()
    return str(value)


class Member(Base):
    """One row of the internal ledger (transactions-report.xlsx)."""

    __tablename__ = "members"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # --- match key ---
    member_id = Column(String(64), nullable=False, index=True)  # = "Account No", normalized

    # --- identifiers carried for display / audit only, never matched on ---
    tranx_id = Column(String(64), nullable=True, index=True, unique=True)
    transaction_number = Column(String(64), nullable=True)

    # --- descriptive fields ---
    member_name = Column(String(255), nullable=True)
    branch = Column(String(255), nullable=True)
    scheme = Column(String(255), nullable=True)
    payment_mode = Column(String(64), nullable=True)
    transaction_type = Column(String(16), nullable=True)  # "credit" / "debit"

    credit = Column(Float, nullable=True)
    debit = Column(Float, nullable=True)
    o_balance = Column(Float, nullable=True)
    c_balance = Column(Float, nullable=True)

    transaction_date = Column(Date, nullable=True, index=True)

    source_file = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    def amount(self) -> float:
        return _f(self.credit) or _f(self.debit) or 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "member_id": self.member_id,
            "tranx_id": self.tranx_id,
            "transaction_number": self.transaction_number,
            "member_name": self.member_name,
            "branch": self.branch,
            "scheme": self.scheme,
            "payment_mode": self.payment_mode,
            "transaction_type": self.transaction_type,
            "credit": _f(self.credit),
            "debit": _f(self.debit),
            "o_balance": _f(self.o_balance),
            "c_balance": _f(self.c_balance),
            "transaction_date": _iso(self.transaction_date),
            "source_file": self.source_file,
        }


class Transaction(Base):
    """One row of an uploaded bank / UPI statement."""

    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # --- bank's own reference, display/audit only ---
    transaction_id = Column(String(128), nullable=True, index=True)

    value_date = Column(Date, nullable=True, index=True)
    txn_posted_date = Column(DateTime, nullable=True)
    description = Column(Text, nullable=True)

    cr_dr = Column(String(4), nullable=True)  # "CR" / "DR"
    transaction_amount = Column(Float, nullable=True)
    available_balance = Column(Float, nullable=True)

    name = Column(String(255), nullable=True)

    # comma-joined, normalized (upper/stripped) member IDs found in the
    # "Policy No/ Member ID" cell (a cell can list more than one)
    member_id_list = Column(Text, nullable=True)

    branch = Column(String(255), nullable=True)
    transaction_type = Column(String(64), nullable=True)  # bank's own label, e.g. "COLLECTION"

    source = Column(String(128), nullable=True)  # which bank/file this came from
    source_file = Column(String(255), nullable=True)

    # de-dup fingerprint: hash of (source, transaction_id or row content, date, amount)
    dedup_key = Column(String(128), nullable=True, index=True, unique=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    def member_ids(self) -> list:
        if not self.member_id_list:
            return []
        return [m for m in self.member_id_list.split(",") if m]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "transaction_id": self.transaction_id,
            "value_date": _iso(self.value_date),
            "txn_posted_date": _iso(self.txn_posted_date),
            "description": self.description,
            "cr_dr": self.cr_dr,
            "transaction_amount": _f(self.transaction_amount),
            "available_balance": _f(self.available_balance),
            "name": self.name,
            "member_ids": self.member_ids(),
            "branch": self.branch,
            "transaction_type": self.transaction_type,
            "source": self.source,
            "source_file": self.source_file,
        }


class ReconciliationRecord(Base):
    """One reconciliation outcome for one bank transaction row."""

    __tablename__ = "reconciliation_records"

    id = Column(Integer, primary_key=True, autoincrement=True)

    date = Column(Date, nullable=False, index=True)
    transaction_id = Column(String(128), nullable=True)
    member_id = Column(String(64), nullable=True, index=True)
    member_name = Column(String(255), nullable=True)
    branch = Column(String(255), nullable=True)

    amount = Column(Float, nullable=True)
    cr_dr = Column(String(4), nullable=True)
    payment_type = Column(String(128), nullable=True)  # source bank/file label

    status = Column(String(24), nullable=False, index=True)  # matched / partially_matched / unmatched
    matched_on = Column(String(64), nullable=True)
    issues = Column(Text, nullable=True)  # "; "-joined
    expected = Column(String(255), nullable=True)
    actual = Column(String(255), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "date": _iso(self.date),
            "transaction_id": self.transaction_id,
            "member_id": self.member_id,
            "member_name": self.member_name,
            "branch": self.branch,
            "amount": _f(self.amount),
            "cr_dr": self.cr_dr,
            "payment_type": self.payment_type,
            "status": self.status,
            "matched_on": self.matched_on,
            "issues": self.issues.split("; ") if self.issues else [],
            "expected": self.expected,
            "actual": self.actual,
        }


def create_db_engine(database_url: str):
    """Create a SQLAlchemy engine. Pool args only apply to non-SQLite URLs;
    ':memory:' SQLite gets a StaticPool so every connection shares one DB."""
    is_sqlite = database_url.startswith("sqlite")
    is_memory = ":memory:" in database_url

    kwargs = {}
    if is_sqlite:
        kwargs["connect_args"] = {"check_same_thread": False}
        if is_memory:
            kwargs["poolclass"] = StaticPool
    else:
        kwargs["pool_size"] = 20
        kwargs["max_overflow"] = 30

    engine = create_engine(database_url, **kwargs)
    Base.metadata.create_all(engine)
    return engine


def get_session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)
