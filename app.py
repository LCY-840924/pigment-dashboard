import streamlit as st
import psycopg2
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

# ---------- DATABASE SETUP (Supabase / PostgreSQL) ----------
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    # Fallback – replace with your actual URL for local testing
    DATABASE_URL = "postgresql://postgres:Liewcy@201261@db.soksnhhthrmdrzfeglce.supabase.co:5432/postgres"

def get_db_connection():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def init_db():
    """Create tables if they don't exist, seed default data."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS colour_codes (
            id SERIAL PRIMARY KEY,
            code TEXT UNIQUE NOT NULL,
            description TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS recipes (
            id SERIAL PRIMARY KEY,
            colour_code_id INTEGER NOT NULL REFERENCES colour_codes(id),
            colour_name TEXT NOT NULL,
            tsc_min REAL, tsc_max REAL, ph_min REAL, ph_max REAL,
            visc_min REAL, visc_max REAL, de_max REAL,
            dl_tolerance REAL DEFAULT 0.5, da_tolerance REAL DEFAULT 0.6,
            db_tolerance REAL DEFAULT 0.6, strength_min REAL DEFAULT 95.0,
            strength_max REAL DEFAULT 105.0,
            UNIQUE(colour_code_id, colour_name)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS batches (
            batch_id TEXT PRIMARY KEY,
            batch_number TEXT UNIQUE,
            recipe_id INTEGER REFERENCES recipes(id),
            colour_code TEXT,
            status TEXT, stage TEXT,
            tsc REAL, ph REAL, visc REAL,
            de REAL, dl REAL, da REAL, db REAL, colour_strength REAL,
            manufacturing_date TEXT, attempt_count INTEGER DEFAULT 0,
            remark TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS seq_counter (
            colour_code TEXT PRIMARY KEY, last_seq INTEGER DEFAULT 0
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password TEXT NOT NULL,
            role TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            log_id SERIAL PRIMARY KEY,
            timestamp TEXT,
            username TEXT,
            action TEXT,
            details TEXT,
            batch_number TEXT,
            recipe_id INTEGER
        )
    """)
    # Seed default users
    cur.execute("SELECT COUNT(*) FROM users")
    if cur.fetchone()[0] == 0:
        default_users = [
            ("admin", "admin123", "Admin"),
            ("production", "prod123", "Production"),
            ("qa", "qa123", "QA")
        ]
        for u in default_users:
            cur.execute("INSERT INTO users (username, password, role) VALUES (%s, %s, %s)", u)
    # Seed sample colour codes
    cur.execute("SELECT COUNT(*) FROM colour_codes")
    if cur.fetchone()[0] == 0:
        sample_codes = [
            ("RED", "Red shades"),
            ("BLUE", "Blue shades"),
            ("GREEN", "Green shades"),
            ("YELLOW", "Yellow shades")
        ]
        for c in sample_codes:
            cur.execute("INSERT INTO colour_codes (code, description) VALUES (%s, %s)", c)
    conn.commit()
    cur.close()
    conn.close()
    return "Database initialized successfully."

init_msg = init_db()

# ---------- LOGGING ----------
def add_log(username, action, details, batch_number=None, recipe_id=None):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cur.execute("INSERT INTO logs (timestamp, username, action, details, batch_number, recipe_id) VALUES (%s, %s, %s, %s, %s, %s)",
                    (timestamp, username, action, details, batch_number, recipe_id))
        conn.commit()
        cur.close()
        conn.close()
    except:
        pass

# ---------- DATABASE FUNCTIONS (with caching) ----------
@st.cache_data(ttl=300)
def get_colour_codes():
    conn = get_db_connection()
    df = pd.read_sql_query("SELECT * FROM colour_codes ORDER BY code", conn)
    conn.close()
    return df

def add_colour_code(code, description, username):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("INSERT INTO colour_codes (code, description) VALUES (%s, %s)", (code, description))
        conn.commit()
        cur.close()
        conn.close()
        add_log(username, "Add Colour Code", f"Added colour code {code}")
        return True, None
    except Exception as e:
        return False, str(e)

def update_colour_code(code_id, code, description, username):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE colour_codes SET code = %s, description = %s WHERE id = %s", (code, description, code_id))
        conn.commit()
        cur.close()
        conn.close()
        add_log(username, "Update Colour Code", f"Updated colour code {code}")
        return True, None
    except Exception as e:
        return False, str(e)

def delete_colour_code(code_id, username):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM recipes WHERE colour_code_id = %s", (code_id,))
        if cur.fetchone()[0] > 0:
            conn.close()
            return False, "Cannot delete: there are recipes using this colour code."
        cur.execute("DELETE FROM colour_codes WHERE id = %s", (code_id,))
        conn.commit()
        cur.close()
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
    df = pd.read_sql_query(query, conn)
    conn.close()
    return df

