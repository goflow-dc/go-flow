; Go Flow installer (Inno Setup 6). Build with build.bat, or:
;   ISCC.exe /DAppVersion=1.0.0 installer\setup.iss
; Needs dist\GoFlow\ from PyInstaller first. Output: dist\GoFlow-Setup.exe
; The file name has no version in it on purpose, so the website's
; ".../releases/latest/download/GoFlow-Setup.exe" link always works.

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{B6E0C6C1-7C0E-4E0B-9B1D-6F1A2D5C7E11}
AppName=Go Flow
AppVersion={#AppVersion}
AppVerName=Go Flow {#AppVersion}
AppPublisher=Go Flow @DC
DefaultDirName={autopf}\Go Flow
DefaultGroupName=Go Flow
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=GoFlow-Setup
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\GoFlow.exe
UninstallDisplayName=Go Flow
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
WizardStyle=modern
PrivilegesRequired=admin
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Create a Desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\GoFlow\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\Go Flow"; Filename: "{app}\GoFlow.exe"
Name: "{group}\Go Flow (Admin)"; Filename: "{app}\GoFlow.exe"; Parameters: "--admin"; Comment: "Also cleans Windows system folders and makes restore points"
Name: "{autodesktop}\Go Flow"; Filename: "{app}\GoFlow.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\GoFlow.exe"; Description: "Open Go Flow now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Remove the weekly auto-clean and alert tasks if the user turned them on.
Filename: "{sys}\schtasks.exe"; Parameters: "/Delete /TN ""GoFlow Weekly"" /F"; Flags: runhidden; RunOnceId: "GoFlowWeekly"
Filename: "{sys}\schtasks.exe"; Parameters: "/Delete /TN ""GoFlow Alerts"" /F"; Flags: runhidden; RunOnceId: "GoFlowAlerts"

[UninstallDelete]
; The app-window browser profile. Settings and history in %LOCALAPPDATA%\GoFlow are kept.
Type: filesandordirs; Name: "{localappdata}\GoFlow\window"
