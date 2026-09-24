"""
Turns an uploaded .csv/.xlsx/.xls into normalized dict rows.

Two schemas are supported:
  - "member"      -> the internal transactions-report.xlsx (reference/master
                      ledger). Match key column: Account No.
  - "transaction" -> a bank/UPI statement (source data). Match key column:
                      Policy No/ Member ID (may list more than one ID per cell).

Every bank we've seen (Laxmi Nagar, Patna, Punjab National Bank, YES Bank -
bank txns, YES Bank - QR/UPI) puts its header row at a different offset and
uses different column names for the same thing, so the header row is
*detected* by scanning the first 30 rows for the row that best matches a
known set of column-name aliases, rather than assumed to be row 0.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import pandas as pd

# ---------------------------------------------------------------------------
# Canonical column lists (shown to the user in the Streamlit "expected
# columns" panel; also used as the alias source for header detection)
# ---------------------------------------------------------------------------

MEMBER_COLUMNS = [
    "Account No",            # match key
    "Tranx ID",               # display/audit only
    "Transaction Number",     # display/audit only
    "Member Name",
    "Branch Name",
    "Scheme",
    "Payment Mode",
    "Transaction Type",
    "Credit",
    "Debit",
    "O Balance",
    "C Balance",
    "Transaction Date",
]

TRANSACTION_COLUMNS = [
    "Policy No/ Member ID",   # match key (may contain multiple IDs)
    "Transaction ID / Txn No. / Reference No / RRN",   # display/audit only
    "Value Date / Txn Date / Date",
    "Cr/Dr  (or separate Credit Amount / Debit Amount columns)",
    "Transaction Amount / Amount",
    "Name",
    "Branch / Location",
    "Description / Remarks",
]

# ---------------------------------------------------------------------------
# Header-row detection
# ---------------------------------------------------------------------------

_MEMBER_ALIASES = {
    "member_id": ["account no"],
    "tranx_id": ["tranx id"],
    "transaction_number": ["transaction number"],
    "member_name": ["member name"],
    "branch": ["branch name", "collection center name"],
    "scheme": ["scheme"],
    "payment_mode": ["payment mode"],
    "transaction_type": ["transaction type"],
    "credit": ["credit"],
    "debit": ["debit"],
    "o_balance": ["o balance"],
    "c_balance": ["c balance"],
    "transaction_date": ["transaction date"],
}

_TRANSACTION_ALIASES = {
    "member_id_cell": ["policy no/ member id", "policy no/member id", "policy no / member id"],
    "transaction_id": ["transaction id", "txn no.", "txn no", "reference no", "rrn", "merchant transaction id"],
    "reference_transaction_no": ["reference transaction no", "settlement utr number", "bank rrn"],
    "value_date": ["value date", "txn date", "date"],
    "txn_posted_date": ["txn posted date"],
    "description": ["description", "transaction description", "remarks"],
    "cr_dr": ["cr/dr"],
    "bank_transaction_type": ["transaction type"],
    "debit_amount": ["debit amount", "dr amount"],
    "credit_amount": ["credit amount", "cr amount"],
    "transaction_amount": ["transaction amount(inr)", "transaction amount (inr)", "amount"],
    "available_balance": ["available balance(inr)", "available balance (inr)", "running balance", "balance"],
    "name": ["name"],
    "branch": ["locaction", "location", "branch name"],
}


def _norm_header(cell) -> str:
    if cell is None:
        return ""
    s = str(cell).strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s


def _alias_keyword_set(alias_map: dict) -> set:
    kws = set()
    for values in alias_map.values():
        kws.update(values)
    return kws


def _find_header_row(raw: pd.DataFrame, alias_map: dict, scan_rows: int = 30) -> Optional[int]:
    """Return the index of the row that looks most like the header, or None."""
    keywords = _alias_keyword_set(alias_map)
    best_row, best_score = None, 0
    for i in range(min(scan_rows, len(raw))):
        row_vals = [_norm_header(v) for v in raw.iloc[i].tolist()]
        score = sum(1 for v in row_vals if v in keywords)
        if score > best_score:
            best_score, best_row = score, i
    if best_score >= 2:
        return best_row
    return None


def _map_columns(columns, alias_map: dict) -> dict:
    """Return {canonical_name: actual_column_name} for whichever aliases were found."""
    norm_to_actual = {_norm_header(c): c for c in columns}
    result = {}
    for canonical, aliases in alias_map.items():
        for alias in aliases:
            if alias in norm_to_actual:
                result[canonical] = norm_to_actual[alias]
                break
    return result


# ---------------------------------------------------------------------------
# Reading raw files (csv / xlsx / xls)
# ---------------------------------------------------------------------------

SUPPORTED_EXTENSIONS = {".csv", ".xlsx", ".xls"}


def is_supported_file_type(path: str) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_EXTENSIONS


def _read_raw_no_header(path: str, sheet_name=0) -> pd.DataFrame:
    ext = Path(path).suffix.lower()
    if ext == ".csv":
        return pd.read_csv(path, header=None, dtype=str, keep_default_na=True)
    return pd.read_excel(path, header=None, sheet_name=sheet_name)


def _read_with_header(path: str, header_row: int, sheet_name=0) -> pd.DataFrame:
    ext = Path(path).suffix.lower()
    if ext == ".csv":
        return pd.read_csv(path, header=header_row)
    return pd.read_excel(path, header=header_row, sheet_name=sheet_name)


def _list_sheet_names(path: str) -> list:
    """All sheet names for an xlsx/xls file; a single pseudo-sheet (0) for csv."""
    ext = Path(path).suffix.lower()
    if ext == ".csv":
        return [0]
    return pd.ExcelFile(path).sheet_names


# ---------------------------------------------------------------------------
# ID normalization
# ---------------------------------------------------------------------------

# Matches the "NNN-XX#####" member/policy ID shape seen across every file,
# e.g. "001-RD04298", "004-LF00775", with an optional trailing letter
# ("001-LF00343R"). Also covers the less common "NNNL-#####" shape where the
# scheme letter sits before the dash instead of after it, with no scheme
# letters at all after the dash ("001M-05750"). Deliberately anchored so a
# trailing "-1000" amount suffix (seen in some Laxmi Nagar / Patna cells) is
# NOT swept into the ID.
_ID_PATTERN = re.compile(r"\d{1,4}[A-Z]{0,3}-[A-Z]{0,4}\d{3,6}[A-Z]?")

# Some bank rows are a single bulk cash deposit covering several members at
# once (an agent deposits one lump sum at the counter for many RD/DD/LF
# accounts together). The bank only records one total, but whoever typed the
# row wrote each member's own contribution right after their ID - sometimes
# separated by " - " (e.g. "001-DD00262 - 500"), sometimes by plain
# whitespace/tabs (e.g. "001-FD01236    1000"). This pulls out those (id,
# amount) pairs so each member's real contribution can be checked
# individually, instead of comparing every member against the row's
# combined total.
_ID_AMOUNT_PATTERN = re.compile(
    r"(\d{1,4}[A-Z]{0,3}-[A-Z]{0,4}\d{3,6}[A-Z]?)[-\s]+([\d,]+(?:\.\d+)?)"
)

BULK_SPLIT_TOLERANCE = 1.0  # rupees; how close the sub-amounts must sum to the row total


def extract_ids(cell) -> list:
    """Split a member/policy-ID cell that may hold more than one ID.

    Handles the real formats seen in these files: a single ID, several IDs
    separated by whitespace/newlines/slashes, and IDs with a trailing
    "-<amount>" suffix that must not be mistaken for part of the ID.
    """
    if cell is None or (isinstance(cell, float) and cell != cell):
        return []
    s = str(cell).strip().upper()
    if not s or s == "-":
        return []

    ids = _ID_PATTERN.findall(s)
    if ids:
        seen, out = set(), []
        for i in ids:
            if i not in seen:
                seen.add(i)
                out.append(i)
        return out

    # Fallback for anything that doesn't match the usual shape: split on
    # whitespace/slash/comma and keep tokens that contain a letter (so pure
    # numbers/dashes are dropped).
    parts = re.split(r"[\s/\\,]+", s)
    out, seen = [], set()
    for p in parts:
        p = p.strip("-")
        if p and re.search(r"[A-Z]", p) and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def normalize_id(value) -> Optional[str]:
    """Normalize a single-valued match key (e.g. Account No) the same way."""
    ids = extract_ids(value)
    return ids[0] if ids else None


def extract_id_amount_pairs(cell) -> list:
    """Pull out (member_id, amount) pairs from a bulk-deposit cell where each
    member's own contribution is written as "ID - amount". Returns [] if the
    cell doesn't use this style (e.g. a plain list of IDs with no amounts)."""
    if cell is None or (isinstance(cell, float) and cell != cell):
        return []
    s = str(cell).strip().upper()
    if not s:
        return []
    pairs = []
    for id_match, amt_match in _ID_AMOUNT_PATTERN.findall(s):
        amt = to_float(amt_match)
        if amt is not None:
            pairs.append((id_match, amt))
    return pairs


