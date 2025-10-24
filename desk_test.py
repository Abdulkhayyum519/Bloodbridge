# app.py — BLOOD BRIDGE (per-bank accept/reject)
# App-assigned IDs + correct timestamp semantics for OPEN/PARTIAL/FULFILLED/REJECTED

from flask import Flask, render_template, request, redirect, url_for, jsonify
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from datetime import datetime
from functools import wraps
import os
import secrets
import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv; load_dotenv()

# -----------------------------
# Config / DB
# -----------------------------
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:abc123@localhost:5432/bloodbridge"
)

app = Flask(__name__)

# SECRET_KEY must come from env
app.secret_key = os.getenv('SECRET_KEY')
if not app.secret_key:
    raise RuntimeError("SECRET_KEY env var is required")

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

def get_db():
    """Open a PostgreSQL connection and set search_path."""
    conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    with conn.cursor() as cur:
        cur.execute("SET search_path TO core, ops, public;")
    return conn


# =========================
# Helpers — IDs, statuses, parsing & inventory
# =========================
import secrets

# -----------------------------
# Request status helpers
# -----------------------------
REQUEST_STATUS_OPEN       = "OPEN"
REQUEST_STATUS_CLAIMED    = "CLAIMED"      # kept for compatibility if you later add a "claim" step
REQUEST_STATUS_PARTIAL     = "PARTIAL"      # used for filtering/UX; rows themselves are OPEN or FULFILLED
REQUEST_STATUS_FULFILLED  = "FULFILLED"
REQUEST_STATUS_REJECTED   = "REJECTED"

VALID_REQUEST_STATUSES = {
    REQUEST_STATUS_OPEN,
    REQUEST_STATUS_CLAIMED,
    REQUEST_STATUS_PARTIAL,
    REQUEST_STATUS_FULFILLED,
    REQUEST_STATUS_REJECTED,
}

# -----------------------------
# Normalization & level parsing
# -----------------------------
_COMPONENT_MAP = {
    "rbc": "RBC",
    "plasma": "Plasma",
    "platelets": "Platelets",
    "whole": "Whole",
}
_LEVEL_MAP = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}

def _norm_bt(s: str) -> str:
    """Normalize blood type to uppercase (e.g., 'A-')."""
    return (s or "").strip().upper()

def norm_component_for_db(s: str) -> str:
    """
    Return exact DB value for component ('RBC', 'Plasma', 'Platelets', 'Whole')
    or raise ValueError on invalid input. Accepts canonical names or lower keys.
    """
    key = (s or "").strip().lower()
    if key in _COMPONENT_MAP:
        return _COMPONENT_MAP[key]
    if s in _COMPONENT_MAP.values():
        return s
    raise ValueError(f"Invalid component '{s}'. Must be one of {list(_COMPONENT_MAP.values())}.")

def parse_level(value):
    """
    Return int level 1–3 or None. Accepts 'LOW|MEDIUM|HIGH' or '1|2|3'.
    """
    if value in (None, ""):
        return None
    s = str(value).strip()
    if s.isdigit():
        return int(s)
    s_up = s.upper()
    if s_up in _LEVEL_MAP:
        return _LEVEL_MAP[s_up]
    raise ValueError("Invalid level. Use LOW/MEDIUM/HIGH or 1/2/3.")

# -----------------------------
# ID generation (app logic)
# -----------------------------
def gen_transaction_id(db, entity_id: str) -> str:
    """
    Generate a unique transaction_id: '<ENTITY_ID>-<6 hex>'.
    Uniqueness is enforced against ops.transaction_logs.
    """
    while True:
        suffix = secrets.token_hex(3)  # 6 hex chars
        tx_id = f"{entity_id}-{suffix}"
        exists = db.execute(
            "SELECT 1 FROM ops.transaction_logs WHERE transaction_id = %s LIMIT 1;",
            (tx_id,)
        ).fetchone()
        if not exists:
            return tx_id

def next_request_id(db, origin_prefix: str) -> str:
    """
    Return next request id as '<prefix><NNNN>' based on the count of DISTINCT request_ids
    with that prefix. Use 'hops-' for hospital-origin requests and 'bank-' for bank-origin.
    """
    row = db.execute(
        "SELECT COALESCE(COUNT(DISTINCT request_id), 0) AS c "
        "FROM ops.transaction_logs WHERE request_id LIKE %s;",
        (origin_prefix + "%",)
    ).fetchone()
    n = int(row["c"]) + 1
    return f"{origin_prefix}{n:04d}"

# -----------------------------
# Inventory helpers (time-series append)
# -----------------------------
def get_bank_stock(db, bank_org_id: str, blood_type: str, component: str) -> int:
    """
    Return the latest known units for a bank/org for a blood_type + component.
    """
    bt = _norm_bt(blood_type)
    comp = norm_component_for_db(component)
    row = db.execute("""
        SELECT COALESCE(units, 0) AS units
          FROM ops.inventory
         WHERE org_id = %s AND blood_type = %s AND component = %s
         ORDER BY updated_at DESC
         LIMIT 1;
    """, (bank_org_id, bt, comp)).fetchone()
    return int(row["units"] if row else 0)

def get_inventory_units(db, org_id: str, blood_type: str, component: str) -> int:
    """
    Return the latest known units for any org for a blood_type + component.
    """
    row = db.execute("""
        SELECT COALESCE(units, 0) AS units
          FROM ops.inventory
         WHERE org_id = %s AND blood_type = %s AND component = %s
         ORDER BY updated_at DESC
         LIMIT 1;
    """, (org_id, _norm_bt(blood_type), norm_component_for_db(component))).fetchone()
    return int(row["units"] if row else 0)

