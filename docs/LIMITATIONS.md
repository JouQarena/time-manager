# Technical limitations & honest trade-offs

## Game detection coverage (Phase 8)

- **VALORANT: menu and match are one process.** The detector waits whenever
  the game is running and enforces once it closes (+30 s settle). Match-level
  detection would need Riot's authenticated client API — deliberately not
  used; documented as future opt-in plugin work.
- **R.E.P.O.: no public session signal exists at all** (Unity title, no API,
  Player.log has no documented contract). Same coarse-but-safe behaviour as
  VALORANT, which is exactly what the spec asks for when reliable detection
  is impossible.
- **TFT shares the League match process.** A TFT rule can only tell "a match
  process is alive" (wait) from "it has verifiably ended" (enforce). Riot's
  Live Client API `gameMode` corroborates a TFT match but the guard never
  closes the shared process on indirect evidence — so an LoL match also makes
  a TFT rule wait. Safe direction, documented cost.
- **Log formats are not contracts.** `ShooterGame.log` tokens, Unity
  `Player.log` freshness and Riot's log phases are version-tolerant
  heuristics: they can only delay enforcement, never cause it, and any read
  failure is "no evidence".
- **The automated matrix proves logic, not live games.** It runs on scripted
  machines; the on-Windows manual matrix in `docs/TESTING.md` is the step
  that validates each detector against real game states.

## Anti-bypass hardening (Phase 7)

- **Detection, not prevention.** Killing the agent always works *once*; what
  Phase 7 adds is that the kill is never free: the next start reports the gap
  (`UNEXPECTED_STOP`), the audit trail records it, and the optional watchdog
  revives the agent within about a minute. An admin who kills the agent,
  deletes the watchdog task and never starts the agent again has won — that
  is the documented ceiling of a user-level tool (no kernel, no stealth).
- **Full profile wipe = reinstall.** Deleting the database *and* the floor
  file *and* the config leaves nothing to detect with. The periodic backups
  in `%APPDATA%\TimeManager\backups` survive a casual wipe; a determined
  daily wiper is out of scope for self-control software.
- **The usage floor defends table edits, not rule deletion.** It defeats
  `UPDATE daily_usage SET total_seconds=0` for STRICT rules; deleting the
  rule itself is a separate visible action (rules CRUD + audit trail).
- **Watchdog granularity is one minute** — Task Scheduler's smallest
  repeating interval. A kill-to-revive window of up to ~60 s remains and is
  reported by the lifecycle audit afterwards.
- **Startup repair only fires when STRICT rules are armed** — a user on
  Normal-mode rules keeps full control of the autostart setting.
- **BypassWatch sees only Chrome/Edge** (the Phase 4 extension scope). A
  browser the extension does not support is invisible, and its time is not
  counted — the same limitation as Phase 4, now made visible for the
  browsers we do support.
- **The clock-regression check needs a previous sighting.** The first run on
  a machine has no floor file yet and cannot detect a rolled-back clock;
  from the second run on it can (90 s tolerance for NTP/dual-boot drift).

## Platform
- **Windows 10/11 only for enforcement.** Core engine + tests run anywhere,
  but process closing, foreground detection, tray, startup, and suspend/resume
  need Win32 (`pywin32`). Off-Windows these raise `WindowsOnlyError` with a
  clear message instead of pretending to work.
- **No kernel protection.** A determined admin can always kill the agent.
  Strict mode raises the bar (relaunch-guard, startup repair, unexpected-exit
  detection, clock-skew warnings) but is bypassable by design — this is a
  productivity tool, not parental-control malware. This is documented in-app.

## Time & clock
- Durations use `time.monotonic()` (sleep-aware on Windows: monotonic keeps
  counting across sleep on Win10+; we additionally listen for
  `WM_POWERBROADCAST` suspend/resume in Phase 3 and cut sessions at suspend).
- Calendar day uses the local wall clock. If the user changes the clock:
  - Backward jump > 60 s → logged to `clock_log`, open sessions are closed at
    the last sane timestamp, a notification warns the user, strict rules keep
    the *higher* of monotonic-implied vs wall-implied usage (fail-safe toward
    the limit, never granting free time).
  - Forward jump / DST / timezone change → day boundary recomputed; usage
    already recorded is never deleted; a day may legitimately show > limit
    after a forward jump (documented, not "corrected" by deleting data).
- Sleep/hibernate: no time accrues while suspended (session split on resume).

## Application monitoring
- Foreground detection = `GetForegroundWindow → GetWindowThreadProcessId →
  exe name`. Fullscreen games, UWP apps (`ApplicationFrameHost.exe`), and
  apps with multiple exes need per-target tuning: rules store one primary exe
  plus optional `extra_executables` (Phase 3). UWP resolution via
  `GetApplicationUserModelId` is best-effort and documented.
- "Close application" = graceful `WM_CLOSE` to top-level windows first,
  escalate to `TerminateProcess` after a timeout only for that rule's exes.
  Unsaved work prompts may appear — by design we prefer graceful close.
- Relaunch-guard (strict): polling loop notices the exe restarting within the
  guard window and closes it again + notifies. Millisecond races are possible;
  acceptable for a self-control tool.

## Websites
- Tracking requires the extension installed **and** connected **and** the token
  configured. Without it, website rules show `NO_EXTENSION` status and cannot
  accrue or block — the dashboard says so explicitly.
