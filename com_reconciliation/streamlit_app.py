"""
Streamlit front-end for the financial data reconciliation system.

Run it with:

    streamlit run streamlit_app.py

Workflow, left to right:
  1. Upload the source/transaction files you want reconciled (bank, UPI,
     cash, EMI, RD, FD exports, etc.) - as many as you have, e.g. ten.
  2. Upload the reference/member files to reconcile them against - e.g.
     two or three member/policy master files.
  3. Run reconciliation. It automatically covers every date found across
     everything you uploaded (not just "today"), and gives you a summary,
     a details table, and downloadable reports.

Each browser session gets its own private, throwaway SQLite database (a
temp file cleaned up when you hit "New session" or close the tab), so
multiple people can use the app at once without stepping on each other's
data, and re-running with a clean slate is just one click.
"""

import csv
import io
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from models.database import create_db_engine  # noqa: E402
from services.reconciliation import DataLoader, ReconciliationService  # noqa: E402
from utils.parsers import MEMBER_COLUMNS, TRANSACTION_COLUMNS  # noqa: E402

st.set_page_config(
    page_title="Financial Data Reconciliation",
    page_icon="🔄",
    layout="wide",
)

ALLOWED_TYPES = ["csv", "xlsx", "xls"]


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------
#
# Streamlit's st.dataframe/st.table/st.bar_chart all serialize through
# pyarrow under the hood. pyarrow's official Windows wheels are compiled
# assuming the CPU supports AVX2 - on older CPUs without it, the mere act of
# using pyarrow crashes the whole Python process instantly with no traceback
# (Windows exit code -1073741795 / STATUS_ILLEGAL_INSTRUCTION). To keep this
# app working on any machine, everything below is rendered as plain
# markdown/HTML instead, which never touches pyarrow.

def _render_table(rows: List[Dict[str, Any]], columns: Optional[List[str]] = None) -> None:
    """Render a list of dicts as a markdown table - no pandas/pyarrow involved."""
    if not rows:
        st.caption("No rows to show.")
        return

    cols = columns or list(rows[0].keys())

    def _cell(value: Any) -> str:
        if value is None:
            text = ""
        elif isinstance(value, list):
            text = "; ".join(str(v) for v in value)
        else:
            text = str(value)
        return text.replace("|", "\\|").replace("\n", " ")

    header = "| " + " | ".join(cols) + " |"
    separator = "| " + " | ".join("---" for _ in cols) + " |"
    body = "\n".join(
        "| " + " | ".join(_cell(row.get(col)) for col in cols) + " |"
        for row in rows
    )
    st.markdown(f"{header}\n{separator}\n{body}")


def _rows_to_csv(rows: List[Dict[str, Any]], fieldnames: List[str]) -> str:
    """Build a CSV string from a list of dicts using the stdlib csv module."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        clean = dict(row)
        if isinstance(clean.get("issues"), list):
            clean["issues"] = "; ".join(str(v) for v in clean["issues"])
        writer.writerow(clean)
    return buffer.getvalue()


def _render_count_bars(labels_and_counts: List[tuple]) -> None:
    """A tiny dependency-free horizontal bar visualization (no chart library needed)."""
    max_count = max((c for _, c in labels_and_counts), default=0) or 1
    colors = ["#2ecc71", "#f1c40f", "#e74c3c", "#3498db", "#9b59b6"]
    rows_html = []
    for i, (label, count) in enumerate(labels_and_counts):
        width_pct = round((count / max_count) * 100, 1)
        color = colors[i % len(colors)]
        rows_html.append(
            f'<div style="display:flex;align-items:center;margin:6px 0;">'
            f'<div style="width:150px;font-size:14px;">{label}</div>'
            f'<div style="flex:1;background:#eee;border-radius:4px;overflow:hidden;">'
            f'<div style="width:{width_pct}%;background:{color};padding:4px 8px;'
            f'color:white;font-size:12px;min-width:24px;">{count}</div></div></div>'
        )
    st.markdown("".join(rows_html), unsafe_allow_html=True)


def _render_result_section(
    title: str,
    rows: List[Dict[str, Any]],
    display_columns: Dict[str, str],
    csv_fieldnames: List[str],
    file_name: str,
    empty_message: str,
    expanded: bool = True,
) -> None:
    """One status's table (Matched / Partially matched / Unmatched) plus its own CSV download."""
    with st.expander(f"{title} ({len(rows)} row(s))", expanded=expanded):
        if not rows:
            st.caption(empty_message)
            return

        display_rows = [
            {display_columns[k]: r.get(k, "") for k in display_columns}
            for r in rows
        ]
        _render_table(display_rows)

        st.download_button(
            f"⬇️ Download {title.split(' ', 1)[-1]} (CSV)",
            data=_rows_to_csv(rows, fieldnames=csv_fieldnames),
            file_name=file_name,
            mime="text/csv",
            key=f"download_{file_name}",
        )