def upsert_inventory(db, org_id: str, blood_type: str, component: str, delta_units: int):
    """
    Append a new inventory version row (Timeseries style):
    new_units = max(0, latest_units + delta_units).
    """
    bt = _norm_bt(blood_type)
    comp = norm_component_for_db(component)

    prev = db.execute("""
        SELECT units
          FROM ops.inventory
         WHERE org_id = %s AND blood_type = %s AND component = %s
         ORDER BY updated_at DESC
         LIMIT 1;
    """, (org_id, bt, comp)).fetchone()

    prev_units = int(prev["units"]) if prev and prev["units"] is not None else 0
    new_units  = max(0, prev_units + int(delta_units))

    db.execute("""
        INSERT INTO ops.inventory (org_id, blood_type, component, units, updated_at)
        VALUES (%s, %s, %s, %s, NOW());
    """, (org_id, bt, comp, new_units))

# -----------------------------
# Optional utility (NOT used to auto-fulfill)
# -----------------------------
def find_bank_with_stock(db, blood_type: str, component: str, min_units: int):
    """
    Utility to query a bank with enough stock (latest snapshot).
    Keep for diagnostics or manual selection UIs.
    NOTE: The app should NOT auto-fulfill on create; fulfillment is by manual accept.
    """
    bt = _norm_bt(blood_type)
    comp = norm_component_for_db(component)

    return db.execute("""
        WITH latest AS (
            SELECT DISTINCT ON (i.org_id)
                   i.org_id, i.units
              FROM ops.inventory i
              JOIN core.organizations o ON o.org_id = i.org_id
             WHERE o.org_type = 'BloodBank'
               AND i.blood_type = %s
               AND i.component  = %s
             ORDER BY i.org_id, i.updated_at DESC
        )
        SELECT org_id AS bank_id, COALESCE(units, 0) AS units
          FROM latest
         WHERE COALESCE(units, 0) >= %s
         ORDER BY units DESC, org_id
         LIMIT 1;
    """, (bt, comp, int(min_units))).fetchone()


# -----------------------------
# User model
# -----------------------------

class User(UserMixin):
    __slots__ = ("id","username","role","org_id","donor_id")
    def __init__(self, user_pk, username, role, org_id=None, donor_id=None):
        self.id = str(user_pk)
        self.username = username
        self.role = role
        self.org_id = str(org_id) if org_id is not None else None
        self.donor_id = str(donor_id) if donor_id is not None else None
    def get_id(self): return self.id

# --- loader ---
@login_manager.user_loader
def load_user(user_id):
    db = get_db()
    row = db.execute("""
        SELECT username, id, role
        FROM core.auth
        WHERE username = %s OR lower(username) = lower(%s)
        LIMIT 1;
    """, (user_id, user_id)).fetchone()
    if not row: return None

    if row["role"] in ("Hospital","BloodBank"):
        return User(row["username"], row["username"], row["role"], org_id=row["id"])
    if row["role"] == "Donor":
        return User(row["username"], row["username"], row["role"], donor_id=row["id"])
    return User(row["username"], row["username"], row["role"])



# -----------------------------
# Auth utils
# -----------------------------
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
_ph = PasswordHasher()

def verify_password(plain_text: str, stored_value: str) -> bool:
    if not stored_value:
        return False
    try:
        return _ph.verify(stored_value, plain_text)
    except VerifyMismatchError:
        return False
    except Exception:
        return False

def role_required(*roles):
    def wrapper(fn):
        @wraps(fn)
        def decorated_view(*args, **kwargs):
            if not current_user.is_authenticated:
                return redirect(url_for('login'))
            if current_user.role not in roles:
                return redirect(url_for('dashboard'))
            return fn(*args, **kwargs)
        return decorated_view
    return wrapper

# =========================
# Inventory management UI
# =========================
@app.route('/inventory/manage')
@login_required
def manage_inventory():
    db = get_db()
    rows = db.execute("""
        WITH latest AS (
            SELECT DISTINCT ON (i.blood_type, i.component)
                   i.org_id, i.blood_type, i.component, i.units, i.updated_at
              FROM ops.inventory i
             WHERE i.org_id = %s
             ORDER BY i.blood_type, i.component, i.updated_at DESC
        )
        SELECT NULL::bigint AS id,
               org_id,
               UPPER(blood_type) AS blood_type,
               UPPER(component)  AS component,
               COALESCE(units,0) AS units,
               COALESCE(updated_at::text, '') AS updated_at
          FROM latest
         ORDER BY blood_type, component;
    """, (current_user.org_id,)).fetchall()

    return render_template('inventory_manage.html',
                           org=current_user.org_id,
                           role=current_user.role,
                           inventory=rows)

