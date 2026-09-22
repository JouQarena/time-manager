# Phase 8 — Testing & Game Detection Coverage (v0.8.0)

Status: **complete (automated)**. This phase expands game-session detection
from League of Legends to four games — **VALORANT**, **R.E.P.O.** and
**Teamfight Tactics** join the existing LoL detector — and adds the full test
matrix the spec requires.

**Honesty note, up front.** The spec says: *"Only mark active-session
detection as successful if it has been tested against real game states."*
Everything here is verified by 60+ automated tests and the 21-step
`--detector-selftest` against **scripted machines** (real detectors, real
guard, real state machine, scripted process tables/logs/APIs). That proves the
logic; it cannot prove Riot/semiwork's next patch. The **on-Windows manual
matrix** at the end of `docs/TESTING.md` is the step that turns a ✓ below into
a field-verified ✓ — run it once per game and record the results.

## 1. What each game actually is, on Windows

| Game | Processes (all lowercased) | The trap | Verified facts used |
| --- | --- | --- | --- |
| League of Legends | `league of legends.exe` (match), `leagueclientux.exe`, `leagueclient.exe`, `riotclientservices.exe` (client) | client ≠ match | Phase 6: match process exists only during matches; LCU phases; Live Client API `127.0.0.1:2999`; `Logs/GameLogs/*.log` |
| VALORANT | `valorant-win64-shipping.exe` (game: menu, range AND matches), `riotclient*` (launcher), `vgc.exe`/`vgm.exe`/`vgtray.exe` (Vanguard, runs at boot) | **one process for menu and match** | game exe at `...\VALORANT\live\ShooterGame\Binaries\Win64\`; `ShooterGame.log` in `%LOCALAPPDATA%\VALORANT\Saved\Logs\`; Riot's match-state API is authenticated/private → out of scope |
| R.E.P.O. | `repo.exe` (game: menu, shop AND levels), `steam.exe`/`steamwebhelper.exe` (launcher) | **no public session signal at all** | Unity title (semiwork): `Player.log` under `%USERPROFILE%\AppData\LocalLow\semiwork\Repo\`; no API; log has no documented session contract |
| TFT | same as LoL — a TFT match runs in `League of Legends.exe` | **the match process is shared with every LoL mode** | Live Client API `gameData.gameMode`; LCU phases; `Logs/GameLogs/` (TFT writes there too) |

## 2. Detectors: method, reliability, failure modes

Common contract (unchanged since Phase 6): a detector returns
`SessionVerdict(in_session, confidence 0..1)`. The enforcement engine may act
**only** on `(in_session=False, confidence=1.0)`. Everything else waits. A
detector that crashes/hangs/gets quarantined becomes "unknown" → wait
(`DetectorHost`, Phase 6).

### 2.1 `LeagueOfLegendsDetector` (maintained, Phase 6 verdict table unchanged)

- **Method:** match process decisive; settle window (30 s) for reconnects;
  Live Client API + client-log phase + game-log freshness as caution-only
  evidence. See `docs/PHASE6.md`.
- **Windows APIs:** process table (psutil `process_iter`), local HTTPS loopback
  (2999), file mtimes.
- **Reliability:** high — the match process is a hard signal.
- **False positives (waits too long):** a TFT/Training-Range match reads as an
  LoL match (shared process) — safe direction, documented below.
- **False negatives (kills wrongly):** none known; every uncertain path waits.
- **Safe fallback:** unknown → WAIT.

### 2.2 `ValorantDetector` (new)

- **Method:** `VALORANT-Win64-Shipping.exe` present → **in session, confidence
  0.5** (menu/match indistinguishable) → WAIT. Gone < 30 s → unknown → WAIT.
  Gone ≥ 30 s (+ only launcher left, or nothing) → decisive not-in-session.
  `ShooterGameLogWatcher` reads the `ShooterGame.log` tail for tolerant
  match-lifecycle tokens (`matchid`, `gamemap`, `provisioningflow`,
  `gamesession`, `mapgamemode`, `loadingmap`): corroboration only — can delay
  enforcement, never cause it (Riot doesn't document the format).
- **Windows APIs:** process table, file mtime + tail read.
- **Reliability:** process-level high; match-vs-menu honestly **coarse**.
- **False positives:** any time the game is merely open in the menu → WAIT
  (by design). Vanguard running is explicitly *not* evidence (it runs at boot).
- **False negatives:** none known that lead to a kill; the token list could
  miss a renamed log event (cost: less corroboration, never a wrong kill).
- **Safe fallback:** unknown → WAIT. Practical consequence: a
  `WAIT_FOR_SESSION_END` VALORANT rule enforces when the game closes, not the
  moment the match ends. Riot's authenticated local client API is the
  documented future work for match-granularity enforcement.

### 2.3 `RepoDetector` (new)

- **Method:** `REPO.exe` present → in session 0.5 → WAIT (the spec's own rule
  for R.E.P.O.: *"if reliable session detection is technically impossible …
  document that limitation and fall back to the safest available behavior"*).
  Gone < 30 s → unknown → WAIT (covers crash-to-desktop right after a level).
  Gone ≥ 30 s → decisive not-in-session. Steam alone is never evidence.
  `PlayerLogWatcher` (Player.log freshness) is detail-only corroboration.
- **Windows APIs:** process table, file mtime.
- **Reliability:** process-level high; session-level **coarse by necessity**.
- **False positives:** game open in menu/shop → WAIT (by design).
- **False negatives:** none known leading to a kill.
- **Safe fallback:** unknown → WAIT. Practical consequence: a
  `WAIT_FOR_SESSION_END` R.E.P.O. rule enforces on game close.

### 2.4 `TftDetector` (new)

- **Method:** the match process is **shared**, so:
  - process up + Live API `gameData.gameMode` contains `tft` → in session 1.0;
  - process up, mode not verifiable (no API, or `CLASSIC` = a sibling LoL
    match!) → in session 0.5 → **WAIT — enforcing could kill the wrong game**;
  - process gone < 30 s → unknown; client-log match phases (`InProgress`,
    `ReadyCheck`, `WaitingForStats`, …) or fresh game logs → 0.5;
  - client-only, settled, or nothing running → decisive not-in-session.
  Reuses the Phase 6 signal providers (`LiveClientApi`, `ClientPhaseReader`,
  `GameLogWatcher`) — signal code shared, verdict logic independent.
- **Windows APIs:** as LoL.
- **Reliability:** high for "safe"; mode attribution depends on the API
  answering (corroboration-grade, as everywhere in this project).
- **False positives:** an LoL match makes a TFT rule WAIT (safe; documented).
- **False negatives:** none known leading to a kill while a match of *any*
  kind is visible.
- **Safe fallback:** unknown → WAIT.

### 2.5 Routing & the fail-safe merge (`GameSessionGuard`)

TFT and LoL route to **both** detectors (identical exe sets).
`GameSessionGuard.for_rule` now probes **all** candidates and merges:

```
any candidate: in-session AND confidence 1.0   -> in session (WAIT, certain)
any candidate: confidence < 1.0 (either way)   -> unknown   (WAIT)
all candidates: not-in-session AND certain     -> not in session (may enforce)
a candidate raises                              -> unknown   (WAIT)
```

With a single matching detector this is byte-for-byte the Phase 6 behaviour
(pinned by tests); with two, the safer verdict always wins.

## 3. Test matrix (automated; scripted machine = real detectors + fakes)

| Game | Launcher/Client | Loading | Active game | Post-game | Closed | Crash |
| --- | --- | --- | --- | --- | --- | --- |
| League of Legends | ✓ client-only decisive | ✓ ChampSelect phase → WAIT | ✓ match process → WAIT 1.0 | ✓ settle → enforce | ✓ nothing running decisive | ✓ logs-fresh → WAIT |
| VALORANT | ✓ launcher decisive | ✓ (log tokens, process pending) → WAIT | ✓ process up → WAIT 0.5 (menu==match) | ✓ settle → enforce | ✓ nothing/Vanguard-only decisive | ✓ fresh match tokens → WAIT |
| R.E.P.O. | ✓ Steam-only decisive | n/a (no public loading signal — WAIT via process) | ✓ process up → WAIT 0.5 | ✓ settle → enforce | ✓ nothing running decisive | ✓ process gone → settle → WAIT |
| TFT | ✓ client-only decisive | ✓ `InProgress` phase → WAIT | ✓ verified TFT → WAIT 1.0 / unverified shared → WAIT 0.5 | ✓ settle → enforce | ✓ nothing running decisive | ✓ fresh logs → WAIT |

Where to see each cell: `tests/test_valorant_detector.py`,
`tests/test_repo_detector.py`, `tests/test_tft_detector.py`,
`tests/test_lol_detector.py`, and end-to-end steps 1–21 of
`python -m app.main --detector-selftest` (any OS).

**Manual on-Windows matrix** (the one that needs a real game): see the Phase 8
section at the end of `docs/TESTING.md` — per game: client open, queue,
loading, in match, finish match, alt-F4 mid-match, crash. Record process
snapshots (`--status-json` → `detectors`) for each step.

## 4. Deliverable: implemented status

| Game | Detection | Active session | End detection |
| --- | --- | --- | --- |
| League of Legends | ✓ (Phase 6) | ✓ process-decisive (automated; live-verified in Phase 6 plan) | ✓ settle + signals |
| VALORANT | ✓ (process + launcher, Vanguard excluded) | ✓* coarse — process = WAIT; match-vs-menu unverifiable at user level (documented) | ✓ game-exit + settle |
| R.E.P.O. | ✓ (process; Steam excluded) | ✓* coarse by design — no public signal exists (spec's documented-limitation case) | ✓ game-exit + settle |
| TFT | ✓ (League client + match process) | ✓* shared-process WAIT; `gameMode` verification as evidence | ✓ settle |

`✓` = implemented + automated-tested + on-machine selftest step.
`✓*` = implemented + automated-tested; **match-granularity** session detection
is honestly impossible with public local signals for that title — the shipped
behaviour is the safest available (wait while the game process lives), and the
manual matrix is what confirms it against real games.

## 5. What would improve match granularity later

- **VALORANT:** the Riot client's authenticated local remoting API (dynamic
  port + basic-auth token) exposes match state (`IN_PROGRESS` etc.). Reading
  another app's auth material is fragile and borderline; deliberately not
  done. A future opt-in plugin could implement it inside the existing plugin
  host (drop-in, quarantined, fail-safe).
- **TFT/LoL:** Riot's Live Client Data API already answers `gameMode`; a
  future Riot-side change exposing per-mode end-of-game events would let the
  detectors move their "certain not-in-session" verdict earlier.
- **R.E.P.O.:** nothing public. If semiwork ships structured logs or an API,
  `PlayerLogWatcher` upgrades to a phase reader like the LoL one.

## 6. Everything else in Phase 8 (testing)

- Automated suite grew from 497 to **542 tests** (as of v0.9.0), all passing; the detector
  selftest grew from 17 to **21 steps**, all passing.
- `--status-json` → `detectors.items` now lists all four built-ins with their
  last verdict (the GUI detector panel picks this up automatically).
- The rule editor's *Known game* list now offers League of Legends, VALORANT,
  R.E.P.O. and Teamfight Tactics from the registry (no UI code changed).
- Rule-of-thumb documented in the rule editor hint text: for VALORANT and
  R.E.P.O., `WAIT_FOR_SESSION_END` means "wait until the game is closed" —
  users who want the limit at match granularity pick CLOSE/BLOCK instead.
