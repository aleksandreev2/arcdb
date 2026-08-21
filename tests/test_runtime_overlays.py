from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GALLERY_APP = ROOT / ".runtime" / "source" / "gallery_app.py"


class RuntimeOverlayTests(unittest.TestCase):
    def test_runtime_state_routes_are_shadow_wired(self) -> None:
        text = GALLERY_APP.read_text(encoding="utf-8")
        expected = (
            'shadow_reason="user_status"',
            'shadow_reason="bulk_remove"',
            'shadow_reason="user_progress"',
            'shadow_reason="user_hide"',
            'shadow_reason="local_download"',
            'shadow_reason="telegram_download"',
            'shadow_reason="collection_create"',
            'shadow_reason="collection_rename"',
            'shadow_reason="collection_delete"',
            'shadow_reason="collection_delete_memberships"',
            'shadow_reason="collection_assign"',
            '"community_import_collection"',
            'shadow_reason="community_import_memberships"',
            'shadow_reason="upload_create"',
            'shadow_reason="upload_approve"',
            'shadow_reason="upload_reject"',
            'shadow_reason="auth_register"',
            'shadow_reason="auth_verify"',
            'shadow_reason="auth_reset_request"',
            'shadow_reason="auth_password_reset"',
            '"allowlist_add"',
            '"allowlist_remove"',
            "from arcdb.storage.runtime_state import mirror_user_changes",
            "from arcdb.storage.runtime_state import mirror_collection_user",
            "from arcdb.storage.runtime_state import mirror_upload_changes",
            "from arcdb.storage.runtime_state import mirror_custom_metadata_entry",
            "from arcdb.storage.runtime_state import mirror_allowed_emails",
            "from arcdb.storage.runtime_state import mirror_auth_users_changes",
            "ARCHIVEDB_AUTH_TEST_MODE",
            "arcdb_legacy_write_succeeded",
        )
        for marker in expected:
            self.assertIn(marker, text)
        self.assertIn("STATE_DUAL_WRITE_STRICT", text)
        self.assertNotIn("save_user_data(all_udata)", text)


if __name__ == "__main__":
    unittest.main()
