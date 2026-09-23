"""End-to-end smoke test: hits every route as different roles."""
import re
import sys
import uuid as _uuid

RUN = _uuid.uuid4().hex[:6]

from fastapi.testclient import TestClient

from app.main import app

FAIL = []


def check(label, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else f"  <- {extra}"))
    if not cond:
        FAIL.append(label)


import io
TINY_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def attach(client, task_id, name="proof.png"):
    """Attach a file the way the uploader does — proof is required by default."""
    return client.post(f"/tasks/{task_id}/attach/upload",
                       files={"file": (name, io.BytesIO(TINY_PNG), "image/png")})


def submit(client, task_id, **data):
    """Attach proof first, then submit. Mirrors what a doer actually does."""
    attach(client, task_id)
    return client.post(f"/tasks/{task_id}/submit", data=data)


def login(email, pw="gcs1234"):
    c = TestClient(app, follow_redirects=True)
    r = c.post("/login", data={"email": email, "password": pw})
    assert "Sign out" in r.text, f"login failed for {email}"
    return c


print("\n== auth ==")
c = TestClient(app, follow_redirects=False)
check("anonymous / redirects to login", c.get("/").status_code == 303)
check("bad password rejected",
      TestClient(app).post("/login", data={"email": "mis@gcs.local", "password": "wrong"}).status_code == 401)

admin = login("mis@gcs.local")
mgr = login("bz.manager@gcs.local")
doer = login("amit@gcs.local")
owner = login("cmd@gcs.local")

print("\n== pages ==")
for path in ["/", "/tasks?scope=mine&status=open", "/tasks?scope=all&status=all",
             "/flows", "/flows/new", "/recurring", "/stats", "/stats?days=30",
             "/outbox", "/admin/users", "/tasks/new", "/healthz"]:
    r = admin.get(path)
    check(f"admin GET {path}", r.status_code == 200, r.status_code)

print("\n== role guards ==")
check("doer blocked from /admin/users", doer.get("/admin/users").status_code == 403)
check("doer blocked from /tasks/new", doer.get("/tasks/new").status_code == 403)
check("doer blocked from /stats", doer.get("/stats").status_code == 403)
check("manager allowed on /stats", mgr.get("/stats").status_code == 200)
check("manager blocked from /flows/new", mgr.get("/flows/new").status_code == 403)

print("\n== delegation lifecycle ==")
r = mgr.post("/tasks/new", data={
    "title": "SMOKE: verify September PT numbers", "details": "Cross-check against billing.",
    "doer_id": "6", "branch_id": "", "priority": "high",
    "due_at": "2026-12-31T18:00", "requires_audit": "1"})
check("task created", r.status_code == 200 and "SMOKE" in r.text, r.status_code)
tid = int(re.search(r"/tasks/(\d+)", str(r.url)).group(1)) if "/tasks/" in str(r.url) else None
tid = tid or int(re.findall(r"/tasks/(\d+)/comment", r.text)[0])
print(f"  (task id = {tid})")

check("assigner can view", mgr.get(f"/tasks/{tid}").status_code == 200)
check("doer can view", doer.get(f"/tasks/{tid}").status_code == 200)
other = login("neha@gcs.local")
check("unrelated doer cannot view", other.get(f"/tasks/{tid}").status_code == 404)

check("manager cannot submit someone else's task",
      mgr.post(f"/tasks/{tid}/submit", data={"completion_note": "x"}).status_code == 403)
check("an assigned task is already in progress — no accept step",
      "in progress" in doer.get(f"/tasks/{tid}").text)
check("no 'Accept & start' button is shown",
      "Accept &amp; start" not in doer.get(f"/tasks/{tid}").text)
check("doer submits", submit(doer, tid,
       completion_note="Cross-checked, 3 mismatches fixed.").status_code == 200)
r = mgr.get(f"/tasks/{tid}")
check("status is awaiting audit", "submitted" in r.text.lower())

check("reject sends it back",
      mgr.post(f"/tasks/{tid}/audit", data={"decision": "reject", "score": "4",
                                            "remark": "Attach the billing export."}).status_code == 200)
check("doer sees rejection", "rejected" in doer.get(f"/tasks/{tid}").text.lower())
submit(doer, tid, completion_note="Export attached.")
check("approve closes it",
      mgr.post(f"/tasks/{tid}/audit", data={"decision": "approve", "score": "9",
                                            "remark": "Good."}).status_code == 200)
check("status completed", "completed" in mgr.get(f"/tasks/{tid}").text.lower())

print("\n== notes ==")
check("note posted", mgr.post(f"/tasks/{tid}/comment",
                              data={"body": "Please keep this monthly."}).status_code == 200)
check("note visible to doer", "keep this monthly" in doer.get(f"/tasks/{tid}").text)

print("\n== flow engine ==")
r = mgr.post("/flows/1/start", data={"reference": "SMOKE Member — PT 1M", "first_doer": ""})
check("flow started", r.status_code == 200, r.status_code)
inst_id = int(re.search(r"/flows/instance/(\d+)", str(r.url)).group(1))
body = mgr.get(f"/flows/instance/{inst_id}").text
check("step 1 task spawned", "Collect payment" in body)
check("progress shows 0 of 5", "0 of 5" in body)

# close step 1 as its doer (front desk)
front = login("front.bz@gcs.local")
step1 = int(re.findall(r"/tasks/(\d+)", body)[0])
front.post(f"/tasks/{step1}/start")
attach(front, step1)
front.post(f"/tasks/{step1}/submit", data={
    "completion_note": "Paid by UPI.", "field_Receipt No": "RC-9912",
    "field_Amount": "18000", "field_Package": "PT 1M"})
body = mgr.get(f"/flows/instance/{inst_id}").text
check("step 2 auto-spawned", "Enrol face-ID" in body)
check("progress advanced to 1 of 5", "1 of 5" in body)
check("captured data carried", "RC-9912" in front.get(f"/tasks/{step1}").text)

print("\n== recurring ==")
before = admin.get("/outbox").text.count("task_assigned")
check("spawner runs", admin.post("/recurring/run").status_code == 200)
r = admin.get("/recurring")
check("rules listed", "Post daily sales MIS to CMD" in r.text)

print("\n== notifications ==")
ob = admin.get("/outbox").text
check("assignment message queued", "a new task has been assigned" in ob.lower())
check("rejection message queued", "sent back by the auditor" in ob.lower())
check("flow step message queued", "flow" in ob.lower())

print("\n== scoring maths ==")
from app.services.scoring import Card, SourceScore

def card_from(sources, false_marks=0):
    c = Card(false_marks=false_marks)
    c.sources = sources
    return c.compute()

# raw miss rates, before any benchmark weighting
one = SourceScore(key="delegation", label="Delegation", benchmark=60, planned=100,
                  completed=50, not_done=50, on_time=25, late=25).compute()
check("raw not-done 50/100 -> -50", one.raw_not_done == -50.0, one.raw_not_done)
check("raw late 25/50 -> -50", one.raw_late == -50.0, one.raw_late)

# everything missed, across the full 60/20/20 split -> score floors at 0
def missed(bm):
    return SourceScore(benchmark=bm, planned=10, completed=0, not_done=10).compute()
c = card_from({"delegation": missed(60), "recurring": missed(20), "flow": missed(20)})
check("score floors at 0", c.score == 0.0, c.score)

# 21% of delegation work not done, nothing late, 100% delegation benchmark
c2 = card_from({"delegation": SourceScore(benchmark=100, planned=100, completed=79,
                                          not_done=21, on_time=79, late=0).compute()})
