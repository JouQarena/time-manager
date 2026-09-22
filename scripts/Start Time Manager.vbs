' Launches the Time Manager GUI with NO console window at all.
'
' Use this when the console-less TimeManagerTray.exe will not start on your
' machine (antivirus software sometimes silently blocks unsigned windowed
' PyInstaller exes while leaving the console sibling alone): this script
' starts the working console exe with its window hidden instead.
'
' Place this file NEXT TO TimeManager.exe (build.bat copies it into
' dist\TimeManager automatically) and double-click it.

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

here = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = here

' 0 = hidden window, False = do not wait for the process to exit
shell.Run """" & here & "\TimeManager.exe"" --gui", 0, False