def get_recipe_by_id(recipe_id):
    conn = get_db_connection()
    df = pd.read_sql_query("SELECT * FROM recipes WHERE id = %s", conn, params=(recipe_id,))
    conn.close()
    return df

def add_recipe(colour_code_id, colour_name, tsc_min, tsc_max, ph_min, ph_max,
               visc_min, visc_max, de_max, dl_tol, da_tol, db_tol, str_min, str_max, username):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""INSERT INTO recipes (colour_code_id, colour_name, tsc_min, tsc_max, ph_min, ph_max,
                     visc_min, visc_max, de_max, dl_tolerance, da_tolerance, db_tolerance,
                     strength_min, strength_max)
                     VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                  (colour_code_id, colour_name, tsc_min, tsc_max, ph_min, ph_max,
                   visc_min, visc_max, de_max, dl_tol, da_tol, db_tol, str_min, str_max))
        recipe_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
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
        cur = conn.cursor()
        cur.execute("""UPDATE recipes SET
                     colour_name = %s, tsc_min = %s, tsc_max = %s, ph_min = %s, ph_max = %s,
                     visc_min = %s, visc_max = %s, de_max = %s,
                     dl_tolerance = %s, da_tolerance = %s, db_tolerance = %s,
                     strength_min = %s, strength_max = %s
                     WHERE id = %s""",
                  (colour_name, tsc_min, tsc_max, ph_min, ph_max, visc_min, visc_max,
                   de_max, dl_tol, da_tol, db_tol, str_min, str_max, recipe_id))
        conn.commit()
        cur.close()
        conn.close()
        add_log(username, "Update Recipe", f"Updated recipe ID {recipe_id}", recipe_id=recipe_id)
        return True, None
    except Exception as e:
        return False, str(e)

def delete_recipe(recipe_id, username):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM batches WHERE recipe_id = %s", (recipe_id,))
        if cur.fetchone()[0] > 0:
            conn.close()
            return False, "Cannot delete recipe: it is used by one or more batches."
        cur.execute("DELETE FROM recipes WHERE id = %s", (recipe_id,))
        conn.commit()
        cur.close()
        conn.close()
        add_log(username, "Delete Recipe", f"Deleted recipe ID {recipe_id}", recipe_id=recipe_id)
        return True, None
    except Exception as e:
        return False, str(e)

# ---------- BATCH FUNCTIONS (with caching) ----------
@st.cache_data(ttl=300)
def get_batches():
    conn = get_db_connection()
    df = pd.read_sql_query("SELECT * FROM batches ORDER BY created_at DESC", conn)
    conn.close()
    return df

@st.cache_data(ttl=300)
def get_completed_batches():
    conn = get_db_connection()
    df = pd.read_sql_query("SELECT * FROM batches WHERE status = 'Completed' ORDER BY created_at DESC", conn)
    conn.close()
    return df

def batch_exists(batch_number):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM batches WHERE batch_number = %s", (batch_number,))
    exists = cur.fetchone() is not None
    conn.close()
    return exists

