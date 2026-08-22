from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TRACKED_APP = ROOT / "arcdb" / "app.py"
TRACKED_TEMPLATES = ROOT / "arcdb" / "templates"
BOOTSTRAP_PATH = ROOT / "scripts" / "dev_bootstrap.py"
WORKFLOW_DIR = ROOT / ".github" / "workflows"


def _load_bootstrap_module():
    spec = importlib.util.spec_from_file_location("arcdb_dev_bootstrap", BOOTSTRAP_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load dev bootstrap module.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TrackedRuntimeSourceTests(unittest.TestCase):
    def test_bootstrap_uses_tracked_source_without_materialization(self) -> None:
        bootstrap = _load_bootstrap_module()
        self.assertEqual(bootstrap.ensure_source(), TRACKED_APP)

        bootstrap_text = BOOTSTRAP_PATH.read_text(encoding="utf-8")
        self.assertNotIn("materialize_baseline", bootstrap_text)
        self.assertNotIn("runtime_overlays", bootstrap_text)
        self.assertNotIn(".runtime", bootstrap_text)

    def test_required_runtime_files_are_tracked(self) -> None:
        self.assertTrue(TRACKED_APP.is_file())
        expected_templates = {
            "community.html",
            "forgot_password.html",
            "gallery.html",
            "login.html",
            "reader.html",
            "register.html",
            "reset_password.html",
            "verify.html",
        }
        self.assertEqual(
            {path.name for path in TRACKED_TEMPLATES.glob("*.html")},
            expected_templates,
        )

    def test_runtime_workflows_do_not_launch_materialized_source(self) -> None:
        workflows = {
            path.name: path.read_text(encoding="utf-8")
            for path in WORKFLOW_DIR.glob("*.yml")
        }
        combined = "\n".join(workflows.values())
        self.assertNotIn(".runtime/source/gallery_app.py", combined)
        self.assertNotIn("tests/test_runtime_overlays.py", combined)
        self.assertIn("arcdb/app.py", workflows["read-backend-parity.yml"])
        self.assertIn("tests/test_tracked_runtime.py", combined)

    def test_runtime_state_routes_remain_wired(self) -> None:
        text = TRACKED_APP.read_text(encoding="utf-8")
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
            "from arcdb.storage.runtime_reads import read_users",
            "from arcdb.storage.runtime_reads import read_user_data",
            "from arcdb.storage.runtime_reads import read_collections",
            "from arcdb.storage.runtime_reads import read_user_uploads",
            "from arcdb.storage.runtime_reads import read_custom_meta",
            "from arcdb.storage.runtime_reads import read_allowed_emails",
            "mirror_allowed_emails(_get_allowed_emails_legacy()",
            "arcdb_legacy_write_succeeded",
        )
        for marker in expected:
            self.assertIn(marker, text)
        self.assertIn("STATE_DUAL_WRITE_STRICT", text)
        self.assertNotIn("save_user_data(all_udata)", text)

    def test_runtime_uses_bounded_epub_io_paths(self) -> None:
        text = TRACKED_APP.read_text(encoding="utf-8")
        self.assertIn("copy_upload_limited(storage.stream", text)
        self.assertIn("package_epub_streaming(", text)
        self.assertIn("_load_owned_epub_session(session_id)", text)
        self.assertIn("copy_zip_entry_atomic(", text)
        self.assertNotIn("all_entries =", text)
        self.assertNotIn("file_storage.save(", text)
        self.assertNotIn("out.write(z.read(", text)


if __name__ == "__main__":
    unittest.main()
