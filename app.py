# app.py
# Streamlit app: Leser safetyvalve spring stock
# - Medewerker: QR scan -> duidelijke scan-success melding -> verbruik boeken -> duidelijke boekingsmelding
# - Admin: voorraad overzicht + CSV export + ontvangst boeken EN direct label-PDF genereren (DYMO) in één actie
# - Email notificatie bij verbruik (SMTP env vars)
# - Admin pincode gate (ADMIN_PIN)

import os
import re
import io
import smtplib
from email.message import EmailMessage
from datetime import datetime, timezone

import pandas as pd
import psycopg2
import streamlit as st
from streamlit_qrcode_scanner import qrcode_scanner

import qrcode
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader

# -----------------------------
# Validation / formats
# -----------------------------
# Flexibel orderformat: ddd-yyL<digits...>  (bv. 005-26R01, 005-26S1, 123-27R001)
ORDER_RE = re.compile(r"^\d{3}-\d{2}[A-Z]\d+$")
INITIALS_RE = re.compile(r"^[A-Z]{2}$")

# DYMO 99012 default: 89mm x 36mm (breedte x hoogte)
LABEL_W_MM = 89
LABEL_H_MM = 36

DEFAULT_ORDER_LETTERS = ["R", "S", "I", "W", "X"]  # pas aan indien gewenst


# -----------------------------
# Database
# -----------------------------
def get_conn():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL ontbreekt.")
    return psycopg2.connect(db_url)


def ensure_tables():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS springs (
                spring_no VARCHAR(50) PRIMARY KEY,
                qty_on_hand INTEGER NOT NULL CHECK (qty_on_hand >= 0),
                updated_at TIMESTAMPTZ NOT NULL
            );
            """)
            cur.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id SERIAL PRIMARY KEY,
                created_at TIMESTAMPTZ NOT NULL,
                type VARCHAR(10) NOT NULL, -- USE / RECEIVE
                spring_no VARCHAR(50) NOT NULL,
                qty INTEGER NOT NULL CHECK (qty > 0),
                initials VARCHAR(2),
                order_no VARCHAR(30),
                note TEXT
            );
            """)
            conn.commit()


