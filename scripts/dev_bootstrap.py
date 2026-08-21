from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import venv
import webbrowser
from pathlib import Path

from materialize_baseline import materialize

ROOT = Path(__file__).resolve().parents[1]
VENV_DIR = ROOT / ".venv"
ENV_FILE = ROOT / ".env"
ENV_TEMPLATE = ROOT / ".env.local.example"
REQUIREMENTS = ROOT / "requirements.txt"
STAMP = VENV_DIR / ".requirements.sha256"


def venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def ensure_env() -> dict[str, str]:
    if not ENV_FILE.exists():
        if not ENV_TEMPLATE.exists():
            raise RuntimeError(f"Missing {ENV_TEMPLATE.name}")
        shutil.copy2(ENV_TEMPLATE, ENV_FILE)
        print("[setup] Created .env from .env.local.example")

    text = ENV_FILE.read_text(encoding="utf-8")
    if "FLASK_SECRET_KEY=__AUTO_GENERATE__" in text:
        text = text.replace(
            "FLASK_SECRET_KEY=__AUTO_GENERATE__",
            f"FLASK_SECRET_KEY={secrets.token_hex(32)}",
            1,
        )
        ENV_FILE.write_text(text, encoding="utf-8")
        print("[setup] Generated local FLASK_SECRET_KEY")

    return parse_env(ENV_FILE)


def ensure_venv() -> Path:
    py = venv_python()
    if py.exists():
        return py
    print(f"[setup] Creating virtual environment: {VENV_DIR}")
    venv.EnvBuilder(with_pip=True, clear=False).create(VENV_DIR)
    if not py.exists():
        raise RuntimeError("Virtual environment was created but Python executable is missing.")
    return py


def requirements_digest() -> str:
    return hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()


def ensure_dependencies(py: Path) -> None:
    digest = requirements_digest()
    if STAMP.exists() and STAMP.read_text(encoding="utf-8").strip() == digest:
        print("[setup] Dependencies are already up to date.")
        return

    print("[setup] Installing/updating Python dependencies...")
    subprocess.run(
        [str(py), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(REQUIREMENTS)],
        cwd=ROOT,
        check=True,
    )
    STAMP.write_text(digest + "\n", encoding="utf-8")
    print("[setup] Dependencies are ready.")


def ensure_local_directories(env: dict[str, str]) -> None:
    dir_keys = (
        "LOCAL_OUTPUT_DIR",
        "STRUCTURED_OUTPUT_DIR",
        "BATCHED_EPUBS_DIR",
        "META_DIR",
        "EPUB_PACKAGE_SESSIONS_DIR",
    )
    for key in dir_keys:
        raw = env.get(key)
        if not raw:
            continue
        path = Path(raw)
        if not path.is_absolute():
            path = ROOT / path
        path.mkdir(parents=True, exist_ok=True)

    (ROOT / "data" / "telegram").mkdir(parents=True, exist_ok=True)


def seed_dev_account(py: Path, child_env: dict[str, str]) -> None:
    if child_env.get("ARCHIVEDB_LOCAL_DEV") != "1":
        return
    subprocess.run(
        [str(py), str(ROOT / "scripts" / "dev_seed.py")],
        cwd=ROOT,
        env=child_env,
        check=True,
    )


def _has_any_epub_or_html(path: Path) -> bool:
    if not path.exists():
        return False
    if path.is_file():
        return path.suffix.lower() in {".epub", ".html", ".htm", ".xhtml"}
    for candidate in path.rglob("*"):
        if candidate.is_file() and candidate.suffix.lower() in {".epub", ".html", ".htm", ".xhtml"}:
            return True
    return False


def local_library_is_empty(env: dict[str, str]) -> bool:
    structured_raw = env.get("STRUCTURED_OUTPUT_DIR", "./data/structured_output")
    batched_raw = env.get("BATCHED_EPUBS_DIR", "./data/batched_epubs")
    structured = Path(structured_raw)
    batched = Path(batched_raw)
    if not structured.is_absolute():
        structured = ROOT / structured
    if not batched.is_absolute():
        batched = ROOT / batched
    return not (_has_any_epub_or_html(structured) or _has_any_epub_or_html(batched))