@app.route('/inventory/update', methods=['POST'])
@login_required
def update_inventory():
    db = get_db()
    org_id = current_user.org_id

    blood_type = _norm_bt(request.form.get('blood_type') or '')
    component_in = (request.form.get('component') or '')
    action     = (request.form.get('action') or '').strip().lower()

    try:
        component = norm_component_for_db(component_in)
    except ValueError:
        return redirect(url_for('manage_inventory'))

    if not blood_type or not component:
        return redirect(url_for('manage_inventory'))

    units_raw = request.form.get('units')
    try:
        units_val = int(units_raw) if units_raw not in (None, '') else 0
    except ValueError:
        return redirect(url_for('manage_inventory'))

    try:
        if action == 'set':
            current_units = get_inventory_units(db, org_id, blood_type, component)
            delta = int(units_val) - current_units
            upsert_inventory(db, org_id, blood_type, component, delta)
            db.commit()

        elif action == 'add':
            if units_val > 0:
                upsert_inventory(db, org_id, blood_type, component, +units_val)
                db.commit()

        elif action == 'remove':
            if units_val > 0:
                upsert_inventory(db, org_id, blood_type, component, -units_val)
                db.commit()

        elif action == 'delete':
            current_units = get_inventory_units(db, org_id, blood_type, component)
            if current_units > 0:
                upsert_inventory(db, org_id, blood_type, component, -current_units)
            else:
                upsert_inventory(db, org_id, blood_type, component, 0)
            db.commit()

        else:
            pass

    except Exception:
        db.rollback()

    return redirect(url_for('manage_inventory'))

# ---------------------------
# Routes
# ---------------------------
@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

# ---------- Helpers for timestamps in inserts ----------

EARLIEST_REQ_SQL = """
COALESCE(
  (SELECT requested_at
     FROM ops.transaction_logs
    WHERE request_id = %s
    ORDER BY requested_at ASC
    LIMIT 1),
  NOW()
)
"""

def insert_open_event(db, tx_id, request_id, hospital_id, blood_type, component, level, units, requested_at_sql="NOW()"):
    """OPEN event: requested_at provided (default NOW()), completed_at NULL."""
    db.execute(f"""
        INSERT INTO ops.transaction_logs
            (transaction_id,
             request_id,
             requester_entity_type, requester_entity_id,
             blood_type, component, level,
             units_requested, status, requested_at, completed_at)
        VALUES
            (%s,
             %s,
             'Hospital', %s,
             %s, %s, %s,
             %s, 'OPEN', {requested_at_sql}, NULL);
    """, (tx_id, request_id, hospital_id,
          blood_type, component, level, units))

def insert_fulfillment_event(db, tx_id, request_id, hospital_id, bank_id, blood_type, component, level, units):
    """FULFILLED/PARTIAL/REJECTED helper uses earliest requested_at and NOW() completed."""
    # status is determined by caller (FULFILLED/PARTIAL/REJECTED); we pass it.
    pass

@app.route('/request/new', methods=['GET', 'POST'])
@login_required
def new_request():
    """
    Hybrid route combining Version 1 and Version 2 behaviors.

    - If user is BloodBank: immediately redirect to the Blood Drive form (`new_blood_drive`).
      (Keeps the single "New Request" button working for banks without extra UI links.)
    - If user is Hospital: show the Hospital form (GET) and handle Emergency submissions (POST).
      Blood Drive choice from this form redirects to `new_blood_drive`.
    - Any other role: bounce to dashboard.

    Effectively:
      * /request/new stays the single entrypoint for both roles.
      * Hospital logic remains clean and hospital-only for actual Emergency creation.
    """
    role = (current_user.role or "").strip()

    # ---- BloodBank users: go straight to Blood Drive form (single option for banks) ----
    if role == "BloodBank":
        return redirect(url_for('new_blood_drive'))

    # ---- Only Hospitals can proceed with creating requests here ----
    if role != "Hospital":
        return redirect(url_for('dashboard'))

    db = get_db()

    if request.method == 'POST':
        # Step 1: which urgency was chosen?
        urgency_kind = (request.form.get('urgency_kind') or '').strip().lower()

        # Blood Drive → dedicated form (date + location, level=2, request_to='Donor')
        if urgency_kind == 'blood_drive':
            return redirect(url_for('new_blood_drive'))

        # Only “Emergency” creates a real request here
        if urgency_kind != 'emergency':
            return redirect(url_for('new_request'))

        # Step 2: collect Emergency fields (Hospital creates a level=1 request)
        blood_type_raw = request.form.get('blood_type') or ''
        component_in   = request.form.get('component') or ''
        units_raw      = request.form.get('units')
        send_to_raw    = (request.form.get('send_to') or '').strip()

        # Normalize/validate inputs
        blood_type = _norm_bt(blood_type_raw)
        try:
            component = norm_component_for_db(component_in)
        except ValueError:
            return redirect(url_for('new_request'))

        try:
            units = int(units_raw or 0)
        except ValueError:
            units = 0

        if not blood_type or not component or units <= 0:
            return redirect(url_for('new_request'))

        # Emergency => level 1
        level = 1

        # Audience -> request_to
        st = send_to_raw.lower()
        if st in ('bloodbank', 'bank'):
            request_to = 'BloodBank'
        elif st == 'donor':
            request_to = 'Donor'
        elif st == 'hospital':
            request_to = 'Hospital'
        else:
            request_to = 'BloodBank'  # default

        # IDs (app-assigned)
        request_id = next_request_id(db, "hops-")
        tx_id_open = gen_transaction_id(db, current_user.org_id)

        # Insert one OPEN row (completed_at, fulfilled_by_* remain NULL)
        db.execute("""
            INSERT INTO ops.transaction_logs
                (transaction_id,
                 request_id,
                 requester_entity_type, requester_entity_id,
                 blood_type, component, level,
                 units_requested, status,
                 requested_at, completed_at,
                 request_to)
            VALUES
                (%s,
                 %s,
                 'Hospital', %s,
                 %s, %s, %s,
                 %s, 'OPEN',
                 NOW(), NULL,
                 %s);
        """, (
            tx_id_open,
            request_id,
            current_user.org_id,
            blood_type,
            component,
            level,
            int(units),
            request_to
        ))

        db.commit()
        return redirect(url_for('dashboard'))

    # GET → show Hospital request form (with Emergency/Blood Drive choices)
    return render_template('request_form.html', org=current_user.org_id)




