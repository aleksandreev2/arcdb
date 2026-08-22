"""Persistent, rebuildable SQLite index for library and reader discovery."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid


LIBRARY_INDEX_SCHEMA_VERSION = 1
LEGACY_UPLOAD_DATE = "2024-01-01"


class LibraryIndexError(RuntimeError):
    """Base class for sanitized library-index failures."""


class LibraryIndexUnavailable(LibraryIndexError):
    """The active index is missing, invalid, or incompatible."""


SCHEMA_SQL = """
CREATE TABLE index_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE library_items (
    stable_id TEXT PRIMARY KEY,
    novel_key TEXT NOT NULL,
    library_key TEXT,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    original_title TEXT NOT NULL,
    author TEXT NOT NULL,
    language TEXT NOT NULL,
    cover TEXT NOT NULL,
    publication_status INTEGER NOT NULL,
    adult INTEGER NOT NULL,
    chapters INTEGER NOT NULL,
    views INTEGER NOT NULL,
    likes INTEGER NOT NULL,
    uploaded INTEGER NOT NULL,
    updated INTEGER NOT NULL,
    translated INTEGER NOT NULL,
    upload_date TEXT NOT NULL,
    updated_sort TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE library_aliases (
    alias TEXT PRIMARY KEY,
    stable_id TEXT NOT NULL REFERENCES library_items(stable_id) ON DELETE CASCADE,
    kind TEXT NOT NULL
);

CREATE TABLE library_tags (
    stable_id TEXT NOT NULL REFERENCES library_items(stable_id) ON DELETE CASCADE,
    tag_key TEXT NOT NULL,
    display_name TEXT NOT NULL,
    PRIMARY KEY (stable_id, tag_key)
);

CREATE TABLE library_chapters (
    stable_id TEXT NOT NULL REFERENCES library_items(stable_id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    relative_path TEXT NOT NULL,
    title TEXT NOT NULL,
    PRIMARY KEY (stable_id, position)
);

CREATE TABLE library_images (
    stable_id TEXT NOT NULL REFERENCES library_items(stable_id) ON DELETE CASCADE,
    relative_path TEXT NOT NULL,
    PRIMARY KEY (stable_id, relative_path)
);

CREATE INDEX idx_library_items_novel_key ON library_items(novel_key);
CREATE INDEX idx_library_items_views ON library_items(views DESC);
CREATE INDEX idx_library_items_likes ON library_items(likes DESC);
CREATE INDEX idx_library_items_chapters ON library_items(chapters DESC);
CREATE INDEX idx_library_items_updated ON library_items(updated_sort DESC);
CREATE INDEX idx_library_items_author ON library_items(author COLLATE NOCASE);
CREATE INDEX idx_library_items_language ON library_items(language COLLATE NOCASE);
CREATE INDEX idx_library_tags_key ON library_tags(tag_key, stable_id);
CREATE INDEX idx_library_aliases_stable ON library_aliases(stable_id);
"""


def _canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _integer(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def novel_key(item: Mapping) -> str:
    return str(item.get("id")) if item.get("id") else str(item.get("filename") or "")


def stable_id_for(item: Mapping) -> str:
    if item.get("uploaded") and item.get("id"):
        identity = f"user-upload:{item['id']}"
    elif item.get("id"):
        identity = f"source-id:{item['id']}"
    elif item.get("_library_key"):
        identity = f"library-key:{item['_library_key']}"
    else:
        identity = f"filename:{item.get('filename') or ''}"
    return hashlib.sha256(("arcdb-library-v1\0" + identity).encode("utf-8")).hexdigest()[:32]


def aliases_for(item: Mapping) -> list[tuple[str, str]]:
    candidates: list[tuple[object, str]] = [
        (item.get("id"), "id"),
        (item.get("filename"), "filename"),
        (item.get("_library_key"), "library_key"),
        (novel_key(item), "novel_key"),
    ]
    candidates.extend((value, "source_id") for value in (item.get("_source_ids") or []))
    seen = set()
    aliases = []
    for raw, kind in candidates:
        value = str(raw or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        aliases.append((value, kind))
    return aliases


def _source_for(item: Mapping) -> str:
    if item.get("uploaded"):
        return "uploaded"
    if item.get("is_updated"):
        return "updated"
    if item.get("is_raw_only"):
        return "raw"
    return "official"


class LibraryIndex:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()

    def _read_connection(self) -> sqlite3.Connection:
        if not self.path.is_file():
            raise LibraryIndexUnavailable("Library index is missing")
        try:
            conn = sqlite3.connect(
                self.path.as_uri() + "?mode=ro",
                uri=True,
                timeout=5.0,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version != LIBRARY_INDEX_SCHEMA_VERSION:
                raise LibraryIndexUnavailable("Library index schema is incompatible")
            return conn
        except LibraryIndexUnavailable:
            try:
                conn.close()
            except (NameError, sqlite3.Error):
                pass
            raise
        except sqlite3.Error as exc:
            raise LibraryIndexUnavailable("Library index cannot be opened") from exc

    @staticmethod
    def _create_connection(path: Path) -> tuple[sqlite3.Connection, bool]:
        conn = sqlite3.connect(path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA synchronous=FULL")
        conn.executescript(SCHEMA_SQL)
        fts_enabled = True
        try:
            conn.execute(
                "CREATE VIRTUAL TABLE library_fts USING "
                "fts5(stable_id UNINDEXED, search_text, tokenize='trigram')"
            )
        except sqlite3.OperationalError:
            fts_enabled = False
        conn.execute(f"PRAGMA user_version={LIBRARY_INDEX_SCHEMA_VERSION}")
        return conn, fts_enabled

    @staticmethod
    def _content_for(loader, item: Mapping) -> dict:
        if loader is None:
            return {"chapters": [], "titles": {}, "images": []}
        content = loader(item) or {}
        return {
            "chapters": list(content.get("chapters") or []),
            "titles": dict(content.get("titles") or {}),
            "images": list(content.get("images") or []),
        }

    @staticmethod
    def _insert_item(
        conn: sqlite3.Connection,
        item: Mapping,
        content: Mapping,
        *,
        fts_enabled: bool,
        replace_existing: bool = False,
    ) -> tuple[str, str]:
        payload = dict(item)
        stable_id = stable_id_for(payload)
        payload_json = _canonical_json(payload)
        content_hash = hashlib.sha256(
            _canonical_json({"item": payload, "content": content}).encode("utf-8")
        ).hexdigest()
        title = str(payload.get("title_en") or "")
        original_title = str(payload.get("title_kr") or "")
        author = str(payload.get("author") or "")
        language = str(payload.get("language") or "").strip()
        upload_date = str(payload.get("upload_date") or "")
        updated_sort = "" if upload_date == LEGACY_UPLOAD_DATE else upload_date
        alias_rows = aliases_for(payload)
        alias_text = " ".join(alias for alias, _kind in alias_rows)
        alternate_titles = " ".join(
            str(value) for value in (payload.get("alternate_titles") or []) if value
        )
        search_text = " ".join(
            value for value in (title, original_title, alternate_titles, author, alias_text) if value
        )
        tags = {}
        for raw in payload.get("tags") or []:
            display = str(raw or "").strip()
            if display:
                tags.setdefault(display.casefold(), display)

        existing = conn.execute(
            "SELECT 1 FROM library_items WHERE stable_id=?", (stable_id,)
        ).fetchone()
        if existing and not replace_existing:
            raise LibraryIndexError("Duplicate stable library identity")
        if existing:
            conn.execute("DELETE FROM library_items WHERE stable_id=?", (stable_id,))
        conn.execute(
            """INSERT INTO library_items (
                   stable_id, novel_key, library_key, source, title, original_title,
                   author, language, cover, publication_status, adult, chapters, views,
                   likes, uploaded, updated, translated, upload_date, updated_sort,
                   content_hash, payload_json
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                stable_id,
                novel_key(payload),
                str(payload.get("_library_key") or ""),
                _source_for(payload),
                title,
                original_title,
                author,
                language,
                str(payload.get("cover") or ""),
                _integer(payload.get("complete")),
                1 if _integer(payload.get("age")) == 19 else 0,
                max(0, _integer(payload.get("chapters"))),
                max(0, _integer(payload.get("views"))),
                max(0, _integer(payload.get("likes"))),
                1 if payload.get("uploaded") else 0,
                1 if payload.get("is_updated") else 0,
                1
                if (
                    payload.get("tg_link")
                    or payload.get("translated_epub_path")
                    or payload.get("has_local_read")
                )
                else 0,
                upload_date,
                updated_sort,
                content_hash,
                payload_json,
            ),
        )
        for alias, kind in alias_rows:
            conflict = conn.execute(
                "SELECT stable_id FROM library_aliases WHERE alias=?", (alias,)
            ).fetchone()
            if conflict and str(conflict[0]) != stable_id:
                raise LibraryIndexError("Duplicate library alias")
            conn.execute(
                "INSERT INTO library_aliases(alias, stable_id, kind) VALUES (?,?,?)",
                (alias, stable_id, kind),
            )
        for tag_key, display in tags.items():
            conn.execute(
                "INSERT INTO library_tags(stable_id, tag_key, display_name) VALUES (?,?,?)",
                (stable_id, tag_key, display),
            )
        titles = content.get("titles") or {}
        for position, relative_path in enumerate(content.get("chapters") or []):
            rel = str(relative_path)
            title_value = str(titles.get(Path(rel).name.casefold()) or "")
            conn.execute(
                "INSERT INTO library_chapters(stable_id, position, relative_path, title) "
                "VALUES (?,?,?,?)",
                (stable_id, position, rel, title_value),
            )
        for relative_path in sorted(set(str(value) for value in (content.get("images") or []))):
            conn.execute(
                "INSERT INTO library_images(stable_id, relative_path) VALUES (?,?)",
                (stable_id, relative_path),
            )
        if fts_enabled:
            conn.execute("DELETE FROM library_fts WHERE stable_id=?", (stable_id,))
            conn.execute(
                "INSERT INTO library_fts(stable_id, search_text) VALUES (?,?)",
                (stable_id, search_text),
            )
        return stable_id, content_hash

    def rebuild(
        self,
        items: Iterable[Mapping],
        *,
        content_loader: Callable[[Mapping], Mapping] | None = None,
    ) -> dict:
        if self.path.exists() and not self.path.is_file():
            raise LibraryIndexError("Library index target is not a regular file")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        candidate = self.path.with_name(
            f".{self.path.name}.candidate.{os.getpid()}.{uuid.uuid4().hex}"
        )
        item_list = sorted((dict(item) for item in items), key=stable_id_for)
        conn = None
        try:
            conn, fts_enabled = self._create_connection(candidate)
            hashes = []
            with conn:
                for item in item_list:
                    _stable_id, content_hash = self._insert_item(
                        conn,
                        item,
                        self._content_for(content_loader, item),
                        fts_enabled=fts_enabled,
                    )
                    hashes.append(content_hash)
                generation = uuid.uuid4().hex
                metadata = {
                    "schema_version": str(LIBRARY_INDEX_SCHEMA_VERSION),
                    "generation_id": generation,
                    "built_at": str(time.time()),
                    "item_count": str(len(item_list)),
                    "source_fingerprint": hashlib.sha256(
                        "\n".join(sorted(hashes)).encode("ascii")
                    ).hexdigest(),
                    "fts5_trigram": "1" if fts_enabled else "0",
                }
                conn.executemany(
                    "INSERT INTO index_meta(key, value) VALUES (?,?)", metadata.items()
                )
            checks = self._validate_connection(conn, expected_items=len(item_list))
            conn.close()
            conn = None
            with candidate.open("rb+") as handle:
                os.fsync(handle.fileno())
            os.replace(candidate, self.path)
            if os.name == "posix":
                directory_fd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            return {
                "status": "rebuilt",
                "items": len(item_list),
                "chapters": checks["chapters"],
                "images": checks["images"],
                "fts5_trigram": fts_enabled,
                "generation_id": generation,
            }
        except Exception:
            if conn is not None:
                conn.close()
            try:
                candidate.unlink()
            except OSError:
                pass
            raise

    @staticmethod
    def _validate_connection(conn: sqlite3.Connection, expected_items: int | None = None) -> dict:
        quick = conn.execute("PRAGMA quick_check").fetchone()[0]
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
        item_count = conn.execute("SELECT COUNT(*) FROM library_items").fetchone()[0]
        if quick != "ok" or integrity != "ok" or foreign_keys:
            raise LibraryIndexError("Library index integrity validation failed")
        if expected_items is not None and item_count != expected_items:
            raise LibraryIndexError("Library index item count validation failed")
        return {
            "items": item_count,
            "chapters": conn.execute("SELECT COUNT(*) FROM library_chapters").fetchone()[0],
            "images": conn.execute("SELECT COUNT(*) FROM library_images").fetchone()[0],
        }

    def verify(self) -> dict:
        with closing(self._read_connection()) as conn:
            checks = self._validate_connection(conn)
            meta = dict(conn.execute("SELECT key, value FROM index_meta").fetchall())
            if _integer(meta.get("item_count"), -1) != checks["items"]:
                raise LibraryIndexUnavailable("Library index metadata is inconsistent")
            return {**checks, **meta}

    def check_ready(self) -> None:
        """Perform a bounded read-path probe suitable for frequent readiness checks."""
        try:
            with closing(self._read_connection()) as conn:
                meta = dict(
                    conn.execute(
                        "SELECT key, value FROM index_meta "
                        "WHERE key IN ('generation_id', 'item_count')"
                    ).fetchall()
                )
                item_count = conn.execute(
                    "SELECT COUNT(*) FROM library_items"
                ).fetchone()[0]
                if not meta.get("generation_id"):
                    raise LibraryIndexUnavailable("Library index generation is missing")
                if _integer(meta.get("item_count"), -1) != item_count:
                    raise LibraryIndexUnavailable(
                        "Library index metadata is inconsistent"
                    )
        except LibraryIndexUnavailable:
            raise
        except sqlite3.Error as exc:
            raise LibraryIndexUnavailable(
                "Library index readiness check failed"
            ) from exc

    def generation(self) -> str:
        with closing(self._read_connection()) as conn:
            row = conn.execute(
                "SELECT value FROM index_meta WHERE key='generation_id'"
            ).fetchone()
            if row is None:
                raise LibraryIndexUnavailable("Library index generation is missing")
            return str(row[0])

    @staticmethod
    def _decode_rows(rows) -> list[dict]:
        return [json.loads(row["payload_json"]) for row in rows]

    def all_items(self) -> list[dict]:
        with closing(self._read_connection()) as conn:
            rows = conn.execute(
                "SELECT payload_json FROM library_items ORDER BY stable_id"
            ).fetchall()
            return self._decode_rows(rows)

    def lookup(self, alias: str) -> dict | None:
        value = str(alias or "").strip()
        if not value:
            return None
        with closing(self._read_connection()) as conn:
            row = conn.execute(
                """SELECT i.payload_json FROM library_aliases a
                   JOIN library_items i ON i.stable_id=a.stable_id
                   WHERE a.alias=?""",
                (value,),
            ).fetchone()
            return json.loads(row[0]) if row else None

    def _stable_for_alias(self, conn: sqlite3.Connection, alias: str) -> str | None:
        row = conn.execute(
            "SELECT stable_id FROM library_aliases WHERE alias=?", (str(alias),)
        ).fetchone()
        return str(row[0]) if row else None

    def chapters(self, alias: str) -> tuple[list[str], dict[str, str]]:
        with closing(self._read_connection()) as conn:
            stable_id = self._stable_for_alias(conn, alias)
            if not stable_id:
                return [], {}
            rows = conn.execute(
                "SELECT relative_path, title FROM library_chapters "
                "WHERE stable_id=? ORDER BY position",
                (stable_id,),
            ).fetchall()
            paths = [str(row[0]) for row in rows]
            titles = {
                Path(str(row[0])).name.casefold(): str(row[1])
                for row in rows
                if row[1]
            }
            return paths, titles

    def images(self, alias: str) -> list[str]:
        with closing(self._read_connection()) as conn:
            stable_id = self._stable_for_alias(conn, alias)
            if not stable_id:
                return []
            return [
                str(row[0])
                for row in conn.execute(
                    "SELECT relative_path FROM library_images WHERE stable_id=? "
                    "ORDER BY relative_path",
                    (stable_id,),
                )
            ]

    def tag_counts(self) -> dict[str, int]:
        with closing(self._read_connection()) as conn:
            rows = conn.execute(
                """SELECT MIN(display_name), COUNT(*) FROM library_tags
                   WHERE tag_key <> 'unmatched' GROUP BY tag_key
                   ORDER BY tag_key"""
            ).fetchall()
            return {str(row[0]): int(row[1]) for row in rows}

    def authors(self) -> list[str]:
        with closing(self._read_connection()) as conn:
            rows = conn.execute(
                """SELECT DISTINCT author FROM library_items
                   WHERE author NOT IN ('', 'Unknown', 'Raw Upload')
                   ORDER BY author COLLATE NOCASE"""
            ).fetchall()
            return [str(row[0]) for row in rows]

    @staticmethod
    def _user_key_constraints(filters: Mapping, user_data: Mapping) -> tuple[set[str] | None, set[str]]:
        allowed: set[str] | None = None
        reading_status = str(filters.get("reading_status") or "all")
        if reading_status != "all":
            selected = {
                str(key)
                for key, record in user_data.items()
                if isinstance(record, Mapping)
                and (
                    (reading_status == "any" and record.get("status") not in (None, "", "none"))
                    or record.get("status") == reading_status
                )
            }
            allowed = selected
        excluded: set[str] = set()
        collection = str(filters.get("collection") or "all")
        if collection not in {"", "all"}:
            members = {
                str(key)
                for key, record in user_data.items()
                if isinstance(record, Mapping) and (record.get("collections") or [])
            }
            if collection == "none":
                excluded = members
            else:
                selected = {
                    str(key)
                    for key, record in user_data.items()
                    if isinstance(record, Mapping)
                    and collection in (record.get("collections") or [])
                }
                allowed = selected if allowed is None else allowed.intersection(selected)
        return allowed, excluded

    def query(
        self,
        *,
        filters: Mapping,
        user_data: Mapping,
        sort_by: str,
        sort_order: str,
        page: int,
        limit: int,
        random_one: bool = False,
    ) -> dict:
        page = max(1, int(page))
        limit = min(100, max(1, int(limit)))
        where = ["1=1"]
        params: list[object] = []
        upload_source = str(filters.get("upload_source") or "all")
        if upload_source == "uploaded":
            where.append("i.uploaded=1")
        elif upload_source == "updated":
            where.append("i.updated=1")
        elif upload_source == "official":
            where.append("i.uploaded=0 AND i.updated=0")
        translated = str(filters.get("translated_chapter") or "all")
        if translated == "translated":
            where.append("i.translated=1")
        elif translated == "raw":
            where.append("i.translated=0")
        audience = str(filters.get("audience") or "all")
        if audience == "adult":
            where.append("i.adult=1")
        elif audience == "non_adult":
            where.append("i.adult=0")
        status = str(filters.get("status") or "all")
        if status == "complete":
            where.append("i.publication_status=1")
        elif status == "ongoing":
            where.append("i.publication_status=0")
        author = str(filters.get("author") or "").strip().casefold()
        if author and author != "all":
            where.append("instr(lower(i.author), ?) > 0")
            params.append(author)
        language = str(filters.get("language") or "").strip().casefold()
        if language and language != "all":
            where.append("lower(i.language)=?")
            params.append(language)
        where.append("i.chapters BETWEEN ? AND ?")
        params.extend(
            [
                max(0, _integer(filters.get("min_chapters"))),
                max(0, _integer(filters.get("max_chapters"), 999999)),
            ]
        )
        updated_after = str(filters.get("updated_after") or "").strip()
        updated_before = str(filters.get("updated_before") or "").strip()
        if updated_after:
            where.append("i.updated_sort >= ?")
            params.append(updated_after)
        if updated_before:
            where.append("i.updated_sort <= ?")
            params.append(updated_before)

        includes = {str(value).strip().casefold() for value in (filters.get("includes") or []) if str(value).strip()}
        excludes = {str(value).strip().casefold() for value in (filters.get("excludes") or []) if str(value).strip()}
        if includes:
            placeholders = ",".join("?" for _ in includes)
            if str(filters.get("tag_match") or "and") == "or":
                where.append(
                    f"EXISTS (SELECT 1 FROM library_tags t WHERE t.stable_id=i.stable_id AND t.tag_key IN ({placeholders}))"
                )
                params.extend(sorted(includes))
            else:
                for tag in sorted(includes):
                    where.append(
                        "EXISTS (SELECT 1 FROM library_tags t WHERE t.stable_id=i.stable_id AND t.tag_key=?)"
                    )
                    params.append(tag)
        if excludes:
            placeholders = ",".join("?" for _ in excludes)
            where.append(
                f"NOT EXISTS (SELECT 1 FROM library_tags t WHERE t.stable_id=i.stable_id AND t.tag_key IN ({placeholders}))"
            )
            params.extend(sorted(excludes))

        allowed_keys, excluded_keys = self._user_key_constraints(filters, user_data)
        if allowed_keys is not None:
            where.append("i.novel_key IN (SELECT value FROM json_each(?))")
            params.append(_canonical_json(sorted(allowed_keys)))
        if excluded_keys:
            where.append("i.novel_key NOT IN (SELECT value FROM json_each(?))")
            params.append(_canonical_json(sorted(excluded_keys)))

        search = str(filters.get("search") or "").strip()
        with closing(self._read_connection()) as conn:
            fts_row = conn.execute(
                "SELECT value FROM index_meta WHERE key='fts5_trigram'"
            ).fetchone()
            if search:
                if (
                    fts_row
                    and fts_row[0] == "1"
                    and len(search) >= 3
                    and '"' not in search
                ):
                    where.append(
                        "i.stable_id IN (SELECT stable_id FROM library_fts WHERE library_fts MATCH ?)"
                    )
                    params.append('"' + search.replace('"', '""') + '"')
                else:
                    where.append(
                        "instr(lower(i.title || ' ' || i.original_title || ' ' || i.author || ' ' || i.novel_key), ?) > 0"
                    )
                    params.append(search.casefold())

            where_sql = " AND ".join(where)
            count = conn.execute(
                f"SELECT COUNT(*) FROM library_items i WHERE {where_sql}", params
            ).fetchone()[0]
            total_pages = max(1, (count + limit - 1) // limit)
            if random_one:
                row = conn.execute(
                    f"SELECT i.payload_json FROM library_items i WHERE {where_sql} ORDER BY RANDOM() LIMIT 1",
                    params,
                ).fetchone()
                return {"random": json.loads(row[0]) if row else None, "total": count}

            order_columns = {
                "views": "i.views",
                "likes": "i.likes",
                "chapters": "i.chapters",
                "upload_date": "i.updated_sort",
                "title": "i.title COLLATE NOCASE",
                "author": "i.author COLLATE NOCASE",
            }
            direction = "ASC" if sort_order == "asc" else "DESC"
            order_params: list[object] = []
            if sort_by == "last_read":
                order_sql = (
                    "COALESCE((SELECT CAST(value AS REAL) FROM json_each(?) u "
                    "WHERE u.key=i.novel_key), 0) " + direction
                )
                order_params.append(
                    _canonical_json(
                        {
                            str(key): float(record.get("last_read") or 0)
                            for key, record in user_data.items()
                            if isinstance(record, Mapping)
                        }
                    )
                )
            else:
                order_sql = order_columns.get(sort_by, "i.views") + " " + direction
            rows = conn.execute(
                f"SELECT i.payload_json FROM library_items i WHERE {where_sql} "
                f"ORDER BY {order_sql}, i.stable_id ASC LIMIT ? OFFSET ?",
                [*params, *order_params, limit, (page - 1) * limit],
            ).fetchall()
            return {
                "items": self._decode_rows(rows),
                "total": int(count),
                "total_pages": int(total_pages),
                "page": page,
                "limit": limit,
            }

    def upsert(self, item: Mapping, *, content: Mapping | None = None) -> None:
        if not self.path.is_file():
            raise LibraryIndexUnavailable("Library index is missing")
        conn = sqlite3.connect(self.path, timeout=10.0)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version != LIBRARY_INDEX_SCHEMA_VERSION:
                raise LibraryIndexUnavailable("Library index schema is incompatible")
            fts = conn.execute(
                "SELECT value FROM index_meta WHERE key='fts5_trigram'"
            ).fetchone()
            if content is None:
                stable_id = stable_id_for(item)
                chapter_rows = conn.execute(
                    "SELECT relative_path, title FROM library_chapters "
                    "WHERE stable_id=? ORDER BY position",
                    (stable_id,),
                ).fetchall()
                content = {
                    "chapters": [str(row[0]) for row in chapter_rows],
                    "titles": {
                        Path(str(row[0])).name.casefold(): str(row[1])
                        for row in chapter_rows
                        if row[1]
                    },
                    "images": [
                        str(row[0])
                        for row in conn.execute(
                            "SELECT relative_path FROM library_images "
                            "WHERE stable_id=? ORDER BY relative_path",
                            (stable_id,),
                        )
                    ],
                }
            with conn:
                self._insert_item(
                    conn,
                    item,
                    content or {"chapters": [], "titles": {}, "images": []},
                    fts_enabled=bool(fts and fts[0] == "1"),
                    replace_existing=True,
                )
                conn.execute(
                    "UPDATE index_meta SET value=? WHERE key='generation_id'",
                    (uuid.uuid4().hex,),
                )
                count = conn.execute("SELECT COUNT(*) FROM library_items").fetchone()[0]
                conn.execute(
                    "UPDATE index_meta SET value=? WHERE key='item_count'", (str(count),)
                )
        except sqlite3.Error as exc:
            raise LibraryIndexUnavailable("Library index update failed") from exc
        finally:
            conn.close()

    def delete_alias(self, alias: str) -> bool:
        if not self.path.is_file():
            raise LibraryIndexUnavailable("Library index is missing")
        conn = sqlite3.connect(self.path, timeout=10.0)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            row = conn.execute(
                "SELECT stable_id FROM library_aliases WHERE alias=?", (str(alias),)
            ).fetchone()
            if not row:
                return False
            with conn:
                fts = conn.execute(
                    "SELECT value FROM index_meta WHERE key='fts5_trigram'"
                ).fetchone()
                if fts and fts[0] == "1":
                    conn.execute("DELETE FROM library_fts WHERE stable_id=?", (row[0],))
                conn.execute("DELETE FROM library_items WHERE stable_id=?", (row[0],))
                conn.execute(
                    "UPDATE index_meta SET value=? WHERE key='generation_id'",
                    (uuid.uuid4().hex,),
                )
                count = conn.execute("SELECT COUNT(*) FROM library_items").fetchone()[0]
                conn.execute(
                    "UPDATE index_meta SET value=? WHERE key='item_count'", (str(count),)
                )
            return True
        except sqlite3.Error as exc:
            raise LibraryIndexUnavailable("Library index update failed") from exc
        finally:
            conn.close()
