"""
DataLoader   - reads uploaded files into the DB via the repositories.
ReconciliationEngine - does the actual matching, per the keys confirmed
                        against the real files:
                          primary key:  Transaction.member_id  (any ID split
                                        out of "Policy No/ Member ID")
                                        == Member.member_id ("Account No")
                          confidence:   + same date, + same amount
                        Reference-only fields (Transaction ID/Txn No./
                        Reference No/RRN/SETTLEMENT UTR NUMBER on the bank
                        side, Tranx ID/Transaction Number on the ledger
                        side) are carried through for display but never
                        used to decide a match - direct testing against
                        the real files showed under 15% overlap on those
                        fields, so they aren't reliable keys.
ReconciliationService - thin facade the Streamlit app / API call.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date as date_cls
from pathlib import Path
from typing import Optional

from sqlalchemy import or_

from models.database import Member, Transaction, ReconciliationRecord, create_db_engine, get_session_factory
from repositories.transaction_repo import TransactionRepository
from repositories.member_repo import MemberRepository
from repositories.reconciliation_repo import ReconciliationRepository
from utils.parsers import parse_transaction_file, parse_member_file, ParseError

AMOUNT_TOLERANCE = 0.01


def _close(a: Optional[float], b: Optional[float], tol: float = AMOUNT_TOLERANCE) -> bool:
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) <= tol


# ---------------------------------------------------------------------------
# DataLoader
# ---------------------------------------------------------------------------

class DataLoader:
    """Parses uploaded files and loads them into this session's database."""

    def __init__(self, database_url: str, engine=None):
        self.database_url = database_url
        self.engine = engine or create_db_engine(database_url)
        self.Session = get_session_factory(self.engine)
        self.transaction_repo = TransactionRepository(self.Session)
        self.member_repo = MemberRepository(self.Session)

    # -- transactions (bank/UPI source files) --------------------------------

    def load_transactions_from_file(self, path: str) -> dict:
        """Parse + insert one source/transaction file. Returns
        {parsed, inserted, skipped, error, skipped_details}.

        `skipped` keeps its original meaning (duplicates rejected by
        insert_if_new) so nothing that already reads this dict breaks.
        `skipped_details` is new: a full row-level record of everything
        that didn't make it in - duplicates AND rows/sheets dropped during
        parsing (blank rows, unrecognized sheets) - each tagged with why.
        """
        parse_skipped: list = []
        try:
            rows = parse_transaction_file(path, source_file=Path(path).name, skipped_out=parse_skipped)
        except ParseError as exc:
            return {"parsed": 0, "inserted": 0, "skipped": 0, "error": str(exc), "skipped_details": []}
        except Exception as exc:  # pragma: no cover - defensive
            return {"parsed": 0, "inserted": 0, "skipped": 0, "error": f"Unexpected error: {exc}", "skipped_details": []}

        skipped_details = list(parse_skipped)
        inserted = skipped = 0
        for row in rows:
            if self.transaction_repo.insert_if_new(row):
                inserted += 1
            else:
                skipped += 1
                skipped_details.append({
                    "reason": "Duplicate - matches a transaction already loaded this session",
                    "transaction_id": row.get("transaction_id"),
                    "date": row.get("value_date"),
                    "amount": row.get("transaction_amount"),
                    "name": row.get("name"),
                    "member_ids": ", ".join(row.get("member_ids") or []),
                    "source": row.get("source"),
                })
        return {
            "parsed": len(rows), "inserted": inserted, "skipped": skipped,
            "error": None, "skipped_details": skipped_details,
        }

    def load_transactions_from_csv(self, path: str) -> int:
        """Back-compat wrapper (older callers expect a bare int)."""
        return self.load_transactions_from_file(path)["inserted"]

    # -- members (reference / internal ledger files) --------------------------

    def load_members_from_file(self, path: str) -> dict:
        """Parse + insert one reference/member file. Same {parsed, inserted,
        skipped, error, skipped_details} shape as load_transactions_from_file."""
        parse_skipped: list = []
        try:
            rows = parse_member_file(path, source_file=Path(path).name, skipped_out=parse_skipped)
        except ParseError as exc:
            return {"parsed": 0, "inserted": 0, "skipped": 0, "error": str(exc), "skipped_details": []}
        except Exception as exc:  # pragma: no cover - defensive
            return {"parsed": 0, "inserted": 0, "skipped": 0, "error": f"Unexpected error: {exc}", "skipped_details": []}

        skipped_details = list(parse_skipped)
        inserted = skipped = 0
        for row in rows:
            if self.member_repo.insert_if_new(row):
                inserted += 1
            else:
                skipped += 1
                skipped_details.append({
                    "reason": "Duplicate - matches a reference record already loaded this session",
                    "member_id": row.get("member_id"),
                    "member_name": row.get("member_name"),
                    "date": row.get("transaction_date"),
                    "credit": row.get("credit"),
                    "debit": row.get("debit"),
                    "source": row.get("source_file"),
                })
        return {
            "parsed": len(rows), "inserted": inserted, "skipped": skipped,
            "error": None, "skipped_details": skipped_details,
        }

    def load_members_from_csv(self, path: str) -> int:
        return self.load_members_from_file(path)["inserted"]


