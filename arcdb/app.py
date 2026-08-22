"""ArchiveDB server - browse, search, read, and download translated novels.

This is intentionally kept as a single file because the deployment launches
this exact script. It is organised into clearly separated sections:

    1.  Configuration & environment loading
    2.  Flask app setup & startup warnings
    3.  Text utilities
    4.  Security / path safety
    5.  Atomic persistence (user data, custom metadata, user uploads)
    5b. User-upload file validation and EPUB extraction
    6.  Auth (allowed e-mails, accounts, login gate, auto-ban)
    7.  Rate limiting & download accounting
    8.  Metadata loading & gallery assembly (cached)
    8b. Novelpia notice-image gallery (manifests + direct CDN URLs)
    8c. Tag similarity & recommendations
    8d. Community (usernames, shared novels/collections, general chat)
    9.  Reader pipeline (TOC, chapters, assets)
   10.  Telegram service link parsing
   11.  API response helpers
   12.  Routes
"""

import collections
from collections import defaultdict, deque
import csv
import hashlib
import ipaddress
import json
import logging
import math
import os
import random
import re
import secrets
import shutil
import smtplib
import threading
import time
import uuid
import zipfile
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from functools import wraps
from html import escape
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import requests
from flask import (
    Flask,
    Response,
    after_this_request,
    g,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from jinja2 import ChainableUndefined
from werkzeug.security import generate_password_hash, check_password_hash

from arcdb.epub_io import (
    EpubLimits,
    EpubSafetyError,
    copy_upload_limited,
    copy_zip_entry_atomic,
    extract_epub_safely,
    iter_epub_text_entries,
    validate_epub_archive,
)
from arcdb.jobs import JobStore
from arcdb.library_index import LibraryIndex, LibraryIndexUnavailable
from arcdb.security import (
    OriginConfigurationError,
    parse_allowed_origins,
    request_source_allowed,
)
from arcdb.storage.runtime_reads import StateReadError, check_state_read_backend_ready
from arcdb.telegram_gateway import TelegramGateway, TelegramGatewayError

# ====================================================================
# 1. CONFIGURATION & ENVIRONMENT LOADING
# ====================================================================

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_STARTUP_WARNINGS = []

def _warn(message):
    _STARTUP_WARNINGS.append(message)

def _env_str(name, default=None):
    """Read a trimmed string environment variable with a fallback."""
    value = os.environ.get(name, "").strip()
    return value if value else default

def _env_int(name, default):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        _warn(f"{name}={raw!r} is not an integer - using default {default}.")
        return default

# --- Secrets ----------------------------------------------------------------
FLASK_SECRET_KEY = _env_str("FLASK_SECRET_KEY")
if not FLASK_SECRET_KEY:
    raise RuntimeError(
        "FLASK_SECRET_KEY is not set. Generate one with "
        "`python3 -c \"import secrets; print(secrets.token_hex(32))\"` and "
        "export it before launching. Changing this value logs out all users."
    )

TELEGRAM_SERVICE_URL = _env_str("TELEGRAM_SERVICE_URL")
TELEGRAM_SERVICE_TOKEN = _env_str("TELEGRAM_SERVICE_TOKEN")
TELEGRAM_SERVICE_CONNECT_TIMEOUT = _env_int("TELEGRAM_SERVICE_CONNECT_TIMEOUT", 3)
TELEGRAM_SERVICE_READ_TIMEOUT = _env_int("TELEGRAM_SERVICE_READ_TIMEOUT", 30)
TELEGRAM_GATEWAY = None
if TELEGRAM_SERVICE_URL and TELEGRAM_SERVICE_TOKEN:
    try:
        TELEGRAM_GATEWAY = TelegramGateway(
            TELEGRAM_SERVICE_URL,
            TELEGRAM_SERVICE_TOKEN,
            connect_timeout=TELEGRAM_SERVICE_CONNECT_TIMEOUT,
            read_timeout=TELEGRAM_SERVICE_READ_TIMEOUT,
        )
    except ValueError as exc:
        _warn(f"Telegram service configuration rejected: {exc}.")
else:
    _warn(
        "TELEGRAM_SERVICE_URL / TELEGRAM_SERVICE_TOKEN are not both set - "
        "Telegram-backed downloads stay disabled."
    )

# --- Email (SMTP) for verification codes ------------------------------------
SMTP_HOST = _env_str("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = _env_int("SMTP_PORT", 587)
SMTP_USER = _env_str("SMTP_USER")              # sending address
SMTP_PASS = _env_str("SMTP_PASS")              # Gmail App Password (NOT account pw)
if not (SMTP_USER and SMTP_PASS):
    if (os.environ.get("ARCHIVEDB_AUTH_TEST_MODE", "0") == "1"
            and os.environ.get("ARCHIVEDB_LOCAL_DEV", "0") == "1"):
        _warn("SMTP is disabled; local auth test message bodies are suppressed.")
    else:
        _warn("SMTP_USER/SMTP_PASS not set - verification codes will only print to the console.")

# --- Paths -------------------------------------------------------------------
TRANSLATED_CSV_PATH = _env_str("TRANSLATED_CSV_PATH", os.path.join(_BASE_DIR, "uploaded_novels_tracker.csv"))
RAW_MASTER_CSV_PATH = _env_str("RAW_MASTER_CSV_PATH", "/home/ubuntu/master_library_index.csv")
LOCAL_OUTPUT_DIR = _env_str("LOCAL_OUTPUT_DIR", "/home/ubuntu/nvidia_chat_bot/output/")
STRUCTURED_OUTPUT_DIR = _env_str("STRUCTURED_OUTPUT_DIR", "/home/ubuntu/all_translated_epubs/structured_output/")
BATCHED_EPUBS_DIR = _env_str("BATCHED_EPUBS_DIR", "/home/ubuntu/batched_epubs/")
META_DIR = _env_str("META_DIR", "/home/ubuntu/metadata/")
LIBRARY_INDEX_DB_PATH = _env_str(
    "LIBRARY_INDEX_DB_PATH",
    os.path.join(META_DIR, "library_index.sqlite3"),
)
LIBRARY_INDEX = LibraryIndex(LIBRARY_INDEX_DB_PATH)

JSON_DB_PATH = os.path.join(META_DIR, "novels_full.json")
TITLES_EN_PATH = os.path.join(META_DIR, "titles_en.txt")
TAGS_EN_PATH = os.path.join(META_DIR, "tags_en.txt")
DESC_EN_PATH = os.path.join(META_DIR, "descriptions.txt")
CUSTOM_META_PATH = os.path.join(META_DIR, "custom_meta.json")
USER_DATA_PATH = os.path.join(META_DIR, "user_data.json")
COLLECTIONS_PATH = os.path.join(META_DIR, "collections.json")
LEGACY_BOOKMARKS_PATH = os.path.join(META_DIR, "bookmarks.json")
DOWNLOAD_ABUSE_LOG_PATH = _env_str("DOWNLOAD_ABUSE_LOG_PATH", os.path.join(META_DIR, "download_abuse.jsonl"))
DOWNLOAD_LOG_PATH = _env_str("DOWNLOAD_LOG_PATH", os.path.join(META_DIR, "download_log.jsonl"))
ACCESS_REVOCATION_LOG_PATH = _env_str(
    "ACCESS_REVOCATION_LOG_PATH", os.path.join(META_DIR, "access_revocations.jsonl")
)
ALLOWED_EMAILS_PATH = _env_str("ALLOWED_EMAILS_PATH", os.path.join(META_DIR, "allowed_gmails.txt"))
USERS_PATH = _env_str("USERS_PATH", os.path.join(META_DIR, "users.json"))

# --- User-uploaded novels storage paths & limits -----------------------------
USER_UPLOADS_PATH = _env_str(
    "USER_UPLOADS_PATH",
    os.path.join(META_DIR, "user_uploads.json"),
)
USER_UPLOAD_EPUB_DIR = _env_str(
    "USER_UPLOAD_EPUB_DIR",
    os.path.join(META_DIR, "user_uploaded_epubs"),
)
USER_UPLOAD_COVER_DIR = _env_str(
    "USER_UPLOAD_COVER_DIR",
    os.path.join(META_DIR, "user_uploaded_covers"),
)

# Tuned to fit Cloudflare Free Tunnel's 100 MB request edge limit
MAX_EPUB_UPLOAD_BYTES = _env_int(
    "MAX_EPUB_UPLOAD_BYTES",
    90 * 1024 * 1024,  # 90 MB per EPUB
)
MAX_COVER_UPLOAD_BYTES = _env_int(
    "MAX_COVER_UPLOAD_BYTES",
    8 * 1024 * 1024,  # 8 MB
)
MAX_EPUB_FILES = _env_int("MAX_EPUB_FILES", 10_000)
MAX_EPUB_UNCOMPRESSED_BYTES = _env_int(
    "MAX_EPUB_UNCOMPRESSED_BYTES",
    750 * 1024 * 1024,  # 750 MB after extraction
)
MAX_EPUB_ENTRY_BYTES = _env_int(
    "MAX_EPUB_ENTRY_BYTES",
    128 * 1024 * 1024,
)
MAX_EPUB_TEXT_ENTRY_BYTES = _env_int(
    "MAX_EPUB_TEXT_ENTRY_BYTES",
    8 * 1024 * 1024,
)
MAX_EPUB_COMPRESSION_RATIO = _env_int("MAX_EPUB_COMPRESSION_RATIO", 250)
MAX_EPUB_PACKAGE_IMAGE_BYTES = _env_int(
    "MAX_EPUB_PACKAGE_IMAGE_BYTES",
    12 * 1024 * 1024,
)
MAX_EPUB_PACKAGE_SESSION_BYTES = _env_int(
    "MAX_EPUB_PACKAGE_SESSION_BYTES",
    256 * 1024 * 1024,
)
MAX_EPUB_PACKAGE_SESSION_FILES = _env_int(
    "MAX_EPUB_PACKAGE_SESSION_FILES",
    1_000,
)
MAX_EPUB_PACKAGE_SESSIONS_PER_USER = _env_int(
    "MAX_EPUB_PACKAGE_SESSIONS_PER_USER",
    3,
)
EPUB_PACKAGE_SESSION_TTL_SECONDS = _env_int(
    "EPUB_PACKAGE_SESSION_TTL_SECONDS",
    24 * 60 * 60,
)
PACKAGE_JOBS_DB_PATH = _env_str(
    "PACKAGE_JOBS_DB_PATH",
    os.path.join(META_DIR, "package_jobs.sqlite3"),
)
PACKAGE_JOB_MAX_ATTEMPTS = _env_int("PACKAGE_JOB_MAX_ATTEMPTS", 3)
PACKAGE_JOB_TIMEOUT_SECONDS = _env_int("PACKAGE_JOB_TIMEOUT_SECONDS", 15 * 60)
PACKAGE_JOB_RETENTION_SECONDS = _env_int("PACKAGE_JOB_RETENTION_SECONDS", 24 * 60 * 60)
EPUB_LIMITS = EpubLimits(
    max_entries=MAX_EPUB_FILES,
    max_entry_bytes=MAX_EPUB_ENTRY_BYTES,
    max_total_uncompressed_bytes=MAX_EPUB_UNCOMPRESSED_BYTES,
    max_compression_ratio=MAX_EPUB_COMPRESSION_RATIO,
    max_text_entry_bytes=MAX_EPUB_TEXT_ENTRY_BYTES,
)
MAX_UPLOAD_TAGS = _env_int("MAX_UPLOAD_TAGS", 30)
MAX_UPLOAD_TAG_LENGTH = _env_int("MAX_UPLOAD_TAG_LENGTH", 60)
MAX_UPLOAD_TITLE_LENGTH = _env_int("MAX_UPLOAD_TITLE_LENGTH", 300)
MAX_UPLOAD_AUTHOR_LENGTH = _env_int("MAX_UPLOAD_AUTHOR_LENGTH", 200)
MAX_UPLOAD_DESCRIPTION_LENGTH = _env_int(
    "MAX_UPLOAD_DESCRIPTION_LENGTH",
    20_000,
)

# --- Access & limits ---------------------------------------------------------
ADMIN_EMAILS = [
    e.strip().lower()
    for e in _env_str("ADMIN_EMAILS", "").split(",")
    if e.strip()
]
if not ADMIN_EMAILS:
    _warn("ADMIN_EMAILS is not set - no account has admin access until it is exported.")

DMCA_EMAIL = _env_str("DMCA_EMAIL")
DAILY_DOWNLOAD_LIMIT = _env_int("DAILY_DOWNLOAD_LIMIT", 20)
SESSION_LIFETIME_DAYS = _env_int("SESSION_LIFETIME_DAYS", 30)
try:
    ALLOWED_ORIGINS = parse_allowed_origins(_env_str("ARCHIVEDB_ALLOWED_ORIGINS", ""))
except OriginConfigurationError as exc:
    raise RuntimeError("ARCHIVEDB_ALLOWED_ORIGINS is invalid") from exc

CODE_TTL_SECONDS = _env_int("CODE_TTL_SECONDS", 600)
MAX_CODE_ATTEMPTS = _env_int("MAX_CODE_ATTEMPTS", 5)
MIN_PASSWORD_LEN = _env_int("MIN_PASSWORD_LEN", 8)

MAX_EMAILS_PER_IP      = _env_int("MAX_EMAILS_PER_IP", 0)
MULTI_ACCOUNT_ENFORCE = _env_str("MULTI_ACCOUNT_ENFORCE", "log").lower()
IP_EMAIL_WINDOW_HRS    = _env_int("IP_EMAIL_WINDOW_HRS", 24)
IP_GROUP_IPV6_64       = _env_str("IP_GROUP_IPV6_64", "1") == "1"
IP_EMAIL_MAP_PATH      = _env_str("IP_EMAIL_MAP_PATH", os.path.join(META_DIR, "ip_email_map.json"))
IP_EXEMPTIONS_PATH     = _env_str("IP_EXEMPTIONS_PATH", os.path.join(META_DIR, "ip_exemptions.json"))

MAX_REQUESTS_PER_WINDOW = 75
RATE_WINDOW_SECONDS = 60
COOLDOWN_SECONDS = 60

AUTO_BAN_IGNORE_PREFIXES = (
    "/favicon.ico",
    "/static/",
    "/healthz",
    "/readyz",
    "/api/read/",
    "/read/",
    "/api/user_progress",
    "/api/epub_package/",
)

AUTO_BAN_IGNORE_CONTAINS = (
    "/asset/",
    "/img/",
    "/chapter/",
)

ip_requests = defaultdict(deque)
ip_cooldowns = {}
AUTO_BAN_LOCK = threading.Lock()

CHAPTER_EXTENSIONS = (".html", ".xhtml", ".htm")
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
ASSET_IMAGE_EXTENSIONS = IMAGE_EXTENSIONS + (".svg", ".bmp")
REWRITABLE_ASSET_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".css")
NON_CHAPTER_FILES = {
    "nav.xhtml", "nav.html", "toc.xhtml", "toc.html", "titlepage.xhtml",
    "title.xhtml", "cover.xhtml", "cover.html", "copyright.xhtml", "copyright.html",
    "acknowledgements.xhtml", "acknowledgements.html", "about.xhtml", "about.html",
    "frontmatter.xhtml", "frontmatter.html", "backmatter.xhtml", "backmatter.html"
}

TOC_MISSING_PREFIX = "MISSING||"
MAX_GAP_FILL = 100
VALID_READING_STATUSES = {"none", "want_to_read", "reading", "finished"}
LEGACY_UPLOAD_DATE = "2024-01-01"
MAX_BULK_REMOVE_IDS = 200

def is_source_chapter_filename(path):
    name = os.path.basename(path).lower()
    return name.endswith(CHAPTER_EXTENSIONS) and name not in NON_CHAPTER_FILES

def is_source_image_filename(path):
    name = os.path.basename(path).lower()
    return name.endswith(IMAGE_EXTENSIONS)

def is_rewritable_asset_filename(path):
    name = os.path.basename(path).lower()
    return name.endswith(REWRITABLE_ASSET_EXTENSIONS)

# ====================================================================
# 2. FLASK APP SETUP & STARTUP WARNINGS
# ====================================================================
app = Flask(__name__)


@app.errorhandler(LibraryIndexUnavailable)
def library_index_unavailable(_error):
    message = "Library index is unavailable. Run the controlled reindex procedure."
    if request.path.startswith("/api/"):
        return jsonify({"status": "error", "error": message}), 503
    return message, 503
logging.getLogger("werkzeug").setLevel(logging.ERROR)
app.jinja_env.undefined = ChainableUndefined
app.secret_key = FLASK_SECRET_KEY
app.permanent_session_lifetime = timedelta(days=SESSION_LIFETIME_DAYS)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=_env_str("COOKIE_SECURE", "1") == "1",
    MAX_CONTENT_LENGTH=_env_int("MAX_CONTENT_LENGTH", 98 * 1024 * 1024),
)

for _message in _STARTUP_WARNINGS:
    print(f"[CONFIG WARNING] {_message}")

@app.before_request
def begin_request_observation():
    g.request_id = uuid.uuid4().hex
    g.request_started_at = time.perf_counter()

@app.before_request
def enforce_state_change_origin():
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None
    if request_source_allowed(
        origin=request.headers.get("Origin"),
        referer=request.headers.get("Referer"),
        host=request.host,
        allowed_origins=ALLOWED_ORIGINS,
    ):
        return None
    if request.path.startswith("/api/"):
        return jsonify({"status": "error", "error": "Cross-origin request rejected."}), 403
    return Response("Cross-origin request rejected.", status=403, mimetype="text/plain")

def get_client_ip():
    """Extract real client IP considering Cloudflare Tunnel proxies."""
    ip = request.headers.get("CF-Connecting-IP")
    if not ip:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            ip = forwarded.split(",")[0].strip()
    if not ip:
        ip = request.headers.get("X-Real-IP")
    if not ip:
        ip = request.remote_addr or ""
    return ip.strip().lower()

def _too_many_requests(retry_after):
    retry_after = max(1, int(retry_after))
    msg = f"Too many requests - try again in {retry_after} seconds."
    if request.path.startswith("/api/"):
        resp = jsonify({"status": "error", "error": msg})
        resp.status_code = 429
    else:
        resp = Response(msg, status=429, mimetype="text/plain")
    resp.headers["Retry-After"] = str(retry_after)
    return resp

@app.before_request
def throttle_spammers():
    user_email = (session.get("user_email") or "").strip().lower()
    if user_email in ADMIN_EMAILS:
        return None

    path = request.path or ""
    skip_counting = (
        path.startswith(AUTO_BAN_IGNORE_PREFIXES)
        or any(part in path for part in AUTO_BAN_IGNORE_CONTAINS)
    )
    if skip_counting:
        return None

    client_ip = get_client_ip()
    if not client_ip:
        return None

    # Use email for authenticated users so Cloudflare shared proxy IP does not collide
    throttle_key = f"email:{user_email}" if user_email else f"ip:{client_ip}"
    limit = MAX_REQUESTS_PER_WINDOW * 3 if user_email else MAX_REQUESTS_PER_WINDOW
    now = time.time()

    with AUTO_BAN_LOCK:
        cooldown_until = ip_cooldowns.get(throttle_key)
        if cooldown_until:
            if now < cooldown_until:
                return _too_many_requests(cooldown_until - now)
            ip_cooldowns.pop(throttle_key, None)
            ip_requests.pop(throttle_key, None)

        q = ip_requests[throttle_key]
        while q and q[0] < now - RATE_WINDOW_SECONDS:
            q.popleft()

        q.append(now)

        if len(q) > limit:
            ip_cooldowns[throttle_key] = now + COOLDOWN_SECONDS
            ip_requests.pop(throttle_key, None)

            print(
                f"[THROTTLE] target={_log_safe(throttle_key)} ip={_log_safe(client_ip)} "
                f"cooldown={COOLDOWN_SECONDS}s email={_log_safe(user_email or '-')}"
            )

            return _too_many_requests(COOLDOWN_SECONDS)

    return None

_ACCESS_LOG_SKIP_PREFIXES = ("/favicon.ico", "/static/")
_CTRL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")

def _log_safe(value):
    return _CTRL_CHARS_RE.sub("?", str(value))

@app.after_request
def log_request(response):
    try:
        path = request.path
        request_id = getattr(g, "request_id", uuid.uuid4().hex)
        started_at = getattr(g, "request_started_at", None)
        duration_ms = (
            max(0.0, (time.perf_counter() - started_at) * 1000.0)
            if started_at is not None
            else 0.0
        )
        response.headers.setdefault("X-Request-ID", request_id)

        if not (
            path.startswith(_ACCESS_LOG_SKIP_PREFIXES)
            or any(part in path for part in AUTO_BAN_IGNORE_CONTAINS)
        ):
            route = request.url_rule.rule if request.url_rule is not None else "unmatched"
            print(
                f"[REQUEST] request_id={_log_safe(request_id)} "
                f"route={_log_safe(route)} method={_log_safe(request.method)} "
                f"status={response.status_code} duration_ms={duration_ms:.3f}"
            )

    except Exception:
        pass

    return response

_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src * data: blob:; "
    "font-src 'self' data:; "
    "connect-src 'self' https://*.novelpia.com https://images.novelpia.com https://cdnjs.cloudflare.com; "
    "object-src 'none'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)

@app.after_request
def set_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Content-Security-Policy", _CSP)
    return response

# ====================================================================
# 3. TEXT UTILITIES
# ====================================================================
_CJK_PATTERN = re.compile(r"[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff\uac00-\ud7a3]")
_ALNUM_PATTERN = re.compile(r"[a-zA-Z0-9]")
_NATSORT_SPLIT = re.compile(r"(\d+)")

_ENGLISH_CHAR = re.compile(r"[A-Za-z0-9]")
_TRAILING_EN_PUNCT = set(" \t!?.,\u2026~-:;''\"`\u2019\u201d\u201c·*&")
_ID_PREFIX_PATTERN = re.compile(r"^\s*\[(\d+)\]\s*")

def _top_level_paren_groups(text):
    groups, depth, start = [], 0, None
    for i, ch in enumerate(text):
        if ch == "(":
            if depth == 0:
                start = i
            depth += 1
        elif ch == ")" and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                groups.append((start, i, text[start + 1:i]))
                start = None
    return groups

def extract_korean_block(filename):
    name = re.sub(r"\.epub\s*$", "", str(filename), flags=re.IGNORECASE).strip()
    groups = _top_level_paren_groups(name)
    if not groups:
        return ""
    for open_idx, _close, inner in groups:
        j = open_idx - 1
        while j >= 0 and name[j] in _TRAILING_EN_PUNCT:
            j -= 1
        if j >= 0 and _ENGLISH_CHAR.match(name[j]):
            return inner.strip()
    return groups[-1][2].strip()

def extract_novel_id(filename):
    name = re.sub(r"\.epub\s*$", "", str(filename), flags=re.IGNORECASE).strip()
    match = (_ID_PREFIX_PATTERN.match(name)
             or _ID_PREFIX_PATTERN.match(extract_korean_block(filename)))
    return match.group(1) if match else ""

def extract_korean_name(filename):
    name = re.sub(r"\.epub\s*$", "", str(filename), flags=re.IGNORECASE).strip()
    if _ID_PREFIX_PATTERN.match(name):
        return _ID_PREFIX_PATTERN.sub("", name).strip()
    return _ID_PREFIX_PATTERN.sub("", extract_korean_block(filename)).strip()

def normalize_korean_key(text):
    return re.sub(r"\s+", "", str(text)).lower()

def get_pure_cjk(text):
    return "".join(_CJK_PATTERN.findall(str(text)))

def get_pure_english(text):
    return "".join(_ALNUM_PATTERN.findall(str(text))).lower()

def natural_sort_key(s):
    return [(0, int(part)) if part.isdigit() else (1, part.lower()) for part in _NATSORT_SPLIT.split(s)]

def to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default

# ====================================================================
# 4. SECURITY / PATH SAFETY
# ====================================================================
def resolve_under(base_dir, relative_path):
    """Resolve relative_path strictly inside base_dir."""
    cleaned = unquote(str(relative_path)).replace("\\", "/").split("?")[0]
    candidate = os.path.realpath(os.path.join(base_dir, cleaned))
    base_real = os.path.realpath(base_dir)
    if candidate == base_real or candidate.startswith(base_real + os.sep):
        return candidate
    return None

# ====================================================================
# 5. ATOMIC PERSISTENCE (USER DATA, CUSTOM METADATA, USER UPLOADS)
# ====================================================================
_USER_DATA_LOCK = threading.Lock()
_CUSTOM_META_LOCK = threading.Lock()
_COLLECTIONS_LOCK = threading.Lock()
_USER_UPLOADS_LOCK = threading.Lock()

def read_json_file(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[WARN] Could not read {path}: {exc}")
        return default

def write_json_atomic(path, data, **dump_kwargs):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp_path = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, **dump_kwargs)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, path)

def _load_user_data_unlocked():
    data = read_json_file(USER_DATA_PATH, None)
    if data is not None:
        return data
    legacy = read_json_file(LEGACY_BOOKMARKS_PATH, None)
    if legacy is not None:
        migrated = {}
        for email, entry in legacy.items():
            if isinstance(entry, list):
                migrated[email] = {str(i): {"status": "want_to_read", "progress": 0} for i in entry}
            else:
                migrated[email] = entry
        try:
            write_json_atomic(USER_DATA_PATH, migrated)
        except OSError as exc:
            print(f"[WARN] Could not persist migrated user data: {exc}")
        return migrated
    return {}

def load_user_data():
    with _USER_DATA_LOCK:
        from arcdb.storage.runtime_reads import read_user_data
        return read_user_data(_load_user_data_unlocked)

def save_user_data(data):
    with _USER_DATA_LOCK:
        write_json_atomic(USER_DATA_PATH, data)

