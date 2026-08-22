from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arcdb.production_inventory import (
    ALLOWED_CONTENT_LABELS,
    ALLOWED_METADATA_LABELS,
    InventoryError,
    collect_production_inventory,
    resolve_inventory_paths,
    write_new_json,
)


def _content_root(value: str) -> tuple[str, Path]:
    label, separator, raw_path = value.partition("=")
    if not separator or not label.strip() or not raw_path.strip():
        raise argparse.ArgumentTypeError("Expected LABEL=/explicit/path.")
    return label, Path(raw_path)


def _metadata_file(value: str) -> tuple[str, Path]:
    label, separator, raw_path = value.partition("=")
    if not separator or not label.strip() or not raw_path.strip():
        raise argparse.ArgumentTypeError("Expected LABEL=/explicit/path.")
    return label, Path(raw_path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Collect a read-only private ArchiveDB production inventory and a "
            "separate path-free report. Explicit paths must come from host discovery."
        )
    )
    parser.add_argument("--app-root", required=True)
    parser.add_argument("--meta-dir", required=True)
    parser.add_argument("--sqlite-db", required=True)
    parser.add_argument(
        "--metadata-file",
        action="append",
        default=[],
        type=_metadata_file,
        metavar="LABEL=PATH",
        help=(
            "Repeat for configured metadata outside --meta-dir. Approved labels: "
            + ", ".join(sorted(ALLOWED_METADATA_LABELS))
            + "."
        ),
    )
    parser.add_argument(
        "--content-root",
        action="append",
        default=[],
        type=_content_root,
        metavar="LABEL=PATH",
        help=(
            "Repeat for storage roots. Approved labels: "
            + ", ".join(sorted(ALLOWED_CONTENT_LABELS))
            + "."
        ),
    )
    parser.add_argument(
        "--systemd-unit", action="append", default=[], help="Explicit unit file path"
    )
    parser.add_argument("--cloudflared-config")
    parser.add_argument("--mountinfo", default="/proc/self/mountinfo")
    parser.add_argument("--source-revision")
    parser.add_argument("--private-report", required=True)
    parser.add_argument("--report", required=True)
    args = parser.parse_args()

    private_path = Path(args.private_report).expanduser().resolve()
    public_path = Path(args.report).expanduser().resolve()
    if private_path == public_path:
        raise InventoryError("report_paths_equal", "Private and public reports must differ.")
    if private_path.exists() or public_path.exists():
        raise InventoryError("report_exists", "Refusing to overwrite an existing report.")

    paths = resolve_inventory_paths(
        app_root=Path(args.app_root),
        meta_dir=Path(args.meta_dir),
        sqlite_db=Path(args.sqlite_db),
        metadata_files=args.metadata_file,
        content_roots=args.content_root,
        systemd_units=(Path(value) for value in args.systemd_unit),
        cloudflared_config=(
            None if not args.cloudflared_config else Path(args.cloudflared_config)
        ),
        mountinfo_path=Path(args.mountinfo),
    )
    private, public = collect_production_inventory(
        paths, source_revision=args.source_revision
    )
    write_new_json(private_path, private, private=True)
    try:
        write_new_json(public_path, public, private=False)
    except Exception:
        private_path.unlink(missing_ok=True)
        raise
    print(
        "Production inventory: COLLECTED. Keep the private report private; "
        "the path-free report does not authorize migration or read cutover."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except InventoryError as exc:
        print(f"Production inventory: FAILED [{exc.code}]: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except Exception as exc:
        print(
            "Production inventory: FAILED [internal_error]: "
            f"unexpected {type(exc).__name__}.",
            file=sys.stderr,
        )
        raise SystemExit(1)
