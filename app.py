import streamlit as st
import pandas as pd
from datetime import datetime
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import io
import zipfile
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
import os
import traceback
import duckdb

# ---------- QR DECODER ----------
try:
    from PIL import Image
    import pyzbar.pyzbar as pyzbar
    QR_AVAILABLE = True
except ImportError:
    QR_AVAILABLE = False

def decode_qr_from_image(image):
    if not QR_AVAILABLE:
        return None
    try:
        rgb_image = image.convert('RGB')
        decoded_objects = pyzbar.decode(rgb_image)
        for obj in decoded_objects:
            return obj.data.decode('utf-8')
    except Exception:
        pass
    return None

# ---------- DATABASE SETUP (DuckDB with Persistent Disk) ----------
# On Render/Railway, the persistent disk is mounted at /app/data.
# Locally, we fall back to the current directory.
DATA_DIR = "/app/data" if os.path.exists("/app/data") else "."
DB_PATH = os.path.join(DATA_DIR, "pigment.duckdb")  # .duckdb extension

def get_db_connection():
    """Return a DuckDB connection."""
    return duckdb.connect(DB_PATH)

def init_db():
    """Create tables if they don't exist, seed default data."""
    conn = get_db_connection()
    # Check if colour_codes table exists
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='colour_codes'").df()
    if tables.empty:
        # Tables don't exist, create them
        conn.execute('''CREATE TABLE colour_codes (
                        id INTEGER PRIMARY KEY,
                        code TEXT UNIQUE NOT NULL,
                        description TEXT
                    )''')
        conn.execute('''CREATE TABLE recipes (
                        id INTEGER PRIMARY KEY,
                        colour_code_id INTEGER NOT NULL,
                        colour_name TEXT NOT NULL,
                        tsc_min REAL, tsc_max REAL, ph_min REAL, ph_max REAL,
                        visc_min REAL, visc_max REAL, de_max REAL,
                        dl_tolerance REAL DEFAULT 0.5, da_tolerance REAL DEFAULT 0.6,
                        db_tolerance REAL DEFAULT 0.6, strength_min REAL DEFAULT 95.0,
                        strength_max REAL DEFAULT 105.0,
                        UNIQUE(colour_code_id, colour_name)
                    )''')
        conn.execute('''CREATE TABLE batches (
                        batch_id TEXT PRIMARY KEY,
                        batch_number TEXT UNIQUE,
                        recipe_id INTEGER,
                        colour_code TEXT,
                        status TEXT, stage TEXT,
                        tsc REAL, ph REAL, visc REAL,
                        de REAL, dl REAL, da REAL, db REAL, colour_strength REAL,
                        manufacturing_date TEXT, attempt_count INTEGER DEFAULT 0,
                        remark TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )''')
        conn.execute('''CREATE TABLE seq_counter (
                        colour_code TEXT PRIMARY KEY, last_seq INTEGER DEFAULT 0
                    )''')
        conn.execute('''CREATE TABLE users (
                        username TEXT PRIMARY KEY,
                        password TEXT NOT NULL,
                        role TEXT NOT NULL
                    )''')
        conn.execute('''CREATE TABLE logs (
                        log_id INTEGER PRIMARY KEY,
                        timestamp TEXT,
                        username TEXT,
                        action TEXT,
                        details TEXT,
                        batch_number TEXT,
                        recipe_id INTEGER
                    )''')
        # Seed default users
        default_users = [
            ("admin", "admin123", "Admin"),
            ("production", "prod123", "Production"),
            ("qa", "qa123", "QA")
        ]
        conn.executemany("INSERT INTO users (username, password, role) VALUES (?,?,?)", default_users)
        # Seed sample colour codes
        sample_codes = [
            ("RED", "Red shades"),
            ("BLUE", "Blue shades"),
            ("GREEN", "Green shades"),
            ("YELLOW", "Yellow shades")
        ]
        conn.executemany("INSERT INTO colour_codes (code, description) VALUES (?,?)", sample_codes)
        conn.commit()
        conn.close()
        return "Database initialized with sample data."
    else:
        conn.close()
        return "Using existing database."

# Call init_db once at startup
init_msg = init_db()

# ---------- LOGGING ----------
def add_log(username, action, details, batch_number=None, recipe_id=None):
    try:
        conn = get_db_connection()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("INSERT INTO logs (timestamp, username, action, details, batch_number, recipe_id) VALUES (?,?,?,?,?,?)",
                     (timestamp, username, action, details, batch_number, recipe_id))
        conn.commit()
        conn.close()
    except:
        pass

# ---------- DATABASE FUNCTIONS (with caching) ----------
@st.cache_data(ttl=300)
def get_colour_codes():
    conn = get_db_connection()
    df = conn.execute("SELECT * FROM colour_codes ORDER BY code").df()
    conn.close()
    return df

def add_colour_code(code, description, username):
    try:
        conn = get_db_connection()
        conn.execute("INSERT INTO colour_codes (code, description) VALUES (?,?)", (code, description))
        conn.commit()
        conn.close()
        add_log(username, "Add Colour Code", f"Added colour code {code}")
        return True, None
    except Exception as e:
        return False, str(e)

def update_colour_code(code_id, code, description, username):
    try:
        conn = get_db_connection()
        conn.execute("UPDATE colour_codes SET code=?, description=? WHERE id=?", (code, description, code_id))
        conn.commit()
        conn.close()
        add_log(username, "Update Colour Code", f"Updated colour code {code}")
        return True, None
    except Exception as e:
        return False, str(e)

def delete_colour_code(code_id, username):
    try:
        conn = get_db_connection()
        count = conn.execute("SELECT COUNT(*) FROM recipes WHERE colour_code_id=?", (code_id,)).fetchone()[0]
        if count > 0:
            conn.close()
            return False, "Cannot delete: there are recipes using this colour code."
        conn.execute("DELETE FROM colour_codes WHERE id=?", (code_id,))
        conn.commit()
        conn.close()
        add_log(username, "Delete Colour Code", f"Deleted colour code ID {code_id}")
        return True, None
    except Exception as e:
        return False, str(e)

@st.cache_data(ttl=300)
def get_recipes():
    conn = get_db_connection()
    query = """
        SELECT r.id, r.colour_code_id, cc.code as colour_code, r.colour_name,
               r.tsc_min, r.tsc_max, r.ph_min, r.ph_max,
               r.visc_min, r.visc_max, r.de_max,
               r.dl_tolerance, r.da_tolerance, r.db_tolerance,
               r.strength_min, r.strength_max
        FROM recipes r
        JOIN colour_codes cc ON r.colour_code_id = cc.id
        ORDER BY cc.code, r.colour_name
    """
    df = conn.execute(query).df()
    conn.close()
    return df

def get_recipe_by_id(recipe_id):
    conn = get_db_connection()
    df = conn.execute("SELECT * FROM recipes WHERE id=?", (recipe_id,)).df()
    conn.close()
    return df

def add_recipe(colour_code_id, colour_name, tsc_min, tsc_max, ph_min, ph_max,
               visc_min, visc_max, de_max, dl_tol, da_tol, db_tol, str_min, str_max, username):
    try:
        conn = get_db_connection()
        # DuckDB supports RETURNING id
        result = conn.execute("""INSERT INTO recipes (colour_code_id, colour_name, tsc_min, tsc_max, ph_min, ph_max,
                     visc_min, visc_max, de_max, dl_tolerance, da_tolerance, db_tolerance,
                     strength_min, strength_max)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id""",
                  (colour_code_id, colour_name, tsc_min, tsc_max, ph_min, ph_max,
                   visc_min, visc_max, de_max, dl_tol, da_tol, db_tol, str_min, str_max))
        recipe_id = result.fetchone()[0]
        conn.commit()
        conn.close()
        add_log(username, "Add Recipe", f"Added recipe {colour_name} (colour code ID {colour_code_id})", recipe_id=recipe_id)
        return True, recipe_id
    except Exception as e:
        return False, str(e)