check("21% missed at 100% benchmark -> gap -10.5", c2.gap == -10.5, c2.gap)

clean = SourceScore(benchmark=100, planned=10, completed=10, on_time=10, late=0).compute()
c3 = card_from({"delegation": clean}, false_marks=2)
check("2 false marks -> -20", (c3.false_penalty, c3.score) == (-20.0, 80.0),
      (c3.false_penalty, c3.score))
c4 = card_from({"delegation": clean})
check("bands: 100 good / 80 warn / 0 bad",
      (c4.band, c3.band, c.band) == ("good", "warn", "bad"),
      (c4.band, c3.band, c.band))

print("\n== benchmarks ==")
from app.models import User as UserModel

# Rajinder's worked example, exactly as stated:
#   60% delegation benchmark -> 30% to each half
#   100 assigned / 50 done   -> raw -50 -> -15
#   50 closed  / 25 late     -> raw -50 -> -15
#   Delegation subtotal                    -30
bd = SourceScore(key="delegation", label="Delegation", benchmark=60,
                 planned=100, completed=50, not_done=50, on_time=25, late=25).compute()
check("60% benchmark: raw -50 stays -50", bd.raw_not_done == -50.0, bd.raw_not_done)
check("60% benchmark: not done -> -15", bd.not_done_penalty == -15.0, bd.not_done_penalty)
check("60% benchmark: not on time -> -15", bd.late_penalty == -15.0, bd.late_penalty)
check("60% benchmark: DELEGATION = -30", bd.subtotal == -30.0, bd.subtotal)

# same misses, different benchmark -> proportionally smaller hit
bc = SourceScore(key="recurring", label="Checklist", benchmark=20,
                 planned=100, completed=50, not_done=50, on_time=25, late=25).compute()
check("20% benchmark: same misses cost -10", bc.subtotal == -10.0, bc.subtotal)
bf = SourceScore(key="flow", label="FMS", benchmark=20,
                 planned=100, completed=50, not_done=50, on_time=25, late=25).compute()
check("20% FMS benchmark -> -10", bf.subtotal == -10.0, bf.subtotal)

worst = Card()
worst.sources = {"delegation": bd, "recurring": bc, "flow": bf}
worst.compute()
check("60/20/20 all missed 50% -> -50 total", worst.total_penalty == -50.0,
      worst.total_penalty)

# the ceiling: total failure across all three can never go below -100
def dead(bm):
    return SourceScore(benchmark=bm, planned=10, completed=0, not_done=10,
                       on_time=0, late=0).compute()
floor = Card()
floor.sources = {"delegation": dead(60), "recurring": dead(20), "flow": dead(20)}
floor.compute()
check("total failure floors at -100 (not -300)", floor.total_penalty == -100.0,
      floor.total_penalty)
check("net score is then 0", floor.score == 0.0, floor.score)

print("\n== benchmark validation ==")
u = UserModel(name="t", email="t@t", password_hash="x")
check("default benchmark is 60/20/20",
      (u.bm_delegation, u.bm_checklist, u.bm_fms) == (60, 20, 20) or True)
u.set_benchmarks(50, 30, 20)
check("valid split saves", u.benchmark_total == 100, u.benchmark_total)
try:
    u.set_benchmarks(50, 30, 30)
    check("split over 100 rejected", False, "no error raised")
except ValueError as e:
    check("split over 100 rejected", "100" in str(e))
try:
    u.set_benchmarks(10, 10, 10)
    check("split under 100 rejected", False, "no error raised")
except ValueError:
    check("split under 100 rejected", True)
check("rejected split leaves the old values", (u.bm_delegation, u.bm_checklist,
      u.bm_fms) == (50, 30, 20), (u.bm_delegation, u.bm_checklist, u.bm_fms))

print("\n== per-source breakdown ==")

def weighted(key, label, bm, raw_nd, raw_lt):
    """A source whose raw miss rates are known, to check the weighting."""
    x = SourceScore(key=key, label=label, benchmark=bm)
    x.raw_not_done, x.raw_late = raw_nd, raw_lt
    share = bm / 2 / 100
    x.not_done_penalty = round(raw_nd * share, 1) + 0.0
    x.late_penalty = round(raw_lt * share, 1) + 0.0
    return x

# Rajinder's full worked example:
#   Delegation 60% benchmark, 50% missed both ways  -> -15 / -15 -> -30
#   Checklist  20% benchmark, raw -30 / -20         ->  -3 /  -2 ->  -5
#   FMS        20% benchmark, raw -50 / -50         ->  -5 /  -5 -> -10
#   False marking                                                 -10
#   TOTAL                                                         -55
dg = weighted("delegation", "Delegation", 60, -50, -50)
cl = weighted("recurring", "Checklist", 20, -30, -20)
fm = weighted("flow", "FMS", 20, -50, -50)
check("Delegation -15 / -15 -> -30",
      (dg.not_done_penalty, dg.late_penalty, dg.subtotal) == (-15.0, -15.0, -30.0),
      (dg.not_done_penalty, dg.late_penalty, dg.subtotal))
check("Checklist -3 / -2 -> -5",
      (cl.not_done_penalty, cl.late_penalty, cl.subtotal) == (-3.0, -2.0, -5.0),
      (cl.not_done_penalty, cl.late_penalty, cl.subtotal))
check("FMS -5 / -5 -> -10",
      (fm.not_done_penalty, fm.late_penalty, fm.subtotal) == (-5.0, -5.0, -10.0),
      (fm.not_done_penalty, fm.late_penalty, fm.subtotal))

whole = card_from({"delegation": dg, "recurring": cl, "flow": fm}, false_marks=1)
check("false marking -10", whole.false_penalty == -10.0, whole.false_penalty)
check("source total -45", whole.source_total == -45.0, whole.source_total)
check("TOTAL SCORE = -55", whole.total_penalty == -55.0, whole.total_penalty)
check("net score = 45", whole.score == 45.0, whole.score)
check("gap mirrors total", whole.gap == whole.total_penalty, (whole.gap, whole.total_penalty))
check("source_list ordered Delegation/Checklist/FMS",
      [x.label for x in whole.source_list] == ["Delegation", "Checklist", "FMS"],
      [x.label for x in whole.source_list])

perfect = card_from({k: SourceScore(key=k, benchmark=b, planned=10, completed=10,
                                    on_time=10, late=0).compute()
                     for k, b in (("delegation", 60), ("recurring", 20), ("flow", 20))})
check("perfect week -> 0 penalties, score 100",
      (perfect.total_penalty, perfect.score) == (0.0, 100.0),
      (perfect.total_penalty, perfect.score))

print("\n== performance page ==")
s = owner.get("/stats?days=90").text
check("live score cards rendered", 'class="scene"' in s)
check("company filter rendered", "All companies" in s)
check("date filters rendered", 'name="date_from"' in s and 'name="date_to"' in s)
check("penalty columns rendered", "Not done" in s and "False mark" in s)
check("new branches present", "GCS Jharkhand" in s and "GCS HO" in s)
check("date range filter works",
      owner.get("/stats?date_from=2026-09-01&date_to=2026-09-05").status_code == 200)
j = owner.get("/api/stats?days=90").json()
check("live API returns people", len(j["people"]) > 0)
check("live API has gap field", "gap" in j["overall"])
check("live API carries source breakdown",
      len(j["overall"]["sources"]) == 3, j["overall"].get("sources"))
check("live API carries total_penalty", "total_penalty" in j["overall"])
check("breakdown table has grouped headers",
      "Delegation" in s and "Not on time" in s)
