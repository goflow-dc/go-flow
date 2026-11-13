# Go Flow

Cleans junk files and scans for viruses with Microsoft Defender. Runs only on this laptop: a small
local page at http://127.0.0.1:8770 that no other computer or website can reach.

## How to use
- Double-click **GO FLOW** on the Desktop (or `Run_Cleaner.bat`); **GO FLOW (Admin)** /
  `Run_Cleaner_Admin.bat` also cleans Windows system folders and makes restore points.
  Go Flow opens in its own window (Microsoft Edge "app" mode: no tabs, no address bar, no console).
  Closing the window stops Go Flow a few seconds later, unless a scan or clean is still running.
  Opening it twice just brings up the copy that is already running. Without Edge it opens in your
  default browser. `python cleaner.py --browser` forces the old browser-tab way.
- **Smart Scan** (big button on Home): checks junk, old downloads, duplicates, memory and security
  in one go (about 30 seconds), then shows how much can be freed with a tick box per fix.
  "Fix selected" cleans junk (deleted for good), moves old setup files / unzipped zips to the Recycle
  Bin, boosts memory and updates virus definitions. Duplicates are never fixed automatically: the
  Review button opens them on the Duplicates screen, because copies can be program parts or in OneDrive.
- **Threat check** (Security): about a minute, read-only. Checks protection (antivirus, firewall,
  UAC, Smart App Control, Remote Desktop, proxy, Defender exclusions when run as admin), startup
  programs, non-Microsoft scheduled tasks, programs running from user-writable folders, files in
  Downloads and Desktop (disguised names like `x.pdf.exe`, broken or missing signatures, odd script
  types, the EICAR test file) and the hosts file. Signatures come from Windows' own check
  (`Get-AuthenticodeSignature`); startup shortcuts are followed to the real program. It is not an
  antivirus engine: findings offer safe actions only (scan with Defender, show the file, turn off at
  startup, open the Windows setting) and nothing is deleted.
- **Graphics:** Home has a storage donut (sizes of Downloads, Documents, Pictures, Videos, Music,
  Desktop, OneDrive and App data; everything else used = "Windows and programs"; measured in the
  background, about 30 seconds, remembered for an hour in `logs\storage_cache.json`; click a folder
  to list its biggest files) and a live memory graph (last 2 minutes, also on Speed boost). Health
  has ring gauges for disk, memory and battery. History shows total space freed and a bar per day
  for the last 14 days. Screen titles have icon badges and empty screens show a small illustration.
- **Settings:** protected folders and protected file names (files there are listed but can never be
  deleted by Large files / Duplicates / Downloads / Smart Scan; `*` = anything), which checks Smart
  Scan runs, and the default "old download" age and "large file" size. Saved in `settings.json`.
  The Go Flow folder is always protected.
- **About:** version (from `version.txt`, the one place the version number lives), where it is
  installed, buttons to open the folder and the logs.
- **Layout:** menu on the left grouped into Clean / Speed / Security / System (a scrolling strip on a
  narrow window), page title and disk bar on top, and a light / dark button (follows Windows until you
  pick one; the choice is remembered in the browser).
- **Home:** a 0-100 health score with what to fix (each with a button to the right screen), quick
  buttons (Boost memory, Find junk, Quick virus scan, Find duplicates), disk / memory / antivirus /
  Windows cards and the last 5 things done. Score: antivirus off -30, disk 85%+ full -15 (95%+ -25),
  old virus definitions -10, memory 80%+ -5 (90%+ -10), restart needed -10, no restore points -5,
  no virus scan for 14 days -5, auto-clean off -3, alerts off -2. Another antivirus (for example
  Reason Cybersecurity) puts Defender on standby: that counts as protected.
- **Junk cleaner:** Scan for junk, untick anything you want to keep, then Clean selected and confirm.
- **Virus scan:** Update definitions, then a quick scan (a few minutes) or full scan (1-2 hours).
  You can also scan one folder or a USB drive. Threats found are listed with a Remove button.
- **Speed:** memory (RAM) use, power mode and the apps using the most memory and CPU. Tick apps you
  are not using and press **Boost now**: they are asked to close normally (they can ask you to save),
  then every app is asked to give back memory it is not using right now. Apps without a window can be
  force closed after a second confirm. Windows parts (and Go Flow itself) can never be ticked.
  "Change power mode" opens Windows Settings, where "Best performance" makes the laptop faster.
- **Large files:** lists the biggest files in a folder or drive (default: your user folder). Tick what
  you no longer need and move it to the Recycle Bin. "Show" opens the file's folder in File Explorer.
- **Duplicates:** finds files with exactly the same content (checked byte by byte, not just by name).
  The oldest copy is kept; extra copies are ticked. You can never tick every copy of a file.
  Windows can take 1-3 seconds per file to move things into the Recycle Bin, so a progress counter
  shows how far it is. For duplicates you can tick "Delete the extra copies permanently (fast)":
  safe, because one identical copy of each file is always kept.
