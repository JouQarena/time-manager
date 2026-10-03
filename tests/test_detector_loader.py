"""Plugin loading: drop-in detectors are validated, never trusted.

The bar is deliberately high. A detector that is wrong in the "no match" direction
gets a game killed, so anything unusable is rejected *with a reason* — and a bad
plugin must never stop the good ones from loading.
"""

from app.core.detection.base import GameSessionDetector, SessionVerdict
from app.core.detection.loader import (
    PluginReport,
    build_host,
    builtin_detectors,
    load_plugin_file,
)


GOOD_PLUGIN = '''
from app.core.detection.base import GameSessionDetector, SessionVerdict


class MinecraftDetector(GameSessionDetector):
    detector_id = "minecraft"
    display_name = "Minecraft"
    known_executables = ("javaw.exe",)

    def probe(self, snapshot):
        return SessionVerdict(in_session=False, confidence=1.0, detail="never in a session")
'''


def write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding='utf-8')
    return path


# -------------------------------------------------------------------- builtins
def test_builtin_detectors_include_league_of_legends():
    detectors = builtin_detectors()
    ids = [d.detector_id for d in detectors]
    assert "league_of_legends" in ids
    lol = next(d for d in detectors if d.detector_id == "league_of_legends")
    assert "league of legends.exe" in lol.known_executables
    assert lol.display_name == "League of Legends"


def test_build_host_without_plugins():
    host, report = build_host()
    # Phase 8: League of Legends, VALORANT, R.E.P.O. and TFT ship built-in.
    assert host.ids() == ["league_of_legends", "repo", "teamfight_tactics", "valorant"]
    assert set(report.accepted) == {"league_of_legends", "repo", "teamfight_tactics", "valorant"}
    assert report.rejected == () and report.directories == ()
    assert "league_of_legends" in report.to_dict()["accepted"]


# ----------------------------------------------------------------- fine plugins
def test_valid_plugin_is_loaded_and_registered(tmp_path):
    write(tmp_path, "minecraft.py", GOOD_PLUGIN)
    host, report = build_host(plugin_dir=tmp_path)
    assert set(report.accepted) == {"league_of_legends", "repo", "teamfight_tactics",
                                    "valorant", "minecraft"}
    assert report.rejected == ()
    managed = host.managed("minecraft")
    assert managed is not None and managed.stats.source == "plugin"
    assert managed.stats.origin.endswith("minecraft.py")
    assert host.registry.get("minecraft") is managed


def test_plugin_file_contract_accepts_a_detectors_list(tmp_path):
    text = GOOD_PLUGIN.replace('''def probe''', '''def probe''') + '''

DETECTORS = [MinecraftDetector()]
'''
    write(tmp_path, "listed.py", text)
    host, report = build_host(plugin_dir=tmp_path)
    assert "minecraft" in report.accepted


def test_plugin_dir_may_not_exist(tmp_path):
    host, report = build_host(plugin_dir=tmp_path / "nope")
    assert host.ids() == ["league_of_legends", "repo", "teamfight_tactics", "valorant"]
    assert report.rejected == ()


def test_underscore_files_are_ignored(tmp_path):
    write(tmp_path, "__init__.py", "raise RuntimeError('never imported')")
    write(tmp_path, "_notes.py", "raise RuntimeError('never imported')")
    host, report = build_host(plugin_dir=tmp_path)
    assert host.ids() == ["league_of_legends", "repo", "teamfight_tactics", "valorant"]
    assert report.rejected == ()


# ----------------------------------------------------------------- rejections
def test_syntax_error_is_rejected_with_a_reason(tmp_path):
    path = write(tmp_path, "broken.py", "def create(:\n")
    detectors, reason = load_plugin_file(path)
    assert detectors == [] and "import failed" in reason
    host, report = build_host(plugin_dir=tmp_path)
    assert report.rejected and report.rejected[0][0].endswith("broken.py")
    assert "import failed" in report.rejected[0][1]


def test_import_time_exception_is_rejected(tmp_path):
    write(tmp_path, "boom.py", "raise RuntimeError('kaboom')\n")
    host, report = build_host(plugin_dir=tmp_path)
    assert "kaboom" in report.rejected[0][1]
    assert "league_of_legends" in host.ids()  # the built-ins are unaffected


def test_missing_contract_is_rejected(tmp_path):
    write(tmp_path, "empty.py", "X = 1\n")
    detectors, reason = load_plugin_file(tmp_path / "empty.py")
    assert detectors == [] and "no create()" in reason


def test_factory_that_raises_is_rejected(tmp_path):
    write(tmp_path, "faulty.py", "def create():\n    raise ValueError('nope')\n")
    detectors, reason = load_plugin_file(tmp_path / "faulty.py")
    assert detectors == [] and "create() raised" in reason and "nope" in reason


def test_non_detector_object_is_rejected(tmp_path):
    write(tmp_path, "obj.py", "def create():\n    return object()\n")
    detectors, reason = load_plugin_file(tmp_path / "obj.py")
    assert detectors == [] and "GameSessionDetector" in reason


