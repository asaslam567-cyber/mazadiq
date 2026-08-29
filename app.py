#!/usr/bin/env python3
"""مزاد محمد الفضلي للساعات الأصلية — تطبيق الويب."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sqlite3
import sys
import threading
import uuid
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from urllib.parse import quote

from flask import (
    Flask,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    send_file,
    url_for,
)
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR / "data")))
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", str(BASE_DIR / "static" / "uploads")))


def _load_dotenv_file() -> None:
    for candidate in (BASE_DIR / ".env", DATA_DIR / ".env"):
        if not candidate.is_file():
            continue
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError:
            continue
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


_load_dotenv_file()
THUMB_DIR = UPLOAD_DIR / "_thumbs"
THUMB_PX = 160
DB_PATH = DATA_DIR / "auction.db"

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
ALLOWED_MIME = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
UPLOAD_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGES_PER_WATCH = 6
DISPLAY_IMAGE_MAX_PX = 1600
DISPLAY_IMAGE_MAX_BYTES = 400 * 1024
MIN_BID_INCREMENT = 5.0
WATCH_PAGE_SIZE = 4
SNIPE_WINDOW_SECONDS = 180
SNIPE_EXTEND_SECONDS = 90

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "alfadhli-luxury-watches-2026")
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024
app.config["ADMIN_PASSWORD"] = (os.environ.get("ADMIN_PASSWORD") or "").strip() or "aslam12345"
app.config["FACEBOOK_PAGE_URL"] = os.environ.get(
    "FACEBOOK_PAGE_URL",
    "https://www.facebook.com/mazad.mohammed.alfadhli",
)
app.config["PUBLIC_SITE_URL"] = os.environ.get("PUBLIC_SITE_URL", "").rstrip("/")
app.config["PREFERRED_URL_SCHEME"] = "https" if os.environ.get("RENDER") else "http"

_db_lock = threading.Lock()


def _bid_email_trace(message: str) -> None:
    line = f"[bid-email] {message}"
    print(line, flush=True)
    sys.stdout.flush()
    app.logger.info(line)


def _resend_api_key() -> str:
    raw = (os.environ.get("RESEND_API_KEY") or os.environ.get("RESEND_APIKEY") or "").strip()
    return raw.strip('"').strip("'")


def _send_admin_bid_email(bidder_name: str, watch_name: str) -> str:
    api_key = _resend_api_key()
    _bid_email_trace(
        f"send start from=onboarding@resend.dev to=as.aslam567@gmail.com key_set={bool(api_key)} key_len={len(api_key)}"
    )
    if not api_key:
        _bid_email_trace("skip: RESEND_API_KEY is missing")
        app.logger.warning("Bid email skipped: RESEND_API_KEY is not set")
        return "لم يُعثر على المفتاح. اسم المتغير يجب أن يكون RESEND_API_KEY تماماً."
    text = f"قام {bidder_name} بالمزايدة على ساعة {watch_name}."
    payload = json.dumps(
        {
            "from": "onboarding@resend.dev",
            "to": ["as.aslam567@gmail.com"],
            "subject": text,
            "text": text,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "MazadAlfadhli/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            _bid_email_trace(f"resend ok status={resp.status} body={body}")
        return ""
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", errors="replace")
        _bid_email_trace(f"resend HTTPError status={err.code} body={detail}")
        app.logger.error("Bid email failed: %s %s", err.code, detail)
        try:
            message = json.loads(detail).get("message") or detail
        except json.JSONDecodeError:
            message = detail or str(err.code)
        return str(message)
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        _bid_email_trace(f"resend network error type={type(err).__name__} err={err}")
        app.logger.exception("Bid email failed")
        return str(err)


def utcnow() -> datetime:
    return datetime.utcnow()


def iso(dt: datetime) -> str:
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.replace(microsecond=0).isoformat() + "Z"


def parse_iso(value: str) -> datetime:
    value = (value or "").replace("Z", "")
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return utcnow()


_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def westernize_digits(text: str) -> str:
    return (text or "").translate(_ARABIC_DIGITS)


def auction_end_from_form(existing: dict | None = None) -> str | None:
    hours_raw = (request.form.get("auction_duration_hours") or "").strip()
    if hours_raw:
        try:
            hours = int(hours_raw)
        except ValueError:
            raise ValueError("مدة المزاد يجب أن تكون عدداً صحيحاً من الساعات.")
        if hours < 1:
            raise ValueError("مدة المزاد يجب أن تكون ساعة واحدة على الأقل.")
        return iso(utcnow() + timedelta(hours=hours))
    if existing and existing.get("listing_type") == "auction" and existing.get("auction_ends_at"):
        return existing["auction_ends_at"]
    return None


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH), timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=8000")
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(_exc=None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    (UPLOAD_DIR / ".gitkeep").touch()
    db = sqlite3.connect(str(DB_PATH))
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS watches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT UNIQUE NOT NULL,
            name_ar TEXT NOT NULL,
            brand TEXT NOT NULL,
            condition_ar TEXT NOT NULL,
            movement_ar TEXT NOT NULL,
            year INTEGER NOT NULL,
            description_ar TEXT NOT NULL,
            listing_type TEXT NOT NULL CHECK (listing_type IN ('auction', 'sale')),
            current_price REAL NOT NULL,
            sale_price REAL,
            auction_ends_at TEXT,
            featured INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            watch_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (watch_id) REFERENCES watches(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS bids (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            watch_id INTEGER NOT NULL,
            full_name TEXT NOT NULL,
            phone TEXT NOT NULL,
            address TEXT NOT NULL,
            amount REAL NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (watch_id) REFERENCES watches(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_bids_watch_amount ON bids (watch_id, amount DESC, id DESC);
        CREATE INDEX IF NOT EXISTS idx_watches_listing ON watches (listing_type);
        """
    )
    cols = {row[1] for row in db.execute("PRAGMA table_info(watches)").fetchall()}
    if "bid_increment" not in cols:
        db.execute(
            "ALTER TABLE watches ADD COLUMN bid_increment REAL NOT NULL DEFAULT 5"
        )
    count = db.execute("SELECT COUNT(*) FROM watches").fetchone()[0]
    if count == 0:
        seed(db)
    db.commit()
    db.close()