@app.route('/register', methods=['GET', 'POST'])
def register():
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username_in = (request.form.get('username') or '').strip()
        password_in = (request.form.get('password') or '')
        if not username_in or not password_in:
            return redirect(url_for('login'))

        db = get_db()
        row = db.execute("""
            SELECT username, id, role, password
              FROM core.auth
             WHERE lower(username) = lower(%s)
             LIMIT 1;
        """, (username_in,)).fetchone()

        from argon2 import PasswordHasher
        from argon2.exceptions import VerifyMismatchError
        _ph = PasswordHasher()

        def verify_password(plain_text: str, stored_value: str) -> bool:
            if not stored_value:
                return False
            try:
                return _ph.verify(stored_value, plain_text)
            except VerifyMismatchError:
                return False
            except Exception:
                return False

        if row and verify_password(password_in, row['password']):
            role = row["role"]
            if role in ("Hospital", "BloodBank"):
                user_obj = User(row['username'], row['username'], role, org_id=row["id"])
            elif role == "Donor":
                user_obj = User(row['username'], row['username'], role, donor_id=row["id"])
            else:
                user_obj = User(row['username'], row['username'], role)
            login_user(user_obj)
            return redirect(url_for('dashboard'))
        else:
            return redirect(url_for('login'))

    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    db = get_db()


    
    # -------- DONOR DASHBOARD --------
    if current_user.role == "Donor":
        donor = db.execute("""
            SELECT
              d.donor_id         AS "DonorId",
              d.firstname        AS "FirstName",
              d.lastname         AS "LastName",
              UPPER(d.bloodtype) AS "BloodType",
              d.age              AS "Age",
              d.gender           AS "Gender",
              d.city             AS "City",
              d.state            AS "State",
              d.level            AS "Level"
            FROM core.donors d
            WHERE d.donor_id = %s
        """, (current_user.donor_id,)).fetchone()

        requests_rows = []

        # Emergencies (level 1) — visible to donors with level 1 or 3; must match donor blood type
        if donor and int(donor["Level"]) in (1, 3):
            emergency_rows = db.execute("""
                SELECT
                  t.request_id,
                  o.name,
                  o.city,
                  o.state,
                  o.org_type,
                  UPPER(t.blood_type)  AS blood_type,
                  UPPER(t.component)   AS component,
                  COALESCE(t.units_requested, t.units_fulfilled) AS units,
                  t.level,
                  t.status,
                  COALESCE(t.completed_at, t.requested_at) AS ts,
                  NULL::text AS drive_location
                FROM ops.transaction_logs t
                JOIN core.organizations o ON o.org_id = t.requester_entity_id
                WHERE t.request_to = 'Donor'
                  AND t.status = 'OPEN'
                  AND t.level = 1
                  AND UPPER(t.blood_type) = UPPER(%s)
                  AND NOT EXISTS (
                        SELECT 1 FROM ops.transaction_logs td
                        WHERE td.request_id = t.request_id
                          AND td.fulfilled_by_entity_type = 'Donor'
                          AND td.fulfilled_by_entity_id   = %s
                          AND td.status = 'REJECTED'
                  )
                ORDER BY COALESCE(t.completed_at, t.requested_at) DESC;
            """, (donor["BloodType"], current_user.donor_id)).fetchall()
            requests_rows.extend(emergency_rows)

        # Blood Drives (level 2) — visible to donors with level 2 or 3; no blood-type restriction
        if donor and int(donor["Level"]) in (2, 3):
            drive_rows = db.execute("""
                SELECT
                  t.request_id,
                  o.name,
                  o.city,
                  o.state,
                  o.org_type,
                  NULL::text            AS blood_type,
                  NULL::text            AS component,
                  NULL::int             AS units,
                  t.level,
                  t.status,
                  COALESCE(t.completed_at, t.requested_at) AS ts,
                  COALESCE(NULLIF(trim(t.notes), ''), 'Blood drive') AS drive_location
                FROM ops.transaction_logs t
                JOIN core.organizations o ON o.org_id = t.requester_entity_id
                WHERE t.request_to = 'Donor'
                  AND t.status = 'OPEN'
                  AND t.level = 2
                  AND NOT EXISTS (
                        SELECT 1 FROM ops.transaction_logs td
                        WHERE td.request_id = t.request_id
                          AND td.fulfilled_by_entity_type = 'Donor'
                          AND td.fulfilled_by_entity_id   = %s
                          AND td.status = 'REJECTED'
                  )
                ORDER BY COALESCE(t.completed_at, t.requested_at) DESC;
            """, (current_user.donor_id,)).fetchall()
            requests_rows.extend(drive_rows)

        # Sort combined rows by most recent timestamp
        requests_rows = sorted(requests_rows, key=lambda r: r.get("ts"), reverse=True)


        return render_template("donor_dashboard.html",
                               donor=donor,
                               requests_rows=requests_rows)


    # ---- ORG DASHBOARD ----
    org = db.execute("""
        SELECT org_id, org_type, name, address, city, state, zip, phone, email
          FROM core.organizations
         WHERE org_id = %s;
    """, (current_user.org_id,)).fetchone()

    q          = (request.args.get('q') or '').strip()
    blood_type = _norm_bt(request.args.get('blood_type') or '')
    component_in  = (request.args.get('component') or '')

    component = None
    if component_in:
        try:
            component = norm_component_for_db(component_in)
        except ValueError:
            component = None

    filters = {"q": q.upper(), "blood_type": blood_type, "component": (component or '').upper()}

    where_clauses = ["i.org_id = %s"]
    params = [current_user.org_id]

    if blood_type:
        where_clauses.append("i.blood_type = %s")
        params.append(blood_type)
    if component:
        where_clauses.append("i.component = %s")
        params.append(component)
    if q:
        where_clauses.append("(UPPER(i.blood_type) LIKE %s OR UPPER(i.component) LIKE %s)")
        like = f"%{q.upper()}%"
        params.extend([like, like])

    sql = f"""
        WITH latest AS (
            SELECT DISTINCT ON (i.blood_type, i.component)
                   i.org_id, i.blood_type, i.component, i.units, i.updated_at
              FROM ops.inventory i
             WHERE {" AND ".join(where_clauses)}
             ORDER BY i.blood_type, i.component, i.updated_at DESC
        )
        SELECT org_id,
               UPPER(blood_type) AS blood_type,
               UPPER(component)  AS component,
               COALESCE(units,0) AS units,
               COALESCE(updated_at::text,'') AS updated_at
          FROM latest
         ORDER BY blood_type, component;
    """
    inventory_rows = db.execute(sql, tuple(params)).fetchall()

    return render_template(
        'org_dashboard.html',
        org=org,
        role=current_user.role,
        inventory=inventory_rows,
        filters=filters
    )