# ---------------------------------------------------------------------------
# Matching engine
# ---------------------------------------------------------------------------

class ReconciliationEngine:
    def __init__(self, Session):
        self.Session = Session

    def get_transaction_date_range(self) -> tuple:
        with self.Session() as session:
            rows = (
                session.query(Transaction.value_date)
                .filter(Transaction.value_date.isnot(None))
                .distinct()
                .all()
            )
        dates = sorted({r[0] for r in rows if r[0] is not None})
        return dates

    def process_all_reconciliation(self) -> dict:
        """Reconcile every date present in the loaded transactions -
        needed because a batch upload usually spans many dates, unlike a
        single automated daily run."""
        dates = self.get_transaction_date_range()
        if not dates:
            return {"status": "no_data", "message": "No transaction data loaded yet."}

        daily_results = []
        all_details = []
        all_matched = []
        agg = {"total_processed": 0, "matched": 0, "partially_matched": 0,
               "unmatched": 0, "non_member_transactions_skipped": 0}
        discrepancy_total = 0.0

        for d in dates:
            day = self.process_daily_reconciliation(d)
            daily_results.append({"date": d.isoformat(), "status": "ok", "results": day["summary"]})
            all_matched.extend(day["matched_details"])
            all_details.extend(day["details"])
            for k in ("total_processed", "matched", "partially_matched",
                      "unmatched", "non_member_transactions_skipped"):
                agg[k] += day["summary"][k]
            discrepancy_total += day["summary"]["discrepancy_amount"]

        agg["match_rate"] = round(100 * agg["matched"] / agg["total_processed"], 2) if agg["total_processed"] else 0.0
        agg["discrepancy_amount"] = round(discrepancy_total, 2)

        return {
            "status": "ok",
            "summary": agg,
            "dates_processed": [d.isoformat() for d in dates],
            "daily_results": daily_results,
            "matched_details": all_matched,
            "details": all_details,
        }

    def process_daily_reconciliation(self, target_date: Optional[date_cls] = None) -> dict:
        """Reconcile a single calendar day (defaults to today - fine for a
        scheduled daily run, but process_all_reconciliation should be used
        for a multi-date batch upload)."""
        if target_date is None:
            target_date = date_cls.today()

        with self.Session() as session:
            txns = session.query(Transaction).filter(Transaction.value_date == target_date).all()
            members = session.query(Member).all()

        member_index = defaultdict(list)
        for m in members:
            member_index[m.member_id].append(m)

        matched, partial, unmatched = [], [], []
        non_member_skipped = 0
        discrepancy_amount = 0.0

        # Group every bank transaction on this date by the member ID(s) on
        # it. A single transaction can land in more than one group when its
        # member-ID cell had several IDs (multi-member rows that balance are
        # already resolved by Pass 0 above; the ones that don't balance fall
        # through to here) - the `resolved` set makes sure any given
        # transaction is only ever turned into a record once.
        groups = defaultdict(list)
        for t in txns:
            mids = t.member_ids()
            if not mids:
                # No member/policy ID on this bank row at all - a rent,
                # salary, vendor or other non-member payment.
                non_member_skipped += 1
                continue
            for mid in mids:
                groups[mid].append(t)

        resolved = set()  # ids() of Transaction objects already turned into a record

        # Pass 0: bank rows whose Policy No/Member ID cell names two or more
        # members with ONE combined amount and no per-member amount baked
        # into the cell itself (that per-ID "ID-amount" style is already
        # split into separate single-member rows at parse time - see
        # parsers.extract_id_amount_pairs / is_bulk_deposit). Left
        # unhandled, the loop below would land the whole combined amount on
        # whichever member happens to be processed first, wrongly flag it
        # as a mismatch, and silently drop every other member on the row.
        # Instead: check whether the row's amount equals the SUM of each
        # named member's own ledger entries for this date: if it balances,
        # that confirms the real split, so record one matched entry per
        # member against their own ledger line.
        for t in txns:
            mids = t.member_ids()
            if len(mids) < 2 or id(t) in resolved:
                continue

            per_member_ledger = {}
            all_members_found = True
            for mid in mids:
                candidates = [c for c in member_index.get(mid, []) if c.transaction_date == target_date]
                if not candidates:
                    all_members_found = False
                    break
                per_member_ledger[mid] = candidates
            if not all_members_found:
                continue  # fall through to the normal per-member handling below

            ledger_total = sum(c.amount() or 0 for cs in per_member_ledger.values() for c in cs)
            if not _close(ledger_total, t.transaction_amount):
                continue  # doesn't balance - let the normal handling below report it

            resolved.add(id(t))
            shares = ", ".join(
                f"{mid} \u20b9{sum(c.amount() or 0 for c in cs):,.2f}"
                for mid, cs in per_member_ledger.items()
            )
            note = (
                f"1 bank transaction (\u20b9{t.transaction_amount:,.2f}) covers {len(mids)} members "
                f"in one cell ({shares}); confirmed by summing each member's own ledger entry."
            )
            for mid, cs in per_member_ledger.items():
                for c in cs:
                    matched.append(self._to_record(
                        t, status="matched", matched_on=f"member_id+date+multi_member_split ({note})",
                        member=c,
                    ))

        for mid, group_txns in groups.items():
            all_ledger_entries = member_index.get(mid, [])
            if not all_ledger_entries:
                for t in group_txns:
                    if id(t) in resolved:
                        continue
                    resolved.add(id(t))
                    unmatched.append(self._to_record(
                        t, status="unmatched",
                        issues=[f"Account No {mid} not found in the reference/internal ledger."],
                    ))
                continue

            same_date_candidates = [c for c in all_ledger_entries if c.transaction_date == target_date]
            if not same_date_candidates:
                c = all_ledger_entries[0]
                for t in group_txns:
                    if id(t) in resolved:
                        continue
                    resolved.add(id(t))
                    partial.append(self._to_record(
                        t, status="partially_matched", matched_on="member_id only", member=c,
                        issues=[f"Account No matched, but the internal ledger has no entry on {t.value_date}; "
                                f"nearest ledger entry is dated {c.transaction_date}."],
                        expected=str(c.transaction_date) if c.transaction_date else None,
                        actual=str(t.value_date) if t.value_date else None,
                    ))
                continue

            remaining_txns = [t for t in group_txns if id(t) not in resolved]
            remaining_ledger = list(same_date_candidates)

            # Pass 1: exact single-transaction matches (most common case -
            # one bank row, one ledger entry, same amount).
            for t in list(remaining_txns):
                hit = next((c for c in remaining_ledger if _close(c.amount(), t.transaction_amount)), None)
                if hit is not None:
                    resolved.add(id(t))
                    remaining_txns.remove(t)
                    remaining_ledger.remove(hit)
                    matched.append(self._to_record(t, status="matched", matched_on="member_id+date+amount", member=hit))

            # Pass 2: whatever's left over on either side for this member on
            # this date is compared as totals, not row by row. This covers
            # every "split across several rows" shape in one rule: several
            # bank transactions funding one ledger entry (e.g. many small
            # collections recorded as one FD/RD deposit), several ledger
            # entries funding one bank transaction (e.g. one bank deposit
            # that was logged as two separate ledger postings), or several
            # on both sides. If the two totals agree, every remaining row on
            # both sides for this member/date is counted as matched together.
            if remaining_txns and remaining_ledger:
                txn_total = sum(t.transaction_amount or 0 for t in remaining_txns)
                ledger_total = sum(c.amount() or 0 for c in remaining_ledger)
                if _close(txn_total, ledger_total):
                    representative = remaining_ledger[0]
                    n_txn, n_ledger = len(remaining_txns), len(remaining_ledger)
                    if n_txn == 1 and n_ledger > 1:
                        note = f"1 bank transaction (\u20b9{txn_total:,.2f}) = {n_ledger} ledger entries summed"
                    elif n_txn > 1 and n_ledger == 1:
                        note = f"{n_txn} bank transactions totalling \u20b9{txn_total:,.2f} = 1 ledger entry"
                    else:
                        note = f"{n_txn} bank transaction(s) totalling \u20b9{txn_total:,.2f} = {n_ledger} ledger entries summed"
                    for t in remaining_txns:
                        resolved.add(id(t))
                        matched.append(self._to_record(
                            t, status="matched", matched_on=f"member_id+date+summed_amount ({note})",
                            member=representative,
                        ))
                    remaining_txns = []

            # Anything still unresolved for this member/date is a genuine
            # amount mismatch - report it against the nearest ledger entry.
            for t in remaining_txns:
                resolved.add(id(t))
                c = same_date_candidates[0]
                diff = abs((c.amount() or 0) - (t.transaction_amount or 0))
                discrepancy_amount += diff
                partial.append(self._to_record(
                    t, status="partially_matched", matched_on="member_id+date", member=c,
                    issues=[f"Amount differs from the internal ledger by \u20b9{diff:,.2f}."],
                    expected=f"{c.amount():,.2f}" if c.amount() is not None else None,
                    actual=f"{t.transaction_amount:,.2f}" if t.transaction_amount is not None else None,
                ))

        total = len(matched) + len(partial) + len(unmatched)
        match_rate = round(100 * len(matched) / total, 2) if total else 0.0

        return {
            "summary": {
                "total_processed": total,
                "matched": len(matched),
                "partially_matched": len(partial),
                "unmatched": len(unmatched),
                "match_rate": match_rate,
                "discrepancy_amount": round(discrepancy_amount, 2),
                "non_member_transactions_skipped": non_member_skipped,
            },
            "matched_details": matched,
            "details": partial + unmatched,
        }

    @staticmethod
    def _to_record(t: Transaction, status: str, matched_on: Optional[str] = None,
                    issues: Optional[list] = None, expected: Optional[str] = None,
                    actual: Optional[str] = None, member: Optional[Member] = None) -> dict:
        mids = t.member_ids()
        return {
            "transaction_id": t.transaction_id,
            "date": t.value_date.isoformat() if t.value_date else None,
            "amount": t.transaction_amount,
            "cr_dr": t.cr_dr,
            "payment_type": t.source,
            "member_id": member.member_id if member else (mids[0] if mids else None),
            "member_name": member.member_name if member else None,
            "branch": member.branch if member else t.branch,
            "matched_on": matched_on,
            "status": status,
            "issues": issues or [],
            "expected": expected,
            "actual": actual,
        }