def update_recipe(recipe_id, colour_name, tsc_min, tsc_max, ph_min, ph_max,
                  visc_min, visc_max, de_max, dl_tol, da_tol, db_tol,
                  str_min, str_max, username):
    try:
        conn = get_db_connection()
        conn.execute("""UPDATE recipes SET
                     colour_name=?, tsc_min=?, tsc_max=?, ph_min=?, ph_max=?,
                     visc_min=?, visc_max=?, de_max=?,
                     dl_tolerance=?, da_tolerance=?, db_tolerance=?,
                     strength_min=?, strength_max=?
                     WHERE id=?""",
                  (colour_name, tsc_min, tsc_max, ph_min, ph_max, visc_min, visc_max,
                   de_max, dl_tol, da_tol, db_tol, str_min, str_max, recipe_id))
        conn.commit()
        conn.close()
        add_log(username, "Update Recipe", f"Updated recipe ID {recipe_id}", recipe_id=recipe_id)
        return True, None
    except Exception as e:
        return False, str(e)

def delete_recipe(recipe_id, username):
    try:
        conn = get_db_connection()
        count = conn.execute("SELECT COUNT(*) FROM batches WHERE recipe_id=?", (recipe_id,)).fetchone()[0]
        if count > 0:
            conn.close()
            return False, "Cannot delete recipe: it is used by one or more batches."
        conn.execute("DELETE FROM recipes WHERE id=?", (recipe_id,))
        conn.commit()
        conn.close()
        add_log(username, "Delete Recipe", f"Deleted recipe ID {recipe_id}", recipe_id=recipe_id)
        return True, None
    except Exception as e:
        return False, str(e)

# ---------- BATCH FUNCTIONS (with caching) ----------
@st.cache_data(ttl=300)
def get_batches():
    conn = get_db_connection()
    df = conn.execute("SELECT * FROM batches ORDER BY created_at DESC").df()
    conn.close()
    return df

@st.cache_data(ttl=300)
def get_completed_batches():
    conn = get_db_connection()
    df = conn.execute("SELECT * FROM batches WHERE status = 'Completed' ORDER BY created_at DESC").df()
    conn.close()
    return df

def batch_exists(batch_number):
    conn = get_db_connection()
    res = conn.execute("SELECT 1 FROM batches WHERE batch_number=?", (batch_number,)).fetchone()
    exists = res is not None
    conn.close()
    return exists

def add_batch(batch_number, recipe_id, colour_code, manufacturing_date, username):
    conn = get_db_connection()
    batch_id = f"b_{batch_number}"
    conn.execute(
        "INSERT INTO batches (batch_id, batch_number, recipe_id, colour_code, status, stage, manufacturing_date) VALUES (?,?,?,?,?,?,?)",
        (batch_id, batch_number, recipe_id, colour_code, 'Issued', 'Mixing', manufacturing_date))
    conn.commit()
    conn.close()
    add_log(username, "Issue Batch", f"Issued batch {batch_number} for {colour_code}", batch_number=batch_number, recipe_id=recipe_id)
    return batch_number

def update_status(batch_id, status, stage, username):
    conn = get_db_connection()
    batch_number = conn.execute("SELECT batch_number FROM batches WHERE batch_id=?", (batch_id,)).fetchone()[0]
    conn.execute("UPDATE batches SET status=?, stage=? WHERE batch_id=?", (status, stage, batch_id))
    conn.commit()
    conn.close()
    add_log(username, "Update Status", f"Batch {batch_number} status changed to {status} (stage: {stage})", batch_number=batch_number)

def update_qa(batch_id, tsc, ph, visc, de, dl, da, db, colour_strength, remark, username):
    conn = get_db_connection()
    batch_number = conn.execute("SELECT batch_number FROM batches WHERE batch_id=?", (batch_id,)).fetchone()[0]
    row = conn.execute("""SELECT tsc_min, tsc_max, ph_min, ph_max, visc_min, visc_max,
                        de_max, dl_tolerance, da_tolerance, db_tolerance,
                        strength_min, strength_max
                 FROM recipes r JOIN batches b ON r.id = b.recipe_id
                 WHERE b.batch_id=?""", (batch_id,)).fetchone()
    if not row:
        conn.close()
        return "❌ Recipe not found!"
    tsc_min, tsc_max, ph_min, ph_max, visc_min, visc_max, de_max, dl_tol, da_tol, db_tol, str_min, str_max = row

    tsc_ok = tsc_min <= tsc <= tsc_max
    ph_ok = ph_min <= ph <= ph_max
    visc_ok = visc_min <= visc <= visc_max
    de_ok = de <= de_max
    dl_ok = abs(dl) <= dl_tol
    da_ok = abs(da) <= da_tol
    db_ok = abs(db) <= db_tol
    strength_ok = str_min <= colour_strength <= str_max
    passed = all([tsc_ok, ph_ok, visc_ok, de_ok, dl_ok, da_ok, db_ok, strength_ok])

    current_attempt = conn.execute("SELECT attempt_count FROM batches WHERE batch_id=?", (batch_id,)).fetchone()[0] or 0
    new_attempt = current_attempt + 1

    if passed:
        status, stage, msg = 'QA_Passed', 'Finished', '✅ QA PASSED!'
    else:
        status, stage, msg = 'QA_Failed', 'Milling', '❌ QA FAILED! Back to Milling.'

    conn.execute(
        """UPDATE batches SET tsc=?, ph=?, visc=?, de=?, dl=?, da=?, db=?,
           colour_strength=?, status=?, stage=?, attempt_count=?, remark=?
           WHERE batch_id=?""",
        (tsc, ph, visc, de, dl, da, db, colour_strength, status, stage, new_attempt, remark, batch_id))
    conn.commit()
    conn.close()
    add_log(username, "Submit QA", f"QA submitted for batch {batch_number}, result: {msg}", batch_number=batch_number)
    return msg

# ---------- USER MANAGEMENT (with caching) ----------
@st.cache_data(ttl=300)
def get_users():
    conn = get_db_connection()
    df = conn.execute("SELECT username, role FROM users ORDER BY username").df()
    conn.close()
    return df

def add_user(username, password, role):
    conn = get_db_connection()
    conn.execute("INSERT INTO users (username, password, role) VALUES (?,?,?)", (username, password, role))
    conn.commit()
    conn.close()

def update_user(username, password, role):
    conn = get_db_connection()
    conn.execute("UPDATE users SET password=?, role=? WHERE username=?", (password, role, username))
    conn.commit()
    conn.close()

def delete_user(username):
    conn = get_db_connection()
    conn.execute("DELETE FROM users WHERE username=?", (username,))
    conn.commit()
    conn.close()

def check_login(username, password):
    conn = get_db_connection()
    row = conn.execute("SELECT username, role FROM users WHERE username=? AND password=?", (username, password)).fetchone()
    conn.close()
    return row

@st.cache_data(ttl=300)
def get_logs():
    conn = get_db_connection()
    df = conn.execute("SELECT * FROM logs ORDER BY timestamp DESC").df()
    conn.close()
    return df

