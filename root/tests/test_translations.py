"""Guards for the i18n catalog (TASK-222, TASK-229).

Django's translation loading fails silently: a missing or stale .mo file just
means every {% trans %}/gettext call falls back to the English msgid with no
error. These tests exist so a contributor who edits a translated string (or
adds a language to settings.LANGUAGES) without re-running
`just compilemessages` finds out from a failing test, not from a native
speaker filing a bug about half-English UI.

This file guards both directions:
  - catalog integrity (TASK-222): every msgid in the .po actually renders
    translated at runtime.
  - source-string coverage (TASK-229): every string a contributor wrapped in
    {% trans %}/{% blocktrans %}/gettext()/gettext_lazy() actually made it into
    the .po catalog. Without this, a newly wrapped string that was never added
    via `just makemessages` falls back to English silently (the exact gap that
    shipped 'Points by Lift'/'Recent Activity'/etc. in English on the otherwise
    Spanish challenges detail page).
"""

import ast
import re
from pathlib import Path

import pytest
from django.conf import settings
from django.utils import translation

# A handful of msgids known to exist across the app; used as a cheap proxy for
# "the catalog is wired up and loaded" without hardcoding the full string set.
_SENTINEL_MSGIDS = ["Dashboard", "Settings", "Sign in", "Save"]


def _non_english_language_codes():
    return [code for code, _name in settings.LANGUAGES if code != "en"]


def _unescape_po_string(raw: str) -> str:
    """Undo .po string escaping (\\", \\\\, \\n, \\t) to get the runtime value."""
    return (
        raw.replace('\\"', '"')
        .replace("\\n", "\n")
        .replace("\\t", "\t")
        .replace("\\\\", "\\")
    )


def _po_msgids(po_path: Path) -> set[str]:
    """Extract the set of msgid values from a .po file via a small regex parser.

    Deliberately dependency-free (no polib) so this test runs with only the
    project's pinned requirements. Handles both single-line and adjacent
    multi-line (implicit string concatenation) msgid entries; does not need to
    handle msgid_plural specially since plural entries also carry a plain
    msgid line that this pattern already captures.
    """
    text = po_path.read_text(encoding="utf-8")
    msgids = set()
    for match in re.finditer(
        r'^msgid((?:\s*"(?:[^"\\]|\\.)*"\s*\n?)+)', text, re.MULTILINE
    ):
        pieces = re.findall(r'"((?:[^"\\]|\\.)*)"', match.group(1))
        joined = _unescape_po_string("".join(pieces))
        if joined:  # skip the empty-string header entry
            msgids.add(joined)
    return msgids


def _po_msgid_plurals(po_path: Path) -> set[str]:
    """Extract msgid_plural values — needed because {% blocktrans count %}
    produces a plural-form source string that _po_msgids() never sees (its
    regex anchors on literal `msgid`, so it structurally cannot match
    `msgid_plural`). Kept a separate function rather than folded into
    _po_msgids() so test_catalog_has_no_untranslated_entries' coverage math
    (which consumes _po_msgids()) is unaffected."""
    text = po_path.read_text(encoding="utf-8")
    plurals = set()
    for match in re.finditer(
        r'^msgid_plural((?:\s*"(?:[^"\\]|\\.)*"\s*\n?)+)', text, re.MULTILINE
    ):
        pieces = re.findall(r'"((?:[^"\\]|\\.)*)"', match.group(1))
        joined = _unescape_po_string("".join(pieces))
        if joined:
            plurals.add(joined)
    return plurals


# TASK-229 source-string extraction ------------------------------------------
#
# These scan the repo's own templates/Python for already-marked translatable
# strings and normalize them to the exact form Django's makemessages writes as
# a msgid, so they can be cross-checked against the .po catalog.

_TRANS_TAG_RE = re.compile(
    r"""\{%-?\s*trans\s+(?P<q>["'])(?P<str>(?:[^\\]|\\.)*?)(?P=q)"""
)
_BLOCKTRANS_RE = re.compile(
    r"\{%-?\s*blocktrans\b[^%]*%\}(?P<body>.*?)\{%-?\s*endblocktrans\s*-?%\}",
    re.DOTALL,
)
_TEMPLATE_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


