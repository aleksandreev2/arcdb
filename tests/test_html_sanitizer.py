from __future__ import annotations

import unittest

from arcdb.html_sanitizer import sanitize_epub_html


class EpubHTMLSanitizerTests(unittest.TestCase):
    def test_preserves_normal_epub_structure_and_reader_assets(self) -> None:
        source = (
            '<section epub:type="chapter" class="main" lang="en">'
            '<h2 id="start">Title &amp; subtitle</h2>'
            '<p><em>Text</em><br><img src="/api/read/42/asset/Images/a.jpg" '
            'alt="A &amp; B" width="640"></p>'
            '<a href="#start" title="Back">again</a></section>'
        )
        result = sanitize_epub_html(source)
        self.assertEqual(
            result,
            '<section epub:type="chapter" class="main" lang="en">'
            '<h2 id="start">Title &amp; subtitle</h2>'
            '<p><em>Text</em><br><img src="/api/read/42/asset/Images/a.jpg" '
            'alt="A &amp; B" width="640"></p>'
            '<a href="#start" title="Back">again</a></section>',
        )

    def test_drops_active_subtrees_attributes_and_foreign_namespaces(self) -> None:
        source = (
            '<p onclick="steal()" style="background:url(javascript:steal())">safe</p>'
            '<script><p>script payload</p></script>'
            '<iframe srcdoc="<script>bad()</script>">frame payload</iframe>'
            '<object data="x">object payload</object><embed src="x">'
            '<style>@import "https://evil.invalid"</style>'
            '<svg><a xlink:href="javascript:bad()">svg payload</a></svg>'
            '<math><annotation-xml>math payload</annotation-xml></math>'
            '<form action="https://evil.invalid"><input name="secret">form payload</form>'
            '<p>after</p>'
        )
        result = sanitize_epub_html(source)
        self.assertEqual(result, "<p>safe</p><p>after</p>")
        for marker in (
            "script", "iframe", "object", "embed", "style=", "onclick", "svg",
            "math", "form",
        ):
            self.assertNotIn(marker, result.casefold())

    def test_rejects_dangerous_and_obfuscated_url_schemes(self) -> None:
        source = (
            '<a href="javascript:alert(1)">one</a>'
            '<a href="jav&#x09;ascript:alert(2)">two</a>'
            '<a href="data:text/html,&lt;script&gt;">three</a>'
            '<img src="vbscript:bad" onerror="bad()">'
            '<a href="https://safe.example/path">safe</a>'
            '<a href="mailto:reader@example.test">mail</a>'
            '<img src="../Images/cover.jpg">'
        )
        result = sanitize_epub_html(source)
        self.assertEqual(
            result,
            '<a>one</a><a>two</a><a>three</a><img>'
            '<a href="https://safe.example/path">safe</a>'
            '<a href="mailto:reader@example.test">mail</a>'
            '<img src="../Images/cover.jpg">',
        )

    def test_duplicate_attributes_and_malformed_markup_fail_closed(self) -> None:
        duplicate = sanitize_epub_html(
            '<a href="javascript:bad()" href="https://safe.example">link</a>'
        )
        self.assertEqual(duplicate, "<a>link</a>")
        malformed = sanitize_epub_html('<div><p>before<script>bad()<b>hidden</script><p>after')
        self.assertNotIn("bad", malformed)
        self.assertNotIn("hidden", malformed)
        self.assertTrue(malformed.startswith("<div><p>before"), malformed)

    def test_comments_declarations_processing_instructions_and_unknown_tags_are_removed(self) -> None:
        result = sanitize_epub_html(
            '<!DOCTYPE html><!-- secret --><custom data-x="1"><p>text</p></custom><?xml x?>'
        )
        self.assertEqual(result, "<p>text</p>")


if __name__ == "__main__":
    unittest.main()
