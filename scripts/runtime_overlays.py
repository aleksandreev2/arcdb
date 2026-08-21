from __future__ import annotations

import hashlib
from pathlib import Path

OVERLAY_VERSION = "users-auth-dual-write-v1"


def overlay_digest() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"Runtime overlay {label!r} expected 1 match, found {count}.")
    return text.replace(old, new, 1)


def _replace_in_section(
    text: str,
    *,
    start: str,
    end: str,
    old: str,
    new: str,
    label: str,
) -> str:
    start_at = text.find(start)
    if start_at < 0:
        raise RuntimeError(f"Runtime overlay {label!r}: start marker not found.")
    end_at = text.find(end, start_at + len(start))
    if end_at < 0:
        raise RuntimeError(f"Runtime overlay {label!r}: end marker not found.")
    section = text[start_at:end_at]
    count = section.count(old)
    if count != 1:
        raise RuntimeError(f"Runtime overlay {label!r} expected 1 section match, found {count}.")
    section = section.replace(old, new, 1)
    return text[:start_at] + section + text[end_at:]


def apply_gallery_app_overlay(path: Path) -> None:
    text = path.read_text(encoding="utf-8")

    text = _replace_once(
        text,
        '''if not (SMTP_USER and SMTP_PASS):
    _warn("SMTP_USER/SMTP_PASS not set - verification codes will only print to the console.")''',
        '''if not (SMTP_USER and SMTP_PASS):
    if (os.environ.get("ARCHIVEDB_AUTH_TEST_MODE", "0") == "1"
            and os.environ.get("ARCHIVEDB_LOCAL_DEV", "0") == "1"):
        _warn("SMTP is disabled; local auth test message bodies are suppressed.")
    else:
        _warn("SMTP_USER/SMTP_PASS not set - verification codes will only print to the console.")''',
        "local auth test SMTP warning",
    )

    old_mutator = '''def mutate_user_data(mutator):
    with _USER_DATA_LOCK:
        data = _load_user_data_unlocked()
        result = mutator(data)
        write_json_atomic(USER_DATA_PATH, data)
        return result
'''
    new_mutator = '''def mutate_user_data(mutator, shadow_email=None, shadow_reason="user_data"):
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
'''
    text = _replace_once(text, old_mutator, new_mutator, "mutate_user_data shadow hook")

    old_metadata_helpers = '''def load_custom_meta():
    return read_json_file(CUSTOM_META_PATH, {})

def save_custom_meta_entry(filename, entry):
    with _CUSTOM_META_LOCK:
        custom_meta = load_custom_meta()
        custom_meta[filename] = entry
        write_json_atomic(CUSTOM_META_PATH, custom_meta, indent=4, ensure_ascii=False)

# ===================== User-uploaded novels persistence =====================
def _load_user_uploads_unlocked():
    data = read_json_file(USER_UPLOADS_PATH, {})
    return data if isinstance(data, dict) else {}

def load_user_uploads():
    with _USER_UPLOADS_LOCK:
        return _load_user_uploads_unlocked()

def mutate_user_uploads(mutator):
    with _USER_UPLOADS_LOCK:
        data = _load_user_uploads_unlocked()
        result = mutator(data)
        write_json_atomic(
            USER_UPLOADS_PATH,
            data,
            ensure_ascii=False,
            indent=2,
        )
        return result
'''
    new_metadata_helpers = '''def load_custom_meta():
    return read_json_file(CUSTOM_META_PATH, {})

def save_custom_meta_entry(filename, entry):
    with _CUSTOM_META_LOCK:
        custom_meta = load_custom_meta()
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
        return _load_user_uploads_unlocked()

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
'''
    text = _replace_once(
        text,
        old_metadata_helpers,
        new_metadata_helpers,
        "custom metadata and uploads shadow hooks",
    )

    old_collection_helpers = '''def load_collections():
    with _COLLECTIONS_LOCK:
        return read_json_file(COLLECTIONS_PATH, {})

def save_collections(data):
    with _COLLECTIONS_LOCK:
        write_json_atomic(COLLECTIONS_PATH, data, ensure_ascii=False, indent=2)
'''
    new_collection_helpers = '''def load_collections():
    with _COLLECTIONS_LOCK:
        return read_json_file(COLLECTIONS_PATH, {})

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
'''
    text = _replace_once(
        text,
        old_collection_helpers,
        new_collection_helpers,
        "collection metadata shadow hook",
    )

    old_allowlist_lock = '''_ALLOWLIST_WRITE_LOCK = threading.Lock()

def remove_email_from_allowlist(email):'''
    new_allowlist_lock = '''_ALLOWLIST_WRITE_LOCK = threading.Lock()

def _mirror_allowlist_shadow(reason):
    try:
        from arcdb.storage.runtime_state import mirror_allowed_emails
        with _ALLOWLIST_WRITE_LOCK:
            mirror_allowed_emails(get_allowed_emails(), reason=reason)
    except Exception as exc:
        print(f"[STATE-DUAL-WRITE][ERROR] {reason}: {exc}")
        if os.environ.get("STATE_DUAL_WRITE_STRICT", "0") == "1":
            raise

def remove_email_from_allowlist(email):'''
    text = _replace_once(
        text,
        old_allowlist_lock,
        new_allowlist_lock,
        "allowlist shadow helper",
    )

    old_users_mutator = '''def mutate_users(mutator):
    with _USERS_LOCK:
        data = _load_users_unlocked()
        result = mutator(data)
        write_json_atomic(USERS_PATH, data)
        return result
'''
    new_users_mutator = '''def mutate_users(mutator, shadow_reason="users_auth"):
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
'''
    text = _replace_once(
        text,
        old_users_mutator,
        new_users_mutator,
        "users auth shadow hook",
    )

    text = _replace_in_section(
        text,
        start="def send_email(to_addr, subject, body):",
        end="def login_required(f):",
        old='''    if not (SMTP_USER and SMTP_PASS):
        print(f"[EMAIL] (SMTP not configured) to={_log_safe(to_addr)} :: {_log_safe(body)}")
        return True''',
        new='''    if not (SMTP_USER and SMTP_PASS):
        if (os.environ.get("ARCHIVEDB_AUTH_TEST_MODE", "0") == "1"
                and os.environ.get("ARCHIVEDB_LOCAL_DEV", "0") == "1"):
            print(f"[EMAIL] Local auth test message suppressed for {_log_safe(to_addr)}.")
        else:
            print(f"[EMAIL] (SMTP not configured) to={_log_safe(to_addr)} :: {_log_safe(body)}")
        return True''',
        label="local auth test email sink",
    )
    text = _replace_in_section(
        text,
        start="def remove_email_from_allowlist(email):",
        end="def extract_emails_from_text(text):",
        old='''    with _ALLOWED_EMAILS_LOCK:
        _allowed_emails_cache["mtime"] = None
    return True''',
        new='''    with _ALLOWED_EMAILS_LOCK:
        _allowed_emails_cache["mtime"] = None
    _mirror_allowlist_shadow("allowlist_remove")
    return True''',
        label="allowlist remove",
    )
    text = _replace_in_section(
        text,
        start="def add_emails_to_allowlist(emails):",
        end="_IP_EMAIL_LOCK = threading.Lock()",
        old='''    with _ALLOWED_EMAILS_LOCK:
        _allowed_emails_cache["mtime"] = None
    return added''',
        new='''    with _ALLOWED_EMAILS_LOCK:
        _allowed_emails_cache["mtime"] = None
    _mirror_allowlist_shadow("allowlist_add")
    return added''',
        label="allowlist add",
    )

    text = _replace_in_section(
        text,
        start='@app.route("/api/user_status", methods=["POST"])',
        end='@app.route("/api/bulk_remove", methods=["POST"])',
        old='user_record = mutate_user_data(mutator)',
        new='user_record = mutate_user_data(mutator, shadow_email=user_email, shadow_reason="user_status")',
        label="user_status",
    )
    text = _replace_in_section(
        text,
        start='@app.route("/api/bulk_remove", methods=["POST"])',
        end='@app.route("/api/user_progress", methods=["POST"])',
        old='user_record = mutate_user_data(mutator)',
        new='user_record = mutate_user_data(mutator, shadow_email=user_email, shadow_reason="bulk_remove")',
        label="bulk_remove",
    )
    text = _replace_in_section(
        text,
        start='@app.route("/api/user_progress", methods=["POST"])',
        end='@app.route("/api/user_hide", methods=["POST"])',
        old='mutate_user_data(mutator)',
        new='mutate_user_data(mutator, shadow_email=user_email, shadow_reason="user_progress")',
        label="user_progress",
    )
    text = _replace_in_section(
        text,
        start='@app.route("/api/user_hide", methods=["POST"])',
        end='def _passes_filters(',
        old='user_record = mutate_user_data(mutator)',
        new='user_record = mutate_user_data(mutator, shadow_email=user_email, shadow_reason="user_hide")',
        label="user_hide",
    )
    text = _replace_once(
        text,
        'mutate_user_data(record_local_download)',
        'mutate_user_data(record_local_download, shadow_email=user_email, shadow_reason="local_download")',
        "local download counter",
    )
    text = _replace_once(
        text,
        'mutate_user_data(_record_download)',
        'mutate_user_data(_record_download, shadow_email=user_email, shadow_reason="telegram_download")',
        "telegram download counter",
    )

    text = _replace_in_section(
        text,
        start='@app.route("/register", methods=["GET", "POST"])',
        end='@app.route("/verify", methods=["GET", "POST"])',
        old='mutate_users(m)',
        new='mutate_users(m, shadow_reason="auth_register")',
        label="auth register",
    )
    text = _replace_in_section(
        text,
        start='@app.route("/verify", methods=["GET", "POST"])',
        end='@app.route("/login", methods=["GET", "POST"])',
        old='mutate_users(m)',
        new='mutate_users(m, shadow_reason="auth_verify")',
        label="auth verify",
    )
    text = _replace_in_section(
        text,
        start='@app.route("/forgot", methods=["GET", "POST"])',
        end='@app.route("/reset_password", methods=["GET", "POST"])',
        old='mutate_users(m)',
        new='mutate_users(m, shadow_reason="auth_reset_request")',
        label="auth reset request",
    )
    text = _replace_in_section(
        text,
        start='@app.route("/reset_password", methods=["GET", "POST"])',
        end='if __name__ == "__main__":',
        old='mutate_users(m)',
        new='mutate_users(m, shadow_reason="auth_password_reset")',
        label="auth password reset",
    )

    text = _replace_in_section(
        text,
        start='@app.route("/api/collection_create", methods=["POST"])',
        end='@app.route("/api/collection_rename", methods=["POST"])',
        old='save_collections(allcols)',
        new='save_collections(allcols, shadow_email=email, shadow_reason="collection_create")',
        label="collection_create",
    )
    text = _replace_in_section(
        text,
        start='@app.route("/api/collection_rename", methods=["POST"])',
        end='@app.route("/api/collection_delete", methods=["POST"])',
        old='save_collections(allcols)',
        new='save_collections(allcols, shadow_email=email, shadow_reason="collection_rename")',
        label="collection_rename",
    )
    text = _replace_in_section(
        text,
        start='@app.route("/api/collection_delete", methods=["POST"])',
        end='@app.route("/api/collection_assign", methods=["POST"])',
        old='''save_collections(allcols)

    all_udata = load_user_data()
    udata = all_udata.get(email, {})
    changed = False
    for entry in udata.values():
        if isinstance(entry, dict) and cid in (entry.get("collections") or []):
            entry["collections"] = [x for x in entry["collections"] if x != cid]
            changed = True
    if changed:
        all_udata[email] = udata
        save_user_data(all_udata)''',
        new='''save_collections(allcols, shadow_email=email, shadow_reason="collection_delete")

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
    )''',
        label="collection_delete metadata and memberships",
    )
    text = _replace_in_section(
        text,
        start='@app.route("/api/collection_assign", methods=["POST"])',
        end='@app.route("/api/tags", methods=["GET"])',
        old='''    all_udata = load_user_data()
    udata = all_udata.setdefault(email, {})
    entry = udata.setdefault(novel_id, {})
    cur = [x for x in (entry.get("collections") or []) if x != cid]
    if add:
        cur.append(cid)
    entry["collections"] = cur
    all_udata[email] = udata
    save_user_data(all_udata)
    return jsonify({"user_data": udata})''',
        new='''    def assign_collection(store):
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
    return jsonify({"user_data": udata})''',
        label="collection_assign membership",
    )
    text = _replace_in_section(
        text,
        start='@app.route("/api/community/import_collection", methods=["POST"])',
        end='@app.route("/api/edit", methods=["POST"])',
        old='''        write_json_atomic(COLLECTIONS_PATH, allcols, ensure_ascii=False, indent=2)

    def m(store):''',
        new='''        write_json_atomic(COLLECTIONS_PATH, allcols, ensure_ascii=False, indent=2)
        _mirror_collections_shadow(email, user_cols, "community_import_collection")

    def m(store):''',
        label="community import collection metadata",
    )
    text = _replace_in_section(
        text,
        start='@app.route("/api/community/import_collection", methods=["POST"])',
        end='@app.route("/api/edit", methods=["POST"])',
        old='mutate_user_data(m)',
        new='mutate_user_data(m, shadow_email=email, shadow_reason="community_import_memberships")',
        label="community import memberships",
    )
    text = _replace_in_section(
        text,
        start='@app.route("/api/upload_novel", methods=["POST"])',
        end='@app.route("/api/upload/<upload_id>/asset/cover")',
        old='mutate_user_uploads(save_record)',
        new='mutate_user_uploads(save_record, shadow_reason="upload_create")',
        label="upload create",
    )
    text = _replace_in_section(
        text,
        start='@app.route("/api/upload_novel", methods=["POST"])',
        end='@app.route("/api/upload/<upload_id>/asset/cover")',
        old='''    except Exception as exc:
        print(f"[WARN] Novel upload failed for {_log_safe(user_email)}: {exc}")
        shutil.rmtree(temporary_extract_dir, ignore_errors=True)
        shutil.rmtree(originals_dir, ignore_errors=True)''',
        new='''    except Exception as exc:
        print(f"[WARN] Novel upload failed for {_log_safe(user_email)}: {exc}")
        if getattr(exc, "arcdb_legacy_write_succeeded", False):
            return json_error(
                "The upload was saved, but local shadow verification failed. "
                "Restart local ArchiveDB to rebuild the shadow before retrying.",
                500,
            )
        shutil.rmtree(temporary_extract_dir, ignore_errors=True)
        shutil.rmtree(originals_dir, ignore_errors=True)''',
        label="upload shadow failure preserves files",
    )
    text = _replace_in_section(
        text,
        start='@app.route("/admin/access", methods=["GET", "POST"])',
        end='@app.route("/api/read/<novel_id>/chapter/<path:chap_path>")',
        old='mutate_user_uploads(approve_record)',
        new='mutate_user_uploads(approve_record, shadow_reason="upload_approve")',
        label="upload approve",
    )
    text = _replace_in_section(
        text,
        start='@app.route("/admin/access", methods=["GET", "POST"])',
        end='@app.route("/api/read/<novel_id>/chapter/<path:chap_path>")',
        old='mutate_user_uploads(remove_record)',
        new='mutate_user_uploads(remove_record, shadow_reason="upload_reject")',
        label="upload reject",
    )

    path.write_text(text, encoding="utf-8")


def apply_runtime_overlays(target: Path) -> None:
    apply_gallery_app_overlay(target / "gallery_app.py")
    (target / ".overlay.sha256").write_text(overlay_digest() + "\n", encoding="ascii")
