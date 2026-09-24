# Financial Data Reconciliation System

A complete financial data reconciliation system that handles multi-source transaction
processing across bank transactions, UPI payments, cash deposits, fixed deposits (FD),
recurring deposits (RD), and equated monthly installments (EMI) for loans.

---

## Quick Start (VS Code)

```bash
# 1. Open the folder in VS Code
code com_reconciliation

# 2. Create a virtual environment
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Run the application
python run.py
```

Then open <http://localhost:8000/docs> for the interactive API documentation.

Alternative ways to start the server:

```bash
uvicorn main:app --reload --app-dir src     # from the project root
cd src && python main.py                    # direct module run
```

Press `F5` in VS Code to use the bundled debug configurations
(`FastAPI: run.py`, `FastAPI: uvicorn --reload`, `Pytest: all tests`).

### Prerequisites

- Python 3.10+ (3.11 recommended)
- pip 21.0+
- SQLite (bundled with Python) for development; PostgreSQL for production

---

## Web app (Streamlit) — the easy way to reconcile files

For uploading and reconciling files by hand (no curl, no API client needed),
run the Streamlit app instead of/alongside the API:

```bash
# from the project root, with the same virtual environment activated
streamlit run streamlit_app.py
```

This opens a browser tab (usually <http://localhost:8501>) with a three-step
page:

1. **Upload source data files** — the data you want to reconcile: bank,
   UPI, cash, EMI, RD, FD exports, etc. Upload as many as you have (a
   `Load transaction files` button processes all of them at once, e.g. 10).
2. **Upload reference data files** — the data to reconcile *against* (your
   member/policy master records), typically 2-3 files, via the same
   multi-file upload + a `Load reference files` button.
3. **Run reconciliation** — one button. It automatically covers *every*
   date found across everything you loaded (not just today, since a batch
   of uploaded files will usually span many dates), then shows a summary,
   a match/partial/unmatched breakdown per date, a details table for
   anything that didn't match cleanly, and JSON/CSV download buttons for
   the report.

Each browser session gets its own private, temporary SQLite database, so
re-running with a clean slate is just the "New session" button in the
sidebar — nothing is written into `data/reconciliation.db` or affects the
FastAPI service above.

Duplicate transaction/member IDs across files (e.g. the same row appearing
in two of your ten source files) are detected and skipped automatically,
and reported as "skipped" rather than silently dropped.

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Service banner |
| GET | `/api/health` | Health check |
| POST | `/api/upload/transactions` | Upload one or more source/transaction files (multipart) |
| POST | `/api/upload/members` | Upload one or more reference/member files (multipart) |
| POST | `/api/reconcile/daily` | Trigger reconciliation for one day (optional `?date=YYYY-MM-DD`, defaults to today) |
| POST | `/api/reconcile/full` | Reconcile every date currently loaded, aggregated into one summary |
| GET | `/api/reconcile/date-range` | Distinct transaction dates currently loaded |
| GET | `/api/reconcile/history` | Reconciliation history with filters |

```bash
curl http://localhost:8000/api/health
curl -X POST http://localhost:8000/api/upload/transactions -F "files=@data/transactions.csv"
curl -X POST http://localhost:8000/api/upload/members -F "files=@data/members.csv"
curl -X POST http://localhost:8000/api/reconcile/full
curl "http://localhost:8000/api/reconcile/history?start_date=2026-09-01&end_date=2026-09-30"
```

---

## Loading the sample data (script, without the web app)

```bash
cd src
python -c "from services.reconciliation import DataLoader; \
dl = DataLoader(); \
print('members:', dl.load_members_from_csv('../data/members.csv')); \
print('transactions:', dl.load_transactions_from_csv('../data/transactions.csv'))"
```

Then trigger a reconciliation for a date that has data:

```bash
curl -X POST "http://localhost:8000/api/reconcile/daily?date=2026-08-01"
```

Or reconcile every date the sample data spans in one call:

```bash
curl -X POST "http://localhost:8000/api/reconcile/full"
```

---

## Project Structure

```
com_reconciliation/
├── streamlit_app.py                # Streamlit web app (upload + reconcile UI)
├── src/
│   ├── main.py                     # FastAPI application
│   ├── config/
│   │   ├── __init__.py
│   │   └── settings.py             # Application configuration
│   ├── models/
│   │   ├── __init__.py             # Database models
│   │   ├── transaction.py          # Transaction models
│   │   ├── member.py               # Member models
│   │   ├── reconciliation.py       # Reconciliation models
│   │   └── database.py             # Database setup + full schema
│   ├── services/
│   │   ├── __init__.py
│   │   ├── reconciliation.py       # Core reconciliation logic (daily + full/multi-date)
│   │   ├── validation.py           # Data validation
│   │   ├── report.py               # Report generation
│   │   └── file_handler.py         # File upload validation + ingestion (transactions + members)
│   ├── api/
│   │   ├── __init__.py
│   │   ├── endpoints.py            # API endpoints (incl. upload + full reconcile)
│   │   └── routers.py              # Route definitions
│   ├── utils/
│   │   ├── __init__.py             # Utilities
│   │   ├── helpers.py              # Helper functions
│   │   ├── parsers.py              # Data parsers (TRANSACTION_COLUMNS, MEMBER_COLUMNS)
│   │   ├── validators.py           # Boolean validators
│   │   ├── config.py               # Config loader
│   │   └── logger.py               # Logging
│   ├── repositories/
│   │   ├── __init__.py
│   │   ├── transaction_repo.py     # Transaction repository
│   │   ├── member_repo.py          # Member repository
│   │   └── reconciliation_repo.py  # Reconciliation repository
│   └── Dockerfile                  # Container configuration
├── data/
│   ├── transactions.csv            # Main transaction data
│   ├── members.csv                 # Member data
│   └── reconciliation_history.csv  # Reconciliation history
├── tests/
│   ├── conftest.py                 # Shared fixtures
│   ├── unit/
│   │   ├── test_transactions.py
│   │   ├── test_reconciliation.py
│   │   └── test_validation.py
│   └── integration/
│       └── test_api.py
├── config/app.yaml
├── .vscode/                        # VS Code launch + settings
├── requirements.txt
├── pyproject.toml
├── run.py
├── Dockerfile
├── docker-compose.yml
├── CHANGES.md                      # Fixes applied to the original draft
└── README.md
```

---

## Features

- Multi-source reconciliation (bank, UPI, cash, FD, RD, EMI)
- Multi-file, multi-date reconciliation in one action (`/api/reconcile/full`,
  and the Streamlit "Run reconciliation" button) — not limited to "today"
- Streamlit web app for uploading and reconciling files without an API client
- File upload API for both source and reference data (`/api/upload/...`)
- Automated validation and cleaning of raw data
- Duplicate detection and removal (within and across uploaded files)
- Detailed reconciliation records with amount-difference tracking
- REST API for operations
- Structured logging with rotation
- Pytest suite covering utils, repositories, services and the API

### Technology Stack

- **Backend**: FastAPI, SQLAlchemy 2.x, Uvicorn
- **Web UI**: Streamlit
- **Database**: SQLite (development), PostgreSQL (production)
- **Data Processing**: Pandas, NumPy, openpyxl
- **Validation**: Pydantic, custom validation logic
- **Testing**: Pytest

---

## Data Models

### Members Table

| Field | Type | Description |
|-------|------|-------------|
| member_id | string | Unique member identifier |
| name | string | Member name |
| code | string | Member code |
| account_number | string | Bank account number |
| branch | string | Branch location |
| scheme | string | Scheme type |
| payment_mode | string | Payment mode |
| transaction_type | string | Transaction type |
| o_balance | decimal | Opening balance |
| credit | decimal | Total credits |
| debit | decimal | Total debits |
| c_balance | decimal | Closing balance |
| maturity_amount | decimal | Maturity amount |

### Transactions Table

| Field | Type | Description |
|-------|------|-------------|
| id | string | Transaction identifier |
| value_date | datetime | Transaction date |
| txn_posted_date | datetime | Posted date |
| description | string | Transaction description |
| cr_dr | string | Credit/Debit indicator |
| transaction_amount | decimal | Transaction amount |
| available_balance | decimal | Available balance |
| name | string | Account holder name |
| policy_no_member_id | string | Member ID reference |
| location | string | Location/branch |
| branch | string | Branch used for matching |
| transaction_type | string | Type used for matching |
| source | string | Transaction source |

Related tables: `emi_records`, `rd_records`, `fd_records`, `loan_records`,
`reconciliation_records`.

---

## Configuration

Environment variables override `config/app.yaml`:

| Variable | Default | Description |
|----------|---------|-------------|
| DATABASE_URL | sqlite:///./data/reconciliation.db | Database connection string |
| API_HOST | 0.0.0.0 | API server host |
| API_PORT | 8000 | API server port |
| LOG_LEVEL | INFO | Logging level |
| DEBUG_MODE | false | Auto-reload / debug mode |

---

## Testing

```bash
pytest              # all tests
pytest tests/unit   # unit tests only
pytest -v           # verbose
```

---

## Deployment

```bash
docker compose up -d      # app + PostgreSQL
docker compose logs -f app
docker compose down
```

To run the container against SQLite instead of PostgreSQL, set
`DATABASE_URL=sqlite:///./data/reconciliation.db` in `docker-compose.yml`
and remove the `db` service dependency.
