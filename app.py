import io
import os
import re
import sqlite3
from datetime import datetime
from urllib.parse import quote

from flask import Flask, g, jsonify, render_template, request, send_file, session
from PIL import Image, ImageDraw, ImageFont
from qrcode.constants import ERROR_CORRECT_M
from werkzeug.security import check_password_hash, generate_password_hash
import qrcode

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.path.join(BASE_DIR, "ojt.db")
# BSU OJT ID template — QR fills inner red frame (reference 591×1004). Calibrated to inner box ~311×301.
ID_CARD_TEMPLATE_PATH = os.path.join(BASE_DIR, "resources", "OJT-ID.png")
_ID_REF_W = 591
_ID_REF_H = 1004
_ID_QR_LEFT = 148
_ID_QR_TOP = 367
_ID_QR_SIDE = 301
_ID_QR_INSET = 0
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")
ADMIN_PASSWORD = os.environ.get("OJT_ADMIN_PASSWORD", "Melo1234")
# Bind address. Use 0.0.0.0 to allow other PCs on the network to connect.
# Override with OJT_HOST / OJT_PORT as needed.
OJT_HOST = os.environ.get("OJT_HOST", "0.0.0.0")
OJT_PORT = int(os.environ.get("OJT_PORT", "5000"))
ALLOWED_GENDERS = frozenset({"Male", "Female"})
ALLOWED_ENTRY_METHODS = frozenset({"scan", "manual"})

app = Flask(__name__)
app.config["SECRET_KEY"] = SECRET_KEY


def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db


@app.teardown_appcontext
def close_db(_exc):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DATABASE)
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
    migrate_time_entries_session_note()
    migrate_time_entries_methods()


def migrate_ojt_users_sr_code():
    """Add sr_code and copy from legacy qr_token for existing databases."""
    db = sqlite3.connect(DATABASE)
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
    cur.execute("PRAGMA table_info(ojt_users)")
    return {r[1] for r in cur.fetchall()}


def ensure_ojt_user_indexes():
    db = sqlite3.connect(DATABASE)
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


def migrate_time_entries_session_note():
    db = sqlite3.connect(DATABASE)
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
    db = sqlite3.connect(DATABASE)
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