def _render_search_panel() -> None:
    """Global search box: find any transaction/UTR/RRN number, member name,
    or policy number across everything loaded into this session so far."""
    with st.container(border=True):
        box_col, close_col = st.columns([6, 1])
        with box_col:
            query = st.text_input(
                "Search",
                placeholder="🔍 Transaction No / UTR / RRN, member name, or policy number...",
                key="global_search_query",
                label_visibility="collapsed",
            )
        with close_col:
            if st.button("✖ Close", key="close_search_btn", use_container_width=True):
                st.session_state.show_search = False
                st.rerun()

        query = (query or "").strip()
        if not query:
            st.caption("Start typing a transaction/UTR/RRN number, member name, or policy number.")
            return

        results = st.session_state.service.search_all(query)
        txns = results["transactions"]
        members = results["members"]
        recon = results["reconciliation_records"]

        if not txns and not members and not recon:
            st.warning(f"No matches found for **{query}**.")
            return

        st.caption(
            f"Found **{len(txns)}** transaction(s), **{len(members)}** ledger record(s), "
            f"**{len(recon)}** reconciliation result(s) matching **{query}**."
        )

        if txns:
            st.markdown("**Bank / UPI transactions**")
            _render_table([
                {
                    "Transaction ID / UTR": t["transaction_id"],
                    "Date": t["value_date"],
                    "Amount": t["transaction_amount"],
                    "Cr/Dr": t["cr_dr"],
                    "Name": t["name"],
                    "Policy/Member ID(s)": "; ".join(t["member_ids"]),
                    "Source": t["source"],
                }
                for t in txns
            ])

        if members:
            st.markdown("**Ledger / member records**")
            _render_table([
                {
                    "Member/Policy No": m["member_id"],
                    "Tranx ID": m["tranx_id"],
                    "Transaction Number": m["transaction_number"],
                    "Member Name": m["member_name"],
                    "Branch": m["branch"],
                    "Credit": m["credit"],
                    "Debit": m["debit"],
                    "Date": m["transaction_date"],
                }
                for m in members
            ])

        if recon:
            st.markdown("**Reconciliation results** (from the last run)")
            _render_table([
                {
                    "Transaction ID": r["transaction_id"],
                    "Date": r["date"],
                    "Member ID": r["member_id"],
                    "Member Name": r["member_name"],
                    "Amount": r["amount"],
                    "Status": r["status"],
                    "Matched on": r["matched_on"],
                }
                for r in recon
            ])


def _render_back_to_top() -> None:
    """A small floating button that jumps back to the #recon-top anchor at the page start."""
    st.markdown(
        '<a href="#recon-top" style="position:fixed;bottom:24px;right:24px;'
        'background:#e74c3c;color:white;border-radius:50%;width:48px;height:48px;'
        'display:flex;align-items:center;justify-content:center;font-size:22px;'
        'text-decoration:none;box-shadow:0 2px 8px rgba(0,0,0,0.3);z-index:9999;" '
        'title="Back to top">⬆️</a>',
        unsafe_allow_html=True,
    )




# ---------------------------------------------------------------------------
# Session / database plumbing
# ---------------------------------------------------------------------------

