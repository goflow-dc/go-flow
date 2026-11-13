# Go Live: put Go Flow online

A step-by-step checklist. You need a free GitHub account; everything else is free too.

When you are done you will have:

- **Website:** `https://goflow-dc.github.io/go-flow/`
- **Download link:** `https://github.com/goflow-dc/go-flow/releases/latest/download/GoFlow-Setup.exe`

Replace `goflow-dc` with your GitHub username everywhere below.

---

## Part A: one-time setup (about 20 minutes)

### 1. Fill in your details
Open `website\config.js` in Notepad and change:

| Line | Put |
|---|---|
| `owner` | Your name or company (shown in the website footer) |
| `email` | A contact email to show publicly. Use a separate address, not your personal one. `""` hides it |
| `githubUser` | Your GitHub username |
| `repo` | Leave as `go-flow` (or the repository name you choose in step 3) |

### 2. Get GitHub Desktop
1. Make an account at https://github.com (if you do not have one).
2. Install **GitHub Desktop** from https://desktop.github.com and sign in. It includes Git, so you do
   not need to install anything else.

### 3. Upload the project
1. GitHub Desktop: **File > Add local repository** > choose this folder
   (the `LaptopCleaner` folder on your Desktop).
2. It says "this directory does not appear to be a Git repository": click **create a repository**.
   Name: `go-flow`. Leave the rest. Click **Create repository**.
3. Click **Publish repository**. **Untick "Keep this code private"** (free GitHub Pages needs a
   public repository). Click **Publish**.

The `.gitignore` file already keeps your own data out of the upload: `logs\`, `settings.json`,
`versions\`, and the build folders. Before publishing, glance at the file list in GitHub Desktop and
check none of those appear.

### 4. Turn on the website
1. On github.com open your `go-flow` repository > **Settings** > **Pages**.
2. Under "Build and deployment", **Source: GitHub Actions**.
3. Open the **Actions** tab > **Publish website** > **Run workflow**. Wait for the green tick
   (about 1 minute).
4. Open `https://goflow-dc.github.io/go-flow/`. The site is live.

### 5. Make the first release (this builds the installer)
1. Repository page > **Releases** (right side) > **Draft a new release**.
2. **Choose a tag**: type `v1.0.0` > **Create new tag**. (It must match `version.txt`, with a `v`.)
3. Title: `Go Flow 1.0.0`. Describe what is new. Click **Publish release**.
4. **Actions** tab > **Build installer** runs for about 5 minutes. When it turns green, the Release
   has two new files: `GoFlow-Setup.exe` and `GoFlow-Setup.exe.sha256`.

### 6. Check the download works
1. Open the website and press **Free Download**. `GoFlow-Setup.exe` should download.
2. The line under the button should now read like `v1.0.0 Â· 9 MB Â· Windows 10 / 11`.
3. Install it on a PC and open Go Flow (see Part C about Windows warnings first).
4. Upload the installer to https://www.virustotal.com and check the result before telling anyone
   about the site. Do this for every release.

---

## Part B: releasing a new version

1. Change the number in `version.txt` (for example `1.0.1`).
2. Add a block for it at the top of `website\changelog.html` (copy the existing block).
3. GitHub Desktop: write a summary like "Version 1.0.1" > **Commit to main** > **Push origin**.
   The website updates by itself in about a minute.
4. On github.com: **Releases > Draft a new release**, tag `v1.0.1`, **Publish release**.
5. Wait for **Build installer** to go green. The website's Download button always points to the
   newest release, so there is nothing to edit.
6. Repeat the checks in step 6 above.

If the build fails with "Tag ... does not match version.txt", the tag and the file disagree: fix
`version.txt`, push, delete the release and tag, and release again.

---

## Part C: trust and Windows warnings

Go Flow's installer is **not code-signed** yet. That has three effects you should know before sharing it.

### 1. SmartScreen: "Windows protected your PC"
Most people see this the first time they run a new unsigned installer. They can click
**More info > Run anyway**. The website's FAQ and Support page already explain this and link to the
SHA-256 checksum so people can check their download is genuine.

### 2. Smart App Control: the app is blocked completely
Windows 11 PCs with **Smart App Control** turned on refuse to run unsigned apps: "An Application
Control policy has blocked this file", with no way to continue. **This PC has it on**, which is why
the built `GoFlow.exe` could not be test-run here. People with it on cannot use Go Flow until it is
signed. Do not tell users to turn it off, and do not turn it off here just to test: Windows cannot
turn it back on without a reset.

### 3. The proper fix: a code-signing certificate (paid)
Signing removes the Smart App Control block and, with time, the SmartScreen warning. Options:

| Option | Rough cost | Notes |
|---|---|---|
| Standard (OV) certificate from a certificate authority | about $200-400 a year | Needs identity checks; comes on a USB token or cloud key. SmartScreen trust builds up over time |
| EV certificate | about $300-600 a year | Stricter checks (usually a registered business) |
| Microsoft's cloud signing service (Azure "Trusted Signing" / "Artifact Signing") | about $10 a month | Cheapest, but only offered to organisations and individuals in some countries: check whether yours is eligible |
| SignPath Foundation | Free | Only for open-source projects that meet their rules |

Prices and eligibility change: check each provider's site before you buy. Once you have a
certificate, the signing step is added to `.github\workflows\release.yml` (sign `GoFlow.exe` before
the installer is built, then sign `GoFlow-Setup.exe`).

### 4. Antivirus false positives
Cleaner apps are sometimes flagged by mistake, because they delete files and change startup settings.

- Scan every release at https://www.virustotal.com before announcing it.
- If Microsoft Defender flags it, submit the file as a false positive at
  https://www.microsoft.com/wdsi/filesubmission (choose "Software developer").
- Other antivirus makers have similar forms; search "<name> false positive submit".

---

## Optional: your own domain
1. Buy a domain (for example `goflowapp.com`) from any registrar.
2. Repository > **Settings > Pages > Custom domain**: type the domain, **Save**.
3. At the registrar, add the DNS records GitHub shows on that page (four `A` records for the bare
   domain, or one `CNAME` to `goflow-dc.github.io` for `www`).
4. When the check passes, tick **Enforce HTTPS**.

---

## What only you can do
- Create the GitHub account, sign in, and publish the repository (Part A, steps 2-5).
- Choose the public contact email and name in `website\config.js`.
- Decide whether to buy a code-signing certificate, and which one.
- Buy a domain, if you want one.
- Decide whether the website screenshot (`website\img\app-screenshot.png`) is the one you want
  public: it is a real picture of this laptop's Go Flow (disk size, antivirus name).

## Files that make this work
| File | Purpose |
|---|---|
| `website\` | The site: `index.html`, `privacy.html`, `support.html`, `changelog.html`, `style.css`, `site.js`, `config.js`, `img\` |
| `.github\workflows\pages.yml` | Publishes `website\` to GitHub Pages when it changes |
| `.github\workflows\release.yml` | Builds `GoFlow.exe` and `GoFlow-Setup.exe` when you publish a Release, test-starts the program, and attaches the installer and its checksum |
| `installer\setup.iss` | The installer recipe (Inno Setup) |
| `build.bat`, `BUILD.md` | Building on your own PC |
| `version.txt` | The one place the version number lives |
| `.gitignore` | Keeps your logs, settings and build output out of the upload |
