@echo off
rem Builds dist\GoFlow\GoFlow.exe, then (if Inno Setup is installed) dist\GoFlow-Setup.exe.
rem See BUILD.md. The version number comes from version.txt.
cd /d "%~dp0"
set /p VERSION=<version.txt

if not exist .build-venv\Scripts\python.exe (
  python -m venv .build-venv || exit /b 1
  .build-venv\Scripts\python -m pip install --quiet --disable-pip-version-check pyinstaller || exit /b 1
)

.build-venv\Scripts\python -m PyInstaller --noconfirm --clean --noconsole --onedir --name GoFlow ^
  --icon assets\icon.ico ^
  --add-data "cleaner_ui.html;." --add-data "version.txt;." --add-data "assets;assets" ^
  cleaner.py || exit /b 1
echo Built dist\GoFlow\GoFlow.exe (version %VERSION%)

set ISCC=
for %%P in ("%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" "%ProgramFiles%\Inno Setup 6\ISCC.exe" "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe") do if exist %%P set ISCC=%%P
if not defined ISCC (
  echo Inno Setup 6 is not installed, so the installer was skipped. See BUILD.md.
  exit /b 0
)
%ISCC% /Qp /DAppVersion=%VERSION% installer\setup.iss || exit /b 1
echo Built dist\GoFlow-Setup.exe
certutil -hashfile dist\GoFlow-Setup.exe SHA256