- **Downloads:** setup files (.exe, .msi, .apk, .iso …) and zip files (.zip, .rar, .7z …) in Downloads
  that you have not opened for 30 / 60 / 90 days, 6 months or a year (Windows records when a file was
  last opened). Setup files and zips that are already unzipped (a folder with the same name sits next
  to them) are ticked; other zips may be your only copy, so you decide. Goes to the Recycle Bin.
- **Startup apps:** turn apps on or off at Windows startup, the same way Task Manager does. Nothing
  is uninstalled, and you can turn them back on any time. "All users" apps need the admin launcher.
- **Programs:** installed programs, biggest first, with search. "Uninstall" opens the program's own
  uninstaller (the same one Windows Settings uses); nothing is removed until you confirm there.
- **Health:** disk health, free space, battery health (charge it holds vs when new, charge cycles),
  memory use and the apps using the most memory, with plain advice. Disk temperature and wear show
  when opened with the Admin shortcut.
- **Updates:** Windows version, whether a restart is needed, last check and last install, whether
  updates are paused, and the last 25 updates with their result (Store app failures get their own tip).
  "Find waiting updates" asks Microsoft what is waiting (takes about a minute) and marks each as
  recommended, optional or driver. Installing is left to Windows Settings ("Check in Windows Update").
- **Auto-clean:** turn on a weekly safe clean (temp files, browser caches if the browser is closed,
  crash reports, thumbnails) plus a Defender update and quick scan. It never empties the Recycle Bin
  or touches large files, duplicates or your folders. If the laptop is off, it runs when you switch on.
  Turned on from the Admin shortcut, it also cleans the Windows system folders. If Defender finds an
  active threat, a message box tells you; removing it is left to you (Virus scan tab).
- **Protection:**
  - *Restore points:* a Windows "undo" for system changes (not your personal files). Shows whether
    System Protection is on for C:, lists restore points, creates one on demand, and can turn System
    Protection on (all of this needs the Admin shortcut). When you clean Windows system folders as
    admin, the confirm box offers "Create a restore point first" (ticked by default), and the weekly
    auto-clean makes one first too. Windows allows one new restore point per 24 hours.
  - *Alerts:* a scheduled task checks every hour and at sign-in, and shows a Windows notification
    if any drive has less free space than you chose (5-25%) or Defender real-time protection is off.
    At most one notification per problem per day (`logs\alert_state.json` remembers).
- **History:** every clean, scan and change is saved in `logs\`.

Opening the app twice just brings up the copy that is already running.
`python cleaner.py --auto` runs the weekly clean by hand (this is what the scheduled task runs).
`python cleaner.py --check-alerts 10` runs the alert check once (what the alerts task runs).

## What it cleans
Your temp files and Windows temp files (only files older than 1 day), Chrome and Edge cache (not
passwords, history or logins), Windows Update downloads, crash dumps and error reports, the
thumbnail cache, and the Recycle Bin (unticked by default).

## Safety
- Nothing is deleted until you confirm, and files in use are skipped.
- Desktop, Documents, Downloads, Pictures, Videos, Music and OneDrive are never touched.
- It never follows shortcuts or links out of the folders it cleans.
- Large files and duplicates go to the **Recycle Bin**, never straight to permanent deletion.
- The file finders skip Windows, Program Files, ProgramData and AppData, and OneDrive files that
  are only in the cloud (reading them would download them).
- Protected folders and file names (Settings) are shown as "Kept" and can't be deleted. Go Flow's
  own folders are always protected.
- The duplicate finder reads files to compare them and then puts each file's "last opened" time
  back, so the Downloads tidy-up still knows when you last used a file.

## Files
- `cleaner.py`: engine + local server
- `cleaner_ui.html`: the page (layout, styles, animations; they switch off when Windows has
  "animation effects" turned off)
- `version.txt`: the version number (About screen; later the installer and website)
- `settings.json`: your settings (created on first run)
- `assets\icon.ico`, `icon.png`: the app icon; `make_icon.py` redraws them (needs Pillow)
- `build.bat`, `BUILD.md`, `installer\setup.iss`: turn the app into `GoFlow.exe` and an installer
- `website\`: the product website; `GO_LIVE.md`: how to publish it and the installer on GitHub
- `.github\workflows\`: GitHub builds the installer on each Release and publishes the website
- `logs\`: history (one file per month)
- `versions\v2\` … `versions\v8\`: copies of earlier versions, to go back if needed (v7 = last one
  with the old tab layout, v8 = before Smart Scan / app window / animations, v9 = before charts and gauges)

## Ideas for later
- One-file .exe (no Python needed) and a tray icon