def fixture_source_available() -> bool:
    inbox = ROOT / "dev-fixtures" / "inbox"
    if (inbox / "Downloads.zip").is_file():
        return True
    if not inbox.is_dir():
        return False
    return any(p.is_file() and p.suffix.lower() == ".epub" for p in inbox.iterdir())


def auto_seed_library_if_empty(py: Path, child_env: dict[str, str]) -> None:
    if child_env.get("ARCHIVEDB_LOCAL_DEV") != "1":
        return
    if not local_library_is_empty(child_env):
        print("[setup] Local library already contains data; automatic seed skipped.")
        return
    if not fixture_source_available():
        print("[setup] Local library is empty and no dev fixtures were found; automatic seed skipped.")
        return

    print("[setup] Local library is empty; seeding from dev-fixtures/inbox...")
    subprocess.run(
        [str(py), str(ROOT / "scripts" / "dev_seed_library.py")],
        cwd=ROOT,
        env=child_env,
        check=True,
    )
    print("[setup] Local library seed completed.")


def browser_url(env: dict[str, str]) -> str:
    host = env.get("HOST", "127.0.0.1")
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    port = int(env.get("PORT", "5004"))
    return f"http://{host}:{port}/login"


def wait_for_server_and_open(url: str, proc: subprocess.Popen, env: dict[str, str]) -> None:
    if os.environ.get("CI") or not url.startswith("http://"):
        return
    if env.get("ARCHIVEDB_OPEN_BROWSER", "1") != "1":
        return
    host_port = url.removeprefix("http://").split("/", 1)[0]
    host, port_text = host_port.rsplit(":", 1)
    port = int(port_text)
    for _ in range(80):
        if proc.poll() is not None:
            return
        try:
            with socket.create_connection((host, port), timeout=0.2):
                webbrowser.open(url)
                return
        except OSError:
            time.sleep(0.25)


def ensure_source() -> Path:
    source = materialize(ROOT / ".runtime" / "source")
    entrypoint = source / "gallery_app.py"
    if not entrypoint.exists():
        raise RuntimeError(f"Baseline entrypoint is missing: {entrypoint}")
    return entrypoint


def run_server(py: Path, child_env: dict[str, str], entrypoint: Path) -> int:
    url = browser_url(child_env)
    print("\n[ArchiveDB] Starting local server")
    print(f"[ArchiveDB] URL: {url}")
    if child_env.get("ARCHIVEDB_LOCAL_DEV") == "1":
        print(
            "[ArchiveDB] Login: "
            f"{child_env.get('LOCAL_DEV_EMAIL', '')} / {child_env.get('LOCAL_DEV_PASSWORD', '')}"
        )
    print("[ArchiveDB] Press Ctrl+C to stop.\n")

    proc = subprocess.Popen([str(py), str(entrypoint)], cwd=ROOT, env=child_env)
    try:
        wait_for_server_and_open(url, proc, child_env)
        return proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        try:
            return proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            return proc.wait()


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare or run the ArchiveDB local development environment.")
    parser.add_argument(
        "--setup-only",
        action="store_true",
        help="Create .env/.venv, install dependencies and seed the local login without starting Flask.",
    )
    parser.add_argument(
        "--auto-seed-if-empty",
        action="store_true",
        help="In local dev only, seed the library once when it is empty and dev fixtures are available.",
    )
    args = parser.parse_args()

    os.chdir(ROOT)
    env_values = ensure_env()
    entrypoint = ensure_source()
    py = ensure_venv()
    ensure_dependencies(py)

    child_env = os.environ.copy()
    child_env.update(env_values)
    ensure_local_directories(child_env)
    if args.auto_seed_if_empty:
        auto_seed_library_if_empty(py, child_env)
    seed_dev_account(py, child_env)

    if args.setup_only:
        print("[setup] Local development environment is ready.")
        return 0
    return run_server(py, child_env, entrypoint)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        print(f"\n[error] Command failed with exit code {exc.returncode}.", file=sys.stderr)
        raise SystemExit(exc.returncode)
    except Exception as exc:
        print(f"\n[error] {exc}", file=sys.stderr)
        raise SystemExit(1)
