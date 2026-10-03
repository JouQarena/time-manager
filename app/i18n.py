"""Translations for the desktop app (English + العربية).

One tiny, Qt-free catalog layer so every surface — the dashboard, the tray
menu, the dialogs, the onboarding tour and the `--status` text — can be
translated from a single place and unit-tested without a display.

Design rules (kept deliberately boring):

- **English is the source of truth.** `EN` holds the canonical wording; a
  missing key in another catalog falls back to `EN`, then to the key itself,
  so a half-finished translation never crashes a dialog.
- **Keys are dotted** (`rule.state.ready`), values use `str.format`
  placeholders (`fmt.left` -> `"{duration} left"`). Nothing else.
- The catalog is checked for completeness by `missing_keys()` in the test
  suite, so adding an English string without a translation fails CI instead
  of quietly shipping a half-Arabic screen.
- No external files: translations ship inside the bundle, nothing is loaded
  from disk or the network (the app is offline-first by design).

Arabic is written right-to-left; `is_rtl()` is what the Qt layer uses to flip
the layout direction, and every string below is kept in logical order so the
flip is the only mirroring that has to happen.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

__all__ = [
    "LANGUAGES",
    "DEFAULT_LANGUAGE",
    "Language",
    "available_languages",
    "current_language",
    "is_rtl",
    "language_name",
    "missing_keys",
    "normalize_language",
    "set_language",
    "tr",
]


@dataclass(frozen=True)
class Language:
    """Metadata for one shipped language."""

    code: str
    native_name: str   # how the language calls itself ("العربية")
    english_name: str
    rtl: bool = False


LANGUAGES: dict[str, Language] = {
    "en": Language(code="en", native_name="English", english_name="English"),
    "ar": Language(code="ar", native_name="العربية", english_name="Arabic", rtl=True),
}

DEFAULT_LANGUAGE = "en"

_lock = threading.Lock()
_current = DEFAULT_LANGUAGE


# --------------------------------------------------------------- public API
def available_languages() -> list[Language]:
    """Shipped languages, in the order the UI should offer them."""
    return [LANGUAGES["en"], LANGUAGES["ar"]]


def normalize_language(code: str | None) -> str:
    """Map any user/config value onto a shipped language code."""
    if not code:
        return DEFAULT_LANGUAGE
    token = str(code).strip().lower().replace("_", "-")
    if token in LANGUAGES:
        return token
    base = token.split("-")[0]          # "ar-EG" and "ar-SA" are both Arabic
    return base if base in LANGUAGES else DEFAULT_LANGUAGE


def set_language(code: str | None) -> str:
    """Select the active language; returns the code actually applied."""
    global _current
    resolved = normalize_language(code)
    with _lock:
        _current = resolved
    return resolved


def current_language() -> str:
    with _lock:
        return _current


def is_rtl(code: str | None = None) -> bool:
    """True when the language is written right-to-left."""
    return LANGUAGES[normalize_language(code or current_language())].rtl


def language_name(code: str, *, native: bool = True) -> str:
    lang = LANGUAGES[normalize_language(code)]
    return lang.native_name if native else lang.english_name


def tr(key: str, **kwargs) -> str:
    """Translate `key`, interpolating `kwargs`.

    Falls back to English and finally to the key itself, so a missing entry
    is visible but never fatal. A bad placeholder in one language logs nothing
    and returns the unformatted template rather than raising inside paint().
    """
    text = CATALOGS.get(current_language(), {}).get(key)
    if text is None:
        text = EN.get(key, key)
    if not kwargs:
        return text
    try:
        return text.format(**kwargs)
    except (KeyError, IndexError, ValueError):
        return text


def missing_keys(code: str) -> list[str]:
    """Keys present in English but absent from `code` (empty = complete)."""
    if normalize_language(code) == DEFAULT_LANGUAGE:
        return []
    return sorted(k for k in EN if k not in CATALOGS.get(normalize_language(code), {}))


def unknown_keys(code: str) -> list[str]:
    """Keys translated in `code` that no longer exist in English (dead entries)."""
    return sorted(k for k in CATALOGS.get(normalize_language(code), {}) if k not in EN)


# ---------------------------------------------------------------- catalogues
EN: dict[str, str] = {
    # ---------------------------------------------------------------- format
    "fmt.unlimited": "unlimited",
    "fmt.left": "{duration} left",
    "fmt.resets": "resets at midnight ({duration})",
    "fmt.and": "and",
    "fmt.list_sep": ", ",
    # Durations stay compact and keep Western digits in both languages (that
    # is what Arabic software UIs do); only the unit letters are translated.
    "fmt.dur_hm": "{hours}h {minutes:02d}m",
    "fmt.dur_ms": "{minutes}m {secs:02d}s",
    "fmt.dur_s": "{secs}s",
    # -------------------------------------------------------------- schedules
    "schedule.always": "Always",
    "schedule.every_day": "Every day",
    "schedule.weekdays": "Mon–Fri",
    "schedule.weekends": "Sat–Sun",
    "schedule.custom": "Custom…",
    "day.MON": "Mon",
    "day.TUE": "Tue",
    "day.WED": "Wed",
    "day.THU": "Thu",
    "day.FRI": "Fri",
    "day.SAT": "Sat",
    "day.SUN": "Sun",
    # ------------------------------------------------------- rule vocabulary
    "action.close_app": "Close app",
    "action.block": "Block",
    "action.wait_session": "Wait for match to end",
    "action.warn_only": "Warn only",
    "action.block_site": "Block site",
    "action.prevent_launch": "Prevent launch",
    "type.app": "App",
    "type.game": "Game",
    "type.website": "Website",
    "state.off": "Off",
    "state.paused": "Paused (not counting)",
    "state.outside_schedule": "Outside schedule",
    "state.limit_warned": "Limit reached (warned)",
    "state.blocked_browser": "Blocked in browser",
    "state.limit_after_match": "Limit reached — closes after the match",
    "state.limit_closed": "Limit reached — closed",
    "state.in_match": "In a match — waits for it to end",
    "state.warning": "Warning — {remaining}",
    "state.in_use": "In use — {remaining}",
    "state.ready": "Ready",
    "session.text": "session {duration}",
    "session.none": "no open session",
    # ------------------------------------------------------------------ health
    "health.stopped": "Stopped",
    "health.paused": "Paused — {duration} left",
    "health.errors": "Running with {count} monitor error(s)",
    "health.degraded": "Running (limited OS support)",
    "health.monitoring": "Monitoring",
    "status.tick": "Tick {ticks}",
    "status.browsers": "{count} browser(s) connected",
    "status.no_browser": "no browser connected",
    "status.closes": "{count} app close(s) today",
    "status.sleep_gap": "last sleep gap {duration}",
    "status.last_error": "last error: {text}",
    "pause.banner": "Tracking paused for {duration}",
    "pause.until": " (until {time})",
    "pause.strict": " — strict rules keep running: {names}",
    # ---------------------------------------------------------------- security
    "sec.unclean": "Time Manager stopped unexpectedly (last run started {when}) — "
                   "limits were not enforced until this start.",
    "sec.clock": "The system clock is {duration} behind the last time the agent ran — "
                 "daily limits may have been rolled back.",
    "sec.tamper": "{rule}: recorded usage for {day} was erased or rolled back — the "
                  "limit counts from the remembered {duration} instead.",
    "sec.silent": "No browser extension connected — website limits are NOT enforced "
                  "for: {rules}.",
    "sec.startup_repaired": "Windows startup was re-enabled because STRICT rules are active.",
    # ------------------------------------------------------------- side panels
    "browser.none": "No browser connected — open the extension options and paste the "
                    "token (python -m app.main --show-token).",
    "browser.active": "Active: {domains}",
    "browser.connected_idle": "Connected — no tracked site in focus",
    "detector.none": "No game detectors loaded — games count like any other app.",
    "detector.quarantined": "quarantined — matches will not be closed",
    "detector.in_session": "in session: {detail}",
    "detector.no_session": "no session: {detail}",
    "detector.unknown": "unknown: {detail}",
    "detector.waiting": "waiting for a game",
    "detector.budget": "Budget {ms} ms per probe; {quarantined} quarantined",
    "detector.rejected": "Rejected plugin: {origin} — {reason}",
    "link.unavailable": "Browser link unavailable",
    "link.listening": "Listening on 127.0.0.1:{port}",
    "link.messages": "in {in_count} / out {out_count} messages",
    "link.rate_limited": ", {count} rate-limited",
    "audit.close_app": "Close app",
    "audit.block_website": "Block site",
    "audit.wait_session": "Wait for match",
    "audit.warn": "Warning",
    "audit.notify": "Notified",
    "title.window": "Time Manager — {state}",
    # ---------------------------------------------------------------- widgets
    "card.edit": "Edit",
    "card.disable": "Disable",
    "card.enable": "Enable",
    "card.paused_suffix": " · paused",
    # ------------------------------------------------------------ main window
    "mw.title": "Time Manager",
    "mw.starting": "starting…",
    "mw.chip_starting": "Starting",
    "mw.pause": "Pause",
    "mw.pause_for": "Pause for {minutes} minutes",
    "mw.pause_until_tomorrow": "Pause until tomorrow (max {hours}h)",
    "mw.resume": "Resume tracking",
    "mw.new_rule": "New rule",
    "mw.settings": "Settings",
    "mw.guide": "Guide",
    "mw.rules_title": "Your rules",
    "mw.rules_meta": "{count} rule(s) · {active} active",
    "mw.panel_browser": "Browser extension",
    "mw.panel_link": "Agent link",
    "mw.panel_detectors": "Game detectors",
    "mw.panel_timeline": "Recent enforcement",
    "mw.panel_help": "Help",
    "help.title": "Add the browser extension",
    "help.intro": "Website rules are enforced by a small Chrome or Edge extension. "
                  "This is the whole job: three steps, about two minutes. Nothing "
                  "leaves this machine — the extension talks to 127.0.0.1 only.",
    "help.step1": "Open chrome://extensions in Chrome (edge://extensions in Edge), "
                  "turn on Developer mode, and press \u201cLoad unpacked\u201d.",
    "help.step2": "Select this folder \u2014 the one that contains manifest.json:",
    "help.path_note": "In a packaged install it sits inside the app folder, next to "
                      "TimeManagerTray.exe; TimeManager.exe --show-extension-path "
                      "prints the same path.",
    "help.path_missing": "manifest.json is not in this folder \u2014 this build is "
                         "missing its extension data. Reinstall the app (or run from "
                         "source) to get it back.",
    "help.step3": "Open the extension\u2019s Options, paste the pairing token, and "
                  "press \u201cSave & test\u201d:",
    "help.token_note": "Host 127.0.0.1 \u00b7 Port {port}. The token is a machine "
                       "secret: regenerating it in Settings \u25b8 Browser extension "
                       "disconnects every browser until it is re-paired.",
    "help.copy_path": "Copy path",
    "help.copy_token": "Copy token",
    "help.path_copied": "Folder path copied to the clipboard.",
    "help.token_copied": "Pairing token copied to the clipboard.",
    "help.open_folder": "Open the folder",
    "help.close": "Close",
    "help.footer": "Once it pairs, the Browser extension panel shows the browser and "
                   "the message counters. The extension\u2019s own Guide page "
                   "(its Settings \u25b8 Guide) has screenshots and a troubleshooting "
                   "table.",
    "mw.empty": "No rules yet.\n\nTrack an app, a game or a website to give it a "
                "daily or per-session limit.",
    "mw.empty_action": "Create your first rule",
    "mw.nothing_enforced": "Nothing enforced yet today.",
    "mw.quit_title": "Quit Time Manager",
    "mw.quit_body": "Quit the agent?\n\nMonitoring and enforcement stop until you "
                    "start it again.",
    "mw.paused_chip": "Paused · {duration}",
    "mw.tooltip_profile": "profile: {path}",
    # -------------------------------------------------------------------- tray
    "tray.open_dashboard": "Open dashboard",
    "tray.pause_tracking": "Pause tracking",
    "tray.minutes": "{minutes} minutes",
    "tray.resume": "Resume",
    "tray.rules": "Rules and usage…",
    "tray.show_guide": "Show guide",
    "tray.language": "Language",
    "tray.quit": "Quit Time Manager",
    "tray.tooltip": "Time Manager — {status}\n{line}",
    # ------------------------------------------------------- settings dialog
    "set.title": "Time Manager settings",
    "set.group_language": "Language",
    "set.language": "Language",
    "set.language_hint": "Applies immediately to the dashboard and the tray menu. "
                         "Arabic switches the whole app to right-to-left.",
    "set.group_monitoring": "Monitoring",
    "set.check_every": "Check every",
    "set.foreground_only": "Count an app only while it is in the foreground",
    "set.grace": "Grace after losing focus",
    "set.idle": "Ignore idle time after",
    "set.idle_off": "off",
    "set.browser_foreground": "Website time requires a browser in the foreground",
    "set.close_timeout": "Graceful close timeout",
    "set.group_browser": "Browser extension",
    "set.agent_port": "Agent port",
    "set.token": "Pairing token",
    "set.show": "Show",
    "set.hide": "Hide",
    "set.copy": "Copy",
    "set.regen": "Regenerate token",
    "set.hint": "Paste this token into the extension's Options page. Regenerating "
                "invalidates the old one and restarts the browser link — you must "
                "re-pair every browser.",
    "set.group_general": "General",
    "set.startup": "Start Time Manager with Windows",
    "set.notifications": "Show desktop notifications for warnings and limits",
    "set.backup_now": "Back up now",
    "set.data": "Data",
    "set.group_strict": "Strict-mode protection",
    "set.watchdog": "Restart the agent if it stops (Task Scheduler watchdog, "
                    "checks every minute)",
    "set.protect_hint": "Everything Time Manager does against bypassing is visible in "
                        "the audit trail: STRICT rules keep their Windows startup entry "
                        "(it is re-added if removed), usage cannot be reset by editing "
                        "the database, and an agent that was killed reports the gap at "
                        "the next start. The watchdog registers a task named “Time "
                        "Manager Watchdog” that you can inspect or delete in Task "
                        "Scheduler.",
    "set.protect_hint_nonwindows": " (The watchdog needs Windows and is unavailable on "
                                    "this OS.)",
    "set.saved": "Saved. Monitoring changes apply on the next agent start. Restart the "
                 "agent for the port change to take effect.",
    "set.autostart_windows_only": " (autostart is only available on Windows)",
    "set.autostart_failed": " (autostart not configured: {error})",
    "set.watchdog_note": " (watchdog: {action} — {detail})",
    "set.token_copied": "Token copied to the clipboard.",
    "set.regen_title": "Regenerate token",
    "set.regen_body": "Browsers connected with the current token will be disconnected "
                      "and must be re-paired.\n\nContinue?",
    "set.new_token": "New token issued; the browser link restarted. Re-paste it in the "
                     "extension options.",
    "set.backup_failed": "Backup failed: {error}",
    "set.backup_written": "Backup written to {target}",
    "set.nothing_to_backup": "Nothing to back up yet.",
    # ---------------------------------------------------------- rule editor
    "re.title_edit": "Edit rule",
    "re.title_new": "New rule",
    "re.group_basics": "What to limit",
    "re.name": "Name",
    "re.name_placeholder": "e.g. Discord, League of Legends, YouTube",
    "re.type": "Type",
    "re.type_app": "Application (exe)",
    "re.type_game": "Game",
    "re.type_website": "Website (domain)",
    "re.known_game": "Known game",
    "re.pick_game": "Choose a known game…",
    "re.target": "Target",
    "re.target_placeholder": "discord.exe  •  league of legends.exe  •  youtube.com",
    "re.browse": "Browse…",
    "re.also_match": "Also match",
    "re.also_match_placeholder": "optional: extra exes, comma-separated (launcher, "
                                 "helper processes)",
    "re.group_limits": "Limits",
    "re.daily": "Daily limit",
    "re.session": "Per-session limit",
    "re.warn_me": "Warn me",
    "re.warn_placeholder": "minutes before the limit, comma-separated",
    "re.when_limit": "When the limit is reached",
    "re.mode": "Mode",
    "re.mode_normal": "Normal — instances already running may finish",
    "re.mode_strict": "Strict — close/block immediately and on relaunch",
    "re.group_schedule": "Schedule",
    "re.always": "Always active",
    "re.days": "Days",
    "re.only_between": "Only between",
    "re.action_block_site": "Block the site in the browser",
    "re.action_warn": "Only warn me",
    "re.action_wait": "Wait for the match to end, then close",
    "re.action_close": "Close the app",
    "re.action_prevent": "Prevent launching it",
    "re.hint_website": "Domains only, e.g. youtube.com — subdomains count too. Blocks "
                       "are enforced by the browser extension.",
    "re.hint_app": "The process name as Task Manager shows it, e.g. discord.exe. "
                   "Closing is graceful first (WM_CLOSE).",
    "re.hint_game": "The game's main executable. Limits can wait for a match to finish "
                    "when the game detector knows the session state.",
    "re.preset_hint": "{name} is watched by the '{id}' game detector: the limit can "
                      "wait for a match to end instead of interrupting it.",
    "re.caveat.valorant": " VALORANT's menu and match share one process, so 'wait' "
                          "means the limit applies once the game is closed. Pick Close "
                          "or Block instead if you want the limit immediately.",
    "re.caveat.repo": " R.E.P.O. has no local menu/match signal, so 'wait' means the "
                      "limit applies once the game is closed. Pick Close or Block "
                      "instead if you want the limit immediately.",
    "re.caveat.teamfight_tactics": " TFT shares its match process with League of "
                                   "Legends, so the limit waits while any League match "
                                   "is running (it never closes the wrong game).",
    "re.mode_normal_short": "Normal",
    "re.mode_strict_short": "Strict",
    "re.saved_title": "Rule saved",
    "re.saved_body": "{name}\n\n{type} · {target}\nDaily: {daily} · Session: {session}"
                     "\nAction: {action} · Mode: {mode}",
    "re.minutes": "{minutes} min",
    "re.none": "none",
    "re.err_need_name": "Give the rule a name.",
    "re.err_need_limits": "Set a daily limit, a session limit, or both.",
    "re.err_need_target": "Give the rule a target (exe name or domain).",
    "re.err_need_days": "Pick at least one day.",
    "re.err_window_order": "The window start and end must differ.",
    "re.pick_app_title": "Pick the application",
    "re.pick_app_filter": "Executables (*.exe);;All files (*)",
    # --------------------------------------------------------- onboarding tour
    "tour.step_counter": "Step {current} of {total}",
    "tour.next": "Next",
    "tour.back": "Back",
    "tour.skip": "Skip",
    "tour.done": "Got it",
    "tour.replay": "Show the guided tour again",
    "tour.welcome.title": "Welcome to Time Manager",
    "tour.welcome.body": "A 30-second tour of the dashboard. Use Next to move on, "
                         "Back to return, or Esc to close at any time.",
    "tour.rules.title": "Your rules live here",
    "tour.rules.body": "Each card is one app, game or website. The bar shows how much "
                       "of today's limit it has used; the chip tells you what the agent "
                       "is doing right now.",
    "tour.new_rule.title": "Create your first rule",
    "tour.new_rule.body": "Press “New rule” to pick an app, a website domain or a known "
                          "game from the presets, then set a daily limit, a per-session "
                          "limit, or both.",
    "tour.pause.title": "Pausing is time-boxed",
    "tour.pause.body": "Pause stops counting for 15–120 minutes. STRICT rules keep "
                       "running while paused — that is deliberate, and the banner will "
                       "say so.",
    "tour.extension.title": "Website limits need the browser extension",
    "tour.extension.body": "Website rules are enforced by a small Chrome/Edge extension "
                           "that reports the active tab. Until it is paired this panel "
                           "says “no browser connected” — press Help right here: it "
                           "shows the folder to load and the token to paste, both "
                           "copyable. The long version is docs/INSTALL-EXTENSION.md.",
    "tour.detectors.title": "Games are never interrupted",
    "tour.detectors.body": "The detector panel shows what is watching which game. A "
                           "limit that lands mid-match waits for the match to end, and "
                           "an unknown answer always means “wait”.",
    "tour.timeline.title": "Every enforcement is on the record",
    "tour.timeline.body": "Closes, blocks and skips appear here with the reason. "
                          "Anti-bypass findings (a killed agent, an erased counter, a "
                          "rolled-back clock) are shown above the rule list in red.",
    "tour.language.title": "العربية / English",
    "tour.language.body": "Click the globe in the header — or Settings ▸ Language — to "
                          "switch the whole app to Arabic. The layout flips to "
                          "right-to-left instantly and the choice is remembered.",
    "tour.finish.title": "That is the whole dashboard",
    "tour.finish.body": "Re-open this tour any time from the Guide button, the tray menu "
                        "or Settings. Everything stays on this machine — no account, no "
                        "cloud, no telemetry.",
}

AR: dict[str, str] = {
    # ---------------------------------------------------------------- format
    "fmt.unlimited": "غير محدود",
    "fmt.left": "متبقٍ \u200f{duration}",
    "fmt.resets": "يُعاد التعيين عند منتصف الليل ({duration})",
    "fmt.and": "و",
    "fmt.list_sep": "، ",
    "fmt.dur_hm": "{hours}س {minutes:02d}د",
    "fmt.dur_ms": "{minutes}د {secs:02d}ث",
    "fmt.dur_s": "{secs}ث",
    # -------------------------------------------------------------- schedules
    "schedule.always": "دائمًا",
    "schedule.every_day": "كل يوم",
    "schedule.weekdays": "الاثنين–الجمعة",
    "schedule.weekends": "السبت–الأحد",
    "schedule.custom": "مخصّص…",
    "day.MON": "اثنين",
    "day.TUE": "ثلاثاء",
    "day.WED": "أربعاء",
    "day.THU": "خميس",
    "day.FRI": "جمعة",
    "day.SAT": "سبت",
    "day.SUN": "أحد",
    # ------------------------------------------------------- rule vocabulary
    "action.close_app": "إغلاق التطبيق",
    "action.block": "حجب",
    "action.wait_session": "الانتظار حتى انتهاء المباراة",
    "action.warn_only": "تحذير فقط",
    "action.block_site": "حجب الموقع",
    "action.prevent_launch": "منع التشغيل",
    "type.app": "تطبيق",
    "type.game": "لعبة",
    "type.website": "موقع",
    "state.off": "مُعطّلة",
    "state.paused": "متوقّفة مؤقتًا (لا تُحتسب)",
    "state.outside_schedule": "خارج الجدول الزمني",
    "state.limit_warned": "انتهى الحد (تم التحذير)",
    "state.blocked_browser": "محجوب في المتصفح",
    "state.limit_after_match": "انتهى الحد — يُغلق بعد انتهاء المباراة",
    "state.limit_closed": "انتهى الحد — تم الإغلاق",
    "state.in_match": "داخل مباراة — يُنتظر انتهاؤها",
    "state.warning": "تحذير — متبقٍ {remaining}",
    "state.in_use": "قيد الاستخدام — متبقٍ {remaining}",
    "state.ready": "جاهزة",
    "session.text": "الجلسة {duration}",
    "session.none": "لا توجد جلسة مفتوحة",
    # ------------------------------------------------------------------ health
    "health.stopped": "متوقّف",
    "health.paused": "متوقّف مؤقتًا — متبقٍ {duration}",
    "health.errors": "يعمل مع {count} خطأ في المراقبة",
    "health.degraded": "يعمل (دعم محدود للنظام)",
    "health.monitoring": "المراقبة جارية",
    "status.tick": "نبضة {ticks}",
    "status.browsers": "{count} متصفح متصل",
    "status.no_browser": "لا يوجد متصفح متصل",
    "status.closes": "{count} إغلاق تطبيق اليوم",
    "status.sleep_gap": "آخر فجوة سكون {duration}",
    "status.last_error": "آخر خطأ: {text}",
    "pause.banner": "التتبع متوقّف مؤقتًا لمدة {duration}",
    "pause.until": " (حتى {time})",
    "pause.strict": " — القواعد الصارمة تبقى فعّالة: {names}",
    # ---------------------------------------------------------------- security
    "sec.unclean": "توقّف مدير الوقت بشكل غير متوقّع (بدأت آخر جلسة في {when}) — لم "
                   "تُطبَّق الحدود حتى هذه المرة.",
    "sec.clock": "ساعة النظام متأخّرة بمقدار {duration} عن آخر تشغيل للوكيل — ربما تم "
                 "التراجع عن الحدود اليومية.",
    "sec.tamper": "{rule}: مُسح الاستخدام المسجَّل ليوم {day} أو تم التراجع عنه — "
                  "يُحتسب الحد من {duration} المحفوظة بدلًا من ذلك.",
    "sec.silent": "لا توجد إضافة متصفح متصلة — حدود المواقع غير مُفعَّلة لـ: {rules}.",
    "sec.startup_repaired": "أُعيد تفعيل بدء التشغيل مع ويندوز لأن قواعد صارمة مُفعَّلة.",
    # ------------------------------------------------------------- side panels
    "browser.none": "لا يوجد متصفح متصل — افتح إعدادات الإضافة والصق الرمز "
                    "(python -m app.main --show-token).",
    "browser.active": "النشط: {domains}",
    "browser.connected_idle": "متصل — لا موقع متتبَّع في المقدمة",
    "detector.none": "لا توجد كاشفات ألعاب محمَّلة — تُحتسب الألعاب مثل أي تطبيق آخر.",
    "detector.quarantined": "معزول — لن تُغلق المباريات",
    "detector.in_session": "داخل جلسة: {detail}",
    "detector.no_session": "لا جلسة: {detail}",
    "detector.unknown": "غير معروف: {detail}",
    "detector.waiting": "في انتظار لعبة",
    "detector.budget": "الميزانية {ms} ملي ثانية لكل فحص؛ {quarantined} معزول",
    "detector.rejected": "إضافة مرفوضة: {origin} — {reason}",
    "link.unavailable": "رابط المتصفح غير متاح",
    "link.listening": "يستمع على 127.0.0.1:{port}",
    "link.messages": "وارد {in_count} / صادر {out_count} رسالة",
    "link.rate_limited": "، {count} محدود المعدّل",
    "audit.close_app": "إغلاق تطبيق",
    "audit.block_website": "حجب موقع",
    "audit.wait_session": "انتظار المباراة",
    "audit.warn": "تحذير",
    "audit.notify": "إشعار",
    "title.window": "مدير الوقت — {state}",
    # ---------------------------------------------------------------- widgets
    "card.edit": "تعديل",
    "card.disable": "تعطيل",
    "card.enable": "تفعيل",
    "card.paused_suffix": " · متوقّفة مؤقتًا",
    # ------------------------------------------------------------ main window
    "mw.title": "مدير الوقت",
    "mw.starting": "جارٍ البدء…",
    "mw.chip_starting": "يبدأ",
    "mw.pause": "إيقاف مؤقت",
    "mw.pause_for": "إيقاف مؤقت لمدة {minutes} دقيقة",
    "mw.pause_until_tomorrow": "إيقاف حتى الغد (بحد أقصى {hours} ساعة)",
    "mw.resume": "استئناف التتبع",
    "mw.new_rule": "قاعدة جديدة",
    "mw.settings": "الإعدادات",
    "mw.guide": "الدليل",
    "mw.rules_title": "قواعدك",
    "mw.rules_meta": "{count} قاعدة · {active} مُفعَّلة",
    "mw.panel_browser": "إضافة المتصفح",
    "mw.panel_link": "رابط الوكيل",
    "mw.panel_detectors": "كاشفات الألعاب",
    "mw.panel_timeline": "آخر عمليات التنفيذ",
    "mw.panel_help": "مساعدة",
    "help.title": "تثبيت إضافة المتصفح",
    "help.intro": "قواعد المواقع ينفّذها امتداد صغير لمتصفح Chrome أو Edge. المهمة كلها "
                  "ثلاث خطوات وتستغرق دقيقتين تقريبًا. ولا شيء يغادر هذا الجهاز — "
                  "الإضافة تتحدث مع 127.0.0.1 فقط.",
    "help.step1": "افتح ‎chrome://extensions‎ في Chrome (و‎edge://extensions‎ في Edge)، "
                  "وشغّل «وضع المطوّر» (Developer mode)، ثم اضغط «Load unpacked».",
    "help.step2": "اختر هذا المجلد — المجلد الذي يحتوي على ‎manifest.json‎:",
    "help.path_note": "في التثبيت المحزوم يوجد داخل مجلد التطبيق بجوار "
                      "TimeManagerTray.exe؛ ويطبعه الأمر "
                      "TimeManager.exe --show-extension-path.",
    "help.path_missing": "ملف ‎manifest.json‎ غير موجود في هذا المجلد — هذه النسخة "
                         "تنقصها بيانات الإضافة. أعد تثبيت التطبيق (أو شغّله من "
                         "المصدر) لاستعادتها.",
    "help.step3": "افتح «إعدادات» الإضافة، والصق رمز الاقتران، ثم اضغط "
                  "«Save & test»:",
    "help.token_note": "المضيف 127.0.0.1 · المنفذ {port}. الرمز سرّ خاص بهذا الجهاز: "
                       "تجديده من الإعدادات ▸ إضافة المتصفح يفصل كل المتصفحات حتى "
                       "تُعيد الاقتران.",
    "help.copy_path": "نسخ المسار",
    "help.copy_token": "نسخ الرمز",
    "help.path_copied": "تم نسخ مسار المجلد إلى الحافظة.",
    "help.token_copied": "تم نسخ رمز الاقتران إلى الحافظة.",
    "help.open_folder": "فتح المجلد",
    "help.close": "إغلاق",
    "help.footer": "بعد الاقتران تعرض لوحة «إضافة المتصفح» اسم المتصفح وعدّادات "
                   "الرسائل. وفي صفحة «الدليل» داخل الإضافة (الإعدادات ▸ Guide) صور "
                   "وجدول لاستكشاف الأخطاء.",
    "mw.empty": "لا توجد قواعد بعد.\n\nتتبّع تطبيقًا أو لعبة أو موقعًا لتحديد حد يومي "
                "أو حد لكل جلسة.",
    "mw.empty_action": "أنشئ قاعدتك الأولى",
    "mw.nothing_enforced": "لم يُنفَّذ أي إجراء اليوم بعد.",
    "mw.quit_title": "إغلاق مدير الوقت",
    "mw.quit_body": "هل تريد إغلاق الوكيل؟\n\nستتوقف المراقبة والتنفيذ حتى تعيد "
                    "تشغيله.",
    "mw.paused_chip": "متوقّف مؤقتًا · {duration}",
    "mw.tooltip_profile": "الملف الشخصي: {path}",
    # -------------------------------------------------------------------- tray
    "tray.open_dashboard": "فتح لوحة التحكم",
    "tray.pause_tracking": "إيقاف التتبع مؤقتًا",
    "tray.minutes": "{minutes} دقيقة",
    "tray.resume": "استئناف",
    "tray.rules": "القواعد والاستخدام…",
    "tray.show_guide": "عرض الدليل",
    "tray.language": "اللغة",
    "tray.quit": "إغلاق مدير الوقت",
    "tray.tooltip": "مدير الوقت — {status}\n{line}",
    # ------------------------------------------------------- settings dialog
    "set.title": "إعدادات مدير الوقت",
    "set.group_language": "اللغة",
    "set.language": "اللغة",
    "set.language_hint": "يُطبَّق فورًا على لوحة التحكم وقائمة أيقونة النظام. اختيار "
                         "العربية يحوّل التطبيق بالكامل إلى الاتجاه من اليمين إلى "
                         "اليسار.",
    "set.group_monitoring": "المراقبة",
    "set.check_every": "الفحص كل",
    "set.foreground_only": "احتسب التطبيق فقط أثناء وجوده في المقدمة",
    "set.grace": "مهلة بعد فقدان التركيز",
    "set.idle": "تجاهل وقت الخمول بعد",
    "set.idle_off": "معطّل",
    "set.browser_foreground": "اشترط وجود المتصفح في المقدمة لاحتساب وقت المواقع",
    "set.close_timeout": "مهلة الإغلاق التدريجي",
    "set.group_browser": "إضافة المتصفح",
    "set.agent_port": "منفذ الوكيل",
    "set.token": "رمز الاقتران",
    "set.show": "إظهار",
    "set.hide": "إخفاء",
    "set.copy": "نسخ",
    "set.regen": "تجديد الرمز",
    "set.hint": "الصق هذا الرمز في صفحة إعدادات الإضافة. تجديد الرمز يُلغي الرمز "
                "القديم ويعيد تشغيل رابط المتصفح — وستحتاج إلى إعادة اقتران كل متصفح.",
    "set.group_general": "عام",
    "set.startup": "تشغيل مدير الوقت مع ويندوز",
    "set.notifications": "إظهار إشعارات سطح المكتب للتحذيرات والحدود",
    "set.backup_now": "إنشاء نسخة احتياطية الآن",
    "set.data": "البيانات",
    "set.group_strict": "حماية الوضع الصارم",
    "set.watchdog": "أعد تشغيل الوكيل إذا توقف (مهمة مجدولة تفحص كل دقيقة)",
    "set.protect_hint": "كل ما يفعله مدير الوقت لمنع التحايل ظاهر في سجل التدقيق: "
                        "القواعد الصارمة تحتفظ بمدخل بدء التشغيل في ويندوز (ويُعاد "
                        "إضافته إذا حُذف)، ولا يمكن تصفير الاستخدام بتعديل قاعدة "
                        "البيانات، والوكيل الذي أُغلق قسرًا يُبلّغ عن الفجوة في "
                        "التشغيل التالي. المهمة المجدولة تُنشئ مهمة باسم «Time "
                        "Manager Watchdog» يمكنك فحصها أو حذفها من مجدول المهام.",
    "set.protect_hint_nonwindows": " (تحتاج المهمة المجدولة إلى ويندوز وهي غير متاحة "
                                    "على هذا النظام.)",
    "set.saved": "تم الحفظ. تُطبَّق تغييرات المراقبة عند بدء الوكيل في المرة القادمة. "
                 "أعد تشغيل الوكيل ليأخذ تغيير المنفذ مفعوله.",
    "set.autostart_windows_only": " (بدء التشغيل التلقائي متاح على ويندوز فقط)",
    "set.autostart_failed": " (لم يُضبط بدء التشغيل التلقائي: {error})",
    "set.watchdog_note": " (المهمة المجدولة: {action} — {detail})",
    "set.token_copied": "تم نسخ الرمز إلى الحافظة.",
    "set.regen_title": "تجديد الرمز",
    "set.regen_body": "سيتم فصل المتصفحات المتصلة بالرمز الحالي ويلزم إعادة "
                      "اقترانها.\n\nهل تريد المتابعة؟",
    "set.new_token": "تم إصدار رمز جديد؛ أُعيد تشغيل رابط المتصفح. الصقه مرة أخرى في "
                     "إعدادات الإضافة.",
    "set.backup_failed": "فشل النسخ الاحتياطي: {error}",
    "set.backup_written": "تم إنشاء نسخة احتياطية في {target}",
    "set.nothing_to_backup": "لا يوجد ما يمكن نسخه بعد.",
    # ---------------------------------------------------------- rule editor
    "re.title_edit": "تعديل قاعدة",
    "re.title_new": "قاعدة جديدة",
    "re.group_basics": "ما الذي تريد تحديده",
    "re.name": "الاسم",
    "re.name_placeholder": "مثال: Discord أو League of Legends أو YouTube",
    "re.type": "النوع",
    "re.type_app": "تطبيق (exe)",
    "re.type_game": "لعبة",
    "re.type_website": "موقع (نطاق)",
    "re.known_game": "لعبة معروفة",
    "re.pick_game": "اختر لعبة معروفة…",
    "re.target": "الهدف",
    "re.target_placeholder": "discord.exe  •  league of legends.exe  •  youtube.com",
    "re.browse": "استعراض…",
    "re.also_match": "مطابقة أيضًا",
    "re.also_match_placeholder": "اختياري: ملفات تنفيذية إضافية مفصولة بفواصل "
                                 "(المشغّل، العمليات المساعدة)",
    "re.group_limits": "الحدود",
    "re.daily": "حد يومي",
    "re.session": "حد لكل جلسة",
    "re.warn_me": "نبّهني",
    "re.warn_placeholder": "دقائق قبل انتهاء الحد، مفصولة بفواصل",
    "re.when_limit": "عند بلوغ الحد",
    "re.mode": "الوضع",
    "re.mode_normal": "عادي — يُترك للنُسخ العاملة حاليًا أن تُكمل",
    "re.mode_strict": "صارم — الإغلاق/الحجب فورًا وعند إعادة التشغيل",
    "re.group_schedule": "الجدول الزمني",
    "re.always": "فعّالة دائمًا",
    "re.days": "الأيام",
    "re.only_between": "فقط بين",
    "re.action_block_site": "حجب الموقع في المتصفح",
    "re.action_warn": "تحذيري فقط",
    "re.action_wait": "الانتظار حتى انتهاء المباراة ثم الإغلاق",
    "re.action_close": "إغلاق التطبيق",
    "re.action_prevent": "منع تشغيله",
    "re.hint_website": "النطاقات فقط، مثل youtube.com — وتُحتسب النطاقات الفرعية "
                       "أيضًا. الحجب تنفّذه إضافة المتصفح.",
    "re.hint_app": "اسم العملية كما يظهر في «إدارة المهام»، مثل discord.exe. "
                   "الإغلاق يتم بلطف أولًا (WM_CLOSE).",
    "re.hint_game": "الملف التنفيذي الرئيسي للعبة. يمكن للحد أن ينتظر انتهاء المباراة "
                    "عندما تعرف كاشفة اللعبة حالة الجلسة.",
    "re.preset_hint": "{name} يتابعها كاشف اللعبة '{id}': يمكن للحد أن ينتظر انتهاء "
                      "المباراة بدل أن يقطعها.",
    "re.caveat.valorant": " قائمة VALORANT والمباراة يعملان في عملية واحدة، لذا يعني "
                          "«الانتظار» أن يُطبَّق الحد بعد إغلاق اللعبة. اختر «إغلاق» أو "
                          "«حجب» إن أردت الحد فورًا.",
    "re.caveat.repo": " لا يوفّر R.E.P.O. أي إشارة محلية للقائمة أو المباراة، لذا يعني "
                      "«الانتظار» أن يُطبَّق الحد بعد إغلاق اللعبة. اختر «إغلاق» أو "
                      "«حجب» إن أردت الحد فورًا.",
    "re.caveat.teamfight_tactics": " تشترك TFT في عملية المباراة مع League of Legends، "
                                   "لذا ينتظر الحد ما دامت أي مباراة في League جارية "
                                   "(ولا يُغلق اللعبة الخطأ أبدًا).",
    "re.mode_normal_short": "عادي",
    "re.mode_strict_short": "صارم",
    "re.saved_title": "تم حفظ القاعدة",
    "re.saved_body": "{name}\n\n{type} · {target}\nيومي: {daily} · لكل جلسة: {session}"
                     "\nالإجراء: {action} · الوضع: {mode}",
    "re.minutes": "{minutes} دقيقة",
    "re.none": "لا شيء",
    "re.err_need_name": "أعطِ القاعدة اسمًا.",
    "re.err_need_limits": "حدّد حدًّا يوميًّا أو حدًّا لكل جلسة أو كليهما.",
    "re.err_need_target": "حدّد هدفًا للقاعدة (اسم ملف تنفيذي أو نطاق).",
    "re.err_need_days": "اختر يومًا واحدًا على الأقل.",
    "re.err_window_order": "يجب أن يختلف وقت بداية النافذة عن نهايتها.",
    "re.pick_app_title": "اختر التطبيق",
    "re.pick_app_filter": "الملفات التنفيذية (*.exe);;كل الملفات (*)",
    # --------------------------------------------------------- onboarding tour
    "tour.step_counter": "الخطوة {current} من {total}",
    "tour.next": "التالي",
    "tour.back": "السابق",
    "tour.skip": "تخطّي",
    "tour.done": "فهمت",
    "tour.replay": "إعادة عرض الجولة التعريفية",
    "tour.welcome.title": "مرحبًا بك في مدير الوقت",
    "tour.welcome.body": "جولة سريعة مدتها ٣٠ ثانية في لوحة التحكم. استخدم «التالي» "
                         "للمتابعة و«السابق» للرجوع، أو Esc للإغلاق في أي وقت.",
    "tour.rules.title": "قواعدك هنا",
    "tour.rules.body": "كل بطاقة تخصّ تطبيقًا أو لعبة أو موقعًا. الشريط يوضح ما "
                       "استُهلك من حد اليوم، والشارة تخبرك بما يفعله الوكيل الآن.",
    "tour.new_rule.title": "أنشئ قاعدتك الأولى",
    "tour.new_rule.body": "اضغط «قاعدة جديدة» لاختيار تطبيق أو نطاق موقع أو لعبة من "
                          "القوالب الجاهزة، ثم حدّد حدًّا يوميًّا أو حدًّا لكل جلسة أو "
                          "كليهما.",
    "tour.pause.title": "الإيقاف المؤقت محدود بوقت",
    "tour.pause.body": "الإيقاف المؤقت يوقف الاحتساب من ١٥ إلى ١٢٠ دقيقة. القواعد "
                       "الصارمة تبقى فعّالة أثناء الإيقاف — وهذا مقصود، وسيظهر تنبيه "
                       "بذلك.",
    "tour.extension.title": "حدود المواقع تحتاج إضافة المتصفح",
    "tour.extension.body": "قواعد المواقع تنفّذها إضافة صغيرة لمتصفح Chrome أو Edge "
                           "تُبلّغ عن علامة التبويب النشطة. إلى أن يتم الاقتران ستقول "
                           "هذه اللوحة «لا يوجد متصفح متصل» — اضغط «مساعدة» هنا: تعرض "
                           "المجلد المطلوب تحميله والرمز المطلوب لصقه، وكلاهما قابل "
                           "للنسخ. والشرح الكامل في docs/INSTALL-EXTENSION.md.",
    "tour.detectors.title": "لا تُقاطَع الألعاب أبدًا",
    "tour.detectors.body": "لوحة الكاشفات توضح من يراقب أي لعبة. الحد الذي ينتهي في "
                           "منتصف مباراة ينتظر انتهاءها، والجواب المجهول يعني دائمًا "
                           "«الانتظار».",
    "tour.timeline.title": "كل إجراء مُسجَّل",
    "tour.timeline.body": "الإغلاق والحجب والتخطّي تظهر هنا مع السبب. ونتائج مكافحة "
                          "التحايل (وكيل أُغلق، عدّاد مُمحى، ساعة متراجعة) تظهر بالأحمر "
                          "أعلى قائمة القواعد.",
    "tour.language.title": "العربية / English",
    "tour.language.body": "اضغط أيقونة الكرة الأرضية في الأعلى — أو الإعدادات ▸ اللغة — "
                          "لتحويل التطبيق بالكامل إلى العربية. ينقلب الاتجاه إلى "
                          "اليمين‑إلى‑اليسار فورًا ويُحفظ اختيارك.",
    "tour.finish.title": "هذه هي لوحة التحكم كاملة",
    "tour.finish.body": "يمكنك إعادة هذه الجولة في أي وقت من زر «الدليل» أو قائمة أيقونة "
                        "النظام أو الإعدادات. كل شيء يبقى على هذا الجهاز — بلا حساب ولا "
                        "سحابة ولا تتبّع.",
}

CATALOGS: dict[str, dict[str, str]] = {"en": EN, "ar": AR}
