from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GALLERY_APP = ROOT / ".runtime" / "source" / "gallery_app.py"


class RuntimeOverlayTests(unittest.TestCase):
    def test_user_state_routes_are_shadow_wired(self) -> None:
        text = GALLERY_APP.read_text(encoding="utf-8")
        expected = (
            'shadow_reason="user_status"',
            'shadow_reason="bulk_remove"',
            'shadow_reason="user_progress"',
            'shadow_reason="user_hide"',
            'shadow_reason="local_download"',
            'shadow_reason="telegram_download"',
            "from arcdb.storage.runtime_state import mirror_user_changes",
        )
        for marker in expected:
            self.assertIn(marker, text)
        self.assertIn("STATE_DUAL_WRITE_STRICT", text)


if __name__ == "__main__":
    unittest.main()
