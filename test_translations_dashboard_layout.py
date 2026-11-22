"""Translations dashboard presentation (layout) regression tests - static checks, no browser needed.

Root causes these guard against:
  * the Translations section was included AFTER the .main wrapper closed, so it had no sidebar offset/padding
    (it sat under the fixed sidebar) and was pushed one viewport down by .main's min-height:100vh;
  * `.trn .trn-table th` set the header text to the muted colour, overriding the dashboard's white-on-green `th` rule.
"""
import re
from html.parser import HTMLParser
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parent
VOID = {"meta", "link", "br", "hr", "img", "input", "col", "source", "area", "base", "wbr", "embed", "param", "track"}


def _rendered():
    return Environment(loader=FileSystemLoader(str(ROOT / "templates"))).get_template("admin_dashboard.html").render()


class _Ancestors(HTMLParser):
    def __init__(self, target):
        super().__init__()
        self.target, self.stack, self.found = target, [], None

    def handle_starttag(self, tag, attrs):
        if tag in VOID:
            return
        d = dict(attrs)
        self.stack.append((tag, d.get("id") or "", (d.get("class") or "").split()))
        if d.get("id") == self.target and self.found is None:
            self.found = list(self.stack[:-1])

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break


def _ancestors(section_id):
    p = _Ancestors(section_id)
    p.feed(_rendered())
    return p.found


def test_translations_section_is_inside_the_main_content_wrapper():
    anc = _ancestors("section-translations")
    assert anc is not None, "section-translations missing from the dashboard"
    assert any("main" in classes for _, _, classes in anc), "Translations must be inside .main (sidebar offset + padding)"


def test_translations_is_mounted_in_the_same_place_as_ordinary_sections():
    ref = _ancestors("section-deals")
    got = _ancestors("section-translations")
    assert [a[:1] + (tuple(a[2]),) for a in ref] == [a[:1] + (tuple(a[2]),) for a in got]


def test_translations_partial_is_included_exactly_once():
    assert _rendered().count('id="section-translations"') == 1


def test_table_header_text_is_white_on_the_dark_green_header():
    css = (ROOT / "static/css/translations.css").read_text(encoding="utf-8")
    rule = re.search(r"\.trn \.trn-table th\{([^}]*)\}", css).group(1)
    assert re.search(r"(^|;)color:#fff(;|$)", rule), rule
    assert "background:#3E5F55" in rule, rule


def test_empty_banner_takes_no_space_and_content_cannot_overflow_the_page():
    css = (ROOT / "static/css/translations.css").read_text(encoding="utf-8")
    assert ".trn .trn-banner:empty{display:none}" in css
    assert ".trn{min-width:0;max-width:100%}" in css


def test_stylesheet_cache_buster_was_bumped():
    assert "/static/css/translations.css?v=h4" in (ROOT / "templates/admin_dashboard.html").read_text(encoding="utf-8")
