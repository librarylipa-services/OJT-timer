import io
import os
import re
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import quote

from flask import Flask, g, jsonify, render_template, request, send_file, session
from flask.json.provider import DefaultJSONProvider
from werkzeug.exceptions import HTTPException
from PIL import Image, ImageDraw, ImageFont
from qrcode.constants import ERROR_CORRECT_M
from werkzeug.security import check_password_hash, generate_password_hash
import qrcode
from dotenv import load_dotenv

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except Exception:  # pragma: no cover
    psycopg2 = None
    RealDictCursor = None

DB_INTEGRITY_ERRORS = (sqlite3.IntegrityError,) + (
    (psycopg2.IntegrityError,) if psycopg2 else ()
)


class ConfigError(Exception):
    """Deployment or environment misconfiguration (returned as JSON on /api)."""

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))
load_dotenv(os.path.join(BASE_DIR, ".env.local"), override=True)


def _normalize_database_url(url: str) -> str:
    """Supabase Postgres expects SSL; append sslmode if missing."""
    if not url:
        return url
    if "supabase.co" in url and "sslmode" not in url:
        return f"{url}{'&' if '?' in url else '?'}sslmode=require"
    return url


def _env_first(*keys: str) -> str:
    """Return the first non-empty env value (Vercel+Supabase uses several names)."""
    for key in keys:
        val = os.environ.get(key, "").strip()
        if val:
            return val
    return ""


SQLITE_DATABASE = os.path.join(BASE_DIR, "ojt.db")
# Vercel "Connect Supabase" often sets POSTGRES_PRISMA_URL / POSTGRES_URL instead of DATABASE_URL.
DATABASE_URL = _normalize_database_url(
    _env_first("DATABASE_URL", "POSTGRES_PRISMA_URL", "POSTGRES_URL")
)
USE_POSTGRES = bool(DATABASE_URL)
SUPABASE_URL = _env_first("SUPABASE_URL", "NEXT_PUBLIC_SUPABASE_URL")
SUPABASE_ANON_KEY = _env_first("SUPABASE_ANON_KEY", "NEXT_PUBLIC_SUPABASE_ANON_KEY")
SUPABASE_SERVICE_ROLE_KEY = _env_first(
    "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SECRET_KEY", "SUPABASE_SERVICE_ROLE"
)
SUPABASE_STORAGE_BUCKET = os.environ.get("SUPABASE_STORAGE_BUCKET", "ojt-photos").strip()

# Use Philippine local time everywhere (store timestamps as naive "local" ISO strings).
PH_TZ = ZoneInfo("Asia/Manila")


def now_ph():
    return datetime.now(PH_TZ).replace(tzinfo=None)
# ID templates live in repo-level `resources/`
ID_FRONT_TEMPLATE_PATH = os.path.join(BASE_DIR, "resources", "front_id.png")
ID_BACK_TEMPLATE_PATH = os.path.join(BASE_DIR, "resources", "back_id.png")
UPLOADS_DIR = os.path.join(BASE_DIR, "uploads")

_FRONT_REF_W = 709
_FRONT_REF_H = 1004
# Calibrated to `resources/front_id.png` (709×1004). This box is the rounded photo frame.
_FRONT_PHOTO_LEFT = 206
_FRONT_PHOTO_TOP = 294
_FRONT_PHOTO_RIGHT = 502
_FRONT_PHOTO_BOTTOM = 599

_BACK_REF_W = 709
_BACK_REF_H = 1004
# Calibrated to `resources/back_id.png` (709×1004). This is the QR square frame.
_BACK_QR_LEFT = 174
_BACK_QR_TOP = 382
_BACK_QR_RIGHT = 534
_BACK_QR_BOTTOM = 692
_BACK_QR_INSET = 0
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")
ADMIN_PASSWORD = os.environ.get("OJT_ADMIN_PASSWORD", "Melo1234")
# Bind address. Use 0.0.0.0 to allow other PCs on the network to connect.
# Override with OJT_HOST / OJT_PORT as needed.
OJT_HOST = os.environ.get("OJT_HOST", "0.0.0.0")
OJT_PORT = int(os.environ.get("OJT_PORT", "5000"))
ALLOWED_GENDERS = frozenset({"Male", "Female"})
ALLOWED_ENTRY_METHODS = frozenset({"scan", "manual"})


class _AppJSONProvider(DefaultJSONProvider):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)


app = Flask(__name__)
app.json_provider_class = _AppJSONProvider
app.config["SECRET_KEY"] = SECRET_KEY
if os.environ.get("VERCEL") == "1":
    app.config["SESSION_COOKIE_SECURE"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SUPABASE_URL"] = SUPABASE_URL
app.config["SUPABASE_ANON_KEY"] = SUPABASE_ANON_KEY
app.config["SUPABASE_STORAGE_BUCKET"] = SUPABASE_STORAGE_BUCKET


@app.context_processor
def _inject_supabase_template_vars():
    return {
        "supabase_url": SUPABASE_URL,
        "supabase_anon_key": SUPABASE_ANON_KEY,
    }


def _storage_headers():
    if not SUPABASE_URL:
        raise ConfigError("SUPABASE_URL is not set")
    if not SUPABASE_SERVICE_ROLE_KEY:
        raise ConfigError(
            "SUPABASE_SERVICE_ROLE_KEY is not set (required for Storage uploads on Vercel)."
        )
    return {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
    }


def _storage_put_object(bucket: str, object_path: str, content_type: str, body: bytes):
    # PUT /storage/v1/object/<bucket>/<path>
    url = (
        SUPABASE_URL.rstrip("/")
        + "/storage/v1/object/"
        + quote(bucket, safe="")
        + "/"
        + quote(object_path.lstrip("/"), safe="/")
    )
    headers = _storage_headers()
    headers.update(
        {
            "Content-Type": content_type,
            "x-upsert": "true",
        }
    )
    req = Request(url, data=body, headers=headers, method="PUT")
    try:
        with urlopen(req, timeout=20) as resp:
            _ = resp.read()
    except HTTPError as e:
        raise RuntimeError(f"Storage upload failed: {e.code} {e.reason}") from e
    except URLError as e:
        raise RuntimeError(f"Storage upload failed: {e.reason}") from e


def _storage_get_object_bytes(bucket: str, object_path: str) -> bytes:
    url = (
        SUPABASE_URL.rstrip("/")
        + "/storage/v1/object/"
        + quote(bucket, safe="")
        + "/"
        + quote(object_path.lstrip("/"), safe="/")
    )
    headers = _storage_headers()
    req = Request(url, headers=headers, method="GET")
    try:
        with urlopen(req, timeout=20) as resp:
            return resp.read()
    except HTTPError:
        return b""
    except URLError:
        return b""


def _image_upload_to_storage(upload, object_path: str) -> str:
    if not upload or getattr(upload, "filename", "") == "":
        return ""
    try:
        im = Image.open(upload.stream)
        im = im.convert("RGB")
    except OSError:
        raise ValueError("Invalid image file")
    max_side = 1400
    if max(im.size) > max_side:
        im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="PNG", optimize=True)
    body = buf.getvalue()
    _storage_put_object(SUPABASE_STORAGE_BUCKET, object_path, "image/png", body)
    return object_path


