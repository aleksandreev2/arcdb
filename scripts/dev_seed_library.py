from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "dev-fixtures" / "seed-manifest.json"
INBOX = ROOT / "dev-fixtures" / "inbox"
BACKUP_DIR = ROOT / ".dev-backups"

EPUB_EXT = ".epub"
HTML_EXTS = {".html", ".htm", ".xhtml"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".avif"}
ID_RE = re.compile(r"\[(\d{3,7})\]")


def parse_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        out[key] = value
    return out


def configured_path(env: dict[str, str], key: str, fallback: Path) -> Path:
    raw = env.get(key, "").strip()
    path = Path(raw) if raw else fallback
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def ensure_local_safety(env: dict[str, str]) -> dict[str, Path]:
    if env.get("ARCHIVEDB_LOCAL_DEV") != "1":
        raise RuntimeError("REFUSING TO SEED: ARCHIVEDB_LOCAL_DEV must be 1.")
    if env.get("HOST", "127.0.0.1").strip() not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("REFUSING TO SEED: HOST must be loopback-only.")

    data_root = (ROOT / "data").resolve()
    paths = {
        "translated_csv": configured_path(env, "TRANSLATED_CSV_PATH", data_root / "uploaded_novels_tracker.csv"),
        "raw_csv": configured_path(env, "RAW_MASTER_CSV_PATH", data_root / "master_library_index.csv"),
        "output": configured_path(env, "LOCAL_OUTPUT_DIR", data_root / "output"),
        "structured": configured_path(env, "STRUCTURED_OUTPUT_DIR", data_root / "structured_output"),
        "batched": configured_path(env, "BATCHED_EPUBS_DIR", data_root / "batched_epubs"),
        "meta": configured_path(env, "META_DIR", data_root / "metadata"),
    }
    for key, path in paths.items():
        try:
            path.relative_to(data_root)
        except ValueError as exc:
            raise RuntimeError(f"REFUSING TO SEED: {key} resolves outside {data_root}: {path}") from exc
    return {"data_root": data_root, **paths}


def load_manifest(path: Path) -> dict:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or doc.get("schema_version") != 1:
        raise RuntimeError("Unsupported or invalid dev seed manifest.")
    return doc


def find_source(explicit: str | None) -> Path:
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if not p.exists():
            raise FileNotFoundError(p)
        return p
    candidates = [INBOX / "Downloads.zip", ROOT / "Downloads.zip", INBOX]
    for p in candidates:
        if p.is_file() or (p.is_dir() and any(x.suffix.lower() == EPUB_EXT for x in p.iterdir())):
            return p.resolve()
    raise RuntimeError(
        "No EPUB fixture source found. Put Downloads.zip in dev-fixtures/inbox, "
        "put Downloads.zip in the repository root, or drag a ZIP/folder onto seed-dev.bat."
    )


def safe_member_name(name: str) -> bool:
    p = PurePosixPath(name.replace("\\", "/"))
    return not (p.is_absolute() or ".." in p.parts)


