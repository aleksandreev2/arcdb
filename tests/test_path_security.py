from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from arcdb.path_security import confined_child, confined_path


class ConfinedPathTests(unittest.TestCase):
    def test_accepts_descendants_and_optionally_the_root(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "uploads"
            child = root / "upload_1" / "book.epub"
            child.parent.mkdir(parents=True)
            child.write_bytes(b"fixture")
            self.assertEqual(confined_path(child, root), str(child.resolve()))
            self.assertIsNone(confined_path(root, root))
            self.assertEqual(
                confined_path(root, root, allow_root=True), str(root.resolve())
            )

    def test_rejects_sibling_prefix_traversal_absolute_child_and_empty_values(self) -> None:
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "structured_output"
            root.mkdir()
            sibling = base / "structured_output_evil" / "book.epub"
            sibling.parent.mkdir()
            sibling.write_bytes(b"fixture")
            self.assertIsNone(confined_path(sibling, root))
            self.assertIsNone(confined_child(root, "../outside.epub"))
            self.assertIsNone(confined_child(root, sibling))
            self.assertIsNone(confined_path("", root))
            self.assertIsNone(confined_path(sibling, ""))
            self.assertIsNone(confined_path(42, root))
            self.assertIsNone(confined_child(root, 42))

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_rejects_symlink_escape_when_supported(self) -> None:
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "uploads"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            secret = outside / "secret.epub"
            secret.write_bytes(b"fixture")
            link = root / "escape"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("the current account cannot create symlinks")
            self.assertIsNone(confined_path(link / secret.name, root))


if __name__ == "__main__":
    unittest.main()