def get_stock_row(spring_no: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT spring_no, qty_on_hand FROM springs WHERE spring_no=%s", (spring_no,))
            return cur.fetchone()


def set_stock(spring_no: str, qty_on_hand: int):
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO springs (spring_no, qty_on_hand, updated_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (spring_no)
                DO UPDATE SET qty_on_hand=EXCLUDED.qty_on_hand, updated_at=EXCLUDED.updated_at
            """, (spring_no, qty_on_hand, now))
            conn.commit()


def add_transaction(tx_type: str, spring_no: str, qty: int, initials=None, order_no=None, note=None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO transactions (created_at, type, spring_no, qty, initials, order_no, note)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
            """, (datetime.now(timezone.utc), tx_type, spring_no, qty, initials, order_no, note))
            conn.commit()


def receive_stock(spring_no: str, qty: int, note: str = None):
    row = get_stock_row(spring_no)
    current = row[1] if row else 0
    new_qty = current + qty
    set_stock(spring_no, new_qty)
    add_transaction("RECEIVE", spring_no, qty, note=note)
    return current, new_qty


def use_stock(spring_no: str, qty: int, initials: str, order_no: str):
    row = get_stock_row(spring_no)
    current = row[1] if row else 0
    if current < qty:
        raise ValueError(f"Onvoldoende voorraad: {spring_no} (huidig {current}, gevraagd {qty})")
    new_qty = current - qty
    set_stock(spring_no, new_qty)
    add_transaction("USE", spring_no, qty, initials=initials, order_no=order_no)
    return current, new_qty


def fetch_stock_df():
    with get_conn() as conn:
        return pd.read_sql_query("""
            SELECT spring_no, qty_on_hand, updated_at
            FROM springs
            ORDER BY spring_no ASC
        """, conn)


def fetch_transactions_df(limit=300):
    with get_conn() as conn:
        return pd.read_sql_query("""
            SELECT created_at, type, spring_no, qty, initials, order_no, note
            FROM transactions
            ORDER BY created_at DESC
            LIMIT %s
        """, conn, params=(limit,))


def fetch_spring_numbers():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT spring_no FROM springs ORDER BY spring_no ASC")
            rows = cur.fetchall()
            return [r[0] for r in rows]


# -----------------------------
# Email
# -----------------------------
def send_email(subject: str, body: str):
    to_addr = os.environ.get("EMAIL_TO")
    from_addr = os.environ.get("EMAIL_FROM")
    host = os.environ.get("SMTP_HOST")
    port = os.environ.get("SMTP_PORT")
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")

    if not all([to_addr, from_addr, host, port, user, password]):
        return False, "SMTP/email vars ontbreken (mail niet verstuurd)."

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.set_content(body)

    try:
        with smtplib.SMTP(host, int(port)) as server:
            server.starttls()
            server.login(user, password)
            server.send_message(msg)
        return True, "Mail verstuurd."
    except Exception as e:
        return False, f"Mail fout: {e}"


# -----------------------------
# Admin gate
# -----------------------------
def admin_gate():
    required = os.environ.get("ADMIN_PIN")
    if not required:
        st.error("ADMIN_PIN is niet ingesteld in Railway Variables.")
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


# -----------------------------
# QR label PDF generator (DYMO)
# -----------------------------
def make_label_pdf(spring_no: str, count: int) -> bytes:
    """
    1 label per pagina. QR bevat het veernummer.
    Standaard labelmaat: 89x36mm (DYMO 99012).
    """
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(LABEL_W_MM * mm, LABEL_H_MM * mm))

    qr = qrcode.QRCode(border=1, box_size=10)
    qr.add_data(spring_no)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")

    qr_size_mm = 26
    text_size = 12

    for _ in range(count):
        # Tekst linksboven
        c.setFont("Helvetica-Bold", text_size)
        c.drawString(4 * mm, (LABEL_H_MM - 12) * mm, spring_no)

        # QR rechts
        img_buf = io.BytesIO()
        img.save(img_buf, format="PNG")
        img_buf.seek(0)
        x = (LABEL_W_MM - qr_size_mm - 4) * mm
        y = (LABEL_H_MM - qr_size_mm - 4) * mm
        c.drawImage(ImageReader(img_buf), x, y, qr_size_mm * mm, qr_size_mm * mm, preserveAspectRatio=True, mask="auto")

        c.showPage()

    c.save()
    return buf.getvalue()


# -----------------------------
# Order input helpers
# -----------------------------
def current_year_2digits():
    return f"{datetime.now(timezone.utc).year % 100:02d}"


def build_order_no(customer_code: str, year_2: str, letter: str, seq: str) -> str:
    return f"{customer_code}-{year_2}{letter}{seq}"


def valid_customer_code(s: str) -> bool:
    return bool(re.fullmatch(r"\d{3}", s))


def valid_year2(s: str) -> bool:
    return bool(re.fullmatch(r"\d{2}", s))


def valid_seq(s: str) -> bool:
    return bool(re.fullmatch(r"\d+", s))


# -----------------------------
# App UI
# -----------------------------
st.set_page_config(page_title="Leser Veer Voorraad", layout="centered")
ensure_tables()

# Session defaults
st.session_state.setdefault("spring_no", "")
st.session_state.setdefault("scan_ok", False)
st.session_state.setdefault("last_use_success", None)   # dict with last use info
st.session_state.setdefault("last_receive_pdf", None)   # bytes
st.session_state.setdefault("last_receive_pdf_name", None)

page = st.sidebar.radio("Menu", ["📱 Verbruik (medewerker)", "📦 Voorraad & Ontvangst (admin)"])

