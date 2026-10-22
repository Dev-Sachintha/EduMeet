# EduMeet - starter project

Online classes and meetings for teachers, lecturers, students, employees and administrators.

| Layer | Technology |
|---|---|
| Desktop UI | **PySide6** (native Qt window; the video room is an embedded Chromium view) |
| Backend API | **FastAPI** + SQLAlchemy 2 |
| Database | **PostgreSQL** |
| Video | **WebRTC**, full-mesh peer-to-peer, signaling over WebSocket |

> **Status:** a working *starter*. It is meant to be run, read and extended - not deployed to
> hundreds of users as-is. See "Features that need a media server" below before promising
> anything about class size, recording or reliability.

---

## Roles

| Role | Sign-up | Create meetings | Join | Share files | Moderate room* | Manage users |
|---|---|---|---|---|---|---|
| student | self-register | no | yes | yes | no | no |
| employee | self-register | yes | yes | yes | own meetings | no |
| teacher | created by admin | yes | yes | yes | own meetings | no |
| lecturer | created by admin | yes | yes | yes | own meetings | no |
| admin | seeded from `.env` | yes | yes | yes | **any** meeting | yes |

\*Moderate = mute all, remove participant, end meeting for everyone.

---

## Project layout

```
EduMeet/
|-- main.py              launcher:  python main.py server | desktop
|-- requirements.txt
|-- .env.example         copy to .env
|-- desktop/             PySide6 client
|   |-- app.py           bootstrap + login/logout flow
|   |-- api_client.py    REST client + background-thread helper
|   |-- login_window.py  sign in / register
|   |-- dashboard.py     meetings, files, admin users, video room window
|   `-- styles.py        Qt style sheet
|-- backend/             FastAPI server
|   |-- main.py          app, routers, startup (creates tables + first admin)
|   |-- config.py        settings from .env
|   |-- database.py      PostgreSQL engine / session
|   |-- models.py        users, meetings, participants, files
|   |-- schemas.py       request/response models
|   |-- security.py      bcrypt + JWT
|   |-- deps.py          auth + role dependencies
|   |-- auth.py          /api/auth/*  and admin /api/users/*
|   |-- meetings.py      /api/meetings/*
|   |-- signaling.py     WebSocket signaling + serves the room page
|   |-- files.py         meeting file upload/download
|   `-- static/room.html the WebRTC room UI (the one file added to your tree)
|-- tests/test_health.py
`-- uploads/             created automatically
```

---

## Setup on Windows 10/11

### 1. Install prerequisites
1. **Python 3.11 or 3.12 (64-bit)** from python.org. Tick **"Add python.exe to PATH"**.
2. **PostgreSQL 16** from the EDB installer (postgresql.org/download/windows). Remember the
   password you set for the `postgres` superuser. Keep the default port `5432`.

### 2. Create the database
**Using SQLite (the default)? Skip this step and step 1.2** - no database setup is needed; `edumeet.db` is created automatically. The PostgreSQL instructions below apply only if you set a `postgresql+psycopg2://` `DATABASE_URL`.

Open PowerShell and run `psql` (adjust the version folder if needed):

```powershell
& "C:\Program Files\PostgreSQL\16\bin\psql.exe" -U postgres
```

Then, inside `psql`:

```sql
CREATE USER edumeet WITH PASSWORD 'change_me';
CREATE DATABASE edumeet OWNER edumeet;
\q
```

### 3. Install the project
```powershell
cd C:\path\to\EduMeet
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
notepad .env
```
If activation is blocked: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, then retry.

In `.env`, at minimum change:
* `DATABASE_URL` - must match the user/password you created (special characters in the
  password must be URL-encoded, e.g. `@` -> `%40`)
* `SECRET_KEY` - generate with `python -c "import secrets; print(secrets.token_urlsafe(48))"`
* `FIRST_ADMIN_EMAIL` / `FIRST_ADMIN_PASSWORD`

### 4. Start the backend
```powershell
python main.py server
```
Tables and the first administrator are created automatically on startup.
Open http://127.0.0.1:8000/docs to see the API; `http://127.0.0.1:8000/api/health` should say `ok`.

### 5. Start the desktop app (second terminal)
```powershell
cd C:\path\to\EduMeet
.\.venv\Scripts\Activate.ps1
python main.py desktop
```
Sign in with the admin account from `.env`.

### 6. Try a video call
1. As admin: **Users (admin)** tab -> **Add user** -> create a teacher.
2. Sign in as the teacher (or just use admin) -> **New meeting**. A room window opens; allow the
   camera/microphone if Windows asks (Settings -> Privacy & security -> Camera / Microphone ->
   *Let desktop apps access*).
3. Start a second client (another terminal, or another PC) -> **Create account** (student) ->
   **Join by code**.

Two clients on the *same PC* usually cannot both use one webcam - the second one falls back to
audio-only/viewer. Test with two machines, or use a virtual camera such as OBS Virtual Camera.

### 7. Run the tests
```powershell
pytest
```
The smoke tests do not need PostgreSQL to be running.

### Letting other computers on your network connect
1. In `.env` on the server PC: `HOST=0.0.0.0`
2. Allow the port (PowerShell **as Administrator**):
   `New-NetFirewallRule -DisplayName "EduMeet" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow`
3. On each client PC set `EDUMEET_SERVER_URL=http://<server-LAN-IP>:8000` in `.env`
   (or type it in the "Server" box on the login screen).

Browsers/Chromium only allow camera access on `https://` or `localhost`. For plain-http LAN
testing, the desktop app automatically marks the server from `EDUMEET_SERVER_URL` as trusted.
That flag is read at startup, so set the variable **before** launching the app. **For real use,
put the server behind HTTPS** (Caddy or nginx with a certificate).

---

## Features that need a media server or extra infrastructure

What works out of the box is a **peer-to-peer mesh**: every participant sends their video
directly to every other participant. This is great for a tutorial or small meeting, and it needs
nothing but this server. Everything below needs more.

| Feature | Works in this starter? | What you need |
|---|---|---|
| 1-to-1 and small groups (about 2-6 people) | Yes | Nothing extra |
| Reliable calls over the internet / behind strict NATs, school or corporate firewalls | **Partly** - STUN only works for friendly networks | A **TURN server** (coturn). It does not run natively on Windows; use a small Linux VPS or Docker. Then fill in `TURN_URL`, `TURN_USERNAME`, `TURN_CREDENTIAL` in `.env` |
| Classes with dozens/hundreds of students, lecture or webinar mode | **No** - mesh upload cost grows with every attendee and collapses past ~6-8 | An **SFU media server**: LiveKit, mediasoup, Janus or Jitsi. The room page would be replaced by that server's client SDK; auth, meetings, roles and files stay as they are |
| Server-enforced mute / camera off / "students cannot unmute" | **No** - mute-all and remove are cooperative client-side controls in a mesh | An SFU that controls publishing permissions |
| Recording | **Basic, included** (host's app records, see below) | For reliable recording of large classes, or recording when the host's PC is weak/offline: SFU server-side recording/egress (e.g. LiveKit Egress) |
| Bandwidth adaptation (simulcast/SVC), many video tiles at good quality | **No** | SFU |
| Screen sharing | **Experimental** | Works in the Chromium engine; in the desktop app it needs **Qt/PySide6 6.7+**, and the screen picker currently selects the *primary screen* automatically. Verify on your Windows setup |
| HTTPS / WSS for non-localhost | Not included | Reverse proxy with a TLS certificate |
| More than one backend process/server | **No** - room state is in memory | Redis pub/sub (or similar) for signaling fan-out |
| Live captions, breakout rooms, whiteboard, polls | Not included | Future work |

---

## Sign-in security

* **Authenticator app (2FA):** open the **Security** tab -> *Turn on* -> scan the QR code with Google Authenticator
  (or Microsoft Authenticator / Authy) -> enter the 6-digit code. You also get 8 one-time **recovery codes**.
  From then on, sign-in asks for a code after the password. Codes can be used only once.
* **Forgot your password?** On the sign-in window use the **Forgot password** tab: email + authenticator (or recovery)
  code + new password. No email server is needed. If 2FA was never turned on, an administrator must set a new
  password (Users tab -> Edit). An admin can also tick *Reset two-step verification* for someone who lost their phone.
* **One sign-in per account:** while an account is signed in on one computer, signing in on another is refused. The app
  checks in every 30 s; after it is closed the account becomes free again within `SESSION_IDLE_SECONDS` (120 s), or
  immediately when you press *Sign out*. Signing in again on the *same* computer simply replaces the old session.
* **One account per computer:** a computer that already has an account signed in cannot sign in a second account.
  Each computer gets a random id stored in `%USERPROFILE%\.edumeet\device_id`. This is *best effort* - someone
  technical can fake the id; it stops ordinary double use, not a determined cheater.
* **Same meeting:** one account cannot be in the same meeting twice.
* **Brute force:** 5 wrong passwords/codes lock the account for 5 minutes.
* To test with two windows on **one** PC, set `ONE_ACCOUNT_PER_DEVICE=false` (and, for the same account twice,
  `SINGLE_SESSION_PER_ACCOUNT=false`) in `.env` and restart the server.

Notes: the authenticator secret is stored unencrypted in the database (encrypt it before production); sessions are
tracked in the `user_sessions` table; an older `edumeet.db` is upgraded automatically on startup, and everyone must
sign in once again after this update.

---

## AI features

Open the **AI** button in the room (everyone) and **Captions** (host/admin).

| Feature | Who | Engine |
|---|---|---|
| **Live captions + transcript** | host switches *Captions* on for everyone; each person's own microphone is transcribed | **Whisper on the server PC** - free, no key (`faster-whisper`) |
| **Summary + action items** | anyone who joined | Claude API |
| **Ask AI** (assistant that knows the transcript) | anyone who joined | Claude API |
| **Quiz and lesson notes** (save to Files) | host / admin | Claude API |

Setup:

1. `python -m pip install anthropic faster-whisper` (captions work without Claude; Claude features work without Whisper).
2. For Claude: create a key at console.anthropic.com and put it in `.env` as `ANTHROPIC_API_KEY=...` (needs API credit).
   `AI_MODEL` defaults to `claude-sonnet-5-5`.
3. `python main.py ai-check` tests both (the first run downloads the Whisper model: `tiny` ~75 MB, `base` ~145 MB).
4. Restart the server. In the room the host presses **Captions**, people talk, then use **AI -> Summary / Ask AI / Teacher tools**.

**Teacher tools - questions from your own material.** Host/admin: *AI -> Teacher tools*.

1. Pick the source: the **meeting transcript**, an **uploaded file**, or a **topic only**.
2. For a file, choose it and press **Upload and read**. Supported: **PDF**, **text** (.txt .md .csv), **audio** (.mp3 .wav .m4a ...)
   and **video** (.mp4 .mkv .mov .webm ...). PDFs and text are read at once; scanned PDFs are read by Claude;
   audio/video is transcribed with Whisper on the server PC (a progress % is shown - an hour of video can take many minutes
   on a CPU, and uploads are limited by `MAX_MATERIAL_MB`, default 300).
3. Set how many **MCQs** and how many **structured questions** (stem + parts (a)(b)(c) with marks, model answers and marking
   points), choose the difficulty and press **Make questions**. *Show answers* reveals the answer key. **Save to Files** or
   **Copy** the result. **Make lesson notes** uses the same source.

Uploaded files are kept only as extracted text (the original file is deleted). Their text is sent to Anthropic when you generate questions.

Good to know:

* **Privacy:** when the host switches captions on, *everyone is told* (blue "CC" badge + chat message) and each person
  can untick *Send my voice for captions* (AI -> Transcript). Muted microphones and silence are never sent. Transcripts are
  stored in the database and readable by people who joined that meeting (and admins). Summaries, questions, quizzes and
  notes send the transcript text to Anthropic - tell participants.
* **Captions run on the server's CPU**, a few seconds behind the speaker. `WHISPER_MODEL=tiny` is faster, `small` is more
  accurate. Set `WHISPER_LANGUAGE` (for example `en`, `si`, `ta`) when auto-detect guesses wrong. Accuracy for less common
  languages (such as Sinhala) is lower, and AI output can contain mistakes - check it before relying on it.
* Each person may make `AI_CALLS_PER_10_MIN` (20) Claude requests per 10 minutes, and an unchanged transcript reuses its summary.

---

## Make the 6-digit code arrive in the Gmail inbox

1. Make sure a file named exactly **`.env`** exists in the project folder (`copy .env.example .env`). The app does
   **not** read `.env.example`.
2. In Google: turn on 2-Step Verification, then create an **app password** at myaccount.google.com/apppasswords.
3. Fill these lines in `.env` (no quotes), using the Gmail address that will *send* the codes:

   ```
   SMTP_HOST=smtp.gmail.com
   SMTP_PORT=587
   SMTP_USER=yourname@gmail.com
   SMTP_PASSWORD=your16letterapppassword
   SMTP_FROM=yourname@gmail.com
   ```
4. Restart the server. Its window now shows `Email: SMTP smtp.gmail.com:587 as ...` (if it says *NOT configured*,
   `.env` was not found or the lines are empty).
5. Test: `python main.py test-mail --to someone@gmail.com`. Error hints explain what to fix.

---

## Fake, temporary and duplicate e-mail addresses

Rules applied when someone creates an account (admin-created accounts are trusted and skip the domain rules):

1. **Temporary / disposable addresses are refused** - a built-in list (`backend/data/disposable_domains.txt`, edit it
   freely), plus domains you add in **Monitor -> Blocked domains**, plus `BLOCKED_EMAIL_DOMAINS` in `.env`. Sub-domains
   are covered too. The list is not exhaustive (new services appear every day).
2. **Fake domains are refused** - the domain must be able to receive mail (DNS check, `CHECK_EMAIL_DNS`).
3. **Real inbox required** - a 6-digit code is e-mailed and must be typed in (`REQUIRE_EMAIL_VERIFICATION`). Without
   SMTP settings the code is printed in the **server window** (handy for testing). To send real mail set `SMTP_*`
   in `.env` (for Gmail use `smtp.gmail.com`, port 587 and an *app password*).
4. **School / company only (strongest):** `ALLOWED_EMAIL_DOMAINS=myschool.edu` lets only that domain register.
5. **One person = one address:** `name+tag@gmail.com` and dots in a Gmail address count as the *same* address.
6. **One person = one account (best effort):** at most `MAX_ACCOUNTS_PER_DEVICE` (default 1) accounts can be created
   from one computer, and `MAX_REGISTRATIONS_PER_IP_PER_DAY` (default 5) from one network address. Together with the
   one-sign-in-per-computer rule this stops ordinary double use. Nothing can prove two different e-mails belong to
   the same human - use the Monitor tab to review suspicious cases (shared school networks share an IP, so keep the
   IP limit generous there).

### Monitor tab (administrators)
**Overview** (users, online, failed sign-ins, blocked sign-ups, locked accounts) - **Online now** (with *Sign out
selected user*) - **Security log** (sign-ins, failures, lock-outs, blocked sign-ups with the reason, e-mail
confirmations, password resets, 2FA changes, duplicate meeting joins, admin actions; filter, search, **Export CSV**) -
**Suspicious** (several accounts on one computer / network) - **Blocked domains**. Refreshes every 15 seconds.

---

## Test microphone & camera

Use **Test mic & camera** (top right of the dashboard) or the same button in the *Join meeting* box before you
enter a room. The window lets you:

* pick your **camera**, **microphone** and **speaker** (output selection needs a recent Chromium);
* see a live camera picture - it warns when the picture is a flat colour (covered lens / wrong device);
* watch a **microphone level meter** and get hints when it is silent, too quiet or too loud;
* **record 4 seconds and play it back**, and **play a test sound** on the chosen speaker.

Your choices are remembered (browser storage of the app) and used when you join a meeting. If a saved device is
unplugged, EduMeet falls back to the default one.

---

## Whiteboard (with hand gestures)

Everyone has a **Whiteboard** button in the room. The board is shared live: pen, marker, eraser, 4 line widths,
8 colours, shapes (line, arrow, double arrow, rectangle, circle, diamond - outline / tinted / solid), undo, and
**Save image** (uploaded to the meeting's Files as a PNG). Late joiners receive the current board. The host can
**Clear all** and **Lock board** (students then view only). If a recording is running while the board is open, the
board is what gets recorded.

**Text box and Table:** in the toolbar under *Text and table*. Choose *Text box* or *Table* (for a table also set rows,
columns and *Header row*), drag a frame on the board and type. Click an existing text box / table with the same tool
to edit it (**Tab** = next table cell, **Enter** = next row, **Ctrl+Enter** finishes a text box, **Esc** cancels). The line-width
buttons set the text size. Only the person who made it, or the host, can edit it; **Undo** removes your last item.

**Hand gestures** (button *Hand gestures: OFF/ON*, uses MediaPipe Hand Landmarker in the room window):

| Gesture | Action |
|---|---|
| one finger up | draw (works with the selected tool and shapes) |
| two fingers | move the pointer; hold it over a button ~1 s to press it |
| four fingers (open hand) | erase |
| fist held ~1 s | undo your last item |

Notes: the first use downloads the hand-tracking library and a ~10 MB model from jsDelivr / Google storage, so it
needs internet once (to run offline, download those files into `backend/static/` and change `MP_BASE` / `MP_MODEL`
in `room.html`). It uses your meeting camera if it is on, otherwise it opens the camera just for tracking (nothing is
sent). Good light and a plain background help. Strokes appear for other people when you finish each stroke. The board
is kept in server memory only (lost when everyone leaves) - use **Save image**.

---

## Recording

The **host** (or an admin) sees a **Record** button in the room.

* Everyone in the room is told immediately: a red **REC** badge, a chat notice and a status message
  (late joiners see the badge too). Staying in the meeting is treated as notice - **you are responsible for
  complying with local consent/privacy rules** (for example, tell students in advance).
* The host's app records one video of all tiles (grid layout, screen shares shown in full) mixed with everyone's
  audio, and uploads it to the meeting's **Files** (limit `MAX_RECORDING_MB`, default 1024). Anyone who joined the
  meeting can download it from **Files...**.
* If the upload fails, the app offers to save the file on the host's computer instead.
* Leaving/closing while recording asks to stop and save first. If the host loses connection, the recording stops
  and whatever was captured is saved.

Limits: it records what the **host's computer** sees, so it needs that PC to stay on and the room window open
(Windows may slow a minimized window); one recorder at a time; the whole file is uploaded at the end (very long
meetings use host memory); files are `.webm` (play in VLC, Chrome or Edge; run `ffmpeg -i in.webm -c copy out.webm`
if a player cannot seek). For recording at class scale you need an SFU (see the table above).

---

## Remote control (consent-based)

A participant who is **sharing their screen** can let someone else use their mouse and keyboard.

1. The sharer clicks **Share screen**. Everyone sees a **Request control** button on that tile.
2. The sharer gets a native Windows prompt and must click **Allow** (one prompt per request).
3. While active, an orange **Stop control** bar stays on top of the sharer's screen. The sharer can also
   throw the mouse into any screen corner (pyautogui fail-safe). The controller has a **Stop control** button.
4. Control ends automatically when sharing stops, either person leaves, or the meeting ends.

Safeguards: the server only relays input after the *sharer* approved (a controller cannot approve itself);
the sharer's desktop app only accepts events from the approved person; it is deliberately **not** an
unattended-access tool.

Limits: the **sharer must use the desktop app** (a browser cannot inject OS input); the shared surface is
assumed to be the primary screen; Windows ignores injected input for elevated (administrator) windows and
the lock screen; input travels through the server WebSocket, so expect some latency; keys are mapped by physical
key position, so unusual keyboard layouts may type differently.

---

## API overview

| Method | Path | Notes |
|---|---|---|
| POST | `/api/auth/register` | student / employee only |
| POST | `/api/auth/login` | returns JWT + user |
| GET | `/api/auth/me` | |
| GET/POST/PATCH | `/api/users`, `/api/users/{id}` | admin only |
| GET/POST | `/api/meetings` | create: admin/teacher/lecturer/employee |
| GET | `/api/meetings/{code}` | |
| POST | `/api/meetings/{code}/end` | host or admin |
| GET | `/api/meetings/ice-config` | STUN/TURN list for WebRTC |
| GET/POST | `/api/meetings/{code}/files` | host/admin/anyone who has joined |
| GET | `/api/files/{id}/download` | |
| WS | `/ws/meetings/{code}?token=JWT` | signaling, chat, hand-raise, host controls |
| GET | `/room/{code}` | WebRTC room page (token in URL **fragment**) |

---

## Before production

* Replace `Base.metadata.create_all` with **Alembic** migrations.
* Serve over **HTTPS/WSS**; set a strong `SECRET_KEY`; restrict `CORS_ORIGINS`.
* Add login rate-limiting, password reset and email verification.
* Move uploads to object storage (S3/MinIO) and add virus scanning.
* Add a TURN server, and an SFU if classes are large.
* Sign the desktop app and package it (PyInstaller / Briefcase).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `password authentication failed for user "edumeet"` | `DATABASE_URL` does not match the user/password from step 2 |
| `connection refused` on 5432 | Start the *postgresql-x64-16* service (Services app) |
| Desktop says "Cannot reach the server" | Backend not running, wrong URL, or firewall |
| Room opens but no camera | Windows camera privacy setting, camera in use by another app, or non-HTTPS non-localhost server |
| Participants see each other's names but no video | Network blocks direct connections -> configure TURN |
| `ModuleNotFoundError: desktop` / `backend` | Run commands from the project root (`EduMeet/`) |
