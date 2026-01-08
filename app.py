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
from PIL import Image


ORDER_RE = re.compile(r"^\d{3}-\d{2}R\d{2}$")   # 005-26R01
INITIALS_RE = re.compile(r"^[A-Z]{2}$")         # 2 letters

# DYMO LabelWriter 99012 = 36mm x 89mm (default)
LABEL_W_MM = 89
LABEL_H_MM = 36


def get_conn():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL ontbreekt.")
    return psycopg2.connect(db_url)


def ensure_tables():
    with get_conn() as conn:
        with conn.cursor() as cur:
            # voorraadstand per veernummer
            cur.execute("""
            CREATE TABLE IF NOT EXISTS springs (
                spring_no VARCHAR(50) PRIMARY KEY,
                qty_on_hand INTEGER NOT NULL CHECK (qty_on_hand >= 0),
                updated_at TIMESTAMPTZ NOT NULL
            );
            """)
            # logboek van alle bewegingen
            cur.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id SERIAL PRIMARY KEY,
                created_at TIMESTAMPTZ NOT NULL,
                type VARCHAR(10) NOT NULL, -- USE / RECEIVE
                spring_no VARCHAR(50) NOT NULL,
                qty INTEGER NOT NULL CHECK (qty > 0),
                initials VARCHAR(2),
                order_no VARCHAR(20),
                note TEXT
            );
            """)
            conn.commit()


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


def send_email(subject: str, body: str):
    """
    Stuurt mail als SMTP vars aanwezig zijn.
    Faalt mail? Dan laten we de app niet crashen.
    """
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


def fetch_transactions_df(limit=500):
    with get_conn() as conn:
        return pd.read_sql_query("""
            SELECT created_at, type, spring_no, qty, initials, order_no, note
            FROM transactions
            ORDER BY created_at DESC
            LIMIT %s
        """, conn, params=(limit,))


def make_label_pdf(spring_no: str, count: int) -> bytes:
    """
    Genereert PDF met 1 label per pagina (DYMO labelmaat default 89x36mm).
    QR bevat spring_no.
    """
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(LABEL_W_MM * mm, LABEL_H_MM * mm))

    # QR image maken (PIL)
    qr = qrcode.QRCode(border=1, box_size=10)
    qr.add_data(spring_no)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")

    # QR grootte in mm op label (aanpasbaar)
    qr_size_mm = 26  # mooi op 36mm hoog label
    text_size = 12

    for _ in range(count):
        # Tekst links
        c.setFont("Helvetica-Bold", text_size)
        c.drawString(4 * mm, (LABEL_H_MM - 12) * mm, spring_no)

        # QR rechts
        # PIL -> bytes -> reportlab image
        img_buf = io.BytesIO()
        img.save(img_buf, format="PNG")
        img_buf.seek(0)

        x = (LABEL_W_MM - qr_size_mm - 4) * mm
        y = (LABEL_H_MM - qr_size_mm - 4) * mm
        c.drawImage(ImageReader(img_buf), x, y, qr_size_mm * mm, qr_size_mm * mm, preserveAspectRatio=True, mask='auto')

        c.showPage()

    c.save()
    return buf.getvalue()


# Reportlab helper for PIL Image
from reportlab.lib.utils import ImageReader

# ---------- APP ----------
st.set_page_config(page_title="Leser Veer Voorraad", layout="centered")
ensure_tables()

page = st.sidebar.radio("Menu", ["📱 Verbruik (medewerker)", "📦 Voorraad & Ontvangst (admin)", "🖨️ QR labels (admin)"])

# ---------- PAGE: Verbruik ----------
if page == "📱 Verbruik (medewerker)":
    st.title("Veer verbruik (QR)")

    st.subheader("1) Scan QR")
    qr = qrcode_scanner(key="scan")
    if qr:
        st.session_state["spring_no"] = qr.strip()

    spring_no = st.session_state.get("spring_no", "")
    if spring_no:
        st.success(f"Veernummer: {spring_no}")

    st.subheader("2) Verbruik registreren")
    with st.form("use_form", clear_on_submit=True):
        c1, c2 = st.columns(2)
        initials = c1.text_input("Initialen (2 letters)", max_chars=2, placeholder="PV")
        order_no = c2.text_input("Ordernummer", placeholder="005-26R01")
        qty = st.number_input("Aantal", min_value=1, step=1, value=1)
        submit = st.form_submit_button("✅ Verbruik")

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
            try:
                before, after = use_stock(spring_clean, int(qty), initials_clean, order_clean)
                st.success(f"✅ Verbruik geboekt. Voorraad {spring_clean}: {before} → {after}")

                # email melding
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
                if ok:
                    st.info("📧 Email melding verstuurd.")
                else:
                    st.warning(f"📧 Geen email verstuurd: {msg}")

                st.session_state["spring_no"] = ""
                st.rerun()
            except ValueError as ve:
                st.error(str(ve))

# ---------- PAGE: Admin voorraad/ontvangst ----------
elif page == "📦 Voorraad & Ontvangst (admin)":
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
    st.subheader("Ontvangst boeken (voorraad +)")
    with st.form("receive_form", clear_on_submit=True):
        spring_no = st.text_input("Veernummer", placeholder="LSR-12345")
        qty = st.number_input("Aantal ontvangen", min_value=1, step=1, value=1)
        note = st.text_input("Opmerking (optioneel)", placeholder="Levering leverancier X / pakbon ...")
        submit = st.form_submit_button("➕ Ontvangst boeken")

    if submit:
        spring_clean = spring_no.strip()
        if not spring_clean:
            st.error("Veernummer is verplicht.")
        else:
            receive_stock(spring_clean, int(qty), note=note.strip() or None)
            st.success(f"✅ Ontvangst geboekt: {spring_clean} +{int(qty)}")
            st.rerun()

    st.divider()
    st.subheader("Laatste transacties (logboek)")
    tx_df = fetch_transactions_df(limit=300)
    st.dataframe(tx_df, use_container_width=True)

# ---------- PAGE: QR Labels ----------
else:
    st.title("QR labels (Admin)")
    if not admin_gate():
        st.stop()

    st.write("Genereer een PDF met 1 label per pagina. QR bevat het veernummer.")

    with st.form("labels_form"):
        spring_no = st.text_input("Veernummer", placeholder="LSR-12345")
        count = st.number_input("Aantal labels", min_value=1, step=1, value=1)
        st.caption(f"Label formaat (default): {LABEL_W_MM}×{LABEL_H_MM} mm (pas dit aan in code als jouw rol anders is).")
        submit = st.form_submit_button("🖨️ Genereer label PDF")

    if submit:
        spring_clean = spring_no.strip()
        if not spring_clean:
            st.error("Veernummer is verplicht.")
        else:
            pdf = make_label_pdf(spring_clean, int(count))
            st.download_button(
                "⬇️ Download label PDF",
                data=pdf,
                file_name=f"labels_{spring_clean}.pdf",
                mime="application/pdf",
            )
            st.success("✅ PDF klaar. Open in DYMO Connect en print.")
