import asyncio
import os
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from starlette.middleware.sessions import SessionMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import (
    SECRET_KEY, BASE_DIR, APP_NAME, SERVERLESS, CRON_SECRET, DATABASE_URL,
)
from . import clock
from .db import Base, engine, SessionLocal
from .deps import RedirectToLogin
from . import migrate
from .routers import (auth, tasks, flows, dashboard, admin, attachments, bulk,
                      help as help_router, setup as setup_router,
                      followups as followups_router,
                      reports as reports_router,
                      detective as detective_router,
                      cmd as cmd_router)
from .services import recurring
from .services import detective
from .services import flows as flow_svc
from .templating import templates
from . import lastview


# How often the loop looks up while it is awake. Ten minutes was chosen so
# the new day's checklist appeared within ten minutes of midnight; the loop
# now guarantees a tick just after midnight in its own right (see below), so
# that precision buys nothing and every extra tick is another database
# wake-up to pay for. Half an hour is plenty for handing a few tasks to the
# detective.
SPAWN_CHECK_SECONDS = int(os.getenv("SPAWN_CHECK_SECONDS", "1800"))

# The hours this loop leaves the database alone entirely.
#
# This is a money setting, not a tidiness one. A managed Postgres that bills
# by the hour sleeps when nothing asks it anything — Neon after five idle
# minutes — and wakes for the next query. A background job poking it every
# ten minutes all night therefore never lets it sleep: 144 wake-ups a day,
# most of them at three in the morning with nobody in the building, billed
# exactly as if the office were open around the clock.
#
# During the working day these ticks cost nothing extra, because staff using
# the site keep the database awake anyway. Overnight they were the whole
# expense, so overnight the loop now stops.
#
# It starts at ten past midnight rather than at midnight on purpose: the
# day's checklist has to be created first. The loop is awake across the
# turn of the day, makes the new day's tasks, and only then goes quiet.
#
# Both ends are "HH:MM" on a 24-hour clock, in the office's own time.
QUIET_FROM = os.getenv("QUIET_FROM", "00:10")
QUIET_UNTIL = os.getenv("QUIET_UNTIL", "07:00")


def _mins(hhmm: str, fallback: int) -> int:
    """'07:30' -> 450. A typo in a setting must not stop the app booting."""
    try:
        h, _, m = hhmm.strip().partition(":")
        total = int(h) * 60 + int(m or 0)
        return total if 0 <= total < 24 * 60 else fallback
    except (ValueError, AttributeError):
        return fallback


_QF = _mins(QUIET_FROM, 10)        # 00:10
_QU = _mins(QUIET_UNTIL, 420)      # 07:00


def _asleep(now) -> bool:
    """True when the loop should leave the database alone."""
    if _QF == _QU:
        return False                              # quiet hours switched off
    m = now.hour * 60 + now.minute
    if _QF > _QU:                                 # the usual 00:10 -> 07:00
        return m >= _QF or m < _QU
    return _QF <= m < _QU                         # a window inside one day


def _seconds_until_wake(now) -> float:
    """Sleep until quiet hours end — but never for more than an hour at once.

    The cap matters: a container paused and resumed, or a clock that jumps,
    must not leave the loop stranded for most of a day.
    """
    wake = now.replace(hour=_QU // 60, minute=_QU % 60, second=0, microsecond=0)
    if wake <= now:
        wake += timedelta(days=1)
    return max(1.0, min((wake - now).total_seconds(), 3600))


def _seconds_to_next_tick(now) -> float:
    """The ordinary wait — shortened when midnight falls inside it.

    Without this the loop could tick at 23:50, sleep its half hour, and open
    its eyes at 00:20 — by then inside quiet hours, so the new day's
    checklist would not be made until seven in the morning. So when the turn
    of the day lands inside the next wait, the loop wakes there instead.
    """
    midnight = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=30, microsecond=0)
    # No floor beyond zero: midnight is always in the future here, so this
    # cannot go negative, and a floor would quietly override a shorter
    # interval set deliberately — which is exactly how the test that drives
    # this loop at speed stopped working.
    return max(0.0, min(SPAWN_CHECK_SECONDS, (midnight - now).total_seconds()))