def seed(db: sqlite3.Connection) -> None:
    now = utcnow()
    items = [
        {
            "slug": "rolex-datejust-36",
            "name_ar": "رولكس دايت جست 36",
            "brand": "رولكس",
            "condition_ar": "ممتازة — شبه جديدة مع علبة وأوراق",
            "movement_ar": "أوتوماتيك (حركة ذاتية التعبئة)",
            "year": 2019,
            "description_ar": "ساعة رولكس دايت جست أصلية بعلبة من الفولاذ والتيتانيوم البصري، مينا شمسي أنيق، وسوار أويستر. قطعة تليق بالمزادات الراقية.",
            "listing_type": "auction",
            "current_price": 6850,
            "sale_price": None,
            "auction_ends_at": iso(now + timedelta(hours=18, minutes=42)),
            "images": ["rolex-datejust-1.jpg", "rolex-datejust-2.jpg"],
        },
        {
            "slug": "omega-speedmaster-professional",
            "name_ar": "أوميغا سبيدماستر بروفيشنال",
            "brand": "أوميغا",
            "condition_ar": "جيدة جداً — صيانة معتمدة",
            "movement_ar": "يدوي التعبئة (كرونوغراف)",
            "year": 2016,
            "description_ar": "السبيدماستر الأسطورية المرتبطة ببرنامج أبولو. كرونوغراف يدوي التعبئة، زجاج هيسالايت، وتفاصيل دقيقة لهواة الساعات الكلاسيكية.",
            "listing_type": "sale",
            "current_price": 5400,
            "sale_price": 5400,
            "auction_ends_at": None,
            "images": ["omega-speedmaster-1.jpg", "omega-speedmaster-2.jpg"],
        },
        {
            "slug": "longines-master-collection",
            "name_ar": "لونجين ماستر كولكشن",
            "brand": "لونجين",
            "condition_ar": "ممتازة — استخدام خفيف",
            "movement_ar": "أوتوماتيك مع تقويم",
            "year": 2021,
            "description_ar": "أناقة سويسرية هادئة: مينا فضي مضلع، مؤشرات رومانية، وحركة أوتوماتيك موثوقة من لونجين مع تاريخ.",
            "listing_type": "auction",
            "current_price": 1950,
            "sale_price": None,
            "auction_ends_at": iso(now + timedelta(hours=6, minutes=15)),
            "images": ["longines-master-1.jpg", "longines-master-2.jpg"],
        },
        {
            "slug": "rolex-submariner-date",
            "name_ar": "رولكس سبمارينر ديت",
            "brand": "رولكس",
            "condition_ar": "ممتازة — كاملة الملحقات",
            "movement_ar": "أوتوماتيك غوص",
            "year": 2020,
            "description_ar": "أيقونة الغوص الاحترافي. مقاومة للماء حتى 300 متر، إطار سيراميك، وحضور قوي على المعصم.",
            "listing_type": "auction",
            "current_price": 12400,
            "sale_price": None,
            "auction_ends_at": iso(now + timedelta(days=1, hours=4)),
            "images": ["rolex-sub-1.jpg", "rolex-sub-2.jpg"],
        },
        {
            "slug": "omega-seamaster-aqua-terra",
            "name_ar": "أوميغا سي ماستر أكوا تيرا",
            "brand": "أوميغا",
            "condition_ar": "جديدة — غير مستخدمة",
            "movement_ar": "أوتوماتيك كواكسيال",
            "year": 2023,
            "description_ar": "أكوا تيرا بخطوط المينا المستوحاة من سطح البحر، حركة كواكسيال ماستر كرونوميتر، وتشطيب فاخر للبيع المباشر.",
            "listing_type": "sale",
            "current_price": 6100,
            "sale_price": 6100,
            "auction_ends_at": None,
            "images": ["omega-aqua-1.jpg", "omega-aqua-2.jpg"],
        },
        {
            "slug": "longines-hydroconquest",
            "name_ar": "لونجين هيدروكونكويست",
            "brand": "لونجين",
            "condition_ar": "جيدة جداً",
            "movement_ar": "أوتوماتيك رياضة / غوص",
            "year": 2018,
            "description_ar": "ساعة رياضية أنيقة بروح الغوص، إطار سيراميك، وإضاءة واضحة. مناسبة للمزاد الحي لهواة القطع العملية الفاخرة.",
            "listing_type": "auction",
            "current_price": 980,
            "sale_price": None,
            "auction_ends_at": iso(now + timedelta(hours=2, minutes=8)),
            "images": ["longines-hydro-1.jpg", "longines-hydro-2.jpg"],
        },
    ]
    for item in items:
        cur = db.execute(
            """
            INSERT INTO watches (
                slug, name_ar, brand, condition_ar, movement_ar, year,
                description_ar, listing_type, current_price, sale_price,
                auction_ends_at, featured, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (
                item["slug"],
                item["name_ar"],
                item["brand"],
                item["condition_ar"],
                item["movement_ar"],
                item["year"],
                item["description_ar"],
                item["listing_type"],
                item["current_price"],
                item["sale_price"],
                item["auction_ends_at"],
                iso(now),
            ),
        )
        watch_id = cur.lastrowid
        for i, filename in enumerate(item["images"]):
            db.execute(
                "INSERT INTO images (watch_id, filename, sort_order) VALUES (?, ?, ?)",
                (watch_id, filename, i),
            )


def watch_increment(row) -> float:
    try:
        value = float(row["bid_increment"])
    except (KeyError, IndexError, TypeError, ValueError):
        value = MIN_BID_INCREMENT
    return value if value > 0 else MIN_BID_INCREMENT


def compute_min_bid(current: float, increment: float) -> float:
    current = round(float(current), 2)
    increment = round(float(increment), 2)
    if current <= 0:
        return increment
    return round(current + increment, 2)


def is_increment_multiple(value: float, increment: float) -> bool:
    if increment <= 0:
        return True
    quotient = round(float(value) / float(increment), 8)
    return abs(quotient - round(quotient)) < 1e-6


def is_valid_bid_amount(amount: float, current: float, increment: float) -> bool:
    amount = round(float(amount), 2)
    current = round(max(float(current), 0), 2)
    increment = round(float(increment), 2)
    if amount + 1e-9 < compute_min_bid(current, increment):
        return False
    raised = round(amount - current, 2)
    return is_increment_multiple(raised, increment)


def bid_increment_error(increment: float) -> str:
    whole = abs(increment - round(increment)) < 1e-9
    label = f"{increment:.0f}" if whole else f"{increment:.2f}"
    return f"يجب أن تكون المزايدة بمضاعفات الـ {label}$"


ADDRESS_DETAIL_ERROR = (
    "يجب عليك كتابة المحافظة واسم المنطقة الخاصة بك بدقة، مثل: بغداد - المنصور."
)


def is_detailed_address(value: str) -> bool:
    text = (value or "").strip()
    if len(text) < 6:
        return False
    parts = [p for p in re.split(r"[\s,،;؛\-–—/\\]+", text) if p]
    words = []
    for part in parts:
        letters = "".join(re.findall(r"[A-Za-z\u0600-\u06FF]", part))
        if len(letters) >= 2:
            words.append(letters)
    return len(words) >= 2


def watch_to_dict(row: sqlite3.Row, images: list[str], extra: dict | None = None) -> dict:
    data = dict(row)
    data["images"] = images
    data["cover"] = images[0] if images else None
    increment = watch_increment(data)
    data["bid_increment"] = increment
    data["min_bid"] = compute_min_bid(float(data["current_price"]), increment)
    data["is_opening"] = float(data["current_price"]) <= 0
    ends = data.get("auction_ends_at")
    data["is_live"] = bool(
        data["listing_type"] == "auction" and ends and parse_iso(ends) > utcnow()
    )
    data["is_ended"] = bool(
        data["listing_type"] == "auction" and ends and parse_iso(ends) <= utcnow()
    )
    if extra:
        data.update(extra)
    return data


def fetch_images(db: sqlite3.Connection, watch_id: int) -> list[str]:
    rows = db.execute(
        "SELECT filename FROM images WHERE watch_id=? ORDER BY sort_order, id",
        (watch_id,),
    ).fetchall()
    return [r["filename"] for r in rows]


def normalize_phone(phone: str) -> str:
    return re.sub(r"\D+", "", phone or "")


def whatsapp_digits(phone: str) -> str:
    """توحيد رقم عراقي لصيغة واتساب الدولية 964XXXXXXXXXX."""
    digits = normalize_phone(phone)
    if not digits:
        return ""
    while digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("964"):
        local = digits[3:]
        if local.startswith("0"):
            local = local[1:]
        return "964" + local if local else ""
    if digits.startswith("0"):
        digits = digits[1:]
    if digits.startswith("964"):
        local = digits[3:]
        if local.startswith("0"):
            local = local[1:]
        return "964" + local if local else ""
    if not digits:
        return ""
    return "964" + digits


def public_watch_url(slug: str) -> str:
    path = url_for("watch_page", slug=slug)
    base = app.config.get("PUBLIC_SITE_URL") or ""
    if base:
        return base + path
    return url_for("watch_page", slug=slug, _external=True)


def whatsapp_outbid_url(phone: str, watch_name: str, slug: str) -> str:
    digits = whatsapp_digits(phone)
    if not digits:
        return ""
    watch_url = public_watch_url(slug)
    text = (
        "مرحباً بك، لقد تم وضع مزايدة أعلى من مزايدتك على ساعة "
        f"{watch_name} في مزاد محمد الفضلي للساعات الأصلية. "
        "إذا كنت تود رفع السعر، اضغط على الرابط أدناه.\n"
        f"{watch_url}"
    )
    return f"https://wa.me/{digits}?text={quote(text)}"


def fetch_leading_bid(db: sqlite3.Connection, watch_id: int) -> dict | None:
    row = db.execute(
        """
        SELECT full_name, phone, amount
        FROM bids
        WHERE watch_id=?
        ORDER BY amount DESC, id DESC
        LIMIT 1
        """,
        (watch_id,),
    ).fetchone()
    return dict(row) if row else None


def fetch_leading_bids(db: sqlite3.Connection, watch_ids: list[int]) -> dict[int, dict]:
    if not watch_ids:
        return {}
    placeholders = ",".join("?" * len(watch_ids))
    rows = db.execute(
        f"""
        SELECT watch_id, full_name, phone, amount
        FROM bids
        WHERE watch_id IN ({placeholders})
          AND id = (
            SELECT id FROM bids AS b2
            WHERE b2.watch_id = bids.watch_id
            ORDER BY amount DESC, id DESC
            LIMIT 1
          )
        """,
        watch_ids,
    ).fetchall()
    return {row["watch_id"]: dict(row) for row in rows}


def current_bidder_phone() -> str:
    raw = session.get("bidder_phone") or request.headers.get("X-Bidder-Phone") or ""
    return normalize_phone(raw)


def is_self_leading(leading: dict | None, mine: str | None = None) -> bool:
    if not leading:
        return False
    if mine is None:
        mine = current_bidder_phone()
    theirs = normalize_phone(leading.get("phone") or "")
    return bool(mine and theirs and mine == theirs)


def fetch_bid_watch_ids(
    db: sqlite3.Connection, watch_ids: list[int], phone_digits: str
) -> set[int]:
    if not phone_digits or not watch_ids:
        return set()
    placeholders = ",".join("?" * len(watch_ids))
    rows = db.execute(
        f"SELECT DISTINCT watch_id, phone FROM bids WHERE watch_id IN ({placeholders})",
        watch_ids,
    ).fetchall()
    return {
        row["watch_id"]
        for row in rows
        if normalize_phone(row["phone"]) == phone_digits
    }


def is_outbid(
    watch_id: int,
    leading: dict | None,
    bid_watch_ids: set[int],
    is_live: bool,
    mine: str | None = None,
) -> bool:
    if not is_live or watch_id not in bid_watch_ids:
        return False
    return not is_self_leading(leading, mine)


def fetch_watch(db: sqlite3.Connection, watch_id: int | None = None, slug: str | None = None):
    if slug:
        row = db.execute("SELECT * FROM watches WHERE slug=?", (slug,)).fetchone()
    else:
        row = db.execute("SELECT * FROM watches WHERE id=?", (watch_id,)).fetchone()
    if not row:
        return None
    return watch_to_dict(row, fetch_images(db, row["id"]))


def fetch_watches(db: sqlite3.Connection, listing_type: str | None = None) -> list[dict]:
    sql = "SELECT * FROM watches"
    params: tuple = ()
    if listing_type:
        sql += " WHERE listing_type=?"
        params = (listing_type,)
    sql += " ORDER BY featured DESC, id DESC"
    rows = db.execute(sql, params).fetchall()
    return [watch_to_dict(r, fetch_images(db, r["id"])) for r in rows]


def slugify(name: str) -> str:
    latin = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if latin:
        return f"{latin}-{uuid.uuid4().hex[:6]}"
    return f"watch-{uuid.uuid4().hex[:10]}"


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("admin_login", next=request.path))
        return fn(*args, **kwargs)

    return wrapper


def image_url(filename: str | None) -> str:
    if not filename:
        return ""
    path = UPLOAD_DIR / filename
    if path.exists() and path.resolve().is_relative_to(UPLOAD_DIR.resolve()):
        return url_for("serve_upload", filename=filename)
    return ""


def thumbnail_url(filename: str | None) -> str:
    if not filename or not image_url(filename):
        return ""
    return url_for("serve_thumb", filename=filename)


def _safe_upload_file(filename: str) -> Path | None:
    name = Path(filename or "").name
    if not name or name != filename or "/" in filename or "\\" in filename:
        return None
    path = (UPLOAD_DIR / name).resolve()
    root = UPLOAD_DIR.resolve()
    thumbs = THUMB_DIR.resolve()
    if not path.is_file() or not path.is_relative_to(root):
        return None
    if path == thumbs or thumbs in path.parents:
        return None
    return path


def _ensure_thumbnail(src: Path) -> Path:
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    dest = THUMB_DIR / f"{src.stem}.jpg"
    if dest.exists() and dest.stat().st_mtime >= src.stat().st_mtime:
        return dest
    from PIL import Image

    with Image.open(src) as img:
        img = img.convert("RGB")
        img.thumbnail((THUMB_PX, THUMB_PX), Image.Resampling.LANCZOS)
        img.save(dest, format="JPEG", quality=72, optimize=True)
    return dest


def delete_image_file(filename: str) -> None:
    path = UPLOAD_DIR / filename
    if path.exists() and path.resolve().is_relative_to(UPLOAD_DIR.resolve()):
        path.unlink()
    thumb = THUMB_DIR / f"{Path(filename).stem}.jpg"
    if thumb.exists():
        thumb.unlink()


@app.context_processor
def inject_globals():
    return {
        "site_name": "مزاد محمد الفضلي للساعات الأصلية",
        "min_increment": MIN_BID_INCREMENT,
        "now_iso": iso(utcnow()),
        "image_url": image_url,
        "thumbnail_url": thumbnail_url,
        "facebook_url": app.config["FACEBOOK_PAGE_URL"],
        "max_images_per_watch": MAX_IMAGES_PER_WATCH,
        "whatsapp_outbid_url": whatsapp_outbid_url,
    }


@app.route("/media/<filename>")
def serve_upload(filename: str):
    src = _safe_upload_file(filename)
    if not src:
        abort(404)
    mime = UPLOAD_MIME.get(src.suffix.lower(), "application/octet-stream")
    return send_file(src, mimetype=mime, max_age=604800)


@app.route("/thumb/<filename>")
def serve_thumb(filename: str):
    src = _safe_upload_file(filename)
    if not src:
        abort(404)
    try:
        dest = _ensure_thumbnail(src)
    except Exception:
        abort(404)
    return send_file(dest, mimetype="image/jpeg", max_age=604800)


@app.route("/")
def home():
    db = get_db()
    latest = fetch_watches(db)
    auctions = fetch_watches(db, "auction")
    sales = fetch_watches(db, "sale")
    return render_template(
        "index.html",
        latest=latest[:WATCH_PAGE_SIZE],
        latest_total=len(latest),
        auctions=auctions[:WATCH_PAGE_SIZE],
        auctions_total=len(auctions),
        sales=sales,
        page_size=WATCH_PAGE_SIZE,
    )


@app.route("/auctions")
def auctions_page():
    watches = fetch_watches(get_db(), "auction")
    return render_template(
        "auctions.html",
        watches=watches[:WATCH_PAGE_SIZE],
        total=len(watches),
        page_size=WATCH_PAGE_SIZE,
    )


@app.route("/shop")
def shop_page():
    watches = fetch_watches(get_db(), "sale")
    return render_template(
        "shop.html",
        watches=watches[:WATCH_PAGE_SIZE],
        total=len(watches),
        page_size=WATCH_PAGE_SIZE,
    )


@app.route("/about")
def about_page():
    return render_template("about.html")


@app.route("/watch/<slug>")
def watch_page(slug: str):
    watch = fetch_watch(get_db(), slug=slug)
    if not watch:
        abort(404)
    bids = []
    if session.get("admin"):
        bids = [
            dict(r)
            for r in get_db()
            .execute(
                "SELECT * FROM bids WHERE watch_id=? ORDER BY amount DESC, id DESC",
                (watch["id"],),
            )
            .fetchall()
        ]
    leading_bid = fetch_leading_bid(get_db(), watch["id"]) if watch["listing_type"] == "auction" else None
    mine = current_bidder_phone()
    self_leading = is_self_leading(leading_bid, mine)
    user_bid_ids = (
        fetch_bid_watch_ids(get_db(), [watch["id"]], mine)
        if watch["listing_type"] == "auction"
        else set()
    )
    gallery_urls = [u for u in (image_url(name) for name in watch["images"]) if u]
    return render_template(
        "watch.html",
        watch=watch,
        bids=bids,
        leading_bid=leading_bid,
        is_self_leading=self_leading,
        is_outbid=is_outbid(
            watch["id"], leading_bid, user_bid_ids, bool(watch.get("is_live")), mine
        ),
        gallery_urls=gallery_urls,
    )


@app.route("/api/watch/<int:watch_id>")
def api_watch(watch_id: int):
    watch = fetch_watch(get_db(), watch_id=watch_id)
    if not watch:
        abort(404)
    return jsonify(
        {
            "id": watch["id"],
            "current_price": watch["current_price"],
            "min_bid": watch["min_bid"],
            "bid_increment": watch["bid_increment"],
            "is_opening": watch["is_opening"],
            "auction_ends_at": watch["auction_ends_at"],
            "is_live": watch["is_live"],
            "is_ended": watch["is_ended"],
            "listing_type": watch["listing_type"],
        }
    )


@app.route("/api/watches")
def api_watches():
    listing_type = request.args.get("type") or ""
    if listing_type not in {"auction", "sale", "all"}:
        abort(400)
    try:
        offset = max(0, int(request.args.get("offset") or 0))
        limit = min(24, max(1, int(request.args.get("limit") or WATCH_PAGE_SIZE)))
    except ValueError:
        abort(400)
    watches = fetch_watches(get_db(), None if listing_type == "all" else listing_type)
    page = watches[offset : offset + limit]
    html = render_template("partials/watch_cards.html", watches=page)
    next_offset = offset + len(page)
    return jsonify(
        {
            "html": html,
            "next_offset": next_offset,
            "done": next_offset >= len(watches),
        }
    )


def auctions_etag(db: sqlite3.Connection, mine: str = "") -> str:
    bid_max = db.execute("SELECT COALESCE(MAX(id), 0) FROM bids").fetchone()[0]
    stats = db.execute(
        """
        SELECT COUNT(*), COALESCE(SUM(current_price), 0), COALESCE(MAX(auction_ends_at), '')
        FROM watches
        WHERE listing_type='auction'
        """
    ).fetchone()
    token = hashlib.sha256(mine.encode()).hexdigest()[:8] if mine else "0"
    return f'W/"{bid_max}-{stats[0]}-{stats[1]}-{stats[2]}-{token}"'


def fetch_auction_states(db: sqlite3.Connection) -> list[dict]:
    rows = db.execute(
        """
        SELECT id, current_price, bid_increment, auction_ends_at, listing_type
        FROM watches
        WHERE listing_type='auction'
        ORDER BY featured DESC, id DESC
        """
    ).fetchall()
    return [watch_to_dict(r, []) for r in rows]


@app.route("/api/auctions")
def api_auctions():
    db = get_db()
    mine = current_bidder_phone()
    etag = auctions_etag(db, mine)
    if request.headers.get("If-None-Match") == etag:
        resp = app.response_class(status=304)
        resp.headers["ETag"] = etag
        resp.headers["Cache-Control"] = "no-cache"
        return resp
    watches = fetch_auction_states(db)
    watch_ids = [w["id"] for w in watches]
    leading_map = fetch_leading_bids(db, watch_ids)
    bid_watch_ids = fetch_bid_watch_ids(db, watch_ids, mine)
    payload = []
    for w in watches:
        leading = leading_map.get(w["id"])
        self_leading = is_self_leading(leading, mine)
        payload.append(
            {
                "id": w["id"],
                "current_price": w["current_price"],
                "min_bid": w["min_bid"],
                "bid_increment": w["bid_increment"],
                "is_opening": w["is_opening"],
                "auction_ends_at": w["auction_ends_at"],
                "is_live": w["is_live"],
                "is_ended": w["is_ended"],
                "leading_bidder_name": (leading or {}).get("full_name") or "",
                "is_self_leading": self_leading,
                "is_outbid": is_outbid(
                    w["id"], leading, bid_watch_ids, bool(w["is_live"]), mine
                ),
            }
        )
    resp = jsonify(payload)
    resp.headers["ETag"] = etag
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.post("/bid/<int:watch_id>")
def place_bid(watch_id: int):
    full_name = (request.form.get("full_name") or "").strip()
    phone = westernize_digits((request.form.get("phone") or "").strip())
    address = (request.form.get("address") or "").strip()
    amount_raw = (request.form.get("amount") or "").strip()
    wants_json = request.headers.get("X-Requested-With") == "fetch" or request.accept_mimetypes.best == "application/json"

    def fail(message: str, code: int = 400):
        if wants_json or request.is_json:
            return jsonify({"ok": False, "error": message}), code
        flash(message, "error")
        watch = fetch_watch(get_db(), watch_id=watch_id)
        return redirect(url_for("watch_page", slug=watch["slug"] if watch else ""))

    if not full_name or len(full_name) < 3:
        return fail("يرجى إدخال الاسم الكامل.")
    if not re.fullmatch(r"[0-9+\-\s]{8,20}", phone):
        return fail("يرجى إدخال رقم هاتف صحيح.")
    if not is_detailed_address(address):
        return fail(ADDRESS_DETAIL_ERROR)
    try:
        amount = round(float(amount_raw), 2)
    except ValueError:
        return fail("قيمة المزايدة غير صالحة.")

    watch_name = None
    with _db_lock:
        db = get_db()
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute("SELECT * FROM watches WHERE id=?", (watch_id,)).fetchone()
            if not row:
                db.execute("ROLLBACK")
                return fail("الساعة غير موجودة.", 404)
            if row["listing_type"] != "auction":
                db.execute("ROLLBACK")
                return fail("هذه الساعة للبيع المباشر وليست في المزاد.")
            ends = row["auction_ends_at"]
            if not ends or parse_iso(ends) <= utcnow():
                db.execute("ROLLBACK")
                return fail("انتهى هذا المزاد ولا يمكن تقديم مزايدة جديدة.")
            current = float(row["current_price"])
            increment = watch_increment(row)
            minimum = compute_min_bid(current, increment)
            if amount < minimum or not is_valid_bid_amount(amount, current, increment):
                db.execute("ROLLBACK")
                return fail(bid_increment_error(increment))
            now = utcnow()
            end_dt = parse_iso(ends)
            remain = (end_dt - now).total_seconds()
            new_ends = ends
            if remain <= SNIPE_WINDOW_SECONDS:
                new_ends = iso(end_dt + timedelta(seconds=SNIPE_EXTEND_SECONDS))
            db.execute(
                """
                INSERT INTO bids (watch_id, full_name, phone, address, amount, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (watch_id, full_name, phone, address, amount, iso(now)),
            )
            db.execute(
                "UPDATE watches SET current_price=?, auction_ends_at=? WHERE id=?",
                (amount, new_ends, watch_id),
            )
            db.execute("COMMIT")
            watch_name = row["name_ar"] or ""
            _bid_email_trace(
                f"bid committed watch_id={watch_id} bidder={full_name!r} watch={watch_name!r}"
            )
        except Exception:
            db.execute("ROLLBACK")
            raise

    email_ok = False
    if watch_name is None:
        email_notice = "لم يُرسل إيميل الإشعار لأن المزايدة لم تُحفظ."
        _bid_email_trace(f"skip send: bid did not commit watch_id={watch_id}")
    else:
        try:
            send_error = _send_admin_bid_email(full_name, watch_name)
        except Exception as err:
            send_error = f"{type(err).__name__}: {err}"
            _bid_email_trace(f"send raised {send_error}")
            app.logger.exception("Bid email failed")
        if send_error:
            email_notice = f"المزايدة سُجّلت، لكن إيميل الإشعار فشل: {send_error}"
            _bid_email_trace(f"send returned error={send_error!r}")
        else:
            email_ok = True
            email_notice = "المزايدة سُجّلت، وتم إرسال إيميل الإشعار إلى as.aslam567@gmail.com."
            _bid_email_trace("send returned success")

    session.permanent = True
    session["bidder_phone"] = phone
    session.modified = True
    watch = fetch_watch(get_db(), watch_id=watch_id)
    leading_bid = fetch_leading_bid(get_db(), watch_id)
    visitor_ok = "تم إرسال مزايدتك بنجاح"
    if wants_json:
        return jsonify(
            {
                "ok": True,
                "message": visitor_ok,
                "current_price": watch["current_price"],
                "min_bid": watch["min_bid"],
                "leading_bidder_name": (leading_bid or {}).get("full_name") or full_name,
                "is_self_leading": True,
                "is_outbid": False,
                "auction_ends_at": watch["auction_ends_at"],
                "extended": bool(new_ends != ends),
            }
        )
    flash(visitor_ok, "success")
    return redirect(url_for("watch_page", slug=watch["slug"]))


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if session.get("admin"):
        return redirect(url_for("admin_dashboard"))
    if request.method == "POST":
        password = (request.form.get("password") or "").strip()
        if password == app.config["ADMIN_PASSWORD"]:
            session["admin"] = True
            session.permanent = True
            return redirect(request.args.get("next") or url_for("admin_dashboard"))
        flash("كلمة المرور غير صحيحة.", "error")
    return render_template("admin_login.html")


@app.post("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("home"))


@app.route("/admin")
@login_required
def admin_dashboard():
    db = get_db()
    watches = fetch_watches(db)
    bid_count = db.execute("SELECT COUNT(*) AS c FROM bids").fetchone()["c"]
    return render_template("admin.html", watches=watches, bid_count=bid_count)


@app.post("/admin/test-email")
@login_required
def admin_test_email():
    error = _send_admin_bid_email("تجربة", "اختبار الإشعار")
    if error:
        flash(f"لم يُرسل الإيميل: {error}", "error")
    else:
        flash("تم إرسال إيميل التجربة إلى as.aslam567@gmail.com. تحقق من الوارد والرسائل غير المرغوب فيها.", "success")
    return redirect(url_for("admin_dashboard"))


def fetch_bid_history(db) -> list[dict]:
    rows = db.execute(
        """
        SELECT bids.*, watches.name_ar AS watch_name, watches.brand, watches.slug,
               watches.current_price
        FROM bids
        JOIN watches ON watches.id = bids.watch_id
        ORDER BY bids.created_at DESC, bids.id DESC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def fetch_bid(db, bid_id: int) -> dict | None:
    row = db.execute(
        """
        SELECT bids.*, watches.name_ar AS watch_name, watches.brand, watches.slug
        FROM bids
        JOIN watches ON watches.id = bids.watch_id
        WHERE bids.id=?
        """,
        (bid_id,),
    ).fetchone()
    return dict(row) if row else None


def sync_watch_price(db, watch_id: int) -> None:
    row = db.execute(
        "SELECT COALESCE(MAX(amount), 0) AS top, COUNT(*) AS c FROM bids WHERE watch_id=?",
        (watch_id,),
    ).fetchone()
    price = float(row["top"]) if row["c"] else 0.0
    db.execute("UPDATE watches SET current_price=? WHERE id=?", (price, watch_id))


@app.route("/admin/bids")
@login_required
def admin_bids():
    db = get_db()
    bids = fetch_bid_history(db)
    bid_count = db.execute("SELECT COUNT(*) AS c FROM bids").fetchone()["c"]
    return render_template("admin_bids.html", bids=bids, bid_count=bid_count)


@app.route("/admin/bids/<int:bid_id>/edit", methods=["GET", "POST"])
@login_required
def admin_edit_bid(bid_id: int):
    bid = fetch_bid(get_db(), bid_id)
    if not bid:
        abort(404)
    if request.method == "POST":
        full_name = (request.form.get("full_name") or "").strip()
        phone = (request.form.get("phone") or "").strip()
        address = (request.form.get("address") or "").strip()
        amount_raw = (request.form.get("amount") or "").strip()
        try:
            amount = round(float(amount_raw), 2)
        except ValueError:
            flash("قيمة المزايدة غير صالحة.", "error")
            return render_template("admin_bid_form.html", bid=bid)
        if not full_name or len(full_name) < 3 or not address or len(address) < 6 or amount < 0:
            flash("يرجى تعبئة بيانات المزايدة بدقة.", "error")
            return render_template("admin_bid_form.html", bid=bid)
        with _db_lock:
            db = get_db()
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    "UPDATE bids SET full_name=?, phone=?, address=?, amount=? WHERE id=?",
                    (full_name, phone, address, amount, bid_id),
                )
                sync_watch_price(db, bid["watch_id"])
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
        flash("تم تعديل المزايدة وتحديث سعر الساعة.", "success")
        return redirect(url_for("admin_bids"))
    return render_template("admin_bid_form.html", bid=bid)


@app.post("/admin/bids/<int:bid_id>/delete")
@login_required
def admin_delete_bid(bid_id: int):
    bid = fetch_bid(get_db(), bid_id)
    if not bid:
        abort(404)
    with _db_lock:
        db = get_db()
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute("DELETE FROM bids WHERE id=?", (bid_id,))
            sync_watch_price(db, bid["watch_id"])
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
    flash("تم حذف المزايدة وتحديث سعر الساعة.", "success")
    return redirect(url_for("admin_bids"))


@app.route("/admin/watch/new", methods=["GET", "POST"])
@login_required
def admin_new_watch():
    if request.method == "POST":
        return save_watch()
    return render_template("admin_watch_form.html", watch=None)


@app.route("/admin/watch/<int:watch_id>/edit", methods=["GET", "POST"])
@login_required
def admin_edit_watch(watch_id: int):
    watch = fetch_watch(get_db(), watch_id=watch_id)
    if not watch:
        abort(404)
    if request.method == "POST":
        return save_watch(watch)
    return render_template("admin_watch_form.html", watch=watch)


@app.post("/admin/watch/<int:watch_id>/delete")
@login_required
def admin_delete_watch(watch_id: int):
    with _db_lock:
        db = get_db()
        db.execute("BEGIN IMMEDIATE")
        try:
            files = fetch_images(db, watch_id)
            db.execute("DELETE FROM bids WHERE watch_id=?", (watch_id,))
            db.execute("DELETE FROM images WHERE watch_id=?", (watch_id,))
            db.execute("DELETE FROM watches WHERE id=?", (watch_id,))
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
    for name in files:
        delete_image_file(name)
    flash("تم حذف الساعة.", "success")
    return redirect(url_for("admin_dashboard"))


def save_watch(existing: dict | None = None):
    name_ar = (request.form.get("name_ar") or "").strip()
    description_ar = (request.form.get("description_ar") or "").strip()
    listing_type = request.form.get("listing_type") or "sale"
    try:
        price_raw = (request.form.get("current_price") or "").strip()
        if price_raw == "":
            raise ValueError("empty price")
        current_price = float(price_raw)
        inc_raw = (request.form.get("bid_increment") or "").strip()
        bid_increment = float(inc_raw) if inc_raw else MIN_BID_INCREMENT
    except ValueError:
        flash("السعر أو قيمة الرفعة غير صالحة.", "error")
        return render_template("admin_watch_form.html", watch=existing)

    if current_price < 0:
        flash("سعر البداية يمكن أن يكون صفراً أو أكثر، لكن لا يمكن أن يكون سالباً.", "error")
        return render_template("admin_watch_form.html", watch=existing)
    if bid_increment < 1:
        flash("أقل رفعة يجب أن تكون دولاراً واحداً على الأقل.", "error")
        return render_template("admin_watch_form.html", watch=existing)

    if not name_ar:
        flash("يرجى إدخال اسم الساعة.", "error")
        return render_template("admin_watch_form.html", watch=existing)

    brand = (existing.get("brand") if existing else "") or ""
    condition_ar = (existing.get("condition_ar") if existing else "") or ""
    movement_ar = (existing.get("movement_ar") if existing else "") or ""
    year = int(existing["year"]) if existing and existing.get("year") else 0

    sale_price = current_price if listing_type == "sale" else None
    auction_ends_at = None
    if listing_type == "auction":
        try:
            auction_ends_at = auction_end_from_form(existing)
        except ValueError as exc:
            flash(str(exc), "error")
            return render_template("admin_watch_form.html", watch=existing)
        if not auction_ends_at:
            flash("أدخل مدة المزاد بالساعات (مثل 5 أو 12 أو 24).", "error")
            return render_template("admin_watch_form.html", watch=existing)

    files = request.files.getlist("images")
    incoming = [f for f in files if f and f.filename]
    remove_set: set[str] = set()
    if existing:
        allowed = set(existing["images"])
        for name in request.form.getlist("remove_existing"):
            if name in allowed:
                remove_set.add(name)
    existing_kept = (len(existing["images"]) - len(remove_set)) if existing else 0
    saved_names: list[str] = []
    try:
        if existing_kept + len(incoming) > MAX_IMAGES_PER_WATCH:
            raise ValueError(f"الحد الأقصى للصور هو {MAX_IMAGES_PER_WATCH} لكل ساعة.")
        for f in incoming:
            saved_names.append(store_image(f))
    except ValueError as exc:
        for name in saved_names:
            delete_image_file(name)
        flash(str(exc), "error")
        return render_template("admin_watch_form.html", watch=existing)

    with _db_lock:
        db = get_db()
        db.execute("BEGIN IMMEDIATE")
        try:
            if existing:
                db.execute(
                    """
                    UPDATE watches SET name_ar=?, brand=?, condition_ar=?, movement_ar=?,
                        year=?, description_ar=?, listing_type=?, current_price=?,
                        sale_price=?, auction_ends_at=?, bid_increment=?
                    WHERE id=?
                    """,
                    (
                        name_ar,
                        brand,
                        condition_ar,
                        movement_ar,
                        year,
                        description_ar,
                        listing_type,
                        current_price,
                        sale_price,
                        auction_ends_at,
                        bid_increment,
                        existing["id"],
                    ),
                )
                watch_id = existing["id"]
                for name in remove_set:
                    db.execute(
                        "DELETE FROM images WHERE watch_id=? AND filename=?",
                        (watch_id, name),
                    )
            else:
                slug = slugify(name_ar)
                cur = db.execute(
                    """
                    INSERT INTO watches (
                        slug, name_ar, brand, condition_ar, movement_ar, year,
                        description_ar, listing_type, current_price, sale_price,
                        auction_ends_at, featured, created_at, bid_increment
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        slug,
                        name_ar,
                        brand,
                        condition_ar,
                        movement_ar,
                        year,
                        description_ar,
                        listing_type,
                        current_price,
                        sale_price,
                        auction_ends_at,
                        iso(utcnow()),
                        bid_increment,
                    ),
                )
                watch_id = cur.lastrowid
            start_order = 0
            if existing:
                row = db.execute(
                    "SELECT COALESCE(MAX(sort_order), -1) AS m FROM images WHERE watch_id=?",
                    (watch_id,),
                ).fetchone()
                start_order = int(row["m"]) + 1
            for i, filename in enumerate(saved_names):
                db.execute(
                    "INSERT INTO images (watch_id, filename, sort_order) VALUES (?, ?, ?)",
                    (watch_id, filename, start_order + i),
                )
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            for name in saved_names:
                delete_image_file(name)
            raise

    for name in remove_set:
        delete_image_file(name)

    flash("تم حفظ الساعة بنجاح.", "success")
    return redirect(url_for("admin_dashboard"))


def store_image(file_storage) -> str:
    filename = secure_filename(file_storage.filename or "")
    ext = Path(filename).suffix.lower()
    if ext == ".jpeg":
        ext = ".jpg"
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError("يُسمح فقط بصور JPG أو PNG أو WebP.")
    data = file_storage.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("حجم الصورة أكبر من 8 ميغابايت.")
    if len(data) < 24:
        raise ValueError("ملف الصورة تالف أو فارغ.")
    if not _looks_like_image(data, ext):
        raise ValueError("تعذر التحقق من نوع الصورة. ارفع ملفاً أصلياً صالحاً.")
    data = _compress_display_image(data)
    new_name = f"{uuid.uuid4().hex}.jpg"
    dest = UPLOAD_DIR / new_name
    dest.write_bytes(data)
    return new_name


def _image_to_rgb(img):
    from PIL import Image, ImageOps

    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[-1])
        return bg
    return img.convert("RGB")


def _jpeg_bytes(img, quality: int) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True, progressive=True)
    return buf.getvalue()


