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
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

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

# ---------- DATABASE SETUP (Supabase via SQLAlchemy) ----------
# Use environment variable if set (Streamlit Cloud Secrets)
DATABASE_URL = os.environ.get("DATABASE_URL")

# For local testing, you can uncomment and set your URL here (but better to use .env)
if not DATABASE_URL:
    DATABASE_URL = "postgresql://postgres:Liewcy%40201261@db.xxxxxxxxxx.supabase.co:5432/postgres"
    # Replace the above with your actual Supabase URL

# Create engine with connection pooling and SSL
engine = create_engine(
    DATABASE_URL,
    connect_args={'sslmode': 'require'},
    pool_size=5,
    max_overflow=10
)

def get_db_connection():
    """Return a SQLAlchemy connection."""
    try:
        return engine.connect()
    except SQLAlchemyError as e:
        st.error(f"❌ Database connection failed: {e}")
        st.stop()

def init_db():
    """Create tables if they don't exist, seed default data."""
    conn = get_db_connection()
    try:
        # Create tables
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS colour_codes (
                id SERIAL PRIMARY KEY,
                code TEXT UNIQUE NOT NULL,
                description TEXT
            )
        """))
        conn.execute(text("""
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
        """))
        conn.execute(text("""
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
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS seq_counter (
                colour_code TEXT PRIMARY KEY, last_seq INTEGER DEFAULT 0
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                password TEXT NOT NULL,
                role TEXT NOT NULL
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS logs (
                log_id SERIAL PRIMARY KEY,
                timestamp TEXT,
                username TEXT,
                action TEXT,
                details TEXT,
                batch_number TEXT,
                recipe_id INTEGER
            )
        """))

        # Seed default users
        res = conn.execute(text("SELECT COUNT(*) FROM users")).fetchone()
        if res[0] == 0:
            default_users = [
                ("admin", "admin123", "Admin"),
                ("production", "prod123", "Production"),
                ("qa", "qa123", "QA")
            ]
            for u in default_users:
                conn.execute(
                    text("INSERT INTO users (username, password, role) VALUES (:u, :p, :r)"),
                    {"u": u[0], "p": u[1], "r": u[2]}
                )

        # Seed sample colour codes
        res = conn.execute(text("SELECT COUNT(*) FROM colour_codes")).fetchone()
        if res[0] == 0:
            sample_codes = [
                ("RED", "Red shades"),
                ("BLUE", "Blue shades"),
                ("GREEN", "Green shades"),
                ("YELLOW", "Yellow shades")
            ]
            for c in sample_codes:
                conn.execute(
                    text("INSERT INTO colour_codes (code, description) VALUES (:code, :desc)"),
                    {"code": c[0], "desc": c[1]}
                )

        conn.commit()
        return "Database initialized successfully."
    except Exception as e:
        conn.rollback()
        return f"Error initializing DB: {str(e)}"
    finally:
        conn.close()

# Call init_db once at startup
init_msg = init_db()
if "Error" in init_msg:
    st.error(f"⚠️ Database initialization failed: {init_msg}")
    st.stop()

# ---------- LOGGING ----------
def add_log(username, action, details, batch_number=None, recipe_id=None):
    try:
        conn = get_db_connection()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            text("INSERT INTO logs (timestamp, username, action, details, batch_number, recipe_id) VALUES (:ts, :u, :a, :d, :b, :r)"),
            {"ts": timestamp, "u": username, "a": action, "d": details, "b": batch_number, "r": recipe_id}
        )
        conn.commit()
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
        conn.execute(
            text("INSERT INTO colour_codes (code, description) VALUES (:code, :desc)"),
            {"code": code, "desc": description}
        )
        conn.commit()
        conn.close()
        add_log(username, "Add Colour Code", f"Added colour code {code}")
        return True, None
    except Exception as e:
        return False, str(e)

def update_colour_code(code_id, code, description, username):
    try:
        conn = get_db_connection()
        conn.execute(
            text("UPDATE colour_codes SET code = :code, description = :desc WHERE id = :id"),
            {"code": code, "desc": description, "id": code_id}
        )
        conn.commit()
        conn.close()
        add_log(username, "Update Colour Code", f"Updated colour code {code}")
        return True, None
    except Exception as e:
        return False, str(e)

def delete_colour_code(code_id, username):
    try:
        conn = get_db_connection()
        res = conn.execute(
            text("SELECT COUNT(*) FROM recipes WHERE colour_code_id = :id"),
            {"id": code_id}
        ).fetchone()
        if res[0] > 0:
            conn.close()
            return False, "Cannot delete: there are recipes using this colour code."
        conn.execute(text("DELETE FROM colour_codes WHERE id = :id"), {"id": code_id})
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
    df = pd.read_sql_query(query, conn)
    conn.close()
    return df

def get_recipe_by_id(recipe_id):
    conn = get_db_connection()
    df = pd.read_sql_query("SELECT * FROM recipes WHERE id = :id", conn, params={"id": recipe_id})
    conn.close()
    return df

def add_recipe(colour_code_id, colour_name, tsc_min, tsc_max, ph_min, ph_max,
               visc_min, visc_max, de_max, dl_tol, da_tol, db_tol, str_min, str_max, username):
    try:
        conn = get_db_connection()
        result = conn.execute(
            text("""INSERT INTO recipes 
                     (colour_code_id, colour_name, tsc_min, tsc_max, ph_min, ph_max,
                      visc_min, visc_max, de_max, dl_tolerance, da_tolerance, db_tolerance,
                      strength_min, strength_max)
                   VALUES 
                     (:cc_id, :name, :tsc_min, :tsc_max, :ph_min, :ph_max,
                      :visc_min, :visc_max, :de_max, :dl_tol, :da_tol, :db_tol,
                      :str_min, :str_max) RETURNING id"""),
            {
                "cc_id": colour_code_id,
                "name": colour_name,
                "tsc_min": tsc_min,
                "tsc_max": tsc_max,
                "ph_min": ph_min,
                "ph_max": ph_max,
                "visc_min": visc_min,
                "visc_max": visc_max,
                "de_max": de_max,
                "dl_tol": dl_tol,
                "da_tol": da_tol,
                "db_tol": db_tol,
                "str_min": str_min,
                "str_max": str_max
            }
        )
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
        conn.execute(
            text("""UPDATE recipes SET
                     colour_name = :name,
                     tsc_min = :tsc_min, tsc_max = :tsc_max,
                     ph_min = :ph_min, ph_max = :ph_max,
                     visc_min = :visc_min, visc_max = :visc_max,
                     de_max = :de_max,
                     dl_tolerance = :dl_tol, da_tolerance = :da_tol, db_tolerance = :db_tol,
                     strength_min = :str_min, strength_max = :str_max
                   WHERE id = :id"""),
            {
                "id": recipe_id,
                "name": colour_name,
                "tsc_min": tsc_min,
                "tsc_max": tsc_max,
                "ph_min": ph_min,
                "ph_max": ph_max,
                "visc_min": visc_min,
                "visc_max": visc_max,
                "de_max": de_max,
                "dl_tol": dl_tol,
                "da_tol": da_tol,
                "db_tol": db_tol,
                "str_min": str_min,
                "str_max": str_max
            }
        )
        conn.commit()
        conn.close()
        add_log(username, "Update Recipe", f"Updated recipe ID {recipe_id}", recipe_id=recipe_id)
        return True, None
    except Exception as e:
        return False, str(e)

def delete_recipe(recipe_id, username):
    try:
        conn = get_db_connection()
        res = conn.execute(
            text("SELECT COUNT(*) FROM batches WHERE recipe_id = :id"),
            {"id": recipe_id}
        ).fetchone()
        if res[0] > 0:
            conn.close()
            return False, "Cannot delete recipe: it is used by one or more batches."
        conn.execute(text("DELETE FROM recipes WHERE id = :id"), {"id": recipe_id})
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
    res = conn.execute(
        text("SELECT 1 FROM batches WHERE batch_number = :bn"),
        {"bn": batch_number}
    ).fetchone()
    conn.close()
    return res is not None

def add_batch(batch_number, recipe_id, colour_code, manufacturing_date, username):
    conn = get_db_connection()
    batch_id = f"b_{batch_number}"
    conn.execute(
        text("""INSERT INTO batches 
                 (batch_id, batch_number, recipe_id, colour_code, status, stage, manufacturing_date)
               VALUES (:bid, :bn, :rid, :cc, 'Issued', 'Mixing', :mfg)"""),
        {"bid": batch_id, "bn": batch_number, "rid": recipe_id, "cc": colour_code, "mfg": manufacturing_date}
    )
    conn.commit()
    conn.close()
    add_log(username, "Issue Batch", f"Issued batch {batch_number} for {colour_code}", batch_number=batch_number, recipe_id=recipe_id)
    return batch_number

def update_status(batch_id, status, stage, username):
    conn = get_db_connection()
    batch_number = conn.execute(
        text("SELECT batch_number FROM batches WHERE batch_id = :bid"),
        {"bid": batch_id}
    ).fetchone()[0]
    conn.execute(
        text("UPDATE batches SET status = :status, stage = :stage WHERE batch_id = :bid"),
        {"status": status, "stage": stage, "bid": batch_id}
    )
    conn.commit()
    conn.close()
    add_log(username, "Update Status", f"Batch {batch_number} status changed to {status} (stage: {stage})", batch_number=batch_number)

def update_qa(batch_id, tsc, ph, visc, de, dl, da, db, colour_strength, remark, username):
    conn = get_db_connection()
    batch_number = conn.execute(
        text("SELECT batch_number FROM batches WHERE batch_id = :bid"),
        {"bid": batch_id}
    ).fetchone()[0]
    row = conn.execute(
        text("""SELECT tsc_min, tsc_max, ph_min, ph_max, visc_min, visc_max,
                       de_max, dl_tolerance, da_tolerance, db_tolerance,
                       strength_min, strength_max
                FROM recipes r JOIN batches b ON r.id = b.recipe_id
                WHERE b.batch_id = :bid"""),
        {"bid": batch_id}
    ).fetchone()
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

    current_attempt = conn.execute(
        text("SELECT attempt_count FROM batches WHERE batch_id = :bid"),
        {"bid": batch_id}
    ).fetchone()[0] or 0
    new_attempt = current_attempt + 1

    if passed:
        status, stage, msg = 'QA_Passed', 'Finished', '✅ QA PASSED!'
    else:
        status, stage, msg = 'QA_Failed', 'Milling', '❌ QA FAILED! Back to Milling.'

    conn.execute(
        text("""UPDATE batches SET
                 tsc = :tsc, ph = :ph, visc = :visc, de = :de,
                 dl = :dl, da = :da, db = :db,
                 colour_strength = :cs,
                 status = :status, stage = :stage,
                 attempt_count = :attempt, remark = :remark
               WHERE batch_id = :bid"""),
        {
            "tsc": tsc, "ph": ph, "visc": visc, "de": de,
            "dl": dl, "da": da, "db": db,
            "cs": colour_strength,
            "status": status, "stage": stage,
            "attempt": new_attempt, "remark": remark,
            "bid": batch_id
        }
    )
    conn.commit()
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
    conn.execute(
        text("INSERT INTO users (username, password, role) VALUES (:u, :p, :r)"),
        {"u": username, "p": password, "r": role}
    )
    conn.commit()
    conn.close()

def update_user(username, password, role):
    conn = get_db_connection()
    conn.execute(
        text("UPDATE users SET password = :p, role = :r WHERE username = :u"),
        {"p": password, "r": role, "u": username}
    )
    conn.commit()
    conn.close()

def delete_user(username):
    conn = get_db_connection()
    conn.execute(text("DELETE FROM users WHERE username = :u"), {"u": username})
    conn.commit()
    conn.close()

def check_login(username, password):
    conn = get_db_connection()
    row = conn.execute(
        text("SELECT username, role FROM users WHERE username = :u AND password = :p"),
        {"u": username, "p": password}
    ).fetchone()
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
    with zipfile.ZipFile(zip_file, 'r') as zipf:
        for table in ['colour_codes', 'recipes', 'batches', 'seq_counter', 'users', 'logs']:
            if f"{table}.csv" in zipf.namelist():
                df = pd.read_csv(zipf.open(f"{table}.csv"))
                conn.execute(text(f"DELETE FROM {table}"))
                df.to_sql(table, conn, if_exists='append', index=False)
    conn.commit()
    conn.close()

# ---------- COA GENERATION (unchanged - keep the existing generate_coa_pdf function) ----------
# ... (the COA function is long; you can keep the same one from your SQLite version – it doesn't touch the database)

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

# Build tabs (same as before) – keep all your existing tab code here.
# The tabs (Define Recipe, Issue Batch, etc.) are exactly the same as in your original app.
# I'm omitting them here for brevity – you can copy them from your SQLite version.
# Everything remains identical; only the database layer is changed.