async def _daily_spawn():
    """Create each day's checklist tasks, once, for as long as we are running.

    Checks the date rather than sleeping 24 hours, so it survives the clock
    moving and starts the new day within ten minutes of midnight. run_spawn
    is blocking, so it goes to a thread — a slow database must not freeze
    every request on the server.
    """
    last_done = None
    while True:
        try:
            now = clock.now()
            # Overnight the loop does nothing at all, so the database can
            # sleep and stop being charged for. The day's checklist is
            # created at the first tick after quiet hours end, which is
            # before anybody starts work.
            if _asleep(now):
                await asyncio.sleep(_seconds_until_wake(now))
                continue
            today = clock.today()
            if today != last_done:
                created = await asyncio.to_thread(_spawn_once)
                last_done = today
                if created:
                    print(f"Checklist for {today}: created {created} task(s)")
            # And a few of the oldest unchecked tasks handed to the
            # detective, every tick, within the day's allowance. The free
            # allowance is about twenty a day, so a backlog only ever clears
            # if something spends it without being asked to.
            looked = await asyncio.to_thread(detective.top_up)
            if looked:
                print(f"Task Detective: checked {looked} task(s)")

            # Every FMS run that is still going: give it a plan if it
            # started before plans existed, and unstick it if its last step
            # closed and nothing opened after it. On a timer rather than
            # when somebody opens the run, because a report nobody can
            # trust until somebody has clicked through every run is not a
            # report.
            caught = await asyncio.to_thread(_catch_up_runs)
            if caught:
                print(f"FMS: brought {caught} run(s) up to date")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A bad day must not kill the loop — tomorrow deserves a try.
            print(f"Checklist spawn failed: {exc!r}")
        await asyncio.sleep(_seconds_to_next_tick(clock.now()))


def _spawn_once() -> int:
    with SessionLocal() as db:
        return recurring.run_spawn(db)


def _catch_up_runs() -> int:
    with SessionLocal() as db:
        return flow_svc.catch_up_all(db)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # On serverless this runs on every cold start. Keep it cheap: create_all
    # and the column check are no-ops once the schema is in place.
    Base.metadata.create_all(engine)
    added = migrate.run()
    if added:
        print("Schema updated: " + ", ".join(added))

    # A host with no shell (Render's free plan) cannot run a setup script, so
    # the first admin is created here when the environment supplies one.
    from .routers.setup import needs_setup, create_admin
    admin_email, admin_pw = os.getenv("ADMIN_EMAIL"), os.getenv("ADMIN_PW")
    if admin_email and admin_pw:
        with SessionLocal() as db:
            if needs_setup(db):
                create_admin(db, os.getenv("ADMIN_NAME", "Administrator"),
                             admin_email, admin_pw)
                print(f"Created the first administrator: {admin_email}")

    # Today's checklist tasks. This used to happen only at boot, which was
    # fine while the host slept every night and woke each morning — the wake
    # WAS the daily trigger. Keep such a host awake around the clock and it
    # never boots again, so the checklist silently stops after day one.
    #
    # So the app now carries its own daily clock: it spawns at boot and then
    # once per day for as long as it is running, whatever the host does.
    # /cron/spawn still works and is still safe to call — each rule fires at
    # most once a day either way.
    spawner = None
    if not SERVERLESS:
        spawner = asyncio.create_task(_daily_spawn())
    try:
        yield
    finally:
        if spawner:
            spawner.cancel()
            try:
                await spawner
            except asyncio.CancelledError:
                pass


class HeadAsGet:
    """Answer HEAD anywhere GET is answered.

    A route registered with @app.get only accepts GET, so every page in this
    app refused HEAD with 405 — including /healthz, which is exactly what an
    uptime monitor pings. UptimeRobot, Better Stack and most link checkers
    send HEAD first, so the site looked permanently down while being
    perfectly healthy.

    HTTP says a HEAD response carries the same status and headers as the GET
    would, with no body. So the request is served as a GET and the body is
    dropped on the way out — which keeps Content-Length truthful rather than
    inventing a zero.

    Written as plain ASGI so it sits outside routing and covers every route,
    including ones added later, instead of a per-endpoint patch somebody has
    to remember.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("method") != "HEAD":
            return await self.app(scope, receive, send)

        finished = False

        async def send_without_body(message):
            nonlocal finished
            if message["type"] == "http.response.body":
                if finished:
                    return          # a streamed response: swallow the rest
                finished = True
                message = {"type": "http.response.body", "body": b"",
                           "more_body": False}
            await send(message)

        await self.app(dict(scope, method="GET"), receive, send_without_body)



class RememberTheList:
    """Keep hold of whichever filtered list the person is looking at.

    Done here rather than in each list route so it cannot be forgotten when
    the next list page is added, and so it covers every filter on every list
    without a line of code per filter.

    Only a successful GET counts: a failed page is nowhere to go back to,
    and a POST is an action rather than a place.

    This must sit INSIDE SessionMiddleware. The session cookie is written as
    the response travels back out through that middleware, so a change made
    outside it is made after the cookie has already gone and is lost without
    a word. Being inner means this sees http.response.start first, updates
    the session, and only then hands the message on to be serialised.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "GET":
            await self.app(scope, receive, send)
            return

        async def send_remembering(message):
            if message["type"] == "http.response.start" \
                    and message["status"] == 200:
                lastview.remember_scope(scope)
            await send(message)

        await self.app(scope, receive, send_remembering)


