import asyncio
from contextlib import asynccontextmanager

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
                      reports as reports_router)
from .services import recurring
from .templating import templates


SPAWN_CHECK_SECONDS = 600      # look at the calendar every ten minutes


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
            today = clock.today()
            if today != last_done:
                created = await asyncio.to_thread(_spawn_once)
                last_done = today
                if created:
                    print(f"Checklist for {today}: created {created} task(s)")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A bad day must not kill the loop — tomorrow deserves a try.
            print(f"Checklist spawn failed: {exc!r}")
        await asyncio.sleep(SPAWN_CHECK_SECONDS)


def _spawn_once() -> int:
    with SessionLocal() as db:
        return recurring.run_spawn(db)


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
    import os
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


app = FastAPI(title=APP_NAME, lifespan=lifespan)
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
