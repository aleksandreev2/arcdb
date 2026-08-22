from __future__ import annotations

from io import BytesIO
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock
import warnings
import zipfile

from arcdb.epub_io import (
    EPUB_MIMETYPE,
    EpubLimits,
    EpubSafetyError,
    copy_upload_limited,
    copy_zip_entry_atomic,
    extract_epub_safely,
    iter_epub_text_entries,
    package_epub_streaming,
    validate_epub_archive,
)


CONTAINER_XML = b'''<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>'''
OPF_XML = b'''<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <metadata/><manifest><item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/></manifest>
  <spine><itemref idref="chapter"/></spine>
</package>'''


def make_epub(
    path: Path,
    *,
    chapter_name: str = "OEBPS/chapter.xhtml",
    chapter: bytes = b"<html><body>normal chapter</body></html>",
    extras: list[tuple[str | zipfile.ZipInfo, bytes, int | None]] | None = None,
    include_mimetype: bool = True,
    container: bytes = CONTAINER_XML,
    opf: bytes = OPF_XML,
) -> None:
    with zipfile.ZipFile(path, "w", allowZip64=True) as archive:
        if include_mimetype:
            archive.writestr(
                "mimetype", EPUB_MIMETYPE, compress_type=zipfile.ZIP_STORED
            )
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", opf)
        archive.writestr(chapter_name, chapter)
        for name, content, compression in extras or []:
            kwargs = {} if compression is None else {"compress_type": compression}
            archive.writestr(name, content, **kwargs)


def limits(**overrides: int | float) -> EpubLimits:
    values: dict[str, int | float] = {
        "max_entries": 100,
        "max_entry_bytes": 4 * 1024 * 1024,
        "max_total_uncompressed_bytes": 8 * 1024 * 1024,
        "max_compression_ratio": 250,
        "compression_ratio_min_bytes": 1024,
        "max_text_entry_bytes": 1024 * 1024,
    }
    values.update(overrides)
    return EpubLimits(**values)  # type: ignore[arg-type]