@app.route('/requests')
@login_required
def view_requests():
    db = get_db()

    q          = (request.args.get('q') or '').strip()
    blood_type = _norm_bt((request.args.get('blood_type') or ''))
    component_in  = (request.args.get('component') or '')
    status     = (request.args.get('status') or '').strip().upper()

    component = None
    if component_in:
        try:
            component = norm_component_for_db(component_in)
        except ValueError:
            component = None

    filters = {
        "q": q.upper(),
        "blood_type": blood_type,
        "component": (component or '').upper(),
        "status": status,
    }

    # My org's own requests
    req_type = 'Hospital' if current_user.role == 'Hospital' else 'BloodBank'

    where_my = [
        "t.requester_entity_type = %s",
        "t.requester_entity_id = %s"
    ]
    params_my = [req_type, current_user.org_id]

    if blood_type:
        where_my.append("t.blood_type = %s")
        params_my.append(blood_type)
    if component:
        where_my.append("t.component = %s")
        params_my.append(component)
    if status:
        where_my.append("t.status = %s")
        params_my.append(status)
    if q:
        where_my.append("(UPPER(t.blood_type) LIKE %s OR UPPER(t.component) LIKE %s)")
        like = f"%{q.upper()}%"
        params_my.extend([like, like])

    my_sql = f"""
        WITH latest AS (
            SELECT DISTINCT ON (t.request_id)
                   t.request_id,
                   t.requester_entity_id AS org_id,
                   t.blood_type, t.component, t.level,
                   COALESCE(t.units_requested, t.units_fulfilled) AS units,
                   t.status,
                   COALESCE(t.completed_at, t.requested_at) AS ts
              FROM ops.transaction_logs t
             WHERE {" AND ".join(where_my)}
             ORDER BY t.request_id, COALESCE(t.completed_at, t.requested_at) DESC
        )
        SELECT request_id, org_id, blood_type, component, units,
               level, status, ts AS created_at,
               NULL::text AS accepted_by_bank_id,
               NULL::text AS decision_note,
               NULL::timestamp AS decision_at
          FROM latest
         ORDER BY ts DESC;
    """
    my_requests = db.execute(my_sql, tuple(params_my)).fetchall()

    fulfilled = []

    # BANK view
    if current_user.role == "BloodBank":
        # Base filters: show only hospital-origin, OPEN requests, sent to banks
        where_all = [
            "t.requester_entity_type = 'Hospital'",
            "t.status = 'OPEN'",
            "t.request_to = 'BloodBank'",
            # Hide requests this specific bank has already REJECTED
            """
            NOT EXISTS (
            SELECT 1
                FROM ops.transaction_logs t2
            WHERE t2.request_id = t.request_id
                AND t2.fulfilled_by_entity_type = 'BloodBank'
                AND t2.fulfilled_by_entity_id   = %s
                AND t2.status = 'REJECTED'
            )
            """
        ]
        params_all = [current_user.org_id]

        # Optional filters from the UI
        if blood_type:
            where_all.append("t.blood_type = %s")
            params_all.append(blood_type)

        if component:
            where_all.append("t.component = %s")
            params_all.append(component)

        # (We force OPEN already; ignore a conflicting status filter)
        if status and status != 'OPEN':
            where_all.append("t.status = %s")
            params_all.append(status)

        if q:
            where_all.append(
                "(UPPER(o.name) LIKE %s OR UPPER(o.city) LIKE %s OR UPPER(o.state) LIKE %s "
                "OR UPPER(t.blood_type) LIKE %s OR UPPER(t.component) LIKE %s)"
            )
            like = f"%{q.upper()}%"
            params_all.extend([like, like, like, like, like])

        all_sql = f"""
            WITH latest AS (
                SELECT DISTINCT ON (t.request_id)
                    t.request_id,
                    t.requester_entity_id AS hospital_id,
                    t.blood_type, t.component, t.level,
                    COALESCE(t.units_requested, t.units_fulfilled) AS units,
                    t.status,
                    COALESCE(t.completed_at, t.requested_at) AS ts
                FROM ops.transaction_logs t
                WHERE {" AND ".join(where_all)}
                ORDER BY t.request_id, COALESCE(t.completed_at, t.requested_at) DESC
            )
            SELECT l.request_id,
                l.hospital_id,
                l.blood_type, l.component, l.units,
                l.level, l.status, l.ts AS created_at,
                NULL::text AS accepted_by_bank_id,
                NULL::text AS decision_note,
                NULL::timestamp AS decision_at,
                o.name  AS hospital_name,
                o.city, o.state
            FROM latest l
            JOIN core.organizations o ON o.org_id = l.hospital_id
            ORDER BY l.ts DESC;
        """

        all_hospital_requests = db.execute(all_sql, tuple(params_all)).fetchall()

        fulfilled = db.execute("""
            SELECT t.transaction_id, t.request_id,
                t.requester_entity_id AS hospital,
                t.blood_type, t.component, t.units_fulfilled,
                t.level, t.completed_at AS fulfilled_at
            FROM ops.transaction_logs t
            WHERE t.fulfilled_by_entity_id = %s
            ORDER BY t.completed_at DESC NULLS LAST;
        """, (current_user.org_id,)).fetchall()

        return render_template(
            'requests.html',
            org=current_user.org_id,
            role=current_user.role,
            my_requests=my_requests,
            requests=my_requests,
            all_hospital_requests=all_hospital_requests,
            fulfilled=fulfilled,
            filters=filters
        )



        # Non-BANK users
    return render_template(
        'requests.html',
        org=current_user.org_id,
        role=current_user.role,
        my_requests=my_requests,
        requests=my_requests,
        fulfilled=fulfilled,
        filters=filters
        )