def add_batch(batch_number, recipe_id, colour_code, manufacturing_date, username):
    conn = get_db_connection()
    cur = conn.cursor()
    batch_id = f"b_{batch_number}"
    cur.execute(
        "INSERT INTO batches (batch_id, batch_number, recipe_id, colour_code, status, stage, manufacturing_date) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (batch_id, batch_number, recipe_id, colour_code, 'Issued', 'Mixing', manufacturing_date))
    conn.commit()
    cur.close()
    conn.close()
    add_log(username, "Issue Batch", f"Issued batch {batch_number} for {colour_code}", batch_number=batch_number, recipe_id=recipe_id)
    return batch_number

def update_status(batch_id, status, stage, username):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT batch_number FROM batches WHERE batch_id = %s", (batch_id,))
    batch_number = cur.fetchone()[0]
    cur.execute("UPDATE batches SET status = %s, stage = %s WHERE batch_id = %s", (status, stage, batch_id))
    conn.commit()
    cur.close()
    conn.close()
    add_log(username, "Update Status", f"Batch {batch_number} status changed to {status} (stage: {stage})", batch_number=batch_number)

def update_qa(batch_id, tsc, ph, visc, de, dl, da, db, colour_strength, remark, username):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT batch_number FROM batches WHERE batch_id = %s", (batch_id,))
    batch_number = cur.fetchone()[0]
    cur.execute("""SELECT tsc_min, tsc_max, ph_min, ph_max, visc_min, visc_max,
                        de_max, dl_tolerance, da_tolerance, db_tolerance,
                        strength_min, strength_max
                 FROM recipes r JOIN batches b ON r.id = b.recipe_id
                 WHERE b.batch_id = %s""", (batch_id,))
    row = cur.fetchone()
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

    cur.execute("SELECT attempt_count FROM batches WHERE batch_id = %s", (batch_id,))
    current_attempt = cur.fetchone()[0] or 0
    new_attempt = current_attempt + 1

    if passed:
        status, stage, msg = 'QA_Passed', 'Finished', '✅ QA PASSED!'
    else:
        status, stage, msg = 'QA_Failed', 'Milling', '❌ QA FAILED! Back to Milling.'

    cur.execute(
        """UPDATE batches SET tsc = %s, ph = %s, visc = %s, de = %s, dl = %s, da = %s, db = %s,
           colour_strength = %s, status = %s, stage = %s, attempt_count = %s, remark = %s
           WHERE batch_id = %s""",
        (tsc, ph, visc, de, dl, da, db, colour_strength, status, stage, new_attempt, remark, batch_id))
    conn.commit()
    cur.close()
    conn.close()
    add_log(username, "Submit QA", f"QA submitted for batch {batch_number}, result: {msg}", batch_number=batch_number)
    return msg

# ---------- USER MANAGEMENT (with caching) ----------
@st.cache_data(ttl=300)
def get_users():
    conn = get_db_connection()
    df = pd.read_sql_query("SELECT username, role FROM users ORDER BY username", conn)
    conn.close()
    return df

def add_user(username, password, role):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO users (username, password, role) VALUES (%s, %s, %s)", (username, password, role))
    conn.commit()
    cur.close()
    conn.close()

def update_user(username, password, role):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE users SET password = %s, role = %s WHERE username = %s", (password, role, username))
    conn.commit()
    cur.close()
    conn.close()

def delete_user(username):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM users WHERE username = %s", (username,))
    conn.commit()
    cur.close()
    conn.close()

def check_login(username, password):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT username, role FROM users WHERE username = %s AND password = %s", (username, password))
    row = cur.fetchone()
    conn.close()
    return row

@st.cache_data(ttl=300)
def get_logs():
    conn = get_db_connection()
    df = pd.read_sql_query("SELECT * FROM logs ORDER BY timestamp DESC", conn)
    conn.close()
    return df

# ---------- BACKUP / RESTORE ----------
def export_db_to_zip():
    conn = get_db_connection()
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for table in ['colour_codes', 'recipes', 'batches', 'seq_counter', 'users', 'logs']:
            try:
                df = pd.read_sql_query(f"SELECT * FROM {table}", conn)
                csv_data = df.to_csv(index=False).encode('utf-8')
                zipf.writestr(f"{table}.csv", csv_data)
            except:
                pass
    conn.close()
    zip_buffer.seek(0)
    return zip_buffer

def import_db_from_zip(zip_file):
    conn = get_db_connection()
    cur = conn.cursor()
    with zipfile.ZipFile(zip_file, 'r') as zipf:
        for table in ['colour_codes', 'recipes', 'batches', 'seq_counter', 'users', 'logs']:
            if f"{table}.csv" in zipf.namelist():
                df = pd.read_csv(zipf.open(f"{table}.csv"))
                cur.execute(f"DELETE FROM {table}")
                # Insert using a simple loop (could use COPY for speed, but this works)
                for _, row in df.iterrows():
                    columns = ', '.join(row.index)
                    placeholders = ', '.join(['%s'] * len(row))
                    cur.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", tuple(row))
    conn.commit()
    cur.close()
    conn.close()

# ---------- COA GENERATION (unchanged - keep the existing generate_coa_pdf function) ----------
# ... (the COA function is long; you can keep the same one from earlier – it doesn't touch the database)

# ---------- STREAMLIT APP ----------
st.set_page_config(page_title="Pigment Monitor", layout="wide")

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
st.sidebar.write(f"Using: Supabase PostgreSQL")
st.sidebar.write(f"Init: {init_msg}")

# ---- Role helpers ----
def is_admin():
    return st.session_state.role == "Admin"
def is_production():
    return st.session_state.role == "Production"
def is_qa():
    return st.session_state.role == "QA"

st.title("🎨 Pigment Dispersion System")

# Build tabs (same as before) – keep all the tabs from your original code.
# They are exactly the same; I'm omitting them here for brevity, but you can keep the entire tab logic from your SQLite version.
# Please refer to the original code for the tabs – everything remains identical.