def _compress_display_image(data: bytes) -> bytes:
    from PIL import Image, UnidentifiedImageError

    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except (UnidentifiedImageError, OSError) as err:
        raise ValueError("تعذر قراءة الصورة. ارفع ملفاً أصلياً صالحاً.") from err
    img = _image_to_rgb(img)
    img.thumbnail((DISPLAY_IMAGE_MAX_PX, DISPLAY_IMAGE_MAX_PX), Image.Resampling.LANCZOS)
    qualities = (86, 82, 78, 74)
    widths = (DISPLAY_IMAGE_MAX_PX, 1400, 1200)
    best = b""
    for max_side in widths:
        work = img.copy()
        work.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        for quality in qualities:
            best = _jpeg_bytes(work, quality)
            if len(best) <= DISPLAY_IMAGE_MAX_BYTES:
                return best
    return best


def _looks_like_image(data: bytes, ext: str) -> bool:
    if ext in {".jpg", ".jpeg"} and data[:3] == b"\xff\xd8\xff":
        return True
    if ext == ".png" and data[:8] == b"\x89PNG\r\n\x1a\n":
        return True
    if ext == ".webp" and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    return False


@app.route("/health")
def health():
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        conn.execute("SELECT 1")
        conn.close()
    except (OSError, sqlite3.Error):
        return "error", 503
    return "ok", 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.errorhandler(404)
def not_found(_e):
    return render_template("404.html"), 404


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5050, debug=True)
else:
    init_db()