# ------------------------------------------------------------
# DONOR: ACCEPT  (no partials; closes the oldest OPEN donor row)
# ------------------------------------------------------------
from flask import jsonify

@app.route('/donor/accept/<string:request_id>', methods=['POST'])
@login_required
@role_required('Donor')
def donor_accept_request(request_id):
    """
    Close exactly one OPEN donor-targeted row for this request_id.
    Returns:
      200 JSON {ok:true} on success
      409 JSON {ok:false, error:"reason"} when nothing to accept
      500 JSON {ok:false, error:"exception"} on server/SQL error
    """
    db = get_db()
    try:
        # Find & lock the oldest OPEN donor-visible row
        picked = db.execute("""
            SELECT transaction_id
            FROM ops.transaction_logs
            WHERE request_id = %s
              AND status = 'OPEN'
              AND request_to = 'Donor'
            ORDER BY requested_at ASC, transaction_id ASC
            FOR UPDATE SKIP LOCKED
            LIMIT 1;
        """, (request_id,)).fetchone()

        if not picked:
            db.rollback()
            # Nothing to accept (already taken/closed or misrouted)
            return jsonify(ok=False, error="No OPEN donor row found for this request."), 409

        updated = db.execute("""
            UPDATE ops.transaction_logs
               SET fulfilled_by_entity_type = 'Donor',
                   fulfilled_by_entity_id   = %s,
                   units_fulfilled          = 1,   -- donors count as 1
                   status                   = 'FULFILLED',
                   completed_at             = NOW()
             WHERE transaction_id = %s
             RETURNING transaction_id;
        """, (current_user.donor_id, picked["transaction_id"])).fetchone()

        if not updated:
            db.rollback()
            return jsonify(ok=False, error="Update failed (no rows changed)."), 500

        db.commit()
        return jsonify(ok=True), 200

    except Exception as e:
        app.logger.exception("donor_accept_request failed")
        db.rollback()
        return jsonify(ok=False, error=str(e)), 500




# -------------------------------------------------------------------
# DONOR: REJECT  (per-donor event; others still see the OPEN request)
# -------------------------------------------------------------------
@app.route('/donor/reject/<string:request_id>', methods=['POST'])
@login_required
@role_required('Donor')
def donor_reject_request(request_id):
    db = get_db()
    try:
        already = db.execute("""
            SELECT 1
              FROM ops.transaction_logs
             WHERE request_id = %s
               AND fulfilled_by_entity_type = 'Donor'
               AND fulfilled_by_entity_id   = %s
               AND status IN ('REJECTED','FULFILLED')
             LIMIT 1
        """, (request_id, current_user.donor_id)).fetchone()
        if already:
            return redirect(url_for('dashboard'))

        base = db.execute("""
            SELECT MIN(requested_at)             AS earliest,
                   MAX(requester_entity_id)      AS hospital_id,
                   MAX(blood_type)               AS blood_type,
                   MAX(component)                AS component,
                   MAX(level)                    AS level
              FROM ops.transaction_logs
             WHERE request_id = %s
        """, (request_id,)).fetchone()
        if not base or not base["earliest"]:
            return redirect(url_for('dashboard'))

        tx_id = gen_transaction_id(db, current_user.donor_id)
        db.execute("""
            INSERT INTO ops.transaction_logs(
                transaction_id, request_id,
                requester_entity_type, requester_entity_id,
                fulfilled_by_entity_type, fulfilled_by_entity_id,
                blood_type, component, level,
                units_fulfilled, status,
                requested_at, completed_at, notes
            )
            VALUES(
                %s, %s,
                'Hospital', %s,
                'Donor', %s,
                %s, %s, %s,
                0, 'REJECTED',
                %s, NOW(), 'Donor rejected'
            );
        """, (
            tx_id, request_id,
            base["hospital_id"], current_user.donor_id,
            _norm_bt(base["blood_type"]), norm_component_for_db(base["component"]), base["level"],
            base["earliest"]
        ))
        db.commit()
    except Exception:
        db.rollback()
    return redirect(url_for('dashboard'))