def _save_user_profile_photo(upload, sr_code: str, *, slot: str) -> str:
    """
    Profile image for ojt_users.photo_filename / extra_photo_filename.
    With Postgres (Vercel + Supabase), files go to Storage so they survive deploys.
    Locally (SQLite), files go under UPLOADS_DIR.
    slot must be 'photo' or 'extra'.
    """
    if not upload or getattr(upload, "filename", "") == "":
        return ""
    if slot not in ("photo", "extra"):
        raise ValueError("Invalid photo slot")
    stem = _safe_filename_stem(sr_code)
    if USE_POSTGRES:
        object_path = f"users/{stem}/photo.png" if slot == "photo" else f"users/{stem}/extra.png"
        return _image_upload_to_storage(upload, object_path)
    os.makedirs(UPLOADS_DIR, exist_ok=True)
    try:
        im = Image.open(upload.stream)
        im = im.convert("RGB")
    except OSError:
        raise ValueError("Invalid image file")
    max_side = 1400
    if max(im.size) > max_side:
        im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    prefix = f"{sr_code}_photo" if slot == "photo" else f"{sr_code}_extra"
    out_name = f"{_safe_filename_stem(prefix)}.png"
    out_path = os.path.join(UPLOADS_DIR, out_name)
    im.save(out_path, format="PNG", optimize=True)
    return out_name


@app.errorhandler(ConfigError)
def _handle_config_error(e):
    return jsonify({"error": str(e)}), 500


if psycopg2:

    @app.errorhandler(psycopg2.OperationalError)
    @app.errorhandler(psycopg2.InterfaceError)
    def _handle_pg_connect_errors(e):
        if request.path.startswith("/api/"):
            return jsonify({"error": "Database connection failed", "detail": str(e)}), 503
        raise e


@app.errorhandler(Exception)
def _handle_unexpected_exception(e):
    # Ensure API callers don't receive HTML error pages.
    if isinstance(e, HTTPException):
        return e
    if request.path.startswith("/api/"):
        return jsonify({"error": "Internal server error", "detail": str(e)}), 500
    raise e


@app.get("/api/health/db")
def api_health_db():
    """
    Debug endpoint: verifies DB connectivity and basic table presence.
    Always returns JSON so Vercel issues are visible via Network tab.
    """
    try:
        db = get_db()
        cur = db.cursor()
        if USE_POSTGRES:
            cur.execute(
                """
                SELECT table_name AS t
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name IN ('batches', 'ojt_users', 'time_entries')
                ORDER BY table_name
                """
            )
            tables = [r["t"] for r in cur.fetchall()]
        else:
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('batches','ojt_users','time_entries')"
            )
            tables = [r["name"] for r in cur.fetchall()]
        return jsonify(
            {
                "ok": True,
                "dialect": ("postgres" if USE_POSTGRES else "sqlite"),
                "tables": tables,
            }
        )
    except Exception as e:  # broad on purpose for remote debugging
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "health_check_failed",
                    "detail": str(e),
                    "dialect": ("postgres" if USE_POSTGRES else "sqlite"),
                }
            ),
            500,
        )


class _DBCursor:
    def __init__(self, cur, dialect):
        self._cur = cur
        self._dialect = dialect

    def execute(self, query, params=None):
        if self._dialect == "postgres":
            query = query.replace("?", "%s")
        if params is None:
            return self._cur.execute(query)
        return self._cur.execute(query, params)

    def executemany(self, query, seq):
        if self._dialect == "postgres":
            query = query.replace("?", "%s")
        return self._cur.executemany(query, seq)

    def __getattr__(self, name):
        return getattr(self._cur, name)


class _DBConn:
    def __init__(self, conn, dialect):
        self._conn = conn
        self._dialect = dialect

    def cursor(self):
        return _DBCursor(self._conn.cursor(), self._dialect)

    def commit(self):
        return self._conn.commit()

    def close(self):
        return self._conn.close()

    def __getattr__(self, name):
        return getattr(self._conn, name)


def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        if os.environ.get("VERCEL") == "1" and not USE_POSTGRES:
            raise ConfigError(
                "No Postgres URL on Vercel. SQLite and local files are wiped on each deploy; "
                "set DATABASE_URL (or POSTGRES_PRISMA_URL / POSTGRES_URL from Vercel’s Supabase "
                "integration). Local .env.local is not deployed."
            )
        if USE_POSTGRES:
            if psycopg2 is None or RealDictCursor is None:
                raise RuntimeError("psycopg2 is required when DATABASE_URL is set")
            conn = psycopg2.connect(
                DATABASE_URL,
                cursor_factory=RealDictCursor,
                connect_timeout=10,
            )
            db = g._database = _DBConn(conn, "postgres")
        else:
            conn = sqlite3.connect(SQLITE_DATABASE)
            conn.row_factory = sqlite3.Row
            db = g._database = _DBConn(conn, "sqlite")
    return db


@app.teardown_appcontext
def close_db(_exc):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()


