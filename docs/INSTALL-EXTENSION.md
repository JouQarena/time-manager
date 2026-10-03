# Installing the Time Manager browser extension

The desktop app on its own can count **apps and games**. **Website rules need
the browser extension** — it is what tells the agent which domain is in the
focused tab, and it is what renders the "blocked" page when a limit is reached.

This guide is written for a **portable/exe install** (the normal case) with a
note at the end for running from source. Takes about two minutes.

The extension also ships its **own** guide page — it opens automatically the
first time you install it, and lives behind *Guide* in its options page, popup
and toolbar menu. Same three steps as below, plus the badge/error decoder.
For how the extension works internally (messages, heartbeat, MV3 lifetime,
failure modes) see **[EXTENSION.md](EXTENSION.md)**.

> Nothing leaves your machine: the extension talks only to
> `ws://127.0.0.1:<port>` on this computer. No account, no cloud, no telemetry.

---

## 1. Run the desktop app once

Double-click **`TimeManagerTray.exe`** (in the folder where you unzipped
`TimeManager-v<version>-windows-portable.zip`). The tray icon appears and the
dashboard opens. Leave it running — the extension needs a live agent.

## 2. Copy the pairing token

Two ways, both fine:

| Where | How |
| --- | --- |
| Dashboard | **Settings ▸ Browser extension ▸ Pairing token** → press **Show** → **Copy** |
| Terminal | `TimeManager.exe --show-token` in the same folder |

The token is 64 hex characters. It is a **machine secret** — treat it like a
password and keep it out of screenshots and chats. If it ever leaks, press
**Regenerate token** and re-pair (see step 6).

## 3. Find the extension folder

| Where | How |
| --- | --- |
| Terminal | `TimeManager.exe --show-extension-path` — prints the exact path |
| Explorer | inside the app folder, next to `TimeManagerTray.exe`: `_internal\browser-extension\` |

You need the folder that **contains `manifest.json`** — that is the one Chrome
wants, not its parent and not a single file.

## 4. Load it in Chrome or Edge

1. Open `chrome://extensions` (or `edge://extensions`).
2. Turn on **Developer mode** (top-right in Chrome, left sidebar in Edge).
3. Press **Load unpacked**.
4. Select the `browser-extension` folder from step 3.
5. "Time Manager Companion" appears in the list. Pin it to the toolbar
   (puzzle-piece icon → pin) so you can see its badge.

Expected result: the extension's badge shows **`!`** — it is installed but not
paired yet.

## 5. Pair it

1. Open the extension's **Options**:
   `chrome://extensions` → *Time Manager Companion* → **Details** → **Extension
   options**. (Or right-click the toolbar icon → *Options*.)
2. Paste the token from step 2 into **Agent token**.
3. Leave **Host** = `127.0.0.1` and **Port** = `17846` unless you changed the
   agent port in **Settings ▸ Browser extension ▸ Agent port** — then use that
   number.
4. Confirm **"Report browsing activity to the agent"** is ticked.
5. Press **Save & test**.

The **Status** block on the same page should read:

```
Connection     connected
Agent version  1.0.2
This browser id  <a uuid, one per browser profile>
Messages       12 sent / 9 received
```

...and the toolbar badge clears to blank. In the dashboard, the **Browser
extension** panel switches from *"No browser connected…"* to e.g.
`Chrome · 0.2.0 · Active: youtube.com`.

## 6. Add a website rule and prove it works

1. Dashboard → **New rule** → *Name*: `YouTube`, *Type*: **Website (domain)**,
   *Target*: `youtube.com` (domain only — subdomains count too), *Daily limit*:
   `1` minute, *When the limit is reached*: **Block the site in the browser**.
2. Save, then open `youtube.com` in the paired browser and leave the tab in the
   foreground for a minute.
3. The extension renders a local block page with the reset time, and the card
   chip in the dashboard reads **Blocked in browser**.

Only the **active + focused** tab counts: background tabs, minimised windows
and a browser that lost focus do not accumulate time.

---

## Troubleshooting

| Symptom | What it means / fix |
| --- | --- |
| Badge keeps showing `!`, Options says *"not reachable — is the agent running?"* | The dashboard/app is not running, or the port differs. Start `TimeManagerTray.exe`; check **Settings ▸ Agent port** matches the number in the extension. |
| Options says *"That does not look like a 64-character hex token"* | A truncated paste. Copy again with the **Copy** button (not a screenshot). |
| Status is *connected* but the dashboard still says *no browser connected* | Each browser has its own identity: pair every browser profile you use (Chrome **and** Edge, and every Windows user profile). |
| `http://` sites not blocked, `https://` fine | Site rules match the domain either way; if a page is not blocked, check the rule's **Schedule** and that the rule is **enabled** (card chip not "Off"). |
| Website time is not counted at all | The rule exists but the extension is not paired: the dashboard shows a red anti-bypass notice — *"No browser extension connected — website limits are NOT enforced for: …"*. |
| Time keeps counting while the tab is in the background | Expected — only the foreground+focused tab counts. If you *want* stricter accounting of a whole browser, limit the browser `exe` as an **Application** rule instead. |
| Chrome/Edge got a new profile, or you reinstalled Windows | Pair again from step 5; tokens and browser ids live in the browser profile. |
| After **Regenerate token** | Every browser is disconnected by design: re-paste the new token in each one (step 5). |
| Extension disappeared from the toolbar after a Chrome update | Chrome disables unpacked extensions that it considers stale: `chrome://extensions` → **Reload** on the Time Manager card. |

## Updating the extension

Unpacked extensions are not auto-updated — the copy in the app folder *is* the
extension. After updating the app: `chrome://extensions` → **Reload** on the
card. Re-pairing is not needed; your token stays valid.

## Uninstalling

1. `chrome://extensions` → *Time Manager Companion* → **Remove**.
2. In the desktop app: **Settings ▸ General** — optionally press *Back up now*,
   then quit from the tray. Website rules can stay in the database; they simply
   stop being enforced without the extension.

## Running from source (development)

```bash
python -m app.main --show-token            # token
python -m app.main --show-extension-path   # prints "<repo>/browser-extension"
python -m app.main --gui                   # agent + dashboard
```

Then the same step 4/5 flow, pointing Chrome at the repo's `browser-extension/`
folder. `python -m app.main --ipc-selftest` proves the WebSocket link end to
end without a browser, and `python -m app.main --status` shows the connection
count.

## See also

- `docs/EXTENSION.md` — how the extension works: connection, tab reporting,
  blocking, badge states, MV3 lifetime, failure modes.
- `docs/PROTOCOL.md` — the exact messages the extension and agent exchange.
- `docs/SECURITY-PRIVACY.md` — what is stored, what is never sent anywhere.
- `docs/LIMITATIONS.md` — the honest list, including what the extension cannot
  see.