# ---------------------------------------------------------------------------
# Amount / date helpers
# ---------------------------------------------------------------------------

def to_float(value) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.replace(",", "").replace("Cr.", "").replace("Dr.", "").strip()
        if not value or value == "-":
            return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f:
        return None
    return f


def to_date(value):
    if value is None or (isinstance(value, float) and value != value):
        return None
    try:
        ts = pd.to_datetime(value, errors="coerce", dayfirst=False)
    except Exception:
        return None
    if pd.isna(ts):
        return None
    return ts.date()


# ---------------------------------------------------------------------------
# Public parsers
# ---------------------------------------------------------------------------

class ParseError(Exception):
    pass


def parse_member_file(path: str, source_file: Optional[str] = None,
                       skipped_out: Optional[list] = None) -> list:
    """Parse the internal transactions-report.xlsx (reference data).

    Reads every sheet in the workbook (not just the first), in case a
    reference file ever ships more than one - any sheet that doesn't look
    like a ledger (no recognizable header) is silently skipped rather than
    raising, since most reference files only use one sheet.

    skipped_out: if given, every dropped row/sheet is recorded into it as a
    dict with a "reason". Purely additive - the returned `rows` list is
    unchanged either way.
    """
    if not is_supported_file_type(path):
        raise ParseError(f"Unsupported file type: {path}")

    sheet_names = _list_sheet_names(path)
    bank_label = source_file or Path(path).stem
    rows = []
    any_sheet_matched = False

    for sheet in sheet_names:
        raw = _read_raw_no_header(path, sheet_name=sheet)
        header_row = _find_header_row(raw, _MEMBER_ALIASES)
        if header_row is None:
            if skipped_out is not None:
                skipped_out.append({
                    "reason": "Whole sheet skipped - no recognizable header row found",
                    "source": f"{bank_label} [{sheet}]" if len(sheet_names) > 1 else bank_label,
                })
            continue  # this sheet isn't a ledger sheet - skip it, don't fail the whole file

        df = _read_with_header(path, header_row, sheet_name=sheet)
        df = df.dropna(how="all")
        colmap = _map_columns(df.columns, _MEMBER_ALIASES)
        if "member_id" not in colmap:
            if skipped_out is not None:
                skipped_out.append({
                    "reason": "Whole sheet skipped - header found but no Account No column mapped",
                    "source": f"{bank_label} [{sheet}]" if len(sheet_names) > 1 else bank_label,
                })
            continue
        any_sheet_matched = True

        for _, r in df.iterrows():
            raw_account_no = r.get(colmap.get("member_id"))
            member_id = normalize_id(raw_account_no)
            if not member_id:
                if skipped_out is not None:
                    skipped_out.append({
                        "reason": "Blank/unrecognized row - no usable Account No",
                        "raw_account_no": _str_or_none(raw_account_no),
                        "raw_member_name": _str_or_none(r.get(colmap.get("member_name"))),
                        "raw_date": _str_or_none(r.get(colmap.get("transaction_date"))),
                        "source": f"{bank_label} [{sheet}]" if len(sheet_names) > 1 else bank_label,
                    })
                continue
            rows.append({
                "member_id": member_id,
                "tranx_id": _str_or_none(r.get(colmap.get("tranx_id"))),
                "transaction_number": _str_or_none(r.get(colmap.get("transaction_number"))),
                "member_name": _str_or_none(r.get(colmap.get("member_name"))),
                "branch": _str_or_none(r.get(colmap.get("branch"))),
                "scheme": _str_or_none(r.get(colmap.get("scheme"))),
                "payment_mode": _str_or_none(r.get(colmap.get("payment_mode"))),
                "transaction_type": _str_or_none(r.get(colmap.get("transaction_type"))),
                "credit": to_float(r.get(colmap.get("credit"))),
                "debit": to_float(r.get(colmap.get("debit"))),
                "o_balance": to_float(r.get(colmap.get("o_balance"))),
                "c_balance": to_float(r.get(colmap.get("c_balance"))),
                "transaction_date": to_date(r.get(colmap.get("transaction_date"))),
                "source_file": source_file,
            })

    if not any_sheet_matched:
        raise ParseError(
            "Could not find a recognizable ledger sheet in this file (expected columns "
            "like 'Account No', 'Tranx ID', 'Transaction Date', ... on at least one sheet)."
        )
    return rows