class UploadCopyTests(unittest.TestCase):
    def test_streams_upload_and_syncs_once_before_atomic_publish(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "upload.epub"
            payload = b"a" * (2 * 1024 * 1024 + 17)
            with mock.patch("arcdb.epub_io.os.fsync") as fsync:
                copied = copy_upload_limited(
                    BytesIO(payload), destination, len(payload) + 1
                )
            self.assertEqual(copied, len(payload))
            self.assertEqual(destination.read_bytes(), payload)
            fsync.assert_called_once()
            self.assertEqual(list(destination.parent.glob("upload.epub.tmp.*")), [])

    def test_oversized_upload_preserves_existing_destination_and_cleans_partial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "upload.epub"
            destination.write_bytes(b"existing")
            with self.assertRaisesRegex(EpubSafetyError, "exceeds"):
                copy_upload_limited(BytesIO(b"new payload"), destination, 3)
            self.assertEqual(destination.read_bytes(), b"existing")
            self.assertEqual(list(destination.parent.glob("upload.epub.tmp.*")), [])

    def test_zip_member_copy_is_streamed_bounded_and_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive_path = root / "assets.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("asset.bin", b"asset payload")
            destination = root / "asset.bin"
            with zipfile.ZipFile(archive_path, "r") as archive:
                info = archive.getinfo("asset.bin")
                with mock.patch.object(
                    archive,
                    "read",
                    side_effect=AssertionError("ZipFile.read must not be used"),
                ):
                    copied = copy_zip_entry_atomic(
                        archive, info, destination, max_bytes=1024
                    )
            self.assertEqual(copied, len(b"asset payload"))
            self.assertEqual(destination.read_bytes(), b"asset payload")

            destination.write_bytes(b"existing")
            with zipfile.ZipFile(archive_path, "r") as archive:
                with self.assertRaisesRegex(EpubSafetyError, "bounded"):
                    copy_zip_entry_atomic(
                        archive,
                        archive.getinfo("asset.bin"),
                        destination,
                        max_bytes=3,
                    )
            self.assertEqual(destination.read_bytes(), b"existing")


class EpubValidationTests(unittest.TestCase):
    def test_normal_unicode_epub_validates_and_extracts_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            epub = root / "unicode.epub"
            chapter_name = "OEBPS/главы/章.xhtml"
            make_epub(epub, chapter_name=chapter_name)

            summary = validate_epub_archive(epub, limits())
            self.assertEqual(summary.html_entry_count, 1)
            self.assertEqual(summary.opf_path, "OEBPS/content.opf")

            destination = root / "extracted"
            extract_epub_safely(epub, destination, limits())
            self.assertTrue((destination / Path(*chapter_name.split("/"))).is_file())
            self.assertEqual(list(root.glob("extracted.tmp.*")), [])

    def test_malformed_and_non_epub_archives_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            malformed = root / "malformed.epub"
            malformed.write_bytes(b"not a zip")
            with self.assertRaisesRegex(EpubSafetyError, "valid ZIP"):
                validate_epub_archive(malformed, limits())

            missing_mimetype = root / "missing-mimetype.epub"
            make_epub(missing_mimetype, include_mimetype=False)
            with self.assertRaisesRegex(EpubSafetyError, "mimetype"):
                validate_epub_archive(missing_mimetype, limits())

            broken_container = root / "broken-container.epub"
            make_epub(broken_container, container=b"<container>")
            with self.assertRaisesRegex(EpubSafetyError, "container metadata"):
                validate_epub_archive(broken_container, limits())

            entity_container = root / "entity-container.epub"
            make_epub(
                entity_container,
                container=b'<!DOCTYPE x [<!ENTITY x "boom">]><container>&x;</container>',
            )
            with self.assertRaisesRegex(EpubSafetyError, "prohibited XML"):
                validate_epub_archive(entity_container, limits())

            corrupt_crc = root / "corrupt-crc.epub"
            make_epub(
                corrupt_crc,
                extras=[("OEBPS/corrupt.bin", b"CORRUPTME", zipfile.ZIP_STORED)],
            )
            damaged = bytearray(corrupt_crc.read_bytes())
            payload_offset = damaged.index(b"CORRUPTME")
            damaged[payload_offset] ^= 0xFF
            corrupt_crc.write_bytes(damaged)
            with self.assertRaisesRegex(EpubSafetyError, "damaged or invalid"):
                validate_epub_archive(corrupt_crc, limits())

    def test_traversal_absolute_symlink_and_special_paths_are_rejected(self) -> None:
        unsafe_names = (
            "../outside.xhtml",
            "/absolute.xhtml",
            "C:/drive.xhtml",
            "OEBPS/../../escape.xhtml",
            "OEBPS/con.xhtml",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index, unsafe_name in enumerate(unsafe_names):
                epub = root / f"unsafe-{index}.epub"
                make_epub(epub, extras=[(unsafe_name, b"unsafe", None)])
                with self.assertRaises(EpubSafetyError, msg=unsafe_name):
                    validate_epub_archive(epub, limits())

            symlink = root / "symlink.epub"
            link_info = zipfile.ZipInfo("OEBPS/link")
            link_info.create_system = 3
            link_info.external_attr = (stat.S_IFLNK | 0o777) << 16
            make_epub(symlink, extras=[(link_info, b"../../outside", None)])
            with self.assertRaisesRegex(EpubSafetyError, "symbolic link"):
                validate_epub_archive(symlink, limits())

    def test_duplicate_unicode_case_and_file_directory_collisions_are_rejected(self) -> None:
        cases = (
            [
                ("OEBPS/Images/Cover.png", b"one", None),
                ("OEBPS/images/cover.png", b"two", None),
            ],
            [
                ("OEBPS/caf\u00e9.png", b"one", None),
                ("OEBPS/cafe\u0301.png", b"two", None),
            ],
            [
                ("OEBPS/assets", b"file", None),
                ("OEBPS/assets/image.png", b"child", None),
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index, extras in enumerate(cases):
                epub = root / f"collision-{index}.epub"
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    make_epub(epub, extras=extras)
                with self.assertRaisesRegex(EpubSafetyError, "colli"):
                    validate_epub_archive(epub, limits())

    def test_entry_count_size_total_and_compression_ratio_limits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            too_many = root / "too-many.epub"
            make_epub(too_many, extras=[("extra.bin", b"x", None)])
            with self.assertRaisesRegex(EpubSafetyError, "more than 4"):
                validate_epub_archive(too_many, limits(max_entries=4))

            oversized = root / "oversized.epub"
            make_epub(oversized, extras=[("large.bin", b"x" * 4096, None)])
            with self.assertRaisesRegex(EpubSafetyError, "oversized"):
                validate_epub_archive(
                    oversized,
                    limits(max_entry_bytes=2048, max_text_entry_bytes=1024),
                )

            total = root / "total.epub"
            make_epub(
                total,
                extras=[("one.bin", b"x" * 2048, None), ("two.bin", b"y" * 2048, None)],
            )
            with self.assertRaisesRegex(EpubSafetyError, "too large"):
                validate_epub_archive(
                    total,
                    limits(max_total_uncompressed_bytes=3000),
                )

            bomb = root / "bomb.epub"
            make_epub(
                bomb,
                extras=[("bomb.bin", b"0" * (2 * 1024 * 1024), zipfile.ZIP_DEFLATED)],
            )
            with self.assertRaisesRegex(EpubSafetyError, "compression ratio"):
                validate_epub_archive(
                    bomb,
                    limits(max_compression_ratio=10, compression_ratio_min_bytes=1024),
                )

    def test_text_iteration_is_entry_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            epub = Path(tmp) / "large-text.epub"
            make_epub(epub, chapter=b"x" * 4096)
            with self.assertRaisesRegex(EpubSafetyError, "text/metadata"):
                list(
                    iter_epub_text_entries(
                        epub,
                        limits(max_text_entry_bytes=2048),
                    )
                )


class EpubPackagingTests(unittest.TestCase):
    def test_package_without_rewrites_preserves_original_entry_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "base.epub"
            output = root / "final.epub"
            chapter = b"\xff\xfelegacy-non-utf8-chapter"
            make_epub(base, chapter=chapter)
            package_epub_streaming(
                base,
                output,
                injected_files={},
                url_map={},
                limits=limits(),
                max_output_bytes=4 * 1024 * 1024,
            )
            with zipfile.ZipFile(base, "r") as source, zipfile.ZipFile(output, "r") as result:
                self.assertEqual(
                    result.read("OEBPS/chapter.xhtml"),
                    source.read("OEBPS/chapter.xhtml"),
                )
                self.assertEqual(
                    result.read("OEBPS/content.opf"),
                    source.read("OEBPS/content.opf"),
                )

    def test_packages_entry_by_entry_rewrites_urls_and_streams_binary_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "base.epub"
            output = root / "final.epub"
            remote_url = "https://images.example.test/cover.jpg"
            chapter = f'<html><body><img src="{remote_url}"></body></html>'.encode()
            binary = os.urandom(2 * 1024 * 1024)
            make_epub(
                base,
                chapter=chapter,
                extras=[("OEBPS/blob.bin", binary, zipfile.ZIP_STORED)],
            )
            image = root / "image.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")

            with mock.patch.object(
                zipfile.ZipFile,
                "read",
                side_effect=AssertionError("ZipFile.read must not materialize entries"),
            ):
                package_epub_streaming(
                    base,
                    output,
                    injected_files={"OEBPS/Images/img_0001.png": image},
                    url_map={remote_url: "img_0001.png"},
                    limits=limits(),
                    max_output_bytes=8 * 1024 * 1024,
                )

            validate_epub_archive(output, limits())
            with zipfile.ZipFile(output, "r") as archive:
                self.assertEqual(archive.read("OEBPS/blob.bin"), binary)
                rewritten = archive.read("OEBPS/chapter.xhtml").decode()
                self.assertNotIn(remote_url, rewritten)
                self.assertIn("Images/img_0001.png", rewritten)
                self.assertEqual(
                    archive.read("OEBPS/Images/img_0001.png"), image.read_bytes()
                )
                self.assertEqual(archive.getinfo("mimetype").compress_type, zipfile.ZIP_STORED)
                self.assertIn("img_0001.png", archive.read("OEBPS/content.opf").decode())

    def test_failed_package_keeps_previous_output_and_removes_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "base.epub"
            output = root / "final.epub"
            make_epub(base)
            output.write_bytes(b"previous-good-output")
            with self.assertRaisesRegex(EpubSafetyError, "output limit"):
                package_epub_streaming(
                    base,
                    output,
                    injected_files={},
                    url_map={},
                    limits=limits(),
                    max_output_bytes=1,
                )
            self.assertEqual(output.read_bytes(), b"previous-good-output")
            self.assertEqual(list(root.glob("final.epub.tmp.*")), [])


if __name__ == "__main__":
    unittest.main()