from flask import flash

@app.route('/blooddrive/new', methods=['GET', 'POST'])
@login_required
def new_blood_drive():
    """
    Create a Blood Drive announcement (Hospital or BloodBank).
    Writes ONE OPEN row into ops.transaction_logs with:
      - requester_entity_type = current user's role ("Hospital" | "BloodBank")
      - requester_entity_id   = current user's org_id
      - level                 = 2 (blood drive)
      - request_to            = 'Donor'
      - requested_at          = <chosen date> at midnight (server tz)
      - notes                 = location text
    All other request fields remain NULL.
    """
    role = (current_user.role or "").strip()
    if role not in ("Hospital", "BloodBank"):
        return redirect(url_for('dashboard'))

    db = get_db()

    if request.method == 'POST':
        drive_date_raw = (request.form.get('drive_date') or '').strip()   # "YYYY-MM-DD"
        location_raw   = (request.form.get('location') or '').strip()
        if not drive_date_raw:
            return redirect(url_for('new_blood_drive'))

        requester_type = role                                  # "Hospital" or "BloodBank"
        requester_id   = current_user.org_id
        request_to_val = 'Donor'
        prefix         = "hops-" if role == "Hospital" else "bank-"
        request_id     = next_request_id(db, prefix)
        tx_id_open     = gen_transaction_id(db, requester_id)

        try:
            db.execute("""
                INSERT INTO ops.transaction_logs (
                    transaction_id,
                    request_id,
                    requester_entity_type, requester_entity_id,
                    blood_type, component, level,
                    units_requested, units_fulfilled,
                    status,
                    requested_at, completed_at,
                    fulfilled_by_entity_type, fulfilled_by_entity_id,
                    notes, inventory_updated,
                    request_to
                )
                VALUES (
                    %s,                     -- transaction_id
                    %s,                     -- request_id
                    %s, %s,                 -- requester_entity_type, requester_entity_id
                    NULL, NULL, 2,          -- blood_type, component, level=2
                    NULL, NULL,             -- units_requested, units_fulfilled
                    'OPEN',                 -- status
                    (%s::date)::timestamptz,-- requested_at (midnight)
                    NULL,                   -- completed_at
                    NULL, NULL,             -- fulfilled_by_entity_type, fulfilled_by_entity_id
                    %s,                     -- notes (location)
                    NULL,                   -- inventory_updated
                    %s                      -- request_to
                );
            """, (
                tx_id_open,
                request_id,
                requester_type, requester_id,
                drive_date_raw,
                location_raw,
                request_to_val
            ))
            db.commit()
            return redirect(url_for('dashboard'))
        except Exception:
            db.rollback()
            return redirect(url_for('new_blood_drive'))

    # GET
    return render_template('blood_drive_form.html', org=current_user.org_id)





# ---------------------------
# BANK actions (partial-aware with timestamp rules)
# ---------------------------
@app.route('/requests/accept/<string:request_id>', methods=['POST'])
@login_required
@role_required('BloodBank')
def accept_request(request_id):
    db = get_db()

    # 1) Find the **oldest** OPEN row for this request_id
    open_row = db.execute("""
        SELECT transaction_id,
            requester_entity_id AS hospital_id,
            blood_type, component, level,
            units_requested, requested_at,
            request_to               -- << add this
        FROM ops.transaction_logs
        WHERE request_id = %s
        AND status      = 'OPEN'
        ORDER BY requested_at ASC, transaction_id ASC
        LIMIT 1;
    """, (request_id,)).fetchone()

    # Always check existence first
    if not open_row:
        return redirect(url_for('view_requests'))

    # Only allow banks to accept requests that were sent to BloodBank
    if (open_row.get("request_to") or '') != 'BloodBank':
        return redirect(url_for('view_requests'))


    bank_id   = current_user.org_id
    bt        = _norm_bt(open_row["blood_type"])
    comp      = norm_component_for_db(open_row["component"])
    level     = open_row["level"]
    need      = int(open_row["units_requested"] or 0)
    original_requested_at = open_row["requested_at"]
    tx_open   = open_row["transaction_id"]

    # If the form lets a bank choose how many to provide, honor it; else default to "as much as possible".
    try:
        requested_fill = int(request.form.get("units") or need)
    except ValueError:
        requested_fill = need

    # 2) Bank capacity
    available = get_bank_stock(db, bank_id, bt, comp)
    if available <= 0:
        return redirect(url_for('view_requests'))

    give = max(1, min(need, requested_fill, available))

    try:
        # 3) Reduce ONLY the bank's inventory (the hospital inventory is not touched here)
        upsert_inventory(db, bank_id, bt, comp, -give)

        # 4) Full vs Partial
        if give == need:
            # FULL → update the existing OPEN row to FULFILLED (no new row)
            db.execute("""
                UPDATE ops.transaction_logs
                   SET fulfilled_by_entity_type = 'BloodBank',
                       fulfilled_by_entity_id   = %s,
                       units_fulfilled          = %s,
                       status                   = 'FULFILLED',
                       completed_at             = NOW()
                 WHERE transaction_id = %s;
            """, (bank_id, give, tx_open))

        else:
            # PARTIAL:
            # a) mark current OPEN row as FULFILLED for the accepted portion
            db.execute("""
                UPDATE ops.transaction_logs
                   SET fulfilled_by_entity_type = 'BloodBank',
                       fulfilled_by_entity_id   = %s,
                       units_fulfilled          = %s,
                       status                   = 'FULFILLED',
                       completed_at             = NOW()
                 WHERE transaction_id = %s;
            """, (bank_id, give, tx_open))

            # b) insert a NEW OPEN row with the remainder, same request_id & requested_at
            remainder = need - give
            tx_new    = gen_transaction_id(db, open_row["hospital_id"])
            db.execute("""
                INSERT INTO ops.transaction_logs
                    (transaction_id,
                    request_id,
                    requester_entity_type, requester_entity_id,
                    blood_type, component, level,
                    units_requested, status,
                    requested_at, completed_at,
                    fulfilled_by_entity_type, fulfilled_by_entity_id, units_fulfilled,
                    request_to)  -- << add this column
                VALUES
                    (%s,
                    %s,
                    'Hospital', %s,
                    %s, %s, %s,
                    %s, 'OPEN',
                    %s, NULL,
                    NULL, NULL, NULL,
                    %s);          -- << and this value
            """, (tx_new, request_id, open_row["hospital_id"],
                bt, comp, level,
                remainder,
                original_requested_at,
                open_row.get("request_to")))   # carry the audience forward


        db.commit()
    except Exception:
        db.rollback()

    return redirect(url_for('view_requests'))



