from __future__ import annotations

import argparse
import zipfile
from pathlib import Path


def make_epub(path: Path, title: str, chapters: int = 3) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    container = '''<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>'''
    manifest = ''.join(
        f'<item id="c{i}" href="chapter{i}.xhtml" media-type="application/xhtml+xml"/>'
        for i in range(1, chapters + 1)
    )
    spine = ''.join(f'<itemref idref="c{i}"/>' for i in range(1, chapters + 1))
    opf = f'''<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier>dev-{title}</dc:identifier><dc:title>{title}</dc:title><dc:language>ru</dc:language><dc:creator>CI Fixture</dc:creator></metadata><manifest>{manifest}</manifest><spine>{spine}</spine></package>'''
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", container)
        zf.writestr("OEBPS/content.opf", opf)
        for i in range(1, chapters + 1):
            chapter_body = f"<h1>Chapter {i}</h1><p>fixture</p>"
            is_reader_security_fixture = (
                "422601" in title
                or "Я_стал_куратором_охотников_S-ранга" in title
            )
            if is_reader_security_fixture and i == 1:
                chapter_body += (
                    '<script>window.arcdbUnsafe = true</script>'
                    '<p onclick="window.arcdbUnsafe = true">safe after script</p>'
                    '<a href="jav&#x61;script:alert(1)">unsafe link</a>'
                    '<svg><script>alert(2)</script></svg>'
                    '<embed src="unsafe"><p>safe after void element</p>'
                )
            zf.writestr(
                f"OEBPS/chapter{i}.xhtml",
                f"<html><body>{chapter_body}</body></html>",
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    work = args.output.parent / "seed-fixtures-src"
    work.mkdir(parents=True, exist_ok=True)
    names = [
        "[11560] [11560] 너의 페티시가 보여.epub",
        "[2852] 이 회귀자는 무료로 해줍니다!.epub",
        "[422601] S급 헌터들의 가이드가 되었다.epub",
        "[43026] 욕망 실현 어플.epub",
        "[77870] 능욕 아카데미의 순애충.epub",
        "Мои_сексуальные_университесткие_подружки_главы_1-278.epub",
        "Регрессор_Академии_яндере_главы_1-441.epub",
        "Я_стал_куратором_охотников_S-ранга_главы_1-123_с_иллюстрациями_FIXED.epub",
    ]
    for name in names:
        make_epub(work / name, Path(name).stem)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.output, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in work.glob("*.epub"):
            zf.write(p, p.name)
        zf.writestr("ignored.file", "ignore me")
        zf.writestr("ignored.pdf", b"%PDF-1.4 fixture")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
