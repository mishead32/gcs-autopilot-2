# Putting GCS Autopilot online — free

Eight parts, about forty minutes, nothing to pay.

There is an interactive version of this with tick-boxes that remember your place;
ask Claude for the "GCS Autopilot Go-Live" page. This file is the offline copy.

**What you need:** GitHub (you have it), and three free accounts you can all open
with GitHub — [Neon](https://neon.com) for the database,
[Render](https://render.com) to run the site, [cron-job.org](https://cron-job.org)
to wake it each morning.

**Keep Notepad open.** You will park several long values in it.

### Three things never to share

Your Neon connection string, your `MIDAP_SECRET` and your `CRON_SECRET`. The
first contains your database password; the second can forge a login. They belong
only in Notepad and in Render's settings screen — never in chat, email or
WhatsApp, including to Claude.

---

## Part 1 — Send the latest code to GitHub (2 min)

Everything is already committed on your PC and waiting. One command sends it up.

1. Press the **Windows** key, type `cmd`, press **Enter**. A black window opens.
2. Paste this and press Enter (**right-click** pastes in that window; Ctrl+V may
   not work):

   ```
   cd "C:\Users\Rajinder\Documents\GCS- Auto Pilot Dashboard\github-upload"
   ```

3. Then:

   ```
   git push
   ```

   If a browser opens asking you to sign in to GitHub, sign in and click
   **Authorize**. It only asks once.

   **Expect:** a few lines ending in `main -> main`. `Everything up-to-date` is
   fine too — it means it already went up.

4. Check it arrived: open
   <https://github.com/mishead32/gcs-autopilot-2>. You should see an `app` folder
   and the commit message "Reports menu, organised dashboard, report rights, and
   an IST clock".

---

## Part 2 — Create the database (5 min)

Neon's free plan does not expire. 0.5 GB.

1. [neon.com](https://neon.com) → **Sign up** → **Continue with GitHub**.
2. Create a project:
   - Project name: `gcs-autopilot`
   - Postgres version: whatever it offers
   - **Region: Singapore (`ap-southeast-1`)** — closest to India. Mumbai is not on
     the free plan.
3. Copy the connection string into Notepad. Neon shows it right after the project
   is created; if you lose it, **Dashboard → Connect**. It looks like:

   ```
   postgresql://neondb_owner:•••••@ep-cool-name-12345.ap-southeast-1.aws.neon.tech/neondb?sslmode=require
   ```

> **Copy the whole line**, right to the end including `?sslmode=require`. Cutting
> it short is the most common reason the site will not start. The `•••••` part is
> your database password.

---

## Part 3 — Make two secrets for the software (2 min)

These are not passwords you will ever type. One signs the login cookie, the other
protects the daily wake-up address. They just have to be long and random.

In Notepad:

```
MIDAP_SECRET = <mash the keyboard, about 40 characters>
CRON_SECRET  = <mash again, about 20, different from the first>
```

> The value must be **your random characters** — not the words "40 random
> characters".

---

## Part 4 — Put the website online (10 min)

1. [render.com](https://render.com) → **Get Started** → **GitHub**. When it asks
   which repositories it may see, choose **Only select repositories** and pick
   **gcs-autopilot-2**.
2. **New +** → **Web Service** → pick `gcs-autopilot-2` → **Connect**.
3. Fill the settings exactly:

   | Field | Value |
   |---|---|
   | Name | `gcs-autopilot` (this becomes your web address) |
   | Language | Python 3 |
   | Branch | `main` |
   | Region | Singapore |
   | Root Directory | **leave completely empty** |
   | Build Command | `pip install -r requirements.txt` |
   | Start Command | `uvicorn app.main:app --host 0.0.0.0 --port $PORT` |
   | Instance Type | **Free** |

4. Add five **Environment Variables**. The names must match exactly — capitals and
   underscores included:

   | Name | Value |
   |---|---|
   | `DATABASE_URL` | the whole Neon line from Notepad |
   | `MIDAP_SECRET` | your 40 random characters |
   | `CRON_SECRET` | your 20 random characters |
   | `STORAGE_BACKEND` | `db` — exactly these two letters |
   | `APP_NAME` | `GCS Autopilot` |

   > **Why `STORAGE_BACKEND=db`.** A free Render service has no permanent disk —
   > its files are wiped on every restart, several times a day. This keeps every
   > screenshot and photo inside the database, where it survives.

5. Under **Advanced**, set **Health Check Path** to `/healthz`.
6. **Create Web Service**, then wait 3–6 minutes. Watch for
   `==> Your service is live 🎉` and your address at the top, something like
   `https://gcs-autopilot.onrender.com`.
7. Write that address in Notepad. Everywhere below, **YOUR-ADDRESS** means this.
8. Open `https://YOUR-ADDRESS/healthz`. You should see:

   ```
   {"ok":true,"serverless":false,"database":"postgres","file_storage":"db"}
   ```

   The last two matter: `postgres` means it found Neon, `db` means attachments are
   safe.

---

## Part 5 — Create your admin login (2 min)

> **Do this immediately after Part 4.** The site has no users at all, so it offers
> a one-time setup page to whoever opens it first. The moment you create your
> account that page is sealed forever. Until then, anyone who guessed the address
> could take it.

1. Open `https://YOUR-ADDRESS/setup`. The first visit after a quiet spell takes
   about a minute to load — normal on the free plan, see Part 7.
2. Fill in your name, the email you want to sign in with, and a real password of
   at least 8 characters. This account can see and change everything.
3. Sign in on the page it sends you to.
4. Visit `https://YOUR-ADDRESS/setup` again. It must now say
   **"Page not found — Setup is already done"**. That is the lock working. If it
   still shows the form, something is wrong.

---

## Part 6 — Set up GCS inside it (15 min)

The database starts completely empty. Build it in this order — each step needs the
one before it.

1. **Setup → Branches** — add all five: Bodyzone Fitness & Spa, Spa Kora, BIPS
   School, GCS Jharkhand, GCS HO. Every task, person and score is filed under a
   branch, so these must exist before you add anybody.
2. **Setup → Holidays** — add the rest of this year. No checklist task is created
   on those days, and an FMS or delegation deadline landing on one moves to the
   next working day with a note saying why.
3. **Setup → Users** — for each person set role, branch, a temporary password and
   the 60/20/20 benchmark, then the rights:

   | Tick | For |
   |---|---|
   | Follow up Checklist & FMS (PC) | your PC |
   | Follow up Delegation (EA) | your EA |
   | See everyone's reports | only people who should see the whole company, including other people's EM scores. Without it, a doer's reports show their own work only. |
   | Audit submissions | whoever checks completed work |
   | Flag false marking (−10) | the same auditors |

   Tell each person to change their password once they are in.
4. **Operations → Checklist** — the repeating daily and weekly jobs. These start
   creating tasks once Part 7 is done.
5. **Operations → FMS** — your step-by-step processes.
6. **Walk the loop once yourself** before handing out the address: assign a task,
   open it as that person, paste a screenshot with Ctrl+V, submit, then audit it.

---

## Part 7 — Wake it each morning (5 min)

A free Render service sleeps after 15 minutes with nobody on it, and the next
visitor waits about a minute. Worse, the day's checklist tasks are created when
the software starts — so if nobody opens it, nothing is created. One free
scheduled ping solves both.

1. [cron-job.org](https://cron-job.org) → **Sign up**.
2. Create a cronjob:
   - Title: `GCS Autopilot — wake & daily checklist`
   - URL: `https://YOUR-ADDRESS/cron/spawn?key=YOUR_CRON_SECRET`
     (paste your `CRON_SECRET` after `key=`, no spaces anywhere)
3. Schedule:
   - Timezone: **Asia/Kolkata**
   - Custom: every **10 minutes**, hours **08 to 21**, every day

   > **Not around the clock.** Render gives 750 free hours a month; 24/7 is 744 —
   > no margin, and the site would suspend itself before month end. 8 am to 9 pm is
   > about 400 hours.
4. Save, then **Run now**. Expect `{"created":0}` or a small number — that is how
   many checklist tasks it just made. `401` means the key is wrong, `404` means the
   address is wrong.

---

## Part 8 — Living with it

**Getting new features.** When Claude makes a change, it is committed on your PC.
You run the same two commands as Part 1 — `cd` to the github-upload folder, then
`git push`. Render notices within seconds and rebuilds in about four minutes.
Nobody is logged out and no data is touched.

**Checking it is well.** `https://YOUR-ADDRESS/healthz` should always answer
`"ok":true`. Render's **Logs** tab shows what happened if it does not.

**Backups.** Neon's free plan keeps one day of history — enough for a deletion you
notice the same day, nothing more. Once there is real data in there, ask for a
proper export to be set up.

**What free actually costs you:**

| Limit | Day to day |
|---|---|
| Sleeps after 15 min | First visit of the morning takes ~1 min. Between 8 am and 9 pm the ping keeps it awake. |
| 750 hours/month | Enough for 8 am–9 pm daily. A second free service will break it. |
| No permanent disk | Already handled — attachments live in the database. |
| 0.5 GB database | The real ceiling. Text is tiny, photos are not — roughly 200–400 phone screenshots. When it fills, writes start failing. Say so when you pass half and attachments can move to free object storage. |
| 1 day of history | See Backups. |

**If you outgrow free.** Render Starter is US$7/month and never sleeps; Neon's
paid tier lifts the storage. Together roughly ₹1,200/month. Nothing gets rebuilt —
you change the plan and it keeps running.

---

## When something goes wrong

**Build fails: `ModuleNotFoundError: No module named 'app'`**
**Root Directory** in Render has something in it. It must be completely empty —
the `app` folder is at the top level of the repository and Render is looking one
level too deep.

**Build fails mentioning a Python version**
Render now defaults to Python 3.14, which this software has never been tested on.
`.python-version` in the repository pins it to 3.12. If the build ignores it, add
an environment variable `PYTHON_VERSION` = `3.12.11` — Render needs the full
three-part number there.

**"Application failed to respond" or 502**
Usually just waking — wait a minute and reload. If it persists, open **Logs** in
Render. `could not translate host name` or `password authentication failed` means
`DATABASE_URL` is wrong or was pasted incomplete.

**Neon connection or SSL errors**
Make sure the whole line was pasted, including `?sslmode=require`. If Neon's
string ends `&channel_binding=require` and it still refuses, delete just that last
part — the connection stays encrypted either way.

**The setup page says "Page not found" before you made your account**
Somebody else created the first admin. Say so straight away — the database gets
rebuilt and you start again, ten minutes. This is why Part 5 says to do it
immediately.

**Everyone logged out after a deploy**
Harmless, and expected if `MIDAP_SECRET` changed. Sign in again. No data affected.

**Attachments vanish after a day**
`STORAGE_BACKEND` is not `db`. Check the spelling in Render's environment
variables, then check `/healthz` says `"file_storage":"db"`.

**Times look about five and a half hours out**
That was a real bug — the software compared deadlines against UTC while everybody
types Indian times, so a 6 pm task only went overdue at 11:30 pm and the on-time
half of every EM score was wrong by the same margin. Fixed 23 September 2026. If
you see it, you are running an older build: push again.

**The whole site suspends near month end**
The 750 free hours are used up. Narrow the cron-job.org schedule to fewer hours a
day, and delete any other free service in the same Render account. It returns on
the 1st.

**The repository is public — is that a problem?**
Not for your data: no password, connection string or record of any kind is in
there. Every secret lives only in Render's settings. What is public is the
software itself. To change that: GitHub → the repository → **Settings** → **Danger
Zone** → **Change visibility** → **Private**. Render's free plan deploys from
private repositories perfectly well.

---

*Written 23 September 2026, against the version that passes 503 checks on both
SQLite and PostgreSQL. The whole sequence above — empty database, setup page,
branch, user, task, proof, submit, restart — was rehearsed end to end on a UTC
server before this was written.*