# ---------- BACKUP / RESTORE ----------
def export_db_to_zip():
    conn = get_db_connection()
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for table in ['colour_codes', 'recipes', 'batches', 'seq_counter', 'users', 'logs']:
            try:
                df = conn.execute(f"SELECT * FROM {table}").df()
                csv_data = df.to_csv(index=False).encode('utf-8')
                zipf.writestr(f"{table}.csv", csv_data)
            except:
                pass
    conn.close()
    zip_buffer.seek(0)
    return zip_buffer

def import_db_from_zip(zip_file):
    conn = get_db_connection()
    with zipfile.ZipFile(zip_file, 'r') as zipf:
        for table in ['colour_codes', 'recipes', 'batches', 'seq_counter', 'users', 'logs']:
            if f"{table}.csv" in zipf.namelist():
                df = pd.read_csv(zipf.open(f"{table}.csv"))
                conn.execute(f"DELETE FROM {table}")
                # Use DuckDB's insert from dataframe
                conn.register('df', df)
                conn.execute(f"INSERT INTO {table} SELECT * FROM df")
    conn.commit()
    conn.close()

# ---------- COA GENERATION ----------
def generate_coa_pdf(batch_number, template, edited_results=None):
    try:
        all_batches = get_batches()
        batch_df = all_batches[all_batches['batch_number'] == batch_number]
        if batch_df.empty:
            return None
        batch = batch_df.iloc[0]

        recipe_df = get_recipe_by_id(batch['recipe_id'])
        if recipe_df.empty:
            return None
        recipe = recipe_df.iloc[0]

        mfg_val = batch['manufacturing_date']
        if pd.isna(mfg_val) or mfg_val is None:
            mfg_date = datetime.now()
        else:
            try:
                if isinstance(mfg_val, (int, float)):
                    mfg_date = datetime.fromtimestamp(mfg_val)
                else:
                    mfg_date = pd.to_datetime(mfg_val)
            except:
                mfg_date = datetime.now()
        if hasattr(mfg_date, 'to_pydatetime'):
            mfg_date = mfg_date.to_pydatetime()

        expiry_date = mfg_date + pd.DateOffset(months=18)
        mfg_str = mfg_date.strftime("%d.%m.%Y")
        expiry_str = expiry_date.strftime("%d.%m.%Y")

        default_results = {
            "pH": batch['ph'],
            "TSC": batch['tsc'],
            "Viscosity": "Paste",
            "DL": batch['dl'],
            "Da": batch['da'],
            "Db": batch['db'],
            "DE": batch['de'],
            "Colour Strength": batch['colour_strength']
        }

        if edited_results is not None:
            edited_dict = {row['PARAMETER']: row['RESULT'] for _, row in edited_results.iterrows()}
            numeric_params = ["pH", "TSC", "DL", "Da", "Db", "DE", "Colour Strength"]
            for param in numeric_params:
                if param in edited_dict and edited_dict[param] != "":
                    try:
                        default_results[param] = float(edited_dict[param])
                    except ValueError:
                        pass
            if "Viscosity" in edited_dict:
                default_results["Viscosity"] = edited_dict["Viscosity"]

        results = [
            ("pH", f"{recipe['ph_min']:.2f} - {recipe['ph_max']:.2f}", f"{default_results['pH']:.2f}"),
            ("TSC", f"{recipe['tsc_min']:.0f}-{recipe['tsc_max']:.0f}%", f"{default_results['TSC']:.2f}%"),
            ("Viscosity", "Paste", default_results["Viscosity"]),
            ("DL", f"± {recipe['dl_tolerance']:.1f}", f"{default_results['DL']:.2f}"),
            ("Da", f"± {recipe['da_tolerance']:.1f}", f"{default_results['Da']:.2f}"),
            ("Db", f"± {recipe['db_tolerance']:.1f}", f"{default_results['Db']:.2f}"),
            ("DE", f"≤ {recipe['de_max']:.1f}", f"{default_results['DE']:.2f}"),
            ("Colour Strength", f"{recipe['strength_min']:.0f}-{recipe['strength_max']:.0f}%", f"{default_results['Colour Strength']:.2f}%")
        ]

        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=30, leftMargin=30, topMargin=30, bottomMargin=30)
        styles = getSampleStyleSheet()
        story = []

        header_bold_style = ParagraphStyle('HeaderBold', parent=styles['Normal'], fontSize=9, leading=11, alignment=0)
        header_normal_style = ParagraphStyle('HeaderNormal', parent=styles['Normal'], fontSize=9, leading=11, alignment=0)

        company_name = template.get('company_name', "TIARCO CHEMICAL (MALAYSIA) SDN. BHD.")
        reg_no = template.get('reg_no', "199101012802 (223114-K)")
        address_lines = template.get('address_lines', [
            "LOT 47962, PERSIARAN TASEK,",
            "KAWASAN PERINDUSTRIAN TASEK,",
            "31400 IPOH, PERAK, MALAYSIA."
        ])
        phone_fax = template.get('phone_fax', "TEL: 605-5412018            FAX : 605-5412716")

        story.append(Paragraph(f"<b>{company_name}</b>", header_bold_style))
        story.append(Paragraph(reg_no, header_normal_style))
        for line in address_lines:
            story.append(Paragraph(line, header_normal_style))
        story.append(Paragraph(phone_fax, header_normal_style))
        story.append(Spacer(1, 10))

        title_text = template.get('title', "PROVISIONAL CERTIFICATE OF ANALYSIS")
        title_style = ParagraphStyle('Title', parent=styles['Title'], fontSize=14, alignment=1, spaceAfter=10)
        story.append(Paragraph(title_text, title_style))

        top_data = [
            ["Product:", recipe['colour_name']],
            ["Batch no.:", batch['batch_number']],
            ["Manufacturing date:", mfg_str],
            ["Expiry date:", expiry_str]
        ]
        top_table = Table(top_data, colWidths=[doc.width * 0.30, doc.width * 0.70])
        top_table.setStyle(TableStyle([
            ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
            ('FONTSIZE', (0, 0), (-1, -1), 9),
            ('ALIGN', (0, 0), (0, -1), 'RIGHT'),
            ('ALIGN', (1, 0), (1, -1), 'LEFT'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
            ('BACKGROUND', (0, 0), (0, -1), colors.lightgrey),
            ('BACKGROUND', (1, 0), (1, -1), colors.white),
            ('LEFTPADDING', (0, 0), (-1, -1), 4),
            ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ]))
        story.append(top_table)
        story.append(Spacer(1, 10))

        data = [["PARAMETER", "SPECIFICATION", "RESULT"]]
        for param, spec, result in results:
            data.append([param, spec, result])

        main_table = Table(data, colWidths=[doc.width * 0.33, doc.width * 0.34, doc.width * 0.33])
        main_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 9),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 6),
            ('TOPPADDING', (0, 0), (-1, 0), 6),
            ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
            ('GRID', (0, 0), (-1, -1), 1, colors.black),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('LEFTPADDING', (0, 0), (-1, -1), 4),
            ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ]))
        story.append(main_table)
        story.append(Spacer(1, 15))

        bottom_data = [
            ["Date:", mfg_str],
            ["Prepared by:", template.get('prepared_by', 'MOK')],
            ["Reviewed & approved by:", template.get('reviewed_by', 'H.JY')]
        ]
        bottom_table = Table(bottom_data, colWidths=[doc.width * 0.30, doc.width * 0.70])
        bottom_table.setStyle(TableStyle([
            ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
            ('FONTSIZE', (0, 0), (-1, -1), 9),
            ('ALIGN', (0, 0), (0, -1), 'RIGHT'),
            ('ALIGN', (1, 0), (1, -1), 'LEFT'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
            ('BACKGROUND', (0, 0), (0, -1), colors.lightgrey),
            ('BACKGROUND', (1, 0), (1, -1), colors.white),
            ('LEFTPADDING', (0, 0), (-1, -1), 4),
            ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ]))
        story.append(bottom_table)

        doc.build(story)
        buffer.seek(0)
        return buffer
    except Exception as e:
        st.error(f"Error generating COA: {str(e)}")
        return None

# ---------- STREAMLIT APP ----------
st.set_page_config(page_title="Pigment Monitor", layout="wide")

# ---- Login ----
def login():
    if 'logged_in' not in st.session_state:
        st.session_state.logged_in = False
        st.session_state.username = None
        st.session_state.role = None

    if not st.session_state.logged_in:
        st.title("🔐 Pigment Dispersion System - Login")
        with st.form("login_form"):
            username = st.text_input("Username")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Login")
            if submitted:
                user = check_login(username, password)
                if user:
                    st.session_state.logged_in = True
                    st.session_state.username = user[0]
                    st.session_state.role = user[1]
                    st.rerun()
                else:
                    st.error("Invalid username or password")
        st.stop()
    else:
        st.sidebar.success(f"Logged in as: **{st.session_state.username}** (Role: {st.session_state.role})")
        if st.sidebar.button("Logout"):
            st.session_state.logged_in = False
            st.session_state.username = None
            st.session_state.role = None
            st.rerun()

login()

# ---- Sidebar debug ----
st.sidebar.write("---")
st.sidebar.write("**Database Status**")
st.sidebar.write(f"Path: `{DB_PATH}`")
st.sidebar.write(f"Exists: {os.path.exists(DB_PATH)}")
st.sidebar.write(f"Init: {init_msg}")

# ---- Role helpers ----
def is_admin():
    return st.session_state.role == "Admin"
def is_production():
    return st.session_state.role == "Production"
def is_qa():
    return st.session_state.role == "QA"

st.title("🎨 Pigment Dispersion System")

# Build tabs
tabs_list = []
if is_admin():
    tabs_list = ["Define Recipe", "Issue Batch", "QA Testing", "WIP Progress", "📊 Reports", "👥 User Management", "📜 Activity Log"]
elif is_production():
    tabs_list = ["Issue Batch", "WIP Progress", "📊 Reports"]
elif is_qa():
    tabs_list = ["QA Testing", "WIP Progress", "📊 Reports"]
tabs = st.tabs(tabs_list)

# ---------- TAB 1: DEFINE RECIPE ----------
if is_admin():
    with tabs[0]:
        st.header("📄 1. Define Recipe (Control Limits)")

        # Clear edit states function
        def clear_all_edit_states():
            for key in list(st.session_state.keys()):
                if key.startswith('edit_cc_') or key.startswith('edit_recipe_'):
                    del st.session_state[key]

        col_reset, col_db = st.columns([1, 4])
        with col_reset:
            if st.button("🔄 Reset Edit States"):
                clear_all_edit_states()
                st.rerun()
        with col_db:
            if st.button("🗑️ Reset Database (All Data Lost!)", type="primary"):
                conn = get_db_connection()
                conn.execute("DROP TABLE IF EXISTS colour_codes")
                conn.execute("DROP TABLE IF EXISTS recipes")
                conn.execute("DROP TABLE IF EXISTS batches")
                conn.execute("DROP TABLE IF EXISTS seq_counter")
                conn.execute("DROP TABLE IF EXISTS users")
                conn.execute("DROP TABLE IF EXISTS logs")
                conn.commit()
                conn.close()
                init_msg = init_db()
                st.success("Database reset to default!")
                st.rerun()

        st.subheader("🎨 Colour Codes & Recipes")

        # ADD COLOUR CODE
        with st.expander("➕ Add New Colour Code", expanded=False):
            with st.form("add_colour_code_form"):
                new_code = st.text_input("Colour Code (e.g., RED)", max_chars=20)
                new_desc = st.text_input("Description (optional)")
                submitted = st.form_submit_button("Add Colour Code")
                if submitted:
                    if not new_code:
                        st.error("❌ Code cannot be empty.")
                    else:
                        ok, err = add_colour_code(new_code.upper(), new_desc, st.session_state.username)
                        if ok:
                            st.success(f"✅ Colour code {new_code.upper()} added!")
                            st.rerun()
                        else:
                            st.error(f"❌ Failed: {err}")

        # DISPLAY TREE
        colour_codes_df = get_colour_codes()
        recipes_df = get_recipes()

        if colour_codes_df.empty:
            st.info("No colour codes defined. Add one above.")
        else:
            for _, cc_row in colour_codes_df.iterrows():
                cc_id = cc_row['id']
                cc_code = cc_row['code']
                cc_desc = cc_row['description'] or ""

                cc_recipes = recipes_df[recipes_df['colour_code_id'] == cc_id]

                with st.expander(f"🎨 {cc_code}  –  {cc_desc}  ({len(cc_recipes)} recipe(s))", expanded=False):
                    # Colour code actions
                    col1, col2, col3 = st.columns([2, 1, 1])
                    with col1:
                        st.write(f"**ID:** {cc_id}")
                    with col2:
                        if st.button(f"✏️ Edit Code", key=f"edit_cc_{cc_id}"):
                            if st.session_state.get(f'edit_cc_{cc_id}', False):
                                st.session_state.pop(f'edit_cc_{cc_id}', None)
                            else:
                                st.session_state[f'edit_cc_{cc_id}'] = True
                            st.rerun()
                    with col3:
                        if st.button(f"🗑️ Delete Code", key=f"del_cc_{cc_id}"):
                            ok, err = delete_colour_code(cc_id, st.session_state.username)
                            if ok:
                                st.success("✅ Colour code deleted!")
                                st.rerun()
                            else:
                                st.error(f"❌ {err}")

                    # Edit colour code
                    if st.session_state.get(f'edit_cc_{cc_id}', False):
                        with st.form(key=f"edit_cc_form_{cc_id}"):
                            new_code_val = st.text_input("Code", value=cc_code)
                            new_desc_val = st.text_input("Description", value=cc_desc)
                            c1, c2 = st.columns(2)
                            with c1:
                                if st.form_submit_button("✅ Update Code"):
                                    ok, err = update_colour_code(cc_id, new_code_val.upper(), new_desc_val, st.session_state.username)
                                    if ok:
                                        st.success("✅ Colour code updated!")
                                        st.session_state.pop(f'edit_cc_{cc_id}', None)
                                        st.rerun()
                                    else:
                                        st.error(f"❌ {err}")
                            with c2:
                                if st.form_submit_button("❌ Cancel"):
                                    st.session_state.pop(f'edit_cc_{cc_id}', None)
                                    st.rerun()

                    # Add recipe
                    with st.expander(f"➕ Add Recipe under {cc_code}", expanded=False):
                        with st.form(key=f"add_recipe_form_{cc_id}"):
                            recipe_name = st.text_input("Recipe Name (Colour Name)")
                            st.caption("Control limits:")
                            col1, col2 = st.columns(2)
                            with col1:
                                tsc_min = st.number_input("TSC Min (%)", value=43.0, step=0.1)
                                ph_min = st.number_input("pH Min", value=8.0, step=0.1)
                                visc_min = st.number_input("Viscosity Min (cP)", value=1100.0, step=10.0)
                                de_max = st.number_input("DE Max (≤ value)", value=1.0, step=0.01)
                                dl_tol = st.number_input("DL Tolerance (±)", value=0.5, step=0.1)
                            with col2:
                                tsc_max = st.number_input("TSC Max (%)", value=47.0, step=0.1)
                                ph_max = st.number_input("pH Max", value=9.0, step=0.1)
                                visc_max = st.number_input("Viscosity Max (cP)", value=1300.0, step=10.0)
                                da_tol = st.number_input("Da Tolerance (±)", value=0.6, step=0.1)
                                db_tol = st.number_input("Db Tolerance (±)", value=0.6, step=0.1)
                                str_min = st.number_input("Strength Min %", value=95.0, step=1.0)
                                str_max = st.number_input("Strength Max %", value=105.0, step=1.0)

                            if st.form_submit_button("💾 Save Recipe"):
                                if not recipe_name:
                                    st.error("❌ Recipe Name is required.")
                                else:
                                    ok, result = add_recipe(
                                        cc_id, recipe_name,
                                        tsc_min, tsc_max, ph_min, ph_max,
                                        visc_min, visc_max, de_max,
                                        dl_tol, da_tol, db_tol,
                                        str_min, str_max,
                                        st.session_state.username
                                    )
                                    if ok:
                                        st.success(f"✅ Recipe '{recipe_name}' saved! ID={result}")
                                        st.rerun()
                                    else:
                                        st.error(f"❌ Failed: {result}")

                    # List existing recipes
                    if cc_recipes.empty:
                        st.info("No recipes yet.")
                    else:
                        for _, recipe in cc_recipes.iterrows():
                            recipe_id = recipe['id']
                            with st.container():
                                col1, col2, col3, col4 = st.columns([2, 2, 1, 1])
                                with col1:
                                    st.write(f"**{recipe['colour_name']}**")
                                with col2:
                                    st.caption(f"TSC: {recipe['tsc_min']:.1f}-{recipe['tsc_max']:.1f}%  |  pH: {recipe['ph_min']:.1f}-{recipe['ph_max']:.1f}")
                                    st.caption(f"Visc: {recipe['visc_min']:.0f}-{recipe['visc_max']:.0f} cP  |  DE ≤ {recipe['de_max']:.2f}")
                                with col3:
                                    if st.button(f"✏️ Edit", key=f"edit_recipe_{recipe_id}"):
                                        if st.session_state.get(f'edit_recipe_{recipe_id}', False):
                                            st.session_state.pop(f'edit_recipe_{recipe_id}', None)
                                        else:
                                            st.session_state[f'edit_recipe_{recipe_id}'] = True
                                        st.rerun()
                                with col4:
                                    if st.button(f"🗑️ Delete", key=f"del_recipe_{recipe_id}"):
                                        ok, err = delete_recipe(recipe_id, st.session_state.username)
                                        if ok:
                                            st.success(f"✅ Recipe '{recipe['colour_name']}' deleted!")
                                            st.rerun()
                                        else:
                                            st.error(f"❌ {err}")

                                # Edit recipe
                                if st.session_state.get(f'edit_recipe_{recipe_id}', False):
                                    with st.form(key=f"edit_recipe_form_{recipe_id}"):
                                        edit_name = st.text_input("Colour Name", value=recipe['colour_name'])
                                        col1, col2 = st.columns(2)
                                        with col1:
                                            e_tsc_min = st.number_input("TSC Min", value=float(recipe['tsc_min']), step=0.1)
                                            e_ph_min = st.number_input("pH Min", value=float(recipe['ph_min']), step=0.1)
                                            e_visc_min = st.number_input("Viscosity Min", value=float(recipe['visc_min']), step=10.0)
                                            e_de_max = st.number_input("DE Max", value=float(recipe['de_max']), step=0.01)
                                            e_dl_tol = st.number_input("DL Tolerance", value=float(recipe['dl_tolerance']), step=0.1)
                                        with col2:
                                            e_tsc_max = st.number_input("TSC Max", value=float(recipe['tsc_max']), step=0.1)
                                            e_ph_max = st.number_input("pH Max", value=float(recipe['ph_max']), step=0.1)
                                            e_visc_max = st.number_input("Viscosity Max", value=float(recipe['visc_max']), step=10.0)
                                            e_da_tol = st.number_input("Da Tolerance", value=float(recipe['da_tolerance']), step=0.1)
                                            e_db_tol = st.number_input("Db Tolerance", value=float(recipe['db_tolerance']), step=0.1)
                                            e_str_min = st.number_input("Strength Min", value=float(recipe['strength_min']), step=1.0)
                                            e_str_max = st.number_input("Strength Max", value=float(recipe['strength_max']), step=1.0)

                                        c1, c2 = st.columns(2)
                                        with c1:
                                            if st.form_submit_button("✅ Update Recipe"):
                                                ok, err = update_recipe(
                                                    recipe_id, edit_name,
                                                    e_tsc_min, e_tsc_max,
                                                    e_ph_min, e_ph_max,
                                                    e_visc_min, e_visc_max,
                                                    e_de_max, e_dl_tol, e_da_tol, e_db_tol,
                                                    e_str_min, e_str_max,
                                                    st.session_state.username
                                                )
                                                if ok:
                                                    st.success("✅ Recipe updated!")
                                                    st.session_state.pop(f'edit_recipe_{recipe_id}', None)
                                                    st.rerun()
                                                else:
                                                    st.error(f"❌ {err}")
                                        with c2:
                                            if st.form_submit_button("❌ Cancel"):
                                                st.session_state.pop(f'edit_recipe_{recipe_id}', None)
                                                st.rerun()

        # Data preview
        st.divider()
        st.subheader("📋 All Recipes (Preview)")
        preview = get_recipes()
        if preview.empty:
            st.info("No recipes defined yet.")
        else:
            st.dataframe(preview, use_container_width=True)

        # Backup / Restore
        st.divider()
        st.subheader("💾 Backup / Restore Database")
        col1, col2 = st.columns(2)
        with col1:
            if st.button("📥 Export Database (ZIP)", use_container_width=True):
                zip_data = export_db_to_zip()
                st.download_button(
                    label="⬇ Download pigment_db_backup.zip",
                    data=zip_data,
                    file_name="pigment_db_backup.zip",
                    mime="application/zip"
                )
                st.success("✅ Database exported successfully!")
        with col2:
            uploaded_zip = st.file_uploader("📤 Upload backup ZIP to restore", type=["zip"])
            if uploaded_zip is not None:
                if st.button("⚠️ Restore Database (overwrites current data)", type="primary"):
                    try:
                        import_db_from_zip(uploaded_zip)
                        st.toast("✅ Database restored! Refreshing...", icon="🔄")
                        st.rerun()
                    except Exception as e:
                        st.error(f"❌ Restore failed: {str(e)}")

# ---------- TAB 2: ISSUE BATCH ----------
if is_admin() or is_production():
    tab_idx = 1 if is_admin() else 0
    with tabs[tab_idx]:
        st.header("📄 2. Issue New Batch")
        recipes = get_recipes()
        if recipes.empty:
            st.warning("No recipes. Please ask Admin to add a recipe first.")
        else:
            # QR Scanner
            st.subheader("📷 Scan QR with Camera")
            if not QR_AVAILABLE:
                st.warning("⚠️ QR scanning library not installed. You can use manual text input.")
            else:
                camera_image = st.camera_input("Point camera at QR code")
                if camera_image is not None:
                    try:
                        img = Image.open(camera_image)
                        decoded = decode_qr_from_image(img)
                        if decoded:
                            st.success(f"✅ Decoded: {decoded}")
                            parts = decoded.split('_', 1)
                            qr_name_part = parts[0] if len(parts) >= 1 else decoded
                            qr_batch = parts[1] if len(parts) == 2 else ''
                            match = recipes[recipes['colour_name'].str.lower() == qr_name_part.lower()]
                            if not match.empty:
                                recipe_id = match.iloc[0]['id']
                                colour_code = match.iloc[0]['colour_code']
                                st.session_state['qr_recipe_id'] = recipe_id
                                st.session_state['qr_batch'] = qr_batch
                                st.session_state['colour_filter'] = colour_code
                                st.success(f"✅ Recipe found: {match.iloc[0]['colour_name']} (Code: {colour_code})")
                                st.rerun()
                            else:
                                st.error(f"❌ No recipe found with colour name '{qr_name_part}'.")
                        else:
                            st.error("❌ No QR code detected.")
                    except Exception as e:
                        st.error(f"❌ Error processing image: {e}")

            # Manual QR
            st.subheader("📝 Or paste QR text")
            qr_input = st.text_input("Paste QR code content (format: <colour name>_<batch number>)")
            if qr_input:
                parts = qr_input.split('_', 1)
                qr_name_part = parts[0] if len(parts) >= 1 else qr_input
                qr_batch = parts[1] if len(parts) == 2 else ''
                match = recipes[recipes['colour_name'].str.lower() == qr_name_part.lower()]
                if not match.empty:
                    recipe_id = match.iloc[0]['id']
                    colour_code = match.iloc[0]['colour_code']
                    st.session_state['qr_recipe_id'] = recipe_id
                    st.session_state['qr_batch'] = qr_batch
                    st.session_state['colour_filter'] = colour_code
                    st.success(f"✅ Recipe found: {match.iloc[0]['colour_name']} (Code: {colour_code})")
                    st.rerun()
                else:
                    st.error(f"❌ No recipe found with colour name '{qr_name_part}'.")

            # Recipe selection
            unique_colours = recipes['colour_code'].unique().tolist()
            filter_options = ["All"] + sorted(unique_colours)
            colour_filter = st.selectbox("Filter by Colour Code", filter_options)
            if colour_filter != "All":
                filtered_recipes = recipes[recipes['colour_code'] == colour_filter]
            else:
                filtered_recipes = recipes
            if filtered_recipes.empty:
                st.warning(f"No recipes for {colour_filter}")
            else:
                recipe_options = {f"{row['colour_code']} - {row['colour_name']}": row['id']
                                  for _, row in filtered_recipes.iterrows()}
                selected = st.selectbox("Select Recipe", list(recipe_options.keys()))
                recipe_id = recipe_options[selected]
                colour_code = selected.split(" - ")[0]

                default_batch = st.session_state.get('qr_batch', '')
                batch_number = st.text_input("Batch Number", value=default_batch)
                manufacturing_date = st.date_input("Manufacturing Date", datetime.now())
                manufacturing_date_str = manufacturing_date.strftime("%Y-%m-%d")

                if st.button("▶ Issue Batch", type="primary"):
                    if not batch_number:
                        st.error("❌ Please enter a Batch Number.")
                    elif batch_exists(batch_number):
                        st.error(f"❌ Batch Number '{batch_number}' already exists.")
                    else:
                        add_batch(batch_number, recipe_id, colour_code, manufacturing_date_str, st.session_state.username)
                        st.toast(f"✅ Batch {batch_number} issued!", icon="✅")
                        for key in ['qr_recipe_id', 'qr_batch', 'colour_filter']:
                            st.session_state.pop(key, None)
                        st.rerun()

# ---------- TAB 3: QA TESTING ----------
if is_admin() or is_qa():
    tab_idx = 2 if is_admin() else 0
    with tabs[tab_idx]:
        st.header("🔬 3. QA Testing")
        df_batches = get_batches()
        pending = df_batches[df_batches['status'] == 'QA_Pending']
        if not pending.empty:
            batch_options = {f"{row['batch_number']} ({row['colour_code']})": row['batch_id']
                             for _, row in pending.iterrows()}
            selected = st.selectbox("Select Batch", list(batch_options.keys()))
            batch_id = batch_options[selected]

            st.markdown("**Enter Measured Values**")
            col1, col2 = st.columns(2)
            with col1:
                tsc = st.number_input("TSC (%)", value=45.0, step=0.1)
                ph = st.number_input("pH", value=8.5, step=0.1)
                dl = st.number_input("DL", value=0.0, step=0.01)
                da = st.number_input("Da", value=0.0, step=0.01)
            with col2:
                visc = st.number_input("Viscosity (cP)", value=1200.0, step=10.0)
                de = st.number_input("DE", value=0.5, step=0.01)
                db = st.number_input("Db", value=0.0, step=0.01)
                colour_strength = st.number_input("Colour Strength (%)", value=100.0, step=0.1)

            remark = st.text_area("Remark", placeholder="Add comments...")
            if st.button("Submit QA", type="primary"):
                if not remark:
                    st.warning("⚠️ Please add a remark.")
                else:
                    msg = update_qa(batch_id, tsc, ph, visc, de, dl, da, db, colour_strength, remark, st.session_state.username)
                    st.toast(msg, icon="🔬")
                    st.rerun()
        else:
            st.info("No batches waiting for QA.")

# ---------- TAB 4: WIP PROGRESS ----------
wip_index = next(i for i, name in enumerate(tabs_list) if name == "WIP Progress")
with tabs[wip_index]:
    st.header("📋 4. Live WIP Progress")
    df_all = get_batches()
    active = df_all[df_all['status'] != 'Completed']
    # Limit to 20 active rows for speed
    active = active.head(20)
    if active.empty:
        st.info("No active batches.")
    else:
        display_cols = ['batch_number', 'colour_code', 'stage', 'status', 'attempt_count',
                        'manufacturing_date', 'tsc', 'ph', 'visc', 'de', 'dl', 'da', 'db',
                        'colour_strength', 'remark']
        st.dataframe(active[display_cols], use_container_width=True)

        st.subheader("⚡ Actions")
        for _, row in active.iterrows():
            col1, col2, col3, col4, col5 = st.columns([1, 1, 1, 2, 2])
            with col1:
                st.write(f"**{row['batch_number']}**")
            with col2:
                st.write(row['stage'])
            with col3:
                st.write(row['status'])
            with col4:
                st.write(f"Attempt: {row['attempt_count'] or 0}")
            with col5:
                batch_id = row['batch_id']
                if is_admin() or is_production():
                    if row['status'] == 'Issued':
                        if st.button(f"▶ Mix", key=f"mix_{batch_id}"):
                            update_status(batch_id, 'Mixing', 'Mixing', st.session_state.username)
                            st.rerun()
                    elif row['status'] == 'Mixing':
                        if st.button(f"⚙ Mill", key=f"mill_{batch_id}"):
                            update_status(batch_id, 'Milling', 'Milling', st.session_state.username)
                            st.rerun()
                    elif row['status'] == 'Milling':
                        if st.button(f"🔬 Submit to QA", key=f"qa_{batch_id}"):
                            update_status(batch_id, 'QA_Pending', 'QA', st.session_state.username)
                            st.rerun()
                    elif row['status'] == 'QA_Failed':
                        if st.button(f"🔄 Retry", key=f"retry_{batch_id}"):
                            update_status(batch_id, 'Milling', 'Milling', st.session_state.username)
                            st.rerun()
                    elif row['status'] == 'QA_Passed':
                        if st.button(f"✅ Complete", key=f"comp_{batch_id}"):
                            update_status(batch_id, 'Completed', 'Finished', st.session_state.username)
                            st.rerun()
                    else:
                        st.write("⏳")

# ---------- TAB 5: REPORTS ----------
report_index = next(i for i, name in enumerate(tabs_list) if name == "📊 Reports")
with tabs[report_index]:
    st.header("📊 Reports & Analytics")
    report_tabs = st.tabs(["📈 SPC Charts", "📄 COA Generation", "📥 Data Export"])

    with report_tabs[0]:
        st.subheader("📈 Statistical Process Control (SPC) Charts")
        completed_df = get_completed_batches()
        if completed_df.empty:
            st.info("No completed batches available.")
        else:
            colours = completed_df['colour_code'].unique().tolist()
            selected_colour = st.selectbox("Select Colour Code", sorted(colours))
            filtered_df = completed_df[completed_df['colour_code'] == selected_colour]
            # Keep only 50 most recent for speed
            filtered_df = filtered_df.tail(50)
            if filtered_df.empty:
                st.warning(f"No completed batches for {selected_colour}")
            else:
                recipe_df = get_recipes()
                recipe = recipe_df[recipe_df['colour_code'] == selected_colour]
                if not recipe.empty:
                    r = recipe.iloc[0]
                    specs = {
                        'tsc': (r['tsc_min'], r['tsc_max']),
                        'ph': (r['ph_min'], r['ph_max']),
                        'visc': (r['visc_min'], r['visc_max']),
                        'de': (0, r['de_max']),
                        'dl': (-r['dl_tolerance'], r['dl_tolerance']),
                        'da': (-r['da_tolerance'], r['da_tolerance']),
                        'db': (-r['db_tolerance'], r['db_tolerance']),
                        'colour_strength': (r['strength_min'], r['strength_max'])
                    }
                else:
                    specs = None

                params = ['tsc', 'ph', 'visc', 'de', 'dl', 'da', 'db', 'colour_strength']
                param_labels = ['TSC (%)', 'pH', 'Viscosity (cP)', 'DE', 'DL', 'Da', 'Db', 'Colour Strength (%)']
                filtered_df = filtered_df.sort_values('created_at')
                x_vals = filtered_df['batch_number'].tolist()

                fig = make_subplots(rows=4, cols=2, subplot_titles=param_labels)
                row_idx, col_idx = 1, 1
                for i, param in enumerate(params):
                    y_vals = filtered_df[param].tolist()
                    fig.add_trace(go.Scatter(x=x_vals, y=y_vals, mode='lines+markers',
                                             name=param_labels[i], line=dict(color='blue'),
                                             marker=dict(size=6)),
                                  row=row_idx, col=col_idx)
                    if specs:
                        lower, upper = specs[param]
                        fig.add_hline(y=upper, line_dash="dash", line_color="red", row=row_idx, col=col_idx)
                        fig.add_hline(y=lower, line_dash="dash", line_color="red", row=row_idx, col=col_idx)
                    if col_idx == 2:
                        row_idx += 1
                        col_idx = 1
                    else:
                        col_idx += 1

                fig.update_layout(height=800, showlegend=False, title_text=f"SPC Chart: {selected_colour}")
                fig.update_xaxes(tickangle=45)
                st.plotly_chart(fig, use_container_width=True)

    # ---------- COA GENERATION ----------
    with report_tabs[1]:
        st.subheader("📄 Certificate of Analysis")
        completed_list = get_completed_batches()
        if completed_list.empty:
            st.info("No completed batches available. Complete a batch first.")
        else:
            with st.expander("✏️ Customize COA Template", expanded=False):
                col1, col2 = st.columns(2)
                with col1:
                    company_name = st.text_input("Company", value="TIARCO CHEMICAL (MALAYSIA) SDN. BHD.", key="coa_company")
                    reg_no = st.text_input("Reg No.", value="199101012802 (223114-K)", key="coa_reg")
                    addr1 = st.text_input("Address 1", value="LOT 47962, PERSIARAN TASEK,", key="coa_addr1")
                    addr2 = st.text_input("Address 2", value="KAWASAN PERINDUSTRIAN TASEK,", key="coa_addr2")
                    addr3 = st.text_input("Address 3", value="31400 IPOH, PERAK, MALAYSIA.", key="coa_addr3")
                with col2:
                    phone = st.text_input("Phone/Fax", value="TEL: 605-5412018            FAX : 605-5412716", key="coa_phone")
                    title = st.text_input("Title", value="PROVISIONAL CERTIFICATE OF ANALYSIS", key="coa_title")
                    prep_by = st.text_input("Prepared by", value="MOK", key="coa_prepared")
                    rev_by = st.text_input("Reviewed by", value="H.JY", key="coa_reviewed")

            template = {
                'company_name': company_name,
                'reg_no': reg_no,
                'address_lines': [addr1, addr2, addr3],
                'phone_fax': phone,
                'title': title,
                'prepared_by': prep_by,
                'reviewed_by': rev_by
            }

            batch_options = {f"{row['batch_number']} ({row['colour_code']})": row['batch_number']
                             for _, row in completed_list.iterrows()}
            selected_batch_display = st.selectbox("Select Batch", list(batch_options.keys()))
            batch_num = batch_options[selected_batch_display]

            if st.button("🔄 Load Batch Data"):
                st.rerun()

            all_batches = get_batches()
            batch_df = all_batches[all_batches['batch_number'] == batch_num]

            if batch_df.empty:
                st.error(f"❌ Batch '{batch_num}' not found.")
            else:
                batch = batch_df.iloc[0]
                recipe_id = batch['recipe_id']
                recipe_df = get_recipe_by_id(recipe_id)

                if recipe_df.empty:
                    st.warning(f"⚠️ Recipe ID {recipe_id} for batch '{batch_num}' is missing.")
                    all_recipes = get_recipes()
                    matching = all_recipes[all_recipes['colour_code'] == batch['colour_code']]
                    if not matching.empty:
                        new_recipe_id = matching.iloc[0]['id']
                        conn = get_db_connection()
                        conn.execute("UPDATE batches SET recipe_id = ? WHERE batch_number = ?", (new_recipe_id, batch_num))
                        conn.commit()
                        conn.close()
                        st.success(f"✅ Auto‑assigned recipe '{matching.iloc[0]['colour_name']}' to batch.")
                        st.rerun()
                    else:
                        st.info("No recipe with the same colour code. Please assign manually below.")
                        all_recipes = get_recipes()
                        if not all_recipes.empty:
                            recipe_options = {f"{row['colour_code']} - {row['colour_name']}": row['id']
                                              for _, row in all_recipes.iterrows()}
                            selected_recipe_display = st.selectbox("Assign a recipe", list(recipe_options.keys()))
                            new_recipe_id = recipe_options[selected_recipe_display]
                            if st.button("🔄 Update Batch Recipe"):
                                conn = get_db_connection()
                                conn.execute("UPDATE batches SET recipe_id = ? WHERE batch_number = ?", (new_recipe_id, batch_num))
                                conn.commit()
                                conn.close()
                                st.success(f"✅ Batch updated with recipe '{selected_recipe_display}'!")
                                st.rerun()
                        else:
                            st.warning("No recipes available. Please define a recipe first.")
                else:
                    recipe = recipe_df.iloc[0]
                    # COA generation...
                    mfg_val = batch['manufacturing_date']
                    if pd.isna(mfg_val) or mfg_val is None:
                        mfg_date = datetime.now()
                    else:
                        try:
                            if isinstance(mfg_val, (int, float)):
                                mfg_date = datetime.fromtimestamp(mfg_val)
                            else:
                                mfg_date = pd.to_datetime(mfg_val)
                        except:
                            mfg_date = datetime.now()
                    if hasattr(mfg_date, 'to_pydatetime'):
                        mfg_date = mfg_date.to_pydatetime()
                    expiry_date = mfg_date + pd.DateOffset(months=18)
                    mfg_str = mfg_date.strftime("%d.%m.%Y")
                    expiry_str = expiry_date.strftime("%d.%m.%Y")

                    results_data = pd.DataFrame({
                        "PARAMETER": ["pH", "TSC", "Viscosity", "DL", "Da", "Db", "DE", "Colour Strength"],
                        "SPECIFICATION": [
                            f"{recipe['ph_min']:.2f} - {recipe['ph_max']:.2f}",
                            f"{recipe['tsc_min']:.0f}-{recipe['tsc_max']:.0f}%",
                            "Paste",
                            f"± {recipe['dl_tolerance']:.1f}",
                            f"± {recipe['da_tolerance']:.1f}",
                            f"± {recipe['db_tolerance']:.1f}",
                            f"≤ {recipe['de_max']:.1f}",
                            f"{recipe['strength_min']:.0f}-{recipe['strength_max']:.0f}%"
                        ],
                        "RESULT": [
                            f"{batch['ph']:.2f}",
                            f"{batch['tsc']:.2f}%",
                            "Paste",
                            f"{batch['dl']:.2f}",
                            f"{batch['da']:.2f}",
                            f"{batch['db']:.2f}",
                            f"{batch['de']:.2f}",
                            f"{batch['colour_strength']:.2f}%"
                        ]
                    })

                    st.subheader("📋 COA Preview")
                    top_df = pd.DataFrame({
                        "Field": ["Product", "Batch No.", "Manufacturing date", "Expiry date"],
                        "Value": [recipe['colour_name'], batch['batch_number'], mfg_str, expiry_str]
                    })
                    st.dataframe(top_df, use_container_width=True, hide_index=True)

                    st.markdown("**Edit RESULT if needed:**")
                    edited_results = st.data_editor(
                        results_data,
                        use_container_width=True,
                        hide_index=True,
                        key=f"coa_editor_{batch_num}_{datetime.now().timestamp()}",
                        column_config={
                            "PARAMETER": st.column_config.TextColumn("Parameter", disabled=True),
                            "SPECIFICATION": st.column_config.TextColumn("Specification", disabled=True),
                            "RESULT": st.column_config.TextColumn("Result (editable)")
                        }
                    )

                    if st.button("📑 Generate COA PDF", type="primary"):
                        with st.spinner("Generating PDF, please wait..."):
                            pdf_buffer = generate_coa_pdf(batch_num, template, edited_results)
                        if pdf_buffer:
                            st.download_button(
                                label="⬇ Download COA (PDF)",
                                data=pdf_buffer,
                                file_name=f"COA_{batch_num}.pdf",
                                mime="application/pdf"
                            )
                            st.success("✅ COA generated successfully!")
                        else:
                            st.error("❌ Failed to generate COA. Check that all batch data is complete.")

    with report_tabs[2]:
        st.subheader("📥 Export Completed Data")
        completed_data = get_completed_batches()
        if completed_data.empty:
            st.info("No data to export.")
        else:
            st.dataframe(completed_data, use_container_width=True)
            csv = completed_data.to_csv(index=False)
            st.download_button(
                label="⬇ Download CSV",
                data=csv,
                file_name=f"completed_batches_{datetime.now().strftime('%Y%m%d')}.csv",
                mime="text/csv"
            )

# ---------- TAB 6: USER MANAGEMENT ----------
if is_admin():
    user_tab_index = tabs_list.index("👥 User Management")
    with tabs[user_tab_index]:
        st.header("👥 User Management")

        with st.form("add_user_form"):
            st.subheader("➕ Create New User")
            new_username = st.text_input("Username")
            new_password = st.text_input("Password", type="password")
            new_role = st.selectbox("Role", ["Admin", "Production", "QA"])
            if st.form_submit_button("Add User"):
                if new_username and new_password:
                    try:
                        add_user(new_username, new_password, new_role)
                        add_log(st.session_state.username, "Add User", f"Added user {new_username}")
                        st.toast(f"✅ User {new_username} added!", icon="✅")
                        st.rerun()
                    except Exception as e:
                        if "duplicate" in str(e).lower():
                            st.error("❌ Username already exists!")
                        else:
                            st.error(f"❌ Error: {e}")
                else:
                    st.error("❌ Username and password required.")

        st.subheader("📋 Existing Users")
        users_df = get_users()
        if not users_df.empty:
            st.dataframe(users_df, use_container_width=True)

            st.subheader("✏️ Edit User")
            user_list = users_df['username'].tolist()
            selected_user = st.selectbox("Select user", user_list)
            if selected_user:
                user_row = users_df[users_df['username'] == selected_user].iloc[0]
                with st.expander(f"Edit {selected_user}"):
                    with st.form("edit_user_form"):
                        new_pass = st.text_input("New Password", type="password", value="")
                        new_role = st.selectbox("New Role", ["Admin", "Production", "QA"],
                                                index=["Admin","Production","QA"].index(user_row['role']))
                        col1, col2 = st.columns(2)
                        with col1:
                            if st.form_submit_button("Update User"):
                                if new_pass:
                                    update_user(selected_user, new_pass, new_role)
                                else:
                                    conn = get_db_connection()
                                    old_pass = conn.execute("SELECT password FROM users WHERE username=?", (selected_user,)).fetchone()[0]
                                    conn.close()
                                    update_user(selected_user, old_pass, new_role)
                                add_log(st.session_state.username, "Update User", f"Updated user {selected_user}")
                                st.toast(f"✅ User {selected_user} updated!", icon="✅")
                                st.rerun()
                        with col2:
                            if selected_user != st.session_state.username:
                                if st.form_submit_button("Delete User", type="primary"):
                                    delete_user(selected_user)
                                    add_log(st.session_state.username, "Delete User", f"Deleted user {selected_user}")
                                    st.toast(f"🗑️ User {selected_user} deleted!", icon="🗑️")
                                    st.rerun()
                            else:
                                st.warning("You cannot delete your own account.")

# ---------- TAB 7: ACTIVITY LOG ----------
if is_admin():
    log_tab_index = tabs_list.index("📜 Activity Log")
    with tabs[log_tab_index]:
        st.header("📜 Activity Log (Traceability)")
        logs_df = get_logs()
        if logs_df.empty:
            st.info("No logs yet.")
        else:
            st.dataframe(logs_df, use_container_width=True)

# ---------- SIDEBAR REFRESH ----------
st.sidebar.button("🔄 Refresh Data", on_click=lambda: st.rerun())
st.caption("💡 Reports are available to all roles. SPC charts show trends vs control limits.")