@app.route('/requests/reject/<string:request_id>', methods=['POST'])
@login_required
@role_required('BloodBank')
def reject_request(request_id):
    db = get_db()

    # Find the OLDEST OPEN row for this request (preserves original requested_at)
    open_row = db.execute("""
        SELECT transaction_id,
            requester_entity_id AS hospital_id,
            blood_type, component, level,
            requested_at,
            request_to               -- << add this
        FROM ops.transaction_logs
        WHERE request_id = %s
        AND status      = 'OPEN'
        ORDER BY requested_at ASC, transaction_id ASC
        LIMIT 1;
    """, (request_id,)).fetchone()

    # Check existence first
    if not open_row:
        # Nothing left to reject
        return redirect(url_for('view_requests'))

    # Only allow banks to reject requests that were sent to BloodBank
    if (open_row.get("request_to") or '') != 'BloodBank':
        return redirect(url_for('view_requests'))



    bank_id = current_user.org_id
    note = (request.form.get("note") or "").strip()

    try:
        # Has THIS bank already recorded a decision (rejected or fulfilled)?
        already = db.execute("""
            SELECT 1
              FROM ops.transaction_logs
             WHERE request_id = %s
               AND fulfilled_by_entity_type = 'BloodBank'
               AND fulfilled_by_entity_id   = %s
               AND status IN ('REJECTED','FULFILLED')
             LIMIT 1;
        """, (request_id, bank_id)).fetchone()

        if not already:
            # Record THIS bank's rejection as its own event.
            tx_id = gen_transaction_id(db, bank_id)
            db.execute("""
                INSERT INTO ops.transaction_logs
                    (transaction_id,
                     request_id,
                     requester_entity_type, requester_entity_id,
                     fulfilled_by_entity_type, fulfilled_by_entity_id,
                     blood_type, component, level,
                     units_fulfilled, status,
                     requested_at, completed_at, notes)
                VALUES
                    (%s,
                     %s,
                     'Hospital', %s,
                     'BloodBank', %s,
                     %s, %s, %s,
                     0, 'REJECTED',
                     %s, NOW(), %s);
            """, (tx_id, request_id,
                  open_row["hospital_id"], bank_id,
                  _norm_bt(open_row["blood_type"]),
                  norm_component_for_db(open_row["component"]),
                  open_row["level"],
                  open_row["requested_at"],
                  note))

        # Have **all** banks rejected?
        total_banks = db.execute("""
            SELECT COUNT(*) AS c
              FROM core.organizations
             WHERE org_type = 'BloodBank'
               -- AND COALESCE(active, TRUE) = TRUE   -- uncomment if you have an 'active' flag
        """).fetchone()["c"]

        rejected_banks = db.execute("""
            SELECT COUNT(DISTINCT fulfilled_by_entity_id) AS c
              FROM ops.transaction_logs
             WHERE request_id = %s
               AND status = 'REJECTED'
               AND fulfilled_by_entity_type = 'BloodBank';
        """, (request_id,)).fetchone()["c"]

        # If every bank has rejected, globally close the OPEN row.
        if int(rejected_banks) >= int(total_banks):
            db.execute("""
                UPDATE ops.transaction_logs
                   SET status       = 'REJECTED',
                       completed_at = NOW()
                 WHERE transaction_id = %s
                   AND status = 'OPEN';
            """, (open_row["transaction_id"],))

        db.commit()
    except Exception:
        db.rollback()

    return redirect(url_for('view_requests'))


# -----------------------------
# App entry
# -----------------------------
def first_run_bootstrap():
    try:
        with get_db() as conn:
            conn.execute("SELECT 1;")
    except Exception as e:
        raise RuntimeError(f"Database connection failed: {e}")

if __name__ == '__main__':
    first_run_bootstrap()
    app.run(
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "5000")),
        debug=os.getenv("FLASK_DEBUG", "1") == "1"
    )