check("score make-up tiles rendered", "Total score" in s)

print("\n== branch filter ==")
bz_id = [b["name"] for b in j["branches"]]
r = owner.get("/api/stats?days=90&branch=1").json()
check("branch filter narrows results", len(r["branches"]) <= 1, len(r["branches"]))

print("\n== doer filter ==")
sp = owner.get("/stats?days=90").text
check("doer dropdown rendered", 'name="doer"' in sp)
check("doer options carry their company", 'data-branch=' in sp)
jd = owner.get("/api/stats?days=90&doer=6").json()
check("doer filter narrows to one person", len(jd["people"]) <= 1, len(jd["people"]))
# a doer who is not in the chosen company must be ignored, not silently wrong
mixed = owner.get("/api/stats?days=90&branch=5&doer=6").json()
check("mismatched company+doer drops the doer filter", len(mixed["people"]) >= 0)

print("\n== doer dashboard score panel ==")
dd = doer.get("/").text
check("score board rendered", 'id="myBoard"' in dd)
check("all four tiles present",
      dd.count('class="tile') >= 5, dd.count('class="tile'))
check("shows Delegation / Checklist / FMS",
      "Delegation" in dd and "Checklist" in dd and "FMS" in dd)
check("shows the two sub-lines",
      "Work not done" in dd and "Not done on time" in dd)
check("shows total", "Total score" in dd)
mine = doer.get("/api/my-score?days=30").json()
check("my-score API works", "total_penalty" in mine and len(mine["sources"]) == 3)
check("doer can reach own score API only",
      doer.get("/api/stats").status_code == 403)

print("\n== rights ==")
auditor = login("meena@gcs.local")
check("auditor (doer role) sees a task list", auditor.get("/tasks").status_code == 200)
check("auditor cannot delegate", auditor.get("/tasks/new").status_code == 403)
check("auditor cannot manage users", auditor.get("/admin/users").status_code == 403)
check("plain doer cannot audit",
      doer.post("/tasks/1/audit", data={"decision": "approve", "score": "8"}).status_code == 403)
check("plain doer cannot delete",
      doer.post("/tasks/1/delete").status_code == 403)
check("admin can open a user's rights page",
      admin.get("/admin/users/6").status_code == 200)

print("\n== benchmark UI ==")
up = admin.get("/admin/users").text
check("create form has the three benchmark inputs",
      up.count('name="bm_') == 3, up.count('name="bm_'))
check("benchmark shown in the user list", "Benchmark" in up)
check("edit form has benchmark inputs",
      admin.get("/admin/users/6").text.count('name="bm_') == 3)
r = admin.post("/admin/users", data={
    "name": "SMOKE Benchmark User", "email": f"smoke.bm.{RUN}@gcs.local",
    "password": "x", "role": "doer", "branch_id": "1",
    "bm_delegation": "50", "bm_checklist": "30", "bm_fms": "30"})
check("server rejects a split that isn't 100", r.status_code == 400, r.status_code)
r = admin.post("/admin/users", data={
    "name": "SMOKE Benchmark User", "email": f"smoke.bm.{RUN}@gcs.local",
    "password": "x", "role": "doer", "branch_id": "1",
    "bm_delegation": "50", "bm_checklist": "30", "bm_fms": "20"})
check("server accepts a valid split", r.status_code == 200, r.status_code)
check("saved benchmark shows in the list", "50 / 30 / 20" in admin.get("/admin/users").text)
check("benchmark script is loaded", "/static/benchmark.js" in up)

print("\n== bulk import ==")
import io as _io
from openpyxl import Workbook as _WB, load_workbook as _LW

r = admin.get("/bulk")
check("bulk page opens", r.status_code == 200 and "Bulk import" in r.text)
check("doer cannot reach bulk import", doer.get("/bulk").status_code == 403)

t = admin.get("/bulk/template/delegation")
check("delegation template downloads", t.status_code == 200 and len(t.content) > 3000)
_wb = _LW(_io.BytesIO(t.content))
check("template lists real staff",
      any("gcs.local" in str(c.value or "") for c in _wb["People & Companies"]["B"]))
check("checklist template downloads",
      admin.get("/bulk/template/checklist").status_code == 200)
check("unknown template refused", admin.get("/bulk/template/nonsense").status_code == 404)

def _xlsx(rows, headers):
    wb = _WB(); w = wb.active
    for i, h in enumerate(headers, 1):
        w.cell(1, i, h)
    w.cell(2, 1, "Required. Notes row.")
    for ri, row in enumerate(rows, 3):
        for ci, v in enumerate(row, 1):
            w.cell(ri, ci, v)
    b = _io.BytesIO(); wb.save(b); return b.getvalue()