def test_detector_without_executables_is_rejected(tmp_path):
    write(tmp_path, "noexes.py", '''
from app.core.detection.base import GameSessionDetector, SessionVerdict


class Detector(GameSessionDetector):
    detector_id = "noexes"
    known_executables = ()

    def probe(self, snapshot):
        return SessionVerdict(in_session=False, confidence=1.0)
''')
    detectors, reason = load_plugin_file(tmp_path / "noexes.py")
    assert detectors == [] and "known_executables" in reason


def test_uppercase_executables_are_rejected(tmp_path):
    write(tmp_path, "loud.py", GOOD_PLUGIN.replace('("javaw.exe",)', '("JavaW.exe",)'))
    detectors, reason = load_plugin_file(tmp_path / "loud.py")
    assert detectors == [] and "lowercase" in reason


def test_plugin_that_raises_on_the_smoke_probe_is_rejected(tmp_path):
    write(tmp_path, "smoke.py", GOOD_PLUGIN.replace(
        "return SessionVerdict(in_session=False, confidence=1.0, detail=\"never in a session\")",
        "raise AttributeError('missing signal')"))
    detectors, reason = load_plugin_file(tmp_path / "smoke.py")
    assert detectors == [] and "probe() raised" in reason and "missing signal" in reason


def test_plugin_with_duplicate_id_is_rejected(tmp_path):
    write(tmp_path, "clone.py", '''
from app.core.detection.loader import builtin_detectors
from app.core.detection.base import GameSessionDetector, SessionVerdict


class Clone(GameSessionDetector):
    detector_id = "league_of_legends"   # already taken by the built-in
    known_executables = ("league of legends.exe",)

    def probe(self, snapshot):
        return SessionVerdict(in_session=False, confidence=1.0)
''')
    host, report = build_host(plugin_dir=tmp_path)
    assert "league_of_legends" in host.ids()
    assert "already registered" in report.rejected[0][1]
    # The built-in is the one that stayed registered (the plugin must not win).
    assert host.managed("league_of_legends").stats.source == "builtin"


def test_conflicting_plugins_in_one_directory_keep_the_first(tmp_path):
    write(tmp_path, "a.py", GOOD_PLUGIN)
    write(tmp_path, "b.py", GOOD_PLUGIN)  # same detector_id
    host, report = build_host(plugin_dir=tmp_path)
    assert "minecraft" in host.ids() and host.ids() == sorted(host.ids())
    assert len(report.rejected) == 1 and report.rejected[0][0].endswith("b.py")


def test_a_broken_plugin_never_stops_the_others(tmp_path):
    write(tmp_path, "1_broken.py", "raise RuntimeError('nope')\n")
    write(tmp_path, "2_good.py", GOOD_PLUGIN)
    write(tmp_path, "3_wrong.py", "def create():\n    return 42\n")
    host, report = build_host(plugin_dir=tmp_path)
    assert "minecraft" in host.ids() and host.ids() == sorted(host.ids())
    assert len(report.rejected) == 2


def test_partially_valid_file_keeps_the_good_detector(tmp_path):
    text = GOOD_PLUGIN + '''

class Bad(GameSessionDetector):
    detector_id = "bad"
    known_executables = ("x.exe",)

    def probe(self, snapshot):
        return "not a verdict"
''' + '''

DETECTORS = [MinecraftDetector(), Bad()]
'''
    write(tmp_path, "mixed.py", text)
    detectors, reason = load_plugin_file(tmp_path / "mixed.py")
    assert [d.detector_id for d in detectors] == ["minecraft"]
    assert reason == ""  # the file itself loaded; the bad entry is logged


def test_load_plugin_dir_is_deterministic(tmp_path):
    write(tmp_path, "b.py", GOOD_PLUGIN)
    write(tmp_path, "a.py", GOOD_PLUGIN)
    _, report = build_host(plugin_dir=tmp_path)
    assert report.rejected[0][0].endswith("b.py")  # a.py wins the id, b.py is rejected


def test_builtin_failure_is_reported_not_fatal(tmp_path):
    """A built-in that cannot be registered is reported and skipped."""

    class Broken(GameSessionDetector):
        detector_id = "broken_builtin"
        known_executables = ("x.exe",)

        def probe(self, snapshot):
            return SessionVerdict(in_session=False, confidence=1.0)

    original = Broken.known_executables
    host, report = build_host(builtins=[Broken(), Broken()])  # duplicate id
    assert report.rejected and "duplicate" in report.rejected[0][1]
    assert Broken.known_executables == original
    assert host.ids() == ["broken_builtin"]


def test_report_to_dict_is_json_safe():
    import json

    report = PluginReport(accepted=("a",), rejected=(("b.py", "nope"),), directories=("/tmp/p",))
    assert json.loads(json.dumps(report.to_dict()))["rejected"][0]["reason"] == "nope"