def mutate_user_data(mutator, shadow_email=None, shadow_reason="user_data"):
    with _USER_DATA_LOCK:
        data = _load_user_data_unlocked()
        before_user = {}
        if shadow_email:
            current = data.get(shadow_email, {})
            if isinstance(current, dict):
                before_user = json.loads(json.dumps(current))
        result = mutator(data)
        write_json_atomic(USER_DATA_PATH, data)
        if shadow_email:
            current = data.get(shadow_email, {})
            after_user = current if isinstance(current, dict) else {}
            try:
                from arcdb.storage.runtime_state import mirror_user_changes
                mirror_user_changes(
                    shadow_email,
                    before_user,
                    after_user,
                    reason=shadow_reason,
                )
            except Exception as exc:
                print(f"[STATE-DUAL-WRITE][ERROR] {shadow_reason}: {exc}")
                if os.environ.get("STATE_DUAL_WRITE_STRICT", "0") == "1":
                    raise
        return result

def _load_custom_meta_legacy():
    return read_json_file(CUSTOM_META_PATH, {})

def load_custom_meta():
    from arcdb.storage.runtime_reads import read_custom_meta
    return read_custom_meta(_load_custom_meta_legacy)

def save_custom_meta_entry(filename, entry):
    with _CUSTOM_META_LOCK:
        custom_meta = _load_custom_meta_legacy()
        custom_meta[filename] = entry
        write_json_atomic(CUSTOM_META_PATH, custom_meta, indent=4, ensure_ascii=False)
        try:
            from arcdb.storage.runtime_state import mirror_custom_metadata_entry
            mirror_custom_metadata_entry(filename, entry, reason="custom_metadata")
        except Exception as exc:
            print(f"[STATE-DUAL-WRITE][ERROR] custom_metadata: {exc}")
            if os.environ.get("STATE_DUAL_WRITE_STRICT", "0") == "1":
                raise

# ===================== User-uploaded novels persistence =====================
def _load_user_uploads_unlocked():
    data = read_json_file(USER_UPLOADS_PATH, {})
    return data if isinstance(data, dict) else {}

def load_user_uploads():
    with _USER_UPLOADS_LOCK:
        from arcdb.storage.runtime_reads import read_user_uploads
        return read_user_uploads(_load_user_uploads_unlocked)

def mutate_user_uploads(mutator, shadow_reason="user_uploads"):
    with _USER_UPLOADS_LOCK:
        data = _load_user_uploads_unlocked()
        before_uploads = json.loads(json.dumps(data))
        result = mutator(data)
        write_json_atomic(
            USER_UPLOADS_PATH,
            data,
            ensure_ascii=False,
            indent=2,
        )
        try:
            from arcdb.storage.runtime_state import mirror_upload_changes
            mirror_upload_changes(before_uploads, data, reason=shadow_reason)
        except Exception as exc:
            print(f"[STATE-DUAL-WRITE][ERROR] {shadow_reason}: {exc}")
            if os.environ.get("STATE_DUAL_WRITE_STRICT", "0") == "1":
                failure = RuntimeError(
                    f"SQLite upload shadow failed after the legacy write: {exc}"
                )
                failure.arcdb_legacy_write_succeeded = True
                raise failure from exc
        return result

# ===================== Collections =====================
def _load_collections_legacy():
    return read_json_file(COLLECTIONS_PATH, {})

def load_collections():
    with _COLLECTIONS_LOCK:
        from arcdb.storage.runtime_reads import read_collections
        return read_collections(_load_collections_legacy)

def _mirror_collections_shadow(email, user_collections, reason):
    try:
        from arcdb.storage.runtime_state import mirror_collection_user
        mirror_collection_user(email, user_collections, reason=reason)
    except Exception as exc:
        print(f"[STATE-DUAL-WRITE][ERROR] {reason}: {exc}")
        if os.environ.get("STATE_DUAL_WRITE_STRICT", "0") == "1":
            raise

def save_collections(data, shadow_email=None, shadow_reason="collections"):
    with _COLLECTIONS_LOCK:
        write_json_atomic(COLLECTIONS_PATH, data, ensure_ascii=False, indent=2)
        if shadow_email:
            _mirror_collections_shadow(
                shadow_email,
                data.get(shadow_email, []),
                shadow_reason,
            )

def get_user_collections(email):
    return load_collections().get(email, [])

def collection_counts(email):
    counts = {}
    udata = load_user_data().get(email, {})
    for entry in udata.values():
        if isinstance(entry, dict):
            for cid in (entry.get("collections") or []):
                counts[cid] = counts.get(cid, 0) + 1
    return counts

# ====================================================================
# 5b. USER-UPLOAD FILE VALIDATION AND EPUB EXTRACTION
# ====================================================================
_ALLOWED_COVER_MIMES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}