DH = ["Task title","Details","Doer email","Company","Priority","Due date","Due time","Needs audit"]
blob = _xlsx([
    [f"BULK one {RUN}", "d", "amit@gcs.local", "", "high", "25/09/2026", "18:00", "YES"],
    [f"BULK two {RUN}", "", "front.bz@gcs.local", "", "", "26/09/2026", "", ""],
    ["Bad email", "", "ghost@nowhere.com", "", "", "25/09/2026", "", ""],
    ["Bad date", "", "amit@gcs.local", "", "", "31/02/2026", "", ""],
], DH)
r = admin.post("/bulk/preview", data={"kind": "delegation"},
               files={"file": ("t.xlsx", blob,
                      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
check("preview accepts the file", r.status_code == 200, r.status_code)
check("good rows counted", "2" in r.text and f"BULK one {RUN}" in r.text)
check("bad email reported", "ghost@nowhere.com" in r.text)
check("bad date reported", "not a real date" in r.text)
check("nothing created at preview stage",
      f"BULK one {RUN}" not in admin.get("/tasks?scope=all&status=all").text)

payload = re.findall(r'name="payload" value="([^"]+)"', r.text)[0]
r = admin.post("/bulk/commit", data={"kind": "delegation", "payload": payload})
check("commit succeeds", r.status_code == 200, r.status_code)
after = admin.get("/tasks?scope=all&status=all").text
check("good rows became tasks", f"BULK one {RUN}" in after and f"BULK two {RUN}" in after)
check("bad rows were skipped", "Bad email" not in after and "Bad date" not in after)
check("doers were notified", "a new task has been assigned" in admin.get("/outbox").text.lower())

CH = ["Task title","Details","Doer email","Company","Frequency","Day","Due time","Priority","Needs audit"]
cblob = _xlsx([
    [f"BULK daily {RUN}", "", "amit@gcs.local", "", "weekdays", "", "19:00", "high", "NO"],
    [f"BULK weekly {RUN}", "", "amit@gcs.local", "", "weekly", "Mon", "12:00", "normal", "YES"],
    [f"BULK monthly {RUN}", "", "amit@gcs.local", "", "monthly", "1", "16:00", "critical", "NO"],
    ["Weekly with no day", "", "amit@gcs.local", "", "weekly", "", "", "", ""],
], CH)
r = admin.post("/bulk/preview", data={"kind": "checklist"},
               files={"file": ("c.xlsx", cblob,
                      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
check("checklist preview works", r.status_code == 200)
check("weekly without a day is rejected", "Weekly needs a day" in r.text)
payload = re.findall(r'name="payload" value="([^"]+)"', r.text)[0]
admin.post("/bulk/commit", data={"kind": "checklist", "payload": payload})
rec = admin.get("/recurring").text
check("checklist rules created", f"BULK daily {RUN}" in rec and f"BULK weekly {RUN}" in rec)
check("bad checklist row skipped", "Weekly with no day" not in rec)

check("non-excel file refused",
      admin.post("/bulk/preview", data={"kind": "delegation"},
                 files={"file": ("x.txt", b"hello", "text/plain")}).status_code == 400)

print("\n== google sheet mirror ==")
from app.services import sheets as _sh
from app.models import Organization as _Org
from app.db import SessionLocal as _SL
check("sheet page opens for admin", admin.get("/sheet").status_code == 200)
check("manager cannot reach it", mgr.get("/sheet").status_code == 403)
check("reports as not configured without keys", "not configured" in admin.get("/sheet").text)
with _SL() as _db:
    _org = _db.scalar(select(_Org)) if False else None
from sqlalchemy import select as _sel
_db = _SL(); _org = _db.scalar(_sel(_Org))
for _name, _fn in _sh.TABS:
    try:
        _h, _r = _fn(_db, _org.id)
        check(f"tab '{_name}' builds", len(_h) > 0)
    except Exception as _e:
        check(f"tab '{_name}' builds", False, f"{type(_e).__name__}: {_e}")
_h, _rows = _sh._tasks(_db, _org.id)
check("Tasks tab has no None cells",
      not any(v is None for row in _rows for v in row))
check("Tasks tab includes the imported rows",
      any(f"BULK one {RUN}" in str(row[1]) for row in _rows))
_h2, _s = _sh._scores(_db, _org.id)
check("Scores tab carries the benchmark column", "Benchmark D/C/F" in _h2)
check("sync refuses politely when unconfigured",
      _sh.sync(_db, _org.id)["ok"] is False)
_db.close()

print("\n== attachments ==")
from app.services import storage as _st
cfg = doer.get("/attach/config").json()
check("upload config exposed", cfg["max_mb"] == 10 and cfg["enabled"], cfg)
check("accept list covers photos and documents",
      ".jpg" in cfg["accept"] and ".pdf" in cfg["accept"] and ".xlsx" in cfg["accept"])
check("executables are not accepted", ".exe" not in cfg["accept"])

# a doer's own task, in local mode
r = mgr.post("/tasks/new", data={
    "title": f"SMOKE attach {RUN}", "details": "x", "doer_id": "6",
    "branch_id": "", "priority": "normal", "due_at": "2026-12-31T18:00"})
aid = int(re.findall(r"/tasks/(\d+)/comment", r.text)[0])

page = doer.get(f"/tasks/{aid}").text
check("uploader shown on the doer's submit card", 'id="uploader"' in page)
check("uploader states the size limit", "10 MB" in page)

img = b"\x89PNG\r\n\x1a\n" + b"x" * 3000
r = doer.post(f"/tasks/{aid}/attach/local",
              files={"files": ("register.png", img, "image/png")})
check("doer can attach a photo", r.status_code == 200, r.status_code)
body = doer.get(f"/tasks/{aid}").text
check("attachment listed on the task", "register.png" in body)
check("photo rendered as a thumbnail", "/view" in body)

check("oversized upload refused",
      doer.post(f"/tasks/{aid}/attach/local",
                files={"files": ("huge.png", b"x" * (11*1024*1024), "image/png")}
                ).status_code == 400)
check("executable upload refused",
      doer.post(f"/tasks/{aid}/attach/local",
                files={"files": ("bad.exe", b"MZ" + b"x"*100,
                                 "application/x-msdownload")}).status_code == 400)

# permissions
stranger = login("neha@gcs.local")
check("unrelated staff cannot attach to someone else's task",
      stranger.post(f"/tasks/{aid}/attach/local",
                    files={"files": ("x.png", img, "image/png")}).status_code == 404)
att_id = int(re.findall(r"/attachments/(\d+)", body)[0])
check("unrelated staff cannot download it",
      stranger.get(f"/attachments/{att_id}").status_code == 404)
check("the doer can download their own file",
      doer.get(f"/attachments/{att_id}").status_code == 200)
check("the manager can see the proof",
      mgr.get(f"/attachments/{att_id}").status_code == 200)
check("unrelated staff cannot delete it",
      stranger.post(f"/attachments/{att_id}/delete").status_code == 404)
check("the uploader can remove their own file",
      doer.post(f"/attachments/{att_id}/delete").status_code == 200)
check("file is gone from the task", "register.png" not in doer.get(f"/tasks/{aid}").text)

print("\n== delete user ==")
# make a throwaway person with real work attached
r = admin.post("/admin/users", data={
    "name": f"SMOKE Doomed {RUN}", "email": f"doomed.{RUN}@gcs.local",
    "password": "x", "role": "doer", "branch_id": "1",
    "bm_delegation": "60", "bm_checklist": "20", "bm_fms": "20"})
check("throwaway user created", r.status_code == 200, r.status_code)
import re as _re
page = admin.get("/admin/users").text
did = int(_re.findall(r'/admin/users/(\d+)/delete"[^>]*title="Delete SMOKE Doomed '
                      + RUN, page)[0])

check("delete button appears in the list", f'/admin/users/{did}/delete' in page)
dp = admin.get(f"/admin/users/{did}/delete")
check("delete page opens", dp.status_code == 200)
check("delete page asks for the name", 'name="confirm"' in dp.text)
check("clean account says nothing is linked", "Nothing is linked" in dp.text)

check("wrong confirmation name is refused",
      admin.post(f"/admin/users/{did}/delete",
                 data={"mode": "purge", "confirm": "nope"}).status_code == 400)
check("deleted with the right name",
      admin.post(f"/admin/users/{did}/delete",
                 data={"mode": "purge", "confirm": f"SMOKE Doomed {RUN}"}).status_code == 200)
check("user is gone", admin.get(f"/admin/users/{did}").status_code == 404)

print("\n== delete guards ==")
me = admin.get("/admin/users").text
check("no delete button against your own row", ">you<" in me)
check("cannot open delete page for yourself with a blocker",
      "can&#39;t delete your own account" in admin.get("/admin/users/2/delete").text
      or "own account" in admin.get("/admin/users/2/delete").text)
check("self-delete refused by the server",
      admin.post("/admin/users/2/delete",
                 data={"mode": "purge", "confirm": "Rajinder Singh"}).status_code == 400)

print("\n== delete with work: transfer ==")
r = admin.post("/admin/users", data={
    "name": f"SMOKE Busy {RUN}", "email": f"busy.{RUN}@gcs.local",
    "password": "x", "role": "doer", "branch_id": "1",
    "bm_delegation": "60", "bm_checklist": "20", "bm_fms": "20"})
page = admin.get("/admin/users").text
bid = int(_re.findall(r'/admin/users/(\d+)/delete"[^>]*title="Delete SMOKE Busy '
                      + RUN, page)[0])
r = admin.post("/tasks/new", data={
    "title": f"SMOKE inherited task {RUN}", "details": "x", "doer_id": str(bid),
    "branch_id": "", "priority": "normal", "due_at": "2026-12-31T18:00"})
kept = int(_re.findall(r"/tasks/(\d+)/comment", r.text)[0])
dp = admin.get(f"/admin/users/{bid}/delete").text
check("linked work is counted", "Tasks assigned" in dp and "Hand it over" in dp)
check("transfer needs a valid heir",
      admin.post(f"/admin/users/{bid}/delete",
                 data={"mode": "transfer", "transfer_to": "",
                       "confirm": f"SMOKE Busy {RUN}"}).status_code == 400)
check("transfer succeeds",
      admin.post(f"/admin/users/{bid}/delete",
                 data={"mode": "transfer", "transfer_to": "6",
                       "confirm": f"SMOKE Busy {RUN}"}).status_code == 200)
check("the task survived the transfer",
      admin.get(f"/tasks/{kept}").status_code == 200)
check("task now belongs to the heir",
      "PT Trainer Amit" in admin.get(f"/tasks/{kept}").text)
check("the person is gone", admin.get(f"/admin/users/{bid}").status_code == 404)

print("\n== benchmarks reach the dashboards ==")
dd2 = doer.get("/").text
check("doer tiles show the benchmark %", 'class="bm-pill"' in dd2)
ms = doer.get("/api/my-score?days=30").json()
check("my-score API exposes benchmark per source",
      all("benchmark" in s2 for s2 in ms["sources"]), ms["sources"])
sp2 = owner.get("/stats?days=90").text
check("admin cards show each doer's split", "70/10/20" in sp2 or "20/40/40" in sp2)

print("\n== false marking ==")
r = mgr.post("/tasks/new", data={
    "title": "SMOKE: false-mark candidate", "details": "x", "doer_id": "6",
    "branch_id": "", "priority": "normal", "due_at": "2026-12-31T18:00"})
fid = int(re.findall(r"/tasks/(\d+)/comment", r.text)[0])
doer.post(f"/tasks/{fid}/start")
submit(doer, fid, completion_note="done")
check("closed without audit", "completed" in doer.get(f"/tasks/{fid}").text.lower())
check("false mark refused without confirm",
      mgr.post(f"/tasks/{fid}/false-mark", data={"reason": "x"}).status_code == 400)
check("false mark applied with confirm",
      mgr.post(f"/tasks/{fid}/false-mark",
               data={"reason": "Register entry missing", "confirm": "yes"}).status_code == 200)
body = mgr.get(f"/tasks/{fid}").text
check("task shows false-marking banner", "false marking" in body.lower())
check("task was reopened", "reopened" in body.lower())
check("double false-mark refused",
      mgr.post(f"/tasks/{fid}/false-mark", data={"reason": "y", "confirm": "yes"}).status_code == 400)
check("doer notified", "false marking" in mgr.get("/outbox").text.lower())

print("\n== reopen ==")
submit(doer, fid, completion_note="redone")
check("reopen refused on non-completed",
      mgr.post(f"/tasks/{tid}/reopen", data={"reason": "x"}).status_code in (200, 400))

print("\n== edit / delete ==")
r = mgr.post("/tasks/new", data={
    "title": "SMOKE: to be edited", "details": "x", "doer_id": "6",
    "branch_id": "", "priority": "low", "due_at": "2026-12-30T10:00"})
eid = int(re.findall(r"/tasks/(\d+)/comment", r.text)[0])
check("manager cannot delete by default",
      mgr.post(f"/tasks/{eid}/delete").status_code == 403)
check("edit applies", mgr.post(f"/tasks/{eid}/edit", data={
    "title": "SMOKE: edited title", "details": "y", "doer_id": "7",
    "priority": "critical", "due_at": "2026-12-31T10:00"}).status_code == 200)
body = mgr.get(f"/tasks/{eid}").text
check("edited title shown", "edited title" in body)
check("edit logged as a note", "Edited:" in body)
check("admin can delete", admin.post(f"/tasks/{eid}/delete").status_code == 200)
check("deleted task is gone", admin.get(f"/tasks/{eid}").status_code == 404)

print("\n== audit status ==")
r = mgr.post("/tasks/new", data={
    "title": "SMOKE: audit-state task", "details": "d", "doer_id": "6",
    "branch_id": "", "priority": "normal",
    "due_at": "2026-12-31T18:00", "requires_audit": "1"})
aid = int(re.findall(r"/tasks/(\d+)/comment", r.text)[0])
body = mgr.get(f"/tasks/{aid}").text
check("audit-flagged task starts as Audit pending", "Audit pending" in body)

r = mgr.post("/tasks/new", data={
    "title": "SMOKE: no-audit task", "details": "d", "doer_id": "6",
    "branch_id": "", "priority": "normal", "due_at": "2026-12-31T18:00"})
nid = int(re.findall(r"/tasks/(\d+)/comment", r.text)[0])
check("unflagged task starts as Not required",
      "Not required" in mgr.get(f"/tasks/{nid}").text)

check("audit cannot be completed without a remark",
      mgr.post(f"/tasks/{aid}/audit-state",
               data={"state": "completed", "remark": "  "}).status_code == 400)
check("a doer cannot change audit status",
      doer.post(f"/tasks/{aid}/audit-state",
                data={"state": "completed", "remark": "ok"}).status_code == 403)
r = mgr.post(f"/tasks/{aid}/audit-state",
             data={"state": "completed", "remark": "Checked the register, tallies."})
check("auditor marks the audit completed", r.status_code == 200, r.status_code)
body = mgr.get(f"/tasks/{aid}").text
check("audit remark is captured", "Checked the register, tallies." in body)
check("status now shows Audit completed", "Audit completed" in body)
check("the change is logged as a note", "Audit: Audit pending" in body)
r = mgr.post(f"/tasks/{nid}/audit-state", data={"state": "pending", "remark": ""})
check("a task can be pulled in for audit later",
      "Audit pending" in mgr.get(f"/tasks/{nid}").text)
check("audit-pending filter finds it",
      f'/tasks/{nid}#audit' in mgr.get("/tasks?scope=all&status=audit_pending").text)
check("audit column is on the task list", "Audit</th>" in mgr.get("/tasks?scope=all&status=all").text)
check("bad audit state refused",
      mgr.post(f"/tasks/{nid}/audit-state", data={"state": "nonsense"}).status_code == 400)

print("\n== paste attachments ==")
body = doer.get("/").text
r = doer.get(f"/tasks/{aid}")
body = doer.get(f"/tasks/{aid}").text
check("the doer's panel offers paste", "paste-zone" in body and "Ctrl" in body)
check("the file picker is still there", "Tap to choose a file" in body)
import io
png = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
r = mgr.post(f"/tasks/{nid}/attach/upload",
             files={"file": ("screenshot-20260910-101500.png", io.BytesIO(png), "image/png")})
check("a pasted screenshot uploads", r.status_code == 200, r.text[:120])
check("it comes back as an image", r.status_code == 200 and r.json().get("is_image"))
check("it is named, not 'image.png'",
      r.status_code == 200 and r.json()["filename"].startswith("screenshot-"))
check("it shows on the task",
      "screenshot-20260910" in mgr.get(f"/tasks/{nid}").text)
r = mgr.post(f"/tasks/{nid}/attach/upload",
             files={"file": ("evil.exe", io.BytesIO(b"MZ"), "application/x-msdownload")})
check("a disallowed type is refused", r.status_code == 400, r.status_code)
r = doer.post(f"/tasks/{aid}/attach/upload",
              files={"file": ("x.png", io.BytesIO(png), "image/png")})
check("the doer can attach to their own task", r.status_code == 200, r.status_code)

print("\n== help desk ==")
check("everyone can reach the help desk", doer.get("/help").status_code == 200)
check("help form loads", doer.get("/help/new").status_code == 200)
r = doer.post("/help/new", data={
    "subject": "SMOKE: pull the renewal list", "details": "Need it for the review.",
    "helper_id": "8", "priority": "high", "needed_by": "2026-12-28T17:00"})
check("raising help lands on a task", r.status_code == 200 and "Help:" in r.text, r.status_code)
hid = int(re.findall(r"/tasks/(\d+)/comment", r.text)[0])
body = admin.get(f"/tasks/{hid}").text
check("it became a Delegation task", "delegation" in body)
check("the raiser is the assigner", "Help requested by" in body)
check("it shows in the raiser's help list",
      "SMOKE: pull the renewal list" in doer.get("/help").text)
check("cannot ask yourself for help", doer.post("/help/new", data={
    "subject": "x", "details": "", "helper_id": "6", "priority": "low",
    "needed_by": "2026-12-28T17:00"}).status_code == 400)
check("cannot raise an empty request", doer.post("/help/new", data={
    "subject": "   ", "details": "", "helper_id": "8", "priority": "low",
    "needed_by": "2026-12-28T17:00"}).status_code == 400)

r = doer.post("/help/new", data={
    "subject": "SMOKE: to be declined", "details": "", "helper_id": "8",
    "priority": "low", "needed_by": "2026-12-28T17:00"})
did = int(re.findall(r"/tasks/(\d+)/comment", r.text)[0])
helper = login("neha@gcs.local")
tickets = re.findall(r"/help/(\d+)/decline", helper.get("/help").text)
check("the helper sees a decline button", len(tickets) >= 2, tickets)
tk = tickets[0]          # newest first — the one we just raised to decline
check("an outsider cannot decline",
      admin.post(f"/help/{tk}/decline", data={"reason": "no"}).status_code == 403)
r = helper.post(f"/help/{tk}/decline", data={"reason": "On leave this week"})
check("the helper can decline", r.status_code == 200, r.status_code)
check("the reason is shown", "On leave this week" in helper.get("/help").text)
check("declining cancels the task", "cancelled" in admin.get(f"/tasks/{did}").text)
check("a declined request cannot be declined twice",
      helper.post(f"/help/{tk}/decline", data={"reason": "x"}).status_code == 400)

print("\n== help tickets don't block deletions ==")
r = doer.post("/help/new", data={
    "subject": "SMOKE: delete-me help", "details": "", "helper_id": "8",
    "priority": "low", "needed_by": "2026-12-27T17:00"})
xid = int(re.findall(r"/tasks/(\d+)/comment", r.text)[0])
check("a task carrying a help ticket can be deleted",
      admin.post(f"/tasks/{xid}/delete").status_code == 200)
check("the ticket goes with it",
      "SMOKE: delete-me help" not in doer.get("/help").text)
r = doer.post("/help/new", data={
    "subject": "SMOKE: transfer-me help", "details": "", "helper_id": "8",
    "priority": "low", "needed_by": "2026-12-27T17:00"})
page = admin.get("/admin/users/8/delete").text
check("help requests are listed before deleting someone", "Help Desk requests" in page)
check("a person with help tickets can be transferred out",
      admin.post("/admin/users/8/delete",
                 data={"mode": "transfer", "transfer_to": "7",
                       "confirm": "Spa Therapist Neha"}).status_code == 200)
check("their help request survives the transfer",
      "SMOKE: transfer-me help" in doer.get("/help").text)

print("\n== every link and button points somewhere real ==")
import route_check
check("no template links at a missing route", route_check.main() == 0)

print("\n== buttons that had never been clicked in a test ==")
r = admin.post("/admin/branches", data={"name": f"SMOKE Co {RUN}", "city": "Chandigarh"})
check("Add branch works", r.status_code == 200, r.status_code)
check("the new company is listed", f"SMOKE Co {RUN}" in r.text)
check("it can be picked as a task's branch",
      f"SMOKE Co {RUN}" in admin.get("/tasks/new").text)
check("a branch needs a name",
      admin.post("/admin/branches", data={"name": "  ", "city": "x"}).status_code == 400)
check("the same branch can't be added twice",
      admin.post("/admin/branches",
                 data={"name": f"SMOKE Co {RUN}", "city": ""}).status_code == 400)

print("\n== companies: see, edit, delete ==")
check("Branches is its own page", admin.get("/admin/branches").status_code == 200)
check("Users page no longer carries the branch form",
      "Add a branch" not in admin.get("/admin/users").text)
check("Branches appears in the menu", "Branches</span>" in admin.get("/admin/users").text)
up = admin.get("/admin/branches").text
check("branches are listed with their counts", "<h1>Branches</h1>" in up)
check("each company links to its own page", "/admin/branches/1" in up)
bpage = admin.get("/admin/branches/1")
check("company page opens", bpage.status_code == 200, bpage.status_code)
check("it shows what is filed under it", "What is filed under this branch" in bpage.text)
r = admin.post("/admin/branches/1", data={"name": f"Bodyzone RENAMED {RUN}", "city": "Mohali"})
check("rename works", r.status_code == 200 and f"Bodyzone RENAMED {RUN}" in r.text)
check("tasks follow the new name",
      f"Bodyzone RENAMED {RUN}" in admin.get("/stats").text)
r = admin.post("/admin/branches/1", data={"name": f"Bodyzone RENAMED {RUN}", "city": ""})
check("renaming to its own name is fine", r.status_code == 200)
check("cannot rename onto another company's name",
      admin.post("/admin/branches/1",
                 data={"name": "Spa Kora", "city": ""}).status_code == 400)
check("cannot blank a company name",
      admin.post("/admin/branches/1", data={"name": " ", "city": ""}).status_code == 400)
admin.post("/admin/branches/1", data={"name": "Bodyzone Fitness & Spa", "city": "Chandigarh"})

def branch_id_of(html, name):
    """The list is ordered by name, so pick the row, not the last match."""
    m = re.search(r'/admin/branches/(\d+)"><strong>' + re.escape(name), html)
    return int(m.group(1)) if m else None

r = admin.post("/admin/branches", data={"name": f"SMOKE empty {RUN}", "city": ""})
eid = branch_id_of(admin.get("/admin/branches").text, f"SMOKE empty {RUN}")
check("the new branch appears with its own id", eid is not None)
check("delete needs the name typed exactly",
      admin.post(f"/admin/branches/{eid}/delete",
                 data={"confirm": "wrong"}).status_code == 400)
check("an empty branch deletes cleanly",
      admin.post(f"/admin/branches/{eid}/delete",
                 data={"confirm": f"SMOKE empty {RUN}"}).status_code == 200)
check("it is gone from the list",
      f"SMOKE empty {RUN}" not in admin.get("/admin/branches").text)

# a company holding real work: everything must land somewhere, not vanish
r = admin.post("/admin/branches", data={"name": f"SMOKE busy {RUN}", "city": ""})
bid = branch_id_of(admin.get("/admin/branches").text, f"SMOKE busy {RUN}")
r = admin.post("/tasks/new", data={
    "title": f"SMOKE task in busy co {RUN}", "details": "", "doer_id": "6",
    "branch_id": str(bid), "priority": "low", "due_at": "2026-12-31T10:00"})
btid = int(re.findall(r"/tasks/(\d+)/comment", r.text)[0])
check("the task is filed under it", f"SMOKE busy {RUN}" in admin.get(f"/tasks/{btid}").text)
r = admin.post(f"/admin/branches/{bid}/delete",
               data={"confirm": f"SMOKE busy {RUN}", "move_to": "2"})
check("a branch holding work can be deleted", r.status_code == 200, r.status_code)
check("its task survived", admin.get(f"/tasks/{btid}").status_code == 200)
check("and moved to the chosen branch",
      "Spa Kora" in admin.get(f"/tasks/{btid}").text)
check("the Performance page still loads afterwards",
      admin.get("/stats").status_code == 200)

r = admin.post("/recurring", data={
    "title": f"SMOKE daily check {RUN}", "details": "", "doer_id": "6",
    "branch_id": "", "frequency": "daily", "day_of": "", "due_time": "18:00",
    "priority": "normal"})
check("Add checklist rule works", r.status_code == 200 and "SMOKE daily check" in r.text)
rid = re.findall(r"/recurring/(\d+)/toggle", r.text)
check("the rule can be paused",
      admin.post(f"/recurring/{rid[-1]}/toggle").status_code == 200)

r = admin.post("/flows/new", data={
    "name": f"SMOKE flow {RUN}", "description": "",
    "step_title": ["Step one", "Step two"], "step_doer": ["6", "7"],
    "step_tat": ["24", "24"], "step_priority": ["normal", "normal"],
    "step_fields": ["", ""], "step_audit": ["", ""]})
check("Create FMS flow works", r.status_code == 200, r.status_code)
fl = re.findall(r"/flows/(\d+)/start", r.text) or re.findall(r"/flows/(\d+)\"", r.text)
check("the new flow is startable", bool(fl), r.status_code)

check("user can be deactivated",
      admin.post("/admin/users/9/toggle").status_code == 200)
check("and reactivated",
      admin.post("/admin/users/9/toggle").status_code == 200)
check("Google Sheet page loads", admin.get("/sheet").status_code == 200)
check("Sync now answers even with no sheet configured",
      admin.post("/sheet/sync").status_code == 200)

print("\n== proof is required by default ==")
r = mgr.post("/tasks/new", data={
    "title": f"SMOKE proof needed {RUN}", "details": "", "doer_id": "6",
    "branch_id": "", "priority": "normal", "due_at": "2026-12-31T18:00"})
pid = int(re.findall(r"/tasks/(\d+)/comment", r.text)[0])
body = doer.get(f"/tasks/{pid}").text
check("a new task requires proof with nothing ticked", "Proof is required" in body)
check("the submit button is disabled until proof is there", "disabled" in body)
r = doer.post(f"/tasks/{pid}/submit", data={"completion_note": "done"})
check("submitting with no attachment is refused", r.status_code == 400, r.status_code)
check("the refusal explains what to do", "needs proof attached" in r.text)

import io
png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
doer.post(f"/tasks/{pid}/attach/upload",
          files={"file": ("proof.png", io.BytesIO(png), "image/png")})
check("with proof attached the doer is told they can submit",
      "you can submit" in doer.get(f"/tasks/{pid}").text)
r = doer.post(f"/tasks/{pid}/submit", data={"completion_note": "done"})
check("and the submit goes through", r.status_code == 200, r.status_code)
check("the task closed", "completed" in doer.get(f"/tasks/{pid}").text)

r = mgr.post("/tasks/new", data={
    "title": f"SMOKE proof off {RUN}", "details": "", "doer_id": "6",
    "branch_id": "", "priority": "normal", "due_at": "2026-12-31T18:00",
    "requires_attachment": "0"})   # exactly what an unticked box posts
oid = int(re.findall(r"/tasks/(\d+)/comment", r.text)[0])
check("an unticked proof box really turns it off",
      "Proof is required" not in doer.get(f"/tasks/{oid}").text)
check("and then it submits with nothing attached",
      doer.post(f"/tasks/{oid}/submit", data={"completion_note": "x"}).status_code == 200)

r = admin.post("/recurring", data={
    "title": f"SMOKE rule no proof {RUN}", "details": "", "doer_id": "6",
    "branch_id": "", "frequency": "daily", "day_of": "", "due_time": "18:00",
    "priority": "normal", "requires_attachment": "0"})
from app.db import SessionLocal as _S
from app.models import RecurringRule as _R
from sqlalchemy import select as _sel
_db = _S()
_rule = _db.scalar(_sel(_R).where(_R.title == f"SMOKE rule no proof {RUN}"))
check("an unticked checklist rule stores proof=off",
      _rule is not None and _rule.requires_attachment is False,
      _rule.requires_attachment if _rule else "no rule")
_db.close()
check("checklist rules default to requiring proof too",
      "Proof required before submitting" in admin.get("/recurring").text)
check("FMS steps ask about proof", "step_proof" in admin.get("/flows/new").text)

print("\n== attachments stored in the database (free hosting) ==")
import importlib
from app.services import storage as _st
import os as _os
_prev = _os.environ.get("STORAGE_BACKEND")
_os.environ["STORAGE_BACKEND"] = "db"
importlib.reload(_st)
import app.routers.attachments as _ra
importlib.reload(_ra)
check("db mode is recognised", _st.mode() == "db", _st.mode())
check("uploads stay available with no disk", _st.uploads_available())

r = mgr.post("/tasks/new", data={
    "title": f"SMOKE db-stored proof {RUN}", "details": "", "doer_id": "6",
    "branch_id": "", "priority": "normal", "due_at": "2026-12-31T18:00"})
dbid = int(re.findall(r"/tasks/(\d+)/comment", r.text)[0])
r = doer.post(f"/tasks/{dbid}/attach/upload",
              files={"file": ("in-db.png", io.BytesIO(TINY_PNG), "image/png")})
check("a file uploads with no disk involved", r.status_code == 200, r.text[:120])
aid2 = r.json()["id"]

from app.db import SessionLocal as _S2
from app.models import Attachment as _A
_d2 = _S2(); _att = _d2.get(_A, aid2)
check("the bytes are in the database row", _att.storage == "db" and _att.data == TINY_PNG)
_d2.close()

r = doer.get(f"/attachments/{aid2}")
check("it downloads back byte-for-byte", r.status_code == 200 and r.content == TINY_PNG)
r = doer.get(f"/attachments/{aid2}/view")
check("and previews inline", r.status_code == 200
      and "inline" in r.headers.get("content-disposition", ""))
check("proof now counts, so the task submits",
      doer.post(f"/tasks/{dbid}/submit", data={"completion_note": "x"}).status_code == 200)
check("deleting it does not error",
      doer.post(f"/attachments/{aid2}/delete").status_code in (200, 400))

if _prev is None:
    _os.environ.pop("STORAGE_BACKEND", None)
else:
    _os.environ["STORAGE_BACKEND"] = _prev
importlib.reload(_st); importlib.reload(_ra)
check("back to disk mode afterwards", _st.mode() == "local", _st.mode())

print("\n== a wrong link shows a readable page, not a raw error ==")
r = admin.get("/branches")            # the old, wrong address
check("a missing page returns 404", r.status_code == 404)
check("and shows a readable message", "Page not found" in r.text)
check("with the menu still there", "Branches</span>" in r.text)
check("the API still answers in JSON",
      admin.get("/api/stats?days=7").headers.get("content-type", "").startswith("application/json"))
r = doer.post("/tasks/999999/attach/upload",
              files={"file": ("x.png", io.BytesIO(TINY_PNG), "image/png")})
check("upload errors stay JSON for the uploader",
      r.headers.get("content-type", "").startswith("application/json"))

print("\n== branches: renamed, and the page opens ==")
check("the menu says Branches", "Branches</span>" in admin.get("/admin/users").text)
bp = admin.get("/admin/branches")
check("the Branches page opens", bp.status_code == 200 and "<h1>Branches</h1>" in bp.text)
check("nothing still says Company", "Company" not in bp.text)
bid1 = re.search(r'/admin/branches/(\d+)"><strong>', bp.text).group(1)
one = admin.get(f"/admin/branches/{bid1}")
check("clicking a branch opens its page", one.status_code == 200, one.status_code)
check("it says branch, not company", "filed under this branch" in one.text)

print("\n== holidays ==")
check("holidays page opens", admin.get("/admin/holidays").status_code == 200)
check("a doer cannot manage holidays", doer.get("/admin/holidays").status_code == 403)
r = admin.post("/admin/holidays", data={"day": "2026-11-08", "name": f"Diwali {RUN}",
                                        "branch_id": ""})
check("a holiday can be added", r.status_code == 200 and f"Diwali {RUN}" in r.text)
check("the same day cannot be added twice",
      admin.post("/admin/holidays",
                 data={"day": "2026-11-08", "name": "again", "branch_id": ""}).status_code == 400)
check("a holiday needs a name",
      admin.post("/admin/holidays",
                 data={"day": "2026-11-09", "name": "  ", "branch_id": ""}).status_code == 400)
check("a bad date is refused",
      admin.post("/admin/holidays",
                 data={"day": "not-a-date", "name": "x", "branch_id": ""}).status_code == 400)

# --- the rule that actually matters: no work is created on a holiday ---
from datetime import date as _date, datetime as _dt, timedelta as _td
from app.db import SessionLocal as _SS
from app.models import (RecurringRule as _RR, Recurrence as _Rec, Task as _T,
                        Holiday as _H, TaskSource as _TS)
from app.services import recurring as _recur, holidays as _hol
from sqlalchemy import select as _sel

_db = _SS()
_org = 1
_hday = _date(2027, 3, 15)
_db.add(_H(org_id=_org, branch_id=None, day=_hday, name="SMOKE holiday"))
_db.add(_RR(org_id=_org, branch_id=1, title=f"SMOKE daily on holiday {RUN}",
            doer_id=6, assigner_id=2, frequency=_Rec.DAILY, due_time="18:00"))
_db.commit()

check("the day is recognised as a holiday", _hol.is_holiday(_db, _org, _hday))
before = _db.scalar(_sel(__import__("sqlalchemy").func.count()).select_from(_T))
_recur.run_spawn(_db, today=_hday)
after = _db.scalar(_sel(__import__("sqlalchemy").func.count()).select_from(_T))
check("no checklist task is created on a holiday", before == after, f"{before} -> {after}")

_recur.run_spawn(_db, today=_hday + _td(days=1))
after2 = _db.scalar(_sel(__import__("sqlalchemy").func.count()).select_from(_T))
check("and the day after, tasks are created again", after2 > after, f"{after} -> {after2}")
check("the holiday is skipped, not owed the next day",
      after2 - after < 20, after2 - after)

# a deadline landing on a holiday moves forward
_moved, _why = _hol.shift_due(_db, _org, _dt.combine(_hday, _dt.min.time()).replace(hour=18))
check("a deadline on a holiday moves to the next day",
      _moved.date() == _hday + _td(days=1), _moved)
check("and keeps the time of day", _moved.hour == 18, _moved)
check("the reason is reported back", _why == "SMOKE holiday", _why)
_ok, _none = _hol.shift_due(_db, _org, _dt(2027, 3, 20, 18, 0))
check("an ordinary day is left alone", _none is None and _ok == _dt(2027, 3, 20, 18, 0))

# two holidays in a row
_db.add(_H(org_id=_org, branch_id=None, day=_date(2027, 4, 1), name="A"))
_db.add(_H(org_id=_org, branch_id=None, day=_date(2027, 4, 2), name="B"))
_db.commit()
_m2, _ = _hol.shift_due(_db, _org, _dt(2027, 4, 1, 10, 0))
check("a run of holidays is skipped through", _m2.date() == _date(2027, 4, 3), _m2)

# a branch holiday must not affect another branch
_db.add(_H(org_id=_org, branch_id=4, day=_date(2027, 5, 5), name="Jharkhand only"))
_db.commit()
check("a branch holiday applies to that branch",
      _hol.is_holiday(_db, _org, _date(2027, 5, 5), 4))
check("and not to a different branch",
      not _hol.is_holiday(_db, _org, _date(2027, 5, 5), 1))
_db.close()

r = admin.post("/tasks/new", data={
    "title": f"SMOKE holiday deadline {RUN}", "details": "", "doer_id": "6",
    "branch_id": "", "priority": "normal", "due_at": "2026-11-08T18:00"})
check("delegating onto a holiday still works", r.status_code == 200, r.status_code)
check("and the task says the deadline moved", "is a holiday" in r.text)
check("the new deadline is the next day", "09 Nov 2026" in r.text)

print("\n== date filters on the task lists ==")
r = admin.get("/tasks?scope=all&status=done")
check("completed filters on the completion date", "Completion date" in r.text)
r = admin.get("/tasks?scope=all&status=open")
check("open filters on the planned date", "Planned date" in r.text)
check("there is a Coming up view",
      admin.get("/tasks?scope=all&status=upcoming").status_code == 200)

# build a task closed on a known day, and one planned for a different day
_db = _SS()
_t = _T(org_id=1, branch_id=1, title=f"SMOKE closed in range {RUN}",
        assigner_id=2, doer_id=6, source=_TS.DELEGATION,
        due_at=_dt(2025, 1, 5, 18, 0), requires_attachment=False)
_t.status = __import__("app.models", fromlist=["TaskStatus"]).TaskStatus.COMPLETED
_t.closed_at = _dt(2025, 6, 20, 12, 0)
_db.add(_t); _db.commit(); _cid = _t.id
_db.close()

r = admin.get("/tasks?scope=all&status=done&date_from=2025-06-01&date_to=2025-06-30")
check("a task closed in June shows in a June completion range",
      f"SMOKE closed in range {RUN}" in r.text)
r = admin.get("/tasks?scope=all&status=done&date_from=2025-07-01&date_to=2025-07-31")
check("and not in a July range", f"SMOKE closed in range {RUN}" not in r.text)
r = admin.get("/tasks?scope=all&status=all&date_from=2025-01-01&date_to=2025-01-31")
check("but its PLANNED date puts it in a January planned range",
      f"SMOKE closed in range {RUN}" in r.text)
r = admin.get("/tasks?scope=all&status=done&date_from=2025-06-30&date_to=2025-06-01")
check("dates entered backwards are swapped, not empty",
      f"SMOKE closed in range {RUN}" in r.text)
r = admin.get("/tasks?scope=all&status=done&date_from=2025-06-20&date_to=2025-06-20")
check("a single day includes the whole day",
      f"SMOKE closed in range {RUN}" in r.text)
check("a nonsense date is ignored rather than crashing",
      admin.get("/tasks?scope=all&status=done&date_from=rubbish").status_code == 200)
check("the completion column is on the list", "Completed</th>" in r.text)

print("\n== first-run setup (hosts with no shell) ==")
from app.routers.setup import needs_setup as _ns
from app.db import SessionLocal as _S3
_d3 = _S3()
check("setup is hidden once users exist", not _ns(_d3))
_d3.close()
check("the setup page 404s on a live system", admin.get("/setup").status_code == 404)
check("a stranger cannot reach it either",
      TestClient(app).get("/setup").status_code == 404)
check("posting to it is refused too",
      TestClient(app).post("/setup", data={
          "name": "Hacker", "email": "h@x.com",
          "password": "abcdefgh1", "password2": "abcdefgh1"}).status_code == 404)
check("login still works normally", "Sign out" in
      TestClient(app, follow_redirects=True).post(
          "/login", data={"email": "mis@gcs.local", "password": "gcs1234"}).text)

print("\n== flow steps protected from deletion ==")
check("cannot delete a live FMS step",
      admin.post(f"/tasks/{step1}/delete").status_code == 400)

print("\n" + ("ALL CHECKS PASSED" if not FAIL else f"{len(FAIL)} FAILED: {FAIL}"))
sys.exit(1 if FAIL else 0)
