from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arcdb.production_inventory import (
    InventoryError,
    reconcile_production_inventory,
    write_new_json,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Compare a private production inventory with an explicit materialized "
            "ArchiveDB source root and emit private plus path-free reports."
        )
    )
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--reference-root", required=True)
    parser.add_argument("--private-report", required=True)
    parser.add_argument("--report", required=True)
    args = parser.parse_args()

    inventory_path = Path(args.inventory).expanduser().resolve()
    private_path = Path(args.private_report).expanduser().resolve()
    public_path = Path(args.report).expanduser().resolve()
    if private_path == public_path:
        raise InventoryError("report_paths_equal", "Private and public reports must differ.")
    if private_path.exists() or public_path.exists():
        raise InventoryError("report_exists", "Refusing to overwrite an existing report.")
    try:
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InventoryError(
            "inventory_unreadable", "Private inventory could not be read as JSON."
        ) from exc

    private, public = reconcile_production_inventory(
        inventory, Path(args.reference_root)
    )
    write_new_json(private_path, private, private=True)
    try:
        write_new_json(public_path, public, private=False)
    except Exception:
        private_path.unlink(missing_ok=True)
        raise
    print(
        "Production reconciliation: COMPLETE. Review the private diff; no report "
        "authorizes readiness, canary, or primary reads."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except InventoryError as exc:
        print(f"Production reconciliation: FAILED [{exc.code}]: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except Exception as exc:
        print(
            "Production reconciliation: FAILED [internal_error]: "
            f"unexpected {type(exc).__name__}.",
            file=sys.stderr,
        )
        raise SystemExit(1)