def init_db():
    if USE_POSTGRES:
        # Supabase Postgres schema is managed via SQL migrations (see `supabase/schema.sql`).
        return
    db = sqlite3.connect(SQLITE_DATABASE)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ojt_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sr_code TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            gender TEXT NOT NULL,
            department TEXT NOT NULL,
            course TEXT NOT NULL,
            batch_id INTEGER NOT NULL REFERENCES batches(id),
            required_hours REAL NOT NULL,
            password_hash TEXT NOT NULL,
            photo_filename TEXT NOT NULL DEFAULT '',
            extra_photo_filename TEXT NOT NULL DEFAULT '',
            goal_text TEXT NOT NULL DEFAULT '',
            accomplishment_text TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS time_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES ojt_users(id),
            time_in TEXT NOT NULL,
            time_out TEXT,
            session_note TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_ojt_users_batch ON ojt_users(batch_id);
        CREATE INDEX IF NOT EXISTS idx_time_entries_user ON time_entries(user_id);
        """
    )
    db.commit()
    db.close()
    migrate_ojt_users_sr_code()
    ensure_ojt_user_indexes()
    migrate_ojt_users_photos()
    migrate_time_entries_session_note()
    migrate_time_entries_methods()


def migrate_ojt_users_sr_code():
    """Add sr_code and copy from legacy qr_token for existing databases."""
    if USE_POSTGRES:
        return
    db = sqlite3.connect(SQLITE_DATABASE)
    cur = db.cursor()
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ojt_users'"
    )
    if not cur.fetchone():
        db.close()
        return
    cur.execute("PRAGMA table_info(ojt_users)")
    col_names = [r[1] for r in cur.fetchall()]
    if "sr_code" not in col_names:
        cur.execute("ALTER TABLE ojt_users ADD COLUMN sr_code TEXT")
        db.commit()
    if "qr_token" in col_names:
        cur.execute(
            "UPDATE ojt_users SET sr_code = qr_token WHERE sr_code IS NULL OR sr_code = ''"
        )
        db.commit()
        try:
            cur.execute("ALTER TABLE ojt_users DROP COLUMN qr_token")
            db.commit()
        except sqlite3.OperationalError:
            pass
    db.close()


def _ojt_user_columns(cur):
    if USE_POSTGRES:
        cur.execute(
            """
            SELECT column_name AS cn
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'ojt_users'
            """
        )
        return {r["cn"] for r in cur.fetchall()}
    cur.execute("PRAGMA table_info(ojt_users)")
    return {r[1] for r in cur.fetchall()}


def ensure_ojt_user_indexes():
    if USE_POSTGRES:
        return
    db = sqlite3.connect(SQLITE_DATABASE)
    cur = db.cursor()
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ojt_users'"
    )
    if not cur.fetchone():
        db.close()
        return
    cur.execute("PRAGMA table_info(ojt_users)")
    if "sr_code" in {r[1] for r in cur.fetchall()}:
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_ojt_users_sr ON ojt_users(sr_code)"
        )
        db.commit()
    db.close()


def migrate_ojt_users_photos():
    """Add photo filename columns for ID generation."""
    if USE_POSTGRES:
        return
    db = sqlite3.connect(SQLITE_DATABASE)
    cur = db.cursor()
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ojt_users'"
    )
    if not cur.fetchone():
        db.close()
        return
    cur.execute("PRAGMA table_info(ojt_users)")
    cols = {r[1] for r in cur.fetchall()}
    if "photo_filename" not in cols:
        cur.execute(
            "ALTER TABLE ojt_users ADD COLUMN photo_filename TEXT NOT NULL DEFAULT ''"
        )
        db.commit()
    if "extra_photo_filename" not in cols:
        cur.execute(
            "ALTER TABLE ojt_users ADD COLUMN extra_photo_filename TEXT NOT NULL DEFAULT ''"
        )
        db.commit()
    db.close()


def migrate_time_entries_session_note():
    if USE_POSTGRES:
        return
    db = sqlite3.connect(SQLITE_DATABASE)
    cur = db.cursor()
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='time_entries'"
    )
    if not cur.fetchone():
        db.close()
        return
    cur.execute("PRAGMA table_info(time_entries)")
    cols = {r[1] for r in cur.fetchall()}
    if "session_note" not in cols:
        cur.execute(
            "ALTER TABLE time_entries ADD COLUMN session_note TEXT NOT NULL DEFAULT ''"
        )
        db.commit()
    db.close()


def migrate_time_entries_methods():
    """Store whether time in / time out used camera scan vs manual SR-Code entry."""
    if USE_POSTGRES:
        return
    db = sqlite3.connect(SQLITE_DATABASE)
    cur = db.cursor()
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='time_entries'"
    )
    if not cur.fetchone():
        db.close()
        return
    cur.execute("PRAGMA table_info(time_entries)")
    cols = {r[1] for r in cur.fetchall()}
    if "time_in_method" not in cols:
        cur.execute("ALTER TABLE time_entries ADD COLUMN time_in_method TEXT")
        db.commit()
    if "time_out_method" not in cols:
        cur.execute("ALTER TABLE time_entries ADD COLUMN time_out_method TEXT")
        db.commit()
    db.close()


def entry_method_label(value):
    if value == "scan":
        return "Camera"
    if value == "manual":
        return "Manual"
    return "—"


def parse_dt(value):
    if not value:
        return None
    return datetime.fromisoformat(value)


def seconds_to_hm(total_seconds):
    if total_seconds < 0:
        total_seconds = 0
    total_seconds = int(total_seconds)
    h = total_seconds // 3600
    m = (total_seconds % 3600) // 60
    return h, m, f"{h}h {m}m"


def round_time_out(dt):
    """Round time-out up to next full hour when minutes >= 40."""
    if not dt:
        return dt
    if dt.minute < 40:
        return dt
    base = dt.replace(minute=0, second=0, microsecond=0)
    return base + timedelta(hours=1)


def entry_duration_seconds(time_in, time_out, now=None):
    now = now or now_ph()
    tin = parse_dt(time_in)
    if not tin:
        return 0
    if time_out:
        tout = parse_dt(time_out)
        if not tout:
            return 0
        dur = (tout - tin).total_seconds()
        # Deduct fixed break time for completed same-day shifts.
        if dur > 0 and tin.date() == tout.date():
            dur -= 3600
        return max(0, dur)
    return max(0, (now - tin).total_seconds())


def sum_logged_seconds_for_user(cur, user_id, now=None):
    now = now or now_ph()
    cur.execute(
        "SELECT time_in, time_out FROM time_entries WHERE user_id = ? ORDER BY time_in",
        (user_id,),
    )
    total = 0.0
    for row in cur.fetchall():
        total += entry_duration_seconds(row["time_in"], row["time_out"], now)
    return total


def get_open_entry(cur, user_id):
    cur.execute(
        """
        SELECT id, time_in, time_out, time_in_method, time_out_method
        FROM time_entries
        WHERE user_id = ? AND time_out IS NULL
        ORDER BY time_in DESC LIMIT 1
        """,
        (user_id,),
    )
    return cur.fetchone()


def user_row_to_dict(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "gender": row["gender"],
        "department": row["department"],
        "course": row["course"],
        "batch_id": row["batch_id"],
        "required_hours": row["required_hours"],
    }


@app.route("/")
def page_home():
    return render_template("home.html")


@app.route("/register")
def page_register():
    return render_template("register.html")


@app.route("/account")
def page_account():
    return render_template("account.html")


@app.route("/admin")
def page_admin():
    return render_template("admin.html")


@app.route("/api/server-time")
def api_server_time():
    now = now_ph()
    return jsonify(
        {
            "iso": now.isoformat(timespec="seconds"),
            "date_display": now.strftime("%Y-%m-%d"),
            "time_display": now.strftime("%H:%M:%S"),
            "weekday": now.strftime("%A"),
            "tz": "Asia/Manila",
        }
    )


@app.get("/api/qr")
def api_qr_png():
    code = (request.args.get("code") or "").strip()
    if not code:
        return jsonify({"error": "Missing code"}), 400
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id FROM ojt_users WHERE sr_code = ?", (code,))
    if not cur.fetchone():
        return jsonify({"error": "Unknown SR-Code"}), 404
    buf = io.BytesIO()
    img = qrcode.make(code)
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


@app.post("/api/scan")
def api_scan():
    data = request.get_json(silent=True) or {}
    raw = (
        data.get("qr_data") or data.get("sr_code") or data.get("token") or ""
    ).strip()
    if not raw:
        return jsonify({"error": "No QR data"}), 400
    method = (data.get("entry_method") or "scan").strip().lower()
    if method not in ALLOWED_ENTRY_METHODS:
        return jsonify({"error": "entry_method must be scan or manual"}), 400

    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM ojt_users WHERE sr_code = ?", (raw,))
    row = cur.fetchone()
    if not row:
        msg = "Unknown SR-Code" if method == "manual" else "Unknown QR code"
        return jsonify({"error": msg}), 404

    user_id = row["id"]
    now = now_ph()
    now_s = now.isoformat(timespec="seconds")

    open_entry = get_open_entry(cur, user_id)
    action = None
    if open_entry:
        out_dt = round_time_out(now)
        out_s = out_dt.isoformat(timespec="seconds")
        cur.execute(
            """
            UPDATE time_entries SET time_out = ?, time_out_method = ?
            WHERE id = ?
            """,
            (out_s, method, open_entry["id"]),
        )
        action = "time_out"
    else:
        cur.execute(
            """
            INSERT INTO time_entries (user_id, time_in, time_out, time_in_method, time_out_method)
            VALUES (?, ?, NULL, ?, NULL)
            """,
            (user_id, now_s, method),
        )
        action = "time_in"
    db.commit()

    spent_sec = sum_logged_seconds_for_user(cur, user_id, now)
    required_sec = float(row["required_hours"]) * 3600.0
    left_sec = max(0.0, required_sec - spent_sec)
    sh, sm, spent_label = seconds_to_hm(spent_sec)
    lh, lm, left_label = seconds_to_hm(left_sec)

    cur.execute(
        """
        SELECT id, time_in, time_out, time_in_method, time_out_method
        FROM time_entries
        WHERE user_id = ? ORDER BY time_in DESC LIMIT 10
        """,
        (user_id,),
    )
    entries = []
    for e in cur.fetchall():
        dur = entry_duration_seconds(e["time_in"], e["time_out"], now)
        dh, dm, dlabel = seconds_to_hm(dur)
        tin_m = e["time_in_method"]
        tout_m = e["time_out_method"]
        entries.append(
            {
                "id": e["id"],
                "time_in": e["time_in"],
                "time_out": e["time_out"],
                "time_in_method": tin_m,
                "time_out_method": tout_m,
                "time_in_method_label": entry_method_label(tin_m),
                "time_out_method_label": entry_method_label(tout_m),
                "duration_seconds": int(dur),
                "duration_label": dlabel,
            }
        )

    return jsonify(
        {
            "action": action,
            "entry_method": method,
            "user": user_row_to_dict(row),
            "spent_seconds": int(spent_sec),
            "spent_hours": sh,
            "spent_minutes": sm,
            "spent_label": spent_label,
            "required_hours": row["required_hours"],
            "left_seconds": int(left_sec),
            "left_hours": lh,
            "left_minutes": lm,
            "left_label": left_label,
            "recent_entries": entries,
        }
    )


@app.post("/api/register")
def api_register():
    is_multipart = bool(
        request.content_type and "multipart/form-data" in request.content_type
    )
    if is_multipart:
        data = request.form or {}
        files = request.files or {}
    else:
        data = request.get_json(silent=True) or {}
        files = {}

    name = (data.get("name") or "").strip()
    gender = (data.get("gender") or "").strip()
    department = (data.get("department") or "").strip()
    course = (data.get("course") or "").strip()
    sr_code = (data.get("sr_code") or "").strip()
    password = data.get("password") or ""
    try:
        batch_id = int(data.get("batch_id"))
        required_hours = float(data.get("required_hours"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid batch or required hours"}), 400

    if not name or not password:
        return jsonify({"error": "Name and password are required"}), 400
    if not sr_code:
        return jsonify({"error": "SR-Code is required"}), 400
    if gender not in ALLOWED_GENDERS:
        return jsonify({"error": "Gender must be Male or Female"}), 400
    if required_hours <= 0:
        return jsonify({"error": "Required hours must be positive"}), 400

    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id FROM batches WHERE id = ?", (batch_id,))
    if not cur.fetchone():
        return jsonify({"error": "Invalid batch"}), 400

    # Store photos in Supabase Storage when using Postgres (Vercel-safe).
    # Keep SQLite/local behavior for offline/local-only usage.
    photo_filename = ""
    extra_photo_filename = ""
    try:
        if USE_POSTGRES:
            photo_filename = _image_upload_to_storage(
                files.get("photo"),
                f"users/{_safe_filename_stem(sr_code)}/photo.png",
            )
            # extra photo removed from UI; ignore if provided.
        else:
            os.makedirs(UPLOADS_DIR, exist_ok=True)

            def save_image(upload, filename_prefix):
                if not upload:
                    return ""
                if getattr(upload, "filename", "") == "":
                    return ""
                try:
                    im = Image.open(upload.stream)
                    im = im.convert("RGB")
                except OSError:
                    raise ValueError("Invalid image file")
                max_side = 1400
                if max(im.size) > max_side:
                    im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
                out_name = f"{_safe_filename_stem(filename_prefix)}.png"
                out_path = os.path.join(UPLOADS_DIR, out_name)
                im.save(out_path, format="PNG", optimize=True)
                return out_name

            photo_filename = save_image(files.get("photo"), f"{sr_code}_photo")
            extra_photo_filename = save_image(files.get("extra_photo"), f"{sr_code}_extra")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except (ConfigError, RuntimeError) as e:
        return jsonify({"error": str(e)}), 500

    pw_hash = generate_password_hash(password)
    created = now_ph().isoformat(timespec="seconds")
    cols = _ojt_user_columns(cur)
    try:
        if "qr_token" in cols:
            cur.execute(
                """
                INSERT INTO ojt_users (
                    sr_code, qr_token, name, gender, department, course, batch_id,
                    required_hours, password_hash, photo_filename, extra_photo_filename,
                    goal_text, accomplishment_text, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', '', ?)
                """,
                (
                    sr_code,
                    sr_code,
                    name,
                    gender,
                    department,
                    course,
                    batch_id,
                    required_hours,
                    pw_hash,
                    photo_filename,
                    extra_photo_filename,
                    created,
                ),
            )
        else:
            cur.execute(
                """
                INSERT INTO ojt_users (
                    sr_code, name, gender, department, course, batch_id,
                    required_hours, password_hash, photo_filename, extra_photo_filename,
                    goal_text, accomplishment_text, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', '', ?)
                """,
                (
                    sr_code,
                    name,
                    gender,
                    department,
                    course,
                    batch_id,
                    required_hours,
                    pw_hash,
                    photo_filename,
                    extra_photo_filename,
                    created,
                ),
            )
        db.commit()
    except DB_INTEGRITY_ERRORS:
        return jsonify({"error": "SR-Code is already registered"}), 400

    if USE_POSTGRES:
        cur.execute("SELECT id FROM ojt_users WHERE sr_code = ?", (sr_code,))
        row = cur.fetchone()
        uid = row["id"] if row else None
    else:
        uid = cur.lastrowid
    qr_url = f"/api/qr?code={quote(sr_code, safe='')}"
    return jsonify(
        {
            "ok": True,
            "user_id": uid,
            "sr_code": sr_code,
            "qr_url": qr_url,
        }
    )


@app.get("/api/suggest/department")
def api_suggest_department():
    q = (request.args.get("q") or "").strip().lower()
    if not q:
        return jsonify({"items": []})
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """
        SELECT DISTINCT department AS v
        FROM ojt_users
        WHERE LOWER(department) LIKE ?
        ORDER BY department
        LIMIT 12
        """,
        (f"{q}%",),
    )
    return jsonify({"items": [r["v"] for r in cur.fetchall() if r["v"]]})


@app.get("/api/suggest/course")
def api_suggest_course():
    q = (request.args.get("q") or "").strip().lower()
    if not q:
        return jsonify({"items": []})
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """
        SELECT DISTINCT course AS v
        FROM ojt_users
        WHERE LOWER(course) LIKE ?
        ORDER BY course
        LIMIT 12
        """,
        (f"{q}%",),
    )
    return jsonify({"items": [r["v"] for r in cur.fetchall() if r["v"]]})


@app.get("/api/batches")
def api_batches():
    q = (request.args.get("q") or "").strip().lower()
    db = get_db()
    cur = db.cursor()
    if q:
        cur.execute(
            "SELECT id, name FROM batches WHERE LOWER(name) LIKE ? ORDER BY name",
            (f"%{q}%",),
        )
    else:
        cur.execute("SELECT id, name FROM batches ORDER BY name")
    rows = [{"id": r["id"], "name": r["name"]} for r in cur.fetchall()]
    return jsonify({"batches": rows})


@app.get("/api/account/batch/<int:batch_id>/users")
def api_account_batch_users(batch_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id, name FROM batches WHERE id = ?", (batch_id,))
    if not cur.fetchone():
        return jsonify({"error": "Batch not found"}), 404
    cur.execute(
        "SELECT id, name, course FROM ojt_users WHERE batch_id = ? ORDER BY name",
        (batch_id,),
    )
    users = [{"id": r["id"], "name": r["name"], "course": r["course"]} for r in cur.fetchall()]
    return jsonify({"users": users})


@app.post("/api/account/user/<int:user_id>/login")
def api_account_login(user_id):
    data = request.get_json(silent=True) or {}
    password = data.get("password") or ""
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM ojt_users WHERE id = ?", (user_id,))
    row = cur.fetchone()
    if not row or not check_password_hash(row["password_hash"], password):
        return jsonify({"error": "Invalid password"}), 401
    session["account_user_id"] = user_id
    return jsonify({"ok": True})


@app.post("/api/account/logout")
def api_account_logout():
    session.pop("account_user_id", None)
    return jsonify({"ok": True})


def require_account_user(user_id):
    if session.get("account_user_id") != user_id:
        return jsonify({"error": "Unauthorized"}), 401
    return None


def _front_photo_box(im_w, im_h):
    sx = im_w / _FRONT_REF_W
    sy = im_h / _FRONT_REF_H
    left = round(_FRONT_PHOTO_LEFT * sx)
    top = round(_FRONT_PHOTO_TOP * sy)
    right = round(_FRONT_PHOTO_RIGHT * sx)
    bottom = round(_FRONT_PHOTO_BOTTOM * sy)
    return left, top, right, bottom


def _back_qr_box(im_w, im_h):
    sx = im_w / _BACK_REF_W
    sy = im_h / _BACK_REF_H
    left = round(_BACK_QR_LEFT * sx)
    top = round(_BACK_QR_TOP * sy)
    right = round(_BACK_QR_RIGHT * sx)
    bottom = round(_BACK_QR_BOTTOM * sy)
    inset = max(0, int(round(_BACK_QR_INSET * min(sx, sy))))
    return left + inset, top + inset, right - inset, bottom - inset


def _pick_id_font(size):
    # Prefer system fonts; fall back to PIL default bitmap font.
    for fp in (
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/ARIAL.TTF",
        "C:/Windows/Fonts/calibri.ttf",
        "C:/Windows/Fonts/Calibri.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/SegoeUI.ttf",
    ):
        try:
            if os.path.isfile(fp):
                return ImageFont.truetype(fp, size=size)
        except OSError:
            pass
    try:
        return ImageFont.truetype("arial.ttf", size=size)
    except OSError:
        return ImageFont.load_default()


def _draw_front_id_text(template, full_name, course):
    im_w, im_h = template.size
    left, top, right, bottom = _front_photo_box(im_w, im_h)
    # Place text into the large band below the photo (the red rectangle area in the template).
    # Start a bit below the photo, but clamp to a stable ratio so it doesn't crowd the frame.
    y0 = max(bottom + max(10, int(round(im_h * 0.01))), int(round(im_h * 0.63)))
    # Reduce side padding so we can render larger fonts.
    pad_x = max(10, int(round(im_w * 0.03)))
    x0 = pad_x
    x1 = im_w - pad_x
    line_gap = max(10, int(round(im_h * 0.012)))

    name = (full_name or "").strip()
    if not name and not course:
        return
    first = name.split()[0] if name else ""

    def fit_font(text, max_size, min_size):
        size = max_size
        draw = ImageDraw.Draw(template)
        while size >= min_size:
            font = _pick_id_font(size)
            bbox = draw.textbbox((0, 0), text, font=font)
            tw = bbox[2] - bbox[0]
            if tw <= (x1 - x0):
                return font
            size -= 1
        return _pick_id_font(min_size)

    draw = ImageDraw.Draw(template)
    def wrap_words_to_lines(text, max_lines):
        words = [w for w in (text or "").split() if w]
        if not words:
            return []
        lines = []
        i = 0
        while i < len(words) and len(lines) < max_lines:
            line = words[i]
            i += 1
            while i < len(words):
                candidate = f"{line} {words[i]}"
                # Use a medium probe size to decide wrapping, independent of final font size.
                probe = _pick_id_font(max(24, int(round(im_h * 0.05))))
                bbox = draw.textbbox((0, 0), candidate, font=probe)
                if (bbox[2] - bbox[0]) <= (x1 - x0):
                    line = candidate
                    i += 1
                else:
                    break
            lines.append(line)
        # If we ran out of lines but still have words, append them to the last line.
        if i < len(words) and lines:
            lines[-1] = (lines[-1] + " " + " ".join(words[i:])).strip()
        return lines

    # Make the first word very large, then render the rest of the name (wrapped) large too.
    if first:
        up1 = first.upper()
        f2 = fit_font(up1, max_size=max(140, int(round(im_h * 0.33))), min_size=48)
        b2 = draw.textbbox((0, 0), up1, font=f2)
        fx = x0 + ((x1 - x0) - (b2[2] - b2[0])) // 2
        draw.text((fx, y0), up1, fill=(120, 0, 0), font=f2)
        y0 += (b2[3] - b2[1]) + line_gap

    if name:
        rest = " ".join(name.split()[1:]).strip()
        if rest:
            for line in wrap_words_to_lines(rest.upper(), max_lines=2):
                f1 = fit_font(line, max_size=max(92, int(round(im_h * 0.22))), min_size=34)
                b1 = draw.textbbox((0, 0), line, font=f1)
                nx = x0 + ((x1 - x0) - (b1[2] - b1[0])) // 2
                draw.text((nx, y0), line, fill=(120, 0, 0), font=f1)
                y0 += (b1[3] - b1[1]) + line_gap

    c = (course or "").strip()
    if c:
        for line in wrap_words_to_lines(c, max_lines=2):
            f3 = fit_font(line, max_size=max(88, int(round(im_h * 0.19))), min_size=30)
            b3 = draw.textbbox((0, 0), line, font=f3)
            cx = x0 + ((x1 - x0) - (b3[2] - b3[0])) // 2
            draw.text((cx, y0), line, fill=(120, 0, 0), font=f3)
            y0 += (b3[3] - b3[1]) + line_gap


def _load_user_photo(photo_filename):
    if not photo_filename:
        return None
    try:
        if USE_POSTGRES:
            raw = _storage_get_object_bytes(SUPABASE_STORAGE_BUCKET, photo_filename)
            if not raw:
                return None
            return Image.open(io.BytesIO(raw)).convert("RGB")
        p = os.path.join(UPLOADS_DIR, photo_filename)
        if not os.path.isfile(p):
            return None
        return Image.open(p).convert("RGB")
    except OSError:
        return None


def _safe_filename_stem(text):
    t = re.sub(r"[^\w\-.]+", "_", (text or "").strip(), flags=re.UNICODE)
    t = re.sub(r"_+", "_", t).strip("._-")
    return (t or "")[:120]


def build_front_id_png(full_name, course, photo_filename):
    if not os.path.isfile(ID_FRONT_TEMPLATE_PATH):
        raise FileNotFoundError(ID_FRONT_TEMPLATE_PATH)
    template = Image.open(ID_FRONT_TEMPLATE_PATH).convert("RGB")
    im_w, im_h = template.size
    left, top, right, bottom = _front_photo_box(im_w, im_h)
    w = max(1, right - left)
    h = max(1, bottom - top)

    photo = _load_user_photo(photo_filename)
    if photo is not None:
        pr = photo.width / photo.height
        tr = w / h
        if pr > tr:
            new_h = photo.height
            new_w = int(round(new_h * tr))
        else:
            new_w = photo.width
            new_h = int(round(new_w / tr))
        x0 = max(0, (photo.width - new_w) // 2)
        y0 = max(0, (photo.height - new_h) // 2)
        photo = (
            photo.crop((x0, y0, x0 + new_w, y0 + new_h))
            .resize((w, h), Image.Resampling.LANCZOS)
        )

        mask = Image.new("L", (w, h), 0)
        md = ImageDraw.Draw(mask)
        radius = max(12, int(round(min(w, h) * 0.10)))
        md.rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=255)
        template.paste(photo, (left, top), mask)

    _draw_front_id_text(template, full_name, course)
    buf = io.BytesIO()
    template.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf


def build_back_id_png(sr_code):
    if not os.path.isfile(ID_BACK_TEMPLATE_PATH):
        raise FileNotFoundError(ID_BACK_TEMPLATE_PATH)
    template = Image.open(ID_BACK_TEMPLATE_PATH).convert("RGB")
    im_w, im_h = template.size
    left, top, right, bottom = _back_qr_box(im_w, im_h)
    w = max(1, right - left)
    h = max(1, bottom - top)

    qr = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECT_M,
        box_size=10,
        border=0,
    )
    qr.add_data(sr_code)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    qr_img = qr_img.resize((w, h), Image.Resampling.LANCZOS)
    template.paste(qr_img, (left, top))
    buf = io.BytesIO()
    template.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf


@app.get("/api/account/user/<int:user_id>/id-card/front")
def api_account_id_card_front(user_id):
    err = require_account_user(user_id)
    if err:
        return err
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "SELECT name, course, photo_filename FROM ojt_users WHERE id = ?",
        (user_id,),
    )
    row = cur.fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    try:
        buf = build_front_id_png(row["name"], row["course"], row["photo_filename"])
    except FileNotFoundError:
        return jsonify({"error": "ID card template is missing on the server"}), 500
    except OSError:
        return jsonify({"error": "Could not generate ID card"}), 500
    stem = _safe_filename_stem(row["name"]) or "OJT-ID"
    name = f"({stem})_front.png"
    resp = send_file(
        buf,
        mimetype="image/png",
        as_attachment=True,
        download_name=name,
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/api/account/user/<int:user_id>/id-card/back")
def api_account_id_card_back(user_id):
    err = require_account_user(user_id)
    if err:
        return err
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT name, sr_code FROM ojt_users WHERE id = ?", (user_id,))
    row = cur.fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    try:
        buf = build_back_id_png(row["sr_code"])
    except FileNotFoundError:
        return jsonify({"error": "ID card template is missing on the server"}), 500
    except OSError:
        return jsonify({"error": "Could not generate ID card"}), 500
    stem = _safe_filename_stem(row["name"]) or "OJT-ID"
    name = f"({stem})_back.png"
    resp = send_file(
        buf,
        mimetype="image/png",
        as_attachment=True,
        download_name=name,
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/api/account/user/<int:user_id>/id-card/front/preview")
def api_account_id_card_front_preview(user_id):
    err = require_account_user(user_id)
    if err:
        return err
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "SELECT name, course, photo_filename FROM ojt_users WHERE id = ?",
        (user_id,),
    )
    row = cur.fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    try:
        buf = build_front_id_png(row["name"], row["course"], row["photo_filename"])
    except FileNotFoundError:
        return jsonify({"error": "ID card template is missing on the server"}), 500
    except OSError:
        return jsonify({"error": "Could not generate ID card"}), 500
    resp = send_file(buf, mimetype="image/png", as_attachment=False)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/api/account/user/<int:user_id>/id-card/back/preview")
def api_account_id_card_back_preview(user_id):
    err = require_account_user(user_id)
    if err:
        return err
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT sr_code FROM ojt_users WHERE id = ?", (user_id,))
    row = cur.fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    try:
        buf = build_back_id_png(row["sr_code"])
    except FileNotFoundError:
        return jsonify({"error": "ID card template is missing on the server"}), 500
    except OSError:
        return jsonify({"error": "Could not generate ID card"}), 500
    resp = send_file(buf, mimetype="image/png", as_attachment=False)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/api/admin/user/<int:user_id>/id-card/front")
def api_admin_user_id_card_front(user_id):
    err = require_admin()
    if err:
        return err
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "SELECT name, course, photo_filename FROM ojt_users WHERE id = ?",
        (user_id,),
    )
    row = cur.fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    try:
        buf = build_front_id_png(row["name"], row["course"], row["photo_filename"])
    except FileNotFoundError:
        return jsonify({"error": "ID card template is missing on the server"}), 500
    except OSError:
        return jsonify({"error": "Could not generate ID card"}), 500
    stem = _safe_filename_stem(row["name"]) or "OJT-ID"
    name = f"({stem})_front.png"
    resp = send_file(
        buf,
        mimetype="image/png",
        as_attachment=True,
        download_name=name,
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/api/admin/user/<int:user_id>/id-card/back")
def api_admin_user_id_card_back(user_id):
    err = require_admin()
    if err:
        return err
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT name, sr_code FROM ojt_users WHERE id = ?", (user_id,))
    row = cur.fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    try:
        buf = build_back_id_png(row["sr_code"])
    except FileNotFoundError:
        return jsonify({"error": "ID card template is missing on the server"}), 500
    except OSError:
        return jsonify({"error": "Could not generate ID card"}), 500
    stem = _safe_filename_stem(row["name"]) or "OJT-ID"
    name = f"({stem})_back.png"
    resp = send_file(
        buf,
        mimetype="image/png",
        as_attachment=True,
        download_name=name,
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.post("/api/account/user/<int:user_id>/photos")
def api_account_user_update_photos(user_id):
    err = require_account_user(user_id)
    if err:
        return err
    if not request.content_type or "multipart/form-data" not in request.content_type:
        return jsonify({"error": "multipart/form-data required"}), 400
    files = request.files or {}
    photo = files.get("photo")
    extra = files.get("extra_photo")
    if (not photo or not getattr(photo, "filename", "")) and (
        not extra or not getattr(extra, "filename", "")
    ):
        return jsonify({"error": "No files uploaded"}), 400

    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT sr_code FROM ojt_users WHERE id = ?", (user_id,))
    row = cur.fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404

    updates = []
    vals = []
    try:
        if photo and getattr(photo, "filename", ""):
            fn = _save_user_profile_photo(photo, row["sr_code"], slot="photo")
            if fn:
                updates.append("photo_filename = ?")
                vals.append(fn)
        if extra and getattr(extra, "filename", ""):
            fn2 = _save_user_profile_photo(extra, row["sr_code"], slot="extra")
            if fn2:
                updates.append("extra_photo_filename = ?")
                vals.append(fn2)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except (ConfigError, RuntimeError) as e:
        return jsonify({"error": str(e)}), 500

    if not updates:
        return jsonify({"error": "No valid files uploaded"}), 400

    vals.append(user_id)
    cur.execute(f"UPDATE ojt_users SET {', '.join(updates)} WHERE id = ?", vals)
    db.commit()
    return jsonify({"ok": True})


@app.post("/api/admin/user/<int:user_id>/photos")
def api_admin_user_update_photos(user_id):
    err = require_admin()
    if err:
        return err
    if not request.content_type or "multipart/form-data" not in request.content_type:
        return jsonify({"error": "multipart/form-data required"}), 400
    files = request.files or {}
    photo = files.get("photo")
    extra = files.get("extra_photo")
    if (not photo or not getattr(photo, "filename", "")) and (
        not extra or not getattr(extra, "filename", "")
    ):
        return jsonify({"error": "No files uploaded"}), 400

    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT sr_code FROM ojt_users WHERE id = ?", (user_id,))
    row = cur.fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404

    updates = []
    vals = []
    try:
        if photo and getattr(photo, "filename", ""):
            fn = _save_user_profile_photo(photo, row["sr_code"], slot="photo")
            if fn:
                updates.append("photo_filename = ?")
                vals.append(fn)
        if extra and getattr(extra, "filename", ""):
            fn2 = _save_user_profile_photo(extra, row["sr_code"], slot="extra")
            if fn2:
                updates.append("extra_photo_filename = ?")
                vals.append(fn2)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except (ConfigError, RuntimeError) as e:
        return jsonify({"error": str(e)}), 500

    if not updates:
        return jsonify({"error": "No valid files uploaded"}), 400

    vals.append(user_id)
    cur.execute(f"UPDATE ojt_users SET {', '.join(updates)} WHERE id = ?", vals)
    db.commit()
    return jsonify({"ok": True})


@app.delete("/api/admin/user/<int:user_id>")
def api_admin_user_delete(user_id):
    err = require_admin()
    if err:
        return err
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id FROM ojt_users WHERE id = ?", (user_id,))
    if not cur.fetchone():
        return jsonify({"error": "Not found"}), 404
    cur.execute("DELETE FROM time_entries WHERE user_id = ?", (user_id,))
    cur.execute("DELETE FROM ojt_users WHERE id = ?", (user_id,))
    db.commit()
    return jsonify({"ok": True})


@app.get("/api/account/user/<int:user_id>/detail")
def api_account_user_detail(user_id):
    err = require_account_user(user_id)
    if err:
        return err
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """
        SELECT u.*, b.name AS batch_name FROM ojt_users u
        JOIN batches b ON b.id = u.batch_id
        WHERE u.id = ?
        """,
        (user_id,),
    )
    row = cur.fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404

    now = now_ph()
    spent_sec = sum_logged_seconds_for_user(cur, user_id, now)
    required_sec = float(row["required_hours"]) * 3600.0
    left_sec = max(0.0, required_sec - spent_sec)
    sh, sm, spent_label = seconds_to_hm(spent_sec)
    lh, lm, left_label = seconds_to_hm(left_sec)

    cur.execute(
        """
        SELECT id, time_in, time_out, session_note, time_in_method, time_out_method
        FROM time_entries WHERE user_id = ? ORDER BY time_in
        """,
        (user_id,),
    )
    pairs = []
    for e in cur.fetchall():
        dur = entry_duration_seconds(e["time_in"], e["time_out"], now)
        dh, dm, dlabel = seconds_to_hm(dur)
        pairs.append(
            {
                "id": e["id"],
                "time_in": e["time_in"],
                "time_out": e["time_out"],
                "session_note": e["session_note"] or "",
                "time_in_method": e["time_in_method"],
                "time_out_method": e["time_out_method"],
                "time_in_method_label": entry_method_label(e["time_in_method"]),
                "time_out_method_label": entry_method_label(e["time_out_method"]),
                "duration_label": dlabel,
                "duration_hours": dh,
                "duration_minutes": dm,
            }
        )

    return jsonify(
        {
            "user": {
                "id": row["id"],
                "name": row["name"],
                "course": row["course"],
                "department": row["department"],
                "gender": row["gender"],
                "batch_name": row["batch_name"],
                "required_hours": row["required_hours"],
                "sr_code": row["sr_code"],
            },
            "spent_seconds": int(spent_sec),
            "spent_label": spent_label,
            "spent_hours": sh,
            "spent_minutes": sm,
            "left_seconds": int(left_sec),
            "left_label": left_label,
            "left_hours": lh,
            "left_minutes": lm,
            "entries": pairs,
        }
    )


@app.put("/api/account/user/<int:user_id>/notes")
def api_account_user_notes(user_id):
    err = require_account_user(user_id)
    if err:
        return err
    data = request.get_json(silent=True) or {}
    goal = data.get("goal_text")
    accomplishment = data.get("accomplishment_text")
    if goal is None and accomplishment is None:
        return jsonify({"error": "Nothing to update"}), 400
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id FROM ojt_users WHERE id = ?", (user_id,))
    if not cur.fetchone():
        return jsonify({"error": "Not found"}), 404
    if goal is not None:
        cur.execute("UPDATE ojt_users SET goal_text = ? WHERE id = ?", (goal, user_id))
    if accomplishment is not None:
        cur.execute(
            "UPDATE ojt_users SET accomplishment_text = ? WHERE id = ?",
            (accomplishment, user_id),
        )
    db.commit()
    return jsonify({"ok": True})


@app.put("/api/account/user/<int:user_id>/entry/<int:entry_id>/note")
def api_account_entry_note(user_id, entry_id):
    err = require_account_user(user_id)
    if err:
        return err
    data = request.get_json(silent=True) or {}
    if "session_note" not in data:
        return jsonify({"error": "session_note required"}), 400
    note = data.get("session_note")
    if note is None:
        note = ""
    if not isinstance(note, str):
        note = str(note)
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "SELECT id FROM time_entries WHERE id = ? AND user_id = ?",
        (entry_id, user_id),
    )
    if not cur.fetchone():
        return jsonify({"error": "Not found"}), 404
    cur.execute(
        "UPDATE time_entries SET session_note = ? WHERE id = ? AND user_id = ?",
        (note, entry_id, user_id),
    )
    db.commit()
    return jsonify({"ok": True})


@app.post("/api/admin/login")
def api_admin_login():
    data = request.get_json(silent=True) or {}
    if (data.get("password") or "") == ADMIN_PASSWORD:
        session["is_admin"] = True
        return jsonify({"ok": True})
    return jsonify({"error": "Invalid password"}), 401


@app.post("/api/admin/logout")
def api_admin_logout():
    session.pop("is_admin", None)
    return jsonify({"ok": True})


def require_admin():
    if not session.get("is_admin"):
        return jsonify({"error": "Admin only"}), 403
    return None


@app.post("/api/admin/batches")
def api_admin_create_batch():
    err = require_admin()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Batch name required"}), 400
    db = get_db()
    cur = db.cursor()
    created = now_ph().isoformat(timespec="seconds")
    try:
        cur.execute(
            "INSERT INTO batches (name, created_at) VALUES (?, ?)",
            (name, created),
        )
        db.commit()
    except DB_INTEGRITY_ERRORS:
        return jsonify({"error": "Batch name already exists"}), 400
    if USE_POSTGRES:
        cur.execute("SELECT id FROM batches WHERE name = ?", (name,))
        row = cur.fetchone()
        bid = row["id"] if row else None
    else:
        bid = cur.lastrowid
    return jsonify({"ok": True, "id": bid})


@app.get("/api/admin/batches")
def api_admin_batches_list():
    err = require_admin()
    if err:
        return err
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """
        SELECT b.id, b.name,
               (SELECT COUNT(*) FROM ojt_users u WHERE u.batch_id = b.id) AS trainee_count
        FROM batches b
        ORDER BY b.name
        """
    )
    batches = [
        {"id": r["id"], "name": r["name"], "trainee_count": r["trainee_count"]}
        for r in cur.fetchall()
    ]
    return jsonify({"batches": batches})


@app.get("/api/admin/users")
def api_admin_users():
    err = require_admin()
    if err:
        return err
    q = (request.args.get("q") or "").strip().lower()
    batch_id_raw = request.args.get("batch_id")
    batch_id = None
    if batch_id_raw is not None and batch_id_raw != "":
        try:
            batch_id = int(batch_id_raw)
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid batch_id"}), 400

    db = get_db()
    cur = db.cursor()
    conditions = []
    params = []
    if batch_id is not None:
        cur.execute("SELECT id FROM batches WHERE id = ?", (batch_id,))
        if not cur.fetchone():
            return jsonify({"error": "Batch not found"}), 404
        conditions.append("u.batch_id = ?")
        params.append(batch_id)
    if q:
        conditions.append(
            "(LOWER(u.name) LIKE ? OR LOWER(u.course) LIKE ? OR LOWER(b.name) LIKE ?)"
        )
        like = f"%{q}%"
        params.extend([like, like, like])
    where_sql = " AND ".join(conditions) if conditions else "1"
    cur.execute(
        f"""
        SELECT u.id, u.name, u.course, u.department, b.name AS batch_name, u.batch_id
        FROM ojt_users u
        JOIN batches b ON b.id = u.batch_id
        WHERE {where_sql}
        ORDER BY u.name
        """,
        params,
    )
    users = [dict(r) for r in cur.fetchall()]
    return jsonify({"users": users})


@app.get("/api/admin/user/<int:user_id>")
def api_admin_user(user_id):
    err = require_admin()
    if err:
        return err
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """
        SELECT u.*, b.name AS batch_name FROM ojt_users u
        JOIN batches b ON b.id = u.batch_id
        WHERE u.id = ?
        """,
        (user_id,),
    )
    row = cur.fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    now = now_ph()
    spent_sec = sum_logged_seconds_for_user(cur, user_id, now)
    required_sec = float(row["required_hours"]) * 3600.0
    left_sec = max(0.0, required_sec - spent_sec)
    sh, sm, spent_label = seconds_to_hm(spent_sec)
    lh, lm, left_label = seconds_to_hm(left_sec)
    cur.execute("SELECT id, name FROM batches ORDER BY name")
    batches = [{"id": r["id"], "name": r["name"]} for r in cur.fetchall()]
    return jsonify(
        {
            "user": {
                "id": row["id"],
                "sr_code": row["sr_code"],
                "name": row["name"],
                "gender": row["gender"],
                "department": row["department"],
                "course": row["course"],
                "batch_id": row["batch_id"],
                "batch_name": row["batch_name"],
                "required_hours": row["required_hours"],
                "goal_text": row["goal_text"],
                "accomplishment_text": row["accomplishment_text"],
            },
            "spent_label": spent_label,
            "spent_hours": sh,
            "spent_minutes": sm,
            "left_label": left_label,
            "left_hours": lh,
            "left_minutes": lm,
            "batches": batches,
        }
    )


@app.put("/api/admin/user/<int:user_id>")
def api_admin_user_update(user_id):
    err = require_admin()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id FROM ojt_users WHERE id = ?", (user_id,))
    if not cur.fetchone():
        return jsonify({"error": "Not found"}), 404

    if "gender" in data:
        g = (data.get("gender") or "").strip()
        if g not in ALLOWED_GENDERS:
            return jsonify({"error": "Gender must be Male or Female"}), 400

    fields = []
    values = []
    mapping = [
        ("name", "name"),
        ("gender", "gender"),
        ("department", "department"),
        ("course", "course"),
        ("goal_text", "goal_text"),
        ("accomplishment_text", "accomplishment_text"),
    ]
    for key, col in mapping:
        if key in data:
            fields.append(f"{col} = ?")
            values.append(data[key])

    if "sr_code" in data:
        sc = (data.get("sr_code") or "").strip()
        if not sc:
            return jsonify({"error": "SR-Code cannot be empty"}), 400
        fields.append("sr_code = ?")
        values.append(sc)

    if "batch_id" in data:
        try:
            bid = int(data["batch_id"])
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid batch"}), 400
        cur.execute("SELECT id FROM batches WHERE id = ?", (bid,))
        if not cur.fetchone():
            return jsonify({"error": "Invalid batch"}), 400
        fields.append("batch_id = ?")
        values.append(bid)

    if "required_hours" in data:
        try:
            rh = float(data["required_hours"])
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid hours"}), 400
        if rh <= 0:
            return jsonify({"error": "Required hours must be positive"}), 400
        fields.append("required_hours = ?")
        values.append(rh)

    if data.get("password"):
        fields.append("password_hash = ?")
        values.append(generate_password_hash(data["password"]))

    if not fields:
        return jsonify({"error": "No updates"}), 400

    values.append(user_id)
    try:
        cur.execute(
            f"UPDATE ojt_users SET {', '.join(fields)} WHERE id = ?",
            values,
        )
        db.commit()
    except sqlite3.IntegrityError:
        return jsonify({"error": "SR-Code already in use"}), 400
    return jsonify({"ok": True})


@app.get("/api/admin/user/<int:user_id>/entries")
def api_admin_user_entries(user_id):
    err = require_admin()
    if err:
        return err
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id FROM ojt_users WHERE id = ?", (user_id,))
    if not cur.fetchone():
        return jsonify({"error": "Not found"}), 404
    now = now_ph()
    cur.execute(
        """
        SELECT id, time_in, time_out, session_note, time_in_method, time_out_method
        FROM time_entries WHERE user_id = ? ORDER BY time_in
        """,
        (user_id,),
    )
    out = []
    for e in cur.fetchall():
        dur = entry_duration_seconds(e["time_in"], e["time_out"], now)
        _, _, dlabel = seconds_to_hm(dur)
        out.append(
            {
                "id": e["id"],
                "time_in": e["time_in"],
                "time_out": e["time_out"],
                "session_note": e["session_note"] or "",
                "time_in_method": e["time_in_method"],
                "time_out_method": e["time_out_method"],
                "time_in_method_label": entry_method_label(e["time_in_method"]),
                "time_out_method_label": entry_method_label(e["time_out_method"]),
                "duration_label": dlabel,
            }
        )
    return jsonify({"entries": out})


@app.put("/api/admin/entry/<int:entry_id>")
def api_admin_entry_update(entry_id):
    err = require_admin()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    time_in = data.get("time_in")
    time_out = data.get("time_out")
    if time_in is None:
        return jsonify({"error": "time_in required"}), 400
    try:
        parse_dt(time_in)
        if time_out is not None and time_out != "":
            parse_dt(time_out)
    except ValueError:
        return jsonify({"error": "Invalid datetime format (use ISO local)"}), 400

    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id FROM time_entries WHERE id = ?", (entry_id,))
    if not cur.fetchone():
        return jsonify({"error": "Not found"}), 404

    tout = time_out if time_out else None
    if tout == "":
        tout = None
    sn = data.get("session_note")
    if sn is not None:
        if not isinstance(sn, str):
            sn = str(sn)
        cur.execute(
            "UPDATE time_entries SET time_in = ?, time_out = ?, session_note = ? WHERE id = ?",
            (time_in, tout, sn, entry_id),
        )
    else:
        cur.execute(
            "UPDATE time_entries SET time_in = ?, time_out = ? WHERE id = ?",
            (time_in, tout, entry_id),
        )
    db.commit()
    return jsonify({"ok": True})


@app.post("/api/admin/user/<int:user_id>/entries")
def api_admin_entry_create(user_id):
    err = require_admin()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    time_in = data.get("time_in")
    time_out = data.get("time_out")
    if not time_in:
        return jsonify({"error": "time_in required"}), 400
    try:
        parse_dt(time_in)
        if time_out:
            parse_dt(time_out)
    except ValueError:
        return jsonify({"error": "Invalid datetime format"}), 400

    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id FROM ojt_users WHERE id = ?", (user_id,))
    if not cur.fetchone():
        return jsonify({"error": "User not found"}), 404

    tout = time_out if time_out else None
    cur.execute(
        "INSERT INTO time_entries (user_id, time_in, time_out) VALUES (?, ?, ?)",
        (user_id, time_in, tout),
    )
    db.commit()
    if USE_POSTGRES:
        cur.execute(
            """
            SELECT id
            FROM time_entries
            WHERE user_id = ? AND time_in = ? AND (time_out IS NOT DISTINCT FROM ?)
            ORDER BY id DESC
            LIMIT 1
            """,
            (user_id, time_in, tout),
        )
        row = cur.fetchone()
        new_id = row["id"] if row else None
    else:
        new_id = cur.lastrowid
    return jsonify({"ok": True, "id": new_id})


@app.delete("/api/admin/entry/<int:entry_id>")
def api_admin_entry_delete(entry_id):
    err = require_admin()
    if err:
        return err
    db = get_db()
    cur = db.cursor()
    cur.execute("DELETE FROM time_entries WHERE id = ?", (entry_id,))
    db.commit()
    if cur.rowcount == 0:
        return jsonify({"error": "Not found"}), 404
    return jsonify({"ok": True})


with app.app_context():
    init_db()

if __name__ == "__main__":
    app.run(debug=True, host=OJT_HOST, port=OJT_PORT)
