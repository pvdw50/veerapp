import os, re
from datetime import datetime, timezone

import streamlit as st
import psycopg2
from streamlit_qrcode_scanner import qrcode_scanner  # pip install streamlit-qrcode-scanner

ORDER_RE = re.compile(r"^\d{3}-\d{2}R\d{2}$")    # 005-26R01
INITIALS_RE = re.compile(r"^[A-Z]{2}$")          # 2 letters

def get_conn():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL ontbreekt (Railway/hosting env var).")
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

def insert_request(initials, order_no, spring_no, qty):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO requests (created_at, initials, order_no, spring_no, qty, status)
                VALUES (%s,%s,%s,%s,%s,'OPEN')
            """, (datetime.now(timezone.utc), initials, order_no, spring_no, qty))
            conn.commit()

def fetch_open(limit=200):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT created_at, initials, order_no, spring_no, qty
                FROM requests
                WHERE status='OPEN'
                ORDER BY created_at DESC
                LIMIT %s
            """, (limit,))
            return cur.fetchall()

st.set_page_config(page_title="Veer-aanvraag", layout="centered")
st.title("Veer aanvraag (QR)")

ensure_table()

st.subheader("1) Scan QR")
qr = qrcode_scanner(key="scan")  # werkt via telefooncamera; alleen goed met HTTPS
if qr:
    st.session_state["spring_no"] = qr.strip()

spring_no = st.session_state.get("spring_no", "")
if spring_no:
    st.success(f"Veernummer: {spring_no}")

st.subheader("2) Vul in & verstuur")
with st.form("f", clear_on_submit=True):
    initials = st.text_input("Initialen (2 letters)", max_chars=2, placeholder="PV")
    order_no = st.text_input("Ordernummer", placeholder="005-26R01")
    qty = st.number_input("Aantal", min_value=1, step=1, value=1)
    submit = st.form_submit_button("Versturen")

if submit:
    initials = initials.strip().upper()
    order_no = order_no.strip().upper()
    spring_no_clean = spring_no.strip()

    errors = []
    if not spring_no_clean:
        errors.append("Scan eerst een QR-code (veernummer ontbreekt).")
    if not INITIALS_RE.match(initials):
        errors.append("Initialen moeten precies 2 letters zijn (bv. PV).")
    if not ORDER_RE.match(order_no):
        errors.append("Ordernummer ongeldig. Gebruik format 005-26R01.")
    if qty < 1:
        errors.append("Aantal moet 1 of groter zijn.")

    if errors:
        for e in errors:
            st.error(e)
    else:
        insert_request(initials, order_no, spring_no_clean, int(qty))
        st.success("✅ Verzonden!")
        st.session_state["spring_no"] = ""
        st.rerun()

st.divider()
st.subheader("Open aanvragen (OPEN)")
rows = fetch_open()
if not rows:
    st.info("Geen open aanvragen.")
else:
    for created_at, initials, order_no, spring_no, qty in rows:
        st.write(f"- {created_at:%Y-%m-%d %H:%M} | {initials} | {order_no} | {spring_no} | x{qty}")
