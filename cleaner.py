"""Go Flow: clean, speed up and protect a Windows laptop, served as a local page.

Run: python cleaner.py   (opens Go Flow in its own window)
Installed build: GoFlow.exe, made with PyInstaller (see BUILD.md).
Your files never leave the laptop. The server only listens on 127.0.0.1 and
every action needs a secret token that only the page it served knows.
"""
import ctypes
import fnmatch
import glob
import hashlib
import json
import os
import re
import secrets
import shutil
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
import winreg
from ctypes import wintypes
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOME = Path.home()
LOCAL = Path(os.environ.get("LOCALAPPDATA", HOME / "AppData" / "Local"))

# Three places: where the program is, the files packed with it (page, icon,
# version), and where it may write (settings, logs). Run from source they are
# all this folder; the installed GoFlow.exe sits in Program Files, which is
# read-only, so its data goes to %LOCALAPPDATA%\GoFlow.
FROZEN = getattr(sys, "frozen", False)
HERE = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
RES = Path(getattr(sys, "_MEIPASS", HERE))
DATA = LOCAL / "GoFlow" if FROZEN else HERE
LOG_DIR = DATA / "logs"
PORT = 8770
TOKEN = secrets.token_hex(16)


def self_command(extra=""):
    """(program, arguments) that start Go Flow again without a console, e.g. for scheduled tasks."""
    if FROZEN:
        return Path(sys.executable), extra
    pyw = Path(sys.executable).with_name("pythonw.exe")
    return (pyw if pyw.exists() else Path(sys.executable)), f'"{HERE / "cleaner.py"}" {extra}'.strip()


WINDIR = Path(os.environ.get("WINDIR", r"C:\Windows"))
PROGRAMDATA = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
TEMP = Path(os.environ.get("TEMP", LOCAL / "Temp"))

# Folders the cleaner must never delete from, whatever a category says.
PROTECTED = [HOME / d for d in ("Desktop", "Documents", "Downloads", "Pictures",
                                "Videos", "Music", "OneDrive")]
# Temp files younger than this are probably in use by an open program.
MIN_AGE_HOURS = 24


def browser_cache_dirs(base):
    dirs = []
    for profile in glob.glob(str(base / "User Data" / "*")):
        for sub in ("Cache", "Code Cache", "GPUCache", "Service Worker\\CacheStorage"):
            p = Path(profile) / sub
            if p.is_dir():
                dirs.append(p)
    return dirs


# Each category: folders to empty (contents only, never the folder itself),
# or file patterns, plus whether it needs admin rights.
CATEGORIES = [
    {"id": "user_temp", "name": "Your temp files", "admin": False, "aged": True,
     "about": "Leftovers from installers and apps (older than 1 day).",
     "dirs": lambda: [TEMP]},
    {"id": "win_temp", "name": "Windows temp files", "admin": True, "aged": True,
     "about": "System temp folder (older than 1 day).",
     "dirs": lambda: [WINDIR / "Temp"]},
    {"id": "chrome", "name": "Chrome cache", "admin": False, "aged": False, "browser": "chrome.exe",
     "about": "Cached web pages and images. Passwords, history and logins are NOT touched.",
     "dirs": lambda: browser_cache_dirs(LOCAL / "Google" / "Chrome")},
    {"id": "edge", "name": "Edge cache", "admin": False, "aged": False, "browser": "msedge.exe",
     "about": "Cached web pages and images. Passwords, history and logins are NOT touched.",
     "dirs": lambda: browser_cache_dirs(LOCAL / "Microsoft" / "Edge")},
    {"id": "wu", "name": "Windows Update downloads", "admin": True, "aged": True,
     "about": "Update files already installed. Windows downloads them again if needed.",
     "dirs": lambda: [WINDIR / "SoftwareDistribution" / "Download"]},
    {"id": "crash", "name": "Crash dumps and error reports", "admin": False, "aged": False,
     "about": "Memory dumps and reports saved when programs crashed.",
     "dirs": lambda: [LOCAL / "CrashDumps", LOCAL / "Microsoft" / "Windows" / "WER",
                      PROGRAMDATA / "Microsoft" / "Windows" / "WER" / "ReportArchive",
                      PROGRAMDATA / "Microsoft" / "Windows" / "WER" / "ReportQueue",
                      WINDIR / "Minidump"]},
    {"id": "thumbs", "name": "Thumbnail cache", "admin": False, "aged": False,
     "about": "Picture previews in File Explorer. Rebuilt automatically.",
     "files": lambda: glob.glob(str(LOCAL / "Microsoft" / "Windows" / "Explorer" / "thumbcache_*.db"))},
]


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def is_protected(path):
    p = Path(path).resolve()
    return any(p == d or d in p.parents for d in PROTECTED)


def running(exe):
    out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {exe}", "/NH"],
                         capture_output=True, text=True, creationflags=0x08000000).stdout
    return exe.lower() in out.lower()


def iter_files(cat):
    """Yield (path, size) of every file the category would delete."""
    cutoff = time.time() - MIN_AGE_HOURS * 3600
    if "files" in cat:
        for f in cat["files"]():
            try:
                yield f, os.path.getsize(f)
            except OSError:
                pass
        return
    for root in cat["dirs"]():
        if not root.is_dir() or is_protected(root):
            continue
        stack = [str(root)]
        while stack:
            d = stack.pop()
            try:
                with os.scandir(d) as it:
                    for e in it:
                        try:
                            # Never follow links/junctions out of the folder.
                            if e.is_symlink():
                                continue
                            if e.is_dir(follow_symlinks=False):
                                stack.append(e.path)
                            elif e.is_file(follow_symlinks=False):
                                st = e.stat(follow_symlinks=False)
                                if cat["aged"] and st.st_mtime > cutoff:
                                    continue
                                yield e.path, st.st_size
                        except OSError:
                            pass
            except OSError:
                pass


# ---------- Recycle Bin ----------

def recycle_bin_size():
    ps = ("$s=(New-Object -ComObject Shell.Application).NameSpace(10).Items();"
          "$t=0;foreach($i in $s){$t+=$i.ExtendedProperty('Size')};"
          "Write-Output ($s.Count.ToString()+' '+$t)")
    out = powershell(ps, timeout=60)
    try:
        count, size = out["out"].split()[:2]
        return int(count), int(float(size))
    except Exception:
        return 0, 0


def empty_recycle_bin():
    return powershell("Clear-RecycleBin -Force -ErrorAction SilentlyContinue", timeout=300)


# ---------- Scan and clean ----------

def scan():
    admin = is_admin()
    results = []
    for cat in CATEGORIES:
        count = size = 0
        for _, s in iter_files(cat):
            count += 1
            size += s
        r = {"id": cat["id"], "name": cat["name"], "about": cat["about"], "count": count,
             "size": size, "needs_admin": cat["admin"] and not admin}
        if cat.get("browser") and running(cat["browser"]):
            r["warning"] = "Browser is open: close it first to clean more."
        results.append(r)
    n, s = recycle_bin_size()
    results.append({"id": "recycle", "name": "Recycle Bin", "count": n, "size": s,
                    "about": "Files you already deleted. Emptying removes them for good.",
                    "needs_admin": False})
    return results


def remove_empty_dirs(root):
    for d, _, _ in sorted(os.walk(root), key=lambda x: -len(x[0])):
        if Path(d) != root:
            try:
                os.rmdir(d)  # only succeeds when empty
            except OSError:
                pass


def clean(ids):
    started = datetime.now()
    report = []
    for cat in CATEGORIES:
        if cat["id"] not in ids:
            continue
        freed = removed = skipped = 0
        for path, size in list(iter_files(cat)):
            if is_protected(path):
                skipped += 1
                continue
            try:
                os.chmod(path, 0o666)
                os.remove(path)
                freed += size
                removed += 1
            except OSError:
                skipped += 1  # locked by a running program, or no permission
        if "dirs" in cat:
            for root in cat["dirs"]():
                if root.is_dir() and not is_protected(root):
                    remove_empty_dirs(root)
        report.append({"name": cat["name"], "removed": removed, "skipped": skipped, "freed": freed})
    if "recycle" in ids:
        n, s = recycle_bin_size()
        empty_recycle_bin()
        n2, s2 = recycle_bin_size()
        report.append({"name": "Recycle Bin", "removed": n - n2, "skipped": n2, "freed": s - s2})
    write_log("clean", {"started": started.isoformat(timespec="seconds"), "items": report})
    return report


# ---------- Microsoft Defender ----------

def powershell(cmd, timeout=3600):
    try:
        p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                           capture_output=True, text=True, timeout=timeout,
                           creationflags=0x08000000)
        return {"ok": p.returncode == 0, "out": p.stdout.strip(), "err": p.stderr.strip()}
    except subprocess.TimeoutExpired:
        return {"ok": False, "out": "", "err": "Timed out"}


def fmt_date(prop):
    return "@{n='%s';e={if($_.%s){$_.%s.ToString('dd MMM yyyy HH:mm')}else{''}}}" % (prop, prop, prop)


def defender_status():
    fields = ",".join(["AMServiceEnabled", "AntivirusEnabled", "RealTimeProtectionEnabled",
                       "AMRunningMode", "AntivirusSignatureVersion",
                       fmt_date("AntivirusSignatureLastUpdated"), fmt_date("QuickScanEndTime"),
                       fmt_date("FullScanEndTime"), "AntivirusSignatureAge"])
    r = powershell(f"Get-MpComputerStatus | Select-Object {fields} | ConvertTo-Json", timeout=60)
    try:
        s = json.loads(r["out"])
    except Exception:
        return {"error": r["err"] or "Could not read Defender status"}
    s["OtherAntivirus"] = [n for n in active_antivirus() if "defender" not in n.lower()]
    return s


def defender_threats():
    cmd = ("$d=@(Get-MpThreatDetection);$t=@{};Get-MpThreat|%{$t[[string]$_.ThreatID]=$_};"
           "$d|%{$x=$t[[string]$_.ThreatID];[pscustomobject]@{"
           "id=[string]$_.ThreatID;name=$(if($x){$x.ThreatName}else{''});"
           "severity=$(if($x){[int]$x.SeverityID}else{0});"
           "active=$(if($x){[bool]$x.IsActive}else{$false});"
           "when=$_.InitialDetectionTime.ToString('dd MMM yyyy HH:mm');"
           "files=(@($_.Resources)-join '; ');status=[int]$_.ThreatStatusID}}|ConvertTo-Json -Depth 3")
    r = powershell(cmd, timeout=120)
    if not r["out"]:
        return []
    try:
        data = json.loads(r["out"])
        return data if isinstance(data, list) else [data]
    except Exception:
        return []


DEFENDER_ACTIONS = {
    "update": ("Updating virus definitions", "Update-MpSignature"),
    "quick": ("Quick scan", "Start-MpScan -ScanType QuickScan"),
    "full": ("Full scan (can take 1-2 hours)", "Start-MpScan -ScanType FullScan"),
    "remove": ("Removing active threats", "Remove-MpThreat"),
}

job = {"running": False, "title": "", "started": None, "finished": None, "ok": None, "message": ""}
job_lock = threading.Lock()


def run_job(title, cmd):
    with job_lock:
        if job["running"]:
            return False
        job.update(running=True, title=title, started=time.time(), finished=None, ok=None, message="")

    def work():
        r = powershell(cmd)
        msg = r["err"] or r["out"] or "Done"
        if "0x800106ba" in msg or "Access is denied" in msg or "permission" in msg.lower():
            msg += "  (Try opening Go Flow (Admin).)"
        job.update(running=False, finished=time.time(), ok=r["ok"], message=msg[:2000])
        write_log("defender", {"action": title, "ok": r["ok"], "message": msg[:2000]})

    threading.Thread(target=work, daemon=True).start()
    return True


def defender_run(action, path=None):
    if action == "folder":
        if not path or not os.path.exists(path):
            return {"ok": False, "message": "That folder or file does not exist."}
        safe = path.replace("'", "''")
        started = run_job(f"Scanning {path}", f"Start-MpScan -ScanType CustomScan -ScanPath '{safe}'")
    elif action in DEFENDER_ACTIONS:
        started = run_job(*DEFENDER_ACTIONS[action])
    else:
        return {"ok": False, "message": "Unknown action"}
    return {"ok": started, "message": "" if started else "Another job is still running."}


# ---------- Large files and duplicates ----------

# Folders the file finders never go into (system, apps, app data).
SKIP_DIRS = {"windows", "program files", "program files (x86)", "programdata", "appdata",
             "$recycle.bin", "system volume information", "recovery", "$windows.~bt",
             "$windows.~ws", "$sysreset", "perflogs", "onedrivetemp"}

# ---------- Settings and version ----------
# Protected folders / file names: the file finders show these files but never
# let you delete them. Kept in settings.json in the data folder.

VERSION_FILE = RES / "version.txt"
VERSION = VERSION_FILE.read_text(encoding="utf-8").strip() if VERSION_FILE.exists() else "1.0.0"
SETTINGS_FILE = DATA / "settings.json"
SMART_PARTS = ["junk", "downloads", "dupes", "memory", "security"]
OWN_FOLDERS = list(dict.fromkeys([str(HERE), str(DATA)]))  # Go Flow never deletes itself
DEFAULT_SETTINGS = {"protected_folders": OWN_FOLDERS, "protected_patterns": [],
                    "smart": {p: True for p in SMART_PARTS}, "downloads_days": 60, "large_min_mb": 100,
                    "virustotal_api_key": ""}


