from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from arcdb.library_index import (
    LibraryIndex,
    LibraryIndexError,
    LibraryIndexUnavailable,
)


def item(
    novel_id: str,
    title: str,
    *,
    author: str = "Author",
    tags=(),
    chapters: int = 10,
    views: int = 0,
    likes: int = 0,
    complete: int = 0,
    language: str = "en",
    upload_date: str = "2026-01-01",
    **extra,
):
    return {
        "id": novel_id,
        "filename": f"{novel_id}-{title}.epub",
        "_library_key": f"id:{novel_id}",
        "_source_ids": [f"legacy-{novel_id}"],
        "title_en": title,
        "title_kr": extra.pop("title_kr", ""),
        "author": author,
        "tags": list(tags),
        "chapters": chapters,
        "views": views,
        "likes": likes,
        "complete": complete,
        "language": language,
        "upload_date": upload_date,
        "tg_link": "https://t.me/c/1/1",
        **extra,
    }


class LibraryIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.path = self.root / "library.sqlite3"
        self.index = LibraryIndex(self.path)
        self.items = [
            item(
                "1",
                "Alpha Chronicle",
                author="Alice",
                tags=("Fantasy", "Magic"),
                chapters=30,
                views=100,
                likes=20,
                complete=1,
                title_kr="알파",
            ),
            item(
                "2",
                "Beta Story",
                author="Bob",
                tags=("Fantasy", "Action"),
                chapters=20,
                views=300,
                likes=10,
                language="ko",
                upload_date="2026-02-01",
            ),
            item(
                "3",
                "Gamma Notes",
                author="Alice Cooper",
                tags=("Drama",),
                chapters=5,
                views=200,
                likes=30,
                upload_date="2024-01-01",
                is_updated=True,
            ),
        ]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def content_loader(value):
        if value["id"] != "1":
            return {}
        return {
            "chapters": ["Text/chapter1.xhtml", "MISSING||Text/chapter2.xhtml"],
            "titles": {"chapter1.xhtml": "Opening"},
            "images": ["Images/cover.jpg"],
        }

    def rebuild(self):
        return self.index.rebuild(self.items, content_loader=self.content_loader)

    @staticmethod
    def filters(**overrides):
        values = {
            "upload_source": "all",
            "search": "",
            "includes": set(),
            "excludes": set(),
            "reading_status": "all",
            "translated_chapter": "all",
            "audience": "all",
            "status": "all",
            "author": "",
            "language": "all",
            "min_chapters": 0,
            "max_chapters": 999999,
            "tag_match": "and",
            "collection": "all",
            "updated_after": "",
            "updated_before": "",
        }
        values.update(overrides)
        return values

    def query(self, filters=None, **overrides):
        values = {
            "filters": filters or self.filters(),
            "user_data": {},
            "sort_by": "views",
            "sort_order": "desc",
            "page": 1,
            "limit": 30,
        }
        values.update(overrides)
        return self.index.query(**values)

    def test_rebuild_lookup_reader_entries_and_integrity(self) -> None:
        report = self.rebuild()
        self.assertEqual(report["items"], 3)
        self.assertEqual(report["chapters"], 2)
        self.assertEqual(report["images"], 1)
        self.assertEqual(self.index.verify()["items"], 3)
        self.assertEqual(self.index.lookup("legacy-1")["title_en"], "Alpha Chronicle")
        self.assertEqual(self.index.lookup("1-Alpha Chronicle.epub")["id"], "1")
        chapters, titles = self.index.chapters("1")
        self.assertEqual(chapters[0], "Text/chapter1.xhtml")
        self.assertEqual(titles, {"chapter1.xhtml": "Opening"})
        self.assertEqual(self.index.images("id:1"), ["Images/cover.jpg"])

    def test_search_filter_sort_and_pagination_are_database_backed(self) -> None:
        self.rebuild()
        result = self.query(filters=self.filters(search="pha Chron"))
        self.assertEqual([value["id"] for value in result["items"]], ["1"])
        result = self.query(filters=self.filters(search='Alpha "Chronicle"'))
        self.assertEqual(result["items"], [])
        result = self.query(filters=self.filters(search="1"))
        self.assertEqual([value["id"] for value in result["items"]], ["1"])

        result = self.query(
            filters=self.filters(includes={"fantasy", "magic"}, tag_match="and")
        )
        self.assertEqual([value["id"] for value in result["items"]], ["1"])
        result = self.query(
            filters=self.filters(includes={"action", "drama"}, tag_match="or")
        )
        self.assertEqual({value["id"] for value in result["items"]}, {"2", "3"})
        result = self.query(filters=self.filters(excludes={"fantasy"}))
        self.assertEqual([value["id"] for value in result["items"]], ["3"])

        result = self.query(
            filters=self.filters(author="alice", min_chapters=10, status="complete")
        )
        self.assertEqual([value["id"] for value in result["items"]], ["1"])
        result = self.query(filters=self.filters(language="ko"))
        self.assertEqual([value["id"] for value in result["items"]], ["2"])
        result = self.query(filters=self.filters(updated_after="2026-01-15"))
        self.assertEqual([value["id"] for value in result["items"]], ["2"])

        first = self.query(sort_by="likes", page=1, limit=1)
        second = self.query(sort_by="likes", page=2, limit=1)
        self.assertEqual(first["total"], 3)
        self.assertEqual(first["total_pages"], 3)
        self.assertEqual(first["items"][0]["id"], "3")
        self.assertEqual(second["items"][0]["id"], "1")

    def test_user_status_collection_last_read_and_random(self) -> None:
        self.rebuild()
        user_data = {
            "1": {"status": "reading", "collections": ["c"], "last_read": 10},
            "2": {"status": "finished", "collections": [], "last_read": 20},
        }
        result = self.query(
            filters=self.filters(reading_status="any"),
            user_data=user_data,
            sort_by="last_read",
        )
        self.assertEqual([value["id"] for value in result["items"]], ["2", "1"])
        result = self.query(
            filters=self.filters(collection="c"), user_data=user_data
        )
        self.assertEqual([value["id"] for value in result["items"]], ["1"])
        result = self.query(
            filters=self.filters(collection="none"), user_data=user_data
        )
        self.assertEqual({value["id"] for value in result["items"]}, {"2", "3"})
        random_result = self.query(random_one=True)
        self.assertIn(random_result["random"]["id"], {"1", "2", "3"})

    def test_incremental_upsert_preserves_content_and_delete_removes_aliases(self) -> None:
        self.rebuild()
        changed = dict(self.items[0], author="Updated Author", tags=["New"])
        self.index.upsert(changed)
        self.assertEqual(self.index.lookup("1")["author"], "Updated Author")
        self.assertEqual(self.index.chapters("1")[0], [
            "Text/chapter1.xhtml",
            "MISSING||Text/chapter2.xhtml",
        ])
        self.assertEqual(self.index.tag_counts(), {
            "Action": 1,
            "Drama": 1,
            "Fantasy": 1,
            "New": 1,
        })
        self.assertTrue(self.index.delete_alias("legacy-1"))
        self.assertIsNone(self.index.lookup("1"))
        self.assertEqual(self.index.verify()["items"], 2)

    def test_failed_candidate_does_not_replace_active_index(self) -> None:
        self.rebuild()

        def fail_on_second(value):
            if value["id"] == "2":
                raise RuntimeError("fixture failure")
            return {}

        with self.assertRaisesRegex(RuntimeError, "fixture failure"):
            self.index.rebuild(self.items, content_loader=fail_on_second)
        self.assertEqual(self.index.verify()["items"], 3)
        self.assertEqual(list(self.root.glob(".*.candidate.*")), [])

    def test_duplicate_identity_or_alias_fails_closed(self) -> None:
        self.rebuild()
        duplicate_identity = item("1", "Conflicting title")
        with self.assertRaisesRegex(LibraryIndexError, "Duplicate stable"):
            self.index.rebuild([*self.items, duplicate_identity])

        duplicate_alias = item("4", "Different title")
        duplicate_alias["filename"] = self.items[0]["filename"]
        with self.assertRaisesRegex(LibraryIndexError, "Duplicate library alias"):
            self.index.rebuild([*self.items, duplicate_alias])

        self.assertEqual(self.index.verify()["items"], 3)
        self.assertEqual(list(self.root.glob(".*.candidate.*")), [])

    def test_missing_index_fails_closed(self) -> None:
        with self.assertRaises(LibraryIndexUnavailable):
            self.index.all_items()


if __name__ == "__main__":
    unittest.main()