def _extract_template_strings(path: Path) -> set[str]:
    """Extract {% trans %} / {% blocktrans %} source strings from a template,
    normalized to the same form makemessages writes as a msgid: {{ var }}
    becomes %(var)s, blocktrans body whitespace is preserved verbatim (Django
    does NOT trim it), and a {% plural %} split inside blocktrans yields two
    separate strings (msgid + msgid_plural) since Django's ngettext-style
    extraction does. Only the quoted-literal form of {% trans %} is handled —
    {% trans someVar %} carries no static source string and is correctly
    skipped."""
    text = path.read_text(encoding="utf-8")
    strings = set()
    for m in _TRANS_TAG_RE.finditer(text):
        strings.add(_unescape_po_string(m.group("str")))
    for m in _BLOCKTRANS_RE.finditer(text):
        for part in m.group("body").split("{% plural %}", 1):
            strings.add(_TEMPLATE_VAR_RE.sub(r"%(\1)s", part))
    return strings


_TRANSLATABLE_IMPORT_NAMES = {"gettext", "gettext_lazy"}
_PY_SCAN_EXCLUDE_DIR_NAMES = {
    "migrations",
    "tests",
    "__pycache__",
    ".git",
    ".venv",
    ".venv-docker",
    "venv",
    "node_modules",
    ".claude",
    "static",
}


def _iter_python_source_files():
    base = Path(settings.BASE_DIR)
    for path in base.rglob("*.py"):
        if any(
            part in _PY_SCAN_EXCLUDE_DIR_NAMES for part in path.relative_to(base).parts
        ):
            continue
        yield path


def _extract_python_gettext_strings(path: Path) -> set[str]:
    """Extract literal-string arguments to gettext()/gettext_lazy() calls
    (however imported/aliased in this file) via ast, not regex — Python already
    merges adjacent string-literal concatenation into one ast.Constant, so
    multi-line concatenated calls (see challenges/custom_goals.py) resolve
    correctly for free. Calls whose first argument isn't a literal string (e.g.
    a variable, like this file's own translation.gettext(m) lookups) are
    skipped — they are runtime lookups of strings declared elsewhere, not new
    source strings. Only direct-name calls resolved from a
    django.utils.translation import in the same file are matched;
    pgettext/ngettext/gettext_noop are not imported anywhere today and are
    deliberately out of scope."""
    text = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return set()
    alias_to_canonical = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module == "django.utils.translation"
        ):
            for alias in node.names:
                if alias.name in _TRANSLATABLE_IMPORT_NAMES:
                    alias_to_canonical[alias.asname or alias.name] = alias.name
    if not alias_to_canonical:
        return set()
    strings = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in alias_to_canonical
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            strings.add(node.args[0].value)
    return strings


def _all_translatable_source_strings() -> set[str]:
    """Every already-marked translatable source string in the repo: template
    {% trans %}/{% blocktrans %} plus Python gettext()/gettext_lazy(). Scoped to
    settings.TEMPLATES[0]["DIRS"][0] (i.e. templates/) specifically, NOT a
    repo-wide **/*.html glob, so non-Django HTML elsewhere in the repo is never
    swept in. Also covers *.txt (the two password-reset email templates are
    plain text, not HTML)."""
    strings = set()
    templates_dir = Path(settings.TEMPLATES[0]["DIRS"][0])
    for pattern in ("*.html", "*.txt"):
        for path in templates_dir.rglob(pattern):
            strings |= _extract_template_strings(path)
    for path in _iter_python_source_files():
        strings |= _extract_python_gettext_strings(path)
    return strings