def load_settings():
    s = json.loads(json.dumps(DEFAULT_SETTINGS))
    try:
        saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        s.update({k: v for k, v in saved.items() if k in s})
        s["smart"] = {**DEFAULT_SETTINGS["smart"], **(saved.get("smart") or {})}
    except (OSError, ValueError):
        pass
    for f in OWN_FOLDERS:
        if f not in s["protected_folders"]:
            s["protected_folders"].insert(0, f)
    return s


SETTINGS = load_settings()


def save_settings(data):
    folders = [str(f).strip() for f in data.get("protected_folders", []) if str(f).strip()]
    for f in OWN_FOLDERS:
        if f not in folders:
            folders.insert(0, f)
    patterns = [str(p).strip().lower() for p in data.get("protected_patterns", []) if str(p).strip()]
    smart = {p: bool((data.get("smart") or {}).get(p, True)) for p in SMART_PARTS}
    try:
        days = max(7, min(3650, int(data.get("downloads_days", 60))))
        large = max(1, min(100000, int(data.get("large_min_mb", 100))))
    except (TypeError, ValueError):
        return {"ok": False, "message": "Numbers only, please."}
    vt_key = str(data.get("virustotal_api_key", SETTINGS["virustotal_api_key"])).strip()[:128]
    SETTINGS.update(protected_folders=folders[:100], protected_patterns=patterns[:200], smart=smart,
                    downloads_days=days, large_min_mb=large, virustotal_api_key=vt_key)
    DATA.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(SETTINGS, indent=1), encoding="utf-8")
    write_log("settings", {"action": "Settings saved", "ok": True})
    return {"ok": True, "settings": SETTINGS}


def keep_reason(path):
    p = os.path.normcase(os.path.abspath(path))
    for d in SETTINGS["protected_folders"]:
        d = os.path.normcase(os.path.abspath(d))
        if p == d or p.startswith(d.rstrip("\\") + "\\"):
            return "Protected folder"
    name = os.path.basename(p)
    if any(fnmatch.fnmatch(name, pat) for pat in SETTINGS["protected_patterns"]):
        return "Protected file name"
    return ""


def about():
    return {"version": VERSION, "python": sys.version.split()[0], "folder": str(HERE),
            "logs": str(LOG_DIR), "settings": str(SETTINGS_FILE), "admin": is_admin(),
            "window": app_window["mode"]}


def open_folder(which):
    target = {"app": HERE, "logs": LOG_DIR}.get(which)
    if target:
        target.mkdir(parents=True, exist_ok=True)
        os.startfile(str(target))
    return {"ok": bool(target)}


# FILE_ATTRIBUTE_OFFLINE | RECALL_ON_OPEN | RECALL_ON_DATA_ACCESS
CLOUD_ONLY = 0x1000 | 0x40000 | 0x400000


def walk_user_files(root, min_size, status):
    """Yield (path, size, mtime) for files >= min_size under root, skipping system folders."""
    stack = [str(root)]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_symlink() or (hasattr(e, "is_junction") and e.is_junction()):
                            continue
                        if e.is_dir(follow_symlinks=False):
                            if e.name.lower() not in SKIP_DIRS and not e.name.startswith("."):
                                stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            status["files"] += 1
                            st = e.stat(follow_symlinks=False)
                            if st.st_file_attributes & CLOUD_ONLY:
                                continue  # OneDrive file not on this laptop: reading it would download it
                            if st.st_size >= min_size:
                                yield e.path, st.st_size, st.st_mtime
                    except OSError:
                        pass
        except OSError:
            pass
        status["progress"] = f"Looked at {status['files']:,} files…"


PROGRAM_PARTS = {".dll", ".sys", ".pak", ".dat", ".bin", ".so", ".node", ".jar", ".pyd", ".ocx"}


def file_row(path, size, mtime):
    notes = []
    if Path(path).suffix.lower() in PROGRAM_PARTS:
        notes.append("Part of a program: deleting it can break that program")
    if any(part.lower().startswith("onedrive") for part in Path(path).parts):
        notes.append("In OneDrive: deleting also removes the cloud copy")
    return {"path": path, "name": os.path.basename(path), "folder": os.path.dirname(path),
            "size": size, "modified": datetime.fromtimestamp(mtime).strftime("%d %b %Y"),
            "keep": keep_reason(path), "notes": notes,
            "program": Path(path).suffix.lower() in PROGRAM_PARTS}


def find_large(root, min_mb, status, limit=200):
    rows = sorted(walk_user_files(root, min_mb * 1048576, status), key=lambda r: -r[1])[:limit]
    return {"files": [file_row(*r) for r in rows]}


def file_hash(path, status, quick=False):
    h = hashlib.blake2b(digest_size=20)
    before = os.stat(path)
    with open(path, "rb") as f:
        if quick:  # first and last 64 KB are enough to rule out most non-duplicates
            h.update(f.read(65536))
            f.seek(max(0, before.st_size - 65536))
            h.update(f.read(65536))
        else:
            while chunk := f.read(1048576):
                h.update(chunk)
    try:
        # Reading counts as "opened" in Windows; put the times back so the Downloads
        # tidy-up still knows when YOU last used the file.
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    except OSError:
        pass
    return h.hexdigest()


def find_duplicates(root, min_mb, status, limit=200):
    by_size = {}
    for path, size, mtime in walk_user_files(root, max(1, int(min_mb * 1048576)), status):
        by_size.setdefault(size, []).append((path, size, mtime))
    candidates = [g for g in by_size.values() if len(g) > 1]
    groups = []
    for i, group in enumerate(candidates, 1):
        status["progress"] = f"Comparing contents: {i:,} of {len(candidates):,} size matches…"
        buckets = {}
        for stage in (True, False):  # quick hash, then full hash on what still matches
            buckets = {}
            for r in group:
                try:
                    buckets.setdefault(file_hash(r[0], status, quick=stage), []).append(r)
                except OSError:
                    pass
            group = [r for b in buckets.values() if len(b) > 1 for r in b]
            if not group:
                break
        for same in buckets.values():
            if len(same) > 1:
                same.sort(key=lambda r: r[2])  # oldest first: suggested to keep
                groups.append({"size": same[0][1], "wasted": same[0][1] * (len(same) - 1),
                               "files": [file_row(*r) for r in same]})
    groups.sort(key=lambda g: -g["wasted"])
    return {"groups": groups[:limit]}


DOWNLOADS = HOME / "Downloads"
INSTALLERS = {".exe", ".msi", ".msix", ".msixbundle", ".appx", ".appxbundle", ".apk", ".iso", ".img"}
ARCHIVES = {".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz"}


def find_old_downloads(root, days, status):
    """Installers and archives in Downloads not opened or changed for `days` days."""
    cutoff = time.time() - days * 86400
    rows = []
    for path, size, mtime in walk_user_files(root, 0, status):
        ext = Path(path).suffix.lower()
        if ext not in INSTALLERS and ext not in ARCHIVES:
            continue
        try:
            used = max(mtime, os.stat(path).st_atime)  # last opened (Windows tracks this) or changed
        except OSError:
            continue
        if used > cutoff:
            continue
        row = file_row(path, size, used)
        row["kind"] = "Installer" if ext in INSTALLERS else "Archive"
        row["days"] = int((time.time() - used) // 86400)
        row["hints"] = []
        if row["kind"] == "Installer":
            row["hints"].append("Setup file: you can download it again if needed")
        else:
            stem = Path(path).with_suffix("")
            if stem.suffix.lower() == ".tar":
                stem = stem.with_suffix("")
            if stem.is_dir():
                row["hints"].append("Already unzipped: the folder next to it has the contents")
        # Tick installers and unzipped archives; leave other archives for you to decide.
        row["suggest"] = not row["keep"] and (row["kind"] == "Installer" or bool(row["hints"]))
        rows.append(row)
    rows.sort(key=lambda r: -r["size"])
    return {"files": rows, "days": days}


fjob = {"running": False, "kind": "", "progress": "", "files": 0, "result": None, "error": ""}
# Only files shown by the last scan may be recycled or opened.
allowed_files = set()
dupe_groups = []


def start_file_scan(kind, root, min_mb):
    root = Path(root or (DOWNLOADS if kind == "downloads" else HOME))
    if not root.is_dir():
        return {"ok": False, "message": "That folder does not exist."}
    if fjob["running"]:
        return {"ok": False, "message": "A file scan is already running."}
    fjob.update(running=True, kind=kind, progress="Starting…", files=0, result=None, error="")

    def work():
        global dupe_groups
        try:
            if kind in ("large", "downloads"):
                res = find_large(root, float(min_mb or 100), fjob) if kind == "large" \
                    else find_old_downloads(root, int(float(min_mb or 60)), fjob)
                paths = [f["path"] for f in res["files"]]
                dupe_groups = []
            else:
                res = find_duplicates(root, float(min_mb or 1), fjob)
                dupe_groups = [[f["path"] for f in g["files"]] for g in res["groups"]]
                paths = [p for g in dupe_groups for p in g]
            allowed_files.clear()
            allowed_files.update(paths)
            fjob["result"] = res
        except Exception as ex:
            fjob["error"] = str(ex)
        fjob["running"] = False

    threading.Thread(target=work, daemon=True).start()
    return {"ok": True}


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort),
                ("fAnyOperationsAborted", wintypes.BOOL), ("hNameMappings", ctypes.c_void_p),
                ("lpszProgressTitle", wintypes.LPCWSTR)]


def send_to_recycle_bin(paths):
    """Move several files to the Recycle Bin in one Windows call (much faster than one by one)."""
    FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 3, 0x4, 0x10, 0x40, 0x400
    op = SHFILEOPSTRUCTW(wFunc=FO_DELETE, pFrom="\0".join(paths) + "\0",
                         fFlags=FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI)
    ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))


rjob = {"running": False, "done": 0, "total": 0, "result": None}
# Windows can take 1-3 s per file to move into the Recycle Bin, so batches are
# small enough for the page to show steady progress.
RECYCLE_BATCH = 10


def recycle_files(paths, permanent=False):
    """Start removing the chosen files in the background; the page polls rjob.
    Recycle Bin by default. Permanent delete is only allowed for extra copies of
    duplicates, where an identical copy is kept, so nothing is lost."""
    if rjob["running"]:
        return {"ok": False, "message": "Still moving the previous files."}
    paths = [p for p in paths if p in allowed_files]
    chosen = set(paths)
    for group in dupe_groups:  # always keep at least one copy of every duplicate
        if group and all(p in chosen for p in group):
            return {"ok": False, "message": "Keep at least one copy of each duplicate."}
    if permanent:
        kept_copy = {p for g in dupe_groups for p in g
                     if any(q not in chosen and os.path.exists(q) for q in g)}
        if not paths or not all(p in kept_copy for p in paths):
            return {"ok": False, "message": "Permanent delete is only for extra copies of duplicates."}
    rjob.update(running=True, done=0, total=len(paths), result=None)

    def work():
        res = remove_files_now(paths, permanent, lambda n: rjob.update(done=n))
        rjob.update(running=False, result=res)

    threading.Thread(target=work, daemon=True).start()
    return {"ok": True, "started": True}


def remove_files_now(paths, permanent=False, progress=None):
    """Recycle (or, for checked duplicate copies, delete) files in batches; protected files are skipped."""
    done, failed, freed, todo = [], [], 0, []
    for p in paths:
        if keep_reason(p):
            failed.append({"path": p, "why": keep_reason(p)})
            continue
        try:
            todo.append((p, os.path.getsize(p)))
        except OSError:
            failed.append({"path": p, "why": "File not found"})
    for i in range(0, len(todo), RECYCLE_BATCH):
        batch = todo[i:i + RECYCLE_BATCH]
        if permanent:
            for p, _ in batch:
                try:
                    os.chmod(p, 0o666)
                    os.remove(p)
                except OSError:
                    pass
        else:
            send_to_recycle_bin([p for p, _ in batch])
        for p, size in batch:  # Windows reports per batch, so check each file
            if os.path.exists(p):
                failed.append({"path": p, "why": "In use or no permission"})
            else:
                done.append(p)
                freed += size
                allowed_files.discard(p)
        if progress:
            progress(len(done) + len(failed))
    write_log("recycle", {"items": [{"name": "Deleted extra copies" if permanent else "Moved to Recycle Bin",
                                     "removed": len(done),
                                     "skipped": len(failed), "freed": freed}], "files": done})
    return {"ok": True, "done": done, "failed": failed, "freed": freed}


def open_in_explorer(path):
    if path in allowed_files and os.path.exists(path):
        subprocess.Popen(["explorer", "/select,", path])
        return {"ok": True}
    return {"ok": False}


# ---------- Startup apps ----------
# Turning an app off works the same way as Task Manager's Startup tab: a flag in
# the StartupApproved key. The app itself is not changed and can be turned back on.

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved"
HKCU, HKLM = winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE
APPDATA = Path(os.environ.get("APPDATA", HOME / "AppData" / "Roaming"))

