# app.py
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
# Flexibel KEN orderformat: ddd-yyL<digits...>  (bv. 005-26R01, 005-26S1, 123-27R001)
ORDER_RE = re.compile(r"^\d{3}-\d{2}[A-Z]\d+$")
INITIALS_RE = re.compile(r"^[A-Z]{2}$")

# DYMO 99012 default: 89mm x 36mm (breedte x hoogte)
LABEL_W_MM = 89
LABEL_H_MM = 36


# -----------------------------
# UX: browser beep on scan
# -----------------------------
def play_beep():
    # ultrakorte embedded WAV (base64)
    beep_base64 = "UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQAAAAA="
    st.markdown(
        f"""
        <audio autoplay>
            <source src="data:audio/wav;base64,{beep_base64}" type="audio/wav">
        </audio>
        """,
        unsafe_allow_html=True,
    )


def normalize_scan_value(qr_result) -> str:
    """
    Maakt scan-output robuust:
    - soms string
    - soms dict/object met 'text' of 'data'
    - altijd terug als nette string
    """
    if qr_result is None:
        return ""
    if isinstance(qr_result, str):
        return qr_result.strip()
    if isinstance(qr_result, dict):
        for k in ("text", "data", "raw", "result", "value"):
            if k in qr_result and qr_result[k]:
                return str(qr_result[k]).strip()
        return str(qr_result).strip()
    return str(qr_result).strip()


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
            return [r[0] for r in cur.fetchall()]


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
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(LABEL_W_MM * mm, LABEL_H_MM * mm))

    qr = qrcode.QRCode(border=1, box_size=10)
    qr.add_data(spring_no)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")

    qr_size_mm = 26
    text_size = 12

    for _ in range(count):
        c.setFont("Helvetica-Bold", text_size)
        c.drawString(4 * mm, (LABEL_H_MM - 12) * mm, spring_no)

        img_buf = io.BytesIO()
        img.save(img_buf, format="PNG")
        img_buf.seek(0)

        x = (LABEL_W_MM - qr_size_mm - 4) * mm
        y = (LABEL_H_MM - qr_size_mm - 4) * mm
        c.drawImage(ImageReader(img_buf), x, y, qr_size_mm * mm, qr_size_mm * mm,
                    preserveAspectRatio=True, mask="auto")
        c.showPage()

    c.save()
    return buf.getvalue()


# -----------------------------
# App UI
# -----------------------------
st.set_page_config(page_title="Leser Veer Voorraad", layout="centered")
ensure_tables()

# Session defaults
st.session_state.setdefault("spring_no", "")
st.session_state.setdefault("last_scanned", "")          # helpt om beep 1x per nieuwe scan te doen
st.session_state.setdefault("last_use_success", None)
st.session_state.setdefault("last_receive_pdf", None)
st.session_state.setdefault("last_receive_pdf_name", None)

page = st.sidebar.radio("Menu", ["📱 Verbruik (medewerker)", "📦 Voorraad & Ontvangst (admin)"])

# =============================
# PAGE 1: Medewerker verbruik
# =============================
if page == "📱 Verbruik (medewerker)":
    st.title("Veer verbruik (QR)")

    # Laatste succesvolle afboeking
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
        if info.get("email_ok"):
            st.info("📧 Email melding verstuurd.")
        else:
            st.warning(f"📧 Geen email verstuurd: {info.get('email_msg')}")
        st.info("🔄 Klaar voor volgende scan")
        if st.button("Nieuwe scan"):
            st.session_state["last_use_success"] = None
            st.rerun()

    st.subheader("1) Scan QR")
    qr_raw = qrcode_scanner(key="scan")
    qr_text = normalize_scan_value(qr_raw)

    if qr_text:
        # beep alleen als het echt een nieuwe scan is
        if qr_text != st.session_state.get("last_scanned", ""):
            st.session_state["spring_no"] = qr_text
            st.session_state["last_scanned"] = qr_text
            play_beep()

    spring_no = st.session_state.get("spring_no", "")

    if spring_no:
        st.success(f"✅ QR gescand – Veernummer: **{spring_no}**")
    else:
        st.info("📷 Richt de camera op de QR-code om te scannen.")

    st.subheader("2) Verbruik registreren")
    with st.form("use_form", clear_on_submit=True):
        c1, c2 = st.columns(2)
        initials = c1.text_input("Initialen (2 letters)", max_chars=2, placeholder="PV")
        order_no = c2.text_input("Ordernummer", placeholder="005-26R01")

        qty = st.number_input("Aantal", min_value=1, step=1, value=1)
        st.caption("Format: 005-26R01 (ddd-yyLnnn...)")

        submit = st.form_submit_button("✅ Verbruik (afboeken)")

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
            errors.append("Ordernummer ongeldig. Verwacht bv. 005-26R01 (ddd-yyLnnn...).")
        if int(qty) < 1:
            errors.append("Aantal moet 1 of groter zijn.")

        if errors:
            for e in errors:
                st.error(e)
        else:
            try:
                before, after = use_stock(spring_clean, int(qty), initials_clean, order_clean)

                # Email (fail-safe)
                subj = f"Veer gebruikt – {order_clean}"
                body = (
                    f"Er is een veer gebruikt.\n\n"
                    f"Order: {order_clean}\n"
                    f"Veer: {spring_clean}\n"
                    f"Aantal: {int(qty)}\n"
                    f"Medewerker: {initials_clean}\n"
                    f"Tijd: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
                    f"Voorraad nu: {after}\n"
                )
                ok, msg = send_email(subj, body)

                st.session_state["last_use_success"] = {
                    "spring_no": spring_clean,
                    "order_no": order_clean,
                    "qty": int(qty),
                    "before": before,
                    "after": after,
                    "email_ok": ok,
                    "email_msg": msg,
                }

                # reset voor volgende scan
                st.session_state["spring_no"] = ""
                st.session_state["last_scanned"] = ""  # zodat volgende scan weer beep geeft

                st.rerun()
            except ValueError as ve:
                st.error(str(ve))

# =============================
# PAGE 2: Admin voorraad + ontvangst + labels
# =============================
else:
    st.title("Voorraad & Ontvangst (Admin)")
    if not admin_gate():
        st.stop()

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

    existing = fetch_spring_numbers()
    picked = st.selectbox("Bestaand veernummer (optioneel)", options=["(nieuw)"] + existing)
    default_spring = "" if picked == "(nieuw)" else picked

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