def _new_session_state() -> None:
    """Create a fresh, private SQLite database and service objects for this session."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="recon_session_"))
    db_path = tmp_dir / "reconciliation.db"
    database_url = f"sqlite:///{db_path}"
    engine = create_db_engine(database_url)

    st.session_state.tmp_dir = str(tmp_dir)
    st.session_state.database_url = database_url
    st.session_state.engine = engine
    st.session_state.loader = DataLoader(database_url, engine=engine)
    st.session_state.service = ReconciliationService(database_url, engine=engine)
    st.session_state.txn_summaries = []
    st.session_state.member_summaries = []
    st.session_state.txn_skipped_rows = []
    st.session_state.member_skipped_rows = []
    st.session_state.reconciliation_result = None


def _reset_session() -> None:
    engine = st.session_state.get("engine")
    if engine is not None:
        engine.dispose()
    tmp_dir = st.session_state.get("tmp_dir")
    if tmp_dir and Path(tmp_dir).exists():
        shutil.rmtree(tmp_dir, ignore_errors=True)
    for key in list(st.session_state.keys()):
        del st.session_state[key]
    _new_session_state()


if "engine" not in st.session_state:
    _new_session_state()


def _save_upload_to_temp(uploaded_file) -> str:
    """Write a Streamlit UploadedFile to a real temp path the parsers can read."""
    suffix = Path(uploaded_file.name).suffix or ".csv"
    tmp_dir = Path(st.session_state.tmp_dir)
    safe_stem = Path(uploaded_file.name).stem.replace(" ", "_")
    target = tmp_dir / f"upload_{uuid4().hex[:8]}_{safe_stem}{suffix}"
    with open(target, "wb") as handle:
        handle.write(uploaded_file.getvalue())
    return str(target)


# ---------------------------------------------------------------------------
# Header / sidebar
# ---------------------------------------------------------------------------

st.markdown('<div id="recon-top"></div>', unsafe_allow_html=True)

header_left, header_right = st.columns([6, 1])
with header_left:
    st.title("🔄 Financial Data Reconciliation")
    st.caption(
        "Upload your source data files, upload the reference data to check them against, "
        "then run reconciliation and review the results — all in one place."
    )
with header_right:
    st.write("")
    st.write("")
    if st.button("🔍 Search", key="toggle_search_btn", use_container_width=True):
        st.session_state.show_search = not st.session_state.get("show_search", False)

if st.session_state.get("show_search"):
    _render_search_panel()

with st.sidebar:
    st.header("Session")
    txn_count = len(st.session_state.txn_summaries)
    member_count = len(st.session_state.member_summaries)
    st.metric("Transaction files loaded", txn_count)
    st.metric("Reference files loaded", member_count)

    if st.session_state.reconciliation_result is not None:
        st.success("Reconciliation has been run.")
    else:
        st.info("Reconciliation not run yet.")

    st.divider()
    if st.button("🗑️ New session (clear everything)", use_container_width=True):
        _reset_session()
        st.rerun()

    with st.expander("Expected file columns"):
        st.caption("Source/transaction files:")
        st.code(", ".join(TRANSACTION_COLUMNS), language=None)
        st.caption("Reference/member files:")
        st.code(", ".join(MEMBER_COLUMNS), language=None)
        st.caption("Extra or missing columns are tolerated — only the columns above are used for matching.")

st.divider()

# ---------------------------------------------------------------------------
# Step 1 — source / transaction files
# ---------------------------------------------------------------------------

st.header("1️⃣ Upload source data files")
st.write(
    "The data you want to reconcile — bank transfers, UPI payments, cash deposits, "
    "EMI, RD, FD exports, and so on. Upload as many as you have (e.g. 10)."
)

txn_files = st.file_uploader(
    "Source / transaction files",
    type=ALLOWED_TYPES,
    accept_multiple_files=True,
    key="txn_uploader",
    label_visibility="collapsed",
)

col1, col2 = st.columns([1, 3])
with col1:
    load_txns_clicked = st.button(
        "📥 Load transaction files",
        type="primary",
        disabled=not txn_files,
        use_container_width=True,
    )

if load_txns_clicked and txn_files:
    with st.spinner(f"Parsing and loading {len(txn_files)} file(s)..."):
        new_summaries = []
        for uploaded_file in txn_files:
            temp_path = _save_upload_to_temp(uploaded_file)
            summary = st.session_state.loader.load_transactions_from_file(temp_path)
            summary["file"] = uploaded_file.name
            new_summaries.append(summary)
            st.session_state.txn_skipped_rows.extend(summary.get("skipped_details", []))
        st.session_state.txn_summaries.extend(new_summaries)
    st.session_state.reconciliation_result = None  # stale after new data
    st.rerun()

if st.session_state.txn_summaries:
    display_rows = [
        {
            "File": s["file"], "Rows found": s["parsed"], "Inserted": s["inserted"],
            "Skipped / duplicate": s["skipped"], "Error": s["error"] or "",
        }
        for s in st.session_state.txn_summaries
    ]
    _render_table(display_rows)
    total_inserted = sum(s["inserted"] for s in st.session_state.txn_summaries)
    st.caption(f"**{total_inserted}** transaction row(s) inserted so far across all files.")

    _render_result_section(
        title="⏭️ Skipped rows",
        rows=st.session_state.txn_skipped_rows,
        display_columns={
            "reason": "Reason", "source": "File / Sheet", "transaction_id": "Transaction ID / UTR",
            "date": "Date", "amount": "Amount", "name": "Name", "member_ids": "Policy/Member ID(s)",
            "raw_member_cell": "Raw Policy/Member cell", "raw_amount": "Raw amount",
            "raw_description": "Raw description",
        },
        csv_fieldnames=["reason", "source", "transaction_id", "date", "amount", "name", "member_ids",
                         "raw_member_cell", "raw_amount", "raw_description"],
        file_name="transactions_skipped.csv",
        empty_message="Nothing skipped — every parsed row was either inserted or already covered above.",
        expanded=False,
    )

st.divider()

# ---------------------------------------------------------------------------
# Step 2 — reference / member files
# ---------------------------------------------------------------------------

st.header("2️⃣ Upload reference data files")
st.write(
    "The data to reconcile *against* — your member / policy master records. "
    "Usually a smaller set (e.g. 2-3 files)."
)

member_files = st.file_uploader(
    "Reference / member files",
    type=ALLOWED_TYPES,
    accept_multiple_files=True,
    key="member_uploader",
    label_visibility="collapsed",
)

col1, col2 = st.columns([1, 3])
with col1:
    load_members_clicked = st.button(
        "📥 Load reference files",
        type="primary",
        disabled=not member_files,
        use_container_width=True,
    )

if load_members_clicked and member_files:
    with st.spinner(f"Parsing and loading {len(member_files)} file(s)..."):
        new_summaries = []
        for uploaded_file in member_files:
            temp_path = _save_upload_to_temp(uploaded_file)
            summary = st.session_state.loader.load_members_from_file(temp_path)
            summary["file"] = uploaded_file.name
            new_summaries.append(summary)
            st.session_state.member_skipped_rows.extend(summary.get("skipped_details", []))
        st.session_state.member_summaries.extend(new_summaries)
    st.session_state.reconciliation_result = None  # stale after new data
    st.rerun()

if st.session_state.member_summaries:
    display_rows = [
        {
            "File": s["file"], "Rows found": s["parsed"], "Inserted": s["inserted"],
            "Skipped / duplicate": s["skipped"], "Error": s["error"] or "",
        }
        for s in st.session_state.member_summaries
    ]
    _render_table(display_rows)
    total_inserted = sum(s["inserted"] for s in st.session_state.member_summaries)
    st.caption(f"**{total_inserted}** reference record(s) inserted so far across all files.")

    _render_result_section(
        title="⏭️ Skipped rows",
        rows=st.session_state.member_skipped_rows,
        display_columns={
            "reason": "Reason", "source": "File / Sheet", "member_id": "Member/Policy No",
            "member_name": "Member Name", "date": "Date", "credit": "Credit", "debit": "Debit",
            "raw_account_no": "Raw Account No", "raw_member_name": "Raw Member Name",
            "raw_date": "Raw Date",
        },
        csv_fieldnames=["reason", "source", "member_id", "member_name", "date", "credit", "debit",
                         "raw_account_no", "raw_member_name", "raw_date"],
        file_name="members_skipped.csv",
        empty_message="Nothing skipped — every parsed row was either inserted or already covered above.",
        expanded=False,
    )

st.divider()

# ---------------------------------------------------------------------------
# Step 3 — run reconciliation
# ---------------------------------------------------------------------------

st.header("3️⃣ Run reconciliation")

date_range = st.session_state.service.get_transaction_date_range()

if date_range["count"] == 0:
    st.warning("Load at least one transaction file above before running reconciliation.")
else:
    span = (
        date_range["min_date"] if date_range["min_date"] == date_range["max_date"]
        else f"{date_range['min_date']} to {date_range['max_date']}"
    )
    st.write(
        f"Found transaction data across **{date_range['count']}** date(s) "
        f"({span}). Running reconciliation covers every one of them, not just today."
    )

run_clicked = st.button(
    "🚀 Run reconciliation",
    type="primary",
    disabled=date_range["count"] == 0,
)

if run_clicked:
    with st.spinner("Reconciling..."):
        result = st.session_state.service.perform_full_reconciliation()
    st.session_state.reconciliation_result = result
    st.rerun()

result = st.session_state.reconciliation_result

if result is not None:
    if result["status"] == "no_data":
        st.warning(result["message"])
    else:
        summary = result["summary"]
        st.subheader("Summary")

        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Total processed", summary["total_processed"])
        m2.metric("Matched", summary["matched"])
        m3.metric("Partially matched", summary["partially_matched"])
        m4.metric("Unmatched", summary["unmatched"])
        m5.metric("Match rate", f"{summary['match_rate']}%")
        st.metric("Discrepancy amount (₹)", f"{summary['discrepancy_amount']:,.2f}")

        chart_data = [
            ("Matched", summary["matched"]),
            ("Partially matched", summary["partially_matched"]),
            ("Unmatched", summary["unmatched"]),
        ]
        _render_count_bars(chart_data)

        with st.expander(f"Per-date breakdown ({len(result['dates_processed'])} date(s))"):
            rows = []
            for day_result in result["daily_results"]:
                r = day_result.get("results", {})
                rows.append({
                    "Date": day_result.get("date"),
                    "Status": day_result.get("status"),
                    "Total": r.get("total_processed", 0),
                    "Matched": r.get("matched", 0),
                    "Partially matched": r.get("partially_matched", 0),
                    "Unmatched": r.get("unmatched", 0),
                    "Match rate %": r.get("match_rate", 0.0),
                })
            _render_table(rows)

        # Split into three separate result sets - one table, one download
        # each - rather than one mixed table, so each can be reviewed and
        # shared on its own.
        matched_rows = result.get("matched_details", [])
        partially_matched_rows = [d for d in result["details"] if d.get("status") == "partially_matched"]
        unmatched_rows = [d for d in result["details"] if d.get("status") == "unmatched"]

        st.subheader("Results by status")

        _render_result_section(
            title="✅ Matched",
            rows=matched_rows,
            display_columns={
                "transaction_id": "Transaction ID", "date": "Date", "amount": "Amount",
                "cr_dr": "Cr/Dr", "payment_type": "Payment Type", "member_id": "Member ID",
                "member_name": "Member Name", "branch": "Branch", "matched_on": "Matched on",
            },
            csv_fieldnames=["transaction_id", "date", "amount", "cr_dr", "payment_type", "member_id", "member_name", "branch", "matched_on"],
            file_name="reconciliation_matched.csv",
            empty_message="No transactions matched cleanly.",
            expanded=False,
        )

        _render_result_section(
            title="🟡 Partially matched",
            rows=partially_matched_rows,
            display_columns={
                "transaction_id": "Transaction ID", "date": "Date", "payment_type": "Payment Type",
                "member_id": "Member ID", "member_name": "Member Name", "branch": "Branch",
                "issues": "Why it's a partial match", "expected": "Expected", "actual": "Actual",
            },
            csv_fieldnames=["transaction_id", "date", "payment_type", "member_id", "member_name", "branch", "issues", "expected", "actual"],
            file_name="reconciliation_partially_matched.csv",
            empty_message="No partially-matched transactions.",
            expanded=True,
        )

        _render_result_section(
            title="🔴 Unmatched",
            rows=unmatched_rows,
            display_columns={
                "transaction_id": "Transaction ID", "date": "Date", "payment_type": "Payment Type",
                "member_id": "Member ID (as read from the bank row - not found on the ledger)",
                "issues": "Why it didn't match",
            },
            csv_fieldnames=["transaction_id", "date", "payment_type", "member_id", "issues"],
            file_name="reconciliation_unmatched.csv",
            empty_message="No unmatched transactions.",
            expanded=True,
        )

        st.subheader("Download full report")
        st.download_button(
            "⬇️ Full report (JSON) — all statuses, all fields",
            data=json.dumps(result, indent=2, default=str),
            file_name="reconciliation_report.json",
            mime="application/json",
        )

_render_back_to_top()