# source id: (where it is listed, registry Run key or startup folder, approved hive, approved sub-key, admin)
STARTUP_SOURCES = {
    "user": ("Registry (you)", (HKCU, RUN_KEY), HKCU, "Run", False),
    "machine": ("Registry (all users)", (HKLM, RUN_KEY), HKLM, "Run", True),
    "machine32": ("Registry (all users, 32-bit)",
                  (HKLM, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run"), HKLM, "Run32", True),
    "folder": ("Startup folder (you)",
               APPDATA / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup", HKCU, "StartupFolder", False),
    "commonfolder": ("Startup folder (all users)",
                     PROGRAMDATA / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "StartUp", HKLM, "StartupFolder", True),
}


def approved_enabled(hive, sub, name):
    try:
        with winreg.OpenKey(hive, f"{APPROVED_KEY}\\{sub}", 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            data, _ = winreg.QueryValueEx(k, name)
            return not (data and data[0] & 1)  # odd first byte = turned off
    except OSError:
        return True


def list_startup():
    admin = is_admin()
    items = []
    for sid, (where, loc, hive, sub, needs_admin) in STARTUP_SOURCES.items():
        entries = []
        if isinstance(loc, tuple):
            try:
                with winreg.OpenKey(loc[0], loc[1], 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
                    i = 0
                    while True:
                        try:
                            name, cmd, _ = winreg.EnumValue(k, i)
                        except OSError:
                            break
                        entries.append((name, str(cmd)))
                        i += 1
            except OSError:
                pass
        elif loc.is_dir():
            entries = [(f.name, str(f)) for f in loc.iterdir()
                       if f.is_file() and f.name.lower() != "desktop.ini"]
        for name, cmd in entries:
            items.append({"id": f"{sid}|{name}", "name": Path(name).stem if sid.endswith("folder") else name,
                          "command": cmd, "where": where, "enabled": approved_enabled(hive, sub, name),
                          "needs_admin": needs_admin and not admin})
    return sorted(items, key=lambda x: x["name"].lower())


def set_startup(item_id, enable):
    sid, _, name = (item_id or "").partition("|")
    if sid not in STARTUP_SOURCES or not name:
        return {"ok": False, "message": "Unknown startup item"}
    _, _, hive, sub, needs_admin = STARTUP_SOURCES[sid]
    if needs_admin and not is_admin():
        return {"ok": False, "message": "This one needs Go Flow (Admin)."}
    if enable:
        data = b"\x02" + b"\x00" * 11
    else:
        filetime = int((time.time() + 11644473600) * 10_000_000)
        data = b"\x03\x00\x00\x00" + struct.pack("<Q", filetime)
    try:
        with winreg.CreateKeyEx(hive, f"{APPROVED_KEY}\\{sub}", 0,
                                winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as k:
            winreg.SetValueEx(k, name, 0, winreg.REG_BINARY, data)
    except OSError as ex:
        return {"ok": False, "message": str(ex)}
    write_log("startup", {"action": f"{'Turned on' if enable else 'Turned off'} startup: {name}", "ok": True})
    return {"ok": True}


# ---------- Health: disk, battery, memory ----------

HEALTH_PS = r"""
$o=[ordered]@{}
$o.disks=@(Get-PhysicalDisk | ForEach-Object { $r=$null
  try { $r = $_ | Get-StorageReliabilityCounter -ErrorAction Stop } catch {}
  [pscustomobject]@{name=$_.FriendlyName; type=[string]$_.MediaType; health=[string]$_.HealthStatus; size=[int64]$_.Size;
    temp=$(if($r){$r.Temperature}); wear=$(if($r){$r.Wear}); hours=$(if($r){$r.PowerOnHours})} })
$os=Get-CimInstance Win32_OperatingSystem
$o.os=$os.Caption; $o.mem_total=[int64]$os.TotalVisibleMemorySize*1024; $o.mem_free=[int64]$os.FreePhysicalMemory*1024
$o.uptime_hours=[int]((Get-Date)-$os.LastBootUpTime).TotalHours
$o.top_memory=@(Get-Process | Group-Object ProcessName | ForEach-Object { [pscustomobject]@{name=$_.Name; count=$_.Count;
  mem=[int64]($_.Group | Measure-Object WorkingSet64 -Sum).Sum} } | Sort-Object mem -Descending | Select-Object -First 8)
$bat=Get-CimInstance Win32_Battery
if ($bat) {
  $f=Join-Path $env:TEMP 'laptopcleaner_battery.xml'
  powercfg /batteryreport /xml /output $f | Out-Null
  [xml]$x=Get-Content $f; Remove-Item $f
  $b=@($x.BatteryReport.Batteries.Battery)[0]
  $o.battery=[pscustomobject]@{charge=[int]$bat.EstimatedChargeRemaining; plugged=([int]$bat.BatteryStatus -ne 1);
    design=[int]$b.DesignCapacity; full=[int]$b.FullChargeCapacity; cycles=[int]$b.CycleCount}
}
$o | ConvertTo-Json -Depth 4
"""


def health():
    r = powershell(HEALTH_PS, timeout=120)
    try:
        h = json.loads(r["out"])
    except Exception:
        return {"error": r["err"] or "Could not read health data"}
    for d in h.get("disks") or []:
        for k in ("temp", "wear", "hours"):  # PowerShell sends {} when not readable without admin
            if not isinstance(d.get(k), (int, float)):
                d[k] = None
    h["drives"] = []
    for d in os.listdrives():
        try:
            du = shutil.disk_usage(d)
            h["drives"].append({"name": d, "total": du.total, "free": du.free})
        except OSError:
            pass
    tips = []
    for d in h["drives"]:
        if d["total"] and d["free"] / d["total"] < 0.15:
            tips.append(f"Drive {d['name']} is {100 - d['free'] * 100 // d['total']}% full. Windows slows down above 85%: run the Junk cleaner and check Large files.")
    for d in h.get("disks") or []:
        if d.get("health") and d["health"] != "Healthy":
            tips.append(f"Disk {d['name']} reports '{d['health']}'. Back up your files now.")
        if d.get("wear") and d["wear"] >= 80:
            tips.append(f"Disk {d['name']} is {d['wear']}% worn. Plan a replacement and keep backups.")
    b = h.get("battery")
    if b and b.get("design"):
        b["health"] = round(b["full"] * 100 / b["design"])
        if b["health"] < 60:
            tips.append(f"Battery holds only {b['health']}% of its original charge. A replacement would help.")
        elif b["health"] < 80:
            tips.append(f"Battery holds {b['health']}% of its original charge. Avoid keeping it at 100% all day.")
    if h.get("mem_total") and (h["mem_total"] - h["mem_free"]) / h["mem_total"] > 0.85:
        tips.append("Memory is over 85% used. Close apps you are not using (see the list below).")
    if h.get("uptime_hours", 0) > 24 * 7:
        tips.append(f"The laptop has not restarted for {h['uptime_hours'] // 24} days. A restart clears memory and finishes updates.")
    h["tips"] = tips
    return h


# ---------- Speed: memory, heavy apps, trim ----------

class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def memory():
    m = MEMORYSTATUSEX(dwLength=ctypes.sizeof(MEMORYSTATUSEX))
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return {"total": m.ullTotalPhys, "free": m.ullAvailPhys, "load": m.dwMemoryLoad}


# Windows parts and Go Flow itself: never offered for closing.
SYSTEM_PROCS = {"system", "idle", "registry", "smss", "csrss", "wininit", "winlogon", "services", "lsass",
                "lsaiso", "svchost", "dwm", "explorer", "fontdrvhost", "memory compression", "msmpeng",
                "nissrv", "securityhealthservice", "securityhealthsystray", "sihost", "ctfmon", "audiodg",
                "spoolsv", "wmiprvse", "runtimebroker", "conhost", "taskhostw", "searchhost", "startmenuexperiencehost",
                "shellexperiencehost", "textinputhost", "dllhost", "smartscreen", "wudfhost", "lockapp",
                "applicationframehost", "systemsettings", "searchindexer", "msedgewebview2", "python", "pythonw",
                "powershell", "cmd", "mpdefendercoreservice", "widgetservice", "phoneexperiencehost",
                "secure system", "vmmemcmzygote", "aggregatorhost", "usocoreworker"}

APPS_PS = r"""
$a=@{}; Get-Process | ForEach-Object { try { $a[$_.Id]=$_.TotalProcessorTime.TotalMilliseconds } catch {} }
Start-Sleep -Milliseconds 1000
$n=[Environment]::ProcessorCount
ConvertTo-Json -Depth 3 -InputObject @(Get-Process | Group-Object ProcessName | ForEach-Object {
  $cpu=0; foreach ($p in $_.Group) { try { if ($a.ContainsKey($p.Id)) { $cpu += $p.TotalProcessorTime.TotalMilliseconds - $a[$p.Id] } } catch {} }
  $w=@($_.Group | Where-Object { $_.MainWindowHandle -ne 0 })
  $d=@($_.Group | Where-Object { $_.Description } | Select-Object -First 1)
  [pscustomobject]@{name=$_.Name; count=$_.Count; mem=[int64]($_.Group | Measure-Object WorkingSet64 -Sum).Sum;
    cpu=[math]::Round($cpu/10/$n,1); window=($w.Count -gt 0); desc=$(if($d){[string]$d[0].Description}else{''})}
} | Sort-Object mem -Descending | Select-Object -First 25)
"""

shown_apps = set()


def heavy_apps():
    r = powershell(APPS_PS, timeout=60)
    try:
        apps = json.loads(r["out"] or "[]")
    except ValueError:
        return {"error": r["err"] or "Could not list apps"}
    for a in apps:
        a["system"] = a["name"].lower() in SYSTEM_PROCS
    shown_apps.clear()
    shown_apps.update(a["name"] for a in apps if not a["system"])
    m = memory()
    return {"apps": apps, "memory": m, "power": power_mode()}


def close_apps(names, force=False):
    """Close whole apps by name. Normal close lets them ask to save; force does not."""
    before = memory()["free"]
    done, failed = [], []
    for name in names:
        if name not in shown_apps or name.lower() in SYSTEM_PROCS:
            failed.append({"name": name, "why": "Not allowed"})
            continue
        args = ["taskkill", "/IM", f"{name}.exe", "/T"] + (["/F"] if force else [])
        p = subprocess.run(args, capture_output=True, text=True, creationflags=0x08000000)
        (done if p.returncode == 0 else failed).append(
            {"name": name, "why": "" if p.returncode == 0 else "It has no window to close; use Force close"})
    time.sleep(2)  # give apps a moment to exit
    freed = max(0, memory()["free"] - before)
    write_log("speed", {"action": f"{'Force closed' if force else 'Closed'}: {', '.join(d['name'] for d in done) or 'nothing'} "
                                  f"(+{freed / 1048576:.0f} MB free)", "ok": bool(done)})
    return {"ok": bool(done), "done": done, "failed": failed, "freed": freed}


def trim_memory():
    """Ask every process we may touch to give back memory it is not using right now.
    Short-term help when RAM is full: Windows does the same on its own under pressure."""
    before = memory()["free"]
    pids = (wintypes.DWORD * 4096)()
    needed = wintypes.DWORD()
    ctypes.windll.psapi.EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(needed))
    trimmed = 0
    for pid in pids[: needed.value // ctypes.sizeof(wintypes.DWORD)]:
        h = ctypes.windll.kernel32.OpenProcess(0x1000 | 0x0100, False, pid)  # QUERY_LIMITED_INFORMATION | SET_QUOTA
        if h:
            trimmed += bool(ctypes.windll.psapi.EmptyWorkingSet(h))
            ctypes.windll.kernel32.CloseHandle(h)
    time.sleep(1)
    freed = max(0, memory()["free"] - before)
    write_log("speed", {"action": f"Memory trim: {trimmed} processes, +{freed / 1048576:.0f} MB free", "ok": True})
    return {"ok": True, "processes": trimmed, "freed": freed, "memory": memory()}


POWER_MODES = {"": "Balanced", "00000000-0000-0000-0000-000000000000": "Balanced",
               "961cc777-2547-4f9d-8174-7d86181b8a7a": "Best power efficiency",
               "ded574b5-45a0-4f42-8737-46345c09c238": "Best performance"}


def power_mode():
    try:
        with winreg.OpenKey(HKLM, r"SYSTEM\CurrentControlSet\Control\Power\User\PowerSchemes", 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            on_ac = True
            try:
                status = ctypes.c_byte * 12
                s = status()
                ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s))
                on_ac = s[0] == 1  # ACLineStatus
            except Exception:
                pass
            name = "ActiveOverlayAcPowerScheme" if on_ac else "ActiveOverlayDcPowerScheme"
            try:
                guid = str(winreg.QueryValueEx(k, name)[0]).lower()
            except OSError:
                guid = ""
            return {"mode": POWER_MODES.get(guid, "Custom"), "plugged": on_ac}
    except OSError:
        return {"mode": "Unknown", "plugged": True}


def open_power_settings():
    os.startfile("ms-settings:powersleep")
    return {"ok": True}


# ---------- Windows Update ----------
# Read-only: shows the state and history. Installing stays in Windows Settings.

UPDATES_PS = r"""
function Local($d) { if ($d -and $d.Year -gt 2000) { [DateTime]::SpecifyKind($d,'Utc').ToLocalTime() } }
$o=[ordered]@{}
$cv=Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
$o.version=[string]$cv.DisplayVersion; $o.build="$($cv.CurrentBuild).$($cv.UBR)"
$o.reboot=(Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired') -or
          (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending')
try { if ((New-Object -ComObject Microsoft.Update.SystemInfo).RebootRequired) { $o.reboot=$true } } catch {}
$o.paused_until=''
try { $p=(Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -ErrorAction Stop).PauseUpdatesExpiryTime
      if ($p -and [datetime]$p -gt (Get-Date)) { $o.paused_until=([datetime]$p).ToString('dd MMM yyyy') } } catch {}
$o.last_check=''; $o.last_install=''; $o.check_days=-1
try { $r=(New-Object -ComObject Microsoft.Update.AutoUpdate).Results
      $c=Local $r.LastSearchSuccessDate; $i=Local $r.LastInstallationSuccessDate
      if ($c) { $o.last_check=$c.ToString('dd MMM yyyy HH:mm'); $o.check_days=[int]((Get-Date)-$c).TotalDays }
      if ($i) { $o.last_install=$i.ToString('dd MMM yyyy HH:mm') } } catch {}
$o.history=@()
try { $s=(New-Object -ComObject Microsoft.Update.Session).CreateUpdateSearcher()
      $n=$s.GetTotalHistoryCount()
      if ($n -gt 0) { $o.history=@($s.QueryHistory(0,[Math]::Min($n,25)) | Where-Object { $_.Title } | ForEach-Object {
        [pscustomobject]@{title=$_.Title; date=(Local $_.Date).ToString('dd MMM yyyy'); result=[int]$_.ResultCode} }) } } catch {}
$o | ConvertTo-Json -Depth 4
"""

PENDING_PS = r"""
$s=(New-Object -ComObject Microsoft.Update.Session).CreateUpdateSearcher()
$r=$s.Search("IsInstalled=0 and IsHidden=0")
[pscustomobject]@{updates=@($r.Updates | ForEach-Object { [pscustomobject]@{
  title=$_.Title; size=[int64]$_.MaxDownloadSize; downloaded=[bool]$_.IsDownloaded;
  severity=[string]$_.MsrcSeverity; driver=([int]$_.Type -eq 2); optional=(-not $_.AutoSelectOnWebSites) } })} | ConvertTo-Json -Depth 4
"""


def update_status():
    r = powershell(UPDATES_PS, timeout=120)
    try:
        u = json.loads(r["out"])
    except Exception:
        return {"error": r["err"] or "Could not read Windows Update"}
    for h in u.get("history") or []:
        # Store apps show as "<12-char store id>-<Publisher.App>"
        m = re.match(r"^[0-9A-Z]{12}-(.+)$", h["title"])
        if m:
            h["title"] = f"Microsoft Store app: {m.group(1)}"
    tips = []
    if u.get("reboot"):
        tips.append("Restart the laptop to finish installing updates.")
    if u.get("paused_until"):
        tips.append(f"Updates are paused until {u['paused_until']}. Security fixes wait until then.")
    if u.get("check_days", -1) > 7:
        tips.append(f"Windows has not checked for updates for {u['check_days']} days. Open Windows Update and click Check for updates.")
    failed = [h for h in u.get("history") or [] if h["result"] in (4, 5)]
    store = [h for h in failed if h["title"].startswith("Microsoft Store app:")]
    if len(failed) > len(store):
        tips.append(f"{len(failed) - len(store)} recent Windows update(s) failed. Windows usually retries; if one keeps failing, run the Windows Update troubleshooter.")
    if store:
        names = ", ".join(h["title"].split(": ", 1)[1] for h in store)
        tips.append(f"Store app update failed ({names}). Open Microsoft Store > Library and click Get updates.")
    u["tips"] = tips
    return u


ujob = {"running": False, "result": None, "error": ""}


def start_update_search():
    if ujob["running"]:
        return {"ok": True}
    ujob.update(running=True, result=None, error="")

    def work():
        r = powershell(PENDING_PS, timeout=600)
        try:
            ujob["result"] = json.loads(r["out"])["updates"]
        except Exception:
            ujob["error"] = r["err"] or "Could not search for updates (no internet?)"
        ujob["running"] = False

    threading.Thread(target=work, daemon=True).start()
    return {"ok": True}


def open_windows_update(check=False):
    os.startfile("ms-settings:windowsupdate-action" if check else "ms-settings:windowsupdate")
    return {"ok": True}


# ---------- Installed programs ----------

UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
UNINSTALL_VIEWS = {"hklm64": (HKLM, winreg.KEY_WOW64_64KEY), "hklm32": (HKLM, winreg.KEY_WOW64_32KEY),
                   "hkcu": (HKCU, 0)}


def reg_values(key):
    vals, i = {}, 0
    while True:
        try:
            name, value, _ = winreg.EnumValue(key, i)
        except OSError:
            return vals
        vals[name] = value
        i += 1


def installed_programs():
    seen, items = set(), []
    for view, (hive, flag) in UNINSTALL_VIEWS.items():
        try:
            root = winreg.OpenKey(hive, UNINSTALL_KEY, 0, winreg.KEY_READ | flag)
        except OSError:
            continue
        with root:
            for i in range(winreg.QueryInfoKey(root)[0]):
                sub = winreg.EnumKey(root, i)
                try:
                    with winreg.OpenKey(root, sub) as k:
                        v = reg_values(k)
                except OSError:
                    continue
                name = str(v.get("DisplayName", "")).strip()
                # Hide Windows parts and update packages, as Windows' own list does.
                if not name or v.get("SystemComponent") == 1 or v.get("ParentKeyName") \
                        or v.get("ReleaseType") in ("Update", "Hotfix", "Security Update"):
                    continue
                key = (name.lower(), str(v.get("DisplayVersion", "")))
                if key in seen:
                    continue
                seen.add(key)
                d = str(v.get("InstallDate", ""))
                items.append({"id": f"{view}|{sub}", "name": name, "publisher": str(v.get("Publisher", "")),
                              "version": str(v.get("DisplayVersion", "")),
                              "size": int(v.get("EstimatedSize") or 0) * 1024,
                              "installed": f"{d[6:8]}-{d[4:6]}-{d[:4]}" if len(d) == 8 and d.isdigit() else "",
                              "can_uninstall": bool(v.get("UninstallString"))})
    return sorted(items, key=lambda x: -x["size"])


def uninstall_program(item_id):
    view, _, sub = (item_id or "").partition("|")
    if view not in UNINSTALL_VIEWS or not sub:
        return {"ok": False, "message": "Unknown program"}
    hive, flag = UNINSTALL_VIEWS[view]
    try:
        with winreg.OpenKey(hive, f"{UNINSTALL_KEY}\\{sub}", 0, winreg.KEY_READ | flag) as k:
            v = reg_values(k)
    except OSError:
        return {"ok": False, "message": "Program not found. It may already be removed."}
    cmd = str(v.get("UninstallString", ""))
    if not cmd:
        return {"ok": False, "message": "This program has no uninstaller. Use Windows Settings > Apps."}
    if "msiexec" in cmd.lower():
        cmd = cmd.replace("/I{", "/X{").replace("/i{", "/X{")  # MSI: /I would repair, /X removes
    # Starts the program's own uninstaller, exactly as Windows Settings does.
    subprocess.Popen(cmd, shell=True)
    write_log("program", {"action": f"Started uninstaller: {v.get('DisplayName', sub)}", "ok": True})
    return {"ok": True}


def open_apps_settings():
    os.startfile("ms-settings:appsfeatures")
    return {"ok": True}


# ---------- Weekly auto-clean ----------
# A Windows scheduled task runs "cleaner.py --auto": safe junk only (never the
# Recycle Bin, large files or duplicates), then a Defender update and quick scan.

TASK_NAME = "GoFlow Weekly"
AUTO_IDS = {"user_temp", "win_temp", "chrome", "edge", "wu", "crash", "thumbs"}
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def ps_quote(s):
    return "'" + str(s).replace("'", "''") + "'"


def auto_run():
    admin = is_admin()
    ids = {c["id"] for c in CATEGORIES if c["id"] in AUTO_IDS and (admin or not c["admin"])
           and not (c.get("browser") and running(c["browser"]))}
    if admin and protection_on():  # system folders are cleaned too: make an undo point first
        create_restore_point("Before Go Flow weekly clean")
    report = clean(ids)
    freed = sum(r["freed"] for r in report)
    update = powershell("Update-MpSignature")
    scan_r = powershell("Start-MpScan -ScanType QuickScan")
    threats = [t for t in defender_threats() if t.get("active")]
    write_log("auto", {"action": f"Weekly auto-clean: freed {freed / 1048576:.0f} MB, definitions "
                                 f"{'updated' if update['ok'] else 'not updated'}, quick scan "
                                 f"{'done' if scan_r['ok'] else 'failed'}, {len(threats)} active threats",
                       "ok": scan_r["ok"] and not threats})
    if threats:
        # Only a message box: removing threats is left to you, from the Virus scan tab.
        ctypes.windll.user32.MessageBoxW(None, f"Defender found {len(threats)} active threat(s). Open Go Flow > Virus scan.",
                                         "Go Flow", 0x30)


def schedule_status():
    r = powershell(f"$t=Get-ScheduledTask -TaskName {ps_quote(TASK_NAME)} -ErrorAction SilentlyContinue;"
                   "if($t){$i=$t|Get-ScheduledTaskInfo;$g=$t.Triggers[0];"
                   "[pscustomobject]@{on=$true;day=[string]$g.DaysOfWeek;time=$g.StartBoundary;"
                   "admin=($t.Principal.RunLevel -eq 'Highest');"
                   "last=$(if($i.LastRunTime -and $i.LastRunTime.Year -gt 2000){$i.LastRunTime.ToString('dd MMM yyyy HH:mm')}else{''});"
                   "next=$(if($i.NextRunTime){$i.NextRunTime.ToString('dd MMM yyyy HH:mm')}else{''})}|ConvertTo-Json}"
                   "else{'{\"on\":false}'}", timeout=60)
    try:
        s = json.loads(r["out"])
    except Exception:
        return {"on": False}
    if s.get("time"):
        s["time"] = s["time"][11:16]
    if str(s.get("day", "")).isdigit():  # Windows sends a bit mask: Sunday=1, Monday=2, ... Saturday=64
        bits = int(s["day"])
        week = ["Sunday"] + DAYS[:6]
        s["day"] = next((d for i, d in enumerate(week) if bits & (1 << i)), "Sunday")
    return s


def schedule_set(on, day="Sunday", at="12:00"):
    if not on:
        r = powershell(f"Unregister-ScheduledTask -TaskName {ps_quote(TASK_NAME)} -Confirm:$false", timeout=60)
        write_log("schedule", {"action": "Weekly auto-clean turned off", "ok": r["ok"]})
        return {"ok": r["ok"], "message": r["err"]}
    if day not in DAYS:
        return {"ok": False, "message": "Pick a day"}
    try:
        datetime.strptime(at, "%H:%M")
    except (TypeError, ValueError):
        return {"ok": False, "message": "Pick a time like 13:00"}
    exe, args = self_command("--auto")
    level = "-RunLevel Highest" if is_admin() else ""
    cmd = (f"$a=New-ScheduledTaskAction -Execute {ps_quote(exe)} -Argument {ps_quote(args)} "
           f"-WorkingDirectory {ps_quote(HERE)};"
           f"$g=New-ScheduledTaskTrigger -Weekly -DaysOfWeek {day} -At {at};"
           "$s=New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
           "-ExecutionTimeLimit (New-TimeSpan -Hours 2);"
           f"Register-ScheduledTask -TaskName {ps_quote(TASK_NAME)} -Action $a -Trigger $g -Settings $s {level} "
           "-Description 'Go Flow: weekly safe junk clean + Defender quick scan' -Force | Out-Null")
    r = powershell(cmd, timeout=60)
    write_log("schedule", {"action": f"Weekly auto-clean on: {day} {at}", "ok": r["ok"]})
    return {"ok": r["ok"], "message": r["err"]}


def auto_run_now():
    exe, args = self_command("--auto")
    started = run_job("Auto-clean (junk + definitions + quick scan)",
                      f"Start-Process -Wait -WindowStyle Hidden -FilePath {ps_quote(exe)} -ArgumentList {ps_quote(args)}")
    return {"ok": started, "message": "" if started else "Another job is still running."}


# ---------- Restore points ----------
# A Windows restore point lets you undo system changes. Needs admin rights and
# System Protection turned on for C:. Windows allows one new point per 24 hours.

def protection_on():
    try:
        with winreg.OpenKey(HKLM, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\SystemRestore", 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            return winreg.QueryValueEx(k, "RPSessionInterval")[0] == 1
    except OSError:
        return False


def restore_status():
    s = {"admin": is_admin(), "protection": protection_on(), "points": []}
    if s["admin"]:
        r = powershell("ConvertTo-Json -InputObject @(Get-ComputerRestorePoint | Sort-Object SequenceNumber -Descending |"
                       " Select-Object -First 10 | ForEach-Object { [pscustomobject]@{name=$_.Description;"
                       " date=([Management.ManagementDateTimeConverter]::ToDateTime($_.CreationTime)).ToString('dd MMM yyyy HH:mm')} })",
                       timeout=60)
        try:
            s["points"] = json.loads(r["out"] or "[]")
        except ValueError:
            pass
    return s


def create_restore_point(description="Go Flow"):
    if not is_admin():
        return {"ok": False, "message": "Creating a restore point needs the Admin shortcut."}
    if not protection_on():
        return {"ok": False, "message": "System Protection is off, so Windows cannot make restore points. Turn it on in the Protection tab."}
    r = powershell(f"Checkpoint-Computer -Description {ps_quote(description)} -RestorePointType MODIFY_SETTINGS "
                   "-WarningVariable w -WarningAction SilentlyContinue; if ($w) { 'WARN: ' + $w[0] }", timeout=900)
    out = r["out"] + r["err"]
    if "1440" in out or "past" in out.lower():
        res = {"ok": True, "message": "A restore point from the last 24 hours already exists, so Windows kept that one."}
    elif r["ok"] and "WARN" not in out:
        res = {"ok": True, "message": "Restore point created."}
    else:
        res = {"ok": False, "message": out.strip() or "Could not create a restore point."}
    write_log("restore", {"action": f"Restore point: {res['message']}", "ok": res["ok"]})
    return res


def enable_protection():
    if not is_admin():
        return {"ok": False, "message": "Needs the Admin shortcut. Or use 'Open System Protection' and turn it on there."}
    r = powershell("Enable-ComputerRestore -Drive \"$env:SystemDrive\\\"", timeout=120)
    write_log("restore", {"action": "Turned on System Protection for C:", "ok": r["ok"]})
    return {"ok": r["ok"], "message": r["err"]}


def open_protection_settings():
    subprocess.Popen(["SystemPropertiesProtection.exe"])
    return {"ok": True}


# ---------- Alerts: low disk space, Defender off ----------
# A scheduled task runs "cleaner.py --check-alerts <percent>" every hour and at
# sign-in. It shows a Windows notification at most once a day per problem.

ALERT_TASK = "GoFlow Alerts"
ALERT_STATE = LOG_DIR / "alert_state.json"


def notify(title, text):
    ps = ("[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null;"
          "$x=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
          f"$t=$x.GetElementsByTagName('text');$t[0].AppendChild($x.CreateTextNode({ps_quote(title)}))|Out-Null;"
          f"$t[1].AppendChild($x.CreateTextNode({ps_quote(text)}))|Out-Null;"
          "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
          "'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe')"
          ".Show([Windows.UI.Notifications.ToastNotification]::new($x))")
    if not powershell(ps, timeout=60)["ok"]:
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x30)


def current_alerts(min_free_pct):
    alerts = []
    for d in os.listdrives():
        try:
            du = shutil.disk_usage(d)
        except OSError:
            continue
        if du.total and du.free * 100 / du.total < min_free_pct:
            alerts.append(("disk:" + d, f"Drive {d} is almost full",
                           f"Only {du.free / 1073741824:.1f} GB free. Open Go Flow: Junk cleaner and Large files."))
    s = powershell("(Get-MpComputerStatus).RealTimeProtectionEnabled", timeout=60)
    # Another active antivirus puts Defender in passive mode: that is still protected.
    if s["ok"] and s["out"].strip().lower() == "false" and not active_antivirus():
        alerts.append(("defender", "Virus protection is off",
                       "No antivirus is protecting this laptop. Turn on Microsoft Defender in Windows Security."))
    return alerts


def check_alerts(min_free_pct):
    try:
        state = json.loads(ALERT_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    today = datetime.now().strftime("%Y-%m-%d")
    for key, title, text in current_alerts(min_free_pct):
        if state.get(key) != today:  # once a day per problem
            notify(title, text)
            state[key] = today
            write_log("alert", {"action": f"Alert: {title}", "ok": False})
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ALERT_STATE.write_text(json.dumps(state), encoding="utf-8")


def alerts_status():
    r = powershell(f"$t=Get-ScheduledTask -TaskName {ps_quote(ALERT_TASK)} -ErrorAction SilentlyContinue;"
                   "if($t){[pscustomobject]@{on=$true;args=$t.Actions[0].Arguments}|ConvertTo-Json}else{'{\"on\":false}'}",
                   timeout=60)
    try:
        s = json.loads(r["out"])
    except ValueError:
        s = {"on": False}
    m = re.search(r"--check-alerts (\d+)", s.pop("args", "") or "")
    s["percent"] = int(m.group(1)) if m else 10
    return s


def alerts_set(on, percent=10):
    if not on:
        r = powershell(f"Unregister-ScheduledTask -TaskName {ps_quote(ALERT_TASK)} -Confirm:$false", timeout=60)
        write_log("schedule", {"action": "Alerts turned off", "ok": r["ok"]})
        return {"ok": r["ok"], "message": r["err"]}
    try:
        percent = max(1, min(50, int(percent)))
    except (TypeError, ValueError):
        return {"ok": False, "message": "Pick a percentage"}
    exe, args = self_command(f"--check-alerts {percent}")
    cmd = (f"$a=New-ScheduledTaskAction -Execute {ps_quote(exe)} -Argument {ps_quote(args)} -WorkingDirectory {ps_quote(HERE)};"
           "$g=@((New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME),"
           "(New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5) -RepetitionInterval (New-TimeSpan -Hours 1)));"
           "$s=New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
           "-ExecutionTimeLimit (New-TimeSpan -Minutes 10);"
           f"Register-ScheduledTask -TaskName {ps_quote(ALERT_TASK)} -Action $a -Trigger $g -Settings $s "
           "-Description 'Go Flow: low disk space and Defender-off alerts' -Force | Out-Null")
    r = powershell(cmd, timeout=60)
    write_log("schedule", {"action": f"Alerts on: below {percent}% free", "ok": r["ok"]})
    return {"ok": r["ok"], "message": r["err"]}


def alerts_test():
    notify("Go Flow", "Alerts work. You will see messages like this when space runs low.")
    return {"ok": True}


# ---------- Home: overview and health score ----------

def reboot_pending():
    for key in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired",
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending"):
        try:
            winreg.CloseKey(winreg.OpenKey(HKLM, key, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY))
            return True
        except OSError:
            pass
    return False


# Windows Security Center lists every antivirus; another one being active puts
# Defender in passive mode, which is fine. productState bits 12-15 = 1 means on.
AV_PS = ("$av=@(Get-CimInstance -Namespace root/SecurityCenter2 -ClassName AntivirusProduct -ErrorAction SilentlyContinue |"
         " Where-Object { (($_.productState -shr 12) -band 0xF) -eq 1 } | ForEach-Object { $_.displayName });")


def active_antivirus():
    r = powershell(AV_PS + "ConvertTo-Json -InputObject @($av)", timeout=60)
    try:
        return json.loads(r["out"] or "[]")
    except ValueError:
        return []


def overview():
    """Quick checks for the Home screen: a 0-100 score and what to fix, each linked to a screen."""
    r = powershell(AV_PS + "$s=Get-MpComputerStatus;"
                   f"$w=[bool](Get-ScheduledTask -TaskName {ps_quote(TASK_NAME)} -ErrorAction SilentlyContinue);"
                   f"$a=[bool](Get-ScheduledTask -TaskName {ps_quote(ALERT_TASK)} -ErrorAction SilentlyContinue);"
                   "[pscustomobject]@{rt=[bool]$s.RealTimeProtectionEnabled; sig=[int]$s.AntivirusSignatureAge;"
                   "quick=[int]$s.QuickScanAge; auto=$w; alerts=$a; av=@($av)}|ConvertTo-Json", timeout=60)
    try:
        d = json.loads(r["out"])
    except ValueError:
        d = {}
    du = shutil.disk_usage(os.environ.get("SystemDrive", "C:") + "\\")
    disk_pct = round((du.total - du.free) * 100 / du.total)
    mem = memory()
    av = [n for n in d.get("av") or [] if n]
    others = [n for n in av if "defender" not in n.lower()]
    protected = bool(d.get("rt")) or bool(others)
    o = {"disk_total": du.total, "disk_free": du.free, "disk_pct": disk_pct, "memory": mem,
         "realtime": protected, "antivirus": others[0] if others and not d.get("rt") else "Microsoft Defender",
         "sig_age": d.get("sig"), "quick_age": d.get("quick"),
         "reboot": reboot_pending(), "protection": protection_on(),
         "auto": d.get("auto", False), "alerts": d.get("alerts", False)}
    issues, score = [], 100

    def issue(points, level, text, tab, action):
        nonlocal score
        score -= points
        issues.append({"level": level, "text": text, "tab": tab, "action": action})

    if d and not protected:
        issue(30, "bad", "No antivirus is protecting this laptop right now.", "virus", "Check")
    if d.get("rt") and (d.get("sig") or 0) > 3:
        issue(10, "warn", f"Virus definitions are {d['sig']} days old.", "virus", "Update")
    if (d.get("quick") or 0) > 14:
        issue(5, "warn", f"No virus scan for {d['quick']} days.", "virus", "Scan")
    if disk_pct >= 95:
        issue(25, "bad", f"Disk C: is {disk_pct}% full. Windows slows down badly.", "junk", "Free space")
    elif disk_pct >= 85:
        issue(15, "warn", f"Disk C: is {disk_pct}% full.", "junk", "Free space")
    if mem["load"] >= 90:
        issue(10, "warn", f"Memory is {mem['load']}% used, so apps can feel slow.", "speed", "Boost")
    elif mem["load"] >= 80:
        issue(5, "warn", f"Memory is {mem['load']}% used.", "speed", "Boost")
    if o["reboot"]:
        issue(10, "warn", "Restart needed to finish Windows updates.", "updates", "View")
    if not o["protection"]:
        issue(5, "warn", "No restore points: System Protection is off.", "protect", "Turn on")
    if not o["auto"]:
        issue(3, "info", "Weekly auto-clean is off.", "auto", "Set up")
    if not o["alerts"]:
        issue(2, "info", "Low-disk alerts are off.", "protect", "Set up")
    o["score"] = max(0, score)
    o["issues"] = issues
    return o


# ---------- Threat Check ----------
# Looks for signs of trouble that a virus scan can miss: protection switched
# off, odd things starting with Windows, unsigned programs running from temp
# folders, disguised files, a changed hosts file or proxy. It is NOT an
# antivirus engine: it only reads and reports, and never deletes on a guess.

THREAT_PS = r"""
$o=[ordered]@{}
$o.firewall=@(Get-NetFirewallProfile -ErrorAction SilentlyContinue | ForEach-Object { [pscustomobject]@{name=[string]$_.Name; on=[bool]$_.Enabled} })
$o.uac=[int](Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System' -ErrorAction SilentlyContinue).EnableLUA
$s=Get-MpComputerStatus -ErrorAction SilentlyContinue
$o.rt=[bool]$s.RealTimeProtectionEnabled; $o.tamper=[bool]$s.IsTamperProtected; $o.sac=[string]$s.SmartAppControlState
$o.rdp=((Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server' -ErrorAction SilentlyContinue).fDenyTSConnections -eq 0)
$p=Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction SilentlyContinue
$o.proxy_on=[int]$p.ProxyEnable; $o.proxy=[string]$p.ProxyServer; $o.pac=[string]$p.AutoConfigURL
$o.excl=@(); try { $m=Get-MpPreference -ErrorAction Stop
  $o.excl=@(@($m.ExclusionPath)+@($m.ExclusionProcess)+@($m.ExclusionExtension) | Where-Object { $_ } | ForEach-Object { [string]$_ }) } catch {}
$o.tasks=@(Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object { $_.TaskPath -notlike '\Microsoft\*' -and $_.State -ne 'Disabled' } | ForEach-Object {
  $t=$_; $_.Actions | Where-Object { $_.Execute } | ForEach-Object { [pscustomobject]@{name=[string]$t.TaskName; exe=[string]$_.Execute; args=[string]$_.Arguments} } })
$o.procs=@(Get-Process | Where-Object { $_.Path } | Select-Object -ExpandProperty Path -Unique)
$o.av=@($av)
$o | ConvertTo-Json -Depth 4 -Compress
"""

SUSPICIOUS_CMD = [(r"-e(nc|ncodedcommand)?\s+[a-z0-9+/=]{20,}", "runs a hidden, encoded PowerShell command"),
                  (r"frombase64string", "decodes hidden code"),
                  (r"\bmshta(\.exe)?\b", "uses mshta, a tool malware often abuses"),
                  (r"regsvr32.*(scrobj|/i:http)", "uses regsvr32 to run a script"),
                  (r"rundll32.*(javascript|vbscript)", "uses rundll32 to run a script"),
                  (r"certutil.*-urlcache", "downloads a file with certutil"),
                  (r"\bbitsadmin\b.*(/transfer|/addfile)", "downloads a file with bitsadmin"),
                  (r"(downloadstring|downloadfile|invoke-webrequest|\biwr\b|\bcurl\b).*https?://", "downloads something from the internet"),
                  (r"-w(indowstyle)?\s+h(idden)?\b", "runs in a hidden window")]
SCRIPT_EXT = {".vbs", ".vbe", ".js", ".jse", ".wsf", ".hta", ".ps1", ".bat", ".cmd", ".scr", ".pif", ".com"}
DISGUISED = re.compile(r"\.(pdf|docx?|xlsx?|pptx?|jpe?g|png|gif|txt|mp[34]|zip|rar)\s*\.(exe|scr|com|bat|cmd|pif|vbs|js|hta|lnk|msi)$", re.I)
HOSTS_WATCH = ("microsoft", "windowsupdate", "google", "facebook", "paypal", "amazon", "apple", "bank",
               "netflix", "github", "kaspersky", "avast", "avg", "norton", "mcafee", "malwarebytes", "bitdefender")

# The EICAR test file: a harmless 68-character file every antivirus agrees to
# detect, used to prove a scanner works (eicar.org). Kept in pieces so this
# source file is not itself mistaken for the test file.
EICAR = ("X5O!P%@AP[4\\PZX54(P^)7CC)7}$" + "EICAR-STANDARD-ANTI" + "VIRUS-TEST-FILE!$H+H*").encode("ascii")


def is_eicar(path):
    """True for the standard test file: it must start with the test text and be tiny (at most 128 bytes)."""
    try:
        if os.path.getsize(path) > 128:
            return False
        with open(path, "rb") as f:
            data = f.read(129)
    except OSError:
        return False
    return data.startswith(EICAR) and not data[len(EICAR):].strip(b" \t\r\n\x1a")


tjob = {"running": False, "progress": "", "result": None, "error": ""}
threat_paths = set()  # files this check listed: only these may be shown in Explorer or sent to Defender


def cmd_program(cmd):
    """The program file a command line starts, with %VARIABLES% filled in."""
    cmd = os.path.expandvars(cmd or "").strip()
    m = re.match(r'^"([^"]+)"', cmd) or re.match(r"^(.+?\.(?:exe|com|bat|cmd|vbs|js|ps1|scr|msi|lnk))(?:\s|$)", cmd, re.I)
    return m.group(1) if m else cmd.split(" ")[0]


def location_risk(path):
    """'temp' for throw-away places malware likes, 'user' for anywhere you can write without admin rights."""
    p = os.path.normcase(os.path.abspath(path)) if path else ""
    inside = lambda d: p.startswith(os.path.normcase(str(d)).rstrip("\\") + "\\")
    if any(inside(d) for d in (TEMP, LOCAL / "Temp", WINDIR / "Temp", HOME / "Downloads",
                               Path(os.environ.get("PUBLIC", r"C:\Users\Public")))):
        return "temp"
    if inside(HOME) or inside(PROGRAMDATA):
        return "user"
    return ""


def signatures(paths):
    """{path: (status, signer)} from Windows' own signature check."""
    paths = [p for p in dict.fromkeys(paths) if p and os.path.isfile(p)][:150]
    if not paths:
        return {}
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    listing = LOG_DIR / "threat_paths.json"
    listing.write_text(json.dumps(paths), encoding="utf-8")
    # The parentheses matter: Windows PowerShell hands a JSON list on as ONE object unless it is wrapped.
    r = powershell(f"ConvertTo-Json -Compress -InputObject @((Get-Content -Raw -Encoding UTF8 {ps_quote(listing)} | ConvertFrom-Json) | ForEach-Object {{"
                   " $s=Get-AuthenticodeSignature -LiteralPath $_ -ErrorAction SilentlyContinue;"
                   " [pscustomobject]@{path=$_; status=[string]$s.Status;"
                   " signer=$(if($s.SignerCertificate){$s.SignerCertificate.Subject}else{''})} })", timeout=300)
    try:
        listing.unlink()
    except OSError:
        pass
    out = {}
    try:
        for row in json.loads(r["out"] or "[]"):
            m = re.search(r"CN=\"?([^,\"]+)", row.get("signer") or "")
            out[os.path.normcase(row["path"])] = (row.get("status") or "", m.group(1) if m else "")
    except ValueError:
        pass
    return out


def shortcut_targets(links):
    """{shortcut: program it opens} for .lnk files, so the real program is judged, not the shortcut."""
    links = [l for l in dict.fromkeys(links) if os.path.isfile(l)]
    if not links:
        return {}
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    listing = LOG_DIR / "threat_links.json"
    listing.write_text(json.dumps(links), encoding="utf-8")
    r = powershell(f"$w=New-Object -ComObject WScript.Shell; ConvertTo-Json -Compress -InputObject @((Get-Content -Raw -Encoding UTF8 {ps_quote(listing)} | ConvertFrom-Json) |"
                   " ForEach-Object { [pscustomobject]@{link=$_; target=[string]$w.CreateShortcut($_).TargetPath} })", timeout=60)
    try:
        listing.unlink()
    except OSError:
        pass
    try:
        return {row["link"]: row["target"] for row in json.loads(r["out"] or "[]") if row.get("target")}
    except ValueError:
        return {}


def run_threat_check():
    res = {"sections": []}
    say = lambda t: tjob.update(progress=t)

    def section(title, about):
        s = {"title": title, "about": about, "items": []}
        res["sections"].append(s)
        return s

    def add(s, level, title, detail="", actions=None):
        s["items"].append({"level": level, "title": title, "detail": detail, "actions": actions or []})

    say("Reading protection settings…")
    r = powershell(AV_PS + THREAT_PS, timeout=180)
    try:
        d = json.loads(r["out"])
    except ValueError:
        d = {}

    # 1. Protection
    s = section("Protection", "Is this laptop's own protection switched on?")
    others = [n for n in d.get("av") or [] if n and "defender" not in n.lower()]
    if d.get("rt"):
        add(s, "ok", "Antivirus is on", "Microsoft Defender is protecting this laptop in real time.")
    elif others:
        add(s, "ok", "Antivirus is on", f"{others[0]} is protecting this laptop (Microsoft Defender is on standby).")
    else:
        add(s, "bad", "No antivirus is running", "Real-time protection is off. Turn on Microsoft Defender in Windows Security.",
            [{"kind": "open", "what": "security", "label": "Open Windows Security"}])
    off = [f["name"] for f in d.get("firewall") or [] if not f["on"]]
    if off:
        add(s, "bad", "Firewall is off", f"Off for: {', '.join(off)} network. The firewall blocks unwanted connections.",
            [{"kind": "open", "what": "firewall", "label": "Open Firewall"}])
    elif d.get("firewall"):
        add(s, "ok", "Firewall is on", "On for all network types.")
    if d.get("uac") == 0:
        add(s, "bad", "Windows permission prompts (UAC) are off",
            "Programs can make system changes without asking you. Malware often switches this off.",
            [{"kind": "open", "what": "uac", "label": "Open UAC settings"}])
    else:
        add(s, "ok", "Windows permission prompts (UAC) are on")
    if d.get("sac") == "On":
        add(s, "ok", "Smart App Control is on", "Windows blocks unknown and unsigned apps before they run.")
    if d.get("rdp"):
        add(s, "warn", "Remote Desktop is on", "Other computers can try to sign in to this one. Turn it off if you do not use it.",
            [{"kind": "open", "what": "rdp", "label": "Open Remote Desktop settings"}])
    if d.get("proxy_on") and d.get("proxy"):
        add(s, "warn", "A proxy is set", f"Your web traffic goes through {d['proxy']}. If you did not set this (work or VPN), remove it.",
            [{"kind": "open", "what": "proxy", "label": "Open proxy settings"}])
    if d.get("pac"):
        add(s, "warn", "A proxy script is set", f"Windows loads proxy rules from {d['pac']}. Remove it if you did not set it.",
            [{"kind": "open", "what": "proxy", "label": "Open proxy settings"}])
    excl = [e for e in d.get("excl") or [] if not e.startswith("N/A")]
    hidden = any(e.startswith("N/A") for e in d.get("excl") or [])
    if excl:
        add(s, "warn", f"{len(excl)} antivirus exclusion(s)", "Defender skips these, so malware likes to add itself here: "
            + "; ".join(excl[:6]) + (" …" if len(excl) > 6 else ""),
            [{"kind": "open", "what": "security", "label": "Open Windows Security"}])
    elif hidden:
        add(s, "info", "Antivirus exclusions not checked", "Windows only shows them to administrators. Open Go Flow (Admin) to check.")

    # Collect programs to check signatures for, in one go
    say("Looking at startup programs…")
    startup = list_startup()
    for it in startup:
        it["program"] = cmd_program(it["command"])
    targets = shortcut_targets([it["program"] for it in startup if it["program"].lower().endswith(".lnk")])
    for it in startup:
        it["program"] = targets.get(it["program"], it["program"])
    tasks = [t for t in d.get("tasks") or [] if not t["name"].startswith("GoFlow")]
    for t in tasks:
        t["program"] = cmd_program(t["exe"])
    procs = [p for p in d.get("procs") or [] if location_risk(p)]
    say("Looking at Downloads and Desktop…")
    loose = []
    for root in (DOWNLOADS, HOME / "Desktop"):
        try:
            entries = list(os.scandir(root))
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_file(follow_symlinks=False):
                    loose.append(e.path)
                elif e.is_dir(follow_symlinks=False) and not keep_reason(e.path):
                    loose += [x.path for x in list(os.scandir(e.path))[:400] if x.is_file(follow_symlinks=False)]
            except OSError:
                pass
    loose = loose[:6000]
    programs = [f for f in loose if Path(f).suffix.lower() in (".exe", ".msi", ".scr", ".com")]
    say("Checking program signatures…")
    sig = signatures([it["program"] for it in startup] + [t["program"] for t in tasks] + procs + programs[:60])
    signed = lambda p: sig.get(os.path.normcase(p), ("", ""))
    trust = lambda p: (f"signed by {signed(p)[1]}" if signed(p)[0] == "Valid" else
                       "signature is BROKEN (file changed after signing)" if signed(p)[0] == "HashMismatch" else "not signed")

    def file_acts(path, show=True):
        return [{"kind": "scan", "path": path, "label": "Scan with Defender"},
                {"kind": "vt", "path": path, "label": "Check with VirusTotal"}] + \
               ([{"kind": "show", "path": path, "label": "Show file"}] if show else [])

    def judge(program, command):
        """(level, reasons) for something that runs automatically."""
        reasons, level = [], "ok"
        for pat, why in SUSPICIOUS_CMD:
            if re.search(pat, command, re.I):
                reasons.append(why)
                level = "bad"
        if program and not os.path.isfile(program) and "\\" in program:
            reasons.append("its file no longer exists")
            level = max(level, "info", key=["ok", "info", "warn", "bad"].index)
        elif os.path.isfile(program):
            st, risk = signed(program)[0], location_risk(program)
            if st == "HashMismatch":
                reasons.append("its signature is broken (the file was changed)")
                level = "bad"
            elif st != "Valid" and risk == "temp":
                reasons.append("it is not signed and runs from a temp or download folder")
                level = max(level, "bad", key=["ok", "info", "warn", "bad"].index)
            elif st != "Valid" and risk == "user":
                reasons.append("it is not signed and sits in your user folder")
                level = max(level, "warn", key=["ok", "info", "warn", "bad"].index)
            if Path(program).suffix.lower() in SCRIPT_EXT - {".bat", ".cmd", ".ps1"}:
                reasons.append("it is a script, not a normal program")
                level = max(level, "warn", key=["ok", "info", "warn", "bad"].index)
        return level, reasons

    # 2. Startup programs
    s = section("Starts with Windows", "Programs that launch by themselves when you sign in.")
    flagged = 0
    for it in startup:
        if not it["enabled"]:
            continue
        level, reasons = judge(it["program"], it["command"])
        if level == "ok":
            continue
        flagged += 1
        acts = [{"kind": "startup_off", "id": it["id"], "label": "Turn off at startup"}] if not it["needs_admin"] else []
        if os.path.isfile(it["program"]):
            threat_paths.add(it["program"])
            acts += file_acts(it["program"])
        add(s, level, it["name"], f"{'; '.join(reasons).capitalize()}. Command: {it['command']}", acts)
    if not flagged:
        add(s, "ok", f"{sum(1 for i in startup if i['enabled'])} startup programs checked", "Nothing unusual.")

    # 3. Scheduled tasks
    s = section("Scheduled tasks", "Jobs Windows runs in the background on a timer (Microsoft's own are skipped).")
    flagged = 0
    for t in tasks:
        level, reasons = judge(t["program"], f"{t['exe']} {t['args']}")
        if level in ("ok", "info"):
            continue
        flagged += 1
        acts = [{"kind": "open", "what": "tasks", "label": "Open Task Scheduler"}]
        if os.path.isfile(t["program"]):
            threat_paths.add(t["program"])
            acts[0:0] = file_acts(t["program"], show=False)
        add(s, level, t["name"], f"{'; '.join(reasons).capitalize()}. Runs: {t['exe']} {t['args']}".strip(), acts)
    if not flagged:
        add(s, "ok", f"{len(tasks)} tasks checked", "Nothing unusual.")

    # 4. Running now
    s = section("Running right now", "Programs running from places you can write to without admin rights.")
    flagged = 0
    for p in procs:
        st, risk = signed(p)[0], location_risk(p)
        if st == "Valid":
            continue
        level = "bad" if st == "HashMismatch" or risk == "temp" else "info"
        flagged += level == "bad"
        threat_paths.add(p)
        add(s, level, os.path.basename(p), f"{trust(p).capitalize()}, running from {os.path.dirname(p)}", file_acts(p))
    if not s["items"]:
        add(s, "ok", f"{len(procs)} programs checked", "All are signed by their makers.")

    # 5. Downloads and Desktop
    s = section("Downloads and Desktop", "Files that pretend to be something else, and programs with no known maker.")
    for f in loose:
        if is_eicar(f):
            threat_paths.add(f)
            add(s, "bad", os.path.basename(f), "Antivirus TEST file (EICAR). It is harmless, and finding it shows this check works. "
                f"You can delete it. In {os.path.dirname(f)}", file_acts(f))
        if DISGUISED.search(os.path.basename(f)):
            threat_paths.add(f)
            add(s, "bad", os.path.basename(f), f"Disguised file: it looks like a document or picture but is really a program. Do not open it. In {os.path.dirname(f)}",
                file_acts(f))
    broken = [f for f in programs[:60] if signed(f)[0] == "HashMismatch"]
    for f in broken:
        threat_paths.add(f)
        add(s, "bad", os.path.basename(f), f"Its signature is broken: the file was changed after its maker signed it. In {os.path.dirname(f)}",
            file_acts(f))
    unsigned = [f for f in programs[:60] if signed(f)[0] not in ("Valid", "HashMismatch")]
    if unsigned:
        for f in unsigned[:8]:
            threat_paths.add(f)
        add(s, "info", f"{len(unsigned)} program file(s) with no signature",
            "Not signed by a known maker. That is common for small tools, but only run ones you downloaded yourself from a site you trust: "
            + ", ".join(os.path.basename(f) for f in unsigned[:8]) + (" …" if len(unsigned) > 8 else ""),
            [{"kind": "scan", "path": str(DOWNLOADS), "label": "Scan Downloads with Defender"}])
        threat_paths.add(str(DOWNLOADS))
    odd = [f for f in loose if Path(f).suffix.lower() in {".scr", ".pif", ".hta", ".vbe", ".jse", ".wsf"}]
    if odd:
        add(s, "warn", f"{len(odd)} unusual script or screensaver file(s)",
            "These file types are rarely needed and often used by malware: " + ", ".join(os.path.basename(f) for f in odd[:8]))
    if not s["items"]:
        add(s, "ok", f"{len(loose):,} files checked", "No disguised or unsigned program files.")

    # 6. Hosts file
    say("Checking the hosts file…")
    s = section("Hosts file", "A Windows file that can redirect websites. Malware edits it to send you to fake sites or block antivirus updates.")
    entries = []
    try:
        for line in (WINDIR / "System32" / "drivers" / "etc" / "hosts").read_text(encoding="utf-8", errors="ignore").splitlines():
            parts = line.split("#")[0].split()
            if len(parts) >= 2 and not all(h in ("localhost", "ip6-localhost", "ip6-loopback") for h in parts[1:]):
                entries += [(parts[0], h) for h in parts[1:]]
    except OSError:
        pass
    redirects = [(ip, h) for ip, h in entries if ip not in ("0.0.0.0", "127.0.0.1", "::1", "::")
                 and any(w in h.lower() for w in HOSTS_WATCH)]
    blocked = [(ip, h) for ip, h in entries if ip in ("0.0.0.0", "127.0.0.1", "::1", "::")
               and any(w in h.lower() for w in ("windowsupdate", "kaspersky", "avast", "avg", "norton", "mcafee", "malwarebytes", "bitdefender", "defender"))]
    hosts_act = [{"kind": "open", "what": "hosts", "label": "Show hosts file"}]
    if redirects:
        add(s, "bad", f"{len(redirects)} well-known site(s) are redirected",
            "; ".join(f"{h} goes to {ip}" for ip, h in redirects[:6]), hosts_act)
    if blocked:
        add(s, "bad", f"{len(blocked)} security site(s) are blocked",
            "Blocking update or antivirus sites is a classic malware trick: " + ", ".join(h for _, h in blocked[:6]), hosts_act)
    rest = len(entries) - len(redirects) - len(blocked)
    if rest > 0 and not (redirects or blocked):
        add(s, "info", f"{rest} custom entr{'y' if rest == 1 else 'ies'}",
            "Often added by ad blockers, work tools or developers. Check them if you did not add them: "
            + ", ".join(h for _, h in entries[:5]) + (" …" if len(entries) > 5 else ""), hosts_act)
    if not entries:
        add(s, "ok", "Hosts file is clean", "No websites are redirected.")

    items = [i for sec in res["sections"] for i in sec["items"]]
    res["counts"] = {lv: sum(1 for i in items if i["level"] == lv) for lv in ("bad", "warn", "info", "ok")}
    res["at"] = datetime.now().strftime("%d %b %Y %H:%M")
    write_log("threat", {"action": f"Threat Check: {res['counts']['bad']} serious, {res['counts']['warn']} to review",
                         "ok": not res["counts"]["bad"]})
    return res


def start_threat_check():
    if tjob["running"]:
        return {"ok": True}
    tjob.update(running=True, progress="Starting…", result=None, error="")
    threat_paths.clear()

    def work():
        try:
            tjob["result"] = run_threat_check()
        except Exception as ex:
            tjob["error"] = str(ex)
        tjob["running"] = False

    threading.Thread(target=work, daemon=True).start()
    return {"ok": True}


def threat_action(kind, value):
    """The safe actions offered next to a finding."""
    if kind == "show" and value in threat_paths and os.path.exists(value):
        subprocess.Popen(["explorer", "/select,", value])
        return {"ok": True}
    if kind == "scan" and value in threat_paths:
        return defender_run("folder", value)
    if kind == "startup_off":
        return set_startup(value, False)
    if kind == "open":
        target = {"security": "windowsdefender:", "proxy": "ms-settings:network-proxy",
                  "rdp": "ms-settings:remotedesktop", "tasks": "taskschd.msc",
                  "uac": "UserAccountControlSettings.exe", "firewall": "firewall.cpl"}.get(value)
        if value == "hosts":
            subprocess.Popen(["explorer", "/select,", str(WINDIR / "System32" / "drivers" / "etc" / "hosts")])
        elif target:
            os.startfile(target)
        return {"ok": bool(target) or value == "hosts"}
    return {"ok": False, "message": "That action is not available."}


# ---------- VirusTotal check ----------
# A second opinion from VirusTotal.com, which scans a file with about 70
# antivirus engines at once (Defender, Avast, Kaspersky, Bitdefender...).
# Go Flow has no scanning engine of its own; this calls VirusTotal's own
# service, using an API key YOU create and paste into Settings. Only a
# SHA-256 "fingerprint" of the file is sent for the normal check; the file
# itself is only sent if you explicitly choose "Upload for scanning" for a
# file VirusTotal has never seen before.

VT_API = "https://www.virustotal.com/api/v3"
vtjob = {"running": False, "stage": "", "result": None, "error": ""}


def vt_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1048576):
            h.update(chunk)
    return h.hexdigest()


def vt_request(method, path, key, data=None, headers=None):
    req = urllib.request.Request(f"{VT_API}{path}", data=data, method=method,
                                 headers={"x-apikey": key, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except ValueError:
            return e.code, {}
    except urllib.error.URLError as e:
        return 0, {"error": {"message": str(e.reason)}}


def vt_verdict(attrs):
    stats = attrs.get("last_analysis_stats") or {}
    bad = stats.get("malicious", 0) + stats.get("suspicious", 0)
    total = sum(stats.values()) or 1
    return {"malicious": stats.get("malicious", 0), "suspicious": stats.get("suspicious", 0),
            "harmless": stats.get("harmless", 0), "undetected": stats.get("undetected", 0),
            "total": total, "bad": bad,
            "names": (attrs.get("meaningful_name") or attrs.get("names") or [""])[0] if isinstance(attrs.get("names"), list) else attrs.get("meaningful_name", ""),
            "reputation": attrs.get("reputation", 0), "date": attrs.get("last_analysis_date")}


def run_vt_check(path):
    key = SETTINGS["virustotal_api_key"]
    if not key:
        return {"ok": False, "need_key": True, "message": "Add a free VirusTotal API key in Settings first."}
    if not os.path.isfile(path):
        return {"ok": False, "message": "That file was not found."}
    vtjob["stage"] = "Working out the file's fingerprint…"
    sha = vt_sha256(path)
    vtjob["stage"] = "Asking VirusTotal…"
    status, body = vt_request("GET", f"/files/{sha}", key)
    if status == 200:
        v = vt_verdict(body["data"]["attributes"])
        v.update(ok=True, known=True, sha256=sha, name=os.path.basename(path), path=path,
                 permalink=f"https://www.virustotal.com/gui/file/{sha}")
        return v
    if status == 404:
        return {"ok": True, "known": False, "sha256": sha, "name": os.path.basename(path), "path": path,
                "message": "VirusTotal has not seen this exact file before, so there is no report yet."}
    if status == 401:
        return {"ok": False, "need_key": True, "message": "That VirusTotal API key was not accepted. Check it in Settings."}
    if status == 429:
        return {"ok": False, "message": "VirusTotal's free plan allows a few checks a minute. Wait a moment and try again."}
    return {"ok": False, "message": (body.get("error") or {}).get("message") or f"VirusTotal could not be reached (status {status})."}


def run_vt_upload(path):
    key = SETTINGS["virustotal_api_key"]
    if not key:
        return {"ok": False, "need_key": True, "message": "Add a free VirusTotal API key in Settings first."}
    if not os.path.isfile(path):
        return {"ok": False, "message": "That file was not found."}
    size = os.path.getsize(path)
    if size > 32 * 1024 * 1024:
        return {"ok": False, "message": "VirusTotal's free plan only accepts files up to 32 MB."}
    vtjob["stage"] = "Uploading the file to VirusTotal…"
    with open(path, "rb") as f:
        content = f.read()
    boundary = secrets.token_hex(16)
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{os.path.basename(path)}\"\r\n"
           "Content-Type: application/octet-stream\r\n\r\n").encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode("utf-8")
    status, resp = vt_request("POST", "/files", key, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    if status not in (200, 201):
        if status == 401:
            return {"ok": False, "need_key": True, "message": "That VirusTotal API key was not accepted. Check it in Settings."}
        return {"ok": False, "message": (resp.get("error") or {}).get("message") or f"Upload failed (status {status})."}
    analysis_id = resp["data"]["id"]
    vtjob["stage"] = "VirusTotal is scanning it (usually under a minute)…"
    for _ in range(40):
        time.sleep(3)
        s2, a = vt_request("GET", f"/analyses/{analysis_id}", key)
        if s2 == 200 and a["data"]["attributes"]["status"] == "completed":
            sha = a["meta"]["file_info"]["sha256"]
            v = vt_verdict({"last_analysis_stats": a["data"]["attributes"]["stats"]})
            v.update(ok=True, known=True, sha256=sha, name=os.path.basename(path), path=path,
                     permalink=f"https://www.virustotal.com/gui/file/{sha}")
            return v
    return {"ok": False, "message": "VirusTotal is still scanning the file. Check again on its website in a minute."}


def start_vt(path, upload=False):
    if vtjob["running"]:
        return {"ok": False, "message": "Already checking a file."}
    vtjob.update(running=True, stage="Starting…", result=None, error="")

    def work():
        try:
            vtjob["result"] = run_vt_upload(path) if upload else run_vt_check(path)
        except Exception as ex:
            vtjob["error"] = str(ex)
        vtjob["running"] = False

    threading.Thread(target=work, daemon=True).start()
    return {"ok": True}


# ---------- Storage breakdown (the donut chart on Home) ----------
# Sizes of your main folders, measured in the background and remembered for an
# hour. "Windows and programs" is whatever else is used on the drive.

STORAGE_CACHE = LOG_DIR / "storage_cache.json"
STORAGE_FOLDERS = [("Downloads", "Downloads"), ("Documents", "Documents"), ("Pictures", "Pictures"),
                   ("Videos", "Videos"), ("Music", "Music"), ("Desktop", "Desktop"),
                   ("OneDrive", "OneDrive"), ("App data", "AppData")]
storage = {"running": False, "data": None}


def folder_size(root):
    total, stack = 0, [str(root)]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    try:
                        if e.is_symlink() or (hasattr(e, "is_junction") and e.is_junction()):
                            continue
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        else:
                            st = e.stat(follow_symlinks=False)
                            if not st.st_file_attributes & CLOUD_ONLY:
                                total += st.st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return total


def measure_storage():
    try:
        items = []
        for name, sub in STORAGE_FOLDERS:
            p = HOME / sub
            if p.is_dir():
                items.append({"name": name, "path": str(p), "size": folder_size(p)})
        du = shutil.disk_usage(os.environ.get("SystemDrive", "C:") + "\\")
        rest = max(0, du.total - du.free - sum(i["size"] for i in items))
        items.append({"name": "Windows and programs", "path": "", "size": rest})
        storage["data"] = {"total": du.total, "free": du.free, "at": time.time(),
                           "items": sorted(items, key=lambda i: -i["size"])}
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        STORAGE_CACHE.write_text(json.dumps(storage["data"]), encoding="utf-8")
    finally:
        storage["running"] = False


def storage_info(refresh=False):
    if storage["data"] is None and STORAGE_CACHE.exists():
        try:
            storage["data"] = json.loads(STORAGE_CACHE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    d = storage["data"]
    if not storage["running"] and (refresh or d is None or time.time() - d.get("at", 0) > 3600):
        storage["running"] = True
        threading.Thread(target=measure_storage, daemon=True).start()
    return {"running": storage["running"], "data": d}


# ---------- Smart Scan: everything in one go ----------
# Scan finds junk, old downloads, duplicates, memory use and security state.
# "Fix" cleans only the safe parts you tick (junk, old setup files / unzipped
# zips, memory, virus definitions); duplicates always go to their own screen
# for review, because copies can be parts of programs or in OneDrive.

sjob = {"running": False, "phase": "", "steps": [], "result": None, "fixed": None, "error": ""}
smart_state = {"junk_ids": [], "dl_paths": []}
SMART_STEPS = [("junk", "Junk files"), ("downloads", "Old downloads"), ("dupes", "Duplicate files"),
               ("memory", "Memory"), ("security", "Security")]


def human(b):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024 or unit == "TB":
            return f"{b:.0f} {unit}" if unit in ("B", "KB") else f"{b:.1f} {unit}"
        b /= 1024


def set_step(sid, state, note=""):
    for s in sjob["steps"]:
        if s["id"] == sid:
            s.update(state=state, note=note)


def smart_scan():
    global dupe_groups
    if sjob["running"] or fjob["running"] or rjob["running"]:
        return {"ok": False, "message": "Another scan is still running. Try again in a moment."}
    parts = SETTINGS["smart"]
    sjob.update(running=True, phase="scan", result=None, fixed=None, error="",
                steps=[{"id": i, "label": l, "state": "wait" if parts.get(i, True) else "skip", "note": ""}
                       for i, l in SMART_STEPS])

    def work():
        global dupe_groups
        res, paths = {}, []
        try:
            if parts.get("junk", True):
                set_step("junk", "run", "Looking in temp folders and caches…")
                pick = [c for c in scan() if c["count"] and not c["needs_admin"] and c["id"] != "recycle"]
                smart_state["junk_ids"] = [c["id"] for c in pick]
                res["junk"] = {"size": sum(c["size"] for c in pick), "count": sum(c["count"] for c in pick),
                               "names": [c["name"] for c in pick]}
                set_step("junk", "done", f"{human(res['junk']['size'])} found")
            if parts.get("downloads", True):
                set_step("downloads", "run", "Checking setup files and zips…")
                r = find_old_downloads(DOWNLOADS, SETTINGS["downloads_days"], {"files": 0, "progress": ""})
                sugg = [f for f in r["files"] if f["suggest"]]
                smart_state["dl_paths"] = [f["path"] for f in sugg]
                paths += smart_state["dl_paths"]
                res["downloads"] = {"size": sum(f["size"] for f in sugg), "count": len(sugg),
                                    "old": len(r["files"]), "days": r["days"]}
                set_step("downloads", "done", f"{len(sugg)} safe to remove")
            if parts.get("dupes", True):
                st = {"files": 0, "progress": ""}
                set_step("dupes", "run", "Comparing files…")
                stop = threading.Event()

                def mirror():  # show the finder's own progress on this step
                    while not stop.wait(0.5):
                        set_step("dupes", "run", st["progress"] or "Comparing files…")
                threading.Thread(target=mirror, daemon=True).start()
                d = find_duplicates(HOME, 1, st)
                stop.set()
                dupe_groups = [[f["path"] for f in g["files"]] for g in d["groups"]]
                paths += [p for g in dupe_groups for p in g]
                fjob.update(kind="dupes", result=d, error="", progress="")  # ready on the Duplicates screen
                res["dupes"] = {"wasted": sum(g["wasted"] for g in d["groups"]), "sets": len(d["groups"])}
                set_step("dupes", "done", f"{human(res['dupes']['wasted'])} in extra copies")
            if parts.get("memory", True):
                set_step("memory", "run")
                res["memory"] = memory()
                set_step("memory", "done", f"{res['memory']['load']}% used")
            if parts.get("security", True):
                set_step("security", "run", "Asking Windows Security…")
                r = powershell(AV_PS + "$s=Get-MpComputerStatus;[pscustomobject]@{rt=[bool]$s.RealTimeProtectionEnabled;"
                               "sig=[int]$s.AntivirusSignatureAge;av=@($av)}|ConvertTo-Json", timeout=60)
                try:
                    s = json.loads(r["out"])
                except ValueError:
                    s = {}
                others = [n for n in s.get("av") or [] if n and "defender" not in n.lower()]
                res["security"] = {"protected": bool(s.get("rt")) or bool(others),
                                   "antivirus": "Microsoft Defender" if s.get("rt") else (others[0] if others else ""),
                                   "defender_active": bool(s.get("rt")), "sig_age": s.get("sig", 0),
                                   "reboot": reboot_pending()}
                set_step("security", "done", "Protected" if res["security"]["protected"] else "Not protected")
            allowed_files.clear()
            allowed_files.update(paths)
            sjob["result"] = res
        except Exception as ex:
            sjob["error"] = str(ex)
        sjob["running"] = False

    threading.Thread(target=work, daemon=True).start()
    return {"ok": True}


def smart_fix(choices):
    res = sjob.get("result") or {}
    if sjob["running"] or not res:
        return {"ok": False, "message": "Run Smart Scan first."}
    plan = [(k, l) for k, l in [("junk", "Cleaning junk files"), ("downloads", "Removing old setup files"),
                                ("memory", "Boosting memory"), ("defs", "Updating virus definitions")]
            if choices.get(k)]
    if not plan:
        return {"ok": False, "message": "Tick at least one thing to fix."}
    sjob.update(running=True, phase="fix", fixed=None, error="",
                steps=[{"id": k, "label": l, "state": "wait", "note": ""} for k, l in plan])

    def work():
        out = {"freed": 0, "memory": 0, "lines": []}
        try:
            for k, _ in plan:
                set_step(k, "run")
                if k == "junk":
                    rep = clean(set(smart_state["junk_ids"]))
                    f = sum(r["freed"] for r in rep)
                    out["freed"] += f
                    set_step(k, "done", f"{human(f)} freed")
                elif k == "downloads":
                    paths = [p for p in smart_state["dl_paths"] if p in allowed_files]
                    r = remove_files_now(paths, False,
                                         lambda n, t=len(paths): set_step("downloads", "run", f"{n} of {t} files"))
                    out["freed"] += r["freed"]
                    set_step(k, "done", f"{len(r['done'])} files to the Recycle Bin")
                elif k == "memory":
                    t = trim_memory()
                    out["memory"] = t["freed"]
                    set_step(k, "done", f"{human(t['freed'])} of memory freed")
                elif k == "defs":
                    r = powershell("Update-MpSignature", timeout=600)
                    set_step(k, "done" if r["ok"] else "fail", "Up to date" if r["ok"] else "Could not update")
            write_log("smart", {"action": f"Smart Scan fix: freed {human(out['freed'])} disk, "
                                          f"{human(out['memory'])} memory", "ok": True})
            sjob["fixed"] = out
        except Exception as ex:
            sjob["error"] = str(ex)
        sjob["running"] = False

    threading.Thread(target=work, daemon=True).start()
    return {"ok": True}


# ---------- App window ----------
# Go Flow opens in its own Edge "app" window (no tabs or address bar). The page
# pings the server; when the window is closed the server stops by itself.

app_window = {"mode": "browser", "last_ping": 0.0, "bye_at": 0.0}
WINDOW_PROFILE = LOCAL / "GoFlow" / "window"


def find_edge():
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), str(LOCAL)):
        if base:
            p = Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            if p.exists():
                return p
    return None


def open_window(url):
    edge = find_edge()
    if edge:
        WINDOW_PROFILE.mkdir(parents=True, exist_ok=True)
        subprocess.Popen([str(edge), f"--app={url}", f"--user-data-dir={WINDOW_PROFILE}",
                          "--window-size=1280,860", "--no-first-run", "--no-default-browser-check"])
        app_window["mode"] = "window"
    else:
        webbrowser.open(url)


def busy():
    return any(j["running"] for j in (job, fjob, rjob, sjob, ujob, tjob, vtjob))


def watch_window(server):
    """Stop the server once the window has been closed (no ping), unless a job is still running."""
    while True:
        time.sleep(2)
        now, last, bye = time.time(), app_window["last_ping"], app_window["bye_at"]
        if not last or busy():
            continue
        closed = bye and bye > last and now - bye > 6     # page said goodbye and did not come back
        silent = now - last > 180                          # minimized windows ping slowly; be patient
        if closed or silent:
            server.shutdown()
            return


# ---------- Logs and system info ----------

def write_log(kind, data):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    entry = {"time": datetime.now().isoformat(timespec="seconds"), "kind": kind, **data}
    with open(LOG_DIR / f"{datetime.now():%Y-%m}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def history(limit=30):
    rows = []
    for f in sorted(LOG_DIR.glob("*.jsonl"), reverse=True):
        for line in reversed(f.read_text(encoding="utf-8").splitlines()):
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
            if len(rows) >= limit:
                return rows
    return rows


def info():
    du = shutil.disk_usage(os.environ.get("SystemDrive", "C:") + "\\")
    return {"admin": is_admin(), "protection": protection_on(), "disk_total": du.total, "disk_free": du.free,
            "computer": os.environ.get("COMPUTERNAME", ""), "home": str(HOME),
            "drives": os.listdrives()}


# ---------- Local web server ----------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def allowed(self):
        # Block other websites from talking to this server.
        host_ok = self.headers.get("Host", "") in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")
        return host_ok and self.headers.get("X-Token") == TOKEN

    def do_GET(self):
        if self.path.split("?")[0] in ("/", "/index.html"):
            html = (RES / "cleaner_ui.html").read_text(encoding="utf-8").replace("__TOKEN__", TOKEN)
            body = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/icon.png":
            body = (RES / "assets" / "icon.png").read_bytes() if (RES / "assets" / "icon.png").exists() else b""
            self.send_response(200 if body else 404)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(body)
            return
        if not self.allowed():
            return self.send_json({"error": "forbidden"}, 403)
        routes = {"/api/info": info, "/api/scan": scan, "/api/defender/status": defender_status,
                  "/api/defender/threats": defender_threats, "/api/job": lambda: job,
                  "/api/history": history, "/api/files/job": lambda: fjob,
                  "/api/startup": list_startup, "/api/health": health,
                  "/api/programs": installed_programs, "/api/schedule": schedule_status,
                  "/api/updates": update_status, "/api/updates/search": lambda: ujob,
                  "/api/restore": restore_status, "/api/alerts": alerts_status,
                  "/api/speed": heavy_apps, "/api/files/recycle": lambda: rjob,
                  "/api/overview": overview, "/api/settings": lambda: SETTINGS, "/api/about": about,
                  "/api/smart": lambda: sjob, "/api/ping": self.ping,
                  "/api/mem": memory, "/api/storage": storage_info, "/api/threat": lambda: tjob,
                  "/api/vt": lambda: vtjob,
                  "/api/history/all": lambda: history(400)}
        fn = routes.get(self.path)
        if fn:
            self.send_json(fn())
        else:
            self.send_json({"error": "not found"}, 404)

    def ping(self):
        app_window["last_ping"] = time.time()
        return {"ok": True}

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) or b"{}"
        if self.path == "/api/bye":
            # Sent as the window closes; a beacon cannot set headers, so the token is in the body.
            host_ok = self.headers.get("Host", "") in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")
            if host_ok and raw.decode("utf-8", "ignore") == TOKEN:
                app_window["bye_at"] = time.time()
            return self.send_json({"ok": True})
        if not self.allowed():
            return self.send_json({"error": "forbidden"}, 403)
        data = json.loads(raw)
        if self.path == "/api/settings":
            self.send_json(save_settings(data))
        elif self.path == "/api/folder":
            self.send_json(open_folder(data.get("which")))
        elif self.path == "/api/threat/scan":
            self.send_json(start_threat_check())
        elif self.path == "/api/threat/action":
            self.send_json(threat_action(data.get("kind"), data.get("value")))
        elif self.path == "/api/vt/check":
            self.send_json(start_vt(data.get("path"), upload=False))
        elif self.path == "/api/vt/upload":
            self.send_json(start_vt(data.get("path"), upload=True))
        elif self.path == "/api/storage/refresh":
            self.send_json(storage_info(refresh=True))
        elif self.path == "/api/smart/scan":
            self.send_json(smart_scan())
        elif self.path == "/api/smart/fix":
            self.send_json(smart_fix(data))
        elif self.path == "/api/clean":
            rp = create_restore_point("Before Go Flow clean") if data.get("restore") else None
            self.send_json({"items": clean(set(data.get("ids", []))), "restore": rp})
        elif self.path == "/api/restore/create":
            self.send_json(create_restore_point(data.get("name") or "Go Flow (manual)"))
        elif self.path == "/api/restore/enable":
            self.send_json(enable_protection())
        elif self.path == "/api/restore/settings":
            self.send_json(open_protection_settings())
        elif self.path == "/api/alerts":
            self.send_json(alerts_set(bool(data.get("on")), data.get("percent")))
        elif self.path == "/api/alerts/test":
            self.send_json(alerts_test())
        elif self.path == "/api/speed/close":
            self.send_json(close_apps(data.get("names", []), bool(data.get("force"))))
        elif self.path == "/api/speed/trim":
            self.send_json(trim_memory())
        elif self.path == "/api/speed/power":
            self.send_json(open_power_settings())
        elif self.path == "/api/defender/run":
            self.send_json(defender_run(data.get("action"), data.get("path")))
        elif self.path == "/api/files/scan":
            self.send_json(start_file_scan(data.get("kind"), data.get("root"), data.get("min_mb")))
        elif self.path == "/api/files/recycle":
            self.send_json(recycle_files(data.get("paths", []), bool(data.get("permanent"))))
        elif self.path == "/api/files/open":
            self.send_json(open_in_explorer(data.get("path")))
        elif self.path == "/api/startup/set":
            self.send_json(set_startup(data.get("id"), bool(data.get("enable"))))
        elif self.path == "/api/programs/uninstall":
            self.send_json(uninstall_program(data.get("id")))
        elif self.path == "/api/programs/settings":
            self.send_json(open_apps_settings())
        elif self.path == "/api/schedule":
            self.send_json(schedule_set(bool(data.get("on")), data.get("day"), data.get("time")))
        elif self.path == "/api/schedule/run":
            self.send_json(auto_run_now())
        elif self.path == "/api/updates/search":
            self.send_json(start_update_search())
        elif self.path == "/api/updates/open":
            self.send_json(open_windows_update(bool(data.get("check"))))
        else:
            self.send_json({"error": "not found"}, 404)


class Server(ThreadingHTTPServer):
    # On Windows, address reuse lets a second copy silently share the port.
    allow_reuse_address = False


def main():
    global PORT
    if "--auto" in sys.argv:
        auto_run()
        return
    if "--check-alerts" in sys.argv:
        i = sys.argv.index("--check-alerts")
        check_alerts(int(sys.argv[i + 1]) if len(sys.argv) > i + 1 else 10)
        return
    if "--admin" in sys.argv and not is_admin():
        # Start again with administrator rights (Windows asks for permission), then leave.
        exe, args = self_command(" ".join(a for a in sys.argv[1:] if a != "--admin"))
        ctypes.windll.shell32.ShellExecuteW(None, "runas", str(exe), args, str(HERE), 1)
        return
    if "--port" in sys.argv:
        PORT = int(sys.argv[sys.argv.index("--port") + 1])
    url = f"http://127.0.0.1:{PORT}/"
    try:
        server = Server(("127.0.0.1", PORT), Handler)
    except OSError:
        print("Go Flow is already open. Bringing up its window.")
        open_window(url)
        return
    print(f"Go Flow {VERSION} running at {url}  (admin: {is_admin()})")
    if "--browser" in sys.argv:        # old way: a tab in your normal browser, runs until this is closed
        webbrowser.open(url)
    elif "--no-browser" not in sys.argv:
        open_window(url)
        if app_window["mode"] == "window":
            threading.Thread(target=watch_window, args=(server,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
