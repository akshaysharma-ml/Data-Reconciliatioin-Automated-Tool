# Data-Reco — Financial Data Reconciliation Engine

A Python engine (with a Streamlit interface for local testing) that
reconciles bank / UPI statements against an internal member ledger —
matching incoming payments against member/policy records across multiple
bank formats.

## In production

**The core reconciliation engine from this repository has been adapted and
deployed as part of a client solution by Survetrics**, where I work,
covering additional requirements such as multi-user access, persistent
storage, and an audit trail. That deployment is client-specific and isn't
linked from here.

## What it does

1. **Parse source data** — one or more bank/UPI statement files (the
   "actual" side: what the bank says came in).
2. **Parse reference data** — the internal ledger / transactions-report
   file (the "expected" side: what members were supposed to pay, against
   which policy).
3. **Run reconciliation** — the engine matches each bank transaction to a
   ledger entry by **member ID + date + amount**, and classifies every row
   as:
   - **Matched** — bank row and ledger entry agree
   - **Partially matched** — same member/date but amount differs, or split
     across multiple ledger lines
   - **Unmatched** — no corresponding ledger entry found
4. **Search** — a search interface to look up any transaction
   number/UTR/RRN, member name, or policy number across everything loaded
   into the session, plus any saved reconciliation results.
5. **Review skipped rows** — instead of just an "N rows skipped" count,
   every skipped row (duplicates, blank rows, unrecognized sheets) is
   listed with a reason, so nothing silently disappears.

## Supported bank formats

The parser auto-detects the header row (scanning the first ~30 rows) and
maps columns by alias, so it works across differently-formatted exports
without hard-coded column positions. Verified against multiple bank
statement layouts, including ones that split a regular statement and a
QR/UPI collection sheet across different tabs of the same workbook.

Workbooks with multiple sheets are read in full; a sheet with no
recognizable header is skipped and reported (not silently dropped).

## Key reconciliation logic

- **Match key**: member ID + date + amount — chosen over transaction
  ID/RRN after measuring that reference-number overlap between bank and
  ledger data was under 15%.
- **Multi-member rows**: a single bank row can carry more than one policy
  number in one cell (e.g. a member paying for two policies in one
  transfer). The engine checks whether the combined bank amount equals the
  sum of each named member's own ledger entries for that date — if it
  balances, it's split into one matched record per member instead of being
  forced onto whichever member is processed first.
- **Many-to-one matching**: several bank transactions that together sum to
  one ledger entry (or vice versa) are matched as a group, not just 1:1.

## Project structure

```
├── streamlit_app.py     # local test UI: uploads, search, results, downloads
├── reconciliation.py    # DataLoader + ReconciliationService (matching engine)
├── parsers.py            # Bank-format-agnostic Excel/CSV parsing
├── database.py            # SQLAlchemy models: Member, Transaction, ReconciliationRecord
└── requirements.txt
```

## Running locally

```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
```

Each browser session gets its own private SQLite database (created fresh
in a temp directory) — nothing persists between sessions by design. The
Streamlit UI here exists to exercise and test the engine locally; it was
not intended as a production interface.

## Known limitations (in this repo's version)

- Session-only storage — no logins, roles, or persistent history across
  sessions.
- Reconciliation currently checks bank → ledger only; a ledger entry with
  no matching bank row isn't separately flagged.
- Credit/debit direction isn't yet enforced in the amount comparison.
- Amounts are stored as `Float`; a move to fixed-point/`Decimal` is planned
  to avoid paisa-level rounding issues.

## License

*(add your license here)*