def extract_fixture_epubs(source: Path, temp_root: Path) -> list[Path]:
    out = temp_root / "epubs"
    out.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        return sorted([p.resolve() for p in source.iterdir() if p.is_file() and p.suffix.lower() == EPUB_EXT])
    if source.suffix.lower() == EPUB_EXT:
        target = out / source.name
        shutil.copy2(source, target)
        return [target]
    if not zipfile.is_zipfile(source):
        raise RuntimeError(f"Fixture source is not a ZIP/EPUB/directory: {source}")
    with zipfile.ZipFile(source) as zf:
        for info in zf.infolist():
            name = info.filename
            if info.is_dir() or Path(name).suffix.lower() != EPUB_EXT:
                continue
            if not safe_member_name(name):
                raise RuntimeError(f"Unsafe fixture archive member: {name}")
            target = out / Path(name).name
            if target.exists():
                stem, suffix = target.stem, target.suffix
                i = 2
                while (out / f"{stem}-{i}{suffix}").exists():
                    i += 1
                target = out / f"{stem}-{i}{suffix}"
            with zf.open(info) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
    return sorted(out.glob("*.epub"))


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def epub_metadata(path: Path) -> dict:
    report = {
        "filename": path.name,
        "size_bytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "valid_zip": False,
        "valid_epub": False,
        "title": "",
        "creator": "",
        "language": "",
        "identifier": "",
        "spine_items": 0,
        "html_files": 0,
        "image_files": 0,
        "issues": [],
    }
    try:
        with zipfile.ZipFile(path) as zf:
            bad = zf.testzip()
            if bad:
                report["issues"].append(f"CRC error: {bad}")
                return report
            report["valid_zip"] = True
            names = zf.namelist()
            report["html_files"] = sum(Path(n).suffix.lower() in HTML_EXTS for n in names)
            report["image_files"] = sum(Path(n).suffix.lower() in IMAGE_EXTS for n in names)
            opf_name = ""
            try:
                root = ET.fromstring(zf.read("META-INF/container.xml"))
                for el in root.iter():
                    if local_name(el.tag) == "rootfile" and el.attrib.get("full-path"):
                        opf_name = el.attrib["full-path"]
                        break
            except (KeyError, ET.ParseError) as exc:
                report["issues"].append(f"container.xml: {exc}")
            if not opf_name:
                opfs = [n for n in names if n.lower().endswith(".opf")]
                if opfs:
                    opf_name = opfs[0]
                    report["issues"].append("container.xml missing/invalid; used first OPF")
            if opf_name:
                try:
                    opf = ET.fromstring(zf.read(opf_name))
                    for el in opf.iter():
                        name = local_name(el.tag)
                        text = (el.text or "").strip()
                        if name in {"title", "creator", "language", "identifier"} and text and not report[name]:
                            report[name] = text
                    report["spine_items"] = sum(local_name(el.tag) == "itemref" for el in opf.iter())
                    report["valid_epub"] = report["html_files"] > 0
                except (KeyError, ET.ParseError) as exc:
                    report["issues"].append(f"OPF: {exc}")
            else:
                report["issues"].append("No OPF package document found")
    except (OSError, zipfile.BadZipFile) as exc:
        report["issues"].append(str(exc))
    return report


def selector_matches(filename: str, selector: dict) -> bool:
    lowered = filename.casefold()
    all_parts = [str(x).casefold() for x in selector.get("contains_all", [])]
    any_parts = [str(x).casefold() for x in selector.get("contains_any", [])]
    excludes = [str(x).casefold() for x in selector.get("exclude_contains", [])]
    if all_parts and not all(x in lowered for x in all_parts):
        return False
    if any_parts and not any(x in lowered for x in any_parts):
        return False
    if excludes and any(x in lowered for x in excludes):
        return False
    return True


def choose_file(files: list[Path], selector: dict | None) -> Path | None:
    if not selector:
        return None
    matches = [p for p in files if selector_matches(p.name, selector)]
    if not matches:
        return None
    matches.sort(key=lambda p: (p.stat().st_size, p.name), reverse=True)
    return matches[0]


