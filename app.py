import os
import re
from datetime import datetime, timezone

import pandas as pd
import psycopg2
import streamlit as st
from streamlit_qrcode_scanner import qrcode_scanner

ORDER_RE = re.compile(r"^\d{3}-\d{2}R\d{2}$")   # 005-26R01
INITIALS_RE = re.compile(r"^[A-Z]{2}$")         # 2 letters

def get_conn():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL ontbreekt.")
    return psycopg2.connect(db_url)

def ensure_table():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS requests (
                id SERIAL PRIMARY KEY,
                created_at TIMESTAMPTZ NOT NULL,
                initials VARCHAR(2) NOT NULL,
                order_no VARCHAR(20) NOT NULL,
                spring_no VARCHAR(50) NOT NULL,
                qty INTEGER NOT NULL CHECK (qty >= 1),
                status VARCHAR(10) NOT NULL DEFAULT 'OPEN'
            );
            """)
            conn.commit()

def insert_request(initials: str, order_no: str, spring_no: str, qty: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO requests (created_at, initials, order_no, spring_no, qty, status)
                VALUES (%s,%s,%s,%s,%s,'OPEN')
            """, (datetime.now(timezone.utc), initials, order_no, spring_no, qty))
            conn.commit()

def fetch_open_requests():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, created_at, initials, order_no, spring_no, qty
                FROM requests
                WHERE status='OPEN'
                ORDER BY created_at DESC
            """)
            return cur.fetchall()

def fetch_open_by_order(order_no: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, created_at, initials, order_no, spring_no, qty
                FROM requests
                WHERE status='OPEN' AND order_no=%s
                ORDER BY created_at ASC
            """, (order_no,))
            return cur.fetchall()

def mark_order_as_ordered(order_no: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE requests
                SET status='ORDERED'
                WHERE status='OPEN' AND order_no=%s
            """, (order_no,))
            conn.commit()

def to_df(rows):
    # rows: (id, created_at, initials, order_no, spring_no, qty)
    return pd.DataFrame(rows, columns=["id", "created_at", "initials", "order_no", "spring_no", "qty"])

def admin_gate():
    """Return True if admin pin ok, else False."""
    required = os.environ.get("ADMIN_PIN")
    if not required:
        st.error("ADMIN_PIN is niet ingesteld in de hosting variables.")
        return False

    if "admin_ok" not in st.session_state:
        st.session_state["admin_ok"] = False

    if st.session_state["admin_ok"]:
        return True

    st.info("Admin toegang vereist.")
    pin = st.text_input("Admin pincode", type="password")
    if st.button("Inloggen"):
        if pin.strip() == required.strip():
            st.session_state["admin_ok"] = True
            st.success("✅ Ingelogd")
            st.rerun()
        else:
            st.error("❌ Verkeerde pincode")
    return False

# ---------- UI ----------
st.set_page_config(page_title="Veer-aanvraag", layout="centered")
ensure_table()

page = st.sidebar.radio("Menu", ["📱 Aanvraag (medewerker)", "🧾 Bestellijst (admin)"])

# ---------- PAGE 1: MEDERWERKER ----------
if page == "📱 Aanvraag (medewerker)":
    st.title("Veer aanvraag (QR)")

    st.subheader("1) Scan QR")
    qr = qrcode_scanner(key="scan")  # werkt met telefooncamera (HTTPS)

    if qr:
        st.session_state["spring_no"] = qr.strip()

    spring_no = st.session_state.get("spring_no", "")
    if spring_no:
        st.success(f"Veernummer: {spring_no}")

    st.subheader("2) Vul in & verstuur")
    with st.form("request_form", clear_on_submit=True):
        c1, c2 = st.columns(2)
        initials = c1.text_input("Initialen (2 letters)", max_chars=2, placeholder="PV")
        order_no = c2.text_input("Ordernummer", placeholder="005-26R01")
        qty = st.number_input("Aantal", min_value=1, step=1, value=1)
        st.caption("Order format: 005-26R01")
        submit = st.form_submit_button("Versturen")

    if submit:
        initials_clean = initials.strip().upper()
        order_clean = order_no.strip().upper()
        spring_clean = (spring_no or "").strip()

        errors = []
        if not spring_clean:
            errors.append("Scan eerst een QR-code (veernummer ontbreekt).")
        if not INITIALS_RE.match(initials_clean):
            errors.append("Initialen moeten precies 2 letters zijn (bv. PV).")
        if not ORDER_RE.match(order_clean):
            errors.append("Ordernummer ongeldig. Gebruik format 005-26R01.")
        if int(qty) < 1:
            errors.append("Aantal moet 1 of groter zijn.")

        if errors:
            for e in errors:
                st.error(e)
        else:
            insert_request(initials_clean, order_clean, spring_clean, int(qty))
            st.success("✅ Verzonden!")
            st.session_state["spring_no"] = ""
            st.rerun()

# ---------- PAGE 2: ADMIN ----------
else:
    st.title("Bestellijst (Admin)")
    if not admin_gate():
        st.stop()

    rows = fetch_open_requests()
    df = to_df(rows)

    if df.empty:
        st.info("Geen OPEN aanvragen.")
        st.stop()

    st.subheader("OPEN aanvragen (alles)")
    st.dataframe(df[["created_at", "initials", "order_no", "spring_no", "qty"]], use_container_width=True)

    st.divider()
    st.subheader("Per ordernummer")
    orders = sorted(df["order_no"].unique().tolist())
    selected = st.selectbox("Selecteer order", options=orders)

    order_rows = fetch_open_by_order(selected)
    odf = to_df(order_rows)

    st.write(f"OPEN regels voor **{selected}**:")
    st.dataframe(odf[["created_at", "initials", "spring_no", "qty"]], use_container_width=True)

    # Export CSV voor dit order
    csv_bytes = odf[["created_at", "initials", "order_no", "spring_no", "qty"]].to_csv(index=False).encode("utf-8")
    st.download_button(
        label="⬇️ Download CSV (dit order)",
        data=csv_bytes,
        file_name=f"veer_bestellijst_{selected}.csv",
        mime="text/csv",
    )

    # Markeer order als besteld
    st.warning("Let op: dit zet alle OPEN regels van dit order naar ORDERED.")
    if st.button("✅ Markeer dit order als BESTELD"):
        mark_order_as_ordered(selected)
        st.success(f"{selected} gemarkeerd als ORDERED.")
        st.rerun()