def parse_transaction_file(path: str, source_file: Optional[str] = None,
                            skipped_out: Optional[list] = None) -> list:
    """Parse a bank/UPI statement (source data).

    Reads every sheet in the workbook, not just the first - several of these
    files carry more than one real data sheet (e.g. a bank-transfer sheet and
    a separate QR/UPI sheet in the same workbook). Each row's `source` field
    is tagged with the sheet it came from whenever more than one sheet
    contributed rows, so you can trace a row back to its exact tab. A sheet
    with a schema we don't recognize at all (e.g. a payment-gateway export
    with completely different columns) is skipped rather than guessed at -
    call `list_unrecognized_sheets()` on the same path to see what got left
    out.

    skipped_out: if given, every row (and whole unrecognized sheet) dropped
    during parsing is recorded into it as a dict with a "reason". Purely
    additive - the returned `rows` list is unchanged either way.
    """
    if not is_supported_file_type(path):
        raise ParseError(f"Unsupported file type: {path}")

    sheet_names = _list_sheet_names(path)
    bank_label = source_file or Path(path).stem

    rows = []
    matched_sheets = []

    for sheet in sheet_names:
        raw = _read_raw_no_header(path, sheet_name=sheet)
        header_row = _find_header_row(raw, _TRANSACTION_ALIASES)
        if header_row is None:
            if skipped_out is not None:
                skipped_out.append({
                    "reason": "Whole sheet skipped - no recognizable header row found",
                    "source": f"{bank_label} [{sheet}]" if len(sheet_names) > 1 else bank_label,
                })
            continue  # doesn't look like a transaction sheet - skip, don't fail the file

        df = _read_with_header(path, header_row, sheet_name=sheet)
        df = df.dropna(how="all")
        colmap = _map_columns(df.columns, _TRANSACTION_ALIASES)
        if "member_id_cell" not in colmap and "value_date" not in colmap:
            if skipped_out is not None:
                skipped_out.append({
                    "reason": "Whole sheet skipped - header found but no usable columns mapped",
                    "source": f"{bank_label} [{sheet}]" if len(sheet_names) > 1 else bank_label,
                })
            continue
        matched_sheets.append(sheet)

        sheet_label = f"{bank_label} [{sheet}]" if len(sheet_names) > 1 else bank_label
        rows.extend(_parse_transaction_dataframe(df, colmap, sheet_label, source_file, skipped_out=skipped_out))

    if not matched_sheets:
        raise ParseError(
            "Could not find a recognizable header row on any sheet (expected columns like "
            "'Policy No/ Member ID', 'Value Date'/'Txn Date', an amount column, ...)."
        )
    return rows