# =============================
# PAGE 1: Medewerker verbruik
# =============================
if page == "📱 Verbruik (medewerker)":
    st.title("Veer verbruik (QR)")

    # Toon laatste succesvolle afboeking (blijft staan na rerun)
    if st.session_state.get("last_use_success"):
        info = st.session_state["last_use_success"]
        st.success(
            f"""✅ **Verbruik succesvol afgeboekt**

**Veer:** {info['spring_no']}  
**Order:** {info['order_no']}  
**Aantal:** {info['qty']}  
**Voorraad:** {info['before']} → {info['after']}
"""
        )
        st.info("🔄 Klaar voor volgende scan")
        if st.button("Nieuwe scan"):
            st.session_state["last_use_success"] = None
            st.rerun()

    st.subheader("1) Scan QR")
    qr = qrcode_scanner(key="scan")

    if qr:
        st.session_state["spring_no"] = qr.strip()
        st.session_state["scan_ok"] = True

    spring_no = st.session_state.get("spring_no", "")

    # Melding bij succesvolle scan
    if st.session_state.get("scan_ok") and spring_no:
        st.success(f"✅ QR gescand – Veernummer: **{spring_no}**")

    if not spring_no:
        st.info("📷 Richt de camera op de QR-code om te scannen.")

    st.subheader("2) Verbruik registreren")

    with st.form("use_form", clear_on_submit=True):
        c1, c2 = st.columns(2)
        initials = c1.text_input("Initialen (2 letters)", max_chars=2, placeholder="PV")

        st.markdown("**Ordernummer** (format: `005-26R01` / `ddd-yyLnnn...`)")
        oc1, oc2, oc3, oc4 = st.columns([1, 1, 1, 2])
        customer_code = oc1.text_input("Klant (ddd)", max_chars=3, placeholder="005")
        year_2 = oc2.text_input("Jaar (yy)", max_chars=2, value=current_year_2digits())
        letter = oc3.selectbox("Letter", options=DEFAULT_ORDER_LETTERS, index=0)
        seq = oc4.text_input("Volgnummer", placeholder="01")

        qty = st.number_input("Aantal", min_value=1, step=1, value=1)
        submit = st.form_submit_button("✅ Verbruik (afboeken)")

    if submit:
        initials_clean = initials.strip().upper()
        spring_clean = (spring_no or "").strip()
        customer_clean = customer_code.strip()
        year_clean = year_2.strip()
        seq_clean = seq.strip()

        order_no = build_order_no(customer_clean, year_clean, letter, seq_clean).upper()

        errors = []
        if not spring_clean:
            errors.append("Scan eerst een QR-code (veernummer ontbreekt).")
        if not INITIALS_RE.match(initials_clean):
            errors.append("Initialen moeten precies 2 letters zijn (bv. PV).")
        if not valid_customer_code(customer_clean):
            errors.append("Klantcode moet exact 3 cijfers zijn (bv. 005).")
        if not valid_year2(year_clean):
            errors.append("Jaar moet exact 2 cijfers zijn (bv. 26).")
        if not valid_seq(seq_clean):
            errors.append("Volgnummer moet alleen cijfers bevatten (bv. 01, 1, 001).")
        if not ORDER_RE.match(order_no):
            errors.append("Ordernummer ongeldig. Verwacht bv. 005-26R01 (ddd-yyLnnn...).")
        if int(qty) < 1:
            errors.append("Aantal moet 1 of groter zijn.")

        if errors:
            for e in errors:
                st.error(e)
        else:
            try:
                before, after = use_stock(spring_clean, int(qty), initials_clean, order_no)

                # Email notificatie
                subj = f"Veer gebruikt – {order_no}"
                body = (
                    f"Er is een veer gebruikt.\n\n"
                    f"Order: {order_no}\n"
                    f"Veer: {spring_clean}\n"
                    f"Aantal: {int(qty)}\n"
                    f"Medewerker: {initials_clean}\n"
                    f"Tijd: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
                    f"Voorraad nu: {after}\n"
                )
                ok, msg = send_email(subj, body)

                # UI feedback: success blok + (optioneel) mail status
                st.session_state["last_use_success"] = {
                    "spring_no": spring_clean,
                    "order_no": order_no,
                    "qty": int(qty),
                    "before": before,
                    "after": after,
                    "email_ok": ok,
                    "email_msg": msg,
                }

                # reset scan state for next run
                st.session_state["spring_no"] = ""
                st.session_state["scan_ok"] = False

                # Toon korte status over mail (bij volgende render tonen we het erbij)
                st.rerun()

            except ValueError as ve:
                st.error(str(ve))

    # Als we net een succes hadden, toon mailstatus als extra info
    if st.session_state.get("last_use_success"):
        info = st.session_state["last_use_success"]
        if info.get("email_ok"):
            st.info("📧 Email melding verstuurd.")
        else:
            st.warning(f"📧 Geen email verstuurd: {info.get('email_msg')}")

