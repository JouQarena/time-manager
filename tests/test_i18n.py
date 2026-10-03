"""Catalog tests: completeness, placeholder parity, fallbacks, RTL.

These run without Qt on purpose — the catalog is a plain dict and the UI only
*reads* it, so a half-translated screen fails here instead of shipping.
"""

import re

import pytest

from app import i18n

PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


def placeholders(template: str) -> set[str]:
    return set(PLACEHOLDER.findall(template))


@pytest.fixture(autouse=True)
def english_by_default():
    """Language is process-global state; every test starts and ends on English."""
    before = i18n.current_language()
    i18n.set_language("en")
    yield
    i18n.set_language(before)


# ------------------------------------------------------------------ coverage
def test_arabic_translates_every_english_string():
    assert i18n.missing_keys("ar") == []


def test_no_dead_translations():
    """A key that no longer exists in English must not linger in a catalog."""
    assert i18n.unknown_keys("ar") == []


def test_catalogs_hold_exactly_the_same_keys():
    assert set(i18n.EN) == set(i18n.AR)


def test_placeholders_match_between_languages():
    """A translation that drops `{count}` shows a broken sentence, not a typo."""
    mismatched = {
        key: (sorted(placeholders(en)), sorted(placeholders(i18n.AR[key])))
        for key, en in i18n.EN.items()
        if placeholders(en) != placeholders(i18n.AR[key])
    }
    assert mismatched == {}


def test_arabic_is_actually_arabic():
    """Guard against copy-paste: the Arabic half must not be English text."""
    # Keys that are *meant* to stay identical: the tour's language step names
    # both languages, and the placeholders here are process names and domains.
    same_on_purpose = {"tour.language.title", "re.none", "re.target_placeholder"}
    untranslated = [
        key for key, value in i18n.AR.items()
        if value == i18n.EN[key] and key not in same_on_purpose and len(value) > 12
    ]
    assert untranslated == []
    assert sum(1 for value in i18n.AR.values() if any("\u0600" <= c <= "\u06ff" for c in value)) > 150


# ------------------------------------------------------------------ behaviour
def test_language_metadata():
    codes = [lang.code for lang in i18n.available_languages()]
    assert codes == ["en", "ar"]                      # English stays first/default
    assert i18n.language_name("ar") == "العربية"
    assert i18n.language_name("ar", native=False) == "Arabic"
    assert i18n.LANGUAGES["ar"].rtl is True
    assert i18n.LANGUAGES["en"].rtl is False


@pytest.mark.parametrize("value,expected", [
    (None, "en"), ("", "en"), ("EN", "en"), ("ar", "ar"), ("ar-EG", "ar"),
    ("AR_SA", "ar"), ("fr", "en"), ("ar-weird", "ar"), ("xx-YY", "en"),
])
def test_normalize_language(value, expected):
    assert i18n.normalize_language(value) == expected


def test_set_language_returns_what_it_applied():
    assert i18n.set_language("ar-EG") == "ar"
    assert i18n.current_language() == "ar"
    assert i18n.is_rtl() is True
    assert i18n.set_language("klingon") == "en"      # unknown -> default
    assert i18n.is_rtl() is False


def test_tr_falls_back_instead_of_failing():
    assert i18n.tr("mw.title") == "Time Manager"
    i18n.set_language("ar")
    assert i18n.tr("mw.title") == "مدير الوقت"
    assert i18n.tr("nope.not.a.key") == "nope.not.a.key"     # visible, not fatal
    # The Arabic template carries a RIGHT-TO-LEFT MARK (U+200F) before the
    # placeholder so the number/duration keeps its position next to the word
    # "left" under bidi reordering.
    assert i18n.tr("fmt.left", duration="5m") == "متبقٍ \u200f5m"


def test_missing_placeholder_returns_the_template_not_an_exception():
    """A catalog typo must never raise inside a paint/timer callback."""
    i18n.set_language("en")
    assert i18n.tr("fmt.left") == "{duration} left"


