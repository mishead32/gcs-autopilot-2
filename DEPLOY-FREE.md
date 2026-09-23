# Put GCS Autopilot online for free

For showing management. Three accounts, all free, no card. About 30 minutes.

| Step | What it is | Why |
|---|---|---|
| 1. GitHub | Where the code sits | Render reads the code from here |
| 2. Neon | The database | Free Render gives you no database |
| 3. Render | Runs the software | Gives you the link to share |

Attachments go **inside the database**, so nothing is lost when the free
server sleeps or restarts. No fourth account, and nothing to pay.

**Not Vercel.** Vercel's free Hobby plan is restricted to non-commercial,
personal use — this is a tool for running a business, so it does not qualify.
Two other things would also break there: uploads pass through the server and
Vercel caps a request at about 4.5 MB, so a 10 MB phone photo would fail; and
there is no always-on process for the daily Checklist. Render's free plan has
none of those problems.

---

## Before you start — what "free" costs you

Two things, and you should know them before management clicks the link.

**It sleeps.** Render spins a free service down after **15 minutes** with no
traffic. The next person to open the link waits **about a minute** while it
wakes; Render shows a loading page meanwhile. After that it is normal speed.
Tell whoever you send the link to: *"if it takes a minute to open the first
time, that's normal."* Better still, open it yourself five minutes before the
meeting so it is already awake.

**Space is limited.** Neon's free database is **0.5 GB per project**, and
attachments live inside it — roughly **300–800 phone photos**. When Neon fills
up it stops accepting writes rather than charging you, and because proof is
mandatory that means **nobody can submit a task**. Watch the storage figure on
your Neon dashboard; when it passes about 70%, come back and we will move
attachments to file storage. That is one setting, not a code change.

Neither matters for a demo. Both matter for daily use by 30 people. When the
answer is yes, come back and we will move it to a paid plan properly.

---

## Step 1 — GitHub (10 min)

1. Sign up at **https://github.com/signup** — free, no card.
2. Go to **https://github.com/new**
   - Repository name: `gcs-autopilot`
   - Choose **Private**
   - Do **not** tick "Add a README"
   - Click **Create repository**
3. On the next page click **uploading an existing file**.
4. Open your `midap` folder on your PC. Drag **everything except these** into
   the browser window:
   - `.venv` (huge, and Render builds its own)
   - `midap.db` (your local test data)
   - `uploads` (local files)
   - `setup-log.txt`
5. Wait for the uploads to finish, then click **Commit changes**.

> If drag-and-drop struggles with the folders, install GitHub Desktop instead
> — it handles nested folders properly.

---

## Step 2 — Neon, the database (5 min)

1. Sign up at **https://neon.com** with your GitHub account.
2. Create a project:
   - Name: `gcs-autopilot`
   - Postgres version: leave the default
   - Region: **AWS Asia Pacific 1 (Singapore)**

   > Mumbai is not offered on the free plan, and Singapore is the right choice
   > anyway. What matters is that the database sits beside the **app**, not
   > beside you: every page makes several database calls, and your Render
   > service is in Singapore too, so those hops are about a millisecond. Your
   > browser talks to Render only once per page, so the extra distance from
   > Chandigarh costs a few tens of milliseconds — imperceptible. A Mumbai
   > database with a Singapore app would be slower, not faster.

3. On the project dashboard, find **Connection string** and click copy.
   It looks like:
   ```
   postgresql://neondb_owner:xxxxxxxx@ep-cool-name.ap-southeast-1.aws.neon.tech/neondb?sslmode=require
   ```
4. Paste it into Notepad. You need it in the next step.

That is all. The tables build themselves on first start.

---

## Step 3 — Render, which runs it (10 min)

1. Sign up at **https://render.com** — choose **Sign in with GitHub**.
2. **New +** → **Web Service** → **Build and deploy from a Git repository**.
3. Find `gcs-autopilot` and click **Connect**. Authorise Render to see it if
   asked.
4. Fill in:

   | Field | Value |
   |---|---|
   | Name | `gcs-autopilot` |
   | Region | **Singapore** |
   | Branch | `main` |
   | Runtime | **Python 3** |
   | Build Command | `pip install -r requirements.txt` |
   | Start Command | `uvicorn app.main:app --host 0.0.0.0 --port $PORT` |
   | Instance Type | **Free** |

5. Click **Advanced** → **Add Environment Variable**, and add these four:

   | Key | Value |
   |---|---|
   | `DATABASE_URL` | the Neon connection string from step 2 |
   | `STORAGE_BACKEND` | `db` |
   | `MIDAP_SECRET` | any long random text — mash the keyboard, 40+ characters |
   | `PYTHON_VERSION` | `3.12` |

   `STORAGE_BACKEND=db` is the one that keeps attachments alive. Without it,
   every photo disappears the first time the server sleeps.

6. Click **Create Web Service** and wait. First build takes 3–5 minutes.
   When the log ends with `Application startup complete`, it is live.

Your link appears at the top of the page:
`https://gcs-autopilot.onrender.com`

---

## Step 4 — Create your first login

Open your link. Because the database is empty, it shows a **one-time setup
page** asking you to create the first administrator. Fill it in and you are
signed in.

That page exists **only while there are no users at all** and disappears the
moment you create one, so nobody can use it later to mint themselves an
account. Do this immediately after deploying — do not hand the link out first.

> Render's free plan has no Shell tab (that is a paid feature), so there is no
> command to run. If you would rather set the password in advance, add
> `ADMIN_EMAIL` and `ADMIN_PW` as environment variables before the first
> deploy and the account is created automatically at start-up instead.

Then set up your real branches and staff:

- **Branches** → add Bodyzone, Spa Kora, BIPS, GCS Jharkhand, GCS HO
- **Users** → add your people. Tick **Audit submissions** and
  **See all branches** for whoever audits. Set each person's benchmark.
- **Holidays** → add this year's holidays before anyone starts using it
- **Bulk import** → load your delegation tasks and checklist rules from Excel

---

## Step 5 — Today's checklist tasks

Render's free plan has no scheduler, so nothing creates the daily Checklist
tasks by itself. Two options:

**For a demo:** click **Run now** on the Checklist page. Takes a second.

**For daily use:** use a free external scheduler such as https://cron-job.org
to call this once each morning:

```
https://gcs-autopilot.onrender.com/cron/spawn
```

It is safe to call repeatedly — each rule fires at most once per day. Calling
it also wakes the service up, which is a useful side effect if you schedule it
for 15 minutes before your team starts.

---

## Updating it later

Upload the changed files to GitHub again (or push from GitHub Desktop).
Render notices and redeploys within a minute or two. Your data is in Neon, so
it is untouched by a redeploy.

---

## When management says yes

Two changes, roughly ₹1,200/month total:

1. **Render Starter — $7/month.** No sleeping, instant every time.
2. **Neon Launch — $19/month**, or Cloudflare R2 for attachments (10 GB free,
   then about ₹1.30 per GB). With R2 you set four `S3_*` settings and change
   `STORAGE_BACKEND` to `s3` — the code already supports it, and existing
   attachments keep working from the database.

Do that when it is being used, not before.