def list_unrecognized_sheets(path: str) -> list:
    """Sheet names in this workbook that parse_transaction_file skipped
    because their columns didn't match any known bank format. Useful for
    surfacing "this sheet was ignored" to the user instead of silently
    dropping data."""
    if not is_supported_file_type(path) or Path(path).suffix.lower() == ".csv":
        return []
    unrecognized = []
    for sheet in _list_sheet_names(path):
        raw = _read_raw_no_header(path, sheet_name=sheet)
        if _find_header_row(raw, _TRANSACTION_ALIASES) is None:
            unrecognized.append(sheet)
    return unrecognized


def _parse_transaction_dataframe(df, colmap: dict, bank_label: str, source_file: Optional[str],
                                  skipped_out: Optional[list] = None) -> list:
    """Row-parsing logic for one already-header-mapped sheet/dataframe.

    skipped_out: if given, every row dropped here (blank/stray rows with
    neither a date nor a policy/member ID - nothing to anchor them to) is
    recorded into it as a dict, instead of just vanishing uncounted. Purely
    additive - parsing behavior and the returned `rows` list are unchanged
    whether or not a caller passes this.
    """
    rows = []
    for _, r in df.iterrows():
        member_cell = r.get(colmap.get("member_id_cell"))
        member_ids = extract_ids(member_cell)

        value_date = to_date(r.get(colmap.get("value_date")))
        if value_date is None and not member_ids:
            # stray/blank row that slipped past dropna(how="all") - no date
            # and no member ID means there's nothing to reconcile it against.
            if skipped_out is not None:
                skipped_out.append({
                    "reason": "Blank/unrecognized row - no date and no policy/member ID",
                    "raw_member_cell": _str_or_none(member_cell),
                    "raw_amount": _str_or_none(
                        r.get(colmap.get("transaction_amount")) or r.get(colmap.get("credit_amount"))
                    ),
                    "raw_description": _str_or_none(r.get(colmap.get("description"))),
                    "source": bank_label,
                })
            continue

        credit_amt = to_float(r.get(colmap.get("credit_amount")))
        debit_amt = to_float(r.get(colmap.get("debit_amount")))
        single_amt = to_float(r.get(colmap.get("transaction_amount")))

        if credit_amt is not None or debit_amt is not None:
            amount = credit_amt if (credit_amt or 0) > 0 else debit_amt
            cr_dr = "CR" if (credit_amt or 0) > 0 else ("DR" if (debit_amt or 0) > 0 else None)
        else:
            amount = single_amt
            raw_crdr = _str_or_none(r.get(colmap.get("cr_dr")))
            cr_dr = _normalize_cr_dr(raw_crdr, r.get(colmap.get("bank_transaction_type")))

        txn_id = _str_or_none(r.get(colmap.get("transaction_id")))
        ref_txn_no = _str_or_none(r.get(colmap.get("reference_transaction_no")))
        if not txn_id:
            txn_id = ref_txn_no

        description = _str_or_none(r.get(colmap.get("description")))
        available_balance = to_float(r.get(colmap.get("available_balance")))
        name = _str_or_none(r.get(colmap.get("name")))
        branch = _str_or_none(r.get(colmap.get("branch")))
        bank_txn_type = _str_or_none(r.get(colmap.get("bank_transaction_type")))

        base_row = {
            "transaction_id": txn_id,
            "value_date": value_date,
            "description": description,
            "cr_dr": cr_dr,
            "available_balance": available_balance,
            "name": name,
            "branch": branch,
            "transaction_type": bank_txn_type,
            "source": bank_label,
            "source_file": source_file,
        }

        # Bulk-deposit check: several member IDs, each with its own amount
        # written right after it, that together add up to this row's total.
        pairs = extract_id_amount_pairs(member_cell)
        pairs_total = sum(a for _, a in pairs)
        is_bulk_deposit = (
            len(pairs) >= 2
            and amount is not None
            and abs(pairs_total - amount) <= BULK_SPLIT_TOLERANCE
        )

        if is_bulk_deposit:
            bulk_note = (
                f"Split from a bulk deposit of {amount:,.2f} covering {len(pairs)} members "
                f"(original cell: {str(member_cell).strip()!r})"
            )
            for mid, sub_amount in pairs:
                row = dict(base_row)
                row["member_ids"] = [mid]
                row["transaction_amount"] = sub_amount
                row["description"] = (
                    f"{description} | {bulk_note}" if description else bulk_note
                )
                rows.append(row)
        else:
            row = dict(base_row)
            row["member_ids"] = member_ids
            row["transaction_amount"] = amount
            if len(pairs) >= 2:
                # sub-amounts were present but didn't sum to the row total -
                # flag for manual review rather than silently guessing
                mismatch_note = (
                    f"Per-member amounts in this cell sum to {pairs_total:,.2f}, "
                    f"which does NOT match the row total {amount!r} - needs manual review."
                )
                row["description"] = (
                    f"{description} | {mismatch_note}" if description else mismatch_note
                )
            rows.append(row)
    return rows


def _normalize_cr_dr(raw_crdr: Optional[str], bank_txn_type) -> Optional[str]:
    if raw_crdr:
        u = raw_crdr.strip().upper()
        if u.startswith("CR"):
            return "CR"
        if u.startswith("DR"):
            return "DR"
    if bank_txn_type is not None and str(bank_txn_type).strip().upper() == "COLLECTION":
        # QR/UPI collection statements: every row is money coming in
        return "CR"
    return None


def _str_or_none(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, float) and value != value:
        return None
    s = str(value).strip()
    return s if s else None