@pytest.mark.parametrize("code", _non_english_language_codes())
class TestTranslationCatalogs:
    def test_compiled_catalog_exists(self, code):
        mo_path = settings.LOCALE_PATHS[0] / code / "LC_MESSAGES" / "django.mo"
        assert mo_path.exists(), (
            f"locale/{code}/LC_MESSAGES/django.mo is missing — run "
            f"`just compilemessages` after editing locale/{code}/LC_MESSAGES/django.po"
        )

    def test_sentinel_strings_render_differently_from_english(self, code):
        with translation.override(code):
            translated = {m: translation.gettext(m) for m in _SENTINEL_MSGIDS}
        with translation.override("en"):
            english = {m: translation.gettext(m) for m in _SENTINEL_MSGIDS}
        assert translated != english, (
            f"None of {_SENTINEL_MSGIDS} render differently in '{code}' than in "
            "English — the .mo is likely stale or empty"
        )

    def test_catalog_has_no_untranslated_entries(self, code):
        po_path = settings.LOCALE_PATHS[0] / code / "LC_MESSAGES" / "django.po"
        source_msgids = _po_msgids(po_path)
        with translation.override(code):
            untranslated = [
                msgid
                for msgid in source_msgids
                if translation.gettext(msgid) == msgid
                # A translation identical to the English source is legitimate
                # for msgids that ARE the same word in both languages (e.g.
                # "FitnessVolt"); only flag the empty-catalog failure mode by
                # requiring at least 95% of msgids to differ, not every one.
            ]
        # Some short/borrowed-word strings (e.g. brand names, "kg") are
        # legitimately identical across languages, so this asserts near-total
        # rather than 100% coverage — it still catches "forgot to translate
        # most of the file" or "catalog didn't compile".
        untranslated_fraction = len(untranslated) / len(source_msgids)
        assert untranslated_fraction < 0.05, (
            f"{len(untranslated)}/{len(source_msgids)} msgids in '{code}' render "
            f"identically to English — catalog looks incomplete or stale: "
            f"{untranslated[:10]}"
        )

    def test_no_source_strings_missing_from_catalog(self, code):
        """Regression guard for TASK-229: a string wrapped in {% trans %} /
        {% blocktrans %} / gettext() / gettext_lazy() that never made it into
        the .po catalog fails silently at runtime (falls back to English)
        instead of erroring anywhere — see the challenges/detail.html
        incident. Cross-references every such source string against the
        catalog's own msgid/msgid_plural set."""
        po_path = settings.LOCALE_PATHS[0] / code / "LC_MESSAGES" / "django.po"
        known = _po_msgids(po_path) | _po_msgid_plurals(po_path)
        missing = sorted(_all_translatable_source_strings() - known)
        assert not missing, (
            f"{len(missing)} string(s) wrapped in trans/blocktrans/gettext "
            f"have no msgid in locale/{code}/LC_MESSAGES/django.po. Run "
            f"`just makemessages {code}`, translate the new entries, then "
            f"`just compilemessages`:\n" + "\n".join(f"  - {s!r}" for s in missing[:20])
        )


def test_guard_catches_source_string_missing_from_catalog(tmp_path):
    """Regression test for TASK-229: reproduces the exact failure shape of the
    challenges/detail.html incident (a {% trans %}-wrapped string present in
    a template, absent from locale/es/LC_MESSAGES/django.po) and proves
    _extract_template_strings()/_po_msgids() together would have caught it. Uses
    synthetic fixtures, not the live catalog, so this stays green/red for the
    right reason regardless of future catalog edits."""
    template_dir = tmp_path / "templates"
    template_dir.mkdir()
    (template_dir / "detail.html").write_text(
        '{% trans "Points by Lift" %}\n{% trans "Recent Activity" %}\n'
    )
    po_path = tmp_path / "django.po"
    po_path.write_text(
        'msgid ""\nmsgstr ""\n\nmsgid "Points by Lift"\n'
        'msgstr "Puntos por levantamiento"\n'
    )  # "Recent Activity" deliberately omitted, reproducing the incident.

    extracted = set()
    for path in template_dir.rglob("*.html"):
        extracted |= _extract_template_strings(path)
    missing = extracted - (_po_msgids(po_path) | _po_msgid_plurals(po_path))

    assert missing == {"Recent Activity"}


def test_out_of_scope_content_has_no_translation_markers():
    """Pins down TRANSLATIONS.md's 'What isn't translatable yet' list: these
    surfaces must stay free of {% trans %}/{% blocktrans %} so the TASK-229
    guard never has a reason to flag them. If this starts failing, someone
    wrapped legal-page text in {% trans %} — see TRANSLATIONS.md before
    "fixing" it by adding a translation."""
    for rel in ("legal/terms.html", "legal/privacy.html"):
        path = Path(settings.TEMPLATES[0]["DIRS"][0]) / rel
        assert not _extract_template_strings(path), (
            f"{rel} should have no trans markers"
        )


def test_default_english_unaffected_by_other_locales():
    """Existing (pre-i18n) tests assume English rendering with no explicit
    language selection. This guards that assumption stays true: the default
    LANGUAGE_CODE is English and no language cookie/header is sent."""
    assert settings.LANGUAGE_CODE == "en"
    assert translation.get_language() in (None, "en", "en-us")
