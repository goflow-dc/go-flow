# Building Go Flow

Go Flow is a Python app (`cleaner.py` + `cleaner_ui.html`, standard library only). Building turns
it into `GoFlow.exe` (no Python needed) and then into an installer.

## The easy way: let GitHub build it
Push a tag like `v1.0.1` and `.github/workflows/release.yml` builds the program and the installer on
a Windows machine in the cloud and attaches `GoFlow-Setup.exe` to a new Release. See `GO_LIVE.md`.

## On your own PC

Needs: Python 3.12+, and Inno Setup 6 for the installer step (https://jrsoftware.org/isdl.php, or
`winget install JRSoftware.InnoSetup`).

```bat
build.bat
```

What it does:

1. Creates `.build-venv\` and installs PyInstaller there (first run only).
2. Program:
   ```bat
   .build-venv\Scripts\python -m PyInstaller --noconfirm --clean --noconsole --onedir --name GoFlow ^
     --icon assets\icon.ico ^
     --add-data "cleaner_ui.html;." --add-data "version.txt;." --add-data "assets;assets" cleaner.py
   ```
   Result: `dist\GoFlow\GoFlow.exe` plus its `_internal\` folder (about 18 MB).
3. Installer (skipped with a message if Inno Setup is missing):
   ```bat
   "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" /DAppVersion=1.0.0 installer\setup.iss
   ```
   Result: `dist\GoFlow-Setup.exe`, and its SHA-256 checksum is printed.

`--onedir` (a folder) is used instead of `--onefile`: with an installer the user never sees the
folder, the app starts faster (nothing to unpack on every start) and antivirus tools flag it less.

## Version number
One place: `version.txt`. The app's About screen reads it, `build.bat` passes it to the installer,
and the release workflow checks that the tag matches it. The website shows the version of the newest
GitHub Release.

## Icon
`python make_icon.py` redraws `assets\icon.ico` and `icon.png` (needs Pillow). Only when the logo changes.

## What the installer does
- Installs to `Program Files\Go Flow`, adds it to Windows "Installed apps" with an uninstaller.
- Start Menu: **Go Flow** and **Go Flow (Admin)** (`GoFlow.exe --admin`); optional Desktop shortcut.
- Uninstalling removes the program, the two scheduled tasks (weekly auto-clean, alerts) and the
  app-window profile. Settings and history in `%LOCALAPPDATA%\GoFlow` are left in place.

## Where things live when installed
| What | Where |
|---|---|
| Program | `C:\Program Files\Go Flow\` |
| Settings, history logs, storage cache | `%LOCALAPPDATA%\GoFlow\` |
| App-window browser profile | `%LOCALAPPDATA%\GoFlow\window\` |

Run from source (`python cleaner.py`), everything stays in the project folder instead.

## Smart App Control and unsigned builds
A PC with Windows 11 **Smart App Control** turned on refuses to run an unsigned `GoFlow.exe`
("An Application Control policy has blocked this file"), with no way to click through. The build
machine used for this project has it on, so the .exe cannot be test-run there; test it on the GitHub
build or on a PC without Smart App Control. The real fix is code signing: see `GO_LIVE.md`, part 4.
Do not turn Smart App Control off just to test: Windows cannot turn it back on without a reset.