def _sniff_image_mime(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return ""

def _clean_upload_text(value, max_length):
    value = str(value or "").replace("\x00", "")
    value = re.sub(r"\r\n?", "\n", value).strip()
    return value[:max_length]

def _safe_upload_filename(value, fallback):
    name = os.path.basename(str(value or "").replace("\\", "/")).strip()
    name = name.replace("\x00", "")
    name = re.sub(r"[\r\n\t]+", " ", name)
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = name.strip(" .")
    return name[:240] or fallback

def _copy_upload_limited(storage, destination, max_bytes):
    return copy_upload_limited(storage.stream, destination, max_bytes)


def _epub_limits():
    return EPUB_LIMITS

def _validate_epub_archive(epub_path):
    return validate_epub_archive(epub_path, _epub_limits())

def _extract_epub_safely(epub_path, destination):
    return extract_epub_safely(epub_path, destination, _epub_limits())

def _save_uploaded_cover(storage, upload_id):
    if not storage or not storage.filename:
        return ""
    temporary_path = os.path.join(USER_UPLOAD_COVER_DIR, f".{upload_id}.cover-upload")
    try:
        _copy_upload_limited(storage, temporary_path, MAX_COVER_UPLOAD_BYTES)
        with open(temporary_path, "rb") as cover_file:
            head = cover_file.read(512)
            mime = _sniff_image_mime(head)
            extension = _ALLOWED_COVER_MIMES.get(mime)
            if not extension:
                raise ValueError("Cover must be a JPEG, PNG, GIF, or WebP image.")
        final_path = os.path.join(USER_UPLOAD_COVER_DIR, f"{upload_id}{extension}")
        os.replace(temporary_path, final_path)
        return final_path
    except Exception:
        try:
            if os.path.exists(temporary_path):
                os.remove(temporary_path)
        except OSError:
            pass
        raise

def _count_readable_chapters(folder_path):
    count = 0
    if not os.path.isdir(folder_path):
        return 0
    for _root, _dirs, files in os.walk(folder_path):
        for filename in files:
            lower_name = filename.lower()
            if "title_translator" in lower_name or "translated__book" in lower_name:
                continue
            if lower_name.endswith(CHAPTER_EXTENSIONS) and lower_name not in NON_CHAPTER_FILES:
                count += 1
    return count

# ====================================================================
# 6. AUTH (ALLOWED E-MAILS, ACCOUNTS, LOGIN GATE, AUTO-BAN)
# ====================================================================
_allowed_emails_cache = {
    "emails": set(),
    "mtime": None,
}
_ALLOWED_EMAILS_LOCK = threading.Lock()

def _get_allowed_emails_legacy():
    with _ALLOWED_EMAILS_LOCK:
        try:
            mtime = os.path.getmtime(ALLOWED_EMAILS_PATH)
        except OSError as exc:
            print(f"[WARN] Allowed emails file not found/readable: {ALLOWED_EMAILS_PATH} ({exc})")
            _allowed_emails_cache["emails"] = set()
            _allowed_emails_cache["mtime"] = None
            return set()

        if _allowed_emails_cache["mtime"] == mtime:
            return _allowed_emails_cache["emails"]

        emails = set()
        try:
            with open(ALLOWED_EMAILS_PATH, "r", encoding="utf-8") as fh:
                for line in fh:
                    email = line.strip().lower()
                    if not email or line.startswith("#"):
                        continue
                    emails.add(email)
            _allowed_emails_cache["emails"] = emails
            _allowed_emails_cache["mtime"] = mtime
            return emails
        except OSError as exc:
            print(f"[WARN] Could not read allowed emails file: {ALLOWED_EMAILS_PATH} ({exc})")
            return _allowed_emails_cache["emails"]

def get_allowed_emails():
    from arcdb.storage.runtime_reads import read_allowed_emails
    return read_allowed_emails(_get_allowed_emails_legacy)

_ALLOWLIST_WRITE_LOCK = threading.Lock()

def _mirror_allowlist_shadow(reason):
    try:
        from arcdb.storage.runtime_state import mirror_allowed_emails
        with _ALLOWLIST_WRITE_LOCK:
            mirror_allowed_emails(_get_allowed_emails_legacy(), reason=reason)
    except Exception as exc:
        print(f"[STATE-DUAL-WRITE][ERROR] {reason}: {exc}")
        if os.environ.get("STATE_DUAL_WRITE_STRICT", "0") == "1":
            raise

def remove_email_from_allowlist(email):
    email = (email or "").strip().lower()
    if not email:
        return False
    with _ALLOWLIST_WRITE_LOCK:
        try:
            with open(ALLOWED_EMAILS_PATH, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
        except OSError:
            return False
        kept, removed = [], False
        for line in lines:
            if line.strip().lower() == email:
                removed = True
                continue
            kept.append(line)
        if not removed:
            return False
        tmp = f"{ALLOWED_EMAILS_PATH}.tmp.{os.getpid()}.{threading.get_ident()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.writelines(kept)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, ALLOWED_EMAILS_PATH)
    with _ALLOWED_EMAILS_LOCK:
        _allowed_emails_cache["mtime"] = None
    _mirror_allowlist_shadow("allowlist_remove")
    return True

def extract_emails_from_text(text):
    found = re.findall(
        r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}",
        text or ""
    )

    cleaned = []
    seen = set()

    for email in found:
        email = email.strip().lower()
        if email and email not in seen:
            seen.add(email)
            cleaned.append(email)

    return cleaned

def add_emails_to_allowlist(emails):
    emails = [
        e.strip().lower()
        for e in emails
        if e and e.strip()
    ]
    if not emails:
        return []
    with _ALLOWLIST_WRITE_LOCK:
        existing_lines = []
        try:
            with open(ALLOWED_EMAILS_PATH, "r", encoding="utf-8") as fh:
                existing_lines = fh.readlines()
        except FileNotFoundError:
            existing_lines = []
        except OSError:
            existing_lines = []
        kept = []
        seen = set()
        for line in existing_lines:
            raw = line.strip()
            email = raw.lower()
            if not raw:
                continue
            if raw.startswith("#"):
                kept.append(raw)
                continue
            if email not in seen:
                seen.add(email)
                kept.append(email)
        added = []
        for email in emails:
            if email not in seen:
                seen.add(email)
                kept.append(email)
                added.append(email)
        os.makedirs(os.path.dirname(ALLOWED_EMAILS_PATH) or ".", exist_ok=True)
        tmp = f"{ALLOWED_EMAILS_PATH}.tmp.{os.getpid()}.{threading.get_ident()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("\n".join(kept).strip() + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, ALLOWED_EMAILS_PATH)
    with _ALLOWED_EMAILS_LOCK:
        _allowed_emails_cache["mtime"] = None
    _mirror_allowlist_shadow("allowlist_add")
    return added

_IP_EMAIL_LOCK = threading.Lock()
_IP_EXEMPTIONS_LOCK = threading.Lock()
_ACCESS_REVOCATION_LOG_LOCK = threading.Lock()

def _ip_group_key(client_ip):
    ip = (client_ip or "").strip().lower()
    if not ip:
        return ""
    if IP_GROUP_IPV6_64:
        try:
            addr = ipaddress.ip_address(ip)
            if addr.version == 6:
                return str(ipaddress.ip_network(f"{ip}/64", strict=False).network_address) + "/64"
        except ValueError:
            pass
    return ip

def _normalize_ip_exemption(value):
    raw = (value or "").strip().lower()
    if not raw:
        return ""
    try:
        if "/" in raw:
            return str(ipaddress.ip_network(raw, strict=False))
        addr = ipaddress.ip_address(raw)
        if addr.version == 6 and IP_GROUP_IPV6_64:
            return str(ipaddress.ip_network(f"{addr}/64", strict=False))
        return str(addr)
    except ValueError:
        return ""

def load_ip_exemptions():
    with _IP_EXEMPTIONS_LOCK:
        data = read_json_file(IP_EXEMPTIONS_PATH, {})
    return data if isinstance(data, dict) else {}

def add_ip_exemption(value, actor="", note=""):
    rule = _normalize_ip_exemption(value)
    if not rule:
        return "", False
    with _IP_EXEMPTIONS_LOCK:
        data = read_json_file(IP_EXEMPTIONS_PATH, {})
        if not isinstance(data, dict):
            data = {}
        created = rule not in data
        if created:
            data[rule] = {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "created_by": (actor or "").strip().lower(),
                "note": (note or "").strip()[:1000],
            }
            write_json_atomic(IP_EXEMPTIONS_PATH, data)
    return rule, created

def remove_ip_exemption(value):
    rule = _normalize_ip_exemption(value)
    if not rule:
        return False
    with _IP_EXEMPTIONS_LOCK:
        data = read_json_file(IP_EXEMPTIONS_PATH, {})
        if not isinstance(data, dict) or rule not in data:
            return False
        data.pop(rule, None)
        write_json_atomic(IP_EXEMPTIONS_PATH, data)
    return True

def get_ip_exemption(client_ip):
    raw = (client_ip or "").strip().lower()
    if not raw:
        return None
    try:
        addr = ipaddress.ip_address(raw)
    except ValueError:
        return None
    for rule, record in load_ip_exemptions().items():
        try:
            if "/" in rule:
                matched = addr in ipaddress.ip_network(rule, strict=False)
            else:
                matched = addr == ipaddress.ip_address(rule)
        except ValueError:
            matched = False
        if matched:
            result = dict(record) if isinstance(record, dict) else {}
            result["rule"] = rule
            return result
    return None

def _record_ip_login(group_key, email):
    now = time.time()
    window = IP_EMAIL_WINDOW_HRS * 3600
    email = (email or "").lower()
    with _IP_EMAIL_LOCK:
        data = read_json_file(IP_EMAIL_MAP_PATH, {})
        entry = {e: ts for e, ts in (data.get(group_key) or {}).items() if now - ts <= window}
        entry[email] = now
        data[group_key] = entry
        data = {k: v for k, v in data.items() if v}
        write_json_atomic(IP_EMAIL_MAP_PATH, data)
    return entry

def _request_audit_context():
    try:
        return {
            "client_ip": get_client_ip(),
            "request_path": request.path,
            "user_agent": request.headers.get("User-Agent", "")[:500],
        }
    except RuntimeError:
        return {"client_ip": "", "request_path": "", "user_agent": ""}

def log_access_revocation(email, reason, *, action, source, actor="",
                          ip_group="", related_accounts=None,
                          removed_accounts=None, account_last_seen=None,
                          details=None):
    now = time.time()
    event = {
        "ts": now,
        "timestamp": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "date": date.today().isoformat(),
        "email": (email or "").strip().lower(),
        "reason": reason,
        "action": action,
        "source": source,
        "actor": (actor or "").strip().lower(),
        "ip_group": ip_group,
        "related_accounts": sorted(set(related_accounts or [])),
        "removed_accounts": sorted(set(removed_accounts or [])),
        "account_last_seen": account_last_seen or {},
        "policy": {
            "max_emails_per_ip": MAX_EMAILS_PER_IP,
            "ip_email_window_hours": IP_EMAIL_WINDOW_HRS,
            "multi_account_enforcement": MULTI_ACCOUNT_ENFORCE,
            "ipv6_grouped_by_64": IP_GROUP_IPV6_64,
        },
        "details": details or {},
    }
    event.update(_request_audit_context())
    try:
        os.makedirs(os.path.dirname(ACCESS_REVOCATION_LOG_PATH) or ".", exist_ok=True)
        with _ACCESS_REVOCATION_LOG_LOCK:
            with open(ACCESS_REVOCATION_LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as exc:
        print(f"[WARN] Could not write access revocation log: {exc}")

def load_jsonl_events(path, *, limit=250, reason=None):
    events = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    event = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if reason and event.get("reason") != reason:
                    continue
                events.append(event)
    except OSError:
        return []
    events.sort(key=lambda event: float(event.get("ts") or 0), reverse=True)
    return events[:limit]

def log_multi_account(group_key, emails, action, removed, account_last_seen=None):
    print(f"[MULTI-ACCT] group={_log_safe(group_key)} emails={_log_safe(emails)} "
          f"action={action} removed={removed}")
    try:
        os.makedirs(os.path.dirname(DOWNLOAD_ABUSE_LOG_PATH) or ".", exist_ok=True)
        event = {"ts": time.time(), "date": date.today().isoformat(),
                 "reason": "multi_account_ip", "group": group_key,
                 "emails": emails, "action": action, "removed": removed,
                 "account_last_seen": account_last_seen or {}}
        with _DOWNLOAD_ABUSE_LOCK:
            with open(DOWNLOAD_ABUSE_LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as exc:
        print(f"[WARN] Could not write multi-account log: {exc}")

def enforce_multi_account(client_ip, current_email):
    return False

_USERS_LOCK = threading.Lock()

def _load_users_unlocked():
    return read_json_file(USERS_PATH, {})

def load_users():
    with _USERS_LOCK:
        from arcdb.storage.runtime_reads import read_users
        return read_users(_load_users_unlocked)

def mutate_users(mutator, shadow_reason="users_auth"):
    with _USERS_LOCK:
        data = _load_users_unlocked()
        before_users = json.loads(json.dumps(data))
        result = mutator(data)
        write_json_atomic(USERS_PATH, data)
        try:
            from arcdb.storage.runtime_state import mirror_auth_users_changes
            mirror_auth_users_changes(before_users, data, reason=shadow_reason)
        except Exception as exc:
            print(f"[STATE-DUAL-WRITE][ERROR] {shadow_reason}: {exc}")
            if os.environ.get("STATE_DUAL_WRITE_STRICT", "0") == "1":
                raise
        return result

def _new_code():
    return f"{secrets.randbelow(1_000_000):06d}"

def _hash_code(code):
    return hashlib.sha256(f"{FLASK_SECRET_KEY}:{code}".encode("utf-8")).hexdigest()

def send_email(to_addr, subject, body):
    if not (SMTP_USER and SMTP_PASS):
        if (os.environ.get("ARCHIVEDB_AUTH_TEST_MODE", "0") == "1"
                and os.environ.get("ARCHIVEDB_LOCAL_DEV", "0") == "1"):
            print(f"[EMAIL] Local auth test message suppressed for {_log_safe(to_addr)}.")
        else:
            print(f"[EMAIL] (SMTP not configured) to={_log_safe(to_addr)} :: {_log_safe(body)}")
        return True
    try:
        msg = EmailMessage()
        msg["From"] = SMTP_USER
        msg["To"] = to_addr
        msg["Subject"] = subject
        msg.set_content(body)
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
        return True
    except Exception as exc:
        print(f"[EMAIL] Failed to send to {_log_safe(to_addr)}: {exc}")
        return False

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        email = session.get("user_email")
        if not email:
            if request.path.startswith("/api/"):
                return json_error("Authentication required.", 401)
            return redirect(url_for("login"))
        if email not in ADMIN_EMAILS and email not in get_allowed_emails():
            log_access_revocation(
                email, "allowlist_entry_missing", action="session_revoked",
                source="allowlist_check", ip_group=_ip_group_key(get_client_ip()),
                details={
                    "explanation": "The account had a session, but its email was no longer on the allowlist.",
                    "possible_causes": [
                        "removed in the admin access page",
                        "removed directly from the allowlist file",
                        "allowlist file missing or unreadable",
                    ],
                },
            )
            session.pop("user_email", None)
            if request.path.startswith("/api/"):
                return json_error("Access revoked.", 403)
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated_function

# ====================================================================
# 7. RATE LIMITING & DOWNLOAD ACCOUNTING
# ====================================================================
_email_download_tracker = {}
_DOWNLOAD_LIMIT_LOCK = threading.Lock()

def check_download_limit(user_email, novel_key_str=None):
    if not user_email or user_email in ADMIN_EMAILS:
        return True
    today = date.today().isoformat()
    with _DOWNLOAD_LIMIT_LOCK:
        entry = _email_download_tracker.get(user_email)
        if not entry or entry.get("date") != today:
            return True
        downloaded_set = entry.get("novels", set())
        # If user already downloaded this specific novel today, allow retries / re-downloads freely
        if novel_key_str and str(novel_key_str).strip() in downloaded_set:
            return True
        return len(downloaded_set) < DAILY_DOWNLOAD_LIMIT

def increment_download_count(user_email, novel_key_str=None):
    if not user_email or user_email in ADMIN_EMAILS:
        return 0

    # Ignore HTTP Range continuation chunks (multi-threaded download managers)
    try:
        range_header = request.headers.get("Range", "")
        if range_header and not range_header.strip().startswith("bytes=0-"):
            with _DOWNLOAD_LIMIT_LOCK:
                entry = _email_download_tracker.get(user_email, {})
                return len(entry.get("novels", set()))
    except Exception:
        pass

    today = date.today().isoformat()
    with _DOWNLOAD_LIMIT_LOCK:
        entry = _email_download_tracker.get(user_email)
        if not entry or entry.get("date") != today:
            entry = {"date": today, "novels": set(), "count": 0}

        nid_str = str(novel_key_str).strip() if novel_key_str else ""
        if nid_str:
            entry["novels"].add(nid_str)
        else:
            entry["count"] = entry.get("count", 0) + 1

        _email_download_tracker[user_email] = entry
        return len(entry["novels"]) if entry["novels"] else entry["count"]

RATE_LIMITS = {
    "read":           {"email": (20, 600),   "ip": (20, 600)},
    "asset":          {"email": (125, 2250), "ip": (250, 3750)},
    "library":        {"email": (38, 450),   "ip": (75, 750)},
    "auth":           {"email": (6, 45),     "ip": (13, 90)},
    "community":      {"email": (38, 900),   "ip": (75, 1500)},
    "community_post": {"email": (13, 180),   "ip": (25, 360)},
    "upload":         {"email": (3, 15),     "ip": (5, 25)},
}
_RATE_LIMIT_LOCK = threading.Lock()
_rate_buckets = collections.defaultdict(collections.deque)

def _retry_after_seconds(key, per_min, per_hour, now):
    dq = _rate_buckets.get(key)
    if not dq:
        return 0
    cutoff = now - 3600
    while dq and dq[0] < cutoff:
        dq.popleft()
    if not dq:
        _rate_buckets.pop(key, None)
        return 0
    wait = 0.0
    if len(dq) >= per_hour:
        wait = max(wait, 3600 - (now - dq[0]))
    recent = [t for t in dq if t >= now - 60]
    if len(recent) >= per_min:
        wait = max(wait, 60 - (now - recent[0]))
    return int(wait) + 1 if wait > 0 else 0

def enforce_rate_limit(route_class, as_json=False):
    user_email = session.get("user_email", "")
    if user_email in ADMIN_EMAILS:
        return None
    client_ip = get_client_ip()
    em, eh = RATE_LIMITS[route_class]["email"]
    im, ih = RATE_LIMITS[route_class]["ip"]
    email_key = (route_class, "email", user_email)
    ip_key = (route_class, "ip", client_ip)
    now = time.time()

    with _RATE_LIMIT_LOCK:
        wait = 0
        if user_email:
            # Authenticated users are isolated by their own account email
            wait = max(wait, _retry_after_seconds(email_key, em, eh, now))
        elif client_ip:
            # Unauthenticated visitors are limited by IP
            wait = max(wait, _retry_after_seconds(ip_key, im, ih, now))

        if wait > 0:
            msg = f"Rate limit exceeded - try again in {wait} seconds."
            if as_json:
                resp, status = json_error(msg, 429)
                resp.headers["Retry-After"] = str(wait)
                return resp, status
            resp = Response(msg, status=429, mimetype="text/plain")
            resp.headers["Retry-After"] = str(wait)
            return resp
        if user_email:
            _rate_buckets[email_key].append(now)
        elif client_ip:
            _rate_buckets[ip_key].append(now)
        return None

_DOWNLOAD_ABUSE_LOCK = threading.Lock()
def log_download_limit_exceeded(user_email, tg_link, client_ip):
    try:
        os.makedirs(os.path.dirname(DOWNLOAD_ABUSE_LOG_PATH) or ".", exist_ok=True)
        event = {
            "ts": time.time(),
            "date": date.today().isoformat(),
            "email": (user_email or "").lower(),
            "ip": client_ip or "",
            "tg_link": tg_link,
            "limit": DAILY_DOWNLOAD_LIMIT,
        }
        line = json.dumps(event, ensure_ascii=False)
        with _DOWNLOAD_ABUSE_LOCK:
            with open(DOWNLOAD_ABUSE_LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception as exc:
        print(f"[WARN] Could not write abuse log: {exc}")

_DOWNLOAD_LOG_LOCK = threading.Lock()
def log_download_event(user_email, novel, tg_link, want_raw, client_ip, count_today):
    label = (novel.get("title_en") or novel.get("title_kr") or "").strip()
    novel_id = novel_key(novel)
    kind = "raw" if want_raw else "translated"
    print(f"[DOWNLOAD] email={_log_safe(user_email or '-')} ip={_log_safe(client_ip)} "
          f"novel={_log_safe(novel_id)} kind={kind} count_today={count_today} title={label!r}")
    try:
        os.makedirs(os.path.dirname(DOWNLOAD_LOG_PATH) or ".", exist_ok=True)
        event = {
            "ts": time.time(),
            "date": date.today().isoformat(),
            "email": (user_email or "").lower(),
            "ip": client_ip or "",
            "novel_id": novel_id,
            "title": label,
            "kind": kind,
            "tg_link": tg_link,
        }
        line = json.dumps(event, ensure_ascii=False)
        with _DOWNLOAD_LOG_LOCK:
            with open(DOWNLOAD_LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception as exc:
        print(f"[WARN] Could not write download log: {exc}")

# ====================================================================
# 8. METADATA LOADING & GALLERY ASSEMBLY (CACHED)
# ====================================================================
def _public_novel(n):
    """Return a copy of a gallery item safe to send to the browser."""
    private_fields = {
        "tg_link",
        "raw_tg_link",
        "local_folder",
        "_library_key",
        "_source_ids",
        "translated_epub_path",
        "raw_epub_path",
        "cover_file_path",
        "uploader_email",
    }
    safe = {key: value for key, value in n.items() if key not in private_fields}
    safe["has_download"] = bool(n.get("tg_link") or n.get("translated_epub_path"))
    safe["has_raw"] = bool(n.get("raw_tg_link") or n.get("raw_epub_path"))
    safe["uploaded"] = bool(n.get("uploaded"))
    safe["is_updated"] = bool(n.get("is_updated"))
    safe["uploader_name"] = n.get("uploader_name", "")
    return safe

def dedupe_tags(tags):
    if not tags:
        return []
    seen = set()
    result = []
    for t in tags:
        if not t:
            continue
        t_str = str(t).strip()
        if not t_str:
            continue
        key = t_str.lower()
        if key not in seen:
            seen.add(key)
            result.append(t_str)
    return result

def load_text_map(filepath, key_is_int=False, is_tag=False):
    data_map = {}
    if not os.path.exists(filepath):
        return data_map
    try:
        with open(filepath, "r", encoding="utf-8") as fh:
            for line in fh:
                parts = line.strip().split("|||")
                if len(parts) < 2:
                    continue
                key = parts[0].strip()
                if key_is_int and key.isdigit():
                    key = int(key)
                if is_tag:
                    val = parts[1].strip()
                    data_map[key] = val
                    if isinstance(key, str):
                        data_map.setdefault(key.lower(), val)
                elif len(parts) >= 3:
                    data_map[key] = parts[2].strip().replace("\\n", "\n")
    except OSError as exc:
        print(f"[WARN] Could not read {filepath}: {exc}")
    return data_map

_RAW_FOLDER_DATE_RE = re.compile(r"(?:\s+update)?\s+(\d{1,2})-(\d{1,2})-(\d{2,4})\s*$", re.IGNORECASE)

def _raw_folder_metadata(folder):
    original = re.sub(r"\s+", " ", str(folder or "")).strip()
    lowered = original.lower()
    complete = 1 if lowered.startswith("complete") else (0 if lowered.startswith("ongoing") else None)
    upload_date = ""
    match = _RAW_FOLDER_DATE_RE.search(original)
    if match:
        day, month, year = (int(part) for part in match.groups())
        if year < 100:
            year += 2000
        try:
            upload_date = date(year, month, day).isoformat()
        except ValueError:
            upload_date = ""

    label = _RAW_FOLDER_DATE_RE.sub("", original)
    label = re.sub(r"\s+update\s*$", "", label, flags=re.IGNORECASE).strip(" -_")
    if label.lower() in ("", "complete", "ongoing", "(no images)", "rq2"):
        tag = ""
    else:
        aliases = {
            "ts": "TS", "academy": "Academy", "genderreversal": "Gender Reversal",
            "yandere": "Yandere", "depressing (피폐)": "Depressing (피폐)",
        }
        tag = aliases.get(label.lower(), label)
    return tag, complete, upload_date

def _dedupe_title_key(filename):
    stem = re.sub(r"\.epub\s*$", "", str(filename), flags=re.IGNORECASE).strip()
    stem = _ID_PREFIX_PATTERN.sub("", stem).strip().casefold()
    trailing = re.search(r"([.!?…]+)$", stem)
    suffix = trailing.group(1) if trailing else ""
    base = "".join(ch for ch in stem if ch.isalnum())
    return f"{base}|tail:{suffix}" if base and suffix else base

def _raw_record_key(filename):
    title_key = _dedupe_title_key(filename)
    if title_key:
        return f"title:{title_key}"
    novel_id = extract_novel_id(filename)
    if novel_id:
        return f"id:{novel_id}"
    return f"file:{normalize_korean_key(filename)}"

def _raw_link_priority(folder, upload_date, filename):
    lowered = str(folder or "").strip().lower()
    tier = 3 if lowered.startswith(("complete", "ongoing")) else (2 if lowered == "(no images)" else 1)
    return upload_date or "", tier, 1 if extract_novel_id(filename) else 0

def load_raw_library():
    lookup, records = {}, {}
    if not os.path.exists(RAW_MASTER_CSV_PATH):
        return lookup, []
    try:
        with open(RAW_MASTER_CSV_PATH, "r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                folder = (row.get("Folder") or "").strip()
                filename = (row.get("File Name") or "").strip()
                link = (row.get("Telegram Link") or "").strip()
                if not filename or not link:
                    continue
                key = _raw_record_key(filename)
                tag, complete, upload_date = _raw_folder_metadata(folder)
                record = records.setdefault(key, {
                    "_key": key, "filename": filename, "raw_tg_link": "", "tags": [],
                    "complete": None, "upload_date": "", "_link_priority": ("", 0, 0),
                    "_ids": set(), "_names": set(),
                })
                row_id = extract_novel_id(filename)
                row_name = re.sub(r"\.epub\s*$", "", filename, flags=re.IGNORECASE).strip()
                row_name = _ID_PREFIX_PATTERN.sub("", row_name).strip()
                if row_id:
                    record["_ids"].add(row_id)
                if row_name:
                    record["_names"].add(row_name)
                if tag and not any(t.lower() == tag.lower() for t in record["tags"]):
                    record["tags"].append(tag)
                if complete is not None:
                    record["complete"] = max(record["complete"] or 0, complete)
                if upload_date > record["upload_date"]:
                    record["upload_date"] = upload_date
                priority = _raw_link_priority(folder, upload_date, filename)
                if priority > record["_link_priority"]:
                    record["raw_tg_link"] = link
                    record["filename"] = filename
                    record["_link_priority"] = priority
    except (OSError, csv.Error) as exc:
        print(f"[WARN] Could not read {RAW_MASTER_CSV_PATH}: {exc}")
        return lookup, []

    deduped = list(records.values())
    cjk_candidates = {}
    for record in deduped:
        record["_source_ids"] = sorted(record.get("_ids", set()))
        for novel_id in record.pop("_ids", set()):
            lookup.setdefault(f"id:{novel_id}", record)
        for korean_name in record.pop("_names", set()):
            lookup.setdefault(f"kr:{normalize_korean_key(korean_name)}", record)
            pure = get_pure_cjk(korean_name)
            if pure:
                choices = cjk_candidates.setdefault(pure, {})
                choices[record["_key"]] = record
        record.pop("_link_priority", None)

    for pure, choices in cjk_candidates.items():
        if len(choices) == 1:
            lookup[f"cjk:{pure}"] = next(iter(choices.values()))
    return lookup, deduped

def _scan_local_folders():
    folder_cjk_map, folder_en_map = {}, {}
    if os.path.isdir(LOCAL_OUTPUT_DIR):
        try:
            for entry in os.listdir(LOCAL_OUTPUT_DIR):
                if os.path.isdir(os.path.join(LOCAL_OUTPUT_DIR, entry)):
                    folder_cjk_map[get_pure_cjk(entry)] = entry
                    folder_en_map[get_pure_english(entry)] = entry
        except OSError as exc:
            print(f"[WARN] Could not scan {LOCAL_OUTPUT_DIR}: {exc}")
    return folder_cjk_map, folder_en_map

def _scan_structured_output():
    structured_map = {}
    if os.path.isdir(STRUCTURED_OUTPUT_DIR):
        try:
            for entry in os.listdir(STRUCTURED_OUTPUT_DIR):
                folder_path = os.path.join(STRUCTURED_OUTPUT_DIR, entry)
                if not os.path.isdir(folder_path):
                    continue
                novel_id = str(entry).strip()
                try:
                    files = os.listdir(folder_path)
                except OSError:
                    continue
                epubs = [os.path.join(folder_path, f) for f in files if f.lower().endswith(".epub")]
                epub_path = epubs[0] if epubs else ""
                ch_count = _count_readable_chapters(folder_path)
                structured_map[novel_id] = {
                    "novel_id": novel_id,
                    "folder_path": folder_path,
                    "epub_path": epub_path,
                    "chapter_count": ch_count,
                }
        except OSError as exc:
            print(f"[WARN] Could not scan {STRUCTURED_OUTPUT_DIR}: {exc}")
    return structured_map

def _scan_batched_epubs():
    raw_map = {}
    if not os.path.isdir(BATCHED_EPUBS_DIR):
        return raw_map
    try:
        for f in os.listdir(BATCHED_EPUBS_DIR):
            if f.lower().endswith(".epub"):
                # 1. Bracketed ID e.g. [430186]
                for n in re.findall(r"\[(\d{3,7})\]", f):
                    if n not in raw_map:
                        raw_map[n] = os.path.join(BATCHED_EPUBS_DIR, f)
                # 2. Leading ID e.g. 430186_... or 430186-...
                m = re.match(r"^(\d{3,7})[_\s\-]", f)
                if m and m.group(1) not in raw_map:
                    raw_map[m.group(1)] = os.path.join(BATCHED_EPUBS_DIR, f)
                # 3. Delimited by underscores e.g. _430186_
                for n in re.findall(r"_(\d{3,7})_", f):
                    if n not in raw_map:
                        raw_map[n] = os.path.join(BATCHED_EPUBS_DIR, f)
                # 4. Any 3-7 digit sequence separated by non-digits
                for n in re.findall(r"(?:^|[^\d])(\d{3,7})(?:[^\d]|$)", f):
                    if n not in raw_map:
                        raw_map[n] = os.path.join(BATCHED_EPUBS_DIR, f)
    except OSError as exc:
        print(f"[WARN] Could not scan {BATCHED_EPUBS_DIR}: {exc}")
    return raw_map

def _metadata_title_key(value):
    title = re.sub(r"\.epub\s*$", "", str(value or ""), flags=re.IGNORECASE).strip()
    title = _ID_PREFIX_PATTERN.sub("", title).strip()
    return normalize_korean_key(title)

def _load_novel_db_lookups():
    db_by_id, db_exact, db_normalized, db_pure = {}, {}, {}, {}
    novels_json = read_json_file(JSON_DB_PATH, [])
    if isinstance(novels_json, list):
        for novel in novels_json:
            novel_id = str(novel.get("id", "")).strip()
            title = str(novel.get("title", "")).strip()
            clean_title = _ID_PREFIX_PATTERN.sub("", title).strip()
            pure_cjk = get_pure_cjk(clean_title)
            if novel_id:
                db_by_id[novel_id] = novel
            if title:
                db_exact[title] = novel
            if clean_title:
                db_exact.setdefault(clean_title, novel)
                db_normalized.setdefault(_metadata_title_key(clean_title), novel)
            if pure_cjk:
                db_pure.setdefault(pure_cjk, novel)
    return db_by_id, db_exact, db_normalized, db_pure

def _matched_item(match, filename, tg_link, upload_date, has_custom, en_titles, en_tags, en_descs):
    novel_id = match["id"]
    raw_tags = match.get("tags", [])
    translated_tags = [en_tags.get(str(t).strip()) or en_tags.get(str(t).strip().lower(), str(t).strip()) for t in raw_tags]
    item_tags = dedupe_tags(translated_tags)
    is_adult = _novel_is_adult(match) or any(
        isinstance(t, str) and (
            "19금" in t or "r-19" in t.lower() or "r19" in t.lower() or "19+" in t.lower() or "adult" in t.lower()
        ) for t in item_tags
    )
    age_val = 19 if is_adult else match.get("age", 0)
    return {
        "has_meta": True, "is_custom": has_custom, "id": novel_id,
        "filename": filename, "tg_link": tg_link,
        "title_en": en_titles.get(novel_id, match.get("title")),
        "title_kr": match.get("title"),
        "author": match.get("author", "Unknown"),
        "cover": match.get("cover", ""),
        "tags": item_tags,
        "synopsis": en_descs.get(novel_id, match.get("synopsis", "")),
        "views": match.get("views", 0), "likes": match.get("likes", 0),
        "chapters": match.get("chapters", 0), "age": age_val,
        "complete": match.get("complete", 0), "upload_date": upload_date,
    }

def _unmatched_item(filename, tg_link, upload_date, has_custom, custom_cjk):
    return {
        "has_meta": False, "is_custom": has_custom, "id": extract_novel_id(filename),
        "filename": filename, "tg_link": tg_link,
        "title_en": filename.replace(".epub", ""),
        "title_kr": custom_cjk if custom_cjk else "",
        "author": "Raw Upload", "cover": "", "tags": ["Unmatched"],
        "synopsis": "No metadata found.",
        "views": 0, "likes": 0, "chapters": 0, "age": 0, "complete": 0,
        "upload_date": upload_date,
    }

def _match_metadata(filename, custom_cjk, db_by_id, db_exact, db_normalized, db_pure):
    novel_id = extract_novel_id(filename)
    stripped_name = re.sub(r"\.epub\s*$", "", str(filename), flags=re.IGNORECASE).strip()
    stripped_name = _ID_PREFIX_PATTERN.sub("", stripped_name).strip()
    korean_name = extract_korean_name(filename) or stripped_name
    match = db_by_id.get(str(novel_id)) if novel_id else None
    if not match and custom_cjk:
        match = (db_exact.get(custom_cjk)
                 or db_normalized.get(_metadata_title_key(custom_cjk))
                 or db_pure.get(get_pure_cjk(custom_cjk)))
    if not match and stripped_name:
        match = (db_exact.get(stripped_name)
                 or db_normalized.get(_metadata_title_key(stripped_name))
                 or db_pure.get(get_pure_cjk(stripped_name)))
    if not match and korean_name:
        match = (db_exact.get(korean_name)
                 or db_normalized.get(_metadata_title_key(korean_name))
                 or db_pure.get(get_pure_cjk(korean_name)))
    return match

def _raw_only_item(raw, custom_meta, db_by_id, db_exact, db_normalized, db_pure, en_titles, en_tags, en_descs):
    filename = raw["filename"]
    has_custom = filename in custom_meta
    c_data = custom_meta.get(filename, {}) if has_custom else {}
    custom_cjk = c_data.get("title_kr", "").strip()
    match = _match_metadata(filename, custom_cjk, db_by_id, db_exact, db_normalized, db_pure)
    upload_date = raw.get("upload_date") or LEGACY_UPLOAD_DATE
    if match:
        item = _matched_item(match, filename, "", upload_date, has_custom,
                             en_titles, en_tags, en_descs)
        item["tags"] = dedupe_tags((item.get("tags") or []) + raw.get("tags", []))
    else:
        stem = re.sub(r"\.epub\s*$", "", filename, flags=re.IGNORECASE).strip()
        korean_name = extract_korean_name(filename) or stem
        item = {
            "has_meta": bool(raw.get("tags")), "is_custom": has_custom,
            "id": extract_novel_id(filename), "filename": filename, "tg_link": "",
            "title_en": stem, "title_kr": korean_name, "author": "Unknown",
            "cover": "", "tags": dedupe_tags(raw.get("tags", [])),
            "synopsis": "Raw EPUB from the master library.",
            "views": 0, "likes": 0, "chapters": 0, "age": 0,
            "complete": raw.get("complete") or 0, "upload_date": upload_date,
        }
    item["raw_tg_link"] = raw["raw_tg_link"]
    item["_library_key"] = raw["_key"]
    item["_source_ids"] = list(raw.get("_source_ids") or [])
    item["is_raw_only"] = True
    if raw.get("complete") is not None:
        item["complete"] = raw["complete"]
    if has_custom:
        _apply_custom_overrides(item, c_data)
    return item

def _format_uploader_name(email_or_name):
    if not email_or_name:
        return "Anonymous"
    val = str(email_or_name).strip()
    if "@" in val:
        prefix = val.split("@")[0].strip()
        return prefix if prefix else val
    return val

def _uploaded_gallery_item(upload_id, record):
    if not isinstance(record, dict) or not record.get("approved", False):
        return None
    folder_name = str(record.get("local_folder") or "").strip()
    folder_path = os.path.join(LOCAL_OUTPUT_DIR, folder_name) if folder_name else ""
    has_local_read = bool(folder_name and os.path.isdir(folder_path))
    cover_file_path = str(record.get("cover_file_path") or "")
    cover_url = (
        f"/api/upload/{quote(str(upload_id), safe='')}/asset/cover"
        if cover_file_path and os.path.isfile(cover_file_path)
        else ""
    )
    raw_epub_path = str(record.get("raw_epub_path") or "")
    translated_epub_path = str(record.get("translated_epub_path") or "")
    raw_original_name = str(record.get("raw_original_name") or "")
    translated_original_name = str(record.get("translated_original_name") or "")
    return {
        "has_meta": True,
        "is_custom": False,
        "uploaded": True,
        "id": str(upload_id),
        "filename": translated_original_name or raw_original_name or f"{upload_id}.epub",
        "title_en": str(record.get("title_en") or "Untitled"),
        "title_kr": str(record.get("raw_title") or ""),
        "author": str(record.get("author") or "Unknown"),
        "cover": cover_url,
        "tags": dedupe_tags(record.get("tags") or []),
        "synopsis": str(record.get("description") or ""),
        "views": 0,
        "likes": 0,
        "chapters": (
            _count_readable_chapters(folder_path) if has_local_read else 0
        ),
        "age": 0,
        "complete": 0,
        "upload_date": str(record.get("upload_date") or LEGACY_UPLOAD_DATE),
        "tg_link": "",
        "raw_tg_link": "",
        "translated_epub_path": (
            translated_epub_path if os.path.isfile(translated_epub_path) else ""
        ),
        "raw_epub_path": raw_epub_path if os.path.isfile(raw_epub_path) else "",
        "uploader_email": str(record.get("uploader_email") or ""),
        "uploader_name": _format_uploader_name(
            record.get("uploader_name") or record.get("uploader_email")
        ),
        "local_folder": folder_name if has_local_read else "",
        "has_local_read": has_local_read,
        "is_raw_only": not bool(translated_epub_path),
        "_library_key": f"user-upload:{upload_id}",
        "_source_ids": [],
    }

def _uploaded_gallery_items():
    """Convert approved user_uploads.json records into normal gallery items."""
    return [
        item
        for upload_id, record in load_user_uploads().items()
        if (item := _uploaded_gallery_item(upload_id, record)) is not None
    ]

def _gallery_identity(item):
    if item.get("id"):
        return f"id:{item['id']}"
    if item.get("_library_key"):
        return item["_library_key"]
    filename = item.get("filename", "")
    stem = re.sub(r"\.epub\s*$", "", str(filename), flags=re.IGNORECASE).strip()
    return f"title:{normalize_korean_key(stem)}"

def _merge_gallery_pair(left, right):
    left_date = left.get("upload_date") or ""
    right_date = right.get("upload_date") or ""
    winner, other = (right, left) if right_date > left_date else (left, right)
    if not winner.get("tg_link") and other.get("tg_link"):
        winner["tg_link"] = other["tg_link"]
    if not winner.get("raw_tg_link") and other.get("raw_tg_link"):
        winner["raw_tg_link"] = other["raw_tg_link"]
    winner["tags"] = dedupe_tags(
        (winner.get("tags") or []) + (other.get("tags") or [])
    )
    winner["complete"] = max(to_int(winner.get("complete"), 0),
                             to_int(other.get("complete"), 0))
    winner["has_local_read"] = bool(winner.get("has_local_read") or other.get("has_local_read"))
    if other.get("is_updated"):
        winner["is_updated"] = True
        if other.get("local_folder"):
            winner["local_folder"] = other["local_folder"]
        if other.get("translated_epub_path"):
            winner["translated_epub_path"] = other["translated_epub_path"]
            winner["translated_original_name"] = other.get("translated_original_name", "")
            winner["has_download"] = True
        if other.get("chapters"):
            winner["chapters"] = max(to_int(winner.get("chapters"), 0), to_int(other.get("chapters"), 0))
    elif not winner.get("local_folder") and other.get("local_folder"):
        winner["local_folder"] = other["local_folder"]

    if winner.get("is_updated"):
        if not winner.get("translated_epub_path") and other.get("translated_epub_path"):
            winner["translated_epub_path"] = other["translated_epub_path"]
            winner["translated_original_name"] = other.get("translated_original_name", "")
            winner["has_download"] = True

    winner["_source_ids"] = sorted(set(
        (winner.get("_source_ids") or []) + (other.get("_source_ids") or [])
    ))
    winner["is_raw_only"] = not bool(winner.get("tg_link") or winner.get("translated_epub_path"))
    return winner

def _dedupe_gallery_items(items):
    by_title = {}
    for item in items:
        key = _gallery_identity(item)
        by_title[key] = (_merge_gallery_pair(by_title[key], item)
                         if key in by_title else item)

    stage = list(by_title.values())
    parent = list(range(len(stage)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    id_owner = {}
    for index, item in enumerate(stage):
        source_ids = list(item.get("_source_ids") or [])
        direct_id = extract_novel_id(item.get("filename", ""))
        if direct_id and direct_id not in source_ids:
            source_ids.append(direct_id)
        for source_id in source_ids:
            if source_id in id_owner:
                union(index, id_owner[source_id])
            else:
                id_owner[source_id] = index

    collapsed = {}
    for index, item in enumerate(stage):
        root = find(index)
        collapsed[root] = (_merge_gallery_pair(collapsed[root], item)
                           if root in collapsed else item)
    return list(collapsed.values())

def _apply_custom_overrides(item, c_data):
    if c_data.get("title_en", "").strip():
        item["title_en"] = c_data["title_en"].strip()
    if c_data.get("title_kr", "").strip():
        item["title_kr"] = c_data["title_kr"].strip()
    if c_data.get("author", "").strip():
        item["author"] = c_data["author"].strip()
    c_cover = c_data.get("cover", "").strip()
    if c_cover and not c_cover.startswith("data:image"):
        item["cover"] = c_cover
    c_tags = [t for t in c_data.get("tags", []) if t and t != "Unmatched"]
    if c_tags:
        item["tags"] = dedupe_tags(c_tags)
    if c_data.get("synopsis", "").strip():
        item["synopsis"] = c_data["synopsis"].strip()
    item["has_meta"] = True

def _apply_structured_override(item, structured_map, consumed_ids):
    item_id = str(item.get("id") or "")
    source_ids = [str(x) for x in (item.get("_source_ids") or []) if x]
    target_id = None
    if item_id in structured_map:
        target_id = item_id
    else:
        for sid in source_ids:
            if sid in structured_map:
                target_id = sid
                break
    if target_id and target_id in structured_map:
        info = structured_map[target_id]
        item["has_local_read"] = True
        item["local_folder"] = f"structured:{target_id}"
        item["is_updated"] = True
        if info.get("chapter_count"):
            item["chapters"] = max(to_int(item.get("chapters"), 0), info["chapter_count"])
        if info.get("epub_path"):
            item["translated_epub_path"] = info["epub_path"]
            item["translated_original_name"] = os.path.basename(info["epub_path"])
            item["has_download"] = True
            item["is_raw_only"] = False
        consumed_ids.add(target_id)

def _apply_batched_raw_override(item, batched_raw_map):
    item_id = str(item.get("id") or "")
    source_ids = [str(x) for x in (item.get("_source_ids") or []) if x]
    target_id = None
    if item_id in batched_raw_map:
        target_id = item_id
    else:
        for sid in source_ids:
            if sid in batched_raw_map:
                target_id = sid
                break
    if target_id and target_id in batched_raw_map:
        raw_path = batched_raw_map[target_id]
        item["raw_epub_path"] = raw_path
        item["raw_original_name"] = os.path.basename(raw_path)
        item["has_raw"] = True

def _build_gallery_items():
    folder_cjk_map, folder_en_map = _scan_local_folders()
    structured_map = _scan_structured_output()
    batched_raw_map = _scan_batched_epubs()
    consumed_structured_ids = set()
    en_titles = load_text_map(TITLES_EN_PATH, key_is_int=True)
    en_tags = load_text_map(TAGS_EN_PATH, is_tag=True)
    en_descs = load_text_map(DESC_EN_PATH, key_is_int=True)
    raw_lookup, raw_records = load_raw_library()
    db_by_id, db_exact, db_normalized, db_pure = _load_novel_db_lookups()
    custom_meta = load_custom_meta()
    items = []
    consumed_raw_keys = set()

    if os.path.exists(TRANSLATED_CSV_PATH):
        try:
            with open(TRANSLATED_CSV_PATH, "r", encoding="utf-8-sig", newline="") as fh:
                reader = csv.reader(fh)
                next(reader, None)
                for row in reader:
                    try:
                        if len(row) < 2:
                            continue
                        filename = row[0].strip()
                        tg_link = row[1].strip()
                        upload_date = row[2].strip() if len(row) > 2 else LEGACY_UPLOAD_DATE
                        pure_cjk = get_pure_cjk(filename)
                        novel_id = extract_novel_id(filename)
                        korean_name = extract_korean_name(filename)
                        has_custom = filename in custom_meta
                        c_data = custom_meta.get(filename, {}) if has_custom else {}
                        custom_cjk = c_data.get("title_kr", "").strip()

                        match = _match_metadata(filename, custom_cjk, db_by_id, db_exact, db_normalized, db_pure)

                        if match:
                            item = _matched_item(match, filename, tg_link, upload_date,
                                                 has_custom, en_titles, en_tags, en_descs)
                        else:
                            item = _unmatched_item(filename, tg_link, upload_date,
                                                   has_custom, custom_cjk or korean_name)
                        if has_custom:
                            _apply_custom_overrides(item, c_data)

                        search_name = (item["title_kr"].strip() if item.get("title_kr") else "") or korean_name
                        raw_record = (
                            (raw_lookup.get(f"id:{novel_id}") if novel_id else "")
                            or raw_lookup.get(f"kr:{normalize_korean_key(search_name)}")
                            or raw_lookup.get(f"cjk:{get_pure_cjk(search_name)}", "")
                        )
                        item["raw_tg_link"] = raw_record.get("raw_tg_link", "") if raw_record else ""
                        item["_library_key"] = (raw_record["_key"] if raw_record
                                                else _raw_record_key(filename))
                        item["_source_ids"] = (list(raw_record.get("_source_ids") or [])
                                               if raw_record else ([novel_id] if novel_id else []))
                        item["is_raw_only"] = False
                        if raw_record:
                            consumed_raw_keys.add(raw_record["_key"])
                            item["tags"] = dedupe_tags(
                                (item.get("tags") or []) + (raw_record.get("tags") or [])
                            )
                            if raw_record.get("complete") is not None:
                                item["complete"] = raw_record["complete"]
                        search_cjk = get_pure_cjk(item["title_kr"]) if item["title_kr"] else pure_cjk
                        search_en = get_pure_english(item["title_en"]) if item["title_en"] else get_pure_english(filename)
                        target_folder = folder_cjk_map.get(search_cjk) or folder_en_map.get(search_en)
                        item["local_folder"] = target_folder
                        item["has_local_read"] = bool(target_folder)
                        _apply_structured_override(item, structured_map, consumed_structured_ids)
                        items.append(item)
                    except Exception as exc:
                        print(f"[WARN] Skipping malformed tracker row {row!r}: {exc}")
        except OSError as exc:
            print(f"[WARN] Could not read {TRANSLATED_CSV_PATH}: {exc}")

    for raw in raw_records:
        if raw["_key"] in consumed_raw_keys:
            continue
        item = _raw_only_item(raw, custom_meta, db_by_id, db_exact, db_normalized, db_pure,
                              en_titles, en_tags, en_descs)
        search_cjk = get_pure_cjk(item.get("title_kr", ""))
        search_en = get_pure_english(item.get("title_en", ""))
        target_folder = folder_cjk_map.get(search_cjk) or folder_en_map.get(search_en)
        item["local_folder"] = target_folder
        item["has_local_read"] = bool(target_folder)
        _apply_structured_override(item, structured_map, consumed_structured_ids)
        items.append(item)

    for nid, info in structured_map.items():
        if nid in consumed_structured_ids:
            continue
        match = db_by_id.get(nid)
        epub_fname = os.path.basename(info.get("epub_path") or f"{nid}.epub")
        if not match:
            # Try title lookup from EPUB filename
            cjk = extract_korean_name(epub_fname)
            if cjk:
                match = db_exact.get(cjk) or db_normalized.get(_metadata_title_key(cjk)) or db_pure.get(get_pure_cjk(cjk))

        if match:
            item = _matched_item(match, epub_fname, "", LEGACY_UPLOAD_DATE,
                                 False, en_titles, en_tags, en_descs)
        else:
            stem = re.sub(r"\.epub\s*$", "", epub_fname, flags=re.IGNORECASE).strip()
            korean_name = extract_korean_name(epub_fname) or f"소설 {nid}"
            title_en_cand = stem
            author_cand = "Unknown"
            if "(" in stem and ")" in stem:
                outside = stem.split("(")[0].strip()
                inside = stem.split("(")[1].split(")")[0].strip()
                if outside:
                    title_en_cand = re.sub(r"^\d+[_\s\-]+", "", outside).strip()
                if " - " in inside:
                    author_cand = inside.split(" - ")[-1].strip()
                    korean_name = re.sub(r"^\d+[_\s\-]+", "", inside.split(" - ")[0]).strip()

            cover_url = ""
            for cov_name in ["cover.jpg", "cover.png", "cover.webp", "images/cover.jpg"]:
                cov_path = os.path.join(info.get("folder_path", ""), cov_name)
                if os.path.isfile(cov_path):
                    cover_url = f"/api/read/{nid}/asset/{cov_name}"
                    break

            item = {
                "has_meta": False, "is_custom": False, "id": nid,
                "filename": epub_fname, "tg_link": "",
                "title_en": en_titles.get(to_int(nid, 0), title_en_cand) or f"Novel {nid}",
                "title_kr": korean_name,
                "author": author_cand,
                "cover": cover_url,
                "tags": ["Updated"],
                "synopsis": en_descs.get(to_int(nid, 0), "Updated novel from structured output."),
                "views": 0, "likes": 0, "chapters": 0, "age": 0,
                "complete": 0, "upload_date": LEGACY_UPLOAD_DATE,
            }
        item["has_local_read"] = True
        item["local_folder"] = f"structured:{nid}"
        item["is_updated"] = True
        if info.get("chapter_count"):
            item["chapters"] = info["chapter_count"]
        if info.get("epub_path"):
            item["translated_epub_path"] = info["epub_path"]
            item["translated_original_name"] = os.path.basename(info["epub_path"])
            item["has_download"] = True
            item["is_raw_only"] = False
        item["_library_key"] = f"structured:{nid}"
        item["_source_ids"] = [nid]
        items.append(item)

    items.extend(_uploaded_gallery_items())
    deduped = _dedupe_gallery_items(items)
    for item in deduped:
        _apply_batched_raw_override(item, batched_raw_map)
        if _novel_is_adult(item):
            item["age"] = 19
    return deduped

_gallery_cache = {"items": [], "gen": None}
_GALLERY_CACHE_LOCK = threading.Lock()

def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0

def load_gallery_data():
    generation = LIBRARY_INDEX.generation()
    with _GALLERY_CACHE_LOCK:
        cache = _gallery_cache
        if cache["items"] and cache["gen"] == generation:
            return cache["items"]
        items = LIBRARY_INDEX.all_items()
        cache.update(items=items, gen=generation)
        return items

def find_novel(novel_id):
    return LIBRARY_INDEX.lookup(str(novel_id).strip())

def novel_key(novel):
    return str(novel.get("id")) if novel.get("id") else novel.get("filename", "")

# ====================================================================
# 8b. NOVELPIA NOTICE-IMAGE GALLERY
# ====================================================================

NOTICE_DIR = os.path.join(META_DIR, "notice_images")
NP_IMG_CACHE = os.path.join(META_DIR, "np_image_cache")

NP_CDN_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"
    ),
    "Referer": "https://novelpia.com/",
    "Origin": "https://novelpia.com",
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

_notice_cache = {}
_NOTICE_LOCK = threading.Lock()

def _notice_manifest_path(np_id):
    return os.path.join(NOTICE_DIR, f"image_gallery_{np_id}.json")

def load_notice_manifest(np_id):
    np_id = str(np_id)
    path = _notice_manifest_path(np_id)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    with _NOTICE_LOCK:
        cached = _notice_cache.get(np_id)
        if cached and cached["mtime"] == mtime:
            return cached["data"]
    data = read_json_file(path, None)
    if not isinstance(data, dict):
        return None
    with _NOTICE_LOCK:
        _notice_cache[np_id] = {
            "mtime": mtime,
            "data": data,
        }
    return data

def _resolve_novelpia_key(novel):
    if not novel:
        return ""
    nid = str(novel.get("id") or "").strip()
    return nid if nid.isdigit() else ""

def _normalize_cdn_url(src):
    src = str(src or "").strip()
    if not src:
        return ""

    src = src.replace("\\/", "/").replace("&amp;", "&")

    for sep in ('"', "'", "\\", "<", ">", " "):
        src = src.split(sep)[0]

    if src.startswith("//"):
        src = "https:" + src
    if src.startswith("http://"):
        src = "https://" + src[len("http://"):]
    if not src.startswith("https://images.novelpia.com/"):
        return ""

    return src

def _cache_cdn_image(url):
    url = _normalize_cdn_url(url)
    if not url:
        return None, None
    key = hashlib.md5(url.encode("utf-8")).hexdigest()
    path = os.path.join(NP_IMG_CACHE, key)

    if os.path.isfile(path) and os.path.getsize(path) > 0:
        try:
            with open(path, "rb") as fh:
                head = fh.read(256)
            mime = _sniff_image_mime(head)
            if mime.startswith("image/"):
                return path, mime
            try:
                os.remove(path)
            except OSError:
                pass
        except OSError:
            pass

    os.makedirs(NP_IMG_CACHE, exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
    try:
        with requests.get(
            url,
            headers=NP_CDN_HEADERS,
            stream=True,
            timeout=30,
            allow_redirects=True,
        ) as resp:
            content_type = (resp.headers.get("Content-Type") or "").lower()
            if resp.status_code != 200:
                print(f"[WARN] CDN image status {resp.status_code}: {url}")
                return None, None

            iterator = resp.iter_content(64 * 1024)
            first_chunk = b""
            for chunk in iterator:
                if chunk:
                    first_chunk = chunk
                    break

            if not first_chunk:
                print(f"[WARN] CDN returned empty image response: {url}")
                return None, None

            mime = _sniff_image_mime(first_chunk[:256])
            if not mime.startswith("image/"):
                print(
                    f"[WARN] CDN returned non-image for {url}; "
                    f"content-type={content_type!r}; "
                    f"head={first_chunk[:100]!r}"
                )
                return None, None

            with open(tmp, "wb") as out:
                out.write(first_chunk)
                for chunk in iterator:
                    if chunk:
                        out.write(chunk)
            os.replace(tmp, path)
            return path, mime
    except (requests.RequestException, OSError) as exc:
        print(f"[WARN] Could not cache CDN image {url}: {exc}")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return None, None

# ====================================================================
# 8c. TAG SIMILARITY & RECOMMENDATIONS
# ====================================================================
SIMILAR_MAX = 150
_GENERIC_AUTHORS = ("", "Unknown", "Raw Upload")
_REC_STATUS_WEIGHTS = {"reading": 1.5, "finished": 1.0, "want_to_read": 0.5}
_REC_PROFILE_MAX_ITEMS = 64
_REC_ENUM_TAGS = 24
_REL_FLOOR = 0.05
_QUALITY_PSEUDO_VIEWS = 300.0
_AUTHOR_BONUS = 0.15
_QUALITY_WEIGHT = 0.10
_CONFIDENCE_WEIGHT = 0.03
_SIMILAR_AUTHOR_CAP = 3
_REC_AUTHOR_CAP = 2
_REC_DL_WEIGHT = 0.8
_ADULT_PROFILE_SHARE = 0.25
_COMMUNITY_WEIGHT = 0.05
_TRENDING_DAYS = 7
_TRENDING_MIN_READERS = 2
_GEM_INTERVAL = 5
_GEM_QUALITY_PCT = 0.85
_GEM_LOGV_PCT = 0.35
_CF_WEIGHT = 0.25
_CF_NEIGHBORS = 30
_CF_MIN_CO = 2
_CF_SHRINK = 3.0
_CF_TTL = 900
_CF_MAX_ITEMS_PER_USER = 64
_MIN_RECOMMEND_CHAPTERS = 20

_SIM_LOCK = threading.Lock()
_sim_state = {"gen": None, "novel_tags": {}, "tag_novels": {}, "tag_idf": {},
              "by_key": {}, "topk": {}, "novel_norm": {}, "author_key": {},
              "author_novels": {}, "quality": {}, "logv": {},
              "common_tags": set(), "popular_order": []}

_NON_ADULT_19_TAGS = {
    "1984", "1990s", "1994 game", "19th century", "3819", "reverse: 1999", "19yo"
}

def _novel_is_adult(novel):
    if not isinstance(novel, dict):
        return False
    if to_int(novel.get("age"), 0) == 19:
        return True
    tags = novel.get("tags") or []
    for t in tags:
        if not t:
            continue
        t_str = str(t).strip()
        t_lower = t_str.lower()
        if t_lower in _NON_ADULT_19_TAGS:
            continue
        if (
            "19금" in t_str
            or "r-19" in t_lower
            or "r19" in t_lower
            or "19+" in t_lower
            or "adult" in t_lower
            or "떡타지" in t_str
            or "고수위" in t_str
            or "야설" in t_str
            or "야겜" in t_str
            or "조교" in t_str
            or "능욕" in t_str
            or "smut" in t_lower
        ):
            return True
    return False

def _novel_is_recommendable(novel):
    return to_int(novel.get("chapters"), 0) >= _MIN_RECOMMEND_CHAPTERS

def _similarity_state():
    items = load_gallery_data()
    with _GALLERY_CACHE_LOCK:
        gen = _gallery_cache["gen"]
    with _SIM_LOCK:
        if _sim_state["gen"] == gen:
            return _sim_state
        tag_novels = defaultdict(set)
        novel_tags, by_key = {}, {}
        tag_display = {}
        for n in items:
            key = novel_key(n)
            by_key[key] = n
            tags_orig = [t for t in n.get("tags", []) if t and t != "Unmatched"]
            tags_lower = set()
            for t in tags_orig:
                t_str = str(t).strip()
                if not t_str:
                    continue
                t_lower = t_str.lower()
                tags_lower.add(t_lower)
                if t_lower not in tag_display or (tag_display[t_lower].islower() and not t_str.islower()):
                    tag_display[t_lower] = t_str
            novel_tags[key] = tags_lower
            for t_lower in tags_lower:
                tag_novels[t_lower].add(key)
        total = max(1, len(by_key))
        tag_idf = {t: math.log(total / len(keys)) for t, keys in tag_novels.items()}
        common_df = max(50, int(total * 0.25))
        common_tags = {t for t, keys in tag_novels.items() if len(keys) > common_df}
        novel_norm = {k: math.sqrt(sum(tag_idf[t] ** 2 for t in tags))
                      for k, tags in novel_tags.items()}

        author_key = {}
        author_novels = defaultdict(list)
        total_likes = total_views = 0
        for k, n in by_key.items():
            author = (n.get("author") or "").strip()
            author_key[k] = author
            if author not in _GENERIC_AUTHORS:
                author_novels[author].append(k)
            total_likes += max(0, to_int(n.get("likes"), 0))
            total_views += max(0, to_int(n.get("views"), 0))
        p0 = min(0.5, max(1e-4, total_likes / total_views)) if total_views > 0 else 0.01
        quality, logv = {}, {}
        for k, n in by_key.items():
            likes = max(0, to_int(n.get("likes"), 0))
            views = max(0, to_int(n.get("views"), 0))
            q_raw = (likes + _QUALITY_PSEUDO_VIEWS * p0) / (views + _QUALITY_PSEUDO_VIEWS)
            quality[k] = q_raw / (q_raw + p0)
            logv[k] = min(1.0, math.log10(1 + views) / 6.0)
        for author in author_novels:
            author_novels[author].sort(key=lambda k: to_int(by_key[k].get("likes"), 0), reverse=True)
        popular_order = sorted(by_key, key=lambda k: 0.6 * quality[k] + 0.4 * logv[k], reverse=True)

        _sim_state.update(gen=gen, novel_tags=novel_tags, tag_novels=dict(tag_novels),
                          tag_idf=tag_idf, by_key=by_key, topk={}, novel_norm=novel_norm,
                          author_key=author_key, author_novels=dict(author_novels),
                          quality=quality, logv=logv, common_tags=common_tags,
                          popular_order=popular_order, tag_display=tag_display)
        return _sim_state

def _accumulate_overlap(tag_contrib, state, skip_keys, max_enum=None):
    enum_tags, backfill = [], {}
    for t, contrib in tag_contrib.items():
        if contrib <= 0:
            continue
        if t in state["common_tags"]:
            backfill[t] = contrib
        else:
            enum_tags.append((contrib, t))
    if max_enum is not None and len(enum_tags) > max_enum:
        enum_tags.sort(reverse=True)
        for contrib, t in enum_tags[max_enum:]:
            backfill[t] = contrib
        enum_tags = enum_tags[:max_enum]

    overlap = defaultdict(float)
    for contrib, t in enum_tags:
        for k in state["tag_novels"].get(t, ()):
            if k not in skip_keys:
                overlap[k] += contrib
    if not overlap and backfill:
        t = min(backfill, key=lambda tag: len(state["tag_novels"].get(tag, ())))
        contrib = backfill.pop(t)
        for k in state["tag_novels"].get(t, ()):
            if k not in skip_keys:
                overlap[k] += contrib
    if backfill and overlap:
        for k in overlap:
            extra = 0.0
            for t in state["novel_tags"].get(k) or ():
                add = backfill.get(t)
                if add:
                    extra += add
            if extra:
                overlap[k] += extra
    return overlap

def _quality_nudge(state, key):
    return (_QUALITY_WEIGHT * (state["quality"].get(key, 0.5) - 0.5) * 2.0
            + _CONFIDENCE_WEIGHT * state["logv"].get(key, 0.0))

_COMMUNITY_LOCK = threading.Lock()
_community_cache = {"mtime": None, "ts": 0.0, "engagers": {}, "recent": {}}

def _record_is_engaged(rec):
    return (isinstance(rec, dict)
            and (_REC_STATUS_WEIGHTS.get(rec.get("status"), 0.0) > 0
                 or to_int(rec.get("dl"), 0) > 0))

def _community_stats():
    now = time.time()
    mtime = _mtime(USER_DATA_PATH)
    with _COMMUNITY_LOCK:
        if _community_cache["mtime"] == mtime or now - _community_cache["ts"] < 60:
            return _community_cache
    engagers, recent = defaultdict(int), defaultdict(int)
    cutoff = now - _TRENDING_DAYS * 86400
    for records in load_user_data().values():
        if not isinstance(records, dict):
            continue
        for key, rec in records.items():
            if not _record_is_engaged(rec):
                continue
            engagers[key] += 1
            if max(rec.get("last_read", 0), rec.get("last_dl", 0)) >= cutoff:
                recent[key] += 1
    with _COMMUNITY_LOCK:
        _community_cache.update(mtime=mtime, ts=now,
                                engagers=dict(engagers), recent=dict(recent))
        return _community_cache

def _community_bonus(engagers, key):
    n = engagers.get(key, 0)
    if n <= 0:
        return 0.0
    return _COMMUNITY_WEIGHT * min(1.0, math.log10(1 + n) / 2.0)

_CF_LOCK = threading.Lock()
_cf_state = {"neighbors": {}, "built_mtime": None, "ts": 0.0, "building": False, "gen": 0}

def _build_cf_neighbors():
    item_users = defaultdict(int)
    pair_counts = defaultdict(int)
    for records in load_user_data().values():
        if not isinstance(records, dict):
            continue
        items = sorted(
            ((max(rec.get("last_read", 0), rec.get("last_dl", 0)), key)
             for key, rec in records.items() if _record_is_engaged(rec)),
            reverse=True,
        )
        keys = sorted(k for _, k in items[:_CF_MAX_ITEMS_PER_USER])
        for k in keys:
            item_users[k] += 1
        for i, ki in enumerate(keys):
            for kj in keys[i + 1:]:
                pair_counts[(ki, kj)] += 1
    neighbors = defaultdict(list)
    for (a, b), c in pair_counts.items():
        if c < _CF_MIN_CO:
            continue
        sim = (c / math.sqrt(item_users[a] * item_users[b])) * (c / (c + _CF_SHRINK))
        neighbors[a].append((sim, b))
        neighbors[b].append((sim, a))
    out = {}
    for k, lst in neighbors.items():
        lst.sort(reverse=True)
        out[k] = lst[:_CF_NEIGHBORS]
    return out

def _rebuild_cf(mtime):
    try:
        built = _build_cf_neighbors()
        with _CF_LOCK:
            _cf_state.update(neighbors=built, built_mtime=mtime, ts=time.time(),
                             gen=_cf_state["gen"] + 1)
        print(f"[CF] Co-occurrence rebuilt: {len(built)} novels with neighbours.")
    except Exception as exc:
        print(f"[WARN] CF rebuild failed: {exc}")
    finally:
        with _CF_LOCK:
            _cf_state["building"] = False

def _get_cf_neighbors():
    now = time.time()
    mtime = _mtime(USER_DATA_PATH)
    with _CF_LOCK:
        stale = (_cf_state["built_mtime"] is None
                 or (_cf_state["built_mtime"] != mtime and now - _cf_state["ts"] >= _CF_TTL))
        if stale and not _cf_state["building"]:
            _cf_state["building"] = True
            threading.Thread(target=_rebuild_cf, args=(mtime,), daemon=True).start()
        return _cf_state["neighbors"], _cf_state["gen"]

def _cap_authors(scored, state, cap, limit):
    out, per_author = [], {}
    for item in scored:
        author = state["author_key"].get(item[1], "")
        if author and author not in _GENERIC_AUTHORS:
            seen = per_author.get(author, 0)
            if seen >= cap:
                continue
            per_author[author] = seen + 1
        out.append(item)
        if len(out) >= limit:
            break
    return out

def _rank_similar(seed, state, cf_map, engagers):
    seed_key = novel_key(seed)
    seed_tags = state["novel_tags"].get(seed_key) or set()
    seed_norm = state["novel_norm"].get(seed_key, 0.0)
    has_tags = bool(seed_tags) and seed_norm > 0
    if not has_tags and not cf_map:
        return []
    seed_author = (seed.get("author") or "").strip()
    adult_ok = _novel_is_adult(seed)

    idf = state["tag_idf"]
    overlap = {}
    if has_tags:
        contrib = {t: idf.get(t, 0.0) ** 2 for t in seed_tags}
        overlap = _accumulate_overlap(contrib, state, {seed_key})

    scored = []
    for key in set(overlap) | set(cf_map):
        if key == seed_key:
            continue
        cand = state["by_key"].get(key)
        if cand is None or not _novel_is_recommendable(cand):
            continue
        if not adult_ok and _novel_is_adult(cand):
            continue
        cand_norm = state["novel_norm"].get(key, 0.0)
        rel = (overlap.get(key, 0.0) / (seed_norm * cand_norm)
               if has_tags and cand_norm > 0 else 0.0)
        cf_s = cf_map.get(key, 0.0)
        if rel < _REL_FLOOR:
            if cf_s <= 0:
                continue
            rel = 0.0
        score = (rel + _CF_WEIGHT * min(1.0, cf_s)
                 + _quality_nudge(state, key) + _community_bonus(engagers, key))
        if seed_author not in _GENERIC_AUTHORS and state["author_key"].get(key) == seed_author:
            score += _AUTHOR_BONUS
        if rel > 0:
            shared_lowers = sorted(seed_tags & (state["novel_tags"].get(key) or set()),
                                   key=lambda t: idf.get(t, 0.0), reverse=True)[:3]
            shared = [state.get("tag_display", {}).get(t, t) for t in shared_lowers]
            why = "tags"
        else:
            shared, why = [], "readers"
        scored.append((score, key, shared, why))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return _cap_authors(scored, state, _SIMILAR_AUTHOR_CAP, SIMILAR_MAX)

def get_similar_novels(seed, limit):
    state = _similarity_state()
    cf_neighbors, cf_gen = _get_cf_neighbors()
    engagers = _community_stats()["engagers"]
    seed_key = novel_key(seed)
    with _SIM_LOCK:
        if state.get("topk_cf_gen") != cf_gen:
            state["topk"] = {}
            state["topk_cf_gen"] = cf_gen
        ranked = state["topk"].get(seed_key)
    if ranked is None:
        cf_map = {k: s for s, k in cf_neighbors.get(seed_key, ())}
        ranked = _rank_similar(seed, state, cf_map, engagers)
        with _SIM_LOCK:
            state["topk"][seed_key] = ranked
    results = [(score, state["by_key"][key], {"reason": why, "shared_tags": shared})
               for score, key, shared, why in ranked[:limit] if key in state["by_key"]]
    if results:
        rng = _seeded_rng(seed_key)
        exclude = {seed_key} | {key for _s, key, _sh, _w in ranked}
        results = _sprinkle_gems(
            results, lambda gk: (0.0, state["by_key"][gk], {"reason": "gem"}),
            state, exclude, _novel_is_adult(seed), rng)
        return "tags", results

    adult_ok = _novel_is_adult(seed)
    seed_author = (seed.get("author") or "").strip()
    if seed_author not in _GENERIC_AUTHORS:
        same_author = [k for k in state["author_novels"].get(seed_author, [])
                       if k != seed_key
                       and _novel_is_recommendable(state["by_key"][k])
                       and (adult_ok or not _novel_is_adult(state["by_key"][k]))]
        if same_author:
            return "author", [(0.0, state["by_key"][k], {"reason": "author"})
                              for k in same_author[:limit]]

    popular = [k for k in state["popular_order"]
               if k != seed_key
               and _novel_is_recommendable(state["by_key"][k])
               and (adult_ok or not _novel_is_adult(state["by_key"][k]))]
    return "popular", [(0.0, state["by_key"][k], {"reason": "popular"})
                       for k in popular[:limit]]

def _seeded_rng(email):
    seed = hashlib.sha256(f"{email}:{date.today().isoformat()}".encode("utf-8")).hexdigest()
    return random.Random(int(seed[:12], 16))

def _profile_weight(record, novel):
    base = _REC_STATUS_WEIGHTS.get(record.get("status"), 0.0)
    if to_int(record.get("dl"), 0) > 0:
        base = max(base, _REC_DL_WEIGHT)
    if base <= 0:
        return 0.0
    if record.get("status") == "finished":
        ratio = 1.0
    else:
        chapters = to_int(novel.get("chapters"), 0)
        ratio = (min(1.0, max(0.0, to_int(record.get("progress"), 0) / chapters))
                 if chapters > 0 else 0.5)
    return base * (0.5 + ratio)

def _exploration_gems(state, exclude, allow_adult, rng, count=2):
    quality, logv = state["quality"], state["logv"]
    if not quality:
        return []
    q_vals = sorted(quality.values())
    l_vals = sorted(logv.values())
    q_floor = q_vals[min(len(q_vals) - 1, int(len(q_vals) * _GEM_QUALITY_PCT))]
    l_ceil = l_vals[min(len(l_vals) - 1, int(len(l_vals) * _GEM_LOGV_PCT))]
    pool = []
    for k, q in quality.items():
        if q < q_floor or logv.get(k, 0.0) > l_ceil:
            continue
        if k in exclude or not state["novel_tags"].get(k):
            continue
        cand = state["by_key"].get(k)
        if (cand is None or not _novel_is_recommendable(cand)
                or (not allow_adult and _novel_is_adult(cand))):
            continue
        pool.append(k)
    if not pool:
        return []
    pool.sort()
    return rng.sample(pool, min(count, len(pool)))

def _sprinkle_gems(items, make_gem, state, exclude, allow_adult, rng, interval=_GEM_INTERVAL):
    if not items:
        return items
    count = (len(items) + interval - 1) // interval
    gems = _exploration_gems(state, exclude, allow_adult, rng, count=count)
    if not gems:
        return items
    out = []
    gi = 0
    for i, item in enumerate(items, start=1):
        out.append(item)
        if i % interval == 0 and gi < len(gems):
            out.append(make_gem(gems[gi]))
            gi += 1
    return out

def get_user_recommendations(email, limit):
    state = _similarity_state()
    cf_neighbors, _cf_gen = _get_cf_neighbors()
    engagers = _community_stats()["engagers"]
    udata = load_user_data().get(email, {})
    saved = {k for k, r in udata.items() if isinstance(r, dict)}

    entries = []
    for k, r in udata.items():
        if not isinstance(r, dict) or r.get("hidden") or k not in state["by_key"]:
            continue
        weight = _profile_weight(r, state["by_key"][k])
        if weight > 0:
            entries.append((k, r, weight))
    entries.sort(key=lambda e: max(e[1].get("last_read", 0), e[1].get("last_dl", 0)),
                 reverse=True)
    entries = entries[:_REC_PROFILE_MAX_ITEMS]

    idf = state["tag_idf"]
    profile = defaultdict(float)
    author_saves = defaultdict(int)
    entry_weights = {}
    adult_entries = 0
    for key, record, weight in entries:
        entry_weights[key] = weight
        if _novel_is_adult(state["by_key"][key]):
            adult_entries += 1
        author = state["author_key"].get(key, "")
        if author and author not in _GENERIC_AUTHORS:
            author_saves[author] += 1
        for t in state["novel_tags"].get(key, ()):
            profile[t] += weight * idf.get(t, 0.0)
    profile_norm = math.sqrt(sum(v * v for v in profile.values()))
    allow_adult = bool(entries) and adult_entries >= max(
        1, math.ceil(_ADULT_PROFILE_SHARE * len(entries)))

    cf_scores = defaultdict(float)
    total_weight = sum(entry_weights.values())
    if total_weight > 0:
        for key, weight in entry_weights.items():
            for sim, nb in cf_neighbors.get(key, ()):
                if nb not in saved:
                    cf_scores[nb] += weight * sim
        for k in cf_scores:
            cf_scores[k] /= total_weight

    if profile and profile_norm > 0:
        contrib = {t: idf.get(t, 0.0) * pv for t, pv in profile.items()}
        overlap = _accumulate_overlap(contrib, state, saved, max_enum=_REC_ENUM_TAGS)
        scored = []
        for key in set(overlap) | set(cf_scores):
            cand = state["by_key"].get(key)
            if cand is None or not _novel_is_recommendable(cand):
                continue
            if not allow_adult and _novel_is_adult(cand):
                continue
            cand_norm = state["novel_norm"].get(key, 0.0)
            rel = (overlap.get(key, 0.0) / (profile_norm * cand_norm)
                   if cand_norm > 0 else 0.0)
            cf_s = cf_scores.get(key, 0.0)
            if rel < _REL_FLOOR:
                if cf_s <= 0:
                    continue
                rel = 0.0
            score = (rel + _CF_WEIGHT * min(1.0, cf_s)
                     + _quality_nudge(state, key) + _community_bonus(engagers, key))
            author = state["author_key"].get(key, "")
            if author and author not in _GENERIC_AUTHORS and author_saves.get(author):
                score += _AUTHOR_BONUS * min(author_saves[author], 3) / 3.0
            if rel > 0:
                shared_lowers = sorted((t for t in (state["novel_tags"].get(key) or ()) if t in profile),
                                       key=lambda t: profile[t] * idf.get(t, 0.0), reverse=True)[:3]
                shared = [state.get("tag_display", {}).get(t, t) for t in shared_lowers]
                why = "tags"
            else:
                shared, why = [], "readers"
            scored.append((score, key, shared, why))
        scored.sort(key=lambda item: (-item[0], item[1]))
        capped = _cap_authors(scored, state, _REC_AUTHOR_CAP, max(limit, SIMILAR_MAX))
        if capped:
            rng = _seeded_rng(email)
            rotated = []
            for i in range(0, len(capped), 6):
                band = capped[i:i + 6]
                rng.shuffle(band)
                rotated.extend(band)
            exclude = saved | {item[1] for item in rotated}
            rotated = _sprinkle_gems(
                rotated, lambda gk: (0.0, gk, [], "gem"),
                state, exclude, allow_adult, rng)
            return "tags", [(score, state["by_key"][key],
                             {"reason": why, "shared_tags": shared} if why == "tags"
                             else {"reason": why})
                            for score, key, shared, why in rotated[:limit]]

    popular = [k for k in state["popular_order"]
               if k not in saved
               and _novel_is_recommendable(state["by_key"][k])
               and (allow_adult or not _novel_is_adult(state["by_key"][k]))]
    return "popular", [(0.0, state["by_key"][k], {"reason": "popular"})
                       for k in popular[:limit]]

# ====================================================================
# 8d. COMMUNITY (USERNAMES, SHARED NOVELS/COLLECTIONS, GENERAL CHAT)
# ====================================================================
COMMUNITY_PATH = os.path.join(META_DIR, "community.json")
_COMMUNITY_DATA_LOCK = threading.Lock()
COMMUNITY_MAX_SHARES = 300
COMMUNITY_MAX_CHAT = 1000
COMMUNITY_CHAT_MAX_LEN = 500
COMMUNITY_MSG_MAX_LEN = 300
COMMUNITY_COLLECTION_MAX_KEYS = 200
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")

def _load_community_unlocked():
    data = read_json_file(COMMUNITY_PATH, None)
    if not isinstance(data, dict):
        data = {}
    data.setdefault("usernames", {})
    data.setdefault("shares", [])
    data.setdefault("chat", [])
    data.setdefault("next_id", 1)
    return data

def load_community():
    with _COMMUNITY_DATA_LOCK:
        return _load_community_unlocked()

def mutate_community(mutator):
    with _COMMUNITY_DATA_LOCK:
        data = _load_community_unlocked()
        result = mutator(data)
        write_json_atomic(COMMUNITY_PATH, data, ensure_ascii=False)
        return result

def community_username(email):
    return load_community()["usernames"].get(email)

def _public_share(share, email, is_admin):
    out = {k: v for k, v in share.items() if k != "email"}
    out["mine"] = is_admin or share.get("email") == email
    return out

def _public_chat_msg(msg, email, is_admin):
    return {"id": msg["id"], "user": msg["user"], "ts": msg["ts"],
            "text": msg["text"], "mine": is_admin or msg.get("email") == email}

# ====================================================================
# 9. READER PIPELINE (TOC, CHAPTERS, ASSETS)
# ====================================================================
def novel_base_path(novel):
    loc = str(novel.get("local_folder") or "")
    if loc.startswith("structured:"):
        nid = loc.split(":", 1)[1]
        return os.path.join(STRUCTURED_OUTPUT_DIR, nid)
    if novel.get("id"):
        struct_path = os.path.join(STRUCTURED_OUTPUT_DIR, str(novel["id"]))
        if os.path.isdir(struct_path):
            return struct_path
    if loc:
        return os.path.join(LOCAL_OUTPUT_DIR, loc)
    return ""

def _fill_sequence_gaps(chapter_files):
    final_toc = []
    prev = None
    for rel_path in chapter_files:
        filename = os.path.basename(rel_path)
        match = re.search(r"^([^0-9]*)(\d+)([^0-9]*)$", filename)
        if match:
            prefix, num_str, suffix = match.groups()
            dir_path = os.path.dirname(rel_path)
            current_num = int(num_str)
            if prev:
                p_dir, p_prefix, p_num_str, p_suffix, p_num = prev
                if (p_dir == dir_path and p_prefix == prefix and p_suffix == suffix
                        and current_num > p_num + 1
                        and (current_num - p_num) < MAX_GAP_FILL):
                    pad_len = len(num_str) if num_str.startswith("0") else (
                        len(p_num_str) if p_num_str.startswith("0") else 0)
                    for missing_num in range(p_num + 1, current_num):
                        m_num = str(missing_num).zfill(pad_len) if pad_len else str(missing_num)
                        m_name = f"{prefix}{m_num}{suffix}"
                        m_rel = f"{dir_path}/{m_name}" if dir_path else m_name
                        final_toc.append(f"{TOC_MISSING_PREFIX}{m_rel}")
            prev = (dir_path, prefix, num_str, suffix, current_num)
        else:
            prev = None
        final_toc.append(rel_path)
    return final_toc

_HEADING_RE = re.compile(r"<(?:h1|h2|h3)[^>]*>([\s\S]*?)</(?:h1|h2|h3)>", re.IGNORECASE)

def _extract_heading_from_file(file_path):
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as fh:
            content = fh.read(4096)
        matches = _HEADING_RE.findall(content)
        for m in matches:
            clean = re.sub(r"<[^>]*>", "", m).strip()
            if clean and clean.lower() not in ("translated chapter", "cover", "title", "toc"):
                return clean
        p_matches = re.findall(r"<p[^>]*>([\s\S]*?)</p>", content, re.IGNORECASE)
        for p in p_matches:
            clean = re.sub(r"<[^>]*>", "", p).strip()
            if clean and len(clean) < 80 and clean.lower() not in ("translated chapter", "cover"):
                return clean
    except OSError:
        pass
    return ""

_CHAPTER_PREFIX_RE = re.compile(r"^(?:chapter|chap|ch\.?|episode|ep\.?)\s*\d+\s*[:\.-]?\s*", re.IGNORECASE)

def _clean_chapter_title(title):
    if not title:
        return ""
    cleaned = _CHAPTER_PREFIX_RE.sub("", title).strip()
    return cleaned or title

def _scan_epub_toc_titles(base_path):
    titles_map = {}
    if not os.path.isdir(base_path):
        return titles_map
    try:
        for root, _, files in os.walk(base_path):
            for f in files:
                lower_f = f.lower()
                if lower_f.endswith(CHAPTER_EXTENSIONS) and lower_f not in NON_CHAPTER_FILES:
                    fp = os.path.join(root, f)
                    heading = _extract_heading_from_file(fp)
                    if heading:
                        clean_h = _clean_chapter_title(heading)
                        if clean_h and not re.match(r"^(?:chapt|chapter|translated_chapter|chap)\d*$", clean_h, re.IGNORECASE):
                            titles_map[lower_f] = clean_h
    except Exception as exc:
        print(f"[WARN] Could not parse chapter heading titles from {base_path}: {exc}")
    return titles_map

def get_novel_toc_titles(novel):
    return LIBRARY_INDEX.chapters(novel_key(novel))[1]

def _scan_novel_toc(novel):
    base_path = novel_base_path(novel)
    chapter_files = []
    if os.path.isdir(base_path):
        for root, _, files in os.walk(base_path):
            for file in files:
                lower_f = file.lower()
                if "title_translator" in lower_f or "translated__book" in lower_f:
                    continue
                if lower_f.endswith(CHAPTER_EXTENSIONS) and lower_f not in NON_CHAPTER_FILES:
                    rel_path = os.path.relpath(os.path.join(root, file), base_path)
                    chapter_files.append(rel_path.replace("\\", "/"))
        chapter_files.sort(key=natural_sort_key)
        return _fill_sequence_gaps(chapter_files)
    return []

def _scan_novel_images(novel):
    base_path = novel_base_path(novel)
    images = set()
    if os.path.isdir(base_path):
        for root, _, files in os.walk(base_path):
            for f in files:
                if f.lower().endswith(IMAGE_EXTENSIONS):
                    rel = os.path.relpath(os.path.join(root, f), base_path)
                    images.add(rel.replace("\\", "/"))
    if not images:
        try:
            for epub_file in (f for f in os.listdir(base_path) if f.lower().endswith(".epub")):
                with zipfile.ZipFile(os.path.join(base_path, epub_file), "r") as z:
                    for zip_info in z.infolist():
                        if zip_info.filename.lower().endswith(IMAGE_EXTENSIONS):
                            images.add(zip_info.filename)
        except (OSError, zipfile.BadZipFile):
            pass
    return sorted(images)

def _indexed_novel_content(novel):
    base_path = novel_base_path(novel)
    return {
        "chapters": _scan_novel_toc(novel),
        "titles": _scan_epub_toc_titles(base_path),
        "images": _scan_novel_images(novel),
    }

def rebuild_library_index():
    """Explicitly scan source/content storage and atomically publish a new index."""
    return LIBRARY_INDEX.rebuild(
        _build_gallery_items(),
        content_loader=_indexed_novel_content,
    )

def get_novel_toc(novel):
    return LIBRARY_INDEX.chapters(novel_key(novel))[0]

def get_novel_images(novel):
    return LIBRARY_INDEX.images(novel_key(novel))

_MEDIA_REF_PATTERN = re.compile(r"(src=|href=|url\()(['\"]?)([^'\" \)>]+)\2([\s)>]|$)", re.IGNORECASE)

_SCRIPT_BLOCK_RE = re.compile(r"<script\b[^>]*>[\s\S]*?</script\s*>", re.IGNORECASE)
_ORPHAN_SCRIPT_RE = re.compile(r"</?script\b[^>]*>", re.IGNORECASE)
_FORBIDDEN_TAG_RE = re.compile(
    r"</?(?:iframe|frame|frameset|object|embed|form|meta|base|applet)\b[^>]*>",
    re.IGNORECASE,
)
_EVENT_ATTR_RE = re.compile(r"\s+on[a-z]+\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE)
_JS_URL_RE = re.compile(r"(\b(?:href|src|xlink:href)\s*=\s*[\"']?)\s*javascript:[^\"'\s>]*", re.IGNORECASE)

def sanitize_chapter_html(content):
    content = _SCRIPT_BLOCK_RE.sub("", content)
    content = _ORPHAN_SCRIPT_RE.sub("", content)
    content = _FORBIDDEN_TAG_RE.sub("", content)
    content = _EVENT_ATTR_RE.sub("", content)
    content = _JS_URL_RE.sub(r"\1#", content)
    content = re.sub(r'white-space\s*:\s*nowrap;?', '', content, flags=re.IGNORECASE)
    content = re.sub(r'width\s*:\s*\d{3,}px;?', 'max-width:100%;', content, flags=re.IGNORECASE)
    return content

def rewrite_chapter_assets(content, novel_id, chap_rel_path):
    chap_dir = os.path.dirname(chap_rel_path)
    def replace_media(match):
        attr, q, path_str, close_paren = match.group(1), match.group(2), match.group(3), match.group(4)
        clean_path_str = path_str.split("?")[0].lower()
        if not clean_path_str.endswith(REWRITABLE_ASSET_EXTENSIONS):
            return match.group(0)
        if path_str.startswith(("http", "data:")):
            return match.group(0)
        parts = (chap_dir + "/" + path_str).replace("\\", "/").split("/")
        resolved = []
        for p in parts:
            if p == "..":
                if resolved:
                    resolved.pop()
            elif p and p != ".":
                resolved.append(p)
        clean_asset = "/".join(resolved)
        return f"{attr}{q}/api/read/{novel_id}/asset/{quote(clean_asset)}{q}{close_paren}"
    return _MEDIA_REF_PATTERN.sub(replace_media, content)

def _find_case_insensitive(base, parts):
    current = base
    for part in parts:
        if not os.path.isdir(current):
            return None
        try:
            entry = next((e for e in os.listdir(current) if e.lower() == part.lower()), None)
        except OSError:
            return None
        if not entry:
            return None
        current = os.path.join(current, entry)
    return current if os.path.isfile(current) else None

def _extract_asset_from_epubs(base_path, rel_path):
    target_filename = os.path.basename(rel_path).lower()
    target_stem = os.path.splitext(target_filename)[0]
    save_full_path = resolve_under(base_path, rel_path)
    if not save_full_path:
        return None
    asset_dir = os.path.dirname(save_full_path)
    try:
        epubs = [f for f in os.listdir(base_path) if f.lower().endswith(".epub")]
    except OSError:
        return None
    for epub_file in epubs:
        try:
            with zipfile.ZipFile(os.path.join(base_path, epub_file), "r") as z:
                infos = z.infolist()
                for zi in infos:
                    if zi.filename.split("/")[-1].lower() == target_filename:
                        copy_zip_entry_atomic(
                            z,
                            zi,
                            save_full_path,
                            MAX_EPUB_ENTRY_BYTES,
                            MAX_EPUB_COMPRESSION_RATIO,
                        )
                        return save_full_path
                for zi in infos:
                    zip_name = zi.filename.split("/")[-1].lower()
                    if zip_name.startswith(target_stem + ".") and zip_name.endswith(ASSET_IMAGE_EXTENSIONS):
                        true_ext = zip_name.rsplit(".", 1)[-1]
                        true_full_path = os.path.join(asset_dir, f"{target_stem}.{true_ext}")
                        copy_zip_entry_atomic(
                            z,
                            zi,
                            true_full_path,
                            MAX_EPUB_ENTRY_BYTES,
                            MAX_EPUB_COMPRESSION_RATIO,
                        )
                        return true_full_path
        except (zipfile.BadZipFile, EpubSafetyError, OSError):
            continue
    return None

# ====================================================================
# 10. TELEGRAM SERVICE LINK PARSING
# ====================================================================
def parse_telegram_link(tg_link):
    try:
        parts = tg_link.rstrip("/").split("/")
        msg_digits = re.sub(r"\D", "", parts[-1])
        chan_digits = re.sub(r"\D", "", parts[-2])
        message_id = int(msg_digits)
        channel = int("-100" + chan_digits) if chan_digits else parts[-2]
        return channel, message_id
    except (ValueError, IndexError):
        return None

# ====================================================================
# 11. API RESPONSE HELPERS
# ====================================================================
def json_error(message, status):
    return jsonify({"status": "error", "error": message}), status

def require_json(*required_keys):
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None, json_error("Request body must be a JSON object.", 400)
    missing = [k for k in required_keys if k not in data]
    if missing:
        return None, json_error(f"Missing required field(s): {', '.join(missing)}.", 400)
    return data, None

# ====================================================================
# 12. ROUTES - PAGES & AUTH
# ====================================================================
@app.route("/healthz", methods=["GET"])
def healthz():
    return jsonify({"status": "ok"})


@app.route("/readyz", methods=["GET"])
def readyz():
    try:
        LIBRARY_INDEX.check_ready()
        check_state_read_backend_ready()
    except (LibraryIndexUnavailable, StateReadError, OSError):
        return jsonify({"status": "not_ready"}), 503
    return jsonify({"status": "ready"})


@app.route("/")
@login_required
def index():
    resp = make_response(render_template("gallery.html", dmca_email=DMCA_EMAIL))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return render_template("register.html", error=None)
    limited = enforce_rate_limit("auth")
    if limited:
        return limited
    email = request.form.get("email", "").strip().lower()
    pw = request.form.get("password", "")
    if email not in ADMIN_EMAILS and email not in get_allowed_emails():
        return render_template(
            "register.html",
            error="This email is not invited. Send your email to @sigma619 to get access."
        )
    if len(pw) < MIN_PASSWORD_LEN:
        return render_template("register.html", error=f"Password must be {MIN_PASSWORD_LEN}+ characters.")
    code = _new_code()

    def m(users):
        u = users.get(email)
        if u and u.get("verified"):
            return "exists"
        users[email] = {
            "pwd_hash": generate_password_hash(pw),
            "verified": False,
            "code_hash": _hash_code(code),
            "code_expires": time.time() + CODE_TTL_SECONDS,
            "code_attempts": 0,
            "created_at": time.time(),
        }
        return "ok"

    if mutate_users(m, shadow_reason="auth_register") == "exists":
        return render_template("register.html", error="Account already exists - please log in.")
    send_email(email, "Your verification code",
               f"Your verification code is {code}\nIt expires in 10 minutes.")
    return redirect(url_for("verify", email=email))

@app.route("/verify", methods=["GET", "POST"])
def verify():
    email = request.values.get("email", "").strip().lower()
    if request.method == "GET":
        return render_template("verify.html", email=email, error=None)
    limited = enforce_rate_limit("auth")
    if limited:
        return limited
    code = request.form.get("code", "").strip()

    def m(users):
        u = users.get(email)
        if not u or u.get("verified"):
            return "bad"
        if time.time() > u.get("code_expires", 0):
            return "expired"
        if u.get("code_attempts", 0) >= MAX_CODE_ATTEMPTS:
            return "locked"
        u["code_attempts"] = u.get("code_attempts", 0) + 1
        if _hash_code(code) != u.get("code_hash"):
            return "wrong"
        u["verified"] = True
        u.pop("code_hash", None)
        u.pop("code_expires", None)
        u.pop("code_attempts", None)
        return "ok"

    status = mutate_users(m, shadow_reason="auth_verify")
    if status == "ok":
        client_ip = get_client_ip()
        if enforce_multi_account(client_ip, email):
            session.pop("user_email", None)
            return render_template("verify.html", email=email,
                error="Access revoked: multiple accounts detected from your network.")
        session.permanent = True
        session["user_email"] = email
        return redirect(url_for("index"))
    msgs = {
        "wrong": "Incorrect code.",
        "expired": "Code expired - register again to get a new one.",
        "locked": "Too many attempts - register again to get a new code.",
        "bad": "Nothing to verify - please register.",
    }
    return render_template("verify.html", email=email, error=msgs.get(status, "Verification error."))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html", error=None)
    limited = enforce_rate_limit("auth")
    if limited:
        return limited
    email = request.form.get("email", "").strip().lower()
    pw = request.form.get("password", "")
    if email not in ADMIN_EMAILS and email not in get_allowed_emails():
        return render_template("login.html", error="Invalid credentials or access revoked.")
    u = load_users().get(email)
    if not u or not u.get("verified") or not check_password_hash(u.get("pwd_hash", ""), pw):
        return render_template("login.html", error="Invalid credentials or unverified email.")
    client_ip = get_client_ip()
    if enforce_multi_account(client_ip, email):
        session.pop("user_email", None)
        return render_template("login.html",
            error="Access revoked: multiple accounts detected from your network.")
    session.permanent = True
    session["user_email"] = email
    return redirect(url_for("index"))

@app.route("/logout", methods=["POST"])
def logout():
    session.pop("user_email", None)
    return redirect(url_for("login"))

@app.route("/read/<novel_id>")
@login_required
def read_novel(novel_id):
    novel = find_novel(novel_id)
    if not novel or not novel.get("has_local_read"):
        return "Novel not available for online reading.", 404
    toc = get_novel_toc(novel)
    toc_titles = get_novel_toc_titles(novel)
    images = get_novel_images(novel)
    user_email = session.get("user_email", "")
    user_record = load_user_data().get(user_email, {}).get(novel_key(novel), {})
    server_progress = to_int(user_record.get("progress"), 0)
    return render_template(
        "reader.html",
        novel=novel,
        toc=toc,
        toc_titles=toc_titles,
        images=images,
        server_progress=server_progress,
    )

# ====================================================================
# 12. ROUTES - GALLERY API
# ====================================================================
@app.route("/api/collections", methods=["GET", "POST"])
@login_required
def api_collections():
    email = session["user_email"]
    counts = collection_counts(email)
    cols = get_user_collections(email)
    return jsonify({"collections": [
        {"id": c["id"], "name": c["name"], "count": counts.get(c["id"], 0)}
        for c in cols
    ]})

@app.route("/api/collection_create", methods=["POST"])
@login_required
def api_collection_create():
    email = session["user_email"]
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()[:60]
    if not name:
        return jsonify({"error": "Collection name is required"}), 400
    allcols = load_collections()
    user_cols = allcols.get(email, [])
    if any(c["name"].lower() == name.lower() for c in user_cols):
        return jsonify({"error": "You already have a collection with that name"}), 400
    new_col = {"id": uuid.uuid4().hex[:12], "name": name}
    user_cols.append(new_col)
    allcols[email] = user_cols
    save_collections(allcols, shadow_email=email, shadow_reason="collection_create")
    return jsonify({"collection": {**new_col, "count": 0}})

@app.route("/api/collection_rename", methods=["POST"])
@login_required
def api_collection_rename():
    email = session["user_email"]
    data = request.get_json(silent=True) or {}
    cid = str(data.get("id", "")).strip()
    name = str(data.get("name", "")).strip()[:60]
    if not cid or not name:
        return jsonify({"error": "Collection id and name are required"}), 400
    allcols = load_collections()
    user_cols = allcols.get(email, [])
    found = False
    for c in user_cols:
        if c["id"] == cid:
            c["name"] = name
            found = True
        elif c["name"].lower() == name.lower():
            return jsonify({"error": "Another collection already uses that name"}), 400
    if not found:
        return jsonify({"error": "Collection not found"}), 404
    allcols[email] = user_cols
    save_collections(allcols, shadow_email=email, shadow_reason="collection_rename")
    return jsonify({"ok": True})

@app.route("/api/collection_delete", methods=["POST"])
@login_required
def api_collection_delete():
    email = session["user_email"]
    data = request.get_json(silent=True) or {}
    cid = str(data.get("id", "")).strip()
    if not cid:
        return jsonify({"error": "Collection id is required"}), 400
    allcols = load_collections()
    allcols[email] = [c for c in allcols.get(email, []) if c["id"] != cid]
    save_collections(allcols, shadow_email=email, shadow_reason="collection_delete")

    def remove_collection_memberships(store):
        udata = store.get(email, {})
        changed = False
        for entry in udata.values():
            if isinstance(entry, dict) and cid in (entry.get("collections") or []):
                entry["collections"] = [x for x in entry["collections"] if x != cid]
                changed = True
        if changed:
            store[email] = udata
        return changed

    mutate_user_data(
        remove_collection_memberships,
        shadow_email=email,
        shadow_reason="collection_delete_memberships",
    )
    return jsonify({"ok": True})

@app.route("/api/collection_assign", methods=["POST"])
@login_required
def api_collection_assign():
    email = session["user_email"]
    data = request.get_json(silent=True) or {}
    novel_id = str(data.get("id", "")).strip()
    cid = str(data.get("collection", "")).strip()
    add = bool(data.get("add", True))
    if not novel_id or not cid:
        return jsonify({"error": "Novel id and collection id are required"}), 400
    if not any(c["id"] == cid for c in get_user_collections(email)):
        return jsonify({"error": "Collection not found"}), 404

    def assign_collection(store):
        udata = store.setdefault(email, {})
        entry = udata.setdefault(novel_id, {})
        cur = [x for x in (entry.get("collections") or []) if x != cid]
        if add:
            cur.append(cid)
        entry["collections"] = cur
        return udata

    udata = mutate_user_data(
        assign_collection,
        shadow_email=email,
        shadow_reason="collection_assign",
    )
    return jsonify({"user_data": udata})

@app.route("/api/tags", methods=["GET"])
@login_required
def api_tags():
    return jsonify(LIBRARY_INDEX.tag_counts())

@app.route("/api/authors", methods=["GET"])
@login_required
def api_authors():
    return jsonify(LIBRARY_INDEX.authors())

@app.route("/api/admin/state-read-probe", methods=["GET"])
@login_required
def api_admin_state_read_probe():
    """Exercise every state read adapter without returning state or identities."""
    if session.get("user_email", "") not in ADMIN_EMAILS:
        return json_error("Forbidden.", 403)
    values = {
        "users": load_users(),
        "user_data": load_user_data(),
        "collections": load_collections(),
        "user_uploads": load_user_uploads(),
        "custom_meta": load_custom_meta(),
        "allowed_emails": get_allowed_emails(),
    }
    if any(not isinstance(value, (dict, set, list)) for value in values.values()):
        return json_error("State read probe failed.", 503)
    return jsonify({
        "status": "ok",
        "domains": sorted(values),
    })

@app.route("/api/user_status", methods=["POST"])
@login_required
def api_user_status():
    data, error = require_json("id")
    if error:
        return error
    user_email = session["user_email"]
    target_id = str(data.get("id"))
    status = data.get("status", "none")
    if status not in VALID_READING_STATUSES:
        return json_error(f"Invalid status {status!r}.", 400)

    def mutator(store):
        user = store.setdefault(user_email, {})
        record = user.setdefault(target_id, {"status": "none", "progress": 0})
        if status == "none":
            if (record.get("progress", 0) == 0
                    and not (record.get("collections") or [])
                    and not to_int(record.get("dl"), 0)
                    and not record.get("hidden")):
                user.pop(target_id, None)
            else:
                record["status"] = "none"
        else:
            record["status"] = status
        if target_id in user:
            user[target_id]["last_read"] = time.time()
        return user

    user_record = mutate_user_data(mutator, shadow_email=user_email, shadow_reason="user_status")
    return jsonify({"status": "success", "user_data": user_record})

@app.route("/api/bulk_remove", methods=["POST"])
@login_required
def api_bulk_remove():
    data, error = require_json("ids")
    if error:
        return error
    user_email = session["user_email"]
    ids = data.get("ids")
    if not isinstance(ids, list) or not ids:
        return json_error("ids must be a non-empty list.", 400)
    target_ids = {str(i) for i in ids[:MAX_BULK_REMOVE_IDS]}
    collection = str(data.get("collection", "")).strip()

    if collection and collection not in ("all", "none"):
        if not any(c["id"] == collection for c in get_user_collections(user_email)):
            return json_error("Collection not found.", 404)

        def mutator(store):
            user = store.setdefault(user_email, {})
            for target_id in target_ids:
                record = user.get(target_id)
                if not record:
                    continue
                cur = [x for x in (record.get("collections") or []) if x != collection]
                record["collections"] = cur
                if (record.get("status") in (None, "none")
                        and to_int(record.get("progress"), 0) == 0
                        and not cur
                        and not to_int(record.get("dl"), 0)
                        and not record.get("hidden")):
                    user.pop(target_id, None)
            return user
    else:
        def mutator(store):
            user = store.setdefault(user_email, {})
            for target_id in target_ids:
                record = user.get(target_id)
                if not record:
                    continue
                if (record.get("progress", 0) == 0
                        and not (record.get("collections") or [])
                        and not to_int(record.get("dl"), 0)
                        and not record.get("hidden")):
                    user.pop(target_id, None)
                else:
                    record["status"] = "none"
            return user

    user_record = mutate_user_data(mutator, shadow_email=user_email, shadow_reason="bulk_remove")
    return jsonify({"status": "success", "user_data": user_record})

@app.route("/api/user_progress", methods=["POST"])
@login_required
def api_user_progress():
    data, error = require_json("id")
    if error:
        return error
    user_email = session["user_email"]
    target_id = str(data.get("id"))
    progress = to_int(data.get("progress"), 0)

    def mutator(store):
        user = store.setdefault(user_email, {})
        record = user.get(target_id)
        if record is None:
            record = {"status": "reading", "progress": progress}
            user[target_id] = record
        else:
            record["progress"] = progress
        if record.get("status") in ("want_to_read", "none"):
            record["status"] = "reading"
        record["last_read"] = time.time()

    mutate_user_data(mutator, shadow_email=user_email, shadow_reason="user_progress")
    return jsonify({"status": "success"})

@app.route("/api/user_hide", methods=["POST"])
@login_required
def api_user_hide():
    data, error = require_json("id")
    if error:
        return error
    user_email = session["user_email"]
    target_id = str(data.get("id"))
    hide = bool(data.get("hide", True))

    def mutator(store):
        user = store.setdefault(user_email, {})
        if hide:
            record = user.setdefault(target_id, {"status": "none", "progress": 0})
            record["hidden"] = True
        else:
            record = user.get(target_id)
            if record:
                record.pop("hidden", None)
                if (record.get("status") in (None, "none")
                        and to_int(record.get("progress"), 0) == 0
                        and not (record.get("collections") or [])
                        and not to_int(record.get("dl"), 0)):
                    user.pop(target_id, None)
        return user

    user_record = mutate_user_data(mutator, shadow_email=user_email, shadow_reason="user_hide")
    return jsonify({"status": "success", "user_data": user_record})

@app.route("/api/library", methods=["POST"])
@login_required
def api_library():
    limited = enforce_rate_limit("library", as_json=True)
    if limited:
        return limited
    data = request.get_json(silent=True) or {}
    page = max(1, to_int(data.get("page"), 1))
    limit = max(1, to_int(data.get("limit"), 30))
    sort_by = data.get("sortBy", "views")
    sort_order = data.get("sortOrder", "desc")

    filters = {
        "upload_source": str(data.get("uploadSource", "all")).strip().lower(),
        "search": str(data.get("search", "")).strip().lower(),
        "includes": set(data.get("includes", []) or []),
        "excludes": set(data.get("excludes", []) or []),
        "reading_status": data.get("readingStatus", "all"),
        "translated_chapter": str(data.get("translatedChapter", "all")).strip().lower(),
        "audience": data.get("audience", "all"),
        "status": data.get("status", "all"),
        "author": str(data.get("author", "")).strip().lower(),
        "language": str(data.get("language", "all")).strip().lower(),
        "min_chapters": to_int(data.get("minChapters"), 0),
        "max_chapters": to_int(data.get("maxChapters"), 999999) or 999999,
        "tag_match": str(data.get("tagMatch", "and")).strip().lower(),
        "collection": str(data.get("collection", "all")).strip(),
        "updated_after": str(data.get("updatedAfter", "")).strip(),
        "updated_before": str(data.get("updatedBefore", "")).strip(),
    }

    user_email = session.get("user_email", "")
    user_data = load_user_data().get(user_email, {})

    target_sort = sort_by
    if filters["reading_status"] != "all" and sort_by == "views":
        target_sort = "last_read"
    result = LIBRARY_INDEX.query(
        filters=filters,
        user_data=user_data,
        sort_by=target_sort,
        sort_order=sort_order,
        page=page,
        limit=limit,
        random_one=bool(data.get("random")),
    )
    if data.get("random"):
        random_novel = result.get("random")
        return jsonify({"random_id": novel_key(random_novel) if random_novel else None})

    return jsonify({
        "novels": [_public_novel(n) for n in result["items"]],
        "total": result["total"],
        "totalPages": result["total_pages"],
        "currentPage": result["page"],
        "userData": user_data,
    })

def _clamped_limit(default=12):
    return min(SIMILAR_MAX, max(1, to_int(request.args.get("limit"), default)))

@app.route("/api/novel/<path:novel_id>", methods=["GET"])
@login_required
def api_novel_detail(novel_id):
    limited = enforce_rate_limit("library", as_json=True)
    if limited:
        return limited
    novel = find_novel(novel_id)
    if not novel:
        return json_error("Novel not found.", 404)
    record = load_user_data().get(session["user_email"], {}).get(novel_key(novel), {})
    return jsonify({"novel": _public_novel(novel), "user_record": record})

@app.route("/api/novel/<path:novel_id>/similar", methods=["GET"])
@login_required
def api_novel_similar(novel_id):
    limited = enforce_rate_limit("library", as_json=True)
    if limited:
        return limited
    novel = find_novel(novel_id)
    if not novel:
        return json_error("Novel not found.", 404)
    basis, results = get_similar_novels(novel, SIMILAR_MAX)
    udata = load_user_data().get(session["user_email"], {})
    hidden = {k for k, r in udata.items() if isinstance(r, dict) and r.get("hidden")}
    limit = _clamped_limit()
    visible = [r for r in results if novel_key(r[1]) not in hidden][:limit]
    return jsonify({
        "basis": basis,
        "novels": [dict(_public_novel(n), score=round(score, 4), **extra)
                   for score, n, extra in visible],
    })

@app.route("/api/recommendations", methods=["GET"])
@login_required
def api_recommendations():
    limited = enforce_rate_limit("library", as_json=True)
    if limited:
        return limited
    basis, results = get_user_recommendations(session["user_email"], _clamped_limit())
    return jsonify({
        "basis": basis,
        "novels": [dict(_public_novel(n), score=round(score, 4), **extra)
                   for score, n, extra in results],
    })

@app.route("/api/because", methods=["GET"])
@login_required
def api_because():
    limited = enforce_rate_limit("library", as_json=True)
    if limited:
        return limited
    email = session["user_email"]
    state = _similarity_state()
    udata = load_user_data().get(email, {})
    candidates = [(r.get("last_read", 0), k) for k, r in udata.items()
                  if isinstance(r, dict) and not r.get("hidden")
                  and r.get("status") in ("reading", "finished")
                  and k in state["by_key"]]
    if not candidates:
        return jsonify({"seed": None, "novels": []})
    candidates.sort(reverse=True)
    seed = state["by_key"][candidates[0][1]]
    basis, results = get_similar_novels(seed, SIMILAR_MAX)
    skip = {k for k, r in udata.items() if isinstance(r, dict)}
    limit = _clamped_limit()
    visible = [r for r in results if novel_key(r[1]) not in skip][:limit]
    return jsonify({
        "seed": _public_novel(seed),
        "basis": basis,
        "novels": [dict(_public_novel(n), score=round(score, 4), **extra)
                   for score, n, extra in visible],
    })

@app.route("/api/trending", methods=["GET"])
@login_required
def api_trending():
    limited = enforce_rate_limit("library", as_json=True)
    if limited:
        return limited
    state = _similarity_state()
    recent = _community_stats()["recent"]
    ranked = sorted(((cnt, k) for k, cnt in recent.items()
                     if cnt >= _TRENDING_MIN_READERS and k in state["by_key"]
                     and _novel_is_recommendable(state["by_key"][k])),
                    reverse=True)
    limit = _clamped_limit()
    return jsonify({
        "window_days": _TRENDING_DAYS,
        "novels": [dict(_public_novel(state["by_key"][k]),
                        reason="trending", readers=cnt)
                   for cnt, k in ranked[:limit]],
    })

# ====================================================================
# 12. ROUTES - USER NOVEL UPLOADS
# ====================================================================

@app.route("/api/upload_novel", methods=["POST"])
@login_required
def api_upload_novel():
    limited = enforce_rate_limit("upload", as_json=True)
    if limited:
        return limited
    user_email = session["user_email"]
    title_en = _clean_upload_text(
        request.form.get("title_en"),
        MAX_UPLOAD_TITLE_LENGTH,
    )
    raw_title = _clean_upload_text(
        request.form.get("raw_title"),
        MAX_UPLOAD_TITLE_LENGTH,
    )
    author = _clean_upload_text(
        request.form.get("author"),
        MAX_UPLOAD_AUTHOR_LENGTH,
    )
    description = _clean_upload_text(
        request.form.get("description"),
        MAX_UPLOAD_DESCRIPTION_LENGTH,
    )

    if not title_en:
        return json_error("English title is required.", 400)
    if not raw_title:
        raw_title = title_en
    if not author:
        author = "Unknown"

    raw_tags = str(request.form.get("tags") or "")
    tags = []
    seen_tags = set()
    for candidate in raw_tags.split(","):
        tag = _clean_upload_text(candidate, MAX_UPLOAD_TAG_LENGTH)
        normalized = tag.casefold()
        if not tag or normalized in seen_tags:
            continue
        seen_tags.add(normalized)
        tags.append(tag)
        if len(tags) >= MAX_UPLOAD_TAGS:
            break

    raw_epub = request.files.get("raw_epub")
    translated_epub = request.files.get("translated_epub")
    cover = request.files.get("cover")

    has_raw = bool(raw_epub and raw_epub.filename)
    has_translated = bool(translated_epub and translated_epub.filename)

    if not has_raw and not has_translated:
        return json_error("Upload a raw EPUB, translated EPUB, or both.", 400)

    for storage, label in ((raw_epub, "Raw EPUB"), (translated_epub, "Translated EPUB")):
        if not storage or not storage.filename:
            continue
        if not storage.filename.lower().endswith(".epub"):
            return json_error(f"{label} must have an .epub filename.", 400)

    upload_id = "upload_" + uuid.uuid4().hex
    originals_dir = os.path.join(USER_UPLOAD_EPUB_DIR, upload_id)
    temporary_extract_dir = os.path.join(LOCAL_OUTPUT_DIR, f".upload_tmp_{upload_id}")
    final_folder_name = f"user_{upload_id}"

    raw_epub_path = ""
    translated_epub_path = ""
    cover_file_path = ""
    raw_original_name = _safe_upload_filename(raw_epub.filename, "raw.epub") if has_raw else ""
    translated_original_name = _safe_upload_filename(translated_epub.filename, "translated.epub") if has_translated else ""

    try:
        os.makedirs(originals_dir, exist_ok=False)
        if has_raw:
            raw_epub_path = os.path.join(originals_dir, "raw.epub")
            _copy_upload_limited(raw_epub, raw_epub_path, MAX_EPUB_UPLOAD_BYTES)
            _validate_epub_archive(raw_epub_path)
        if has_translated:
            translated_epub_path = os.path.join(originals_dir, "translated.epub")
            _copy_upload_limited(translated_epub, translated_epub_path, MAX_EPUB_UPLOAD_BYTES)
            _validate_epub_archive(translated_epub_path)

        # Test extract to verify EPUB contains valid HTML chapters
        reading_epub_path = translated_epub_path or raw_epub_path
        _extract_epub_safely(reading_epub_path, temporary_extract_dir)

        chapter_count = _count_readable_chapters(temporary_extract_dir)
        if chapter_count <= 0:
            raise ValueError("The EPUB contains no readable HTML/XHTML chapters.")

        # Cleanup temporary extraction directory until admin approves
        shutil.rmtree(temporary_extract_dir, ignore_errors=True)

        cover_file_path = _save_uploaded_cover(cover, upload_id)
        now = datetime.now(timezone.utc)
        record = {
            "id": upload_id,
            "title_en": title_en,
            "raw_title": raw_title,
            "author": author,
            "description": description,
            "tags": tags,
            "uploader_email": user_email,
            "uploader_name": _format_uploader_name(user_email),
            "created_at": now.isoformat(),
            "upload_date": now.date().isoformat(),
            "raw_epub_path": raw_epub_path,
            "translated_epub_path": translated_epub_path,
            "raw_original_name": raw_original_name,
            "translated_original_name": translated_original_name,
            "cover_file_path": cover_file_path,
            "local_folder": final_folder_name,
            "chapters": chapter_count,
            "approved": False,
            "status": "pending",
        }

        def save_record(uploads):
            uploads[upload_id] = record

        mutate_user_uploads(save_record, shadow_reason="upload_create")
        return jsonify({
            "status": "success",
            "id": upload_id,
            "title": title_en,
            "message": "Novel submitted! It will appear in the library once reviewed by an admin.",
        })
    except ValueError as exc:
        shutil.rmtree(temporary_extract_dir, ignore_errors=True)
        shutil.rmtree(originals_dir, ignore_errors=True)
        if cover_file_path:
            try:
                os.remove(cover_file_path)
            except OSError:
                pass
        return json_error(str(exc), 400)
    except FileExistsError:
        return json_error("An upload storage collision occurred. Please retry.", 409)
    except Exception as exc:
        print(f"[WARN] Novel upload failed for {_log_safe(user_email)}: {exc}")
        if getattr(exc, "arcdb_legacy_write_succeeded", False):
            return json_error(
                "The upload was saved, but local shadow verification failed. "
                "Restart local ArchiveDB to rebuild the shadow before retrying.",
                500,
            )
        shutil.rmtree(temporary_extract_dir, ignore_errors=True)
        shutil.rmtree(originals_dir, ignore_errors=True)
        if cover_file_path:
            try:
                os.remove(cover_file_path)
            except OSError:
                pass
        return json_error("The upload could not be processed.", 500)

@app.route("/api/upload/<upload_id>/asset/cover")
@login_required
def api_uploaded_cover(upload_id):
    limited = enforce_rate_limit("asset")
    if limited:
        return limited
    record = load_user_uploads().get(str(upload_id))
    if not isinstance(record, dict):
        return "Not found", 404
    cover_path = str(record.get("cover_file_path") or "")
    if not cover_path or not os.path.isfile(cover_path):
        return "Not found", 404
    return send_file(
        cover_path,
        conditional=True,
        max_age=604800,
    )

# ====================================================================
# 12. ROUTES - COMMUNITY
# ====================================================================
@app.route("/community")
@login_required
def community_page():
    email = session["user_email"]
    return render_template(
        "community.html",
        username=community_username(email),
        is_admin=email in ADMIN_EMAILS,
    )

@app.route("/api/community/overview", methods=["GET"])
@login_required
def api_community_overview():
    limited = enforce_rate_limit("community", as_json=True)
    if limited:
        return limited
    email = session["user_email"]
    is_admin = email in ADMIN_EMAILS
    data = load_community()
    chat_tail = data["chat"][-100:]
    return jsonify({
        "username": data["usernames"].get(email),
        "shares": [_public_share(s, email, is_admin) for s in reversed(data["shares"][-100:])],
        "chat": [_public_chat_msg(m, email, is_admin) for m in chat_tail],
        "last_chat_id": chat_tail[-1]["id"] if chat_tail else 0,
    })

@app.route("/api/community/username", methods=["POST"])
@login_required
def api_community_username():
    limited = enforce_rate_limit("community_post", as_json=True)
    if limited:
        return limited
    data, error = require_json("name")
    if error:
        return error
    name = str(data.get("name", "")).strip()
    if not _USERNAME_RE.match(name):
        return json_error("Username must be 3-20 characters: letters, numbers and underscores only.", 400)
    email = session["user_email"]

    def m(doc):
        current = doc["usernames"].get(email)
        if current:
            return "exists", current
        if name.lower() in {n.lower() for n in doc["usernames"].values()}:
            return "taken", None
        doc["usernames"][email] = name
        return "ok", name

    status, value = mutate_community(m)
    if status == "taken":
        return json_error("That username is already taken - pick another.", 409)
    return jsonify({"status": "success", "username": value})

def _require_community_username(email):
    uname = community_username(email)
    if not uname:
        return None, json_error("Set a community username first.", 403)
    return uname, None

@app.route("/api/community/share_novel", methods=["POST"])
@login_required
def api_community_share_novel():
    limited = enforce_rate_limit("community_post", as_json=True)
    if limited:
        return limited
    data, error = require_json("id")
    if error:
        return error
    email = session["user_email"]
    uname, error = _require_community_username(email)
    if error:
        return error
    novel = find_novel(str(data.get("id")))
    if not novel:
        return json_error("Novel not found.", 404)
    message = str(data.get("message", "")).strip()[:COMMUNITY_MSG_MAX_LEN]

    def m(doc):
        sid = doc["next_id"]
        doc["next_id"] += 1
        doc["shares"].append({
            "id": sid, "type": "novel", "email": email, "user": uname,
            "ts": time.time(), "key": novel_key(novel),
            "title": novel.get("title_en") or "",
            "author": novel.get("author") or "",
            "cover": novel.get("cover") or "",
            "message": message,
        })
        del doc["shares"][:-COMMUNITY_MAX_SHARES]
        return sid

    return jsonify({"status": "success", "id": mutate_community(m)})

@app.route("/api/community/share_collection", methods=["POST"])
@login_required
def api_community_share_collection():
    limited = enforce_rate_limit("community_post", as_json=True)
    if limited:
        return limited
    data, error = require_json("collection")
    if error:
        return error
    email = session["user_email"]
    uname, error = _require_community_username(email)
    if error:
        return error
    cid = str(data.get("collection", "")).strip()
    col = next((c for c in get_user_collections(email) if c["id"] == cid), None)
    if not col:
        return json_error("Collection not found.", 404)
    udata = load_user_data().get(email, {})
    keys = [k for k, r in udata.items()
            if isinstance(r, dict) and cid in (r.get("collections") or [])]
    keys = keys[:COMMUNITY_COLLECTION_MAX_KEYS]
    if not keys:
        return json_error("That collection is empty - add some novels first.", 400)
    state = _similarity_state()
    previews, cover = [], ""
    for k in keys:
        n = state["by_key"].get(k)
        if n is None:
            continue
        if len(previews) < 5:
            previews.append(n.get("title_en") or "")
        if not cover and n.get("cover"):
            cover = n["cover"]
    message = str(data.get("message", "")).strip()[:COMMUNITY_MSG_MAX_LEN]

    def m(doc):
        sid = doc["next_id"]
        doc["next_id"] += 1
        doc["shares"].append({
            "id": sid, "type": "collection", "email": email, "user": uname,
            "ts": time.time(), "name": col["name"], "count": len(keys),
            "keys": keys, "previews": previews, "cover": cover,
            "message": message,
        })
        del doc["shares"][:-COMMUNITY_MAX_SHARES]
        return sid

    return jsonify({"status": "success", "id": mutate_community(m)})

@app.route("/api/community/chat", methods=["GET", "POST"])
@login_required
def api_community_chat():
    email = session["user_email"]
    is_admin = email in ADMIN_EMAILS
    if request.method == "GET":
        limited = enforce_rate_limit("community", as_json=True)
        if limited:
            return limited
        after = to_int(request.args.get("after"), 0)
        data = load_community()
        fresh = [m for m in data["chat"] if m["id"] > after][-100:]
        return jsonify({
            "messages": [_public_chat_msg(m, email, is_admin) for m in fresh],
            "last_id": data["chat"][-1]["id"] if data["chat"] else after,
        })
    limited = enforce_rate_limit("community_post", as_json=True)
    if limited:
        return limited
    uname, error = _require_community_username(email)
    if error:
        return error
    body = request.get_json(silent=True) or {}
    text = str(body.get("text", "")).strip()[:COMMUNITY_CHAT_MAX_LEN]
    if not text:
        return json_error("Message cannot be empty.", 400)

    def m(doc):
        mid = doc["next_id"]
        doc["next_id"] += 1
        doc["chat"].append({"id": mid, "email": email, "user": uname,
                            "ts": time.time(), "text": text})
        del doc["chat"][:-COMMUNITY_MAX_CHAT]
        return mid

    return jsonify({"status": "success", "id": mutate_community(m)})

@app.route("/api/community/delete", methods=["POST"])
@login_required
def api_community_delete():
    limited = enforce_rate_limit("community_post", as_json=True)
    if limited:
        return limited
    data, error = require_json("kind", "id")
    if error:
        return error
    kind = str(data.get("kind"))
    if kind not in ("share", "chat"):
        return json_error("kind must be 'share' or 'chat'.", 400)
    target_id = to_int(data.get("id"), 0)
    email = session["user_email"]
    is_admin = email in ADMIN_EMAILS

    def m(doc):
        items = doc["shares"] if kind == "share" else doc["chat"]
        for i, item in enumerate(items):
            if item.get("id") == target_id:
                if is_admin or item.get("email") == email:
                    items.pop(i)
                    return "ok"
                return "forbidden"
        return "missing"

    status = mutate_community(m)
    if status == "forbidden":
        return json_error("You can only delete your own posts.", 403)
    if status == "missing":
        return json_error("Not found.", 404)
    return jsonify({"status": "success"})

@app.route("/api/community/import_collection", methods=["POST"])
@login_required
def api_community_import_collection():
    limited = enforce_rate_limit("community_post", as_json=True)
    if limited:
        return limited
    data, error = require_json("share_id")
    if error:
        return error
    email = session["user_email"]
    share_id = to_int(data.get("share_id"), 0)
    share = next((s for s in load_community()["shares"]
                  if s.get("id") == share_id and s.get("type") == "collection"), None)
    if not share:
        return json_error("Shared collection not found.", 404)
    keys = [str(k) for k in (share.get("keys") or [])][:COMMUNITY_COLLECTION_MAX_KEYS]
    if not keys:
        return json_error("This share has no novels to import.", 400)

    with _COLLECTIONS_LOCK:
        allcols = read_json_file(COLLECTIONS_PATH, {})
        user_cols = allcols.get(email, [])
        base = share.get("name") or "Imported collection"
        name, suffix = base, 2
        while any(c["name"].lower() == name.lower() for c in user_cols):
            name = f"{base} ({suffix})"
            suffix += 1
        new_col = {"id": uuid.uuid4().hex[:12], "name": name}
        user_cols.append(new_col)
        allcols[email] = user_cols
        write_json_atomic(COLLECTIONS_PATH, allcols, ensure_ascii=False, indent=2)
        _mirror_collections_shadow(email, user_cols, "community_import_collection")

    def m(store):
        user = store.setdefault(email, {})
        for k in keys:
            entry = user.setdefault(k, {"status": "none", "progress": 0})
            memberships = [x for x in (entry.get("collections") or []) if x != new_col["id"]]
            memberships.append(new_col["id"])
            entry["collections"] = memberships

    mutate_user_data(m, shadow_email=email, shadow_reason="community_import_memberships")
    return jsonify({"status": "success",
                    "collection": {"id": new_col["id"], "name": name, "count": len(keys)}})

@app.route("/api/edit", methods=["POST"])
@login_required
def edit_metadata():
    data, error = require_json("filename")
    if error:
        return error
    filename = str(data.get("filename", "")).strip()
    if not filename:
        return json_error("'filename' must be a non-empty string.", 400)
    novel = find_novel(filename)
    if novel is None:
        return json_error("Unknown novel filename.", 404)
    if (session.get("user_email", "") not in ADMIN_EMAILS
            and novel.get("has_meta") and not novel.get("is_custom")):
        return json_error("Only admins can edit automatically matched metadata.", 403)
    cover_url = str(data.get("cover", "")).strip()
    if cover_url and not cover_url.startswith(("http://", "https://")):
        cover_url = ""

    custom_entry = {
        "title_en": str(data.get("title_en", "")),
        "title_kr": str(data.get("title_kr", "")),
        "author": str(data.get("author", "")),
        "cover": cover_url,
        "tags": dedupe_tags([t.strip() for t in str(data.get("tags", "")).split(",") if t.strip()]),
        "synopsis": str(data.get("synopsis", "")),
    }
    save_custom_meta_entry(filename, custom_entry)
    updated_novel = dict(novel)
    updated_novel["is_custom"] = True
    _apply_custom_overrides(updated_novel, custom_entry)
    LIBRARY_INDEX.upsert(updated_novel)
    return jsonify({"status": "success"})

# ====================================================================
# 12. ROUTES - ADMIN
# ====================================================================
@app.route("/admin/access", methods=["GET", "POST"])
@login_required
def admin_access():
    if session.get("user_email", "") not in ADMIN_EMAILS:
        return "Forbidden", 403

    message = ""
    added = []

    if request.method == "POST":
        form_action = request.form.get("action", "add")
        if form_action == "approve_upload":
            upload_id = request.form.get("upload_id", "").strip()
            uploads = load_user_uploads()
            record = uploads.get(upload_id)
            if not record or record.get("approved"):
                message = "Upload not found or already approved."
            else:
                reading_epub = record.get("translated_epub_path") or record.get("raw_epub_path")
                folder_name = record.get("local_folder")
                final_dir = os.path.join(LOCAL_OUTPUT_DIR, folder_name) if folder_name else ""

                if not reading_epub or not os.path.isfile(reading_epub):
                    message = "Error: EPUB file is missing from server storage."
                else:
                    try:
                        _extract_epub_safely(reading_epub, final_dir)
                        ch_count = _count_readable_chapters(final_dir)

                        approved_at = datetime.now(timezone.utc).isoformat()

                        def approve_record(store):
                            if upload_id in store:
                                store[upload_id]["approved"] = True
                                store[upload_id]["status"] = "approved"
                                store[upload_id]["chapters"] = ch_count
                                store[upload_id]["approved_at"] = approved_at

                        mutate_user_uploads(approve_record, shadow_reason="upload_approve")
                        indexed_record = {
                            **record,
                            "approved": True,
                            "status": "approved",
                            "chapters": ch_count,
                            "approved_at": approved_at,
                        }
                        indexed_item = _uploaded_gallery_item(upload_id, indexed_record)
                        try:
                            LIBRARY_INDEX.upsert(
                                indexed_item,
                                content=_indexed_novel_content(indexed_item),
                            )
                            message = f"Approved and published novel '{record.get('title_en')}'!"
                        except LibraryIndexUnavailable:
                            message = (
                                "Upload was approved, but the library index update failed. "
                                "Run the controlled reindex before serving the title."
                            )
                    except Exception as exc:
                        message = f"Failed to extract EPUB: {exc}"

        elif form_action == "reject_upload":
            upload_id = request.form.get("upload_id", "").strip()
            uploads = load_user_uploads()
            record = uploads.get(upload_id)
            if not record:
                message = "Upload not found."
            else:
                # Delete stored files
                originals_dir = os.path.join(USER_UPLOAD_EPUB_DIR, upload_id)
                shutil.rmtree(originals_dir, ignore_errors=True)

                folder_name = record.get("local_folder")
                if folder_name:
                    shutil.rmtree(os.path.join(LOCAL_OUTPUT_DIR, folder_name), ignore_errors=True)

                cover_path = record.get("cover_file_path")
                if cover_path and os.path.isfile(cover_path):
                    try:
                        os.remove(cover_path)
                    except OSError:
                        pass

                def remove_record(store):
                    store.pop(upload_id, None)

                mutate_user_uploads(remove_record, shadow_reason="upload_reject")
                try:
                    LIBRARY_INDEX.delete_alias(upload_id)
                    message = f"Rejected and deleted upload '{record.get('title_en')}'."
                except LibraryIndexUnavailable:
                    message = (
                        "Upload was rejected, but the library index update failed. "
                        "Run the controlled reindex."
                    )

        elif form_action == "add_ip_exemption":
            raw_ip = request.form.get("ip", "").strip()
            note = request.form.get("note", "").strip()
            rule, created = add_ip_exemption(
                raw_ip, actor=session.get("user_email", ""), note=note
            )
            if not rule:
                message = "Invalid IP address or CIDR network."
            elif created:
                message = f"Added IP exemption {rule}. Accounts from it will not be auto-revoked."
            else:
                message = f"IP exemption {rule} already exists."

        elif form_action == "remove_ip_exemption":
            raw_rule = request.form.get("ip", "").strip()
            if remove_ip_exemption(raw_rule):
                message = f"Removed IP exemption {_normalize_ip_exemption(raw_rule)}."
            else:
                message = "That IP exemption was not found."

        elif form_action == "revoke":
            email = request.form.get("email", "").strip().lower()
            reason = request.form.get("reason", "").strip()[:1000]
            if email in ADMIN_EMAILS:
                message = "Admin emails cannot be revoked from this page."
            elif remove_email_from_allowlist(email):
                log_access_revocation(
                    email, "manual_admin_revocation", action="removed_from_allowlist",
                    source="admin_access_page", actor=session.get("user_email", ""),
                    ip_group=_ip_group_key(get_client_ip()),
                    details={"explanation": reason or "No additional reason supplied by the admin."},
                )
                message = f"Revoked access for {email}."
            else:
                message = f"{email or 'That email'} was not on the allowlist."

        else:
            raw = request.form.get("emails", "")
            emails = extract_emails_from_text(raw)
            added = add_emails_to_allowlist(emails)
            message = (
                f"Found {len(emails)} email(s). "
                f"Added {len(added)} new email(s). "
                f"Skipped {len(emails) - len(added)} duplicate/already-approved email(s)."
            )

    current_emails = sorted(get_allowed_emails())

    current_html = "\n".join(
        f"<li>{escape(email)}</li>"
        for email in current_emails
    )

    added_html = ""
    if added:
        added_items = "\n".join(
            f"<li>{escape(email)}</li>"
            for email in added
        )
        added_html = f"""
        <div class="added">
          <strong>Newly added:</strong>
          <ul>{added_items}</ul>
        </div>
        """

    message_html = ""
    if message:
        message_html = f'<div class="message">{escape(message)}</div>'

    if not current_html:
        current_html = "<li>No emails approved yet.</li>"

    # Pending Uploads Section
    pending_uploads = [r for r in load_user_uploads().values() if isinstance(r, dict) and not r.get("approved")]
    pending_cards = []
    for item in pending_uploads:
        uid = item.get("id")
        title_en = item.get("title_en", "Untitled")
        raw_title = item.get("raw_title", "")
        author = item.get("author", "Unknown")
        uploader_email = item.get("uploader_email", "Unknown")
        uploader_name = _format_uploader_name(item.get("uploader_name") or uploader_email)
        uploader_disp = f"{uploader_name} ({uploader_email})" if uploader_email != "Unknown" and uploader_name != uploader_email else uploader_name
        desc = item.get("description", "No description provided.")
        tags_str = ", ".join(item.get("tags") or [])
        cover_path = item.get("cover_file_path")
        cover_url = f"/api/upload/{quote(str(uid), safe='')}/asset/cover" if cover_path and os.path.isfile(cover_path) else ""
        cover_html = f'<img src="{cover_url}" style="width:70px; height:105px; object-fit:cover; border-radius:6px; margin-right:14px; flex-shrink:0;">' if cover_url else ''

        pending_cards.append(f"""
        <div style="background: rgba(2, 6, 23, 0.4); border: 1px solid rgba(148, 163, 184, 0.2); border-radius: 12px; padding: 14px; margin-bottom: 12px; display: flex;">
          {cover_html}
          <div style="flex:1; min-width:0;">
            <h3 style="margin: 0 0 4px; font-size: 16px;">{escape(title_en)} <span style="font-size:12px; color:#94a3b8; font-weight:normal;">({escape(raw_title)})</span></h3>
            <p style="margin:0 0 6px; font-size:12px; color:#cbd5e1;">By <strong>{escape(author)}</strong> &middot; Uploaded by <strong>{escape(uploader_disp)}</strong> on {escape(item.get('upload_date', ''))}</p>
            {f'<p style="margin:0 0 6px; font-size:11px; color:#a78bff;">Tags: {escape(tags_str)}</p>' if tags_str else ''}
            <p style="margin:0 0 10px; font-size:12px; color:#94a3b8; max-height:80px; overflow-y:auto;">{escape(desc)}</p>
            <div style="display:flex; gap:10px;">
              <form method="post" action="/admin/access">
                <input type="hidden" name="action" value="approve_upload">
                <input type="hidden" name="upload_id" value="{escape(uid)}">
                <button type="submit" style="background:#22c55e; width:auto; padding:6px 14px; margin:0; font-size:12px;">Approve &amp; Publish</button>
              </form>
              <form method="post" action="/admin/access">
                <input type="hidden" name="action" value="reject_upload">
                <input type="hidden" name="upload_id" value="{escape(uid)}">
                <button type="submit" class="small-danger-button" style="margin:0;">Reject &amp; Delete</button>
              </form>
            </div>
          </div>
        </div>
        """)

    pending_html = "".join(pending_cards) or '<p class="muted">No pending novel uploads to review.</p>'

    ip_exemptions = load_ip_exemptions()
    exemption_rows = []
    for rule, record in sorted(ip_exemptions.items()):
        record = record if isinstance(record, dict) else {}
        exemption_rows.append(f"""
          <tr>
            <td><code>{escape(rule)}</code></td>
            <td>{escape(str(record.get('note') or '-'))}</td>
            <td>{escape(str(record.get('created_by') or '-'))}</td>
            <td>{escape(str(record.get('created_at') or '-'))}</td>
            <td>
              <form method="post" action="/admin/access">
                <input type="hidden" name="action" value="remove_ip_exemption">
                <input type="hidden" name="ip" value="{escape(rule)}">
                <button type="submit" class="small-danger-button">Remove</button>
              </form>
            </td>
          </tr>
        """)
    exemption_rows_html = "\n".join(exemption_rows) or '<tr><td colspan="5">No exempt IPs yet.</td></tr>'

    revocation_events = load_jsonl_events(ACCESS_REVOCATION_LOG_PATH, limit=100)
    revocation_rows = []
    for event in revocation_events:
        related = event.get("related_accounts") or []
        details = event.get("details") or {}
        when = event.get("timestamp") or event.get("date") or "Unknown"
        revocation_rows.append(f"""
          <tr>
            <td>{escape(str(when))}</td>
            <td><strong>{escape(str(event.get('email') or '-'))}</strong></td>
            <td>{escape(str(event.get('reason') or '-'))}<br><span class="muted">{escape(str(details.get('explanation') or ''))}</span></td>
            <td>{escape(str(event.get('action') or '-'))}</td>
            <td><code>{escape(str(event.get('ip_group') or event.get('client_ip') or '-'))}</code></td>
            <td>{'<br>'.join(escape(str(email)) for email in related) or '-'}</td>
            <td>{escape(str(event.get('actor') or event.get('source') or '-'))}</td>
          </tr>
        """)
    revocation_rows_html = "\n".join(revocation_rows) or '<tr><td colspan="7">No revocations recorded yet.</td></tr>'

    multi_account_events = load_jsonl_events(
        DOWNLOAD_ABUSE_LOG_PATH, limit=100, reason="multi_account_ip"
    )
    incident_cards = []
    for event in multi_account_events:
        accounts = event.get("emails") or []
        removed_accounts = set(event.get("removed") or [])
        account_item_parts = []
        for email in accounts:
            revoked_badge = (
                ' <span class="badge danger">revoked</span>'
                if email in removed_accounts else ""
            )
            account_item_parts.append(
                f'<li><strong>{escape(str(email))}</strong>{revoked_badge}</li>'
            )
        account_items = "".join(account_item_parts) or "<li>No accounts recorded.</li>"
        seen = event.get("account_last_seen") or {}
        seen_items = "".join(
            f'<li>{escape(str(email))}: {escape(datetime.fromtimestamp(float(ts), timezone.utc).isoformat())}</li>'
            for email, ts in seen.items()
            if isinstance(ts, (int, float))
        )
        seen_details_html = ""
        if seen_items:
            seen_details_html = (
                '<details><summary>Last seen (UTC)</summary><ul>'
                + seen_items
                + '</ul></details>'
            )
        incident_cards.append(f"""
          <article class="incident">
            <div><span class="badge">{escape(str(event.get('action') or 'detected'))}</span>
            <span class="muted">{escape(str(event.get('date') or ''))}</span></div>
            <h3>IP group <code>{escape(str(event.get('group') or '-'))}</code></h3>
            <ul>{account_items}</ul>
            {seen_details_html}
          </article>
        """)
    incidents_html = "\n".join(incident_cards) or '<p>No multi-account incidents recorded yet.</p>'

    html = f"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Access &amp; Moderation - ArchiveDB</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; min-height: 100vh;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background:
        radial-gradient(circle at top left, rgba(34, 197, 94, 0.20), transparent 32%),
        radial-gradient(circle at bottom right, rgba(59, 130, 246, 0.22), transparent 30%),
        #0f172a;
      color: #e5e7eb; padding: 24px;
    }}
    .wrap {{ width: 100%; max-width: 900px; margin: 0 auto; }}
    .top-links {{ display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 18px; }}
    .card {{
      background: rgba(15, 23, 42, 0.92); border: 1px solid rgba(148, 163, 184, 0.25);
      border-radius: 22px; padding: 24px; margin-bottom: 18px;
      box-shadow: 0 24px 80px rgba(0, 0, 0, 0.35);
    }}
    h1 {{ margin: 0 0 8px; font-size: 30px; letter-spacing: -0.04em; }}
    h2 {{ margin: 0 0 14px; font-size: 22px; }}
    p {{ color: #94a3b8; line-height: 1.55; margin: 0 0 16px; }}
    code {{ color: #bfdbfe; background: rgba(59, 130, 246, 0.12); padding: 2px 6px; border-radius: 6px; }}
    textarea {{
      width: 100%; min-height: 180px; resize: vertical; padding: 14px;
      border-radius: 14px; border: 1px solid rgba(148, 163, 184, 0.32);
      background: rgba(2, 6, 23, 0.72); color: #f8fafc; font-size: 15px;
      line-height: 1.45; outline: none;
    }}
    textarea:focus {{ border-color: #22c55e; box-shadow: 0 0 0 4px rgba(34, 197, 94, 0.13); }}
    button {{
      width: 100%; margin-top: 14px; padding: 13px 16px; border: 0;
      border-radius: 14px; background: linear-gradient(135deg, #22c55e, #3b82f6);
      color: white; font-size: 15px; font-weight: 800; cursor: pointer;
    }}
    button:hover {{ filter: brightness(1.08); }}
    .message {{
      margin-top: 16px; padding: 13px 14px; border-radius: 14px;
      background: rgba(59, 130, 246, 0.12); border: 1px solid rgba(59, 130, 246, 0.30);
      color: #bfdbfe; font-size: 14px;
    }}
    .added {{
      margin-top: 16px; padding: 13px 14px; border-radius: 14px;
      background: rgba(34, 197, 94, 0.12); border: 1px solid rgba(34, 197, 94, 0.30);
      color: #bbf7d0; font-size: 14px;
    }}
    .current {{
      max-height: 360px; overflow: auto; border-radius: 14px;
      border: 1px solid rgba(148, 163, 184, 0.18); background: rgba(2, 6, 23, 0.38);
      padding: 12px;
    }}
    ul {{ margin: 0; padding-left: 20px; }}
    li {{ padding: 4px 0; word-break: break-word; }}
    a {{ color: #93c5fd; text-decoration: none; font-weight: 800; }}
    a:hover {{ text-decoration: underline; }}
    .count {{ color: #94a3b8; font-weight: 500; }}
    .grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }}
    .danger-form {{ display: grid; grid-template-columns: 1fr 1fr auto; gap: 10px; align-items: end; }}
    label {{ color: #cbd5e1; font-weight: 700; font-size: 14px; }}
    input {{
      width: 100%; margin-top: 7px; padding: 12px; border-radius: 12px;
      border: 1px solid rgba(148, 163, 184, 0.32); background: rgba(2, 6, 23, 0.72);
      color: #f8fafc; font-size: 15px;
    }}
    .danger-button {{ background: #dc2626; width: auto; min-height: 44px; margin: 0; }}
    .small-danger-button {{ background: #dc2626; width: auto; min-height: 36px; margin: 0; padding: 8px 12px; border-radius: 10px; font-size: 12px; }}
    .table-wrap {{ overflow-x: auto; border: 1px solid rgba(148, 163, 184, .18); border-radius: 14px; }}
    table {{ width: 100%; border-collapse: collapse; min-width: 980px; font-size: 13px; }}
    .compact-table table {{ min-width: 720px; }}
    th, td {{ padding: 12px; text-align: left; vertical-align: top; border-bottom: 1px solid rgba(148, 163, 184, .14); }}
    th {{ color: #cbd5e1; background: rgba(30, 41, 59, .75); position: sticky; top: 0; }}
    .muted {{ color: #94a3b8; font-size: 12px; margin:0; }}
    .badge {{ display: inline-block; padding: 3px 8px; border-radius: 999px; background: rgba(59, 130, 246, .16); color: #bfdbfe; font-size: 12px; font-weight: 800; }}
    .badge.danger {{ background: rgba(239, 68, 68, .16); color: #fecaca; }}
    .incident {{ border: 1px solid rgba(148, 163, 184, .18); border-radius: 14px; padding: 16px; background: rgba(2, 6, 23, .38); }}
    .incident h3 {{ margin: 10px 0; font-size: 16px; }}
    .policy {{ border-left: 3px solid #3b82f6; padding: 10px 14px; background: rgba(59, 130, 246, .08); border-radius: 0 10px 10px 0; }}
    .policy strong {{ color: #f8fafc; }}
    @media (max-width: 720px) {{
      body {{ padding: 16px; }}
      .card {{ padding: 18px; border-radius: 16px; }}
      .grid, .danger-form {{ grid-template-columns: 1fr; }}
      .danger-button {{ width: 100%; }}
    }}
  </style>
</head>

<body>
  <div class="wrap">
    <div class="top-links">
      <a href="/">Back to gallery</a>
      <a href="/admin/downloads">Download report</a>
      <form action="/logout" method="post" style="display:inline"><button type="submit">Logout</button></form>
    </div>

    {message_html}

    <section class="card">
      <h2>Pending Novel Uploads <span class="count">({len(pending_uploads)})</span></h2>
      <p>Review user-uploaded novels. Approving extracts the EPUB for online reading and publishes it to the library gallery.</p>
      {pending_html}
    </section>

    <section class="card">
      <h1>Access list</h1>
      <p>Paste emails here to approve them for the gallery access list.</p>
      <form method="post" action="/admin/access">
        <textarea name="emails" placeholder="user1@gmail.com&#10;user2@gmail.com"></textarea>
        <button type="submit">Add emails</button>
      </form>
      {added_html}
    </section>

    <section class="card">
      <h2>Currently approved emails <span class="count">({len(current_emails)})</span></h2>
      <div class="current">
        <ul>{current_html}</ul>
      </div>
    </section>

    <section class="card">
      <h2>Revoke an email</h2>
      <p>This removes the email from the allowlist.</p>
      <form method="post" action="/admin/access" class="danger-form">
        <input type="hidden" name="action" value="revoke">
        <label>Email<input type="email" name="email" required placeholder="account@example.com"></label>
        <label>Reason<input type="text" name="reason" maxlength="1000" placeholder="Policy violation, request, etc."></label>
        <button type="submit" class="danger-button">Revoke access</button>
      </form>
    </section>

    <section class="card">
      <h2>IP exemptions <span class="count">({len(ip_exemptions)})</span></h2>
      <form method="post" action="/admin/access" class="danger-form">
        <input type="hidden" name="action" value="add_ip_exemption">
        <label>IP or network<input type="text" name="ip" required placeholder="203.0.113.8 or 2001:db8::/64"></label>
        <label>Note<input type="text" name="note" maxlength="1000" placeholder="Home, office, etc."></label>
        <button type="submit">Add exemption</button>
      </form>
      <div class="table-wrap compact-table" style="margin-top:16px">
        <table>
          <thead><tr><th>IP/network</th><th>Note</th><th>Added by</th><th>Added at (UTC)</th><th></th></tr></thead>
          <tbody>{exemption_rows_html}</tbody>
        </table>
      </div>
    </section>

    <section class="card">
      <h2>Revocation audit log <span class="count">({len(revocation_events)})</span></h2>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Time (UTC)</th><th>Email</th><th>Why</th><th>Action</th><th>IP group</th><th>Related accounts</th><th>Actor/source</th></tr></thead>
          <tbody>{revocation_rows_html}</tbody>
        </table>
      </div>
    </section>
  </div>
</body>
</html>
"""

    return Response(html, mimetype="text/html")


# ====================================================================
# 12. ROUTES - READER API
# ====================================================================
@app.route("/api/read/<novel_id>/chapter/<path:chap_path>")
@login_required
def api_read_chapter(novel_id, chap_path):
    limited = enforce_rate_limit("read")
    if limited:
        return limited
    novel = find_novel(novel_id)
    if not novel or not novel.get("has_local_read"):
        return "Not found", 404
    base_path = novel_base_path(novel)
    clean_chap_path = chap_path.replace(TOC_MISSING_PREFIX, "")
    full_path = resolve_under(base_path, clean_chap_path)
    if not full_path or not os.path.isfile(full_path):
        return "Chapter file missing.", 404
    try:
        with open(full_path, "r", encoding="utf-8", errors="ignore") as fh:
            content = fh.read()
    except OSError:
        return "Chapter file unreadable.", 500

    content = rewrite_chapter_assets(content, novel_id, clean_chap_path)
    body_match = re.search(r"<body[^>]*>([\s\S]*?)</body>", content, re.IGNORECASE)
    if body_match:
        content = body_match.group(1)
    return sanitize_chapter_html(content)

@app.route("/api/read/<novel_id>/asset/<path:asset_path>")
@login_required
def api_read_asset(novel_id, asset_path):
    limited = enforce_rate_limit("asset")
    if limited:
        return limited
    novel = find_novel(novel_id)
    if not novel or not novel.get("has_local_read"):
        return "Not found", 404
    base_path = novel_base_path(novel)
    rel_path = unquote(asset_path).split("?")[0].replace("\\", "/")
    parts = [p for p in rel_path.split("/") if p and p not in (".", "..")]
    if not parts:
        return "Asset missing", 404

    local_match = _find_case_insensitive(base_path, parts)
    if local_match:
        return send_file(local_match)

    extracted = _extract_asset_from_epubs(base_path, "/".join(parts))
    if extracted:
        return send_file(extracted)
    return "Asset missing", 404

# ====================================================================
# NOTICE GALLERY ROUTES
# ====================================================================
@app.route("/api/read/<novel_id>/notice_gallery")
@login_required
def api_notice_gallery(novel_id):
    limited = enforce_rate_limit("library", as_json=True)
    if limited:
        return limited

    novel = find_novel(novel_id)
    if not novel:
        return json_error("Novel not found.", 404)

    np_id = _resolve_novelpia_key(novel)
    if not np_id:
        return jsonify({
            "available": False,
            "notices": [],
        })

    manifest = load_notice_manifest(np_id)
    if not manifest:
        return jsonify({
            "available": False,
            "notices": [],
        })

    notices = []
    for ntc in manifest.get("notices", []):
        raw_imgs = ntc.get("images", []) or []
        clean_imgs = []
        for src in raw_imgs:
            u = _normalize_cdn_url(src)
            if u:
                clean_imgs.append(u)
        if clean_imgs:
            notices.append({
                "id": str(ntc.get("id", "")),
                "count": len(clean_imgs),
                "images": clean_imgs,
            })

    return jsonify({
        "available": bool(notices),
        "novel_id": str(novel_id),
        "np_id": str(np_id),
        "notices": notices,
    })

@app.route("/api/read/<novel_id>/notice/<notice_id>/img/<int:idx>")
@login_required
def api_notice_image(novel_id, notice_id, idx):
    limited = enforce_rate_limit("asset")
    if limited:
        return limited

    novel = find_novel(novel_id)
    if not novel:
        return "Not found", 404

    np_id = _resolve_novelpia_key(novel)
    if not np_id:
        return "Not found", 404

    manifest = load_notice_manifest(np_id)
    if not manifest:
        return "Not found", 404

    target = next(
        (n for n in manifest.get("notices", []) if str(n.get("id")) == str(notice_id)),
        None,
    )
    if not target:
        return "Not found", 404

    images = target.get("images", []) or []
    if idx < 0 or idx >= len(images):
        return "Not found", 404

    src = _normalize_cdn_url(images[idx])
    if not src:
        return "Bad image URL.", 400

    path, mime = _cache_cdn_image(src)
    if not path:
        return "Upstream image unavailable.", 502

    resp = send_file(path, mimetype=mime)
    resp.headers["Cache-Control"] = "public, max-age=604800"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp

# ====================================================================
# 12. ROUTES - NOVEL DOWNLOADS (LOCAL UPLOADS & TELEGRAM STREAMING)
# ====================================================================
@app.route("/download/<path:novel_ref>")
@login_required
def download_file(novel_ref):
    novel = find_novel(novel_ref)
    if not novel:
        return "Not found.", 404

    want_raw = request.args.get("type", "").lower() == "raw"
    user_email = session.get("user_email", "")
    is_admin = user_email in ADMIN_EMAILS
    client_ip = get_client_ip()
    nkey = novel_key(novel)

    if not is_admin and not check_download_limit(user_email, nkey):
        source_label = (
            novel.get("raw_tg_link") if want_raw else novel.get("tg_link")
        ) or f"local:{nkey}"
        log_download_limit_exceeded(user_email, source_label, client_ip)
        return "Daily limit reached.", 429

    local_path = (
        novel.get("raw_epub_path") if want_raw else novel.get("translated_epub_path")
    )
    if local_path:
        local_path = os.path.realpath(str(local_path))
        upload_root = os.path.realpath(USER_UPLOAD_EPUB_DIR)
        structured_root = os.path.realpath(STRUCTURED_OUTPUT_DIR)
        batched_root = os.path.realpath(BATCHED_EPUBS_DIR)
        is_allowed = (
            local_path.startswith(upload_root + os.sep)
            or local_path.startswith(structured_root + os.sep)
            or local_path == structured_root
            or local_path.startswith(structured_root)
            or local_path.startswith(batched_root + os.sep)
            or local_path == batched_root
        )
        if not (is_allowed and os.path.isfile(local_path)):
            return "File is unavailable.", 404

        download_name = (
            novel.get("raw_original_name") if want_raw else novel.get("translated_original_name")
        ) or f"{nkey}-{'raw' if want_raw else 'translated'}.epub"

        new_count = increment_download_count(user_email, nkey)
        log_download_event(user_email, novel, f"local:{nkey}", want_raw, client_ip, new_count)

        def record_local_download(store):
            user = store.setdefault(user_email, {})
            record = user.setdefault(nkey, {"status": "none", "progress": 0})
            record["dl"] = to_int(record.get("dl"), 0) + 1
            record["last_dl"] = time.time()

        try:
            mutate_user_data(record_local_download, shadow_email=user_email, shadow_reason="local_download")
        except Exception as exc:
            print(f"[WARN] Could not record local download signal: {exc}")

        return send_file(
            local_path,
            mimetype="application/epub+zip",
            as_attachment=True,
            download_name=_safe_upload_filename(download_name, "novel.epub"),
            conditional=True,
        )

    # The novel does not have a local upload, so use the isolated Telegram service.
    tg_link = (novel.get("raw_tg_link") if want_raw else novel.get("tg_link")) or ""
    if not tg_link:
        return "File not available.", 404

    parsed = parse_telegram_link(tg_link)
    if not parsed:
        return "Invalid Link.", 400
    channel_id, message_id = parsed
    if TELEGRAM_GATEWAY is None:
        return "Telegram service unavailable.", 503
    try:
        upstream = TELEGRAM_GATEWAY.open_media(channel_id, message_id)
    except TelegramGatewayError as exc:
        return str(exc), exc.status_code

    new_count = increment_download_count(user_email, nkey)
    log_download_event(user_email, novel, tg_link, want_raw, client_ip, new_count)

    def _record_download(store):
        user = store.setdefault(user_email, {})
        record = user.setdefault(novel_key(novel), {"status": "none", "progress": 0})
        record["dl"] = to_int(record.get("dl"), 0) + 1
        record["last_dl"] = time.time()
    try:
        mutate_user_data(_record_download, shadow_email=user_email, shadow_reason="telegram_download")
    except Exception as exc:
        print(f"[WARN] Could not record download signal: {exc}")

    def generate_response():
        try:
            yield from upstream.iter_content(chunk_size=512 * 1024)
        finally:
            upstream.close()

    safe_filename = quote(
        _safe_upload_filename(
            TELEGRAM_GATEWAY.response_filename(upstream), "file.epub"
        )
    )
    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{safe_filename}"}
    file_size = TELEGRAM_GATEWAY.response_length(upstream)
    if file_size is not None:
        headers["Content-Length"] = file_size
    return Response(generate_response(), mimetype="application/epub+zip", headers=headers)

# ====================================================================
# 12.1 CLIENT-FETCHED SERVER-SIDE EPUB PACKAGING PIPELINE
# (0% Mobile Browser RAM overhead, 0% Oracle CDN Scraping block)
# ====================================================================

EPUB_PACKAGE_SESSIONS_DIR = _env_str(
    "EPUB_PACKAGE_SESSIONS_DIR",
    os.path.join(META_DIR, "epub_package_sessions"),
)
os.makedirs(EPUB_PACKAGE_SESSIONS_DIR, exist_ok=True)
PACKAGE_JOB_STORE = JobStore(PACKAGE_JOBS_DB_PATH)
_EPUB_PACKAGE_SESSION_RE = re.compile(r"^[0-9a-f]{32}$")
_EPUB_PACKAGE_LOCK = threading.Lock()
_ALLOWED_PACKAGE_IMAGE_MIMES = {
    **_ALLOWED_COVER_MIMES,
    "image/bmp": ".bmp",
}


def _sniff_package_image_mime(data):
    mime = _sniff_image_mime(data)
    if mime:
        return mime
    if data.startswith(b"BM"):
        return "image/bmp"
    return ""


def _epub_package_session_path(session_id):
    if not _EPUB_PACKAGE_SESSION_RE.fullmatch(str(session_id or "")):
        return ""
    return os.path.join(EPUB_PACKAGE_SESSIONS_DIR, session_id)


def _load_owned_epub_session(session_id):
    sess_dir = _epub_package_session_path(session_id)
    if not sess_dir or not os.path.isdir(sess_dir) or os.path.islink(sess_dir):
        return "", None
    meta = read_json_file(os.path.join(sess_dir, "meta.json"), None)
    if not isinstance(meta, dict):
        return "", None
    owner = str(meta.get("owner_email") or "").strip().lower()
    current_user = str(session.get("user_email") or "").strip().lower()
    created_at = meta.get("created_at")
    try:
        expired = float(created_at) < time.time() - EPUB_PACKAGE_SESSION_TTL_SECONDS
    except (TypeError, ValueError):
        expired = True
    if expired:
        shutil.rmtree(sess_dir, ignore_errors=True)
        return "", None
    if not owner or owner != current_user:
        return "", None
    return sess_dir, meta


def _epub_package_session_usage(sess_dir):
    total_bytes = 0
    file_count = 0
    for root, dirs, files in os.walk(sess_dir, followlinks=False):
        dirs[:] = [name for name in dirs if not os.path.islink(os.path.join(root, name))]
        for filename in files:
            path = os.path.join(root, filename)
            if os.path.islink(path):
                raise EpubSafetyError("The package session contains an unsafe link.")
            total_bytes += os.path.getsize(path)
            file_count += 1
    return total_bytes, file_count


def _active_epub_sessions_for_user(user_email):
    count = 0
    now = time.time()
    try:
        names = os.listdir(EPUB_PACKAGE_SESSIONS_DIR)
    except OSError:
        return 0
    for name in names:
        sess_dir = _epub_package_session_path(name)
        if not sess_dir or not os.path.isdir(sess_dir) or os.path.islink(sess_dir):
            continue
        meta = read_json_file(os.path.join(sess_dir, "meta.json"), None)
        if not isinstance(meta, dict):
            continue
        try:
            active = float(meta.get("created_at")) >= now - EPUB_PACKAGE_SESSION_TTL_SECONDS
        except (TypeError, ValueError):
            active = False
        if active and str(meta.get("owner_email") or "").strip().lower() == user_email:
            count += 1
    return count

def _cleanup_old_epub_sessions():
    now = time.time()
    try:
        if not os.path.exists(EPUB_PACKAGE_SESSIONS_DIR):
            return
        for item in os.listdir(EPUB_PACKAGE_SESSIONS_DIR):
            item_path = _epub_package_session_path(item)
            if not item_path:
                continue
            if os.path.getmtime(item_path) < now - EPUB_PACKAGE_SESSION_TTL_SECONDS:
                if os.path.islink(item_path):
                    os.remove(item_path)
                elif os.path.isdir(item_path):
                    shutil.rmtree(item_path, ignore_errors=True)
    except Exception:
        pass

@app.route("/api/epub_package/init", methods=["POST"])
@login_required
def epub_package_init():
    _cleanup_old_epub_sessions()
    data = request.get_json(force=True, silent=True) or {}
    novel_ref = str(data.get("novel_key") or data.get("id") or "").strip()
    want_raw = bool(data.get("want_raw"))

    novel = find_novel(novel_ref)
    if not novel:
        return jsonify({"error": "Novel not found"}), 404

    user_email = session.get("user_email", "")
    nkey = novel_key(novel)
    if user_email not in ADMIN_EMAILS and not check_download_limit(user_email, nkey):
        return jsonify({"error": "Daily download limit reached. Resets at midnight."}), 429
    if _active_epub_sessions_for_user(user_email) >= MAX_EPUB_PACKAGE_SESSIONS_PER_USER:
        return jsonify({"error": "Too many active EPUB package sessions."}), 429

    local_path = (
        novel.get("raw_epub_path")
        if want_raw
        else novel.get("translated_epub_path")
    )

    if not local_path or not os.path.isfile(local_path):
        return jsonify({"error": "Base EPUB not available locally on server"}), 404

    try:
        _validate_epub_archive(local_path)
    except (EpubSafetyError, OSError) as exc:
        return jsonify({"error": f"Base EPUB is invalid: {exc}"}), 400

    session_id = secrets.token_hex(16)
    sess_dir = os.path.join(EPUB_PACKAGE_SESSIONS_DIR, session_id)
    try:
        os.makedirs(sess_dir, exist_ok=False)
        os.makedirs(os.path.join(sess_dir, "images"), exist_ok=False)
        base_copy = os.path.join(sess_dir, "base.epub")
        with open(local_path, "rb") as source:
            copy_upload_limited(source, base_copy, MAX_EPUB_PACKAGE_SESSION_BYTES)

        remote_urls = set()
        def add_remote_urls(pattern, content):
            for match in re.finditer(pattern, content, re.I):
                value = match.group(1).strip().replace("&amp;", "&")
                remote_urls.add(value)
                if len(remote_urls) > MAX_EPUB_PACKAGE_SESSION_FILES:
                    raise EpubSafetyError(
                        "The base EPUB references too many remote images."
                    )

        for _name, content in iter_epub_text_entries(base_copy, _epub_limits()):
            add_remote_urls(
                r'<img[^>]+src=[\"\'](https?://[^\s\"\'\<\>]+)[\"\']',
                content,
            )
            add_remote_urls(
                r'<image[^>]+(?:xlink:href|href)=[\"\'](https?://[^\s\"\'\<\>]+)[\"\']',
                content,
            )
            add_remote_urls(
                r'(https?://[^\s\"\'\<\>]+\.(?:file|jpg|jpeg|png|webp|gif|bmp)\b[^\s\"\'\<\>]*|https?://images\.novelpia\.com/[^\s\"\'\<\>]+)',
                content,
            )
    except Exception as e:
        shutil.rmtree(sess_dir, ignore_errors=True)
        return jsonify({"error": f"Failed reading base EPUB: {e}"}), 400

    filename = (
        novel.get("raw_original_name") if want_raw else novel.get("translated_original_name")
    ) or f"{novel_key(novel)}.epub"

    struct_novel_dir = None
    possible_ids = []
    if novel.get("id"): possible_ids.append(str(novel["id"]))
    for sid in (novel.get("_source_ids") or []):
        if sid and str(sid) not in possible_ids: possible_ids.append(str(sid))
    for nid in possible_ids:
        sdir = os.path.join(STRUCTURED_OUTPUT_DIR, str(nid))
        if os.path.isdir(sdir):
            struct_novel_dir = sdir
            break

    meta = {
        "session_id": session_id,
        "owner_email": user_email,
        "novel_ref": novel_ref,
        "struct_novel_dir": struct_novel_dir,
        "filename": filename,
        "created_at": time.time(),
        "total_urls": len(remote_urls),
        "urls": sorted(remote_urls)
    }
    write_json_atomic(os.path.join(sess_dir, "meta.json"), meta)

    return jsonify({
        "status": "ok",
        "session_id": session_id,
        "total_images": len(remote_urls),
        "urls": sorted(remote_urls),
        "filename": filename
    })

@app.route("/api/epub_package/upload_batch/<session_id>", methods=["POST"])
@login_required
def epub_package_upload_batch(session_id):
    sess_dir, meta = _load_owned_epub_session(session_id)
    if not sess_dir:
        return jsonify({"error": "Session expired or not found"}), 404
    if PACKAGE_JOB_STORE.get_active_by_dedupe(f"epub_package:{session_id}"):
        return jsonify({"error": "This package session is already finalized."}), 409

    images_dir = os.path.join(sess_dir, "images")
    url_map_file = os.path.join(sess_dir, "url_map.json")
    batch_map_raw = request.form.get("url_mapping", "")
    try:
        batch_map = json.loads(batch_map_raw) if batch_map_raw else {}
    except json.JSONDecodeError:
        return jsonify({"error": "url_mapping must be valid JSON."}), 400
    if not isinstance(batch_map, dict):
        return jsonify({"error": "url_mapping must be a JSON object."}), 400

    allowed_urls = {
        str(value)
        for value in (meta.get("urls") or [])
        if isinstance(value, str)
    }
    created_paths = []
    obsolete_paths = []
    incoming_path = ""
    try:
        with _EPUB_PACKAGE_LOCK:
            url_map = read_json_file(url_map_file, {})
            if not isinstance(url_map, dict):
                raise EpubSafetyError("The package session mapping is invalid.")
            session_bytes, _ = _epub_package_session_usage(sess_dir)
            next_index = 1
            for filename in os.listdir(images_dir):
                match = re.fullmatch(r"img_(\d{4,})\.[a-z0-9]+", filename, re.I)
                if match:
                    next_index = max(next_index, int(match.group(1)) + 1)

            uploaded_count = 0
            for field_name, file_storage in request.files.items():
                orig_url = batch_map.get(field_name) or request.form.get(f"url_{field_name}")
                orig_url = str(orig_url or "").strip()
                parsed_url = urlsplit(orig_url)
                if (
                    not orig_url
                    or orig_url not in allowed_urls
                    or parsed_url.scheme not in ("http", "https")
                    or not parsed_url.netloc
                ):
                    raise EpubSafetyError("An uploaded image URL is not part of this session.")
                if (
                    orig_url not in url_map
                    and len(url_map) >= MAX_EPUB_PACKAGE_SESSION_FILES
                ):
                    raise EpubSafetyError("The package session contains too many images.")

                incoming_path = os.path.join(images_dir, f".incoming.{uuid.uuid4().hex}")
                image_size = _copy_upload_limited(
                    file_storage,
                    incoming_path,
                    MAX_EPUB_PACKAGE_IMAGE_BYTES,
                )
                with open(incoming_path, "rb") as image_file:
                    mime = _sniff_package_image_mime(image_file.read(512))
                extension = _ALLOWED_PACKAGE_IMAGE_MIMES.get(mime)
                if not extension:
                    raise EpubSafetyError(
                        "Packaged images must be JPEG, PNG, GIF, WebP, or BMP files."
                    )
                old_filename = url_map.get(orig_url)
                old_path = resolve_under(images_dir, old_filename) if old_filename else ""
                old_size = (
                    os.path.getsize(old_path)
                    if old_path and os.path.isfile(old_path) and not os.path.islink(old_path)
                    else 0
                )
                if session_bytes - old_size + image_size > MAX_EPUB_PACKAGE_SESSION_BYTES:
                    raise EpubSafetyError("The package session exceeds its storage limit.")

                safe_filename = f"img_{next_index:04d}{extension}"
                next_index += 1
                save_path = os.path.join(images_dir, safe_filename)
                os.replace(incoming_path, save_path)
                incoming_path = ""
                created_paths.append(save_path)
                session_bytes = session_bytes - old_size + image_size

                if old_filename and old_filename != safe_filename:
                    if old_path and os.path.isfile(old_path):
                        obsolete_paths.append(old_path)
                url_map[orig_url] = safe_filename
                uploaded_count += 1

            write_json_atomic(url_map_file, url_map)
            for old_path in obsolete_paths:
                try:
                    os.remove(old_path)
                except OSError:
                    pass
    except (EpubSafetyError, OSError) as exc:
        if incoming_path:
            try:
                os.remove(incoming_path)
            except OSError:
                pass
        for created_path in created_paths:
            try:
                os.remove(created_path)
            except OSError:
                pass
        status = 413 if "limit" in str(exc).lower() or "too many" in str(exc).lower() else 400
        return jsonify({"error": str(exc)}), status

    return jsonify({"status": "ok", "saved_in_batch": uploaded_count, "total_saved": len(url_map)})

@app.route("/api/epub_package/finalize/<session_id>", methods=["POST"])
@login_required
def epub_package_finalize(session_id):
    return _enqueue_epub_package_job(session_id)


def _job_response(job):
    response = {
        "job_id": job.job_id,
        "kind": job.kind,
        "state": job.state,
        "progress": job.progress,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "status_url": f"/api/jobs/{job.job_id}",
    }
    if job.state == "done" and job.result:
        response.update(job.result)
    if job.state == "failed":
        response["error"] = {
            "code": job.error_code or "failed",
            "message": job.error_message or "The package job failed.",
        }
    return response


def _enqueue_epub_package_job(session_id):
    sess_dir, _meta = _load_owned_epub_session(session_id)
    if not sess_dir:
        return jsonify({"error": "Session not found"}), 404
    job, created = PACKAGE_JOB_STORE.enqueue(
        kind="epub_package",
        owner_email=session.get("user_email", ""),
        payload={"session_id": session_id},
        dedupe_key=f"epub_package:{session_id}",
        max_attempts=PACKAGE_JOB_MAX_ATTEMPTS,
        timeout_seconds=PACKAGE_JOB_TIMEOUT_SECONDS,
        retention_seconds=PACKAGE_JOB_RETENTION_SECONDS,
    )
    response = _job_response(job)
    response["created"] = created
    return jsonify(response), 202


@app.route("/api/jobs/package", methods=["POST"])
@login_required
def enqueue_package_job():
    data = request.get_json(force=True, silent=True) or {}
    return _enqueue_epub_package_job(str(data.get("session_id") or "").strip())


@app.route("/api/jobs/<job_id>")
@login_required
def get_job(job_id):
    job = PACKAGE_JOB_STORE.get_owned(job_id, session.get("user_email", ""))
    if job is None:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(_job_response(job))


@app.route("/api/jobs/<job_id>/cancel", methods=["POST"])
@login_required
def cancel_job(job_id):
    state = PACKAGE_JOB_STORE.request_cancel(job_id, session.get("user_email", ""))
    if state is None:
        return jsonify({"error": "Job not found"}), 404
    job = PACKAGE_JOB_STORE.get_owned(job_id, session.get("user_email", ""))
    return jsonify(_job_response(job)), 202 if state == "processing" else 200

@app.route("/api/epub_package/download/<session_id>")
@login_required
def epub_package_download_file(session_id):
    sess_dir, meta = _load_owned_epub_session(session_id)
    if not sess_dir:
        return "Download expired or file not ready.", 404
    final_epub = os.path.join(sess_dir, "final.epub")

    if not os.path.isfile(final_epub):
        return "Download expired or file not ready.", 404

    filename = meta.get("filename", "novel.epub")

    user_email = session.get("user_email", "")
    nkey = meta.get("novel_ref", "")
    new_count = increment_download_count(user_email, nkey)

    @after_this_request
    def remove_session(response):
        def _deferred_delete():
            time.sleep(15) # 15s grace period
            shutil.rmtree(sess_dir, ignore_errors=True)
        threading.Thread(target=_deferred_delete, daemon=True).start()
        return response

    return send_file(
        os.path.abspath(final_epub),
        mimetype="application/epub+zip",
        as_attachment=True,
        download_name=_safe_upload_filename(filename, "novel.epub"),
        conditional=True
    )

# ====================================================================
# PASSWORD RESET & FORGOT ROUTES
# ====================================================================
@app.route("/forgot", methods=["GET", "POST"])
def forgot_password():
    if request.method == "GET":
        return render_template("forgot_password.html", error=None)

    limited = enforce_rate_limit("auth")
    if limited:
        return limited

    email = request.form.get("email", "").strip().lower()

    if email not in ADMIN_EMAILS and email not in get_allowed_emails():
        return render_template(
            "forgot_password.html",
            error="This email is not currently approved. Send your email to @sigma619 again to get access."
        )

    code = _new_code()

    def m(users):
        u = users.get(email)
        if not u or not u.get("verified"):
            return "missing"

        u["reset_code_hash"] = _hash_code(code)
        u["reset_code_expires"] = time.time() + CODE_TTL_SECONDS
        u["reset_code_attempts"] = 0
        return "ok"

    status = mutate_users(m, shadow_reason="auth_reset_request")

    if status != "ok":
        return render_template(
            "forgot_password.html",
            error="No verified account exists for this email. Please register first."
        )

    send_email(
        email,
        "Your ArchiveDB password reset code",
        f"Your password reset code is {code}\nIt expires in 10 minutes."
    )

    return redirect(url_for("reset_password", email=email))

@app.route("/reset_password", methods=["GET", "POST"])
def reset_password():
    email = request.values.get("email", "").strip().lower()
    if request.method == "GET":
        return render_template("reset_password.html", email=email, error=None)

    limited = enforce_rate_limit("auth")
    if limited:
        return limited

    if not email:
        return render_template(
            "reset_password.html",
            email=email,
            error="Please enter your email."
        )

    code = request.form.get("code", "").strip()
    new_password = request.form.get("password", "")

    if len(new_password) < MIN_PASSWORD_LEN:
        return render_template(
            "reset_password.html",
            email=email,
            error=f"Password must be {MIN_PASSWORD_LEN}+ characters."
        )

    def m(users):
        u = users.get(email)
        if not u or not u.get("verified"):
            return "bad"
        if time.time() > u.get("reset_code_expires", 0):
            return "expired"
        if u.get("reset_code_attempts", 0) >= MAX_CODE_ATTEMPTS:
            return "locked"

        u["reset_code_attempts"] = u.get("reset_code_attempts", 0) + 1
        if _hash_code(code) != u.get("reset_code_hash"):
            return "wrong"

        u["pwd_hash"] = generate_password_hash(new_password)
        u.pop("reset_code_hash", None)
        u.pop("reset_code_expires", None)
        u.pop("reset_code_attempts", None)
        return "ok"

    status = mutate_users(m, shadow_reason="auth_password_reset")
    if status == "ok":
        session.permanent = True
        session["user_email"] = email
        return redirect(url_for("index"))

    msgs = {
        "wrong": "Incorrect reset code.",
        "expired": "Reset code expired. Request a new one.",
        "locked": "Too many attempts. Request a new reset code.",
        "bad": "Invalid reset request.",
    }
    return render_template(
        "reset_password.html",
        email=email,
        error=msgs.get(status, "Password reset error.")
    )

if __name__ == "__main__":
    app.run(host=_env_str("HOST", "127.0.0.1"), port=_env_int("PORT", 5004))