# =============================
# PAGE 2: Admin voorraad + ontvangst + labels
# =============================
else:
    st.title("Voorraad & Ontvangst (Admin)")
    if not admin_gate():
        st.stop()

    # Voorraad overzicht
    st.subheader("Voorraad overzicht")
    stock_df = fetch_stock_df()
    if stock_df.empty:
        st.info("Nog geen veernummers in voorraad. Boek eerst een ontvangst.")
    else:
        st.dataframe(stock_df[["spring_no", "qty_on_hand", "updated_at"]], use_container_width=True)

        csv_bytes = stock_df[["spring_no", "qty_on_hand"]].to_csv(index=False).encode("utf-8")
        st.download_button(
            "⬇️ Download voorraad CSV",
            data=csv_bytes,
            file_name="veer_voorraad.csv",
            mime="text/csv",
        )

    st.divider()
    st.subheader("Ontvangst boeken + labels printen (1x)")

    # Handig: dropdown van bestaande veernummers, maar ook vrij typen
    existing = fetch_spring_numbers()
    spring_pick = st.selectbox("Bestaand veernummer (optioneel)", options=["(nieuw)"] + existing)
    default_spring = "" if spring_pick == "(nieuw)" else spring_pick

    with st.form("receive_and_labels_form", clear_on_submit=True):
        spring_no = st.text_input("Veernummer", value=default_spring, placeholder="LSR-12345")
        qty_received = st.number_input("Aantal ontvangen (voorraad +)", min_value=1, step=1, value=1)

        c1, c2 = st.columns(2)
        auto_labels = c1.checkbox("Labels = ontvangen", value=True)
        labels_count = c2.number_input("Aantal labels te printen", min_value=1, step=1, value=1)

        note = st.text_input("Opmerking (optioneel)", placeholder="Levering / pakbon ...")

        submit = st.form_submit_button("➕ Ontvangst boeken & labels maken")

    if submit:
        spring_clean = spring_no.strip()
        if not spring_clean:
            st.error("Veernummer is verplicht.")
        else:
            label_qty = int(qty_received) if auto_labels else int(labels_count)

            before, after = receive_stock(spring_clean, int(qty_received), note=note.strip() or None)
            st.success(f"✅ Ontvangst geboekt: {spring_clean} +{int(qty_received)} (voorraad {before} → {after})")

            pdf = make_label_pdf(spring_clean, label_qty)
            st.session_state["last_receive_pdf"] = pdf
            st.session_state["last_receive_pdf_name"] = f"labels_{spring_clean}.pdf"

            st.success(f"✅ Labels gegenereerd ({label_qty} stuks). Download hieronder en print via DYMO Connect.")
            st.rerun()

    # Download knop blijft beschikbaar na rerun
    if st.session_state.get("last_receive_pdf"):
        st.download_button(
            "⬇️ Download label PDF",
            data=st.session_state["last_receive_pdf"],
            file_name=st.session_state.get("last_receive_pdf_name", "labels.pdf"),
            mime="application/pdf",
        )

    st.divider()
    st.subheader("Laatste transacties (logboek)")
    tx_df = fetch_transactions_df(limit=300)
    st.dataframe(tx_df, use_container_width=True)
