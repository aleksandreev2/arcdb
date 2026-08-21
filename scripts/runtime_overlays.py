from __future__ import annotations

import hashlib
from pathlib import Path

OVERLAY_VERSION = "user-state-dual-write-v1"


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

    path.write_text(text, encoding="utf-8")


def apply_runtime_overlays(target: Path) -> None:
    apply_gallery_app_overlay(target / "gallery_app.py")
    (target / ".overlay.sha256").write_text(overlay_digest() + "\n", encoding="ascii")
