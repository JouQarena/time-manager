; Time Manager — optional Inno Setup installer (Phase 9).
;
; The portable build (dist\TimeManager\) needs no installer; this script is
; for users who want Start-menu shortcuts and a proper uninstall entry.
;
; Build: install Inno Setup 6 (https://jrsoftware.org/isinfo.php), then
;   iscc installer\time-manager.iss
; Output: installer\Output\TimeManager-<version>-setup.exe
;
; The installer ONLY touches Program Files; all user data stays in
; %APPDATA%\TimeManager and survives uninstall (documented in PACKAGING.md).

#define AppName "Time Manager"
#define AppExe "TimeManagerTray.exe"
#ifndef AppVersion
#define AppVersion ReadIni(SourcePath + "\..\build", "version.ini", "app", "version", "0.0.0")
#endif

[Setup]
AppId={{7E6F2A4C-52B1-4A57-9C0E-TimeManager0}}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
DefaultDirName={autopf}\TimeManager
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=TimeManager-{#AppVersion}-setup
Compression=lzma2/max
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
UninstallDisplayIcon={app}\{#AppExe}
; No digital certificate in this project yet: Windows SmartScreen will warn
; on first run ("More info" -> "Run anyway"). Signed builds are the documented
; next step; nothing here requires bypassing Defender or anything similar.

[Files]
Source: "..\dist\TimeManager\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\Time Manager"; Filename: "{app}\{#AppExe}"
Name: "{group}\Time Manager CLI"; Filename: "{app}\TimeManager.exe"; Parameters: "--status"
Name: "{autodesktop}\Time Manager"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"
Name: "autostart"; Description: "Start Time Manager with Windows (adds a HKCU Run entry)"; GroupDescription: "Startup:"

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; \
    ValueName: "TimeManager"; ValueData: """{app}\{#AppExe}\"" --gui"; Tasks: autostart; \
    Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#AppExe}"; Description: "Launch Time Manager"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Stop a running agent so files can be replaced/removed. (The scheduled
; watchdog task, if enabled, is removed in Settings -> Strict-mode protection
; or with: schtasks /Delete /TN "Time Manager Watchdog" /F)
Filename: "{cmd}"; Parameters: "/C taskkill /IM TimeManagerTray.exe /F & taskkill /IM TimeManager.exe /F"; \
    Flags: runhidden; RunOnceId: "StopAgent"

[UninstallDelete]
; Nothing: %APPDATA%\TimeManager (database, config, backups, logs) is user
; data and is deliberately preserved across uninstall. Delete it manually if
; you want a pristine machine - this is documented, not hidden.