def entry_duration_seconds(time_in, time_out, now=None):
    now = now or datetime.now()
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
    now = now or datetime.now()
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
    now = datetime.now()
    return jsonify(
        {
            "iso": now.isoformat(timespec="seconds"),
            "date_display": now.strftime("%Y-%m-%d"),
            "time_display": now.strftime("%H:%M:%S"),
            "weekday": now.strftime("%A"),
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
    now = datetime.now()
    now_s = now.isoformat(timespec="seconds")

    open_entry = get_open_entry(cur, user_id)
    action = None
    if open_entry:
        cur.execute(
            """
            UPDATE time_entries SET time_out = ?, time_out_method = ?
            WHERE id = ?
            """,
            (now_s, method, open_entry["id"]),
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
    data = request.get_json(silent=True) or {}
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

    pw_hash = generate_password_hash(password)
    created = datetime.now().isoformat(timespec="seconds")
    cols = _ojt_user_columns(cur)
    try:
        if "qr_token" in cols:
            cur.execute(
                """
                INSERT INTO ojt_users (
                    sr_code, qr_token, name, gender, department, course, batch_id,
                    required_hours, password_hash, goal_text, accomplishment_text, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '', '', ?)
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
                    created,
                ),
            )
        else:
            cur.execute(
                """
                INSERT INTO ojt_users (
                    sr_code, name, gender, department, course, batch_id,
                    required_hours, password_hash, goal_text, accomplishment_text, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', '', ?)
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
                    created,
                ),
            )
        db.commit()
    except sqlite3.IntegrityError:
        return jsonify({"error": "SR-Code is already registered"}), 400

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


def _id_card_qr_placement(im_w, im_h):
    sx = im_w / _ID_REF_W
    sy = im_h / _ID_REF_H
    scale = min(sx, sy)
    left = round(_ID_QR_LEFT * sx)
    top = round(_ID_QR_TOP * sy)
    side = round(_ID_QR_SIDE * scale)
    inset = max(0, int(round(_ID_QR_INSET * scale)))
    return left, top, side, inset


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


def _draw_id_text(template, name, sr_code):
    im_w, im_h = template.size
    left, top, side, inset = _id_card_qr_placement(im_w, im_h)
    box_bottom = top + side

    # Text block placed under the QR/photo frame.
    pad_x = max(18, int(round(im_w * 0.06)))
    x0 = pad_x
    x1 = im_w - pad_x
    y0 = min(im_h - 1, box_bottom + max(18, int(round(im_h * 0.02))))
    line_gap = max(6, int(round(im_h * 0.01)))

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
    safe_name = (name or "").strip()
    safe_sr = (sr_code or "").strip()

    # Keep it clean: only draw when present.
    if safe_name:
        name_font = fit_font(safe_name, max_size=max(18, int(round(im_h * 0.04))), min_size=14)
        nb = draw.textbbox((0, 0), safe_name, font=name_font)
        ny = y0
        nx = x0 + ((x1 - x0) - (nb[2] - nb[0])) // 2
        draw.text((nx, ny), safe_name, fill=(0, 0, 0), font=name_font)
        y0 = ny + (nb[3] - nb[1]) + line_gap

    if safe_sr:
        sr_text = f"SR-CODE: {safe_sr}"
        sr_font = fit_font(sr_text, max_size=max(14, int(round(im_h * 0.03))), min_size=12)
        sb = draw.textbbox((0, 0), sr_text, font=sr_font)
        sy = y0
        sx = x0 + ((x1 - x0) - (sb[2] - sb[0])) // 2
        draw.text((sx, sy), sr_text, fill=(0, 0, 0), font=sr_font)


def build_id_card_png(sr_code, name=None):
    if not os.path.isfile(ID_CARD_TEMPLATE_PATH):
        raise FileNotFoundError(ID_CARD_TEMPLATE_PATH)
    template = Image.open(ID_CARD_TEMPLATE_PATH).convert("RGB")
    im_w, im_h = template.size
    left, top, side, inset = _id_card_qr_placement(im_w, im_h)
    inner = max(32, side - 2 * inset)

    qr = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECT_M,
        box_size=10,
        border=3,
    )
    qr.add_data(sr_code)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    qr_img = qr_img.resize((inner, inner), Image.Resampling.LANCZOS)
    template.paste(qr_img, (left + inset, top + inset))
    _draw_id_text(template, name, sr_code)

    buf = io.BytesIO()
    template.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf


def _safe_filename_stem(text):
    t = re.sub(r"[^\w\-.]+", "_", (text or "").strip(), flags=re.UNICODE)
    t = re.sub(r"_+", "_", t).strip("._-")
    return (t or "")[:120]


@app.get("/api/account/user/<int:user_id>/id-card")
def api_account_id_card(user_id):
    err = require_account_user(user_id)
    if err:
        return err
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT sr_code, name FROM ojt_users WHERE id = ?", (user_id,))
    row = cur.fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    sr = row["sr_code"]
    name = row["name"]
    try:
        buf = build_id_card_png(sr, name=name)
    except FileNotFoundError:
        return jsonify({"error": "ID card template is missing on the server"}), 500
    except OSError:
        return jsonify({"error": "Could not generate ID card"}), 500
    stem = _safe_filename_stem(row["name"])
    if not stem:
        stem = _safe_filename_stem(sr) or "OJT-ID"
    name = f"{stem}.png"
    return send_file(
        buf,
        mimetype="image/png",
        as_attachment=True,
        download_name=name,
    )


@app.get("/api/admin/user/<int:user_id>/id-card")
def api_admin_user_id_card(user_id):
    err = require_admin()
    if err:
        return err
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT sr_code, name FROM ojt_users WHERE id = ?", (user_id,))
    row = cur.fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    sr = row["sr_code"]
    name = row["name"]
    try:
        buf = build_id_card_png(sr, name=name)
    except FileNotFoundError:
        return jsonify({"error": "ID card template is missing on the server"}), 500
    except OSError:
        return jsonify({"error": "Could not generate ID card"}), 500
    stem = _safe_filename_stem(row["name"])
    if not stem:
        stem = _safe_filename_stem(sr) or "OJT-ID"
    name = f"{stem}.png"
    return send_file(
        buf,
        mimetype="image/png",
        as_attachment=True,
        download_name=name,
    )


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

    now = datetime.now()
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
    created = datetime.now().isoformat(timespec="seconds")
    try:
        cur.execute(
            "INSERT INTO batches (name, created_at) VALUES (?, ?)",
            (name, created),
        )
        db.commit()
    except sqlite3.IntegrityError:
        return jsonify({"error": "Batch name already exists"}), 400
    return jsonify({"ok": True, "id": cur.lastrowid})


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
    now = datetime.now()
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
    now = datetime.now()
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
    return jsonify({"ok": True, "id": cur.lastrowid})


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