def test_translated_status_text_round_trip():
    """The dashboard's wording is translated at call time, not at import."""
    from app.ui import viewmodel

    assert viewmodel.health(type("S", (), {"running": False})())[1] == "Stopped"
    i18n.set_language("ar")
    assert viewmodel.health(type("S", (), {"running": False})())[1] == "متوقّف"
    assert viewmodel.format_duration(3725) == "1س 02د"
    assert viewmodel.schedule_text(None) == "دائمًا"


# ------------------------------------------------------------------- defaults
def test_english_is_the_default_language(isolated_profile):
    """English is the default everywhere: catalog, settings model, and the
    config on a machine that has never chosen a language."""
    from app.config.settings import AppSettings, config_path, load_settings
    from app.ui.viewmodel import format_remaining, schedule_text

    # 1. the catalog's default
    assert i18n.DEFAULT_LANGUAGE == "en"
    assert i18n.current_language() == "en"
    #    ... and an unknown or missing value resolves to it, never to Arabic
    assert i18n.normalize_language(None) == "en"
    assert i18n.normalize_language("") == "en"
    assert i18n.normalize_language("de") == "en"

    # 2. the settings model
    assert AppSettings().language == "en"
    assert config_path().exists() is False        # nothing written yet

    # 3. a fresh profile: first read creates the config, still English
    settings = load_settings()
    assert settings.language == "en"
    assert '"language": "en"' in config_path().read_text(encoding="utf-8")

    # 4. strings render English without anyone calling set_language()
    assert schedule_text(None) == "Always"
    assert format_remaining(600) == "10m 00s left"
    assert i18n.is_rtl() is False


def test_arabic_is_only_ever_chosen_explicitly(isolated_profile):
    """A corrupt, partial or hostile config must not silently arabize the UI."""
    from app.config.settings import config_path, load_settings

    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    for payload in ('{"language": "zz"}',
                    '{"language": null}',
                    '{"language": 42}',
                    '{"language": ["ar"]}',
                    "{not json at all"):
        path.write_text(payload, encoding="utf-8")
        assert load_settings().language == i18n.DEFAULT_LANGUAGE, payload


def test_qt_startup_applies_english_when_no_language_is_saved(isolated_profile):
    """`run_gui` calls this before building any widget: no config -> English."""
    pytest.importorskip("PySide6")
    from app.config.settings import AppSettings
    from app.ui.qt.language import apply_saved_language

    assert apply_saved_language(AppSettings()) == "en"
    assert i18n.current_language() == "en"
    assert i18n.is_rtl() is False
    # an explicit Arabic choice, on the other hand, is honoured
    assert apply_saved_language(AppSettings(language="ar")) == "ar"
    assert i18n.is_rtl() is True
    i18n.set_language("en")


def test_app_dir_follows_appdata_on_windows(monkeypatch, tmp_path):
    """`app_dir()` must resolve under %APPDATA% on Windows.

    This branch cannot be executed on Linux/macOS builds, so the module's `os`
    and `Path` are replaced with recording stubs. It matters because the
    isolation fixture (tests/conftest.py) relies on it: redirect `APPDATA` and
    the whole profile follows, on every OS.
    """
    import app.config.settings as settings_mod

    class FakeOs:
        name = "nt"
        environ = {"APPDATA": str(tmp_path / "Roaming")}

    class FakePath:
        def __init__(self, value):
            self.value = str(value)

        def __truediv__(self, other):
            return f"{self.value}\\{other}"

        @staticmethod
        def home():                     # only used when APPDATA is missing
            return FakePath("C:\\Users\\nobody")

    monkeypatch.setattr(settings_mod, "os", FakeOs)
    monkeypatch.setattr(settings_mod, "Path", FakePath)
    assert settings_mod.app_dir() == f"{tmp_path / 'Roaming'}\\TimeManager"