app = FastAPI(title=APP_NAME, lifespan=lifespan)
# Added first, so it ends up innermost — see the class docstring.
app.add_middleware(RememberTheList)
app.add_middleware(HeadAsGet)
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    max_age=60 * 60 * 12,
    https_only=SERVERLESS,      # the cookie is HTTPS-only once deployed
    same_site="lax",
)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")



@app.exception_handler(RedirectToLogin)
async def _redirect_login(request: Request, exc: RedirectToLogin):
    return RedirectResponse("/login", status_code=303)


HEADINGS = {
    400: "That didn't work",
    403: "You don't have access to that",
    404: "Page not found",
    500: "Something went wrong",
}


@app.exception_handler(StarletteHTTPException)
async def _friendly_error(request: Request, exc: StarletteHTTPException):
    """Show people a readable page instead of a raw {"detail": ...} blob.

    A browser gets HTML; anything asking for JSON (the uploader, the live
    score refresh) still gets JSON, because those callers parse the message.
    """
    wants_json = "application/json" in (request.headers.get("accept") or "") \
        or request.url.path.startswith(("/api/", "/attach/")) \
        or "/attach/" in request.url.path
    if wants_json:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    ctx = {"user": None, "code": exc.status_code,
           "heading": HEADINGS.get(exc.status_code, "Something went wrong"),
           "detail": exc.detail or "No further detail."}

    # The page must be rendered while the database session is still open —
    # the sidebar reads user.branch, and a closed session cannot load it.
    try:
        uid = request.session.get("uid")
    except Exception:
        uid = None
    if not uid:
        return templates.TemplateResponse(request, "error.html", ctx,
                                          status_code=exc.status_code)
    try:
        with SessionLocal() as db:
            from .models import User
            ctx["user"] = db.get(User, uid)
            return templates.TemplateResponse(request, "error.html", ctx,
                                              status_code=exc.status_code)
    except Exception:
        ctx["user"] = None
        return templates.TemplateResponse(request, "error.html", ctx,
                                          status_code=exc.status_code)


app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(tasks.router)
app.include_router(flows.router)
app.include_router(admin.router)
app.include_router(attachments.router)
app.include_router(bulk.router)
app.include_router(help_router.router)
app.include_router(setup_router.router)
app.include_router(followups_router.router)
app.include_router(reports_router.router)
app.include_router(detective_router.router)
app.include_router(cmd_router.router)


@app.get("/healthz")
def healthz():
    from .services import storage
    return {
        "ok": True,
        "serverless": SERVERLESS,
        "database": "postgres" if not DATABASE_URL.startswith("sqlite") else "sqlite",
        "file_storage": storage.mode(),
    }


@app.get("/cron/sync-sheet")
def cron_sync_sheet(authorization: str | None = Header(default=None), key: str = ""):
    """Refresh the Google Sheet mirror. Called on a schedule."""
    if CRON_SECRET:
        supplied = (authorization or "").removeprefix("Bearer ").strip() or key
        if supplied != CRON_SECRET:
            raise HTTPException(401, "Bad or missing cron secret")

    from .services import sheets
    from .models import Organization
    if not sheets.enabled():
        return JSONResponse({"ok": False, "error": "Sheet not configured"})

    with SessionLocal() as db:
        org = db.scalar(select(Organization))
        if org is None:
            return JSONResponse({"ok": False, "error": "No organisation yet"})
        return JSONResponse(sheets.sync(db, org.id))


@app.get("/cron/spawn")
def cron_spawn(authorization: str | None = Header(default=None),
               key: str = ""):
    """Create today's Checklist tasks.

    Serverless has no always-on process, so this must be called once a day by
    a scheduler (Vercel Cron sends `Authorization: Bearer <CRON_SECRET>`).
    It is safe to call repeatedly — each rule fires at most once per day.
    """
    if CRON_SECRET:
        supplied = (authorization or "").removeprefix("Bearer ").strip() or key
        if supplied != CRON_SECRET:
            raise HTTPException(401, "Bad or missing cron secret")

    with SessionLocal() as db:
        created = recurring.run_spawn(db)
    return JSONResponse({"created": created})