# ---------------------------------------------------------------------------
# Service facade
# ---------------------------------------------------------------------------

class ReconciliationService:
    """Thin facade over ReconciliationEngine, adds persistence + the
    date-range helper the Streamlit app / API need."""

    def __init__(self, database_url: str, engine=None):
        self.engine = engine or create_db_engine(database_url)
        self.Session = get_session_factory(self.engine)
        self._engine_logic = ReconciliationEngine(self.Session)
        self._recon_repo = ReconciliationRepository(self.Session)

    def get_transaction_date_range(self) -> dict:
        dates = self._engine_logic.get_transaction_date_range()
        if not dates:
            return {"count": 0, "min_date": None, "max_date": None}
        return {"count": len(dates), "min_date": dates[0].isoformat(), "max_date": dates[-1].isoformat()}

    def perform_full_reconciliation(self) -> dict:
        result = self._engine_logic.process_all_reconciliation()
        if result["status"] == "no_data":
            return result
        self._recon_repo.save_all(result["matched_details"] + result["details"])
        return result

    def perform_daily_reconciliation(self, target_date: Optional[date_cls] = None) -> dict:
        day = self._engine_logic.process_daily_reconciliation(target_date)
        self._recon_repo.save_all(day["matched_details"] + day["details"])
        return {"status": "ok", **day}

    def get_history(self, start_date=None, end_date=None, status=None) -> list:
        records = self._recon_repo.get_history(start_date, end_date, status)
        return [r.to_dict() for r in records]

    def search_all(self, query: str, limit: int = 200) -> dict:
        """Free-text search across everything loaded into this session:
        bank/UPI transactions, the internal ledger, and any saved
        reconciliation results. Matches (partial, case-insensitive) on a
        transaction/UTR/RRN number, a member/payer name, or a policy/member
        ID - whichever field the query happens to hit.
        """
        q = (query or "").strip()
        if not q:
            return {"query": q, "transactions": [], "members": [], "reconciliation_records": []}
        like = f"%{q}%"

        with self.Session() as session:
            txns = (
                session.query(Transaction)
                .filter(
                    or_(
                        Transaction.transaction_id.ilike(like),
                        Transaction.name.ilike(like),
                        Transaction.member_id_list.ilike(like),
                        Transaction.description.ilike(like),
                    )
                )
                .order_by(Transaction.value_date.desc())
                .limit(limit)
                .all()
            )
            members = (
                session.query(Member)
                .filter(
                    or_(
                        Member.member_id.ilike(like),
                        Member.member_name.ilike(like),
                        Member.tranx_id.ilike(like),
                        Member.transaction_number.ilike(like),
                    )
                )
                .order_by(Member.transaction_date.desc())
                .limit(limit)
                .all()
            )
            recon = (
                session.query(ReconciliationRecord)
                .filter(
                    or_(
                        ReconciliationRecord.transaction_id.ilike(like),
                        ReconciliationRecord.member_id.ilike(like),
                        ReconciliationRecord.member_name.ilike(like),
                    )
                )
                .order_by(ReconciliationRecord.date.desc())
                .limit(limit)
                .all()
            )

        return {
            "query": q,
            "transactions": [t.to_dict() for t in txns],
            "members": [m.to_dict() for m in members],
            "reconciliation_records": [r.to_dict() for r in recon],
        }