- Only active+focused tab time counts. Background video/audio keeps playing
  but accrues nothing (documented; `audible` is reported for a future
  "count audible background" option, default off).
- Blocking is in-extension (block page + navigation cancel via
  `webNavigation`/`declarativeNetRequest` where available). A user can disable
  the extension — strict mode detects "extension expected but silent" and
  notifies, but cannot force a browser to keep an extension enabled.
- Private/incognito windows: extension must be explicitly allowed there by the
  user; otherwise that traffic is invisible (browser platform limit).

## League of Legends / games (Phase 6)
- **The verdict table is documented in `docs/PHASE6.md`.** Summary: the match
  process (`League of Legends.exe` — loading screen, in game, reconnecting) is
  the ground truth; the client alone is not, and enforcement waits
  `game_settle_seconds` (30 s) after the match process disappears so a reconnect
  is never mistaken for "the game is over".
- **Extra signals can only add caution.** Riot's Live Client Data API
  (`127.0.0.1:2999`, self-signed, live matches only), the last client-log phase
  and match-log freshness can push a verdict *towards* waiting; none of them can
  cause enforcement, and all of them degrade to "no evidence" on any failure.
- **Version tolerance is deliberate, not contractual.** Riot can rename
  processes or change log formats; the process table keeps working, and the
  detector falls back to it. A fresh *client* log is never treated as "in a
  match" (it is written in the lobby too).
- **A broken detector means no enforcement for its game, not a closed game.**
  Crash → unknown, hang → unknown (probe budget 300 ms, one abandoned thread at
  most), repeated failures → quarantined and surfaced in the dashboard and
  `--status-json`. `--detector-selftest` exercises each of those paths.
- **Settle window is a trade-off, and it is a setting.** Too short risks closing
  a client during a reconnect; too long delays a post-game block. 30 s default.
- **Only League of Legends ships a detector.** The host/loader/fail-safe
  machinery is game-agnostic; other games enforce on their own clock until a
  plugin (or a later phase) provides a detector.

## Privacy ceiling
- The agent necessarily sees which tracked exes/domains are active (that's the
  product). It never sees full URLs, page bodies, or other apps' windows
  beyond exe+title-needed-for-debugging (title logging is opt-in, default off).

## Browser link (Phase 4)

- **MV3 service-worker eviction.** Chrome may suspend the extension worker while
  the browser idles; heartbeats stop and the agent stops counting website time
  after 15 s. It resumes on the next tab event, alarm or browser restart. The
  failure mode is "less time counted", never "more".
- **Two browser connections = one clock.** Every connected browser reports its
  own active tab; a domain counts as active if *any* live browser is showing it
  while a browser is the foreground app. Two windows side by side on the same
  domain are counted once, as they should be.
- **Other Chrome profiles.** The extension runs per profile. A second profile
  that never loaded the extension cannot be seen, and its time is not counted.
- **Blocking is page-level, not network-level.** The block screen appears when a
  page starts loading (sub-second) and media is paused; it is not a firewall —
  a page that already buffered media keeps it in memory, and non-HTML requests
  are not intercepted.
- **`chrome://`, extension pages, view-source and IP-literal sites** are never
  reported or blocked (they carry no trackable domain).
- **The agent must be running.** With the agent stopped, nothing is counted and
  nothing is blocked — by design (no silent background enforcement without the
  visible agent).
- **Loopback only.** The server binds `127.0.0.1`; a browser on another machine
  cannot connect (and the token would have to be copied manually — do not).
- **Process-level kill is Windows-only.** All website enforcement paths work on
  any OS; the app/game `CLOSE` path needs Windows (see the Phase 3 section).

## Desktop app (Phase 5)

- **One instance per profile.** A second `--gui` (or `--monitor`) exits with
  code 3 and a dialog instead of sharing the database. The lock is a
  `<db>.lock` file holding `pid:epoch`; a lock left by a crashed process is
  taken over once that PID is gone. Two *profiles* (different `--db`) can run
  side by side, but each needs its own browser port and pairing token.
- **Pause is time-boxed on purpose.** 1–480 minutes, always visible in the
  banner and tray tooltip, always written to `clock_log`, and never able to
  suppress a STRICT rule. There is no "pause forever" switch: an off switch
  with no end is how a tool like this stops being used.
- **A pause seals open sessions.** Counting stops at the tick the pause starts
  (the session in progress is closed, so its last interval is not billed), and
  resuming re-opens the session — the first interval after a resume is
  therefore billed from the following tick. This is the same ±1-interval
  granularity the tracker has everywhere.
- **The dashboard is a poller, not a live stream.** It refreshes once a second,
  so counters can lag by up to one monitoring interval (1 s by default).
- **The GUI is Windows-first.** Off-Windows it starts, but the header reads
  `Running (limited OS support)`: no foreground/idle detection means app
  activity is never attributed. Use `--simulate` for a deterministic demo.
- **Notifications are best-effort.** They depend on the OS tray; if there is no
  tray (some Linux sessions, `QT_QPA_PLATFORM=offscreen`) they are recorded and
  silently dropped — enforcement never depends on them.
- **`--status-json` is read-only by design.** While a live agent owns the
  profile it reports that agent's port/lock rather than claiming them itself
  (`read_only: true`, `ipc.note` explains). It cannot inject actions, change
  rules or pause anything.
- **Screenshots are rendered, not captured.** `--gui-shot` drives the real
  widgets offscreen on a fake clock; they show layout and wording, not a
  particular machine's numbers.