def safe_extract_epub(epub: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with zipfile.ZipFile(epub) as zf:
        total = 0
        for info in zf.infolist():
            if not safe_member_name(info.filename):
                raise RuntimeError(f"Unsafe path inside EPUB {epub.name}: {info.filename}")
            total += info.file_size
            if total > 1_500 * 1024 * 1024:
                raise RuntimeError(f"EPUB expands beyond dev safety limit: {epub.name}")
            target = (destination / info.filename).resolve()
            try:
                target.relative_to(root)
            except ValueError as exc:
                raise RuntimeError(f"Unsafe extraction target in {epub.name}: {info.filename}") from exc
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
    shutil.copy2(epub, destination / epub.name)


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def backup_small_state(paths: dict[str, Path]) -> Path | None:
    candidates = [paths["meta"], paths["translated_csv"], paths["raw_csv"]]
    existing = [p for p in candidates if p.exists()]
    if not existing:
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = BACKUP_DIR / f"dev-state-{stamp}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in existing:
            if p.is_file():
                zf.write(p, p.relative_to(paths["data_root"]).as_posix())
            else:
                for f in p.rglob("*"):
                    if f.is_file():
                        zf.write(f, f.relative_to(paths["data_root"]).as_posix())
    backups = sorted(BACKUP_DIR.glob("dev-state-*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in backups[3:]:
        old.unlink(missing_ok=True)
    return out


def reset_local_state(paths: dict[str, Path]) -> None:
    for key in ("output", "structured", "batched", "meta"):
        p = paths[key]
        if p.exists():
            shutil.rmtree(p)
        p.mkdir(parents=True, exist_ok=True)
    for key in ("translated_csv", "raw_csv"):
        p = paths[key]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.unlink(missing_ok=True)


def seed_users(meta: Path, manifest: dict, env: dict[str, str], primary_record: dict) -> list[str]:
    primary = env.get("LOCAL_DEV_EMAIL", "dev@arcdb.local").strip().lower()
    emails = [primary]
    for item in manifest.get("users", []):
        email = str(item.get("email", "")).strip().lower()
        if email and email not in emails:
            emails.append(email)
    pwd_hash = str(primary_record.get("pwd_hash") or "")
    if not pwd_hash:
        raise RuntimeError("Local dev login is not initialized. Run scripts/dev_bootstrap.py --setup-only first.")
    now = time.time()
    users = {
        email: {
            "pwd_hash": pwd_hash,
            "verified": True,
            "created_at": float(primary_record.get("created_at") or now),
            "dev_managed": True,
        }
        for email in emails
    }
    write_json(meta / "users.json", users)
    (meta / "allowed_gmails.txt").write_text("\n".join(emails) + "\n", encoding="utf-8")
    return emails


def build_metadata(manifest: dict) -> tuple[list[dict], list[str], list[str]]:
    novels, titles, descriptions = [], [], []
    for item in manifest.get("library", []):
        nid = int(item["id"])
        raw_title = item.get("raw_title") or item.get("title") or f"Dev Novel {nid}"
        title = item.get("title") or raw_title
        novels.append({
            "id": nid,
            "title": raw_title,
            "author": item.get("author", "Dev Fixture"),
            "cover": "",
            "tags": item.get("tags", ["Dev Fixture"]),
            "synopsis": item.get("description", "Local development fixture."),
            "views": int(item.get("views", nid % 1000 + 50)),
            "likes": int(item.get("likes", nid % 100 + 5)),
            "chapters": int(item.get("chapters_hint", 0)),
            "age": int(item.get("age", 0)),
            "complete": int(bool(item.get("complete", False))),
        })
        titles.append(f"{nid}|||{raw_title}|||{title}")
        descriptions.append(f"{nid}|||{raw_title}|||{item.get('description', 'Local development fixture.')}")
    return novels, titles, descriptions


def seed_library(files: list[Path], manifest: dict, paths: dict[str, Path]) -> dict:
    by_name = {p.name: p for p in files}
    imported: list[dict] = []
    used_names: set[str] = set()
    raw_rows = []
    for p in files:
        m = ID_RE.search(p.name)
        if not m:
            continue
        nid = m.group(1)
        target = paths["batched"] / p.name
        shutil.copy2(p, target)
        raw_rows.append({"Folder": "Ongoing", "File Name": p.name, "Telegram Link": f"https://example.invalid/dev/raw/{nid}"})
        used_names.add(p.name)
        imported.append({"id": nid, "kind": "raw", "filename": p.name})
    with paths["raw_csv"].open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["Folder", "File Name", "Telegram Link"])
        writer.writeheader()
        writer.writerows(raw_rows)
    with paths["translated_csv"].open("w", encoding="utf-8-sig", newline="") as fh:
        csv.writer(fh).writerow(["File Name", "Telegram Link", "Upload Date"])
    for item in manifest.get("library", []):
        p = choose_file(files, item.get("translated_selector"))
        if not p:
            if item.get("translation_required"):
                raise RuntimeError(f"Required translated fixture not found for id {item['id']}: {item.get('translated_selector')}")
            continue
        nid = str(item["id"])
        safe_extract_epub(p, paths["structured"] / nid)
        used_names.add(p.name)
        imported.append({"id": nid, "kind": "translated", "filename": p.name})
    novels, titles, descriptions = build_metadata(manifest)
    write_json(paths["meta"] / "novels_full.json", novels)
    (paths["meta"] / "titles_en.txt").write_text("\n".join(titles) + "\n", encoding="utf-8")
    (paths["meta"] / "descriptions.txt").write_text("\n".join(descriptions) + "\n", encoding="utf-8")
    (paths["meta"] / "tags_en.txt").write_text("", encoding="utf-8")
    write_json(paths["meta"] / "custom_meta.json", {})
    write_json(paths["meta"] / "user_uploads.json", {})
    return {"imported": imported, "unused_epubs": sorted(set(by_name) - used_names)}


def seed_user_state(meta: Path, manifest: dict, emails: list[str]) -> None:
    now = time.time()
    library_ids = [str(x["id"]) for x in manifest.get("library", [])]
    primary = emails[0]
    collections = {primary: [
        {"id": "dev-reading", "name": "Читаю"},
        {"id": "dev-later", "name": "Прочитать позже"},
        {"id": "dev-test", "name": "Тестовая коллекция"},
    ]}
    user_data: dict[str, dict] = {email: {} for email in emails}
    for item in manifest.get("primary_user_state", []):
        nid = str(item["id"])
        user_data[primary][nid] = {
            "status": item.get("status", "reading"),
            "progress": int(item.get("progress", 0)),
            "collections": item.get("collections", []),
            "last_read": now - int(item.get("last_read_hours_ago", 1)) * 3600,
            "dl": int(item.get("dl", 0)),
        }
    for idx, email in enumerate(emails[1:], start=1):
        for pos, nid in enumerate(library_ids):
            if (pos + idx) % 2 == 0:
                user_data[email][nid] = {
                    "status": "finished" if pos % 3 == 0 else "reading",
                    "progress": max(1, (pos + 1) * 12),
                    "last_read": now - (idx * 5 + pos) * 3600,
                    "dl": 1 if pos % 2 == 0 else 0,
                }
    write_json(meta / "user_data.json", user_data)
    write_json(meta / "collections.json", collections)
    usernames = {primary: "dev_admin"}
    for i, email in enumerate(emails[1:], start=1):
        usernames[email] = f"dev_reader{i}"
    chat, next_id = [], 1
    messages = [
        (primary, "Локальная dev-библиотека заполнена."),
        (emails[1] if len(emails) > 1 else primary, "Проверяю читалку и прогресс."),
        (emails[2] if len(emails) > 2 else primary, "Проверяю коллекции и фильтры."),
    ]
    for email, text in messages:
        chat.append({"id": next_id, "email": email, "user": usernames[email], "ts": now - (10 - next_id) * 60, "text": text})
        next_id += 1
    write_json(meta / "community.json", {"usernames": usernames, "shares": [], "chat": chat, "next_id": next_id})


def main() -> int:
    parser = argparse.ArgumentParser(description="Reset and seed ArchiveDB local development data from EPUB fixtures.")
    parser.add_argument("source", nargs="?", help="ZIP, EPUB, or directory. Optional when Downloads.zip is in the repo/inbox.")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()
    env = {**os.environ, **parse_env(ROOT / ".env")}
    paths = ensure_local_safety(env)
    manifest_path = args.manifest if args.manifest.is_absolute() else ROOT / args.manifest
    manifest = load_manifest(manifest_path)
    source = find_source(args.source)
    print(f"[seed] Fixture source: {source}")
    with tempfile.TemporaryDirectory(prefix="arcdb-seed-") as td:
        files = extract_fixture_epubs(source, Path(td))
        if not files:
            raise RuntimeError("Fixture source contains no EPUB files.")
        print(f"[seed] Found {len(files)} EPUB files (non-EPUB files are ignored).")
        reports = [epub_metadata(p) for p in files]
        invalid_zip = [r["filename"] for r in reports if not r["valid_zip"]]
        if invalid_zip:
            raise RuntimeError(f"Invalid ZIP EPUB fixture(s): {invalid_zip}")
        primary_email = env.get("LOCAL_DEV_EMAIL", "dev@arcdb.local").strip().lower()
        users_path = paths["meta"] / "users.json"
        try:
            existing_users = json.loads(users_path.read_text(encoding="utf-8")) if users_path.exists() else {}
        except (OSError, json.JSONDecodeError):
            existing_users = {}
        primary_record = existing_users.get(primary_email) if isinstance(existing_users, dict) else None
        if not isinstance(primary_record, dict) or not primary_record.get("pwd_hash"):
            raise RuntimeError("Local dev login is not initialized. Run scripts/dev_bootstrap.py --setup-only first.")
        backup = None if args.no_backup else backup_small_state(paths)
        if backup:
            print(f"[seed] Backed up previous small state: {backup}")
        reset_local_state(paths)
        result = seed_library(files, manifest, paths)
        emails = seed_users(paths["meta"], manifest, env, primary_record)
        seed_user_state(paths["meta"], manifest, emails)
        report = {
            "schema_version": 1,
            "seeded_at": time.time(),
            "source": str(source),
            "epub_count": len(files),
            "epubs": reports,
            **result,
            "login": {"email": emails[0]},
        }
        write_json(paths["data_root"] / "dev-seed-report.json", report)
    print(f"[seed] Imported {len(result['imported'])} library assets.")
    print(f"[seed] Unused EPUBs retained as test cases in the report: {len(result['unused_epubs'])}")
    print(f"[seed] Report: {paths['data_root'] / 'dev-seed-report.json'}")
    print(f"[seed] Local login account: {emails[0]}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"\n[seed:error] {exc}", file=sys.stderr)
        raise SystemExit(1)
