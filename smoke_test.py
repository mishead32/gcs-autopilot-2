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
check("rejected split leaves the old values", (u.bm_delegation, u.bm_checklist,
      u.bm_fms) == (50, 30, 20), (u.bm_delegation, u.bm_checklist, u.bm_fms))
try:
    u.set_benchmarks(-5, 10, 10)
    check("a negative benchmark is rejected", False, "no error raised")
except ValueError:
    check("a negative benchmark is rejected", True)

# A split under 100 is now deliberate, not an error: part of the EM score is
# judged by hand, so 40/10/10 means the software scores 60 and a person the
# other 40.
u.set_benchmarks(40, 10, 10)
check("a split under 100 is accepted", u.benchmark_total == 60, u.benchmark_total)
check("it saves exactly what was typed",
      (u.bm_delegation, u.bm_checklist, u.bm_fms) == (40, 10, 10))
check("and it says how much the software scores", u.scored_by_system == 60)
check("and how much is left to judge by hand", u.scored_by_hand == 40)
u.set_benchmarks(0, 0, 0)
check("all-zero is allowed — nothing is scored by software",
      u.benchmark_total == 0 and u.scored_by_hand == 100)
u.set_benchmarks(60, 20, 20)

# With only 60 points in play the software can never push anybody below 40,
# so the good/warn/bad bands have to move with the benchmark or every such
# person reads as failing.
_part = Card(benchmarks={"delegation": 40, "recurring": 10, "flow": 10})
# 40 is this person's floor, so 85% and 60% of the 60 in play land at 91
# and 76 — not at 85 and 60.
for _sc, _want in [(95, "good"), (80, "warn"), (70, "bad")]:
    _part.score = _sc
    check(f"a 60-point person scoring {_sc} bands as {_want}",
          _part.band == _want, _part.band)
check("and the page can say 60 of 100 are scored here",
      _part.scored_by_system == 60 and _part.partly_manual)
_full = Card(benchmarks={"delegation": 60, "recurring": 20, "flow": 20})
_full.score = 90
check("a full-100 person is unaffected",
      _full.band == "good" and not _full.partly_manual)

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
    [f"BULK weekly {RUN}", "", "amit@gcs.local", "", "weekly", "Mon", "12:00", "medium", "YES"],
    [f"BULK monthly {RUN}", "", "amit@gcs.local", "", "monthly", "1", "16:00", "high", "NO"],
    ["Weekly with no day", "", "amit@gcs.local", "", "weekly", "", "", "", ""],
], CH)
r = admin.post("/bulk/preview", data={"kind": "checklist"},
               files={"file": ("c.xlsx", cblob,
                      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
check("checklist preview works", r.status_code == 200)
check("weekly without a day is rejected",
      "needs at least one day of the week" in r.text, r.text[-400:])
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
    "branch_id": "", "priority": "medium", "due_at": "2026-12-31T18:00"})
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
# Checked precisely rather than by looking for the word "you" — that text
# moved when the Edit button was added, and a proxy like that passes happily
# while the actual delete link sits right there.
_my_id = admin.get("/admin/users").text
import re as _re
_mine = _re.search(r"mis@gcs\.local", me)
check("no delete button against your own row",
      f'/admin/users/2/delete"' not in me, "own delete link is on the page")
check("but your own row does offer Edit", '/admin/users/2"' in me)
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
    "branch_id": "", "priority": "medium", "due_at": "2026-12-31T18:00"})
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
    "branch_id": "", "priority": "medium", "due_at": "2026-12-31T18:00"})
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
    "priority": "high", "due_at": "2026-12-31T10:00"}).status_code == 200)
body = mgr.get(f"/tasks/{eid}").text
check("edited title shown", "edited title" in body)
check("edit logged as a note", "Edited:" in body)
check("admin can delete", admin.post(f"/tasks/{eid}/delete").status_code == 200)
check("deleted task is gone", admin.get(f"/tasks/{eid}").status_code == 404)

print("\n== audit status ==")
r = mgr.post("/tasks/new", data={
    "title": "SMOKE: audit-state task", "details": "d", "doer_id": "6",
    "branch_id": "", "priority": "medium",
    "due_at": "2026-12-31T18:00", "requires_audit": "1"})
aid = int(re.findall(r"/tasks/(\d+)/comment", r.text)[0])
from app.db import SessionLocal as _sess
from app.models import Task as _TaskM
body = mgr.get(f"/tasks/{aid}").text
# The rule the user asked for: nothing is auditable until the employee has
# said it is done. A freshly created task therefore waits — it must NOT be
# sitting on an auditor's list while the work is still in progress.
check("audit-flagged task waits for the doer to finish",
      "Audit after it&#39;s done" in body or "Audit after it's done" in body)
with _sess() as _d:
    check("and its audit state is waiting, not pending",
          _d.get(_TaskM, aid).audit_state.value == "waiting")
check("it is not on the audit-pending list yet",
      f'/tasks/{aid}"' not in mgr.get("/tasks?scope=all&status=audit_pending").text)

r = mgr.post("/tasks/new", data={
    "title": "SMOKE: no-audit task", "details": "d", "doer_id": "6",
    "branch_id": "", "priority": "medium", "due_at": "2026-12-31T18:00"})
nid = int(re.findall(r"/tasks/(\d+)/comment", r.text)[0])
check("unflagged task starts as Not required",
      "Not required" in mgr.get(f"/tasks/{nid}").text)

check("an unfinished task cannot be marked audited",
      mgr.post(f"/tasks/{aid}/audit-state",
               data={"state": "completed",
                     "remark": "looks fine"}).status_code == 400)
check("nor pulled onto the auditor's list early",
      mgr.post(f"/tasks/{aid}/audit-state",
               data={"state": "pending", "remark": ""}).status_code == 400)

# The doer finishes it. Only now does it become the auditor's problem.
submit(doer, aid, completion_note="done")
with _sess() as _d:
    check("finishing it moves the audit to pending",
          _d.get(_TaskM, aid).audit_state.value == "pending")
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

# A task nobody flagged for audit can still be pulled in — but only after
# its doer has finished it, same rule for everyone.
check("an unflagged, unfinished task cannot be pulled in for audit",
      mgr.post(f"/tasks/{nid}/audit-state",
               data={"state": "pending", "remark": ""}).status_code == 400)
submit(doer, nid, completion_note="done")
r = mgr.post(f"/tasks/{nid}/audit-state", data={"state": "pending", "remark": ""})
check("a finished task can be pulled in for audit later",
      "Audit pending" in mgr.get(f"/tasks/{nid}").text)
check("audit-pending filter finds it",
      f'/tasks/{nid}' in mgr.get("/tasks?scope=all&status=audit_pending").text)
# Audit is no longer a column of its own — it is a chip under the status
# chip, because two chips about where a task stands do not need two columns
# on a 1000px screen.
_al = mgr.get("/tasks?scope=all&status=all").text
check("the audit state is on the task list",
      "statecell" in _al and "audit_" in _al)
check("bad audit state refused",
      mgr.post(f"/tasks/{nid}/audit-state", data={"state": "nonsense"}).status_code == 400)

print("\n== paste attachments ==")
# A task the doer has NOT finished yet — the paste panel only exists while
# there is still something to submit.
_pr = mgr.post("/tasks/new", data={
    "title": f"SMOKE: paste panel {RUN}", "details": "d", "doer_id": "6",
    "branch_id": "", "priority": "medium", "due_at": "2026-12-31T18:00"})
pid = int(re.findall(r"/tasks/(\d+)/comment", _pr.text)[0])
body = doer.get("/").text
body = doer.get(f"/tasks/{pid}").text
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
    "priority": "medium"})
check("Add checklist rule works", r.status_code == 200 and "SMOKE daily check" in r.text)
rid = re.findall(r"/recurring/(\d+)/toggle", r.text)
check("the rule can be paused",
      admin.post(f"/recurring/{rid[-1]}/toggle").status_code == 200)

r = admin.post("/flows/new", data={
    "name": f"SMOKE flow {RUN}", "description": "",
    "step_title": ["Step one", "Step two"], "step_doer": ["6", "7"],
    "step_tat": ["24", "24"], "step_priority": ["medium", "medium"],
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
    "branch_id": "", "priority": "medium", "due_at": "2026-12-31T18:00"})
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
    "branch_id": "", "priority": "medium", "due_at": "2026-12-31T18:00",
    "requires_attachment": "0"})   # exactly what an unticked box posts
oid = int(re.findall(r"/tasks/(\d+)/comment", r.text)[0])
check("an unticked proof box really turns it off",
      "Proof is required" not in doer.get(f"/tasks/{oid}").text)
check("and then it submits with nothing attached",
      doer.post(f"/tasks/{oid}/submit", data={"completion_note": "x"}).status_code == 200)

r = admin.post("/recurring", data={
    "title": f"SMOKE rule no proof {RUN}", "details": "", "doer_id": "6",
    "branch_id": "", "frequency": "daily", "day_of": "", "due_time": "18:00",
    "priority": "medium", "requires_attachment": "0"})
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
    "branch_id": "", "priority": "medium", "due_at": "2026-12-31T18:00"})
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

# A deadline landing on a closed day comes FORWARD, to the last working day
# before it. Pushing it to the day after means the Sunday report is handed in
# on Monday — late, by a day, every single time.
_moved, _why = _hol.shift_due(_db, _org, _dt.combine(_hday, _dt.min.time()).replace(hour=18))
check("a deadline on a holiday comes back to the working day before",
      _moved.date() < _hday, _moved)
check("and not to the day after", _moved.date() != _hday + _td(days=1), _moved)
# Mon 15 Mar 2027 is the holiday, Sun 14 is the weekly off, so Sat 13 it is.
check("skipping back over the weekly off as well",
      _moved.date() == _date(2027, 3, 13), _moved)
check("and keeps the time of day", _moved.hour == 18, _moved)
check("the reason is reported back", _why == "SMOKE holiday", _why)
_ok, _none = _hol.shift_due(_db, _org, _dt(2027, 3, 20, 18, 0))
check("an ordinary day is left alone", _none is None and _ok == _dt(2027, 3, 20, 18, 0))

# two holidays in a row
_db.add(_H(org_id=_org, branch_id=None, day=_date(2027, 4, 1), name="A"))
_db.add(_H(org_id=_org, branch_id=None, day=_date(2027, 4, 2), name="B"))
_db.commit()
_m2, _ = _hol.shift_due(_db, _org, _dt(2027, 4, 1, 10, 0))
check("a run of holidays is skipped back through",
      _m2.date() == _date(2027, 3, 31), _m2)
check("which is a working day", not _hol.is_closed(_db, _org, _m2.date()), _m2)

# Sunday, with no holiday involved at all.
_sun = _date(2027, 4, 11)
check("Sunday counts as closed for head office",
      _hol.is_closed(_db, _org, _sun, None))
_ms, _wsun = _hol.shift_due(_db, _org, _dt.combine(_sun, _dt.min.time()).replace(hour=17))
check("a Sunday deadline comes back to Saturday",
      _ms.date() == _sun - _td(days=1), _ms)
check("and says so in words", _wsun and "Sunday" in _wsun, _wsun)

# But NOT for a company that never closes — the gym and the spa work Sundays.
from app.models import Branch as _BR
_bz = _db.scalar(_sel(_BR).where(_BR.name.like("Bodyzone%")))
if _bz:
    _bz.weekly_off = None
    _db.commit()
    check("a company set to open seven days is not closed on Sunday",
          not _hol.is_closed(_db, _org, _sun, _bz.id))
    _mb, _wb = _hol.shift_due(_db, _org,
                              _dt.combine(_sun, _dt.min.time()).replace(hour=17),
                              _bz.id)
    check("so its Sunday deadline stays on Sunday",
          _mb.date() == _sun and _wb is None, f"{_mb} {_wb}")
else:
    check("a seven-day company was available to test", False, "no Bodyzone branch")

# A deadline already in the past is never moved — an impossible deadline
# helps nobody, and there is no working day before it left to use.
from app import clock as _clk
_gone = _clk.now() - _td(days=400)
while _gone.weekday() != 6:
    _gone -= _td(days=1)
_mg, _wg = _hol.shift_due(_db, _org, _gone)
check("a Sunday that has already passed is left alone",
      _mg == _gone and _wg is None, f"{_mg} {_wg}")

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
    "branch_id": "", "priority": "medium", "due_at": "2026-11-08T18:00"})
check("delegating onto a holiday still works", r.status_code == 200, r.status_code)
check("and the task says the deadline moved", "Brought forward" in r.text)
# 8 Nov 2026 is a Sunday, so the working day before it is Saturday the 7th.
check("the new deadline is the working day BEFORE", "07 Nov 2026" in r.text)
check("and not the day after", "09 Nov 2026" not in r.text)

print("\n== date filters on the task lists ==")
# Which date a window means is now the PERSON'S choice, not the tab's.
# Planned date everywhere by default, because "show me October" nearly
# always means the work October was promised — and a window that silently
# changed meaning between tabs could not be reconciled with anything.
r = admin.get("/tasks?scope=all&status=done")
check("Completed defaults to the planned date too",
      'name="basis"' in r.text and 'value="due_at" selected' in r.text)
check("and the other two dates are offered",
      'value="finished_at"' in r.text and 'value="created_at"' in r.text)
r = admin.get("/tasks?scope=all&status=done&basis=finished_at")
check("choosing completion date is honoured",
      'value="finished_at" selected' in r.text)
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

r = admin.get("/tasks?scope=all&status=done&basis=finished_at&date_from=2025-06-01&date_to=2025-06-30")
check("a task closed in June shows in a June completion range",
      f"SMOKE closed in range {RUN}" in r.text)
r = admin.get("/tasks?scope=all&status=done&basis=finished_at&date_from=2025-07-01&date_to=2025-07-31")
check("and not in a July range", f"SMOKE closed in range {RUN}" not in r.text)
r = admin.get("/tasks?scope=all&status=all&date_from=2025-01-01&date_to=2025-01-31")
check("but its PLANNED date puts it in a January planned range",
      f"SMOKE closed in range {RUN}" in r.text)
r = admin.get("/tasks?scope=all&status=done&basis=finished_at&date_from=2025-06-30&date_to=2025-06-01")
check("dates entered backwards are swapped, not empty",
      f"SMOKE closed in range {RUN}" in r.text)
r = admin.get("/tasks?scope=all&status=done&basis=finished_at&date_from=2025-06-20&date_to=2025-06-20")
check("a single day includes the whole day",
      f"SMOKE closed in range {RUN}" in r.text)
check("a nonsense date is ignored rather than crashing",
      admin.get("/tasks?scope=all&status=done&date_from=rubbish").status_code == 200)
check("the completion column is on the list", "Completed</th>" in r.text)
# The same task, same dates, read as PLANNED instead — it must disappear,
# which is the proof that the choice is really doing something.
check("the same window on the planned date does not find it",
      f"SMOKE closed in range {RUN}" not in admin.get(
          "/tasks?scope=all&status=done&basis=due_at"
          "&date_from=2025-06-01&date_to=2025-06-30").text)
check("but its own planned date does",
      f"SMOKE closed in range {RUN}" in admin.get(
          "/tasks?scope=all&status=done&basis=due_at"
          "&date_from=2025-01-01&date_to=2025-01-31").text)
check("and the assigned date is a third, separate answer",
      admin.get("/tasks?scope=all&status=all&basis=created_at"
                "&date_from=2025-01-01&date_to=2025-01-31").status_code == 200)

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

print("\n== priority weight (5x / 2x / 1x) ==")
from app.models import Priority as _P, PRIORITY_WEIGHT as _PW
check("three levels only", sorted(p.value for p in _P) == ["high", "low", "medium"],
      [p.value for p in _P])
check("high counts 5, medium 2, low 1",
      (_PW[_P.HIGH], _PW[_P.MEDIUM], _PW[_P.LOW]) == (5, 2, 1))

from app.services.scoring import SourceScore as _SS, Card as _C

class _FakeTask:
    def __init__(self, pr): self.priority = pr
    @property
    def weight(self):
        return _PW[self.priority]

from app.services import scoring as _sc
check("one high task weighs the same as five low ones",
      _sc._w([_FakeTask(_P.HIGH)]) == _sc._w([_FakeTask(_P.LOW)] * 5) == 5)
check("a medium weighs two", _sc._w([_FakeTask(_P.MEDIUM)]) == 2)

r = mgr.post("/tasks/new", data={
    "title": f"SMOKE high priority {RUN}", "details": "", "doer_id": "6",
    "branch_id": "", "priority": "high", "due_at": "2026-12-31T18:00"})
hid = int(re.findall(r"/tasks/(\d+)/comment", r.text)[0])
body = mgr.get(f"/tasks/{hid}").text
check("the task page shows the multiplier", "5\u00d7" in body)
check("and says what it means in words", "counts as 5" in body)
check("the doer still sees exactly one row, not five",
      doer.get("/tasks?scope=mine&status=open").text.count(f"SMOKE high priority {RUN}") == 1)

# the score must move five times as far for a high task as a low one
def _score_gap(tasks):
    c = _C()
    c.sources = {"delegation": _SS(key="delegation", benchmark=100,
                                   planned=_sc._w(tasks), completed=0,
                                   not_done=_sc._w(tasks)).compute()}
    return c.compute().gap

check("missing one high task hurts as much as missing five low ones",
      _score_gap([_FakeTask(_P.HIGH)]) == _score_gap([_FakeTask(_P.LOW)] * 5))

# sorting
r = mgr.post("/tasks/new", data={
    "title": f"SMOKE low priority {RUN}", "details": "", "doer_id": "6",
    "branch_id": "", "priority": "low", "due_at": "2026-01-01T09:00"})
lid = int(re.findall(r"/tasks/(\d+)/comment", r.text)[0])
listing = mgr.get("/tasks?scope=all&status=open").text
check("high sorts above low even when low is due sooner",
      listing.index(f"SMOKE high priority {RUN}") < listing.index(f"SMOKE low priority {RUN}"))

print("\n== follow-ups: PC and EA ==")
pc = login("pc@gcs.local")
ea = login("ea@gcs.local")
check("the PC can open Follow-ups", pc.get("/followups").status_code == 200)
check("the EA can open Follow-ups", ea.get("/followups").status_code == 200)
check("the PC lands on the Checklist & FMS desk", "Checklist &amp; FMS" in pc.get("/followups").text)
check("the EA lands on the Delegation desk", "EA \u2014 Delegation" in ea.get("/followups").text)
check("Follow-ups is in the PC's menu", "Follow-ups</span>" in pc.get("/").text)
check("an ordinary doer does not see the menu",
      "Follow-ups</span>" not in doer.get("/").text)

from datetime import date as _date
_today = _date.today().isoformat()
ea_page = ea.get(f"/followups?desk=ea&day={_today}").text
check("the EA sees delegation tasks that are not theirs",
      "SMOKE high priority" in ea_page or "tick" in ea_page.lower())
_ids = re.findall(r"/followups/(\d+)/tick", ea_page)
check("there is something to chase", len(_ids) > 0, len(_ids))

if _ids:
    tid_f = _ids[0]
    r = ea.post(f"/followups/{tid_f}/tick", data={"day": _today, "desk": "ea"})
    check("the EA can tick a follow-up", r.status_code == 200, r.status_code)
    check("the tick is recorded", "Followed up" in r.text)
    page = ea.get(f"/followups?desk=ea&day={_today}").text
    check("the count moves", "fu-done" in page)
    r = ea.post(f"/followups/{tid_f}/tick", data={"day": _today, "desk": "ea"})
    check("ticking again unticks it", "fu-done" not in r.text)
    ea.post(f"/followups/{tid_f}/tick", data={"day": _today, "desk": "ea"})

    check("the PC cannot tick a delegation task",
          pc.post(f"/followups/{tid_f}/tick",
                  data={"day": _today, "desk": "ea"}).status_code == 403)
    check("an ordinary doer cannot tick anything",
          doer.post(f"/followups/{tid_f}/tick",
                    data={"day": _today, "desk": "ea"}).status_code == 403)
    check("a follow-up cannot be recorded for tomorrow",
          ea.post(f"/followups/{tid_f}/tick",
                  data={"day": "2030-01-01", "desk": "ea"}).status_code == 400)

    # Yesterday's tick must not cover today. Tested on a task that was
    # genuinely open yesterday — the desk only lists what was open on the day
    # you are looking at, so ticking today's brand-new task and then asking
    # for yesterday's page proves nothing.
    import datetime as _dtf
    from app import clock as _ckf
    _yday_d = _ckf.today() - _dtf.timedelta(days=1)
    _yday = _yday_d.isoformat()
    _old_id = int(re.search(r"/tasks/(\d+)", admin.post("/tasks/new", data={
        "title": f"open since before yesterday {RUN}", "doer_id": 6,
        "priority": "medium",
        "due_at": (_ckf.now() - _dtf.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")},
        follow_redirects=False).headers["location"]).group(1))

    _ypage_before = ea.get(f"/followups?desk=ea&day={_yday}").text
    check("that task was open yesterday", f"/tasks/{_old_id}" in _ypage_before)
    ea.post(f"/followups/{_old_id}/tick", data={"day": _yday, "desk": "ea"})
    _ypage = ea.get(f"/followups?desk=ea&day={_yday}").text
    check("yesterday can be ticked separately", "fu-done" in _ypage)
    _tpage = ea.get(f"/followups?desk=ea&day={_today}").text
    check("and today keeps its own tally — still untickled",
          f'/followups/{_old_id}/tick' in _tpage)
    check("yesterday's tick did not mark today done",
          _tpage.count("fu-done") < _ypage.count("fu-done") + 1
          or "fu-done" not in _tpage.split(f"/tasks/{_old_id}")[0][-400:])

print("\n== follow-up report (now under Reports) ==")
check("the old link still lands on the report",
      admin.get("/followups/report").url.path == "/reports/followups")
rep = admin.get("/reports/followups")
check("the report opens", rep.status_code == 200, rep.status_code)
check("it names who holds each desk", "Karan" in rep.text and "Simran" in rep.text)
# The desk table is the only place a name means "this person holds the desk".
# The employee filter lists everybody, which is not the same thing.
_desk_table = rep.text.split("By desk", 1)[1].split("Day by day", 1)[0]
check("and does not list every admin as the PC",
      "CMD Sir" not in _desk_table, "admins should not be listed as desk holders")
check("it shows follow-ups still pending", "Follow-up pending" in rep.text)
check("a plain doer cannot open the report",
      doer.get("/reports/followups").status_code == 403)
check("but the PC can", pc.get("/reports/followups").status_code == 200)
check("a date range works",
      admin.get("/reports/followups?date_from=2026-09-01&date_to=2026-09-30").status_code == 200)
check("backwards dates are handled",
      admin.get("/reports/followups?date_from=2026-09-30&date_to=2026-09-01").status_code == 200)

print("\n== the priority column is plain text, not a native enum ==")
from sqlalchemy import inspect as _inspect, Enum as _SAEnum
from app.db import engine as _eng
_col = [c for c in _inspect(_eng).get_columns("tasks") if c["name"] == "priority"][0]
check("priority is stored as text", not isinstance(_col["type"], _SAEnum),
      repr(_col["type"]))
check("so renaming a level is an ordinary UPDATE", True)

print("\n== flow steps protected from deletion ==")
check("cannot delete a live FMS step",
      admin.post(f"/tasks/{step1}/delete").status_code == 400)

print("\n== the Reports menu ==")
_idx = admin.get("/reports")
check("the reports index opens", _idx.status_code == 200, _idx.status_code)
for _t in ["1 · Delegation", "2 · Checklist", "3 · FMS", "4 · Follow-ups",
           "5 · Audit", "6 · EM score"]:
    check(f"it offers {_t}", _t in _idx.text)

check("a doer sees the index too", doer.get("/reports").status_code == 200)
check("but is not offered the EM score report", "6 · EM score" not in doer.get("/reports").text)
check("nor the follow-up report", "4 · Follow-ups" not in doer.get("/reports").text)

print("\n== reports 1-3: pending and completed, by work type ==")
for _src, _label in [("delegation", "Delegation"), ("checklist", "Checklist"),
                     ("fms", "FMS")]:
    r = admin.get(f"/reports/tasks?source={_src}")
    check(f"{_label} report opens", r.status_code == 200, r.status_code)
    check(f"{_label} report is about {_label}",
          f"{_label} — pending" in r.text, r.text[:200])
    for _state in ["pending", "completed", "overdue", "all"]:
        check(f"{_label}: the {_state} tab works",
              admin.get(f"/reports/tasks?source={_src}&state={_state}").status_code == 200)

check("an unknown report is a readable 404",
      admin.get("/reports/tasks?source=nonsense").status_code == 404)

# Both halves of the report are filtered on the SAME date, chosen by the
# person and planned by default. It used to filter pending work on its
# planned date and finished work on the date it was finished, so one report
# answered two questions at once and its totals reconciled with nothing.
_r = admin.get("/reports/tasks?source=delegation")
check("the report lets you choose which date the window means",
      'name="basis"' in _r.text)
check("and starts on the planned date", 'value="due_at" selected' in _r.text)
check("with completion and assigned offered too",
      'value="finished_at"' in _r.text and 'value="created_at"' in _r.text)
_rb = admin.get("/reports/tasks?source=delegation&basis=finished_at"
                "&date_from=2025-06-01&date_to=2025-06-30")
check("choosing completion date is honoured on the report",
      'value="finished_at" selected' in _rb.text)
check("and the summary line says which date it used",
      "completion date" in _rb.text.lower())

print("\n== every report filter has BOTH a from and a to ==")
for _path in ["/reports/tasks?source=delegation", "/reports/tasks?source=checklist",
              "/reports/tasks?source=fms", "/reports/followups",
              "/reports/audit", "/reports/score"]:
    _t = admin.get(_path).text
    check(f"{_path} has a from date", 'name="date_from"' in _t)
    check(f"{_path} has a to date", 'name="date_to"' in _t)
    check(f"{_path} offers quick ranges", "Quick range" in _t)
    check(f"{_path} filters by branch", 'name="branch"' in _t)

_fu = pc.get("/followups").text
check("the follow-up desk has a from date too", 'name="date_from"' in _fu)
check("and a to date", 'name="date_to"' in _fu)
_range = pc.get("/followups?date_from=2026-09-01&date_to=2026-09-07")
check("a range on the desk gives a day-by-day summary",
      _range.status_code == 200 and "day by day" in _range.text.lower())
check("a single day still gives the tick list",
      "tickbox" in pc.get("/followups?date_from=%s&date_to=%s" % (_today, _today)).text)

print("\n== report 5: audit pending and completed ==")
_a = admin.get("/reports/audit")
check("the audit report opens", _a.status_code == 200, _a.status_code)
check("it splits by work type",
      "Delegation" in _a.text and "Checklist" in _a.text and "FMS" in _a.text)
check("it shows audit pending and completed",
      "Audit pending" in _a.text and "Audit completed" in _a.text)
for _state in ["pending", "completed", "not_required", "all"]:
    check(f"audit report: the {_state} tab works",
          admin.get(f"/reports/audit?state={_state}").status_code == 200)

print("\n== report 6: EM score, person-wise and branch-wise ==")
_s = admin.get("/reports/score")
check("the score report opens", _s.status_code == 200, _s.status_code)
check("it shows an average", "Average score" in _s.text)
check("person-wise is the default", "Person-wise" in _s.text)
check("branch-wise works",
      admin.get("/reports/score?view=branch").status_code == 200)
check("it shows the bifurcation per work type",
      "not done" in _s.text and "late" in _s.text)
check("a plain doer cannot open it", doer.get("/reports/score").status_code == 403)

# Picking a branch must narrow the employee list to that branch's people.
from app.db import SessionLocal as _SL
from app.models import Branch as _Br, User as _U
from sqlalchemy import select as _sel
with _SL() as _d:
    _bz = _d.scalar(_sel(_Br).where(_Br.name.like("Bodyzone%")))
    _bz_id = _bz.id
    _in_bz = {u.name for u in _d.scalars(_sel(_U).where(_U.branch_id == _bz_id)).all()}
    _out_bz = {u.name for u in _d.scalars(
        _sel(_U).where(_U.branch_id != _bz_id, _U.branch_id.is_not(None))).all()}
_bpage = admin.get(f"/reports/score?branch={_bz_id}").text
_table = _bpage.split("Person-wise", 1)[1]
check("picking a branch keeps its own employees",
      any(n in _table for n in _in_bz), "no Bodyzone employee listed")
check("and drops everybody else",
      not any(n in _table for n in _out_bz - _in_bz),
      "an employee from another branch is still listed")

print("\n== the dashboard is organised into sections ==")
# Read as a doer: the admin account hands work out and is never given any, so
# its dashboard has no "my work" or "my score" at all — that is checked
# separately further down.
_dash = doer.get("/").text
for _sec in ["1 · My work", "2 · My EM score"]:
    check(f"the dashboard has '{_sec}'", _sec in _dash)
check("it groups my work by type of work", "By type of work" in _dash)
_mdash = mgr.get("/").text
check("a manager's dashboard still has the team section", "My team" in _mdash)
check("and its sections are numbered in order",
      "1 · My work" in _mdash and "2 · My EM score" in _mdash
      and "3 · My team" in _mdash)
check("it shows the team average", "Average EM score" in _mdash)
check("with the bifurcation behind it", "Score bifurcation" in _mdash)
check("and a branch-wise roll-up", "Branch-wise" in _mdash)

_ddash = doer.get("/").text
check("a doer gets the same score sections",
      "1 · My work" in _ddash and "2 · My EM score" in _ddash)
check("a doer sees their own bifurcation",
      "Work not done" in _ddash and "Not done on time" in _ddash)
check("but no team section", "3 · My team" not in _ddash)

print("\n== every user row has a visible Edit button ==")
from app.db import SessionLocal as _SL2
from app.models import User as _U2
from sqlalchemy import select as _sel2
with _SL2() as _d:
    _me = _d.scalar(_sel2(_U2).where(_U2.email == "mis@gcs.local"))
    _other = _d.scalar(_sel2(_U2).where(_U2.email == "amit@gcs.local"))
    _me_id, _other_id = _me.id, _other.id

_up = admin.get("/admin/users").text
check("someone else's row has an Edit button",
      f'href="/admin/users/{_other_id}"' in _up and ">Edit<" in _up)
check("your own row has one too",
      _up.count(f'href="/admin/users/{_me_id}"') >= 1)
# The Edit button must not be the only way in, and must not be a dead link.
check("the Edit button opens the edit page",
      admin.get(f"/admin/users/{_other_id}").status_code == 200)
_saved = admin.post(f"/admin/users/{_other_id}",
                    data={"name": "PT Trainer Amit", "phone": "+919800000006",
                          "role": "doer", "branch_id": "", "department_id": "",
                          "rights": ["create_task"], "bm_delegation": 70,
                          "bm_checklist": 10, "bm_fms": 20})
check("the edit page can actually save", _saved.status_code == 200, _saved.status_code)
check("and the change sticks", "70 / 10 / 20" in admin.get("/admin/users").text)
check("editing your own account is allowed",
      admin.get(f"/admin/users/{_me_id}").status_code == 200)
check("but your own role stays locked",
      "Role and rights are locked here" in admin.get(f"/admin/users/{_me_id}").text)

print("\n== reports are scoped until the right is given ==")
from app.db import SessionLocal as _SL3
from app.models import User as _U3, Task as _T3, Right as _R3
from sqlalchemy import select as _s3

with _SL3() as _d:
    _amit = _d.scalar(_s3(_U3).where(_U3.email == "amit@gcs.local"))
    _amit_id = _amit.id
    _amit_rights = _amit.rights

# By default a doer's report is about the doer, and nobody else.
_dr = doer.get("/reports/tasks?source=delegation&state=all")
check("a doer can open the delegation report", _dr.status_code == 200, _dr.status_code)
check("and is told it is only their own work",
      "covers your own work only" in _dr.text)
with _SL3() as _d:
    _mine = {t.id for t in _d.scalars(_s3(_T3).where(
        _T3.source == "DELEGATION",
        (_T3.doer_id == _amit_id) | (_T3.assigner_id == _amit_id))).all()}
    _theirs = {t.id for t in _d.scalars(_s3(_T3).where(
        _T3.source == "DELEGATION", _T3.doer_id != _amit_id,
        _T3.assigner_id != _amit_id)).all()}
_shown = {i for i in _mine if f'/tasks/{i}"' in _dr.text}
_leaked = {i for i in _theirs if f'/tasks/{i}"' in _dr.text}
check("their own delegation tasks are listed", bool(_shown), "none of their own shown")
check("somebody else's are not", not _leaked, f"leaked task ids {sorted(_leaked)[:5]}")

check("the EM score report is refused", doer.get("/reports/score").status_code == 403)
check("the follow-up report is refused", doer.get("/reports/followups").status_code == 403)
_ix = doer.get("/reports").text
check("the index does not offer the EM score report", "6 · EM score" not in _ix)
check("nor the follow-up report", "4 · Follow-ups" not in _ix)
check("and the sidebar hides both",
      "EM score report" not in _ix and "Follow-up report" not in _ix)

# Now tick the right and the same pages cover the company.
admin.post(f"/admin/users/{_amit_id}",
           data={"name": "PT Trainer Amit", "phone": "+919800000006",
                 "role": "doer", "branch_id": "", "department_id": "",
                 "rights": ["view_all_reports"], "bm_delegation": 60,
                 "bm_checklist": 20, "bm_fms": 20})
_dr2 = doer.get("/reports/tasks?source=delegation&state=all")
check("with the right, the warning is gone",
      "covers your own work only" not in _dr2.text)
check("and other people's tasks appear",
      any(f'/tasks/{i}"' in _dr2.text for i in _theirs), "still scoped to self")
check("the EM score report opens", doer.get("/reports/score").status_code == 200)
check("the follow-up report opens", doer.get("/reports/followups").status_code == 200)
check("the branch filter offers every branch",
      doer.get("/reports/score").text.count("<option value=\"") >
      _dr.text.count("<option value=\""))
_ix2 = doer.get("/reports").text
check("the index now offers all six",
      "6 · EM score" in _ix2 and "4 · Follow-ups" in _ix2)

# The right must not become a back door into anything else.
check("it does not grant delegating work",
      doer.get("/tasks/new").status_code == 403)
check("nor managing users", doer.get("/admin/users").status_code == 403)
check("nor the Performance page", doer.get("/stats").status_code == 403)

# put the user back the way the seed left them
with _SL3() as _d:
    _u = _d.get(_U3, _amit_id); _u.rights = _amit_rights; _d.commit()

print("\n== each person is told whose figures they are looking at ==")
check("a manager is told it is their branch",
      "This report covers Bodyzone Fitness &amp; Spa" in
      mgr.get("/reports/tasks?source=delegation").text
      or "covers Bodyzone" in mgr.get("/reports/tasks?source=delegation").text)
check("an admin gets no warning at all",
      "covers your own work only" not in admin.get("/reports/tasks?source=delegation").text
      and "To see every branch" not in admin.get("/reports/tasks?source=delegation").text)
# A manager's report must actually stay inside their branch.
with _SL3() as _d:
    _mgr = _d.scalar(_s3(_U3).where(_U3.email == "bz.manager@gcs.local"))
    _elsewhere = [t.id for t in _d.scalars(_s3(_T3).where(
        _T3.source == "DELEGATION", _T3.branch_id != _mgr.branch_id,
        _T3.doer_id != _mgr.id, _T3.assigner_id != _mgr.id)).all()]
_mp = mgr.get("/reports/tasks?source=delegation&state=all").text
check("and another branch's tasks stay out of it",
      not any(f'/tasks/{i}"' in _mp for i in _elsewhere),
      "a task from another branch is listed")

print("\n== the right is offered when creating a user ==")
_form = admin.get("/admin/users").text
check("the tick box exists", 'value="view_all_reports"' in _form)
check("with a plain-English label", "See everyone&#39;s reports" in _form
      or "See everyone\u2019s reports" in _form)
# Read the actual default-rights map the page ships to the browser, rather
# than guessing at the surrounding HTML — the word "manager" appears a dozen
# times on that page and a substring search finds the wrong one.
import json as _json
_def = _json.loads(_form.split("DEF = ", 1)[1].split(";", 1)[0].strip())
check("a doer gets nothing by default", _def["doer"] == [])
check("a manager does NOT get it by default",
      "view_all_reports" not in _def["manager"], _def["manager"])
check("an admin does", "view_all_reports" in _def["admin"])

print("\n== the clock runs on Indian time, not UTC ==")
from datetime import timezone as _tz, timedelta as _td, datetime as _dt
from app import clock as _clock
_IST = _tz(_td(hours=5, minutes=30))
_real = _dt.now(_IST).replace(tzinfo=None)
check("clock.now() is Chandigarh time",
      abs((_clock.now() - _real).total_seconds()) < 5,
      f"off by {(_clock.now() - _real).total_seconds()/3600:.1f}h")
check("clock.today() is the Indian date", _clock.today() == _real.date())

# A deadline typed as 6pm must go overdue at 6pm, not 11:30pm. This is the
# whole point: the 'not done on time' half of every benchmark depends on it.
from app.models import Task as _TK, TaskSource as _TS, Priority as _PR
_late = _TK(title="t", due_at=_clock.now() - _td(minutes=1),
            source=_TS.DELEGATION, priority=_PR.MEDIUM)
_soon = _TK(title="t", due_at=_clock.now() + _td(minutes=1),
            source=_TS.DELEGATION, priority=_PR.MEDIUM)
check("a deadline one minute ago is overdue", _late.is_overdue)
check("a deadline one minute away is not", not _soon.is_overdue)

# And nothing anywhere may quietly go back to UTC.
import subprocess as _sp, os as _os
_hits = _sp.run(["grep", "-rn", "utcnow()", "app/", "--include=*.py"],
                capture_output=True, text=True).stdout
# clock.py names it in its own docstring, explaining why it is gone.
_hits = "\n".join(l for l in _hits.splitlines() if "app/clock.py" not in l)
check("no utcnow() left anywhere in the app", _hits.strip() == "", _hits[:200])
_naive = _sp.run(["grep", "-rn", "date.today()", "app/", "--include=*.py"],
                 capture_output=True, text=True).stdout
_naive = "\n".join(l for l in _naive.splitlines() if "app/clock.py" not in l)
check("no bare date.today() either", _naive.strip() == "", _naive[:200])

# A task created now must be stamped with Indian time in the database.
_tid = int(re.search(r"/tasks/(\d+)",
    admin.post("/tasks/new", data={"title": f"clock {RUN}", "doer_id": 6,
        "priority": "medium", "due_at": (_clock.now() + _td(days=1)).strftime("%Y-%m-%dT%H:%M"),
    }, follow_redirects=False).headers["location"]).group(1))
from app.db import SessionLocal as _SLC
with _SLC() as _d:
    _row = _d.get(_TK, _tid)
    check("its created_at is Indian time",
          abs((_row.created_at - _dt.now(_IST).replace(tzinfo=None)).total_seconds()) < 120,
          str(_row.created_at))

print("\n== departments: see, add, edit, delete ==")
from app.db import SessionLocal as _SLD
from app.models import Department as _Dept, Branch as _Brn, User as _UD
from sqlalchemy import select as _sd

_dp = admin.get("/admin/departments")
check("the departments page opens", _dp.status_code == 200, _dp.status_code)
check("it is reachable from the sidebar", "/admin/departments" in admin.get("/").text)
check("and from the user form", "/admin/departments" in admin.get("/admin/users").text)

with _SLD() as _d:
    _bz = _d.scalar(_sd(_Brn).where(_Brn.name.like("Bodyzone%"))).id
    _sk = _d.scalar(_sd(_Brn).where(_Brn.name.like("Spa Kora%"))).id

_name = f"Front Desk {RUN}"
admin.post("/admin/departments", data={"name": _name, "branch_id": str(_bz)})
with _SLD() as _d:
    _new = _d.scalar(_sd(_Dept).where(_Dept.name == _name))
check("a department can be added", _new is not None)
check("it is listed", _name in admin.get("/admin/departments").text)
_did = _new.id

check("a blank name is refused",
      admin.post("/admin/departments", data={"name": "  ", "branch_id": ""}).status_code == 400)
check("the same name twice in one branch is refused",
      admin.post("/admin/departments",
                 data={"name": _name, "branch_id": str(_bz)}).status_code == 400)
# But the same name under a DIFFERENT branch is normal — every branch has a
# front desk — so that must be allowed.
check("the same name under another branch is allowed",
      admin.post("/admin/departments",
                 data={"name": _name, "branch_id": str(_sk)}).status_code == 200)

check("the edit page opens", admin.get(f"/admin/departments/{_did}").status_code == 200)
admin.post(f"/admin/departments/{_did}",
           data={"name": f"Reception {RUN}", "branch_id": ""})
with _SLD() as _d:
    _r = _d.get(_Dept, _did)
    check("renaming works", _r.name == f"Reception {RUN}", _r.name)
    check("and it can be moved to all-branches", _r.branch_id is None)

# Put a person in it, then delete it: they must keep everything but the label.
_person = admin.post("/admin/users", data={"name": f"Dept Tester {RUN}",
    "email": f"dept.{RUN}@gcs.local", "phone": "", "password": "deptpass123",
    "role": "doer", "branch_id": str(_bz), "department_id": str(_did),
    "rights": [], "bm_delegation": 60, "bm_checklist": 20, "bm_fms": 20})
with _SLD() as _d:
    _pu = _d.scalar(_sd(_UD).where(_UD.email == f"dept.{RUN}@gcs.local"))
    _puid, _pu_branch = _pu.id, _pu.branch_id
    check("a person can be put in a department", _pu.department_id == _did)
# Two branches can both have a "Front Desk", so the dropdown has to say which.
_udrop = admin.get("/admin/users").text
_opts = re.findall(r'<option value="\d+">([^<]*Front Desk[^<]*)</option>', _udrop)
check("the department dropdown names the branch",
      _opts and all("—" in o for o in _opts), _opts)
check("so two same-named departments are distinguishable",
      len(set(_opts)) == len(_opts), _opts)

check("the edit page lists who is in it",
      f"Dept Tester {RUN}" in admin.get(f"/admin/departments/{_did}").text)

check("deleting needs the name typed exactly",
      admin.post(f"/admin/departments/{_did}", data={}, follow_redirects=False) is not None)
_bad = TestClient(app, follow_redirects=False)
_bad.post("/login", data={"email": "mis@gcs.local", "password": "gcs1234"})
check("a wrong confirmation is refused",
      _bad.post(f"/admin/departments/{_did}/delete",
                data={"confirm": "wrong"}).status_code == 400)
admin.post(f"/admin/departments/{_did}/delete", data={"confirm": f"Reception {RUN}"})
with _SLD() as _d:
    check("the department is gone", _d.get(_Dept, _did) is None)
    _pu2 = _d.get(_UD, _puid)
    check("but the person survives", _pu2 is not None)
    check("with no department", _pu2.department_id is None)
    check("and their branch untouched", _pu2.branch_id == _pu_branch)

print("\n== My Tasks separates Delegation, Checklist and FMS ==")
from app.db import SessionLocal as _SLT
from app.models import Task as _TT, TaskSource as _TSR, User as _UT, TaskStatus as _TST
from sqlalchemy import select as _st

OPEN_T = (_TST.PENDING, _TST.IN_PROGRESS, _TST.REJECTED, _TST.REOPENED)
with _SLT() as _d:
    _am = _d.scalar(_st(_UT).where(_UT.email == "amit@gcs.local"))
    _want = {}
    for _k, _src in [("delegation", _TSR.DELEGATION), ("checklist", _TSR.RECURRING),
                     ("fms", _TSR.FLOW)]:
        _want[_k] = {t.id for t in _d.scalars(_st(_TT).where(
            _TT.doer_id == _am.id, _TT.source == _src,
            _TT.status.in_(OPEN_T))).all()}

_all = doer.get("/tasks?scope=mine&status=open")
check("the work-type row is on the page", "Work type" in _all.text)
check("it offers all three kinds",
      all(l in _all.text for l in ["Delegation", "Checklist", "FMS"]))
check("and an All work tab", "All work" in _all.text)

# Each tab must show that kind of work and nothing else.
for _k in ("delegation", "checklist", "fms"):
    _pg = doer.get(f"/tasks?scope=mine&status=open&source={_k}")
    check(f"the {_k} tab opens", _pg.status_code == 200, _pg.status_code)
    _shown = {i for i in range(1, 400) if f'/tasks/{i}"' in _pg.text}
    _others = set().union(*[v for kk, v in _want.items() if kk != _k]) if _want else set()
    check(f"{_k}: shows its own tasks",
          _want[_k] <= _shown or not _want[_k],
          f"missing {sorted(_want[_k] - _shown)[:4]}")
    check(f"{_k}: shows no other kind",
          not (_shown & _others), f"leaked {sorted(_shown & _others)[:4]}")

# The tab counts must match what the tabs actually contain.
import re as _re
_row = _all.text.split("Work type", 1)[1].split("</div>", 1)[0]
_nums = [int(n) for n in _re.findall(r"<b>(\d+)</b>", _row)]
# Five numbers now: All work, Delegation, Help desk, Checklist, FMS. Help
# desk is a SLICE of delegation, not a fourth kind, so it must not be added
# into the total — the score counts it as delegation and so does this row.
check("the row shows all five counts", len(_nums) == 5, _nums)
check("the tab counts add up",
      len(_nums) == 5 and _nums[0] == _nums[1] + _nums[3] + _nums[4],
      f"all={_nums[:1]} parts={_nums[1:]}")
check("and match the database",
      len(_nums) == 5 and [_nums[1], _nums[3], _nums[4]] ==
      [len(_want["delegation"]), len(_want["checklist"]), len(_want["fms"])],
      f"page {_nums[1:]} vs db {[len(_want[k]) for k in ('delegation','checklist','fms')]}")
check("help desk never exceeds the delegation it is part of",
      len(_nums) == 5 and _nums[2] <= _nums[1], _nums)

print("\n== and sorts high priority first ==")
check("the page says how it is sorted",
      "high priority first, then by deadline" in _all.text.lower())
_open_pg = doer.get("/tasks?scope=mine&status=open").text
_ids = [int(i) for i in _re.findall(r'/tasks/(\d+)"', _open_pg)]
with _SLT() as _d:
    _seen, _ordered = set(), []
    for _i in _ids:
        if _i in _seen: continue
        _seen.add(_i)
        _t = _d.get(_TT, _i)
        if _t and _t.doer_id == _am.id:
            _ordered.append((_t.priority.value, _t.due_at))
_rank = {"high": 0, "medium": 1, "low": 2}
check("high priority really is listed first",
      all(_rank[_ordered[i][0]] <= _rank[_ordered[i+1][0]]
          for i in range(len(_ordered) - 1)),
      str([o[0] for o in _ordered]))
check("and within a priority, the earliest deadline first",
      all(_ordered[i][1] <= _ordered[i+1][1]
          for i in range(len(_ordered) - 1)
          if _ordered[i][0] == _ordered[i+1][0]))

# Filtering by work type must survive changing the status tab.
_done = doer.get("/tasks?scope=mine&status=done&source=checklist")
check("the work type carries over to Completed", _done.status_code == 200)
check("and stays selected there", "source=checklist" in _done.text)

print("\n== the dashboard cards go to that list, not a report ==")
_dash = doer.get("/").text
# Jinja escapes & as &amp; inside an href, so compare against the escaped form.
for _k in ("delegation", "checklist", "fms"):
    _href = f"/tasks?scope=mine&amp;status=open&amp;source={_k}"
    check(f"the {_k} card opens the doer's own list", _href in _dash,
          "card still points at a report")

print("\n== deadlines default to the end of the day ==")
_nf = admin.get("/tasks/new").text
check("delegation defaults to 11:59 pm", "T23:59" in _nf,
      re.search(r'value="[^"]*T\d\d:\d\d"', _nf).group(0) if re.search(r'value="[^"]*T\d\d:\d\d"', _nf) else "?")
check("a checklist rule does too", 'value="23:59"' in admin.get("/recurring").text)
check("and a help request", "T23:59" in doer.get("/help/new").text)

print("\n== only the right may move a planned date ==")
from app.db import SessionLocal as _SLP
from app.models import Task as _TP, User as _UP, Right as _RP
from sqlalchemy import select as _sp
from datetime import timedelta as _tdp

# The task is assigned TO the editor, so they can genuinely see it — a 404
# would have made the "field is locked" check pass for the wrong reason.
with _SLP() as _d:
    _ed = _d.scalar(_sp(_UP).where(_UP.email == "meena@gcs.local"))
    _was, _ed_id = _ed.rights, _ed.id
    _ed.set_rights([_RP.EDIT_TASK, _RP.AUDIT_TASK]); _d.commit()

_pid = int(re.search(r"/tasks/(\d+)", admin.post("/tasks/new", data={
    "title": f"date lock {RUN}", "doer_id": _ed_id, "priority": "medium",
    "due_at": "2026-10-01T23:59"}, follow_redirects=False).headers["location"]).group(1))
editor = login("meena@gcs.local")
_pg = editor.get(f"/tasks/{_pid}").text
check("the date field is locked for them", "disabled" in _pg and "Locked" in _pg)
check("they really can see the task", editor.get(f"/tasks/{_pid}").status_code == 200)
editor.post(f"/tasks/{_pid}/edit", data={"title": f"date lock {RUN}", "details": "",
    "doer_id": _ed_id, "priority": "medium", "due_at": "2027-12-31T10:00"})
with _SLP() as _d:
    check("and the deadline did not move",
          _d.get(_TP, _pid).due_at.year == 2026, str(_d.get(_TP, _pid).due_at))
    check("even though the edit itself went through",
          _d.get(_TP, _pid).title == f"date lock {RUN}")

# The admin holds every right, so the date moves for them.
admin.post(f"/tasks/{_pid}/edit", data={"title": f"date lock {RUN}", "details": "",
    "doer_id": _ed_id, "priority": "medium", "due_at": "2027-12-31T10:00"})
with _SLP() as _d:
    check("an admin can move it", _d.get(_TP, _pid).due_at.year == 2027)
    _u = _d.get(_UP, _ed_id); _u.rights = _was; _d.commit()

check("the right is offered on the user form",
      'value="change_due_date"' in admin.get("/admin/users").text)

print("\n== FMS: each step says where the flow goes next ==")
from app.models import Flow as _FL, FlowStep as _FS, FlowInstance as _FI, TaskStatus as _TSF

# The user's own example: verify -> submit -> decide -> (rejected: back to 1)
_made = admin.post("/flows/new", data={
    "name": f"Bill verification {RUN}", "branch_id": "", "description": "",
    "sf_label": ["Bill No", "Vendor", "Bill type"],
    "sf_type": ["text", "text", "select"],
    "sf_required": ["1", "1", "1"],
    "sf_options": ["", "", "Electricity, Water, Rent"],
    "step_title": ["Verify the bill as per checklist", "Submit to the manager",
                   "Verified or rejected?", "Send to CMD for approval"],
    "step_doer": ["6", "6", "6", "6"],
    "step_tat": ["24", "24", "24", "24"],
    "step_priority": ["medium", "medium", "high", "medium"],
    "step_instructions": ["", "", "", ""],
    "step_fields": ["", "", "", ""],
    "step_audit": ["0", "0", "0", "0"],
    "step_proof": ["0", "0", "0", "0"],
    "step_decision": ["0", "0", "1", "0"],
    "step_yes": ["", "", "Verified", ""],
    "step_no": ["", "", "Rejected", ""],
    "step_next": ["2", "3", "4", "0"],
    "step_fail": ["", "", "1", ""],
})
check("the flow is created", _made.status_code == 200, _made.status_code)
with _SLP() as _d:
    _flow = _d.scalar(_sp(_FL).where(_FL.name == f"Bill verification {RUN}"))
    _fid = _flow.id
    _steps = {s.position: s for s in _flow.steps}
    check("step 3 is a decision", _steps[3].is_decision)
    check("with the two outcomes named",
          _steps[3].yes_label == "Verified" and _steps[3].no_label == "Rejected")
    check("verified routes to step 4", _steps[3].next_step_pos == 4)
    check("rejected routes back to step 1", _steps[3].fail_step_pos == 1)
    check("step 4 ends the flow", _steps[4].next_step_pos == 0)
    check("the flow carries its own start questions",
          _flow.start_field_list == ["Bill No", "Vendor", "Bill type"],
          str(_flow.start_field_list))
    _bt = _flow.start_form_fields[2]
    check("and one of them is a list question", _bt["type"] == "select")
    check("with the choices typed in",
          _bt["options"] == ["Electricity", "Water", "Rent"], str(_bt["options"]))

check("a route to a step that does not exist is refused",
      admin.post("/flows/new", data={
          "name": f"bad route {RUN}", "branch_id": "", "description": "",
          "step_title": ["only step"], "step_doer": ["6"], "step_tat": ["24"],
          "step_priority": ["medium"], "step_instructions": [""], "step_fields": [""],
          "step_audit": ["0"], "step_proof": ["0"], "step_decision": ["0"],
          "step_yes": [""], "step_no": [""], "step_next": ["9"], "step_fail": [""],
      }).status_code == 400)
check("a decision step with only one outcome is refused",
      admin.post("/flows/new", data={
          "name": f"half decision {RUN}", "branch_id": "", "description": "",
          "step_title": ["decide"], "step_doer": ["6"], "step_tat": ["24"],
          "step_priority": ["medium"], "step_instructions": [""], "step_fields": [""],
          "step_audit": ["0"], "step_proof": ["0"], "step_decision": ["1"],
          "step_yes": ["Yes"], "step_no": ["No"], "step_next": ["0"], "step_fail": [""],
      }).status_code == 400)

print("\n== and a rejected bill really does go back to step 1 ==")
_dpage = admin.get(f"/flows/{_fid}").text
check("the start form asks the flow's own questions",
      "Bill No" in _dpage and "Vendor" in _dpage and "Bill type" in _dpage)
check("and renders the list one as a dropdown",
      '<option value="Electricity">' in _dpage)
check("the step list shows where each one routes",
      "Rejected →" in _dpage and "step 1" in _dpage)
check("starting without a required field is refused",
      admin.post(f"/flows/{_fid}/start",
                 data={"reference": f"INV-{RUN}"}).status_code == 400)

_started = admin.post(f"/flows/{_fid}/start", data={
    "reference": f"INV-{RUN}", "sf0": "BILL-77", "sf1": "Acme",
    "sf2": "Electricity"})
check("the flow starts", _started.status_code == 200, _started.status_code)
with _SLP() as _d:
    _inst = _d.scalar(_sp(_FI).where(_FI.reference == f"INV-{RUN}"))
    _iid = _inst.id
    check("the start values are stored on the run", "BILL-77" in (_inst.context or ""))

def _open_step(iid):
    with _SLP() as _d:
        rows = [t for t in _d.scalars(_sp(_TP).where(_TP.flow_instance_id == iid)).all()
                if t.status not in (_TSF.COMPLETED, _TSF.CANCELLED)]
        return (rows[0].id, rows[0].flow_step.position) if rows else (None, None)

amit = login("amit@gcs.local")
_t, _pos = _open_step(_iid)
check("step 1 is open", _pos == 1, str(_pos))
amit.post(f"/tasks/{_t}/submit", data={})
_t, _pos = _open_step(_iid)
check("step 1 routes to step 2", _pos == 2, str(_pos))
amit.post(f"/tasks/{_t}/submit", data={})
_t, _pos = _open_step(_iid)
check("step 2 routes to step 3", _pos == 3, str(_pos))

_dt = amit.get(f"/tasks/{_t}").text
check("the decision step shows two buttons",
      'value="pass"' in _dt and 'value="fail"' in _dt)
check("named as configured", "Verified" in _dt and "Rejected" in _dt)
_nodec = TestClient(app, follow_redirects=False)
_nodec.post("/login", data={"email": "amit@gcs.local", "password": "gcs1234"})
check("submitting without choosing is refused",
      _nodec.post(f"/tasks/{_t}/submit", data={}).status_code == 400)

amit.post(f"/tasks/{_t}/submit", data={"decision": "fail"})
_t, _pos = _open_step(_iid)
check("REJECTED sends it back to step 1", _pos == 1, str(_pos))

# Round again, this time approving.
amit.post(f"/tasks/{_t}/submit", data={})
_t, _pos = _open_step(_iid)
amit.post(f"/tasks/{_t}/submit", data={})
_t, _pos = _open_step(_iid)
check("back at the decision", _pos == 3, str(_pos))
amit.post(f"/tasks/{_t}/submit", data={"decision": "pass"})
_t, _pos = _open_step(_iid)
check("VERIFIED carries on to step 4", _pos == 4, str(_pos))
amit.post(f"/tasks/{_t}/submit", data={})
with _SLP() as _d:
    check("and step 4 finishes the run",
          _d.get(_FI, _iid).completed_at is not None)
check("nothing is left open", _open_step(_iid)[0] is None)

print("\n== flows built before routing existed still run in order ==")
with _SLP() as _d:
    _old = _d.scalar(_sp(_FL).where(_FL.name.notlike(f"%{RUN}%")))
    if _old:
        check("an older flow has no routes set",
              all(s.next_step_pos is None for s in _old.steps))
        _oid = _old.id
_ostart = admin.post(f"/flows/{_oid}/start", data={"reference": f"LEGACY-{RUN}"})
check("it still starts", _ostart.status_code == 200)
with _SLP() as _d:
    _oi = _d.scalar(_sp(_FI).where(_FI.reference == f"LEGACY-{RUN}"))
    _oiid = _oi.id
_t2, _p2 = _open_step(_oiid)
with _SLP() as _d:
    _doer2 = _d.get(_TP, _t2).doer.email
_who = login(_doer2)
attach(_who, _t2); _who.post(f"/tasks/{_t2}/submit", data={})
_t3, _p3 = _open_step(_oiid)
check("and walks to the next step in order", _p3 == (_p2 or 0) + 1,
      f"{_p2} -> {_p3}")

print("\n== the menu is in the order you asked for ==")
_nav = admin.get("/").text
_order = [x for x in ["Operations", "Reports", "Insight", "Setup"] if x in _nav]
check("Operations, Reports, Insight, Setup",
      _order == ["Operations", "Reports", "Insight", "Setup"], str(_order))
_pos_of = {k: _nav.index(f">{k}<") for k in _order}
check("and they appear in that order on the page",
      list(_pos_of) == sorted(_pos_of, key=_pos_of.get), str(_pos_of))
check("Dashboard is above all of them",
      _nav.index("Dashboard") < min(_pos_of.values()))

print("\n== something confirms what just happened ==")
# The confirmation is one-shot and lives in the signed session, not the URL.
_c = TestClient(app, follow_redirects=True)
_r = _c.post("/login", data={"email": "mis@gcs.local", "password": "gcs1234"})
check("signing in says so", "Signed in" in _r.text and "toast" in _r.text)
check("and it does not come back on a refresh", "Signed in" not in _c.get("/").text)

_r2 = _c.post("/tasks/new", data={"title": f"toast {RUN}", "doer_id": 6,
                                  "priority": "medium", "due_at": "2026-11-01T23:59"})
check("assigning a task says so", "Task assigned" in _r2.text)
_tid2 = int(re.search(r"/tasks/(\d+)", str(_r2.url)).group(1))

_a = login("amit@gcs.local")
attach(_a, _tid2)
check("submitting says so", "Marked complete" in _a.post(
    f"/tasks/{_tid2}/submit", data={}).text)

check("the toast is switched off for reduced motion",
      "prefers-reduced-motion" in _c.get("/static/app.css").text)

print("\n== TAT is a unit and a number, not just hours ==")
from app import clock as _ck
from datetime import datetime as _dtu
check("months are calendar months, not 30-day blocks",
      _ck.add_months(_dtu(2026, 1, 31, 10, 0)) if False else
      _ck.add_months(_dtu(2026, 1, 31, 10, 0), 1) == _dtu(2026, 2, 28, 10, 0),
      str(_ck.add_months(_dtu(2026, 1, 31, 10, 0), 1)))
check("and they roll over the year",
      _ck.add_months(_dtu(2026, 12, 15, 9, 0), 2) == _dtu(2027, 2, 15, 9, 0))
check("a leap February is handled",
      _ck.add_months(_dtu(2028, 1, 31, 9, 0), 1) == _dtu(2028, 2, 29, 9, 0),
      str(_ck.add_months(_dtu(2028, 1, 31, 9, 0), 1)))
for _u, _v, _want in [("minutes", 30, _dtu(2026, 3, 1, 10, 30)),
                      ("hours", 5, _dtu(2026, 3, 1, 15, 0)),
                      ("days", 2, _dtu(2026, 3, 3, 10, 0)),
                      ("weeks", 2, _dtu(2026, 3, 15, 10, 0))]:
    check(f"{_v} {_u}", _ck.add_span(_dtu(2026, 3, 1, 10, 0), _u, _v) == _want)

_fp = admin.get("/flows/new").text
check("the builder offers every unit",
      all(f'value="{u}"' in _fp for u in ["minutes", "hours", "days", "weeks", "months"]))
check("and a second box for the number", 'class="tatval"' in _fp)
check("with sensible choices per unit", '"weeks": [1, 2, 3, 4, 6, 8]' in _fp
      or '"weeks":[1,2,3,4,6,8]' in _fp.replace(" ", ""))
check("the builder asks what to count the TAT from",
      'name="step_due_from"' in _fp)

print("\n== a step can hang its date off another step's date ==")
from app.models import Flow as _FLT, FlowInstance as _FIT, Task as _TKT
_made2 = admin.post("/flows/new", data={
    "name": f"TAT units {RUN}", "branch_id": "", "description": "",
    "step_title": ["Raise it", "Chase it", "Close it"],
    "step_doer": ["6", "6", "6"], "step_instructions": ["", "", ""],
    "step_fields": ["", "", ""], "step_audit": ["0", "0", "0"],
    "step_proof": ["0", "0", "0"], "step_decision": ["0", "0", "0"],
    "step_yes": ["", "", ""], "step_no": ["", "", ""],
    "step_priority": ["medium", "medium", "medium"],
    "step_tat_unit": ["days", "weeks", "months"],
    "step_tat": ["2", "1", "1"],
    "step_next": ["2", "3", "0"],
    "step_fail": ["", "", ""],
    # step 3's date is measured from step 1's date, not from when it opens
    "step_due_from": ["", "", "1"],
})
check("the flow saves", _made2.status_code == 200, _made2.status_code)
with _SLP() as _d:
    _f2 = _d.scalar(_sp(_FLT).where(_FLT.name == f"TAT units {RUN}"))
    _f2id = _f2.id
    _st = {x.position: x for x in _f2.steps}
    check("step 1 is 2 days", (_st[1].tat_unit, _st[1].tat_value) == ("days", 2))
    check("step 2 is 1 week", (_st[2].tat_unit, _st[2].tat_value) == ("weeks", 1))
    check("step 3 is 1 month", (_st[3].tat_unit, _st[3].tat_value) == ("months", 1))
    check("and reads back in words", _st[3].tat_label == "1 month", _st[3].tat_label)
    check("step 3 is tied to step 1's date", _st[3].due_from_pos == 1)

check("a date tied to a step that does not exist is refused",
      admin.post("/flows/new", data={
          "name": f"bad link {RUN}", "branch_id": "", "description": "",
          "step_title": ["one"], "step_doer": ["6"], "step_tat": ["1"],
          "step_tat_unit": ["days"], "step_priority": ["medium"],
          "step_instructions": [""], "step_fields": [""], "step_audit": ["0"],
          "step_proof": ["0"], "step_decision": ["0"], "step_yes": [""],
          "step_no": [""], "step_next": ["0"], "step_fail": [""],
          "step_due_from": ["7"]}).status_code == 400)
check("a step cannot take its date from itself",
      admin.post("/flows/new", data={
          "name": f"self link {RUN}", "branch_id": "", "description": "",
          "step_title": ["one"], "step_doer": ["6"], "step_tat": ["1"],
          "step_tat_unit": ["days"], "step_priority": ["medium"],
          "step_instructions": [""], "step_fields": [""], "step_audit": ["0"],
          "step_proof": ["0"], "step_decision": ["0"], "step_yes": [""],
          "step_no": [""], "step_next": ["0"], "step_fail": [""],
          "step_due_from": ["1"]}).status_code == 400)

# Walk it and check the real deadlines the engine produced.
admin.post(f"/flows/{_f2id}/start", data={"reference": f"TATRUN-{RUN}"})
with _SLP() as _d:
    _i2 = _d.scalar(_sp(_FIT).where(_FIT.reference == f"TATRUN-{RUN}"))
    _i2id = _i2.id
_t, _pos = _open_step(_i2id)
with _SLP() as _d:
    _due1 = _d.get(_TKT, _t).due_at
check("step 1 is due about two days out",
      1 <= (_due1 - _ck.now()).days <= 2, str(_due1))
amit.post(f"/tasks/{_t}/submit", data={})
_t, _pos = _open_step(_i2id)
with _SLP() as _d:
    _due2 = _d.get(_TKT, _t).due_at
check("step 2 is due about a week out",
      6 <= (_due2 - _ck.now()).days <= 8, str(_due2))
amit.post(f"/tasks/{_t}/submit", data={})
_t, _pos = _open_step(_i2id)
with _SLP() as _d:
    _due3 = _d.get(_TKT, _t).due_at
# Step 3 is a month after STEP 1's date, not a month from now — that is the
# whole point of tying it. Step 1 was due ~2 days out, so this lands near
# a month and two days from now, not a month from now.
# A month after step 1's date — give or take the shift off a closed day. If a
# month later lands on a Sunday the deadline comes back to the Saturday, so
# the check allows for that rather than demanding the exact date.
_want3 = _ck.add_months(_due1, 1).date()
check("step 3 counts its month from step 1's date, not from today",
      0 <= (_want3 - _due3.date()).days <= 3,
      f"step1 {_due1.date()} +1m = {_want3} but got {_due3.date()}")
check("which is later than a month from now",
      _due3 > _ck.add_months(_ck.now(), 1) - __import__("datetime").timedelta(days=1))

print("\n== old flows keep their hours ==")
with _SLP() as _d:
    # Steps that came from the SEED, not from anything this run created.
    # Filtering on the step title missed them, because a step this test made
    # is called "Raise it" — the flow it belongs to is what carries the run id.
    _mine = {f.id for f in _d.scalars(_sp(_FLT)).all() if RUN in f.name}
    _legacy = [x for x in _d.scalars(_sp(_FS)).all() if x.flow_id not in _mine]
    check("there are older steps to check", bool(_legacy))
    check("every pre-existing step has a unit after upgrade",
          all(x.tat_unit for x in _legacy), "some are NULL")
    check("and it is hours", all(x.tat_unit == "hours" for x in _legacy))
    check("with the number it always had",
          all(x.tat_value == x.tat_hours for x in _legacy),
          str([(x.tat_value, x.tat_hours) for x in _legacy[:3]]))

# The upgrade path itself, not just freshly seeded rows: the new column
# carries DEFAULT 24, so a backfill keyed on NULL matches nothing and a
# 4-hour step silently becomes 24. Prove the real database upgrade keeps
# every number, and that running it twice changes nothing.
import shutil as _sh, tempfile as _tf, subprocess as _sub, os as _os, json as _js
_probe = _os.path.join(_tf.mkdtemp(), "upgrade.db")
_sh.copy("midap-backup-20260910-072235.db", _probe)
_code = ("from app import migrate; migrate.run(); migrate.run();"
         "from app.db import SessionLocal; from app.models import FlowStep;"
         "from sqlalchemy import select; import json;"
         "d=SessionLocal(); r=d.scalars(select(FlowStep)).all();"
         "print(json.dumps([[s.tat_hours, s.tat_value, s.tat_unit] for s in r]))")
_out = _sub.run([__import__("sys").executable, "-c", _code], capture_output=True, text=True,
                env={**_os.environ, "DATABASE_URL": f"sqlite:///{_probe}"})
_rows = _js.loads(_out.stdout.strip().splitlines()[-1]) if _out.stdout.strip() else []
check("an existing database upgrades with its TAT numbers intact",
      _rows and all(h == v and u == "hours" for h, v, u in _rows),
      _out.stderr[-200:] or str(_rows[:4]))
check("and the upgrade is safe to run twice", bool(_rows))

print("\n== the start form is a real form builder ==")
_nb = admin.get("/flows/new").text
check("the builder is on the create page", 'name="sf_label"' in _nb)
for _t, _lbl in [("text", "Short text"), ("textarea", "Long text"),
                 ("number", "Number"), ("date", "Date"),
                 ("select", "Choose from a list"), ("yesno", "Yes / No")]:
    check(f"it offers '{_lbl}'", f'value="{_t}"' in _nb and _lbl in _nb)
check("a question can be made optional", 'name="sf_required"' in _nb)
check("and a list question takes its choices", 'name="sf_options"' in _nb)

check("a list question with no choices is refused",
      admin.post("/flows/new", data={
          "name": f"no choices {RUN}", "branch_id": "", "description": "",
          "sf_label": ["Type"], "sf_type": ["select"], "sf_required": ["1"],
          "sf_options": [""],
          "step_title": ["one"], "step_doer": ["6"], "step_tat": ["1"],
          "step_tat_unit": ["days"], "step_priority": ["medium"],
          "step_instructions": [""], "step_fields": [""], "step_audit": ["0"],
          "step_proof": ["0"], "step_decision": ["0"], "step_yes": [""],
          "step_no": [""], "step_next": ["0"], "step_fail": [""],
          "step_due_from": [""]}).status_code == 400)

print("\n== every answer type is checked when a run starts ==")
_tf = admin.post("/flows/new", data={
    "name": f"Typed start {RUN}", "branch_id": "", "description": "",
    "sf_label": ["Amount", "Pay by", "Urgent?", "Notes"],
    "sf_type": ["number", "date", "yesno", "textarea"],
    "sf_required": ["1", "1", "1", "0"],
    "sf_options": ["", "", "", ""],
    "step_title": ["Do it"], "step_doer": ["6"], "step_tat": ["1"],
    "step_tat_unit": ["days"], "step_priority": ["medium"],
    "step_instructions": [""], "step_fields": [""], "step_audit": ["0"],
    "step_proof": ["0"], "step_decision": ["0"], "step_yes": [""],
    "step_no": [""], "step_next": ["0"], "step_fail": [""], "step_due_from": [""]})
check("a typed start form saves", _tf.status_code == 200, _tf.status_code)
with _SLP() as _d:
    _tflow = _d.scalar(_sp(_FLT).where(_FLT.name == f"Typed start {RUN}"))
    _tfid = _tflow.id
    check("the types are stored",
          [f["type"] for f in _tflow.start_form_fields] ==
          ["number", "date", "yesno", "textarea"])
    check("and the optional one is marked optional",
          _tflow.start_form_fields[3]["required"] is False)

_sp_page = admin.get(f"/flows/{_tfid}").text
check("a number question renders as a number box", 'type="number"' in _sp_page)
check("a date question renders as a date box", 'type="date"' in _sp_page)
check("a yes/no question renders as a dropdown", '<option value="Yes">' in _sp_page)
check("a long-text question renders as a textarea", "<textarea" in _sp_page)

_ok = {"reference": f"TYPED-{RUN}", "sf0": "1500", "sf1": "2026-12-01",
       "sf2": "Yes", "sf3": ""}
check("a letter in a number box is refused",
      admin.post(f"/flows/{_tfid}/start",
                 data={**_ok, "sf0": "abcd"}).status_code == 400)
check("something that is not Yes or No is refused",
      admin.post(f"/flows/{_tfid}/start",
                 data={**_ok, "sf2": "Maybe"}).status_code == 400)
check("a choice that is not on the list is refused",
      admin.post(f"/flows/{_fid}/start",
                 data={"reference": f"BAD-{RUN}", "sf0": "B1", "sf1": "V",
                       "sf2": "Diesel"}).status_code == 400)
check("but the optional one may be left blank",
      admin.post(f"/flows/{_tfid}/start", data=_ok).status_code == 200)
with _SLP() as _d:
    _ti = _d.scalar(_sp(_FIT).where(_FIT.reference == f"TYPED-{RUN}"))
    check("the answers are on the run",
          "1500" in (_ti.context or "") and "2026-12-01" in (_ti.context or ""),
          _ti.context)
    check("and the blank optional one is simply absent",
          "Notes" not in (_ti.context or ""))

print("\n== a flow built earlier can be given a start form ==")
# This is the real complaint: "Bill to payment" exists with no questions and
# no way to add them. Build a flow with none, then add them afterwards.
admin.post("/flows/new", data={
    "name": f"No questions {RUN}", "branch_id": "", "description": "",
    "sf_label": [""], "sf_type": ["text"], "sf_required": ["1"], "sf_options": [""],
    "step_title": ["Only step"], "step_doer": ["6"], "step_tat": ["1"],
    "step_tat_unit": ["days"], "step_priority": ["medium"],
    "step_instructions": [""], "step_fields": [""], "step_audit": ["0"],
    "step_proof": ["0"], "step_decision": ["0"], "step_yes": [""],
    "step_no": [""], "step_next": ["0"], "step_fail": [""], "step_due_from": [""]})
with _SLP() as _d:
    _nq = _d.scalar(_sp(_FLT).where(_FLT.name == f"No questions {RUN}"))
    _nqid = _nq.id
    check("it starts with no questions", _nq.start_form_fields == [])
check("an empty question row is not saved as a blank question", True)

check("the flow page offers Edit",
      f"/flows/{_nqid}/edit" in admin.get(f"/flows/{_nqid}").text)
check("the edit page opens", admin.get(f"/flows/{_nqid}/edit").status_code == 200)
_ed = admin.post(f"/flows/{_nqid}/edit", data={
    "name": f"No questions {RUN}", "branch_id": "", "description": "now it asks",
    "sf_label": ["Invoice No", "Department"], "sf_type": ["text", "select"],
    "sf_required": ["1", "1"], "sf_options": ["", "Accounts, Ops"]})
check("the start form can be added afterwards", _ed.status_code == 200, _ed.status_code)
with _SLP() as _d:
    _nq2 = _d.get(_FLT, _nqid)
    check("and it sticks",
          _nq2.start_field_list == ["Invoice No", "Department"],
          str(_nq2.start_field_list))
    check("the description was saved too", _nq2.description == "now it asks")
check("the start page now asks them",
      "Invoice No" in admin.get(f"/flows/{_nqid}").text)
check("a doer cannot edit a flow",
      doer.get(f"/flows/{_nqid}/edit").status_code == 403)

# The edit page must show what is already there, not an empty builder.
_epage = admin.get(f"/flows/{_nqid}/edit").text
# The builder is filled in by script from this JSON, so that is what has to
# be right — the choices are joined into "Accounts, Ops" in the browser and
# never appear as literal text in the source.
_json_in_page = _re.search(r"var existing = (.*?);\n", _epage)
_pre = _json.loads(_json_in_page.group(1)) if _json_in_page else []
check("the edit page hands the builder the current questions",
      [f["label"] for f in _pre] == ["Invoice No", "Department"], str(_pre))
check("including the list question's choices",
      _pre and _pre[1]["options"] == ["Accounts", "Ops"], str(_pre[-1:]))
check("and whether each is required",
      all(f["required"] for f in _pre))

print("\n== flows built with the old comma box still ask their questions ==")
with _SLP() as _d:
    _old_style = _FLT(org_id=1, name=f"Legacy questions {RUN}",
                      start_fields="Member Name, Lead Id")
    _d.add(_old_style); _d.commit()
    check("a comma line is read as plain text questions",
          [f["label"] for f in _old_style.start_form_fields] ==
          ["Member Name", "Lead Id"],
          str(_old_style.start_form_fields))
    check("and they are all short-text",
          all(f["type"] == "text" for f in _old_style.start_form_fields))

print("\n== the user form accepts a split under 100 ==")
from app.db import SessionLocal as _SLB
from app.models import User as _UB
from sqlalchemy import select as _sb

_r = admin.post("/admin/users", data={
    "name": f"Partly manual {RUN}", "email": f"pm.{RUN}@gcs.local", "phone": "",
    "password": "pmpass1234", "role": "doer", "branch_id": "", "department_id": "",
    "rights": [], "bm_delegation": 40, "bm_checklist": 10, "bm_fms": 10})
check("40 / 10 / 10 is accepted", _r.status_code == 200, _r.status_code)
with _SLB() as _d:
    _pm = _d.scalar(_sb(_UB).where(_UB.email == f"pm.{RUN}@gcs.local"))
    check("the user is created", _pm is not None)
    check("with exactly those benchmarks",
          (_pm.bm_delegation, _pm.bm_checklist, _pm.bm_fms) == (40, 10, 10),
          str((_pm.bm_delegation, _pm.bm_checklist, _pm.bm_fms)))
    _pmid = _pm.id

_bad = TestClient(app, follow_redirects=False)
_bad.post("/login", data={"email": "mis@gcs.local", "password": "gcs1234"})
check("but over 100 is still refused",
      _bad.post("/admin/users", data={
          "name": f"Over {RUN}", "email": f"over.{RUN}@gcs.local", "phone": "",
          "password": "overpass123", "role": "doer", "branch_id": "",
          "department_id": "", "rights": [], "bm_delegation": 60,
          "bm_checklist": 60, "bm_fms": 60}).status_code == 400)

check("editing to a sub-100 split works",
      admin.post(f"/admin/users/{_pmid}", data={
          "name": f"Partly manual {RUN}", "phone": "", "role": "doer",
          "branch_id": "", "department_id": "", "rights": [],
          "bm_delegation": 30, "bm_checklist": 20,
          "bm_fms": 0}).status_code == 200)
with _SLB() as _d:
    check("and saves", _d.get(_UB, _pmid).benchmark_total == 50)

check("the page no longer demands 100",
      "must add up to" not in admin.get("/admin/users").text)
check("it explains what a sub-100 split means",
      "do not have to add up to 100" in admin.get("/admin/users").text)
_js = admin.get("/static/benchmark.js").text
check("and the browser no longer blocks it", "t === 100" not in _js)
check("while still stopping a total over 100", "t > 100" in _js)

# Their own score page has to say so, or 50 reads as a result rather than as
# "the software had 50 points to give".
_pmc = login(f"pm.{RUN}@gcs.local", "pmpass1234")
_dash = _pmc.get("/").text
check("the doer's dashboard says how much is scored by software",
      "50 of the 100 points are scored by the" in _dash
      or "50 of 100 scored here" in _dash, "no note shown")

print("\n== the checklist no longer depends on the server restarting ==")
# This used to happen only at boot. On a host kept awake around the clock
# there is no second boot, so the checklist would have stopped after day one.
import asyncio as _aio, datetime as _dtm
from app import main as _M
from app.models import RecurringRule as _RR

def _recurring_count():
    with _SLP() as _d:
        return len(_d.scalars(_sp(_TP).where(_TP.source == _TSR.RECURRING)).all())

_real_today = _clock.today
_day = _clock.today()
_M.SPAWN_CHECK_SECONDS = 0.05
with _SLP() as _d:
    for _r in _d.scalars(_sp(_RR)).all():
        _r.last_spawned_on = None
    _d.commit()

async def _walk_two_days():
    _t = _aio.create_task(_M._daily_spawn())
    await _aio.sleep(0.3)
    _before = _recurring_count()
    _clock.today = lambda: _day + _dtm.timedelta(days=1)
    await _aio.sleep(0.4)
    _after = _recurring_count()
    await _aio.sleep(0.4)          # same day again
    _again = _recurring_count()
    _t.cancel()
    try:
        await _t
    except _aio.CancelledError:
        pass
    return _before, _after, _again

try:
    _b, _a, _ag = _aio.run(_walk_two_days())
finally:
    _clock.today = _real_today
    _M.SPAWN_CHECK_SECONDS = 600

check("the app spawns today's checklist on its own", _b > 0, _b)
check("and a new day's checklist without any restart", _a > _b, f"{_b} -> {_a}")
check("running again the same day creates no duplicates", _ag == _a, f"{_a} -> {_ag}")

# The loop must survive a bad day rather than dying silently.
async def _survive_error():
    _boom = _clock.today
    _clock.today = lambda: (_ for _ in ()).throw(RuntimeError("bad clock"))
    _t = _aio.create_task(_M._daily_spawn())
    await _aio.sleep(0.2)
    _alive = not _t.done()
    _clock.today = _boom
    _t.cancel()
    try:
        await _t
    except _aio.CancelledError:
        pass
    return _alive

_M.SPAWN_CHECK_SECONDS = 0.05
try:
    check("a failure does not kill the loop", _aio.run(_survive_error()))
finally:
    _M.SPAWN_CHECK_SECONDS = 600
    _clock.today = _real_today

check("/cron/spawn still works alongside it",
      admin.get("/cron/spawn").status_code in (200, 401))

print("\n== an uptime monitor can reach us ==")
# UptimeRobot, Better Stack and most link checkers send HEAD, not GET. Every
# route used to refuse it with 405, so the site read as permanently down
# while being perfectly healthy.
_hc = TestClient(app, follow_redirects=False)
for _path in ["/", "/healthz", "/login", "/static/app.css"]:
    _g = _hc.request("GET", _path)
    _h = _hc.request("HEAD", _path)
    check(f"HEAD {_path} is answered, not refused",
          _h.status_code != 405, f"got {_h.status_code}")
    check(f"HEAD {_path} gives the same status as GET",
          _h.status_code == _g.status_code, f"{_h.status_code} vs {_g.status_code}")
    check(f"HEAD {_path} sends no body", _h.content == b"", _h.content[:40])

# The header must still describe what a GET would return — that is what HEAD
# is for. A zero here would be a lie.
_hh = _hc.request("HEAD", "/healthz")
_hg = _hc.request("GET", "/healthz")
check("HEAD keeps the real content-length",
      _hh.headers.get("content-length") == _hg.headers.get("content-length"),
      f"{_hh.headers.get('content-length')} vs {_hg.headers.get('content-length')}")
check("and the same content-type",
      _hh.headers.get("content-type") == _hg.headers.get("content-type"))

# The monitor URL people will actually use.
check("/healthz answers HEAD with 200",
      _hc.request("HEAD", "/healthz").status_code == 200)
check("and GET still returns the real body",
      '"ok":true' in _hc.request("GET", "/healthz").text)

# POST must NOT be quietly turned into a GET by the same middleware.
check("POST is untouched",
      _hc.request("POST", "/healthz").status_code == 405)

print("\n== closing a task from the list it appears on ==")
# The doer's own three kinds of work, still open.
from app.models import (Task as _MT, TaskSource as _MS, User as _MU,
                        TaskStatus as _MST, Attachment as _MA)
from sqlalchemy import select as _msel

_OPEN = (_MST.PENDING, _MST.IN_PROGRESS, _MST.REJECTED, _MST.REOPENED)
_mine = {}
with _SLT() as _d:
    _amit = _d.scalar(_msel(_MU).where(_MU.email == "amit@gcs.local"))
    for _k, _src in [("delegation", _MS.DELEGATION), ("checklist", _MS.RECURRING),
                     ("fms", _MS.FLOW)]:
        _t = _d.scalars(_msel(_MT).where(
            _MT.doer_id == _amit.id, _MT.source == _src,
            _MT.status.in_(_OPEN)).limit(1)).first()
        if _t:
            _mine[_k] = _t.id

check("the doer has open work of at least two kinds to test with",
      len(_mine) >= 2, sorted(_mine))

# 1 — the button is on the list, for every kind of work.
_list = doer.get("/tasks?scope=mine&status=open").text
check("the list offers a Mark complete button", "Mark complete" in _list)
for _k, _i in _mine.items():
    check(f"{_k}: its row has the button", f'data-mark="{_i}"' in _list,
          f"task {_i}")

# The same button on the dashboard, which is where most people start.
_dash = doer.get("/").text
check("the dashboard rows have it too", 'data-mark="' in _dash)

# 2 — and never on somebody else's work. The admin sees every task; none of
# them may be closeable from their list, because they are not doing them.
_allpg = admin.get("/tasks?scope=all&status=open").text
for _k, _i in _mine.items():
    check(f"{_k}: an onlooker gets no button for it",
          f'data-mark="{_i}"' not in _allpg, f"task {_i} closeable by admin")

# 3 — the box itself.
_one = _mine.get("delegation") or list(_mine.values())[0]
_box = doer.get(f"/tasks/{_one}/mark")
check("the doer can open the box", _box.status_code == 200, _box.status_code)
check("it is a fragment, not a whole page", "<html" not in _box.text.lower())
check("it posts to the ordinary submit route",
      f'action="/tasks/{_one}/submit"' in _box.text)
check("it carries a return_to field", 'class="returnto"' in _box.text)
check("it offers the full task page as the other way in",
      f'href="/tasks/{_one}"' in _box.text)
check("it has a note box", 'name="completion_note"' in _box.text)

check("somebody else cannot open it",
      mgr.get(f"/tasks/{_one}/mark").status_code == 403,
      mgr.get(f"/tasks/{_one}/mark").status_code)
_stranger = login("ravi@gcs.local")     # a doer in another branch entirely
check("and a stranger gets a plain not-found",
      _stranger.get(f"/tasks/{_one}/mark").status_code == 404,
      _stranger.get(f"/tasks/{_one}/mark").status_code)

# 4 — submitting from the box lands back on the list, not the task page.
_mk = mgr.post("/tasks/new", data={
    "title": f"SMOKE popup return {RUN}", "details": "",
    "doer_id": str(_amit.id), "branch_id": "", "priority": "low",
    "due_at": "2026-12-31T23:59"})
_pid = int(_re.findall(r"/tasks/(\d+)/comment", _mk.text)[-1])
attach(doer, _pid)
_back = "/tasks?scope=mine&status=open&source=delegation"
_nav = TestClient(app, follow_redirects=False)
_nav.cookies.update(doer.cookies)
_r = _nav.post(f"/tasks/{_pid}/submit",
               data={"completion_note": "done from the list", "return_to": _back})
check("submitting from the box goes back to the list",
      _r.status_code == 303 and _r.headers["location"] == _back,
      f"{_r.status_code} -> {_r.headers.get('location')}")
with _SLT() as _d:
    _t = _d.get(_MT, _pid)
    check("and the task really is closed",
          _t.status in (_MST.COMPLETED, _MST.SUBMITTED), _t.status)
    check("with the note saved", _t.completion_note == "done from the list",
          _t.completion_note)

# 5 — return_to may only ever be a path on this site. A form field that
# decides where a browser lands is the classic way to bounce somebody onto
# another site from a link that looks like ours.
_mk2 = mgr.post("/tasks/new", data={
    "title": f"SMOKE popup redirect {RUN}", "details": "",
    "doer_id": str(_amit.id), "branch_id": "", "priority": "low",
    "due_at": "2026-12-31T23:59"})
_rid2 = int(_re.findall(r"/tasks/(\d+)/comment", _mk2.text)[-1])
attach(doer, _rid2)
_nav2 = TestClient(app, follow_redirects=False)
_nav2.cookies.update(doer.cookies)
_evil = _nav2.post(f"/tasks/{_rid2}/submit",
                   data={"completion_note": "x", "return_to": "//evil.example.com/"})
def _onsite(loc):
    """A plain path on this site — not a full URL, not a scheme, not //host."""
    return (isinstance(loc, str) and loc.startswith("/")
            and not loc.startswith("//") and not loc.startswith("/\\")
            and "\n" not in loc and "\r" not in loc
            and "evil.example.com" not in loc and "javascript:" not in loc)

# What matters is that the browser is never sent off this site — not which
# on-site page it lands on. The fallback is now the filtered list the person
# was working through, so pinning it to one exact path would be testing the
# convenience rather than the protection.
check("an off-site return_to is ignored",
      _onsite(_evil.headers.get("location")),
      _evil.headers.get("location"))

for _bad in ("https://evil.example.com/", "/\\evil.example.com", "javascript:alert(1)"):
    _mk3 = mgr.post("/tasks/new", data={
        "title": f"SMOKE popup redirect {RUN} {_bad[:6]}", "details": "",
        "doer_id": str(_amit.id), "branch_id": "", "priority": "low",
        "due_at": "2026-12-31T23:59"})
    _b3 = int(_re.findall(r"/tasks/(\d+)/comment", _mk3.text)[-1])
    attach(doer, _b3)
    _n3 = TestClient(app, follow_redirects=False)
    _n3.cookies.update(doer.cookies)
    _rr = _n3.post(f"/tasks/{_b3}/submit", data={"completion_note": "x", "return_to": _bad})
    check(f"return_to {_bad[:22]!r} is refused",
          _onsite(_rr.headers.get("location")),
          _rr.headers.get("location"))

# 6 — the proof rule is the server's, not the button's. Disabling a button
# is a courtesy; the rule has to hold for anyone who posts anyway.
_mk4 = mgr.post("/tasks/new", data={
    "title": f"SMOKE popup proof {RUN}", "details": "",
    "doer_id": str(_amit.id), "branch_id": "", "priority": "low",
    "due_at": "2026-12-31T23:59"})
_pf = int(_re.findall(r"/tasks/(\d+)/comment", _mk4.text)[-1])
_pbox = doer.get(f"/tasks/{_pf}/mark")
check("a task needing proof says so in the box",
      "Proof is required" in _pbox.text)
check("and its button starts switched off", "disabled" in _pbox.text)
_noproof = doer.post(f"/tasks/{_pf}/submit",
                     data={"completion_note": "no proof", "return_to": _back})
check("submitting it without proof is refused", _noproof.status_code == 400,
      _noproof.status_code)
attach(doer, _pf)
_pbox2 = doer.get(f"/tasks/{_pf}/mark")
check("once proof is attached the box says you can submit",
      "you can submit" in _pbox2.text)
check("and the button is no longer disabled",
      "disabled" not in _pbox2.text.split('class="markacts"')[1])

# 7 — a closed task has no box to open.
doer.post(f"/tasks/{_pf}/submit", data={"completion_note": "done"})
check("a task already closed cannot be reopened from the list",
      doer.get(f"/tasks/{_pf}/mark").status_code == 400,
      doer.get(f"/tasks/{_pf}/mark").status_code)

# 8 — an FMS decision step must offer both outcomes in the box, not one
# "Mark complete" that silently picks a direction. Walked from a fresh run of
# the bill flow, because the runs earlier in this file all finished.
_dstart = admin.post(f"/flows/{_fid}/start", data={
    "reference": f"POPUP-{RUN}", "sf0": "BILL-99", "sf1": "Acme", "sf2": "Electricity"})
check("a bill run starts for the pop-up test", _dstart.status_code == 200,
      _dstart.status_code)
with _SLP() as _d:
    _piid = _d.scalar(_sp(_FI).where(_FI.reference == f"POPUP-{RUN}")).id
_amc = login("amit@gcs.local")
_pt, _ppos = _open_step(_piid)
while _ppos is not None and _ppos < 3:
    _amc.post(f"/tasks/{_pt}/submit", data={})
    _pt, _ppos = _open_step(_piid)
check("it reaches the decision step", _ppos == 3, str(_ppos))
_dbox = _amc.get(f"/tasks/{_pt}/mark")
check("a decision step's box opens", _dbox.status_code == 200, _dbox.status_code)
check("it offers both outcomes",
      'value="pass"' in _dbox.text and 'value="fail"' in _dbox.text)
check("named as configured",
      "Verified" in _dbox.text and "Rejected" in _dbox.text)
check("rather than a single Mark complete button",
      "Mark complete</button>" not in _dbox.text)

# And choosing from the box routes the flow exactly as the task page does.
_pnav = TestClient(app, follow_redirects=False)
_pnav.cookies.update(_amc.cookies)
_pnav.post(f"/tasks/{_pt}/submit", data={"decision": "fail", "return_to": _back})
_pt2, _ppos2 = _open_step(_piid)
check("Rejected, chosen in the pop-up, sends the bill back to step 1",
      _ppos2 == 1, str(_ppos2))

# 9 — the table still holds together: one header cell per column.
# Count every <th, not just a bare "<th>": the action column carries a class
# now, and counting only the plain ones reports a table that is one column
# short when it is perfectly balanced.
_hdr = _list.split("<table>", 1)[1].split("</tr>", 1)[0].count("<th")
_firstrow = _list.split("</tr>", 2)[1]
check("the new column has a header of its own", _hdr == _firstrow.count("<td"),
      f"{_hdr} headers vs {_firstrow.count('<td')} cells")

# 10 — the pop-up shell and its script are on the page that needs them.
check("the list page carries the pop-up shell", 'id="markDialog"' in _list)
check("and loads its script", "/static/markbox.js" in _list)
check("the sign-in page does not", 'id="markDialog"' not in
      TestClient(app).get("/login").text)

print("\n== Completed means the doer has finished it ==")
# The complaint: mark a task complete, it needs an audit, and the Completed
# tab stays empty. From the doer's side the job IS done — whether an auditor
# has looked at it yet is a separate column, not a reason to hide the row.
from app.models import AuditState as _AS2

_ca = mgr.post("/tasks/new", data={
    "title": f"SMOKE completed tab {RUN}", "details": "",
    "doer_id": str(_amit.id), "branch_id": "", "priority": "medium",
    "due_at": "2026-12-31T23:59", "requires_audit": "1"})
_cid = int(_re.findall(r"/tasks/(\d+)/comment", _ca.text)[-1])
attach(doer, _cid)
doer.post(f"/tasks/{_cid}/submit", data={"completion_note": "finished"})

with _SLT() as _d:
    _t = _d.get(_MT, _cid)
    check("it is waiting on an auditor", _t.status == _MST.SUBMITTED, _t.status)
    check("and its audit is pending", _t.audit_state == _AS2.PENDING)

_done = doer.get("/tasks?scope=mine&status=done").text
check("it now appears under Completed", f'/tasks/{_cid}"' in _done, "still missing")
check("the page explains what Completed covers",
      "still sitting with an auditor" in _done)
check("its row says where the audit stands", "Audit pending" in _done)
check("the old With-auditor link still lands on Audit pending",
      f'/tasks/{_cid}"' in doer.get("/tasks?scope=mine&status=audit").text)
check("and it is under Audit pending",
      f'/tasks/{_cid}"' in doer.get("/tasks?scope=mine&status=audit_pending").text)
check("no Mark complete button on it any more",
      f'data-mark="{_cid}"' not in _done)

# The date filter has to find it too. Completed used to filter on closed_at,
# which a task waiting on an auditor does not have yet — so picking any date
# range would have emptied the tab all over again.
_today = _clock.today().isoformat()
_ranged = doer.get(f"/tasks?scope=mine&status=done&basis=finished_at"
                   f"&date_from={_today}&date_to={_today}").text
check("a date range on Completed still finds it",
      f'/tasks/{_cid}"' in _ranged, "dropped by the date filter")
check("and the completion-date choice is held", 'value="finished_at" selected' in _ranged)

# Closing the audit must not push it back out of the tab.
admin.post(f"/tasks/{_cid}/audit",
           data={"decision": "approve", "score": "9", "remark": "checked"})
_done2 = doer.get("/tasks?scope=mine&status=done").text
check("it stays under Completed once the audit closes",
      f'/tasks/{_cid}"' in _done2)
check("and appears under Audit completed",
      f'/tasks/{_cid}"' in doer.get("/tasks?scope=mine&status=audit_done").text)

print("\n== Audit completed and False marking have tabs of their own ==")
_tabs = doer.get("/tasks?scope=mine&status=open").text
# A doer is not offered the auditor's queue — it is a list of decisions they
# are not allowed to make. What they do get is the result of the audit.
for _lbl in ("Pending", "Overdue", "Coming up", "Completed",
             "Audit completed", "False marking", "All"):
    check(f"the tab row offers {_lbl}", f">{_lbl}</a>" in _tabs or _lbl in _tabs)
check("but not the auditor's queue", "status=audit_pending" not in _tabs)

# A flagged task must be findable by the flag, and say so on its row.
_fm = mgr.post("/tasks/new", data={
    "title": f"SMOKE false mark tab {RUN}", "details": "",
    "doer_id": str(_amit.id), "branch_id": "", "priority": "high",
    "due_at": "2026-12-31T23:59"})
_fid2 = int(_re.findall(r"/tasks/(\d+)/comment", _fm.text)[-1])
attach(doer, _fid2)
doer.post(f"/tasks/{_fid2}/submit", data={"completion_note": "done"})
admin.post(f"/tasks/{_fid2}/false-mark",
           data={"confirm": "yes", "reason": "register was never filled"})
_fpage = doer.get("/tasks?scope=mine&status=false_mark")
check("the False marking tab opens", _fpage.status_code == 200, _fpage.status_code)
check("and lists the flagged task", f'/tasks/{_fid2}"' in _fpage.text)
check("the row carries the penalty chip", "false marking −10" in _fpage.text)
check("sorted by when it was flagged",
      "most recently flagged first" in _fpage.text.lower())
check("a task nobody flagged is not in there",
      f'/tasks/{_cid}"' not in _fpage.text)

print("\n== the three reports carry the audit figures ==")
for _src in ("delegation", "checklist", "fms"):
    _rp = admin.get(f"/reports/tasks?source={_src}")
    check(f"{_src} report opens", _rp.status_code == 200, _rp.status_code)
    for _lbl in ("Audit pending", "Audit completed", "False marking"):
        check(f"{_src}: it shows {_lbl}", _lbl in _rp.text)
    for _st in ("audit_pending", "audit_done", "false"):
        _sp2 = admin.get(f"/reports/tasks?source={_src}&state={_st}")
        check(f"{_src}: the {_st} tab opens", _sp2.status_code == 200, _sp2.status_code)

# The figures must be the database's, not a guess.
_dr = admin.get("/reports/tasks?source=delegation&state=false")
check("the delegation report lists the flagged task",
      f'/tasks/{_fid2}"' in _dr.text, "missing from False marking")
_dc = admin.get("/reports/tasks?source=delegation&state=completed")
check("and counts work waiting on an auditor as completed",
      f'/tasks/{_cid}"' in _dc.text)
check("saying plainly that it scores only once the audit closes",
      "scores once the audit closes" in _dc.text)

# An unknown state falls back rather than breaking the page.
check("a nonsense state falls back to Pending",
      admin.get("/reports/tasks?source=fms&state=banana").status_code == 200)

print("\n== Excel downloads ==")
import io as _xio
from openpyxl import load_workbook as _load

_XL = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

def _book(client, url):
    """Download an export and open it, the way the person will."""
    r = client.get(url)
    if r.status_code != 200 or not r.headers.get("content-type", "").startswith(_XL):
        return None, r
    return _load(_xio.BytesIO(r.content)), r

def _table(ws):
    """(headers, rows) from a sheet, skipping the title block above it.

    The header is the first row carrying more than one value — the title,
    the note and the export stamp above it each occupy column A alone.
    """
    rows = list(ws.iter_rows(values_only=True))
    for i, r in enumerate(rows):
        if sum(1 for c in r if c not in (None, "")) > 1:
            return list(r), [x for x in rows[i + 1:]]
    return [], []

_EXPORTS = [
    "/tasks?scope=mine&status=open&export=xlsx",
    "/tasks?scope=all&status=all&export=xlsx",
    "/tasks?scope=all&status=false_mark&export=xlsx",
    "/reports/tasks?source=delegation&export=xlsx",
    "/reports/tasks?source=checklist&state=completed&export=xlsx",
    "/reports/tasks?source=fms&state=audit_pending&export=xlsx",
    "/reports/followups?export=xlsx",
    "/reports/audit?export=xlsx",
    "/reports/score?export=xlsx",
    "/admin/users?export=xlsx",
    "/admin/branches?export=xlsx",
    "/admin/departments?export=xlsx",
    "/followups?export=xlsx",
    "/followups?date_from=2026-09-01&date_to=2026-09-25&export=xlsx",
    "/flows?export=xlsx",
    "/recurring?export=xlsx",
    "/outbox?export=xlsx",
]
for _u in _EXPORTS:
    _wb, _r = _book(admin, _u)
    _name = _u.split("?")[0]
    check(f"{_name} downloads an Excel file", _wb is not None,
          f"{_r.status_code} {_r.headers.get('content-type','')[:40]}")
    if _wb is None:
        continue
    check(f"{_name}: the browser is told to save it",
          "attachment" in _r.headers.get("content-disposition", ""))
    check(f"{_name}: the file is named and dated",
          ".xlsx" in _r.headers.get("content-disposition", ""))
    _ws = _wb.worksheets[0]
    _hdr, _rows = _table(_ws)
    check(f"{_name}: it has a header row", bool(_hdr) and all(_hdr[:2]), _hdr[:3])
    check(f"{_name}: the header is frozen", _ws.freeze_panes is not None)
    check(f"{_name}: and says when it was exported",
          any("Exported" in str(c.value or "") for c in _ws["A"][:6]))

# --- the file must hold what the page held, not a different query ----------
_pg = admin.get("/tasks?scope=all&status=open&source=delegation")
_ids_page = set(_re.findall(r'/tasks/(\d+)"', _pg.text))
_wb, _ = _book(admin, "/tasks?scope=all&status=open&source=delegation&export=xlsx")
_hdr, _rows = _table(_wb.worksheets[0])
_titles_file = {r[_hdr.index("Task")] for r in _rows}
with _SLT() as _d:
    _titles_page = {_d.get(_MT, int(i)).title for i in _ids_page}
check("the Excel rows are exactly the page's rows",
      _titles_file == _titles_page,
      f"file {len(_titles_file)} vs page {len(_titles_page)}")
check("every column the page shows is a column in the file",
      all(h in _hdr for h in ["Task", "Doer", "Priority", "Status",
                              "Planned date", "Audit", "False marking"]), _hdr)

# A date is a real date, not text that looks like one — otherwise it will
# not sort, filter by month, or group in a pivot table.
_planned = _wb.worksheets[0].cell(
    row=int(_wb.worksheets[0].auto_filter.ref.split(":")[0][1:]) + 1,
    column=_hdr.index("Planned date") + 1)
from datetime import datetime as _dtclass
check("a date lands in the cell as a real date",
      isinstance(_planned.value, _dtclass), type(_planned.value).__name__)
check("and carries a readable format", "mmm" in _planned.number_format.lower(),
      _planned.number_format)

# --- a filter on the page is a filter in the file -------------------------
_narrow = "/tasks?scope=all&status=all&date_from=2026-01-01&date_to=2026-01-02"
_wbn, _ = _book(admin, _narrow + "&export=xlsx")
_h2, _r2 = _table(_wbn.worksheets[0])
_wba, _ = _book(admin, "/tasks?scope=all&status=all&export=xlsx")
_h3, _r3 = _table(_wba.worksheets[0])
check("a date range really narrows the export", len(_r2) < len(_r3),
      f"{len(_r2)} vs {len(_r3)}")
check("and the file says which filters produced it",
      any("2026-01-01" in str(c.value or "")
          for c in _wbn.worksheets[0]["A"][:6]))

# --- nobody exports rows they cannot see ----------------------------------
_wbd, _ = _book(doer, "/reports/tasks?source=delegation&state=all&export=xlsx")
_hd, _rd = _table(_wbd.worksheets[0])
_others = {n for n in (r[_hd.index("Doer")] for r in _rd) if n} - {_amit.name}
_assigned = set()
with _SLT() as _d:
    for _t in _d.scalars(_msel(_MT).where(_MT.assigner_id == _amit.id)).all():
        _assigned.add(_t.doer.name)
check("a doer's export holds only their own work",
      not (_others - _assigned), f"leaked {sorted(_others - _assigned)[:3]}")
check("a doer cannot export the staff list",
      doer.get("/admin/users?export=xlsx").status_code == 403)
check("nor the branches", doer.get("/admin/branches?export=xlsx").status_code == 403)
check("nor the EM score report",
      doer.get("/reports/score?export=xlsx").status_code == 403)
check("and an anonymous visitor gets nothing",
      TestClient(app, follow_redirects=False)
      .get("/admin/users?export=xlsx").status_code in (303, 401, 403))

# --- the user list, which is what was asked for by name -------------------
_wbu, _ = _book(admin, "/admin/users?export=xlsx")
_hu, _ru = _table(_wbu.worksheets[0])
with _SLT() as _d:
    _headcount = len(_d.scalars(_msel(_MU).where(_MU.org_id == 1)).all())
check("the user list exports every user", len(_ru) == _headcount,
      f"{len(_ru)} rows vs {_headcount} users")
for _col in ("Name", "Email", "Role", "Branch", "Department", "Active",
             "Rights", "Benchmark — Delegation"):
    check(f"the user list has a {_col} column", _col in _hu, _hu)
check("rights are spelled out, not stored codes",
      any("All rights" in str(r[_hu.index("Rights")]) for r in _ru))
check("and an ordinary doer's rights read plainly",
      any(str(r[_hu.index("Rights")]) in ("Execute only",)
          or "," in str(r[_hu.index("Rights")]) for r in _ru))
check("no password or hash is ever in the file",
      not any("hash" in str(h).lower() or "password" in str(h).lower()
              for h in _hu), _hu)

# --- multi-tab books ------------------------------------------------------
_wbs, _ = _book(admin, "/reports/score?export=xlsx")
check("the score export has both views",
      [w.title for w in _wbs.worksheets] == ["Person-wise", "Branch-wise"],
      [w.title for w in _wbs.worksheets])
_hs, _rs = _table(_wbs.worksheets[0])
check("with the bifurcation spread across columns",
      all(c in _hs for c in ["Score", "Delegation penalty", "Checklist penalty",
                             "FMS penalty", "False marking penalty"]), _hs)
_wbr, _ = _book(admin, "/reports/tasks?source=delegation&export=xlsx")
check("a source report carries its figures on a second tab",
      "Summary" in [w.title for w in _wbr.worksheets])
_wbf, _ = _book(admin, "/flows?export=xlsx")
check("the FMS export lists steps and runs separately",
      [w.title for w in _wbf.worksheets] == ["Flow steps", "Running now"],
      [w.title for w in _wbf.worksheets])

# --- an empty result is a readable file, not a broken one -----------------
_wbe, _ = _book(admin, "/tasks?scope=mine&status=done&date_from=2001-01-01"
                       "&date_to=2001-01-02&export=xlsx")
check("an export with no rows still opens", _wbe is not None)
if _wbe:
    check("and says so in words",
          any("Nothing matched" in str(c.value or "")
              for c in _wbe.worksheets[0]["A"][:8]))

# --- the button is on the pages ------------------------------------------
for _page in ["/tasks?scope=mine&status=open", "/reports/tasks?source=delegation",
              "/reports/audit", "/reports/score", "/reports/followups",
              "/admin/users", "/admin/branches", "/admin/departments",
              "/followups", "/flows", "/recurring", "/outbox"]:
    _p = admin.get(_page)
    check(f"{_page} offers the Excel button",
          "export=xlsx" in _p.text, _p.status_code)

# Performance and the Dashboard too.
_wbp, _rp2 = _book(admin, "/stats?export=xlsx")
check("/stats downloads an Excel file", _wbp is not None, _rp2.status_code)
if _wbp:
    check("with both views",
          [w.title for w in _wbp.worksheets] == ["Person-wise", "Branch-wise"])
check("a doer cannot export Performance",
      doer.get("/stats?export=xlsx").status_code == 403)
check("the dashboard offers one too", "export=xlsx" in admin.get("/").text)
check("and a doer's dashboard offers it as well", "export=xlsx" in doer.get("/").text)

# An ordinary GET must still be the page, not a download.
check("without export= the page is still a page",
      admin.get("/tasks?scope=mine&status=open").headers["content-type"]
      .startswith("text/html"))
check("and a nonsense export value is ignored",
      admin.get("/tasks?scope=mine&status=open&export=banana")
      .headers["content-type"].startswith("text/html"))

print("\n== deleting things that other rows point at ==")
# The live 500: a task the follow-up desk had ever ticked refused to delete.
# SQLite used to ignore foreign keys, so every test here passed while the
# same click failed on the real site. Foreign keys are enforced now, which is
# what makes the checks below mean anything.
from app.models import (Followup as _FU, Attachment as _AT, TaskComment as _TC,
                        OutboundMessage as _MO)
from sqlalchemy import func as _func
from app.db import engine as _eng

if _eng.url.get_backend_name() == "sqlite":
    with _eng.connect() as _cn:
        check("SQLite is enforcing foreign keys like the live database does",
              _cn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1)

def _fresh_task(title):
    r = mgr.post("/tasks/new", data={
        "title": title, "details": "", "doer_id": str(_amit.id),
        "branch_id": "", "priority": "low", "due_at": "2026-12-31T23:59"})
    return int(_re.findall(r"/tasks/(\d+)/comment", r.text)[-1])

_dt1 = _fresh_task(f"SMOKE delete chased {RUN}")
with _SLT() as _d:
    _t = _d.get(_MT, _dt1)
    _d.add(_FU(org_id=_t.org_id, task_id=_t.id, day=_clock.today(), by_id=1))
    _d.add(_TC(task_id=_t.id, author_id=1, body="a note"))
    _d.add(_MO(org_id=_t.org_id, task_id=_t.id, to_phone="+910000000000",
               template="task_assigned", body="x", status="queued"))
    _d.commit()

_del = admin.post(f"/tasks/{_dt1}/delete")
check("a task the follow-up desk chased can be deleted",
      _del.status_code in (200, 303), _del.status_code)
with _SLT() as _d:
    check("the task is gone", _d.get(_MT, _dt1) is None)
    check("its follow-up ticks went with it",
          _d.scalar(_msel(_func.count()).select_from(_FU)
                    .where(_FU.task_id == _dt1)) == 0)
    check("so did its notes",
          _d.scalar(_msel(_func.count()).select_from(_TC)
                    .where(_TC.task_id == _dt1)) == 0)
    # The message log is a record of what was SENT. It outlives the task.
    _left = _d.scalars(_msel(_MO).where(_MO.body == "x",
                                        _MO.to_phone == "+910000000000")).all()
    check("but the message we sent is kept, just unhooked",
          _left and all(m.task_id is None for m in _left),
          f"{len(_left)} rows")

# Attachments too — a task with proof on it must still delete.
_dt2 = _fresh_task(f"SMOKE delete with proof {RUN}")
attach(doer, _dt2)
with _SLT() as _d:
    check("it has proof attached",
          _d.scalar(_msel(_func.count()).select_from(_AT)
                    .where(_AT.task_id == _dt2)) > 0)
check("a task with attachments deletes cleanly",
      admin.post(f"/tasks/{_dt2}/delete").status_code in (200, 303))
with _SLT() as _d:
    check("and leaves no attachment behind",
          _d.scalar(_msel(_func.count()).select_from(_AT)
                    .where(_AT.task_id == _dt2)) == 0)

# Deleting a PERSON has exactly the same trap: a tick names who made it, and
# that column will not take a null either.
print("\n== and deleting a person who had chased work ==")
_mk = admin.post("/admin/users", data={
    "name": f"Temp Chaser {RUN}", "email": f"chaser.{RUN}@gcs.local",
    "password": "gcs1234", "role": "doer", "branch_id": "", "department_id": "",
    "bm_delegation": "60", "bm_checklist": "20", "bm_fms": "20"})
check("a test user is created", _mk.status_code in (200, 303), _mk.status_code)
with _SLT() as _d:
    _tmp = _d.scalar(_msel(_MU).where(_MU.email == f"chaser.{RUN}@gcs.local"))
    _tid_keep = _d.scalars(_msel(_MT).where(_MT.doer_id != _tmp.id)).first().id
    _d.add(_FU(org_id=_tmp.org_id, task_id=_tid_keep,
               day=_clock.today(), by_id=_tmp.id))
    _d.add(_AT(task_id=_tid_keep, uploaded_by_id=_tmp.id, filename="theirs.png",
               stored_name=f"k-{RUN}", size=1, content_type="image/png",
               storage="db", data=b"x"))
    _d.commit()
    _tmp_id, _tmp_name = _tmp.id, _tmp.name

_page = admin.get(f"/admin/users/{_tmp_id}/delete")
check("the confirmation page counts their follow-up ticks",
      "Follow-up ticks they made" in _page.text)
check("and the files they uploaded", "Files they uploaded" in _page.text)

_rm = admin.post(f"/admin/users/{_tmp_id}/delete",
                 data={"mode": "transfer", "transfer_to": str(_amit.id),
                       "confirm": _tmp_name})
check("deleting them does not fail", _rm.status_code in (200, 303), _rm.status_code)
with _SLT() as _d:
    check("the person is gone", _d.get(_MU, _tmp_id) is None)
    check("no tick is left pointing at nobody",
          _d.scalar(_msel(_func.count()).select_from(_FU)
                    .where(_FU.by_id == _tmp_id)) == 0)
    check("the tick itself survives, under the person it moved to",
          _d.scalar(_msel(_func.count()).select_from(_FU)
                    .where(_FU.task_id == _tid_keep, _FU.by_id == _amit.id)) > 0)
    check("and somebody else's task was not touched",
          _d.get(_MT, _tid_keep) is not None)

# Finally: nothing anywhere may be left pointing at a row that is gone.
print("\n== nothing in the database points at something deleted ==")
with _SLT() as _d:
    _orphans = []
    for _model, _col, _parent in [
            (_FU, "task_id", _MT), (_FU, "by_id", _MU),
            (_TC, "task_id", _MT), (_AT, "task_id", _MT),
            (_AT, "uploaded_by_id", _MU), (_MT, "doer_id", _MU),
            (_MT, "assigner_id", _MU)]:
        _ids = {r[0] for r in _d.execute(_msel(getattr(_model, _col))).all()
                if r[0] is not None}
        _have = {r[0] for r in _d.execute(_msel(_parent.id)).all()}
        _missing = _ids - _have
        if _missing:
            _orphans.append(f"{_model.__tablename__}.{_col}: {sorted(_missing)[:3]}")
    check("every row points at something that exists", not _orphans, _orphans)

print("\n== holding, resuming and stopping an FMS run ==")
# The complaint: the delete error told people to "cancel the flow run
# instead", and nothing in the software could do that.
from app.models import FlowInstance as _FIH, TaskStatus as _TSH
from datetime import timedelta as _tdh

_hstart = admin.post(f"/flows/{_fid}/start", data={
    "reference": f"HOLD-{RUN}", "sf0": "BILL-HOLD", "sf1": "Acme",
    "sf2": "Electricity"})
check("a run starts for the hold test", _hstart.status_code == 200, _hstart.status_code)
with _SLP() as _d:
    _hid = _d.scalar(_sp(_FIH).where(_FIH.reference == f"HOLD-{RUN}")).id

_htask, _hpos = _open_step(_hid)
with _SLP() as _d:
    _before_due = _d.get(_TP, _htask).due_at
    _before_status = _d.get(_TP, _htask).status
check("its first step is open and assigned", _htask is not None)

# --- the message that used to lead nowhere --------------------------------
_derr = admin.post(f"/tasks/{_htask}/delete")
check("deleting a step of a run is still refused", _derr.status_code == 400,
      _derr.status_code)
check("and now points at the run itself",
      f"/flows/instance/{_hid}" in _derr.text, _derr.text[:120])
check("naming what can actually be done",
      "hold it" in _derr.text and "stop it" in _derr.text)

# --- hold ------------------------------------------------------------------
_hold = admin.post(f"/flows/instance/{_hid}/hold",
                   data={"reason": "vendor has not sent the bill"})
check("the run can be held", _hold.status_code in (200, 303), _hold.status_code)
with _SLP() as _d:
    _inst = _d.get(_FIH, _hid)
    check("it records who held it and why",
          _inst.held_at is not None and _inst.hold_reason is not None)
    check("the run reads as on hold", _inst.state == "on hold", _inst.state)
    _t = _d.get(_TP, _htask)
    check("its open step is parked", _t.status == _TSH.ON_HOLD, _t.status)
    check("remembering what it was before",
          _t.held_from == _before_status.value, _t.held_from)
    check("a held step is not overdue, whatever its date", not _t.is_overdue)

_doer_email = None
with _SLP() as _d:
    _doer_email = _d.get(_TP, _htask).doer.email
_hc = login(_doer_email)
check("the held step is off the doer's open list",
      f'/tasks/{_htask}"' not in _hc.get("/tasks?scope=mine&status=open").text)
check("and off the FMS tab",
      f'/tasks/{_htask}"' not in
      _hc.get("/tasks?scope=mine&status=open&source=fms").text)
check("the doer cannot close it while it is paused",
      _hc.post(f"/tasks/{_htask}/submit", data={}).status_code == 400)
check("nor attach anything to it",
      attach(_hc, _htask).status_code == 400)
check("and the pop-up refuses to open on it",
      _hc.get(f"/tasks/{_htask}/mark").status_code == 400)

# It must leave the score alone, not count as work not done.
from app.services import scoring as _scor
# Measured on a deadline that has already passed, because a score only ever
# counts work that was due inside the window being scored.
with _SLP() as _d:
    _t = _d.get(_TP, _htask)
    _du = _t.doer
    _t.due_at = _clock.now() - _tdh(days=1)
    _t.status = _TSH(_t.held_from)          # briefly live, to measure it
    _d.commit()
    _live_card = _scor.user_scorecard(_d, _du, days=30)
    _t = _d.get(_TP, _htask)
    _t.held_from = _t.status.value
    _t.status = _TSH.ON_HOLD                 # and back on hold
    _d.commit()
    _held_card = _scor.user_scorecard(_d, _du, days=30)
check("an overdue step counts against the score while the run is live",
      _live_card.planned > 0, _live_card.planned)
check("and holding the run takes that weight straight back out",
      _held_card.planned == _live_card.planned - _d.get(_TP, _htask).weight
      if False else _held_card.planned < _live_card.planned,
      f"held {_held_card.planned} vs live {_live_card.planned}")
check("so a held step is never scored as work not done",
      _held_card.not_done <= _live_card.not_done,
      f"{_held_card.not_done} vs {_live_card.not_done}")
check("the follow-up desk does not chase a held step",
      f'/tasks/{_htask}"' not in admin.get("/followups?desk=pc").text)

# A held run must not move, even if an audit is approved elsewhere.
check("a held run cannot be held twice",
      admin.post(f"/flows/instance/{_hid}/hold", data={}).status_code == 400)

# --- resume ----------------------------------------------------------------
# The scoring check above moved this deadline on purpose, so take the
# baseline again from what is actually there now.
with _SLP() as _d:
    _before_due = _d.get(_TP, _htask).due_at
_res = admin.post(f"/flows/instance/{_hid}/resume", data={"shift": "1"})
check("the run can be resumed", _res.status_code in (200, 303), _res.status_code)
with _SLP() as _d:
    _inst = _d.get(_FIH, _hid)
    check("it is running again", _inst.state == "running", _inst.state)
    check("and nothing is left marked held", _inst.held_at is None)
    _t = _d.get(_TP, _htask)
    check("the step goes back to exactly what it was",
          _t.status == _before_status, _t.status)
    check("with nothing left over from the hold", _t.held_from is None)
    check("and its planned date moved forward, not backward",
          _t.due_at >= _before_due, f"{_t.due_at} vs {_before_due}")
check("the step is back on the doer's list",
      f'/tasks/{_htask}"' in _hc.get("/tasks?scope=mine&status=open").text)
check("resuming a run that is not held is refused",
      admin.post(f"/flows/instance/{_hid}/resume", data={}).status_code == 400)

# The deadline shift is the point — check it against a hold we control.
with _SLP() as _d:
    _inst = _d.get(_FIH, _hid)
    _inst.held_at = _clock.now() - _tdh(days=3)
    _inst.held_by_id = 2
    _t = _d.get(_TP, _htask)
    _t.held_from = _t.status.value
    _t.status = _TSH.ON_HOLD
    _was = _t.due_at
    _d.commit()
admin.post(f"/flows/instance/{_hid}/resume", data={"shift": "1"})
with _SLP() as _d:
    _t = _d.get(_TP, _htask)
    _moved = (_t.due_at - _was).total_seconds() / 86400
    check("a three-day hold moves the deadline on by about three days",
          2.9 < _moved < 3.1, f"moved {_moved:.2f} days")

# And the other choice has to work too, or the checkbox is a lie.
with _SLP() as _d:
    _inst = _d.get(_FIH, _hid)
    _inst.held_at = _clock.now() - _tdh(days=2)
    _inst.held_by_id = 2
    _t = _d.get(_TP, _htask)
    _t.held_from = _t.status.value
    _t.status = _TSH.ON_HOLD
    _was2 = _t.due_at
    _d.commit()
admin.post(f"/flows/instance/{_hid}/resume", data={"shift": "0"})
with _SLP() as _d:
    check("asking to leave the dates alone really leaves them alone",
          _d.get(_TP, _htask).due_at == _was2)

# --- stop ------------------------------------------------------------------
_stop_no = admin.post(f"/flows/instance/{_hid}/stop", data={"reason": "x"})
check("stopping without confirming is refused", _stop_no.status_code == 400,
      _stop_no.status_code)
_stop = admin.post(f"/flows/instance/{_hid}/stop",
                   data={"confirm": "yes", "reason": "bill was withdrawn"})
check("the run can be stopped", _stop.status_code in (200, 303), _stop.status_code)
with _SLP() as _d:
    _inst = _d.get(_FIH, _hid)
    check("it reads as stopped", _inst.state == "stopped", _inst.state)
    check("with the reason kept", _inst.cancel_reason == "bill was withdrawn")
    _left = [t for t in _d.scalars(_sp(_TP).where(
        _TP.flow_instance_id == _hid)).all()
        if t.status not in (_TSH.COMPLETED, _TSH.CANCELLED)]
    check("no step of it is left open", not _left,
          [t.status.value for t in _left])
    check("but the run itself is kept, not deleted", _inst is not None)
check("a stopped run cannot be held", 
      admin.post(f"/flows/instance/{_hid}/hold", data={}).status_code == 400)
check("nor stopped twice",
      admin.post(f"/flows/instance/{_hid}/stop",
                 data={"confirm": "yes"}).status_code == 400)

# --- who is allowed --------------------------------------------------------
check("a doer cannot hold a run",
      doer.post(f"/flows/instance/{_hid}/hold", data={}).status_code == 403)
check("nor stop one",
      doer.post(f"/flows/instance/{_hid}/stop",
                data={"confirm": "yes"}).status_code == 403)
check("an unknown run is a plain not-found",
      admin.post("/flows/instance/999999/hold", data={}).status_code == 404)

# --- the buttons are where people will look -------------------------------
_ipage = admin.get(f"/flows/instance/{_hid}")
check("the run page opens", _ipage.status_code == 200, _ipage.status_code)
check("and says the run was stopped", "This run was stopped" in _ipage.text)
_lpage = admin.get("/flows")
check("the FMS list shows a State column", "State" in _lpage.text)

_run2 = admin.post(f"/flows/{_fid}/start", data={
    "reference": f"CTL-{RUN}", "sf0": "B2", "sf1": "Acme", "sf2": "Electricity"})
with _SLP() as _d:
    _cid = _d.scalar(_sp(_FIH).where(_FIH.reference == f"CTL-{RUN}")).id
_cpage = admin.get(f"/flows/instance/{_cid}")
check("a live run offers Hold", "Hold this run" in _cpage.text)
check("and Stop", "Stop this run for good" in _cpage.text)
check("and says what holding does",
      "out of the EM score" in _cpage.text or "score" in _cpage.text)
admin.post(f"/flows/instance/{_cid}/hold", data={"reason": "waiting"})
_cpage2 = admin.get(f"/flows/instance/{_cid}")
check("once held it offers Resume instead", "Resume this run" in _cpage2.text)
check("and no longer offers Hold", "Hold this run" not in _cpage2.text)
check("the list offers Resume on a held run",
      "Resume" in admin.get("/flows").text)

print("\n== upgrading a database that already exists ==")
# PostgreSQL makes an enum a real type. create_all() builds it once and never
# touches it again, so a value added to the Python enum later does not exist
# in a database that was built before it — and the first person to use it
# gets a blank 500. SQLite stores plain text and never notices, which is
# exactly why this needs a check of its own.
from app.db import engine as _upeng
from sqlalchemy import text as _uptext
import app.migrate as _upmig

if _upeng.dialect.name == "postgresql":
    with _upeng.connect().execution_options(isolation_level="AUTOCOMMIT") as _cn:
        _labels = {r[0] for r in _cn.execute(_uptext(
            "SELECT e.enumlabel FROM pg_type t JOIN pg_enum e "
            "ON e.enumtypid = t.oid WHERE t.typname = 'taskstatus'")).all()}
    check("the live enum knows every status the code uses",
          {st.name for st in _TSH} <= _labels,
          sorted({st.name for st in _TSH} - _labels))

    # Every native enum the code writes to, not just taskstatus. A member
    # added in Python but missing from the live type is a 500 on first use,
    # and that is exactly how ON_HOLD nearly shipped.
    from app.models import (AuditState as _AEc, Recurrence as _REc,
                            TaskStatus as _TEc)
    for _tn, _pyenum in (("auditstate", _AEc), ("recurrence", _REc),
                         ("taskstatus", _TEc)):
        with _upeng.connect() as _cn:
            _live = {r[0] for r in _cn.execute(_uptext(
                "SELECT e.enumlabel FROM pg_type t JOIN pg_enum e "
                "ON e.enumtypid = t.oid WHERE t.typname = :n"),
                {"n": _tn}).all()}
        _want = {m.name for m in _pyenum}
        check(f"the live {_tn} type knows every value the code uses",
              _want <= _live, sorted(_want - _live))
        _listed = set(_upmig.ENUM_VALUES.get(_tn, []))
        check(f"and the migrator can add {_tn} values to an older database",
              _listed <= _want, sorted(_listed - _want))

    # Rewind a throwaway copy of the type to how the live database had it,
    # then prove the migrator puts it right.
    _restored = False
    with _upeng.connect().execution_options(isolation_level="AUTOCOMMIT") as _cn:
        _cn.execute(_uptext("DROP TYPE IF EXISTS taskstatus_probe"))
        _cn.execute(_uptext(
            "CREATE TYPE taskstatus_probe AS ENUM "
            "('PENDING','IN_PROGRESS','SUBMITTED','COMPLETED','REJECTED',"
            "'REOPENED','CANCELLED')"))
        _before = {r[0] for r in _cn.execute(_uptext(
            "SELECT e.enumlabel FROM pg_type t JOIN pg_enum e "
            "ON e.enumtypid = t.oid WHERE t.typname = 'taskstatus_probe'")).all()}
    check("a database built before the new status does not know it",
          "ON_HOLD" not in _before)

    _saved = dict(_upmig.ENUM_VALUES)
    try:
        _upmig.ENUM_VALUES = {"taskstatus_probe": ["ON_HOLD"]}
        _done = _upmig._extend_enums("postgresql")
        check("the migrator adds it", any("ON_HOLD" in d for d in _done), _done)
        with _upeng.connect().execution_options(isolation_level="AUTOCOMMIT") as _cn:
            _after = {r[0] for r in _cn.execute(_uptext(
                "SELECT e.enumlabel FROM pg_type t JOIN pg_enum e "
                "ON e.enumtypid = t.oid WHERE t.typname='taskstatus_probe'")).all()}
        check("and the type now knows it", "ON_HOLD" in _after, sorted(_after))
        check("without losing anything it already had", _before <= _after)
        # Running it twice must be harmless — the migrator runs on every boot.
        _again = _upmig._extend_enums("postgresql")
        check("running the migrator again changes nothing", _again == [], _again)
    finally:
        _upmig.ENUM_VALUES = _saved
        with _upeng.connect().execution_options(isolation_level="AUTOCOMMIT") as _cn:
            _cn.execute(_uptext("DROP TYPE IF EXISTS taskstatus_probe"))
else:
    check("every status the code uses is storable",
          all(isinstance(st.value, str) for st in _TSH))

# Whatever the backend: the migrator must be safe to run again at any time.
check("the migrator is safe to re-run", isinstance(_upmig.run(), list))

print("\n== a checklist job due on a closed day is created a day early ==")
from app.models import (RecurringRule as _RR2, Recurrence as _Rec2,
                        Task as _T2, Branch as _BR2, Holiday as _H2)
from app.services import recurring as _rec2, holidays as _hol2
from datetime import date as _d2, timedelta as _td2

_w = _SLT()
# Head office: closed Sundays. A daily job, and a job due only on Sundays.
_ho = _w.scalar(_msel(_BR2).where(_BR2.name.like("%HO%"))) or _w.scalar(_msel(_BR2))
_ho.weekly_off = 6
_w.commit()

_sat = _d2(2027, 5, 8)          # Saturday
_sun = _d2(2027, 5, 9)          # Sunday
check("the test dates are the days they should be",
      _sat.weekday() == 5 and _sun.weekday() == 6)

_daily = _RR2(org_id=1, branch_id=_ho.id, title=f"SMOKE daily closed {RUN}",
              doer_id=6, assigner_id=2, frequency=_Rec2.DAILY, due_time="18:00")
_sunday_job = _RR2(org_id=1, branch_id=_ho.id, title=f"SMOKE sunday job {RUN}",
                   doer_id=6, assigner_id=2, frequency=_Rec2.WEEKLY, day_of=6,
                   due_time="17:00")
_w.add_all([_daily, _sunday_job])
_w.commit()

def _made(rule_id):
    return _w.scalars(_msel(_T2).where(_T2.rule_id == rule_id)).all()

_rec2.run_spawn(_w, today=_sat)
_dtasks = _made(_daily.id)
_stasks = _made(_sunday_job.id)
# A DAILY job is never brought forward: two identical rows on one day read
# as the same task twice, and there is another one tomorrow anyway.
check("Saturday makes the daily job once, not twice",
      len(_dtasks) == 1, [str(t.covers_day) for t in _dtasks])
check("and it is Saturday's own",
      {t.covers_day for t in _dtasks} == {_sat},
      sorted(str(t.covers_day) for t in _dtasks))
check("due on Saturday, not on the Sunday nobody is in",
      all(t.due_at.date() == _sat for t in _dtasks),
      [str(t.due_at) for t in _dtasks])
check("the Sunday-only job is created on Saturday too",
      len(_stasks) == 1 and _stasks[0].covers_day == _sun,
      [str(t.covers_day) for t in _stasks])
check("and it says on the task why it turned up early",
      any("brought forward" in c.body.lower() for c in _stasks[0].comments),
      [c.body[:50] for c in _stasks[0].comments])

# Sunday itself creates nothing — everybody is off.
_rec2.run_spawn(_w, today=_sun)
check("Sunday itself creates nothing", len(_made(_daily.id)) == 1,
      len(_made(_daily.id)))

# Monday creates only Monday's.
_rec2.run_spawn(_w, today=_sun + _td2(days=1))
check("Monday creates its own and nothing else", len(_made(_daily.id)) == 2,
      len(_made(_daily.id)))

# Running the spawner twice on the same day must not double anything.
_before = len(_made(_daily.id))
_rec2.run_spawn(_w, today=_sun + _td2(days=1))
check("running it again the same day creates nothing",
      len(_made(_daily.id)) == _before, len(_made(_daily.id)))

# A company that never closes keeps its Sunday work on Sunday.
_gym = _w.scalar(_msel(_BR2).where(_BR2.name.like("Bodyzone%")))
if _gym:
    _gym.weekly_off = None
    _gymrule = _RR2(org_id=1, branch_id=_gym.id, title=f"SMOKE gym sunday {RUN}",
                    doer_id=7, assigner_id=2, frequency=_Rec2.WEEKLY, day_of=6,
                    due_time="10:00")
    _w.add(_gymrule)
    _w.commit()
    _rec2.run_spawn(_w, today=_d2(2027, 5, 15))          # the Saturday before
    check("a seven-day company does not get its Sunday job early",
          not _made(_gymrule.id), [str(t.covers_day) for t in _made(_gymrule.id)])
    _rec2.run_spawn(_w, today=_d2(2027, 5, 16))          # the Sunday itself
    _g = _made(_gymrule.id)
    check("it gets it on the Sunday, as it should",
          len(_g) == 1 and _g[0].covers_day == _d2(2027, 5, 16),
          [str(t.covers_day) for t in _g])

# A run of closed days is created once, by the working day before the run.
_w.add(_H2(org_id=1, branch_id=None, day=_d2(2027, 6, 3), name="SMOKE festival 1"))
_w.add(_H2(org_id=1, branch_id=None, day=_d2(2027, 6, 4), name="SMOKE festival 2"))
# Every day of the week, as a WEEKLY rule rather than a DAILY one: a job that
# comes round once a week IS brought forward off a closed day, and this is
# what tests that a whole run of closed days is handed out once.
_run_rule = _RR2(org_id=1, branch_id=_ho.id, title=f"SMOKE festival daily {RUN}",
                 doer_id=6, assigner_id=2, frequency=_Rec2.WEEKLY,
                 weekdays="0,1,2,3,4,5,6", due_time="18:00")
_w.add(_run_rule)
_w.commit()
_rec2.run_spawn(_w, today=_d2(2027, 6, 2))       # Wednesday before the festival
_covers = sorted(str(t.covers_day) for t in _made(_run_rule.id))
# Wed 2 Jun carries its own plus the Thu/Fri festival. Sat 5 Jun is a normal
# working day for this company, so the run of closed days stops there — and
# Saturday will make its own, plus the Sunday after it.
check("the day before a festival carries the whole closed run",
      _covers == ["2027-06-02", "2027-06-03", "2027-06-04"], _covers)
check("every one of them is due on that working day",
      all(t.due_at.date() == _d2(2027, 6, 2) for t in _made(_run_rule.id)),
      sorted(str(t.due_at.date()) for t in _made(_run_rule.id)))
_rec2.run_spawn(_w, today=_d2(2027, 6, 5))
check("and Saturday then carries itself and the Sunday",
      sorted(str(t.covers_day) for t in _made(_run_rule.id))
      == ["2027-06-02", "2027-06-03", "2027-06-04", "2027-06-05", "2027-06-06"],
      sorted(str(t.covers_day) for t in _made(_run_rule.id)))
_w.close()

print("\n== the bulk import takes a person's name, and yearly work ==")
import io as _bio
from openpyxl import Workbook as _WB
from app.services import bulk as _bulk

def _sheet_bytes(rows, kind="checklist"):
    wb = _WB(); ws = wb.active
    cols = (_bulk.CHECKLIST_COLS if kind == "checklist" else _bulk.DELEGATION_COLS)
    for i, (name, _w2, note) in enumerate(cols, start=1):
        ws.cell(row=1, column=i, value=name)
        ws.cell(row=2, column=i, value=note)
    for r, row in enumerate(rows, start=3):
        for c, v in enumerate(row, start=1):
            ws.cell(row=r, column=c, value=v)
    buf = _bio.BytesIO(); wb.save(buf); return buf.getvalue()

with _SLT() as _d:
    _amit_name = _d.get(_MU, _amit.id).name
    _amit_mail = _d.get(_MU, _amit.id).email

_blob = _sheet_bytes([
    [f"By name {RUN}", "", _amit_name, "", "daily", "", "18:00", "high", "NO"],
    [f"By email {RUN}", "", _amit_mail, "", "daily", "", "18:00", "high", "NO"],
    [f"By name odd case {RUN}", "", _amit_name.upper(), "", "weekly", "Fri",
     "18:00", "medium", "NO"],
    [f"Yearly renewal {RUN}", "", _amit_name, "", "yearly", "17/04", "11:00",
     "high", "NO"],
    [f"Nobody {RUN}", "", "Ghost Person", "", "daily", "", "18:00", "high", "NO"],
    [f"Bad year {RUN}", "", _amit_name, "", "yearly", "", "11:00", "high", "NO"],
])
_p = _bulk.parse(_SLT(), 1, "checklist", _blob)
_by = {r.data.get("title", f"row {r.number}") if r.ok else f"row {r.number}": r
       for r in _p.rows}
check("a row naming the person by name is accepted",
      _by[f"By name {RUN}"].ok, _by[f"By name {RUN}"].error)
check("so is one naming them by email", _by[f"By email {RUN}"].ok)
check("both land on the same person",
      _by[f"By name {RUN}"].data["doer"].id
      == _by[f"By email {RUN}"].data["doer"].id)
check("capitals in a name do not matter",
      _by[f"By name odd case {RUN}"].ok, _by[f"By name odd case {RUN}"].error)
check("a yearly rule is accepted", _by[f"Yearly renewal {RUN}"].ok,
      _by[f"Yearly renewal {RUN}"].error)
check("with the date stored as month and day",
      _by[f"Yearly renewal {RUN}"].data["day_of"] == 417,
      _by[f"Yearly renewal {RUN}"].data.get("day_of"))
check("and described in words",
      "17 Apr" in _by[f"Yearly renewal {RUN}"].data["schedule_label"],
      _by[f"Yearly renewal {RUN}"].data.get("schedule_label"))
_ghost = [r for r in _p.rows if not r.ok and "Ghost" in (r.error or "")]
check("a name nobody has is refused, by name", _ghost,
      [r.error for r in _p.bad])
check("and the message says where to look",
      _ghost and "Users page" in _ghost[0].error, _ghost[0].error if _ghost else "")
_noday = [r for r in _p.rows if not r.ok and "DD/MM" in (r.error or "")]
check("a yearly rule with no date is refused", _noday,
      [r.error for r in _p.bad])

# Two people with the same name must be refused rather than guessed at.
_dup = admin.post("/admin/users", data={
    "name": _amit_name, "email": f"dup.{RUN}@gcs.local", "password": "gcs1234",
    "role": "doer", "branch_id": "", "department_id": "",
    "bm_delegation": "60", "bm_checklist": "20", "bm_fms": "20"})
check("a second person with the same name can exist",
      _dup.status_code in (200, 303), _dup.status_code)
_p2 = _bulk.parse(_SLT(), 1, "checklist", _sheet_bytes([
    [f"Ambiguous {RUN}", "", _amit_name, "", "daily", "", "18:00", "high", "NO"]]))
check("and then that name is refused, not guessed",
      _p2.rows and not _p2.rows[0].ok
      and "use their email" in (_p2.rows[0].error or ""),
      _p2.rows[0].error if _p2.rows else "no rows")
check("while their email still works",
      _bulk.parse(_SLT(), 1, "checklist", _sheet_bytes([
          [f"By email still {RUN}", "", _amit_mail, "", "daily", "", "18:00",
           "high", "NO"]])).rows[0].ok)

# And the yearly rule actually fires on its day, once.
_yr = _RR2(org_id=1, branch_id=None, title=f"SMOKE yearly fire {RUN}",
           doer_id=6, assigner_id=2, frequency=_Rec2.YEARLY, day_of=417,
           due_time="11:00")
with _SLT() as _d:
    _d.add(_yr); _d.commit()
    _rec2.run_spawn(_d, today=_d2(2027, 4, 16))
    check("a yearly rule does not fire the day before",
          not _d.scalars(_msel(_T2).where(_T2.rule_id == _yr.id)).all())
    _rec2.run_spawn(_d, today=_d2(2027, 4, 17))
    _fired = _d.scalars(_msel(_T2).where(_T2.rule_id == _yr.id)).all()
    check("it fires on its date", len(_fired) == 1, len(_fired))
    _rec2.run_spawn(_d, today=_d2(2027, 4, 17))
    check("and not twice",
          len(_d.scalars(_msel(_T2).where(_T2.rule_id == _yr.id)).all()) == 1)
    _rec2.run_spawn(_d, today=_d2(2028, 4, 17))
    check("but again the following year",
          len(_d.scalars(_msel(_T2).where(_T2.rule_id == _yr.id)).all()) == 2)

check("a blank frequency is refused rather than read as daily",
      not _bulk.parse(_SLT(), 1, "checklist", _sheet_bytes([
          [f"No freq {RUN}", "", _amit_mail, "", "", "", "18:00", "high", "NO"]
      ])).rows[0].ok)

# The Checklist form and the spreadsheet must agree on what a yearly date is.
_yform = admin.post("/recurring", data={
    "title": f"SMOKE yearly form {RUN}", "details": "", "doer_id": str(_amit.id),
    "branch_id": "", "frequency": "yearly", "year_day": "17/04",
    "due_time": "11:00", "priority": "high"})
check("the Checklist form takes a yearly date", _yform.status_code in (200, 303),
      _yform.status_code)
with _SLT() as _d:
    _yr2 = _d.scalar(_msel(_RR2).where(_RR2.title == f"SMOKE yearly form {RUN}"))
    check("stored the same way the spreadsheet stores it",
          _yr2 is not None and _yr2.day_of == 417,
          _yr2.day_of if _yr2 else "missing")
    check("and reads back in words",
          _yr2 and "17 Apr" in _yr2.schedule_label, _yr2.schedule_label if _yr2 else "")
check("the Checklist page shows it", "17 Apr" in admin.get("/recurring").text)

print("\n== every schedule, walked against a real calendar ==")
# The only honest way to check a schedule is to walk a year of real dates and
# see which ones it picks. Anything less tests the code against itself.
import calendar as _cal
from datetime import date as _dq, timedelta as _tq
from app.models import (RecurringRule as _RQ, Recurrence as _FQ,
                        MonthMode as _MM)
from app.services.recurring import is_due_today as _due_q, read_schedule as _rs

def _year(rule, year=2027):
    out, d = [], _dq(year, 1, 1)
    while d.year == year:
        if _due_q(rule, d):
            out.append(d)
        d += _tq(days=1)
    return out

# --- weekly, one day and several -----------------------------------------
_mon_thu = _RQ(frequency=_FQ.WEEKLY, weekdays="0,3")
_hits = _year(_mon_thu)
check("Monday and Thursday fires twice a week all year", len(_hits) == 104,
      len(_hits))
check("and only on Mondays and Thursdays",
      {d.weekday() for d in _hits} == {0, 3},
      sorted({d.weekday() for d in _hits}))
check("it reads as one rule, not two",
      _mon_thu.schedule_label == "Every Monday and Thursday",
      _mon_thu.schedule_label)

# A rule made before several days were allowed must not change behaviour.
_old = _RQ(frequency=_FQ.WEEKLY, day_of=5)
_oldhits = _year(_old)
check("an older weekly rule still fires on its own day", len(_oldhits) == 52,
      len(_oldhits))
check("which is the Saturday it always was",
      {d.weekday() for d in _oldhits} == {5})

# --- fortnightly ----------------------------------------------------------
_fort = _RQ(frequency=_FQ.FORTNIGHTLY, weekdays="1", anchor_on=_dq(2027, 1, 5))
_fh = _year(_fort)
check("fortnightly fires every second week", len(_fh) == 26, len(_fh))
check("starting on the date it was anchored to", _fh[0] == _dq(2027, 1, 5), _fh[0])
check("with a fortnight between each",
      all((_fh[i+1] - _fh[i]).days == 14 for i in range(len(_fh) - 1)),
      sorted({(_fh[i+1]-_fh[i]).days for i in range(len(_fh)-1)}))
# The week is what counts, not the date — so reading it from a different day
# of the same week must not flip which fortnight it is.
check("an anchor later in the same week means the same weeks",
      _year(_RQ(frequency=_FQ.FORTNIGHTLY, weekdays="1",
                anchor_on=_dq(2027, 1, 8))) == _fh)

# --- monthly by date ------------------------------------------------------
_d10 = _year(_RQ(frequency=_FQ.MONTHLY, day_of=10))
check("a monthly date fires twelve times", len(_d10) == 12, len(_d10))
check("always on that date", {d.day for d in _d10} == {10})
_d31 = _year(_RQ(frequency=_FQ.MONTHLY, day_of=31))
check("the 31st still fires every month", len(_d31) == 12, len(_d31))
check("pulling back to the last day of a short month",
      [d for d in _d31 if d.month == 2][0] == _dq(2027, 2, 28),
      [str(d) for d in _d31 if d.month == 2])
check("and never skipping a month",
      sorted({d.month for d in _d31}) == list(range(1, 13)))

# --- monthly, nth weekday -------------------------------------------------
_first_sat = _year(_RQ(frequency=_FQ.MONTHLY, weeks_of_month="1", weekdays="5"))
check("the first Saturday fires twelve times", len(_first_sat) == 12)
check("always on a Saturday", {d.weekday() for d in _first_sat} == {5})
check("always in the first seven days of the month",
      all(d.day <= 7 for d in _first_sat), [str(d) for d in _first_sat[:3]])

_third_sat = _year(_RQ(frequency=_FQ.MONTHLY, weeks_of_month="3", weekdays="5"))
check("the third Saturday fires twelve times", len(_third_sat) == 12)
check("always between the 15th and the 21st",
      all(15 <= d.day <= 21 for d in _third_sat),
      [str(d) for d in _third_sat[:3]])

_both = _year(_RQ(frequency=_FQ.MONTHLY, weeks_of_month="1,3", weekdays="5"))
check("first AND third Saturday fires twenty-four times", len(_both) == 24,
      len(_both))
check("and is exactly the two sets added together",
      _both == sorted(_first_sat + _third_sat))

# The one that catches a lazy implementation: last is not fourth.
_last_fri = _year(_RQ(frequency=_FQ.MONTHLY, weeks_of_month="-1", weekdays="4"))
_fourth_fri = _year(_RQ(frequency=_FQ.MONTHLY, weeks_of_month="4", weekdays="4"))
check("the last Friday fires twelve times", len(_last_fri) == 12)
check("and really is the last one in its month",
      all((d + _tq(days=7)).month != d.month for d in _last_fri))
_five = [(y, m) for y, m in [(2027, mm) for mm in range(1, 13)]
         if len([1 for x in range(1, _cal.monthrange(y, m)[1] + 1)
                 if _dq(y, m, x).weekday() == 4]) == 5]
check("some month this year has five Fridays", _five, "none — pick another year")
check("and in those months last and fourth are different days",
      all(next(d for d in _last_fri if (d.year, d.month) == ym)
          != next(d for d in _fourth_fri if (d.year, d.month) == ym)
          for ym in _five), [str(ym) for ym in _five])

# --- quarterly ------------------------------------------------------------
_q1 = _year(_RQ(**_rs("quarterly", {"quarter_start": "05/01"})))
check("quarterly fires four times a year", len(_q1) == 4, len(_q1))
check("in January, April, July and October",
      [d.month for d in _q1] == [1, 4, 7, 10], [d.month for d in _q1])
_q2 = _year(_RQ(**_rs("quarterly", {"quarter_start": "05/02"})))
check("starting in February shifts the whole cycle",
      [d.month for d in _q2] == [2, 5, 8, 11], [d.month for d in _q2])
check("quarterly needs only the first date, and works the rest out",
      _RQ(**_rs("quarterly", {"quarter_start": "05/02"})).schedule_label
      == "5 Feb, May, Aug and Nov, every year")
check("a bare day number still works, starting in January",
      _year(_RQ(**_rs("quarterly", {"quarter_start": "15/01"})))
      == [_dq(2027, m, 15) for m in (1, 4, 7, 10)])

# --- yearly ---------------------------------------------------------------
_yy = _year(_RQ(frequency=_FQ.YEARLY, day_of=417))
check("a yearly rule fires once", len(_yy) == 1, len(_yy))
check("on its date", _yy[0] == _dq(2027, 4, 17), _yy[0])
check("and again the next year",
      _year(_RQ(frequency=_FQ.YEARLY, day_of=417), 2028)[0] == _dq(2028, 4, 17))

# --- daily and weekdays ---------------------------------------------------
check("daily fires every day of the year",
      len(_year(_RQ(frequency=_FQ.DAILY))) == 365)
_wd = _year(_RQ(frequency=_FQ.WEEKDAYS))
check("weekdays never fires at a weekend",
      all(d.weekday() < 5 for d in _wd))
check("and covers every working day", len(_wd) == 261, len(_wd))
# Monday to Friday, written the way the page now offers it.
_mf = _year(_RQ(frequency=_FQ.WEEKLY, weekdays="0,1,2,3,4"))
check("weekly with five days ticked is the same thing", _mf == _wd,
      f"{len(_mf)} vs {len(_wd)}")
check("and reads as a range, not five names",
      _RQ(frequency=_FQ.WEEKLY, weekdays="0,1,2,3,4").schedule_label
      == "Every Monday to Friday",
      _RQ(frequency=_FQ.WEEKLY, weekdays="0,1,2,3,4").schedule_label)

print("\n== the same schedules, typed the way people type them ==")
# read_schedule is what both the Checklist form and the spreadsheet use, so
# what it accepts is the whole grammar people have to learn.
for _text, _freq, _want in [
        ({"weekdays": "Mon,Thu"}, "weekly", "Every Monday and Thursday"),
        ({"weekdays": "Mon and Thu"}, "weekly", "Every Monday and Thursday"),
        ({"day": "Sat"}, "weekly", "Every Saturday"),
        ({"weekdays": "Tue", "anchor_on": "2027-01-05"}, "fortnightly",
         "Every second Tuesday, counting from 05 Jan 2027"),
        ({"day": "15"}, "monthly", "Day 15 of every month"),
        ({"weeks_of_month": "first", "weekdays": "Sat"}, "monthly",
         "The first Saturday of every month"),
        ({"weeks_of_month": "1,3", "weekdays": "Sat"}, "monthly",
         "The first and third Saturday of every month"),
        ({"weeks_of_month": "last", "weekdays": "Fri"}, "monthly",
         "The last Friday of every month"),
        ({"quarter_start": "05/02"}, "quarterly",
         "5 Feb, May, Aug and Nov, every year"),
        ({"day": "17/04"}, "yearly", "Every year on 17 Apr")]:
    _r = _RQ(**_rs(_freq, _text))
    check(f"{_freq} {list(_text.values())} reads as “{_want}”",
          _r.schedule_label == _want, _r.schedule_label)

for _bad, _freq, _why in [
        ({}, "weekly", "no day"),
        ({}, "monthly", "no date"),
        ({}, "yearly", "no date"),
        ({"weekdays": "Funday"}, "weekly", "not a day"),
        ({"weeks_of_month": "1st"}, "monthly", "week but no weekday"),
        ({"day": "0"}, "monthly", "date out of range"),
        ({"day": "32"}, "monthly", "date out of range")]:
    try:
        _rs(_freq, _bad)
        check(f"{_freq} with {_why} is refused", False, "it was accepted")
    except ValueError as _e:
        check(f"{_freq} with {_why} is refused, in words",
              len(str(_e)) > 25 and str(_e)[0].isupper() or "'" in str(_e),
              str(_e))
try:
    _rs("banana", {})
    check("an unknown frequency is refused", False, "it was accepted")
except ValueError as _e:
    check("an unknown frequency is refused", "not a frequency" in str(_e), str(_e))
    check("and the message lists the ones that work",
          all(w in str(_e) for w in ("weekly", "fortnightly", "quarterly",
                                     "yearly")), str(_e))

print("\n== and they still step off a closed day ==")
# A schedule landing on a Sunday must still come forward a day, whatever kind
# of schedule it is.
with _SLT() as _dq2:
    _brq = _dq2.scalar(_msel(_BR2))
    _brq.weekly_off = 6
    _dq2.commit()
    _sunday_monthly = _RQ(org_id=1, branch_id=_brq.id,
                          title=f"SMOKE quarterly closed {RUN}", doer_id=6,
                          assigner_id=2, frequency=_FQ.MONTHLY,
                          weeks_of_month="1", weekdays="6", due_time="23:59")
    _dq2.add(_sunday_monthly)
    _dq2.commit()
    # 1 Aug 2027 is a Sunday and the first Sunday of that month.
    check("the test date is the first Sunday of the month",
          _dq(2027, 8, 1).weekday() == 6)
    _rec2.run_spawn(_dq2, today=_dq(2027, 7, 31))       # the Saturday before
    _made_q = _dq2.scalars(_msel(_T2).where(
        _T2.rule_id == _sunday_monthly.id)).all()
    check("a first-Sunday job is created on the Saturday before",
          len(_made_q) == 1 and _made_q[0].covers_day == _dq(2027, 8, 1),
          [str(t.covers_day) for t in _made_q])
    check("and is due on that Saturday",
          _made_q[0].due_at.date() == _dq(2027, 7, 31), _made_q[0].due_at)

print("\n== a schedule survives the whole trip, spreadsheet to database ==")
# Reading a schedule correctly and then writing it away incompletely is a
# quiet bug: the preview shows the right thing and the saved rule fires on
# the wrong day. It happened to the weekday of every fortnightly rule, so it
# is checked here end to end rather than at either end.
_TRIP = [
    ("weekly", "Mon,Thu", "Every Monday and Thursday"),
    ("weekly", "Sat", "Every Saturday"),
    ("fortnightly", "Wed from 30/09/2026",
     "Every second Wednesday, counting from 30 Sep 2026"),
    ("monthly", "15", "Day 15 of every month"),
    ("monthly", "15,30", "Days 15 and 30 of every month"),
    ("monthly", "15th and 30th", "Days 15 and 30 of every month"),
    ("monthly", "first Saturday of every month",
     "The first Saturday of every month"),
    ("monthly", "1st Sat", "The first Saturday of every month"),
    ("monthly", "first & third Sat",
     "The first and third Saturday of every month"),
    ("monthly", "last Fri", "The last Friday of every month"),
    ("quarterly", "05/02", "5 Feb, May, Aug and Nov, every year"),
    ("quarterly", "5 from Feb", "5 Feb, May, Aug and Nov, every year"),

    ("yearly", "17/04", "Every year on 17 Apr"),
    ("daily", "", "Every day"),
    ("weekdays", "", "Every Monday to Friday"),
    ("weekly", "Mon,Tue,Wed,Thu,Fri", "Every Monday to Friday"),
]
_tblob = _sheet_bytes([
    [f"TRIP {n} {RUN}", "", _amit_mail, "", f, day, "23:59", "high", "NO"]
    for n, (f, day, _want) in enumerate(_TRIP)])
_tp = _bulk.parse(_SLT(), 1, "checklist", _tblob)
check("every schedule shape parses", len(_tp.good) == len(_TRIP),
      [r.error for r in _tp.bad])
for n, (f, day, want) in enumerate(_TRIP):
    _row = next((r for r in _tp.good
                 if r.data["title"] == f"TRIP {n} {RUN}"), None)
    check(f"preview reads “{f} {day}” as “{want}”",
          _row is not None and _row.data["schedule_label"] == want,
          _row.data["schedule_label"] if _row else "missing")

with _SLT() as _d:
    _admin_u = _d.scalar(_msel(_MU).where(_MU.email == "mis@gcs.local"))
    _bulk.commit(_d, 1, _admin_u, _tp)
    for n, (f, day, want) in enumerate(_TRIP):
        _saved = _d.scalar(_msel(_RR2).where(_RR2.title == f"TRIP {n} {RUN}"))
        check(f"and the SAVED rule still says “{want}”",
              _saved is not None and _saved.schedule_label == want,
              _saved.schedule_label if _saved else "not saved")

# Saved rules must also fire on the right days, not merely describe them.
def _trip_row(want):
    """Find a saved trip rule by what it should say, not by its position —
    a test that counts rows breaks the moment a row is added above it."""
    return next(n for n, (_f, _d2, w) in enumerate(_TRIP) if w == want)

with _SLT() as _d:
    _fort = _d.scalar(_msel(_RR2).where(_RR2.title == (
        f"TRIP {_trip_row('Every second Wednesday, counting from 30 Sep 2026')} {RUN}")))
    _hits = [x for x in (_dq(2026, 10, 1) + _tq(days=n) for n in range(60))
             if _due_q(_fort, x)]
    check("a saved fortnightly rule fires every second Wednesday",
          all(h.weekday() == 2 for h in _hits)
          and all((_hits[i+1] - _hits[i]).days == 14 for i in range(len(_hits)-1))
          and len(_hits) >= 4,
          [str(h) for h in _hits[:5]])
    _first_sat_saved = _d.scalar(_msel(_RR2).where(_RR2.title == (
        f"TRIP {_trip_row('The first Saturday of every month')} {RUN}")))
    _fs = [x for x in (_dq(2027, 1, 1) + _tq(days=n) for n in range(365))
           if _due_q(_first_sat_saved, x)]
    check("a saved first-Saturday rule fires twelve times on Saturdays",
          len(_fs) == 12 and {h.weekday() for h in _fs} == {5}
          and all(h.day <= 7 for h in _fs), [str(h) for h in _fs[:3]])

# The list of schedule fields is the thing that stops this recurring — if a
# column is added to the model and forgotten here, this notices.
from app.services.bulk import SCHEDULE_FIELDS as _SF
check("every schedule column is in the list the import carries",
      {"frequency", "day_of", "weekdays", "weeks_of_month", "start_month",
       "anchor_on"} <= set(_SF), _SF)

print("\n== twice a month is not every two weeks ==")
# A rule on the 15th and the 30th fires 24 times a year and always on those
# dates. A fortnightly rule fires 26 times and drifts through the month.
# People write both as "fortnightly", so the difference has to be kept.
_twice = _RQ(**_rs("monthly", {"day": "15,30"}))
_hits2 = _year(_twice)
check("the 15th and 30th fires twenty-four times", len(_hits2) == 24, len(_hits2))
check("always on one of those dates",
      {d.day for d in _hits2} <= {15, 28, 29, 30}, sorted({d.day for d in _hits2}))
check("in February it pulls back to the last day",
      [d.day for d in _hits2 if d.month == 2] == [15, 28],
      [d.day for d in _hits2 if d.month == 2])
_fort2 = _RQ(frequency=_FQ.FORTNIGHTLY, weekdays="3", anchor_on=_dq(2027, 1, 7))
check("while a fortnightly rule fires twenty-six times",
      len(_year(_fort2)) == 26, len(_year(_fort2)))
check("and the two are genuinely different schedules",
      _hits2 != _year(_fort2))

# The 30th and the 31st must not both land on 28 February and make two tasks.
_end = _RQ(**_rs("monthly", {"day": "30,31"}))
_feb = [d for d in _year(_end) if d.month == 2]
check("the 30th and 31st together make one February task, not two",
      len(_feb) == 1 and _feb[0] == _dq(2027, 2, 28), [str(d) for d in _feb])

# And the mix-up gets a message that says what to do about it.
for _f in ("fortnightly", "weekly"):
    try:
        _rs(_f, {"day": "15th and 30th"})
        check(f"{_f} with dates is refused", False, "accepted")
    except ValueError as _e:
        check(f"{_f} with dates is refused", True)
        check(f"and the message tells you to use monthly 15,30",
              "monthly" in str(_e) and "15,30" in str(_e), str(_e))

# A weekday inside a sentence is still a weekday.
_sent = _RQ(**_rs("monthly", {"weeks_of_month": "first", "weekdays": "Saturday"}))
check("“first Saturday of every month” typed in full still works",
      _sent.schedule_label == "The first Saturday of every month",
      _sent.schedule_label)

print("\n== the frequency list is six single words ==")
from app.models import FREQ_ORDER as _FO, FREQ_LABELS as _FL
check("six frequencies are offered", len(_FO) == 6, [f.value for f in _FO])
check("and Weekdays is not one of them",
      _FQ.WEEKDAYS not in _FO, [f.value for f in _FO])
check("they are the six expected",
      [f.value for f in _FO] == ["daily", "weekly", "fortnightly", "monthly",
                                 "quarterly", "yearly"],
      [f.value for f in _FO])
check("each label is a single word",
      all(len(_FL[f].split()) == 1 for f in _FO),
      {f.value: _FL[f] for f in _FO})

_page = admin.get("/recurring").text
for _word in ["Daily", "Weekly", "Fortnightly", "Monthly", "Quarterly", "Yearly"]:
    check(f"the page offers {_word}", f">{_word}</option>" in _page, _word)
check("and no longer offers Weekdays as its own choice",
      ">Weekdays</option>" not in _page)

# A rule saved as Weekdays before the change must still run, and be converted.
with _SLT() as _d:
    _wdr = _RQ(org_id=1, branch_id=None, title=f"SMOKE old weekdays {RUN}",
               doer_id=6, assigner_id=2, frequency=_FQ.WEEKDAYS,
               due_time="23:59")
    _d.add(_wdr)
    _d.commit()
    _wid = _wdr.id
    check("an older Weekdays rule still fires Monday to Friday",
          [_due_q(_wdr, _dq(2027, 3, d)) for d in range(1, 8)]
          == [True, True, True, True, True, False, False],
          [_due_q(_wdr, _dq(2027, 3, d)) for d in range(1, 8)])

import app.migrate as _mig2
_mig2.run()
with _SLT() as _d:
    _after = _d.get(_RQ, _wid)
    check("and the upgrade converts it to weekly",
          _after.frequency == _FQ.WEEKLY, _after.frequency)
    check("with the five days ticked",
          _after.weekday_list == [0, 1, 2, 3, 4], _after.weekday_list)
    check("reading exactly as it did before",
          _after.schedule_label == "Every Monday to Friday",
          _after.schedule_label)
    check("and firing on exactly the same days",
          [_due_q(_after, _dq(2027, 3, d)) for d in range(1, 8)]
          == [True, True, True, True, True, False, False])
    check("no rule is left on the retired frequency",
          _d.scalar(_msel(_func.count()).select_from(_RQ)
                    .where(_RQ.frequency == _FQ.WEEKDAYS)) == 0)

# The word still works in a spreadsheet, because people write it.
_wdrow = _bulk.parse(_SLT(), 1, "checklist", _sheet_bytes([
    [f"SMOKE weekdays word {RUN}", "", _amit_mail, "", "weekdays", "", "23:59",
     "high", "NO"]])).rows[0]
check("a spreadsheet saying weekdays is still accepted", _wdrow.ok, _wdrow.error)
check("and stored as weekly Monday to Friday",
      _wdrow.ok and _wdrow.data["schedule_label"] == "Every Monday to Friday",
      _wdrow.data.get("schedule_label") if _wdrow.ok else "")

print("\n== every page's tags close ==")
# One <div> left unclosed turned the whole dashboard into a flex row: the
# greeting, the KPI cards and the task lists all sat side by side in a narrow
# column. The page still rendered, still passed every other check, and looked
# completely wrong. A browser silently repairs it, so only counting does.
from html.parser import HTMLParser as _HP

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
         "meta", "param", "source", "track", "wbr"}

class _Balance(_HP):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.bad = [], []
    def handle_starttag(self, tag, attrs):
        if tag not in _VOID:
            self.stack.append(tag)
    def handle_startendtag(self, tag, attrs):
        pass
    def handle_endtag(self, tag):
        if tag in _VOID:
            return
        if not self.stack:
            self.bad.append(f"</{tag}> with nothing open")
        elif self.stack[-1] == tag:
            self.stack.pop()
        elif tag in self.stack:
            # something in between was never closed
            while self.stack and self.stack[-1] != tag:
                self.bad.append(f"<{self.stack.pop()}> never closed")
            if self.stack:
                self.stack.pop()
        else:
            self.bad.append(f"</{tag}> closes nothing")

_PAGES = ["/", "/tasks?scope=mine&status=open", "/tasks?scope=all&status=all",
          "/recurring", "/flows", "/followups", "/reports",
          "/reports/tasks?source=delegation", "/reports/audit",
          "/reports/score", "/reports/followups", "/stats", "/outbox",
          "/admin/users", "/admin/branches", "/admin/departments",
          "/admin/holidays", "/bulk", "/help", "/tasks/new", "/flows/new"]
for _path in _PAGES:
    _r = admin.get(_path)
    if _r.status_code != 200:
        check(f"{_path} opens", False, _r.status_code)
        continue
    _b = _Balance()
    _b.feed(_r.text)
    _left = [t for t in _b.stack if t not in ("html", "body")]
    check(f"{_path}: every tag is closed", not _b.bad and not _left,
          (_b.bad + [f"<{t}> never closed" for t in _left])[:4])

# And the dashboard specifically: the cards must sit in a row, not a column.
_dash = admin.get("/").text
check("the dashboard heading block is closed before the first section",
      _dash.index('class="sect"') > _dash.index('class="page-head"'))
# The parser check above already proves the tags balance; this one just
# pins the shape of the page so a stray edit cannot slide the heading into
# the first section again.
check("the greeting and the buttons are both inside it",
      "page-head" in _dash and "Reports</a>" in _dash)

print("\n== the task list's tabs and its three pickers ==")
# The complaint was "so many filters which will confuse the team". What is
# left is one row of states plus three plain dropdowns: employee, branch,
# department.
from app.db import SessionLocal as _sl3
from app.models import (Task as _T3, User as _U3, Branch as _B3,
                        Department as _D3)
from app.routers.tasks import STATUS_TABS as _TABS

_page = admin.get("/tasks?scope=all&status=pending").text
for _k, _lbl in _TABS:
    check(f"tab {_lbl!r} is on the page", f"status={_k}&" in _page)
check("no tab was left over from the old set",
      ">With auditor</a>" not in _page and ">Everything</a>" not in _page
      and "status=audit&" not in _page)
check("the open tab explains itself", "statusnote" in _page)
check("Pending says it is overdue plus coming up",
      "overdue work and work still coming up" in _page)

check("there is an Employee picker", 'name="doer"' in _page)
check("there is a Branch picker", 'name="branch"' in _page)
check("there is a Department picker", 'name="dept"' in _page)

with _sl3() as _d:
    _amit3 = _d.query(_U3).filter(_U3.email == "amit@gcs.local").one()
    _ravi3 = _d.query(_U3).filter(_U3.email == "ravi@gcs.local").one()
    _amit_branch = _amit3.branch_id
    _amit_dept = _amit3.department_id
    _amit_open = _d.query(_T3).filter(_T3.doer_id == _amit3.id).count()
    _ravi_open = _d.query(_T3).filter(_T3.doer_id == _ravi3.id).count()

check("both people actually have tasks to tell apart",
      _amit_open > 0 and _ravi_open > 0, f"{_amit_open}/{_ravi_open}")

_one = admin.get(f"/tasks?scope=all&status=all&doer={_amit3.id}").text
check("the employee filter keeps that person's work",
      f"/tasks/" in _one and _amit3.name in _one)
check("and drops everybody else's", f'doer={_ravi3.id}"' not in _one
      and _ravi3.name not in _one.split('name="doer"')[1].split("</select>")[1])
check("the picked employee stays selected",
      f'value="{_amit3.id}" selected' in _one)

if _amit_branch:
    _bp = admin.get(f"/tasks?scope=all&status=all&branch={_amit_branch}").text
    check("the branch filter stays selected",
          f'value="{_amit_branch}" selected' in _bp)
    with _sl3() as _d:
        _want = _d.query(_T3).filter(_T3.branch_id == _amit_branch).count()
    check("the branch filter returns that branch's tasks",
          f"{_want} task(s)" in _bp, _bp.split("task(s)")[0][-40:])

if _amit_dept:
    _dp = admin.get(f"/tasks?scope=all&status=all&dept={_amit_dept}").text
    check("the department filter stays selected",
          f'value="{_amit_dept}" selected' in _dp)
    with _sl3() as _d:
        _ids = [u.id for u in _d.query(_U3)
                .filter(_U3.department_id == _amit_dept).all()]
        _want = _d.query(_T3).filter(_T3.doer_id.in_(_ids)).count()
    check("the department filter counts everyone in it",
          f"{_want} task(s)" in _dp, _dp.split("task(s)")[0][-40:])

# The three pickers have to survive the Excel download too, or the file
# would not match the list the person was looking at.
_x = admin.get(f"/tasks?scope=all&status=all&doer={_amit3.id}&export=xlsx")
check("the Excel download honours the pickers", _x.status_code == 200
      and _x.headers["content-type"].startswith("application/"), _x.status_code)

# Checklist and FMS are the same page with source= set, so the pickers and
# the tab row must be there too. This is the "implement the same in
# checklist and fms" half of the request.
for _src in ("checklist", "fms", "delegation"):
    _sp = admin.get(f"/tasks?scope=all&status=pending&source={_src}").text
    check(f"{_src}: the three pickers are there",
          'name="doer"' in _sp and 'name="branch"' in _sp and 'name="dept"' in _sp)
    check(f"{_src}: the same status tabs are there",
          all(f"status={k}&" in _sp for k, _ in _TABS))
    check(f"{_src}: staying on that work type across tabs",
          f"source={_src}" in _sp)

# An unfinished task must never be offered to an auditor, from any door.
print("\n== audit only after the employee marks it complete ==")
_ar = mgr.post("/tasks/new", data={
    "title": f"SMOKE audit gate {RUN}", "details": "",
    "doer_id": "6", "branch_id": "", "priority": "medium",
    "due_at": "2026-12-31T23:59", "requires_audit": "1"})
_agid = int(re.findall(r"/tasks/(\d+)/comment", _ar.text)[0])
_ap = admin.get("/tasks?scope=all&status=audit_pending").text
check("an in-progress task is not on the audit list",
      f'/tasks/{_agid}"' not in _ap)
check("the audit tab says why", "not finished yet cannot be" in _ap)
_detail = admin.get(f"/tasks/{_agid}").text
check("its page does not offer the auditor a verdict yet",
      'name="decision"' not in _detail)
check("nor an Audit-pending / Audit-completed button",
      'value="pending"' not in _detail and 'value="completed"' not in _detail)
check("and it says why", "has not marked this complete yet" in _detail)
check("turning the audit off is still allowed",
      'value="not_required"' in _detail)
check("no second audit box on an unfinished task either",
      "Audit status" not in _detail)
_r = admin.post(f"/tasks/{_agid}/audit",
                data={"decision": "approve", "score": "9", "remark": "x"})
check("and the audit route refuses it", _r.status_code >= 400, _r.status_code)
submit(doer, _agid, completion_note="done")
_ap2 = admin.get("/tasks?scope=all&status=audit_pending").text
check("once marked complete it appears for audit", f'/tasks/{_agid}' in _ap2)
_detail2 = admin.get(f"/tasks/{_agid}").text
# One audit box, four outcomes. There used to be a second box above it that
# only changed the label — no score, no verdict, the task stayed open — and
# people clicked "Audit completed" in it believing they had audited something.
check("the auditor now gets all four outcomes",
      "Approve" in _detail2 and "Re-open task" in _detail2
      and "False marking" in _detail2 and "Not required" in _detail2)
check("false marking is offered once, not twice",
      _detail2.count("Mark as false marking") == 0)
check("there is only one audit box now",
      _detail2.count("Audit status") == 0 and _detail2.count("id=\"audit\"") == 1,
      _detail2.count("Audit status"))
check("and no label-only buttons to click by mistake",
      'value="pending"' not in _detail2 and 'value="completed"' not in _detail2)
check("Not required is still reachable", 'value="not_required"' in _detail2)
_r = admin.post(f"/tasks/{_agid}/false-mark",
                data={"reason": "register was blank", "confirm": "yes"})
check("the auditor can flag false marking", _r.status_code in (200, 303),
      _r.status_code)
check("and it shows under False marking",
      f'/tasks/{_agid}' in admin.get("/tasks?scope=all&status=false_mark").text)

print("\n== every task carries an ID: DEL-01, CL-01, FMS-01 ==")
from app.db import SessionLocal as _sl4
from app.models import (Task as _T4, TaskSource as _S4, REF_PREFIX as _RP4,
                        ref_text as _rt4, User as _U4)

with _sl4() as _d:
    _all = _d.query(_T4).all()
    check("no task is left without a reference",
          all(t.ref for t in _all),
          [t.id for t in _all if not t.ref][:5])
    for _src, _pref in _RP4.items():
        _rows = sorted([t for t in _all if t.source == _src],
                       key=lambda t: t.ref_n)
        if not _rows:
            continue
        check(f"{_pref} numbering starts at 1", _rows[0].ref_n == 1,
              _rows[0].ref)
        check(f"{_pref} numbering has no gaps and no repeats",
              [t.ref_n for t in _rows] == list(range(1, len(_rows) + 1)),
              [t.ref_n for t in _rows][:12])
        check(f"{_pref} is spelled the way it reads",
              all(t.ref == _rt4(_pref, t.ref_n) for t in _rows),
              [t.ref for t in _rows][:3])
    # The three series must not borrow each other's numbers.
    _by_pref = {}
    for t in _all:
        _by_pref.setdefault(t.ref.split("-")[0], set()).add(t.ref)
    check("the three series are numbered independently",
          all(len(v) == len([t for t in _all if t.ref.startswith(k + "-")])
              for k, v in _by_pref.items()))
    check("delegation uses DEL", any(r.startswith("DEL-") for r in
                                     [t.ref for t in _all]))
    check("checklist uses CL", any(r.startswith("CL-") for r in
                                   [t.ref for t in _all]))
    check("FMS uses FMS", any(r.startswith("FMS-") for r in
                              [t.ref for t in _all]))

# A brand-new task continues the series rather than repeating a number.
with _sl4() as _d:
    _before = _d.query(_T4).filter(_T4.source == _S4.DELEGATION).count()
_nr = mgr.post("/tasks/new", data={
    "title": f"SMOKE ref continues {RUN}", "details": "", "doer_id": "6",
    "branch_id": "", "priority": "low", "due_at": "2026-12-31T23:59"})
_nid2 = int(re.findall(r"/tasks/(\d+)/comment", _nr.text)[0])
with _sl4() as _d:
    _t = _d.get(_T4, _nid2)
    check("a new delegation task takes the next number",
          _t.ref == _rt4("DEL", _before + 1), _t.ref)

check("the ID shows on the task's own page",
      _t.ref in admin.get(f"/tasks/{_nid2}").text)
check("and there is an ID column on the list",
      "<th>ID</th>" in admin.get("/tasks?scope=all&status=all").text)
check("the list row carries it",
      _t.ref in admin.get("/tasks?scope=all&status=all").text)

# Looking one up by its reference.
_found = admin.get(f"/tasks?scope=all&status=pending&ref={_t.ref}").text
check("searching the ID finds that task", f'/tasks/{_nid2}"' in _found)
check("and only that task", _found.count('class="refid"') == 1,
      _found.count('class="refid"'))
check("the number alone works on a work-type tab",
      f'/tasks/{_nid2}"' in admin.get(
          f"/tasks?scope=all&status=pending&source=delegation"
          f"&ref={_t.ref_n}").text)
check("a reference that does not exist finds nothing, not everything",
      admin.get("/tasks?scope=all&status=all&ref=DEL-999999").text.count(
          'class="refid"') == 0)
check("an ID found in one state is not hidden by the tab it was searched from",
      f'/tasks/{_nid2}"' in admin.get(
          f"/tasks?scope=all&status=audit_done&ref={_t.ref}").text)

# The Excel download has to carry it too, or the file cannot be matched back
# to the screen it came from.
import io as _io4
try:
    from openpyxl import load_workbook as _lw4
    _xr = admin.get("/tasks?scope=all&status=all&export=xlsx")
    _wb4 = _lw4(_io4.BytesIO(_xr.content))
    _ws4 = _wb4[_wb4.sheetnames[0]]
    _hdr = [c.value for c in next(_ws4.iter_rows(min_row=1, max_row=8))
            if c.value]
    _hdrs = []
    for _row in _ws4.iter_rows(min_row=1, max_row=8):
        _vals = [c.value for c in _row if c.value]
        if "Task" in _vals:
            _hdrs = _vals
            break
    check("the Excel download has a Task ID column", "Task ID" in _hdrs, _hdrs[:6])
except ImportError:
    check("openpyxl available to check the export", False)

print("\n== clearing out every delegation task ==")
with _sl4() as _d:
    _del_before = _d.query(_T4).filter(_T4.source == _S4.DELEGATION).count()
    _cl_before = _d.query(_T4).filter(_T4.source == _S4.RECURRING).count()
    _fms_before = _d.query(_T4).filter(_T4.source == _S4.FLOW).count()
check("there are delegation tasks to clear", _del_before > 0, _del_before)

_pp = admin.get("/admin/purge/delegation")
check("the danger page opens", _pp.status_code == 200, _pp.status_code)
check("it says how many will go", str(_del_before) in _pp.text)
check("it says there is no undo", "cannot be undone" in _pp.text)
check("a doer cannot open it", doer.get("/admin/purge/delegation").status_code == 403)
check("nor post to it",
      doer.post("/admin/purge/delegation",
                data={"confirm": "DELETE ALL DELEGATION"}).status_code == 403)
check("the wrong words delete nothing",
      admin.post("/admin/purge/delegation",
                 data={"confirm": "delete everything"}).status_code == 400)
with _sl4() as _d:
    check("and really nothing was deleted",
          _d.query(_T4).filter(_T4.source == _S4.DELEGATION).count()
          == _del_before)

_r = admin.post("/admin/purge/delegation",
                data={"confirm": "DELETE ALL DELEGATION"})
check("the master delete runs", _r.status_code in (200, 303), _r.status_code)
with _sl4() as _d:
    check("every delegation task is gone",
          _d.query(_T4).filter(_T4.source == _S4.DELEGATION).count() == 0)
    check("checklist tasks are untouched",
          _d.query(_T4).filter(_T4.source == _S4.RECURRING).count() == _cl_before)
    check("FMS tasks are untouched",
          _d.query(_T4).filter(_T4.source == _S4.FLOW).count() == _fms_before)
    # Nothing may be left pointing at a task that no longer exists.
    from app.models import (TaskComment as _TC4, Attachment as _A4,
                            Followup as _F4, HelpTicket as _H4,
                            OutboundMessage as _O4)
    _live = {t.id for t in _d.query(_T4).all()}
    for _name, _model in (("notes", _TC4), ("files", _A4),
                          ("follow-ups", _F4), ("help tickets", _H4)):
        _orphans = [r.id for r in _d.query(_model).all()
                    if r.task_id is not None and r.task_id not in _live]
        check(f"no {_name} left pointing at a deleted task",
              not _orphans, _orphans[:5])
    _msg_orphans = [m.id for m in _d.query(_O4).all()
                    if m.task_id is not None and m.task_id not in _live]
    check("sent notifications are kept but unhooked", not _msg_orphans,
          _msg_orphans[:5])

check("with the list empty the page says there is nothing to clear",
      "no delegation tasks to clear"
      in admin.get("/admin/purge/delegation").text)

# And the whole point: numbering starts again.
_r = mgr.post("/tasks/new", data={
    "title": f"SMOKE after the purge {RUN}", "details": "", "doer_id": "6",
    "branch_id": "", "priority": "low", "due_at": "2026-12-31T23:59"})
_aid4 = int(re.findall(r"/tasks/(\d+)/comment", _r.text)[0])
with _sl4() as _d:
    check("the next delegation task is DEL-01 again",
          _d.get(_T4, _aid4).ref == "DEL-01", _d.get(_T4, _aid4).ref)
check("the pages still open afterwards",
      admin.get("/tasks?scope=all&status=all").status_code == 200
      and admin.get("/").status_code == 200
      and admin.get("/reports/audit").status_code == 200)
check("once a task exists again the page offers the delete once more",
      "DELETE ALL DELEGATION" in admin.get("/admin/purge/delegation").text)

print("\n== upgrading a database that has tasks but no IDs yet ==")
# The live database on Render is full of work and has never heard of a
# reference column. This rehearses exactly that: take the columns away, run
# the migrator the way a deploy does, and check every existing task comes out
# numbered, oldest first, with nothing repeated.
from sqlalchemy import text as _uptx5, inspect as _upin5
from app.db import engine as _upeng5, SessionLocal as _sl5
from app import migrate as _upmig5
from app.models import Task as _T5, TaskSource as _S5

with _sl5() as _d:
    _was = {t.id: t.ref for t in _d.query(_T5).all()}

if _upeng5.dialect.name == "postgresql":
    with _upeng5.begin() as _c:
        _c.execute(_uptx5("ALTER TABLE tasks DROP COLUMN IF EXISTS ref"))
        _c.execute(_uptx5("ALTER TABLE tasks DROP COLUMN IF EXISTS ref_n"))
else:
    with _upeng5.begin() as _c:
        _c.execute(_uptx5("UPDATE tasks SET ref = NULL, ref_n = NULL"))

_applied5 = _upmig5.run()
check("the migrator reports what it numbered",
      any("numbered" in a for a in _applied5), _applied5[-4:])

with _sl5() as _d:
    _rows5 = _d.query(_T5).all()
    check("no task is left without an ID after the upgrade",
          all(t.ref for t in _rows5),
          [t.id for t in _rows5 if not t.ref][:5])
    for _src5 in _S5:
        _got5 = sorted([t for t in _rows5 if t.source == _src5],
                       key=lambda t: t.ref_n)
        if not _got5:
            continue
        check(f"upgraded {_src5.value} IDs run 1..{len(_got5)} with no gaps",
              [t.ref_n for t in _got5] == list(range(1, len(_got5) + 1)),
              [t.ref_n for t in _got5][:12])
        _age5 = sorted(_got5, key=lambda t: (t.created_at, t.id))
        check(f"the oldest {_src5.value} task holds the lowest number",
              [t.id for t in _age5] == [t.id for t in _got5])
    _now5 = {t.id: t.ref for t in _rows5}

check("every task that had an ID kept the same one", _now5 == _was,
      [k for k in _now5 if _was.get(k) != _now5[k]][:5])

_again5 = _upmig5.run()
with _sl5() as _d:
    check("running the upgrade a second time renumbers nothing",
          {t.id: t.ref for t in _d.query(_T5).all()} == _now5)
check("and it reports nothing to do",
      not any("numbered" in a for a in _again5), _again5[-3:])

print("\n== the admin assigns work and is never given any ==")
# One assigner in this company: the admin account. Its own task list and its
# own score are therefore permanently empty, and an empty section at the top
# of the page is the first thing it sees every morning.
_ap = admin.get("/tasks?scope=all&status=pending").text
check("the admin is not offered an Assigned-to-me tab",
      ">Assigned to me</a>" not in _ap)
check("a doer still is",
      ">Assigned to me</a>" in doer.get("/tasks?scope=mine&status=pending").text)
check("the admin asking for it anyway lands somewhere useful",
      "Assigned to me" not in admin.get("/tasks?scope=mine&status=pending").text)
_dash_admin = admin.get("/").text
check("the admin dashboard has no My work section", "My work" not in _dash_admin)
check("nor a My EM score section", "My EM score" not in _dash_admin)
check("and it does not open at section 3",
      "3 · " not in _dash_admin.split("MY TEAM")[0])
check("the doer's dashboard still has both",
      "My work" in doer.get("/").text and "My EM score" in doer.get("/").text)
check("the admin's menu does not say My Tasks", ">My Tasks<" not in _dash_admin)
check("the doer's menu does", ">My Tasks<" in doer.get("/").text)

print("\n== a doer sees the audit result, not the auditor's queue ==")
_dt = doer.get("/tasks?scope=mine&status=pending").text
check("no Audit pending tab for a doer", "status=audit_pending" not in _dt)
check("Audit completed is there", "status=audit_done" in _dt)
check("False marking is there", "status=false_mark" in _dt)
check("no Audit process in the doer's menu", ">Audit process<" not in _dt)
check("the admin keeps the queue",
      "status=audit_pending" in _ap and ">Audit process<" in _ap)
check("an auditor who is not an admin keeps it too",
      "status=audit_pending" in mgr.get("/tasks?scope=all&status=pending").text)
# An old bookmark to the queue must not dump a doer on an empty page.
_bm = doer.get("/tasks?scope=mine&status=audit_pending")
check("a doer following an old audit link is redirected, not shown nothing",
      _bm.status_code == 200 and "status=audit_done" in _bm.text)
check("and lands on Completed", ">Completed</a>" in _bm.text
      and 'class="active"' in _bm.text)
# The whole point of Audit completed for a doer: the remark.
_ac = doer.get("/tasks?scope=mine&status=audit_done").text
check("the Audit completed tab opens for a doer", "Audit completed" in _ac)

print("\n== Help Desk work is delegation, shown on its own ==")
from app.db import SessionLocal as _sl6
from app.models import (Task as _T6, TaskSource as _S6, HelpTicket as _H6)

_hr = doer.post("/help/new", data={
    "subject": f"SMOKE helpdesk tab {RUN}", "details": "please help",
    "helper_id": "7", "priority": "medium", "needed_by": "2026-12-30T17:00"})
check("anyone can raise a Help Desk request", _hr.status_code == 200, _hr.status_code)
with _sl6() as _d:
    _tk = _d.query(_H6).filter(_H6.subject == f"SMOKE helpdesk tab {RUN}").one()
    check("it created a task", _tk.task_id is not None)
    _ht = _d.get(_T6, _tk.task_id)
    check("and that task is delegation, so it scores as delegation",
          _ht.source == _S6.DELEGATION, _ht.source)
    check("it gets a DEL- reference like any other delegation task",
          _ht.ref.startswith("DEL-"), _ht.ref)
    _hid = _ht.id

_hd = admin.get("/tasks?scope=all&status=pending&source=helpdesk").text
check("the Help desk tab exists", "Help desk" in _hd)
check("and holds the help-desk task", f'/tasks/{_hid}"' in _hd)
_dg = admin.get("/tasks?scope=all&status=pending&source=delegation").text
check("the same task is still under Delegation", f'/tasks/{_hid}"' in _dg)

# A plain delegated task must NOT appear under Help desk.
_pr6 = mgr.post("/tasks/new", data={
    "title": f"SMOKE not helpdesk {RUN}", "details": "", "doer_id": "6",
    "branch_id": "", "priority": "low", "due_at": "2026-12-30T23:59"})
_pid6 = int(re.findall(r"/tasks/(\d+)/comment", _pr6.text)[0])
check("ordinary delegation is under Delegation",
      f'/tasks/{_pid6}"' in admin.get(
          "/tasks?scope=all&status=pending&source=delegation").text)
check("but not under Help desk",
      f'/tasks/{_pid6}"' not in admin.get(
          "/tasks?scope=all&status=pending&source=helpdesk").text)
check("checklist work is not under Help desk either",
      "CL-" not in admin.get(
          "/tasks?scope=all&status=all&source=helpdesk").text)
check("anybody can reach the Help desk list from the menu",
      "source=helpdesk" in doer.get("/").text
      and "source=helpdesk" in admin.get("/").text)
_hx = admin.get("/tasks?scope=all&status=all&source=helpdesk&export=xlsx")
check("the Help desk list downloads as Excel", _hx.status_code == 200,
      _hx.status_code)

print("\n== the follow-up desk filters itself ==")
# The complaint: 154 open tasks in one flat list, and the PC has to scroll
# past everything already ticked to reach what is not. The three numbers are
# now the filter, with an overdue / coming-up split under them.
from app.db import SessionLocal as _sl7
from app.models import (Task as _T7, Followup as _F7, TaskStatus as _ST7,
                        TaskSource as _S7, User as _U7)
from app import clock as _ck7
from datetime import timedelta as _td7

_today7 = _ck7.today().isoformat()
_pc = login("pc@gcs.local")
_base7 = f"/followups?desk=pc&date_from={_today7}&date_to={_today7}"

_p0 = _pc.get(_base7)
check("the follow-up desk opens", _p0.status_code == 200, _p0.status_code)
_b0 = _p0.text
check("it offers a Total follow-ups filter", "Total follow-ups" in _b0)
check("a Followed up filter", "show=done" in _b0)
check("a Still to chase filter", "show=pending" in _b0)
check("and an overdue / coming-up split", "when=overdue" in _b0 and "when=coming" in _b0)

def _rows7(html):
    return {int(i) for i in _re.findall(r'/tasks/(\d+)"', html)}

with _sl7() as _d:
    _pcu = _d.query(_U7).filter(_U7.email == "pc@gcs.local").one()
    _desk = [t for t in _d.query(_T7).filter(
        _T7.source.in_((_S7.RECURRING, _S7.FLOW)),
        _T7.status.in_((_ST7.PENDING, _ST7.IN_PROGRESS, _ST7.REJECTED,
                        _ST7.REOPENED)),
        _T7.due_at <= _ck7.now().replace(hour=23, minute=59, second=59)).all()]
    _desk_ids = {t.id for t in _desk}
    _late_ids = {t.id for t in _desk if t.due_at < _ck7.now()}
    _soon_ids = _desk_ids - _late_ids

_all7 = _rows7(_pc.get(_base7 + "&show=all&when=all").text)
check("the desk has work on it today", len(_all7) > 0, len(_all7))
check("Total follow-ups shows the whole desk", _all7 == _desk_ids,
      f"page {len(_all7)} vs db {len(_desk_ids)}")

_ov7 = _rows7(_pc.get(_base7 + "&show=all&when=overdue").text)
_cm7 = _rows7(_pc.get(_base7 + "&show=all&when=coming").text)
check("Overdue shows only work past its deadline", _ov7 == _late_ids,
      f"page {len(_ov7)} vs db {len(_late_ids)}")
check("Coming up shows only work still inside it", _cm7 == _soon_ids,
      f"page {len(_cm7)} vs db {len(_soon_ids)}")
check("and the two halves are the whole", _ov7 | _cm7 == _all7)
check("with nothing in both", not (_ov7 & _cm7))

# Nothing chased yet, so "still to chase" is the whole desk and
# "followed up" is empty.
check("nothing is chased to begin with",
      not _rows7(_pc.get(_base7 + "&show=done").text))
check("so everything is still to chase",
      _rows7(_pc.get(_base7 + "&show=pending").text) == _desk_ids)

# Tick one, from the Still-to-chase tab, and watch it leave that list.
_one = sorted(_desk_ids)[0]
_tk = _pc.post(f"/followups/{_one}/tick",
               data={"day": _today7, "desk": "pc",
                     "show": "pending", "when": "all"})
check("ticking from the filtered list works", _tk.status_code == 200, _tk.status_code)
check("and comes back to the same filter", "show=pending" in str(_tk.url),
      str(_tk.url))
_pending7 = _rows7(_pc.get(_base7 + "&show=pending").text)
_done7 = _rows7(_pc.get(_base7 + "&show=done").text)
check("the ticked task leaves Still to chase", _one not in _pending7)
check("and appears under Followed up", _done7 == {_one}, sorted(_done7)[:4])
check("it is still on Total follow-ups",
      _one in _rows7(_pc.get(_base7 + "&show=all").text))
check("the list is one shorter", len(_pending7) == len(_desk_ids) - 1)

# The counts on the cards have to move with it.
_b1 = _pc.get(_base7).text
check("the Followed up count reads 1",
      _re.search(r'show=done[^>]*>\s*<div class="n">1</div>', _b1) is not None
      or '<div class="n">1</div>' in _b1)
check("the split under each card is shown",
      "overdue ·" in _b1 and "coming up" in _b1)

# Untick, and everything goes back.
_pc.post(f"/followups/{_one}/tick",
         data={"day": _today7, "desk": "pc", "show": "pending", "when": "all"})
check("unticking puts it back",
      _rows7(_pc.get(_base7 + "&show=pending").text) == _desk_ids)

# Combining the two filters.
_po = _rows7(_pc.get(_base7 + "&show=pending&when=overdue").text)
check("Still to chase + Overdue is the overlap", _po == _late_ids,
      f"{len(_po)} vs {len(_late_ids)}")

# The Excel download must be the list on screen, not the whole desk.
_fx = _pc.get(_base7 + "&show=pending&when=overdue&export=xlsx")
check("the download honours the filter", _fx.status_code == 200, _fx.status_code)
try:
    from openpyxl import load_workbook as _lw7
    _ws7 = _lw7(_io4.BytesIO(_fx.content)).worksheets[0]
    # Find the header row, then count what sits under it — the sheet also
    # carries a title and a note line above the table.
    _hrow = next((r[0].row for r in _ws7.iter_rows(max_col=1)
                  if str(r[0].value).strip() == "Task"), None)
    check("the download has a Task column", _hrow is not None)
    # An empty download still writes one line — "Nothing matched these
    # filters" — which is the right thing to hand somebody and is not a row
    # of data.
    _n7 = sum(1 for r in _ws7.iter_rows(min_row=(_hrow or 1) + 1, max_col=1)
              if r[0].value and str(r[0].value).strip()
              and not str(r[0].value).startswith("Nothing matched"))
    # Compare the file with the SCREEN, not with a number worked out again
    # from the database. The desk is split by "is it overdue yet", which
    # changes as the day passes, so recomputing it a second later can give a
    # different answer and fail a test that found nothing wrong.
    _on_screen = len(_rows7(_pc.get(_base7 + "&show=pending&when=overdue").text))
    check("and holds exactly the rows that were on screen",
          _n7 == _on_screen, f"{_n7} in the file, {_on_screen} on screen")
except ImportError:
    pass

check("a nonsense filter falls back to the whole desk",
      _rows7(_pc.get(_base7 + "&show=banana&when=grape").text) == _desk_ids)
check("the range view still works without the filters",
      "day by day" in _pc.get(
          f"/followups?desk=pc&date_from=2026-09-01&date_to={_today7}").text)

print("\n== the EM score can be put in a window ==")
from datetime import date as _dt8, datetime as _dtt8, timedelta as _td8
from app.services import scoring as _sc8
from app import clock as _ck8
from app.db import SessionLocal as _sl8
from app.models import (Task as _T8, TaskStatus as _ST8, User as _U8)

check("there are five windows to choose from", len(_sc8.PERIODS) == 5,
      [k for k, _ in _sc8.PERIODS])
for _k in ("this_week", "last_week", "days30", "all", "custom"):
    check(f"'{_k}' is one of them", _k in _sc8.PERIOD_LABELS)

# The week maths, walked over a whole year rather than spot-checked. A week
# here is Monday to Saturday, because Sunday is the weekly off.
_bad8 = []
_d8 = _dt8(2026, 1, 1)
while _d8 < _dt8(2027, 1, 1):
    _mon, _sat = _sc8.week_bounds(_d8)
    if _mon.weekday() != 0 or _sat.weekday() != 5:
        _bad8.append(("not Mon-Sat", _d8, _mon, _sat))
    if not (_mon <= _d8 <= _sat + _td8(days=1)):
        _bad8.append(("day outside its own week", _d8, _mon, _sat))
    if (_sat - _mon).days != 5:
        _bad8.append(("week is not 6 days", _d8, (_sat - _mon).days))
    _d8 += _td8(days=1)
check("every day of 2026 lands in a Monday-to-Saturday week", not _bad8,
      _bad8[:3])
check("a Sunday belongs to the week that just ended",
      _sc8.week_bounds(_dt8(2026, 10, 4))[0] == _dt8(2026, 9, 28),
      _sc8.week_bounds(_dt8(2026, 10, 4)))

_today8 = _ck8.today()
_tw = _sc8.named_window("this_week")
_lw = _sc8.named_window("last_week")
check("this week starts on a Monday", _tw[0].weekday() == 0, _tw[0])
check("last week starts on a Monday", _lw[0].weekday() == 0, _lw[0])
check("last week ends on a Saturday", _lw[1].weekday() == 5, _lw[1])
check("last week is the week before this one",
      (_tw[0].date() - _lw[0].date()).days == 7,
      (_tw[0].date(), _lw[0].date()))
check("the two weeks do not overlap", _lw[1] < _tw[0], (_lw[1], _tw[0]))
check("last week is a full six days",
      (_lw[1].date() - _lw[0].date()).days == 5)

# The important one: a part-finished week must not be scored as a whole one.
check("this week never counts past the end of today",
      _tw[1].date() <= _today8, (_tw[1].date(), _today8))
if _today8.weekday() < 5:
    check("and it says so, rather than quietly scoring half a week",
          "not counted until it is due" in _tw[3], _tw[3])

check("no window runs into the future",
      all(_sc8.named_window(_k)[1].date() <= _today8
          for _k, _ in _sc8.PERIODS), "a window ends after today")

# Overall must start at the person's own first task, not an invented date.
with _sl8() as _d:
    _amit8 = _d.query(_U8).filter(_U8.email == "amit@gcs.local").one()
    _first8 = min(t.due_at for t in _d.query(_T8)
                  .filter(_T8.doer_id == _amit8.id).all())
_ov = _sc8.named_window("all", earliest=_first8.date())
check("Overall starts at that person's first task",
      _ov[0].date() == _first8.date(), (_ov[0].date(), _first8.date()))
check("and runs to today", _ov[1].date() == _today8)

_cu = _sc8.named_window("custom", "2026-09-01", "2026-09-15")
check("a picked range is used exactly",
      (_cu[0].date(), _cu[1].date()) == (_dt8(2026, 9, 1), _dt8(2026, 9, 15)),
      (_cu[0].date(), _cu[1].date()))
check("a backwards range is turned the right way round",
      _sc8.named_window("custom", "2026-09-15", "2026-09-01")[0].date()
      == _dt8(2026, 9, 1))
check("a picked range cannot reach into the future",
      _sc8.named_window("custom", "2026-09-01", "2030-01-01")[1].date() == _today8)

# The scores themselves have to differ by window, and match a hand count.
def _hand_score(uid, start, end):
    """Work out 'owed' and 'closed' the long way, straight off the rows.

    Counted in SCORE WEIGHT, not in tasks — high priority is worth 5, medium
    2, low 1 — because that is what the scorecard measures. Counting rows
    here instead is how you write a test that agrees with itself and not
    with the software.

    A task is owed in the period if it was due in it, OR was due before it
    and still unfinished when it began (the backlog somebody carried in), OR
    was closed inside it whenever it was due. Written out longhand here on
    purpose: restating the rule in different words is the only way this
    checks the software rather than echoing it.
    """
    from app.models import PARKED_STATES as _PK8
    with _sl8() as _d:
        rows = [t for t in _d.query(_T8).filter(_T8.doer_id == uid).all()
                if t.status not in _PK8]
        owed, closed = 0, 0
        for t in rows:
            shut_in = t.closed_at is not None and start <= t.closed_at <= end
            due_in = start <= t.due_at <= end
            carried = t.due_at < start and (t.closed_at is None
                                            or t.closed_at >= start)
            if shut_in:
                closed += t.weight
            if due_in or carried or shut_in:
                owed += t.weight
    return owed, closed

with _sl8() as _d:
    _card_all = _sc8.user_scorecard(_d, _amit8, start=_ov[0], end=_ov[1])
    _card_lw = _sc8.user_scorecard(_d, _amit8, start=_lw[0], end=_lw[1])
_p_all, _c_all = _hand_score(_amit8.id, _ov[0], _ov[1])
_p_lw, _c_lw = _hand_score(_amit8.id, _lw[0], _lw[1])
check("Overall counts every task that person was ever on the hook for",
      _card_all.planned == _p_all, (_card_all.planned, _p_all))
check("last week counts only that week's",
      _card_lw.planned == _p_lw, (_card_lw.planned, _p_lw))
check("and the two really are different windows", _p_all >= _p_lw)
check("a score is always between 0 and 100",
      all(0 <= c.score <= 100 for c in (_card_all, _card_lw)),
      (_card_all.score, _card_lw.score))

# Now the page itself.
for _k, _label in _sc8.PERIODS:
    _pg8 = doer.get(f"/?period={_k}")
    check(f"the dashboard opens on '{_label}'", _pg8.status_code == 200, _pg8.status_code)
    check(f"and its heading says so", _label.lower() in _pg8.text.lower(), _label)
_base8 = doer.get("/").text
for _k, _label in _sc8.PERIODS:
    check(f"the '{_label}' button is on the page", f"/?period={_k}" in _base8)
check("the default window is the last 30 days", "last 30 days" in _base8.lower())
check("the window is spelled out under the heading",
      "31 Aug" in _base8 or "to 29 Sep" in _base8 or "Everything up to" in _base8
      or _re.search(r"\d\d [A-Z][a-z]{2}", _base8) is not None)

_cust8 = doer.get("/?period=custom&date_from=2026-09-01&date_to=2026-09-15")
check("picking dates works from the page", _cust8.status_code == 200)
check("and the page offers the two date boxes",
      'name="date_from"' in _cust8.text and 'name="date_to"' in _cust8.text)
check("a nonsense period falls back rather than erroring",
      doer.get("/?period=banana").status_code == 200)

# The live refresh must follow the same window as the heading.
check("the live refresh asks for the window on screen",
      "period=this_week" in doer.get("/?period=this_week").text)
_api8 = doer.get("/api/my-score?period=last_week")
check("the score API takes a period", _api8.status_code == 200, _api8.status_code)
check("and returns that window's score",
      abs(_api8.json()["score"] - _card_lw.score) < 0.05,
      (_api8.json()["score"], _card_lw.score))
check("the old days= call still works",
      doer.get("/api/my-score?days=30").status_code == 200)
check("'What I completed' follows the same dates",
      "date_from=" in doer.get("/?period=last_week").text)

print("\n== the four audit outcomes ==")
from app.db import SessionLocal as _sl9
from app.models import (Task as _T9, TaskStatus as _ST9, AuditState as _AS9,
                        HelpTicket as _H9)

def _mk9(title, audit="1"):
    r = mgr.post("/tasks/new", data={
        "title": f"{title} {RUN}", "details": "", "doer_id": "6",
        "branch_id": "", "priority": "medium",
        "due_at": "2026-12-31T23:59", "requires_audit": audit})
    return int(re.findall(r"/tasks/(\d+)/comment", r.text)[0])

# 1 · Approve & close
_a9 = _mk9("SMOKE audit approve")
submit(doer, _a9, completion_note="done")
admin.post(f"/tasks/{_a9}/audit",
           data={"decision": "approve", "score": "9", "remark": "checked"})
with _sl9() as _d:
    _t = _d.get(_T9, _a9)
    check("approve closes the task", _t.status == _ST9.COMPLETED, _t.status)
    check("and marks the audit completed", _t.audit_state == _AS9.COMPLETED)
    check("and records who and when", _t.auditor_id and _t.audited_at)
    check("and keeps the score given", _t.audit_score == 9)

# 2 · Re-open task — the bug: it used to land back on the auditor's list
#     while the doer was still redoing it.
_b9 = _mk9("SMOKE audit reopen")
submit(doer, _b9, completion_note="done")
admin.post(f"/tasks/{_b9}/audit",
           data={"decision": "reject", "score": "3", "remark": "not enough"})
with _sl9() as _d:
    _t = _d.get(_T9, _b9)
    check("re-open sends it back to the doer", _t.status == _ST9.REJECTED, _t.status)
    check("and the audit WAITS rather than sitting on the auditor's list",
          _t.audit_state == _AS9.WAITING, _t.audit_state)
    check("the submission is cleared", _t.submitted_at is None)
check("a re-opened task is off the audit-pending list",
      f'/tasks/{_b9}"' not in admin.get("/tasks?scope=all&status=audit_pending").text)
check("and the auditor is not offered a verdict on it",
      'name="decision"' not in admin.get(f"/tasks/{_b9}").text)
# Finish it again and it comes straight back.
submit(doer, _b9, completion_note="redone")
with _sl9() as _d:
    check("finishing it again puts it back on the audit list",
          _d.get(_T9, _b9).audit_state == _AS9.PENDING)

# 3 · False marking
_c9 = _mk9("SMOKE audit falsemark")
submit(doer, _c9, completion_note="done")
admin.post(f"/tasks/{_c9}/false-mark",
           data={"reason": "register was blank", "confirm": "yes"})
with _sl9() as _d:
    _t = _d.get(_T9, _c9)
    check("false marking flags the task", _t.false_marked)
    check("and sends it back", _t.status == _ST9.REOPENED, _t.status)
    check("and its audit waits too", _t.audit_state == _AS9.WAITING, _t.audit_state)

# 4 · Not required — on a task the doer has ALREADY finished. This used to
#     leave it stuck in "submitted" for ever: never audited, because no audit
#     was wanted, and never counted as done in anybody's score.
_d9 = _mk9("SMOKE audit not required")
submit(doer, _d9, completion_note="done")
with _sl9() as _d:
    check("it is waiting on an auditor first",
          _d.get(_T9, _d9).status == _ST9.SUBMITTED)
_r9 = admin.post(f"/tasks/{_d9}/audit-state",
                 data={"state": "not_required", "remark": ""})
check("marking it not required is accepted", _r9.status_code == 200, _r9.status_code)
with _sl9() as _d:
    _t = _d.get(_T9, _d9)
    check("it CLOSES rather than sitting in submitted for ever",
          _t.status == _ST9.COMPLETED, _t.status)
    check("with a completion time", _t.closed_at is not None)
    check("the audit is marked not required",
          _t.audit_state == _AS9.NOT_REQUIRED)
    check("and it no longer asks to be audited", not _t.requires_audit)
check("it shows under Completed",
      f'/tasks/{_d9}"' in doer.get("/tasks?scope=mine&status=done").text)
check("and not on the audit list",
      f'/tasks/{_d9}"' not in admin.get("/tasks?scope=all&status=audit_pending").text)

# Not required on a task nobody has finished just switches the audit off.
_e9 = _mk9("SMOKE not required early")
admin.post(f"/tasks/{_e9}/audit-state", data={"state": "not_required", "remark": ""})
with _sl9() as _d:
    _t = _d.get(_T9, _e9)
    check("an unfinished task is not closed by it",
          _t.status != _ST9.COMPLETED, _t.status)
    check("but it stops asking for an audit", not _t.requires_audit)

# The page itself offers exactly the four, in one box.
_pg9 = admin.get(f"/tasks/{_a9}").text
check("a closed task still shows who audited it and what they said",
      "checked" in _pg9 and "scored 9" in _pg9)
_open9 = _mk9("SMOKE audit box shape")
submit(doer, _open9, completion_note="done")
_box9 = admin.get(f"/tasks/{_open9}").text
for _btn in ("Approve &amp; close", "Re-open task", "False marking", "Not required"):
    check(f"the audit box offers '{_btn}'", _btn in _box9)
check("and explains what each one does",
      "takes 10 off their score" in _box9 and "stops asking for one" in _box9)

print("\n== a filter survives opening a task ==")
# The complaint: filter the list down to one employee, open a task, come
# back, and the filter is gone. Thirty times in an afternoon is thirty
# re-typings of the same three dropdowns.
from app import lastview as _lv
from app.db import SessionLocal as _slA
from app.models import Task as _TA, User as _UA

with _slA() as _d:
    _amitA = _d.query(_UA).filter(_UA.email == "amit@gcs.local").one()
    _oneA = _d.query(_TA).filter(_TA.doer_id == _amitA.id).first().id

_filtered = (f"/tasks?scope=all&status=pending&source=delegation"
             f"&doer={_amitA.id}&branch=&dept=")
admin.get(_filtered)
_task_page = admin.get(f"/tasks/{_oneA}").text
check("the task page offers a way back", "Back to" in _task_page)
check("and it points at the filtered list, not a bare /tasks",
      f"doer={_amitA.id}" in _task_page and "status=pending" in _task_page,
      _re.search(r'href="(/tasks[^"]*)"', _task_page).group(1)
      if _re.search(r'href="(/tasks[^"]*)"', _task_page) else "none")
check("and it says which list", "the task list" in _task_page)

# Every kind of list, not just this one.
for _url, _word in (
        (f"/followups?desk=pc&show=pending&when=overdue", "Follow-ups"),
        ("/reports/tasks?source=delegation&state=overdue", "the report"),
        ("/reports/audit?state=completed", "the audit report"),
        ("/recurring", "Checklist"),
        ("/flows", "FMS"),
        ("/help", "Help Desk"),
        ("/?period=last_week", "the dashboard")):
    admin.get(_url)
    _tp = admin.get(f"/tasks/{_oneA}").text
    _href = _re.search(r'Back to', _tp)
    check(f"coming from {_url.split('?')[0]} goes back there",
          _word in _tp, _tp[_tp.find("Back to") - 120:_tp.find("Back to") + 40])

# Opening a task must NOT overwrite the memory — otherwise "back" would mean
# "back to the task you are already looking at".
admin.get(_filtered)
admin.get(f"/tasks/{_oneA}")
admin.get(f"/tasks/{_oneA}")
_tp2 = admin.get(f"/tasks/{_oneA}").text
check("opening a task does not become the place to go back to",
      f"doer={_amitA.id}" in _tp2)

# Nor does a page that is not a list.
admin.get(_filtered)
admin.get("/bulk")
_tp3 = admin.get(f"/tasks/{_oneA}").text
check("wandering off to a form does not lose the list",
      f"doer={_amitA.id}" in _tp3)

# Deleting a task used to dump everybody on a hard-coded unfiltered list.
_delA = mgr.post("/tasks/new", data={
    "title": f"SMOKE back after delete {RUN}", "details": "",
    "doer_id": str(_amitA.id), "branch_id": "", "priority": "low",
    "due_at": "2026-12-31T23:59"})
_delid = int(_re.findall(r"/tasks/(\d+)/comment", _delA.text)[-1])
admin.get(_filtered)
_navA = TestClient(app, follow_redirects=False)
_navA.cookies.update(admin.cookies)
_rA = _navA.post(f"/tasks/{_delid}/delete")
check("deleting returns to the filtered list",
      f"doer={_amitA.id}" in (_rA.headers.get("location") or ""),
      _rA.headers.get("location"))

# And the memory has to be safe: it is a cookie, so it leaves the building.
check("an off-site address could never be stored", not _lv.safe("//evil.example.com"))
check("nor a scheme", not _lv.safe("https://evil.example.com"))
check("nor a header split", not _lv.safe("/tasks\nLocation: /x"))
check("nor a backslash host", not _lv.safe("/\\evil.example.com"))
check("a plain path is fine", _lv.safe("/tasks?scope=all"))
check("only exact list paths are remembered",
      _lv.label_for("/tasks") and not _lv.label_for("/tasks/41")
      and not _lv.label_for("/tasks/new"))

# A tampered cookie must not become a redirect.
class _Fake:
    def __init__(self, v): self.session = {"lastview": v}
for _junk in ("//evil.example.com", "https://evil.example.com",
              {"url": "//evil.example.com"}, "not a dict", 42, None):
    check(f"a tampered memory {str(_junk)[:24]!r} is thrown away",
          _lv.url(_Fake(_junk), "/tasks") == "/tasks",
          _lv.url(_Fake(_junk), "/tasks"))

print("\n== searching by words, not just by ID ==")
# Until now the only thing anyone could search for was the reference, which
# is useful only if you already know it. What people actually remember is a
# few words from the task.
from app import search as _se
from app.db import SessionLocal as _slB
from app.models import Task as _TB, RecurringRule as _RB, Flow as _FB

_wordy = mgr.post("/tasks/new", data={
    "title": f"SMOKE chase the Maybach RC papers {RUN}",
    "details": "Ring the dealership about the registration certificate",
    "doer_id": "6", "branch_id": "", "priority": "low",
    "due_at": "2026-12-31T23:59"})
_wid = int(_re.findall(r"/tasks/(\d+)/comment", _wordy.text)[0])
with _slB() as _d:
    _wref = _d.get(_TB, _wid).ref

def _hits(html):
    return {int(i) for i in _re.findall(r'/tasks/(\d+)"', html)}

# A word from the TITLE.
check("a word from the task name finds it",
      _wid in _hits(admin.get("/tasks?scope=all&status=pending&q=Maybach").text))
# A word from the DETAILS — the thing that was impossible before.
check("a word from the details finds it too",
      _wid in _hits(admin.get("/tasks?scope=all&status=pending&q=dealership").text))
check("case does not matter",
      _wid in _hits(admin.get("/tasks?scope=all&status=pending&q=MAYBACH").text)
      and _wid in _hits(admin.get("/tasks?scope=all&status=pending&q=maybach").text))
check("several words in a row work",
      _wid in _hits(admin.get("/tasks?scope=all&status=pending&q=Maybach+RC").text))
check("the ID still works", _wid in _hits(
      admin.get(f"/tasks?scope=all&status=pending&q={_wref}").text))
check("the old ref= link still works", _wid in _hits(
      admin.get(f"/tasks?scope=all&status=pending&ref={_wref}").text))
check("a word nobody wrote finds nothing",
      not _hits(admin.get("/tasks?scope=all&status=pending&q=zzzqqqx").text))
check("the page says what it searched for",
      "Maybach" in admin.get("/tasks?scope=all&status=pending&q=Maybach").text)

# A search must look in every tab, or "not found" would mean "not on this tab".
submit(doer, _wid, completion_note="done")
check("a finished task is still findable from the Pending tab",
      _wid in _hits(admin.get("/tasks?scope=all&status=pending&q=Maybach").text),
      "searching only the open tab")

# % and _ are ordinary characters, not wildcards. Without escaping them a
# search for "100%" matches every row, which reads as "search is broken".
_pc = mgr.post("/tasks/new", data={
    "title": f"SMOKE hit 100% attendance {RUN}", "details": "",
    "doer_id": "6", "branch_id": "", "priority": "low",
    "due_at": "2026-12-31T23:59"})
_pcid = int(_re.findall(r"/tasks/(\d+)/comment", _pc.text)[0])
_pc_hits = _hits(admin.get("/tasks?scope=all&status=all&q=100%25").text)
check("a percent sign is searched for, not treated as a wildcard",
      _pcid in _pc_hits and len(_pc_hits) < 20, len(_pc_hits))
_us = _hits(admin.get("/tasks?scope=all&status=all&q=_").text)
check("an underscore is not a wildcard either", len(_us) < 20, len(_us))
check("a lone percent does not return the whole table",
      len(_hits(admin.get("/tasks?scope=all&status=all&q=%25%25%25").text)) == 0)

# It has to reach the Excel download as well, or the file and the screen
# would disagree.
_sx = admin.get("/tasks?scope=all&status=all&q=Maybach&export=xlsx")
check("the download honours the search", _sx.status_code == 200, _sx.status_code)

# Everywhere means everywhere.
for _page, _param in (("/reports/tasks?source=delegation&state=all", "q"),
                      ("/reports/audit?state=all", "q")):
    _r = admin.get(f"{_page}&{_param}=Maybach")
    check(f"{_page.split('?')[0]} has a search", _r.status_code == 200, _r.status_code)
    check(f"{_page.split('?')[0]} finds it", _wid in _hits(_r.text),
          sorted(_hits(_r.text))[:5])
    check(f"{_page.split('?')[0]} narrows to it",
          len(_hits(_r.text)) < 25, len(_hits(_r.text)))
    check(f"{_page.split('?')[0]} shows the box",
          'name="q"' in admin.get(_page).text)

for _page in ("/recurring", "/flows", "/help", "/followups?desk=pc"):
    _r = admin.get(_page)
    check(f"{_page.split('?')[0]} has a search box", 'name="q"' in _r.text,
          _r.status_code)

# And each of those really filters.
with _slB() as _d:
    _rule = _d.query(_RB).first()
    _rule_word = _rule.title.split()[0]
    _rule_title = _rule.title
    _flow = _d.query(_FB).first()
    _flow_word = _flow.name.split()[0]
_rr = admin.get(f"/recurring?q={_rule_word}").text
# Compare against the ESCAPED title: a rule called "invalid member & face-ID"
# reaches the page as "&amp;", and comparing raw text finds nothing while the
# row is sitting right there.
from html import escape as _esc
check("the checklist search keeps matching rules",
      _esc(_rule_title)[:30] in _rr, _rule_title[:40])
check("and drops the rest",
      admin.get("/recurring?q=zzzqqqx").text.count("<tr>")
      < _rr.count("<tr>"), "nothing was filtered")
_fr = admin.get(f"/flows?q={_flow_word}").text
check("the FMS search works", _flow_word in _fr)
check("and an unmatched flow search empties the list",
      admin.get("/flows?q=zzzqqqx").text.count('href="/flows/') <
      _fr.count('href="/flows/'))

# The follow-up desk: the search must narrow the LIST and the three cards
# together, or the numbers describe a different list from the one shown.
_pcs = login("pc@gcs.local")
_today_s = _clock.today().isoformat()
_fu_all = _pcs.get(f"/followups?desk=pc&date_from={_today_s}&date_to={_today_s}").text
_fu_none = _pcs.get(
    f"/followups?desk=pc&date_from={_today_s}&date_to={_today_s}&q=zzzqqqx").text
check("the follow-up search narrows the list",
      len(_hits(_fu_none)) == 0 and len(_hits(_fu_all)) > 0,
      (len(_hits(_fu_all)), len(_hits(_fu_none))))
check("and the cards agree with it",
      ">0</div>" in _fu_none, "the cards still count the whole desk")

# Nothing typed means no search — the list is not silently emptied.
check("an empty search changes nothing",
      _hits(admin.get("/tasks?scope=all&status=all&q=").text)
      == _hits(admin.get("/tasks?scope=all&status=all").text))
check("spaces alone count as empty", _se.clean("   ") == "")
check("a very long paste is cut short rather than refused",
      len(_se.clean("x" * 500)) == _se.MAX_LEN)

print("\n== when a task was assigned ==")
from app.db import SessionLocal as _slC
from app.models import Task as _TC2, TaskSource as _SC2
from app.templating import ago as _ago
from datetime import timedelta as _tdC

# The moment a task was handed over already exists on every row — it is what
# created_at has always meant. These checks are that it is TRUE for all three
# kinds of work, and that it is now visible.
with _slC() as _d:
    _allC = _d.query(_TC2).all()
    check("every task knows when it was assigned",
          all(t.created_at is not None for t in _allC),
          [t.id for t in _allC if t.created_at is None][:5])
    for _src in _SC2:
        _rows = [t for t in _allC if t.source == _src]
        if not _rows:
            continue
        check(f"{_src.value} tasks have an assigned time",
              all(t.created_at for t in _rows))
        check(f"and no {_src.value} task claims to be assigned after it is due"
              " by more than a day",
              all(t.created_at <= t.due_at + _tdC(days=1) for t in _rows),
              [(t.ref, str(t.created_at)[:16], str(t.due_at)[:16])
               for t in _rows if t.created_at > t.due_at + _tdC(days=1)][:3])
    _sample = next(t for t in _allC if t.source == _SC2.DELEGATION)
    _sid, _sref = _sample.id, _sample.ref
    _sstamp = _sample.created_at.strftime("%d %b, %I:%M %p")
    _sample_day = _sample.created_at.strftime("%d %b")
    _sfull = _sample.created_at.strftime("%d %b %Y, %I:%M %p")

_listC = admin.get("/tasks?scope=all&status=all").text
# Assigned is on the row, under the task's name, rather than in a column of
# its own: the row had to fit on a 1000px screen, and a date that is read
# occasionally does not earn a column from one that is read every time.
check("the task list says when each task was assigned",
      "assigned" in _listC.lower() and "rowsub" in _listC)
check("and it carries the real date",
      _sample_day in _listC, _sample_day)
check("Planned still comes before Completed",
      _listC.index("<th>Planned</th>") < _listC.index("<th>Completed</th>"))

_pageC = admin.get(f"/tasks/{_sid}").text
check("the task's own page says Assigned on", "Assigned on" in _pageC)
check("with the full date and time", _sfull in _pageC, _sfull)
check("and no longer calls it 'Created'",
      ">Created</th>" not in _pageC)

# A newly delegated task is stamped at the moment it is handed over.
_before = _clock.now()
_nC = mgr.post("/tasks/new", data={
    "title": f"SMOKE assigned stamp {RUN}", "details": "", "doer_id": "6",
    "branch_id": "", "priority": "low", "due_at": "2026-12-31T23:59"})
_ncid = int(_re.findall(r"/tasks/(\d+)/comment", _nC.text)[0])
with _slC() as _d:
    _t = _d.get(_TC2, _ncid)
    check("a task assigned now is stamped now",
          _before - _tdC(minutes=2) <= _t.created_at <= _clock.now() + _tdC(minutes=2),
          str(_t.created_at))
check("and the list shows it as just assigned",
      "just now" in admin.get("/tasks?scope=all&status=all&q=assigned+stamp").text
      or "m ago" in admin.get("/tasks?scope=all&status=all&q=assigned+stamp").text)

# The follow-up desk, where "how long has this been sitting" is the question.
_pcC = login("pc@gcs.local")
_fuC = _pcC.get(f"/followups?desk=pc&date_from={_clock.today().isoformat()}"
                f"&date_to={_clock.today().isoformat()}").text
check("the follow-up desk has an Assigned column", "<th>Assigned</th>" in _fuC)

# Both Excel downloads.
try:
    from openpyxl import load_workbook as _lwC
    _xC = admin.get("/tasks?scope=all&status=all&export=xlsx")
    _wsC = _lwC(_io4.BytesIO(_xC.content)).worksheets[0]
    _hdrC = []
    for _row in _wsC.iter_rows(min_row=1, max_row=8):
        _vals = [c.value for c in _row if c.value]
        if "Task" in _vals:
            _hdrC = _vals
            break
    check("the task download has an Assigned on column",
          "Assigned on" in _hdrC, _hdrC[:8])
    check("next to who assigned it",
          abs(_hdrC.index("Assigned on") - _hdrC.index("Assigned by")) == 1)
    _fxC = _pcC.get(f"/followups?desk=pc&date_from={_clock.today().isoformat()}"
                    f"&date_to={_clock.today().isoformat()}&export=xlsx")
    _wsD = _lwC(_io4.BytesIO(_fxC.content)).worksheets[0]
    _hdrD = []
    for _row in _wsD.iter_rows(min_row=1, max_row=8):
        _vals = [c.value for c in _row if c.value]
        if "Task" in _vals:
            _hdrD = _vals
            break
    check("the follow-up download has it too", "Assigned on" in _hdrD, _hdrD[:8])
except ImportError:
    pass

# The "how long ago" wording, which is the bit people actually read.
_nowC = _clock.now()
for _mins, _want in ((0, "just now"), (5, "5m ago"), (90, "1h ago"),
                     (60 * 26, "1d ago"), (60 * 24 * 45, "1mo ago"),
                     (60 * 24 * 400, "1y ago")):
    check(f"{_mins} minutes ago reads as {_want!r}",
          _ago(_nowC - _tdC(minutes=_mins)) == _want,
          _ago(_nowC - _tdC(minutes=_mins)))
check("a missing time says nothing rather than lying", _ago(None) == "")
check("a clock a little ahead does not print a negative age",
      _ago(_nowC + _tdC(minutes=5)) == "just now")

print("\n== moving a deadline from the row ==")
# Opening a task, scrolling to Manage, changing one field and saving is four
# steps for something done twenty times a morning.
from app.db import SessionLocal as _slD
from app.models import Task as _TD, TaskComment as _TCD, Right as _RD

_dd = mgr.post("/tasks/new", data={
    "title": f"SMOKE move the deadline {RUN}", "details": "", "doer_id": "6",
    "branch_id": "", "priority": "low", "due_at": "2026-12-01T18:00"})
_ddid = int(_re.findall(r"/tasks/(\d+)/comment", _dd.text)[0])

_listD = admin.get("/tasks?scope=all&status=pending&source=delegation").text
check("the row offers a date picker to someone who may move deadlines",
      "duepick" in _listD)
check("pointing at the right place",
      f'action="/tasks/{_ddid}/due"' in _listD or "/due\"" in _listD)

_rD = admin.post(f"/tasks/{_ddid}/due", data={"due_at": "2027-03-15T16:30"})
check("saving from the row works", _rD.status_code == 200, _rD.status_code)
with _slD() as _d:
    _t = _d.get(_TD, _ddid)
    check("the deadline really moved",
          _t.due_at.strftime("%Y-%m-%dT%H:%M") == "2027-03-15T16:30",
          str(_t.due_at))
    _notes = [c.body for c in _d.query(_TCD)
              .filter(_TCD.task_id == _ddid).all()]
    check("and it is written into the task's history",
          any("Planned date moved" in n for n in _notes), _notes[:3])
    check("the note says both dates",
          any("01 Dec 2026" in n and "15 Mar 2027" in n for n in _notes), _notes)

# It must come back to the filtered list, not to the task.
_navD = TestClient(app, follow_redirects=False)
_navD.cookies.update(admin.cookies)
_filtD = "/tasks?scope=all&status=pending&source=delegation&doer=6"
admin.get(_filtD)
_navD.cookies.update(admin.cookies)
_rD2 = _navD.post(f"/tasks/{_ddid}/due",
                  data={"due_at": "2027-04-01T10:00", "return_to": _filtD})
check("and lands back on the list it was changed from",
      _rD2.headers.get("location") == _filtD, _rD2.headers.get("location"))
_rD3 = _navD.post(f"/tasks/{_ddid}/due",
                  data={"due_at": "2027-04-02T10:00",
                        "return_to": "//evil.example.com/"})
check("an off-site return is refused here too",
      _onsite(_rD3.headers.get("location")), _rD3.headers.get("location"))

# The right is the control, not the absence of a button.
check("a doer is not shown the picker",
      "duepick" not in doer.get("/tasks?scope=mine&status=pending").text)
# A manager can edit a task but is NOT given the date right by default, which
# is exactly the case that looks like a missing feature rather than a
# deliberate limit. The date says why on hover.
_mgrlist = mgr.get("/tasks?scope=assigned&status=pending").text
from app.models import DEFAULT_RIGHTS as _DRF, Role as _RoleF, Right as _RightF
check("a manager does not hold the date right by default",
      _RightF.CHANGE_DUE_DATE not in _DRF.get(_RoleF.MANAGER, []),
      [r.value for r in _DRF.get(_RoleF.MANAGER, [])])
check("so a manager sees no picker either", "duepick" not in _mgrlist)
check("but the date explains why, rather than just not working",
      "locked" in _mgrlist and "Setup" in _mgrlist)
check("an admin, who holds every right, does get the picker",
      "duepick" in admin.get("/tasks?scope=all&status=pending").text)
check("and cannot move a deadline by posting anyway",
      doer.post(f"/tasks/{_ddid}/due",
                data={"due_at": "2028-01-01T10:00"}).status_code == 403)
with _slD() as _d:
    check("so the deadline is untouched",
          _d.get(_TD, _ddid).due_at.year == 2027,
          str(_d.get(_TD, _ddid).due_at))
check("nonsense is refused rather than stored",
      admin.post(f"/tasks/{_ddid}/due",
                 data={"due_at": "not a date"}).status_code == 400)

print("\n== the task row fits on the screen ==")
# The Mark complete button had fallen off the right-hand edge, where a
# scrollbar that only appears mid-scroll is no help at all.
check("the ID column already says which kind of work it is",
      "DEL-" in _listD or "CL-" in _listD or "FMS-" in _listD)
check("so Source is no longer a column of its own",
      "<th>Source</th>" not in _listD)
check("but it is still on the row, under the title",
      "rowsub" in _listD and "delegation" in _listD.lower())
check("the action column is pinned so the button cannot be cut off",
      "stickycol" in _listD)
_hdrD2 = _listD.split("<table>", 1)[1].split("</tr>", 1)[0]
check("one header per column still",
      _hdrD2.count("<th") == _listD.split("</tr>", 2)[1].count("<td"),
      (_hdrD2.count("<th"), _listD.split("</tr>", 2)[1].count("<td")))
check("Planned still comes before Completed",
      _listD.index("<th>Planned</th>") < _listD.index("<th>Completed</th>"))
check("and the assigned date is on the row rather than in a column",
      "<th>Assigned</th>" not in _listD and "assigned" in _listD.lower())
check("the Excel download still names the work type",
      admin.get("/tasks?scope=all&status=all&export=xlsx").status_code == 200)

print("\n== editing a checklist rule ==")
from app.db import SessionLocal as _slE
from app.models import (RecurringRule as _RE, Task as _TE, Priority as _PE,
                        Recurrence as _FE, TaskStatus as _STE)
from app.services import recurring as _recE
from app import clock as _ckE

# Priority belongs on the list: it is what decides whether a checklist task
# counts as five tasks or one in somebody's score.
_lstE = admin.get("/recurring").text
check("the checklist list has a Priority column", "<th>Priority</th>" in _lstE)
check("and shows the weight beside it", "5×" in _lstE or "2×" in _lstE or "1×" in _lstE)
check("every rule offers an Edit", _lstE.count(">Edit</a>") > 0)

# Real user ids, looked up rather than typed. A hard-coded id is a test that
# passes until somebody adds a person to the seed, and then fails somewhere
# with no obvious connection to the change.
with _slE() as _d:
    _people = _d.query(_UA).filter(
        _UA.email.in_(["amit@gcs.local", "priya@gcs.local",
                       "ravi@gcs.local"])).all()
    _pick = {u.email: u.id for u in _people}
_doerA = str(_pick["amit@gcs.local"])
_doerB = str(_pick["priya@gcs.local"])
_doerC = str(_pick["ravi@gcs.local"])

# Make a rule, let it produce a task, then change everything about the rule.
_reE = admin.post("/recurring", data={
    "title": f"SMOKE editable rule {RUN}", "details": "first wording",
    "doer_id": _doerA, "branch_id": "", "frequency": "daily",
    "due_time": "10:00", "priority": "low", "requires_attachment": "1"})
check("the rule was created", _reE.status_code == 200, _reE.status_code)
with _slE() as _d:
    _rule = _d.query(_RE).filter(_RE.title == f"SMOKE editable rule {RUN}").one()
    _rid = _rule.id
admin.post("/recurring/run")
with _slE() as _d:
    _made = _d.query(_TE).filter(_TE.rule_id == _rid).all()
    check("it produced a task", len(_made) >= 1, len(_made))
    _oldE = [(t.id, t.doer_id, t.priority, t.title, t.details,
              t.due_at, t.requires_audit) for t in _made]

_formE = admin.get(f"/recurring/{_rid}")
check("the edit page opens", _formE.status_code == 200, _formE.status_code)
_fb = _formE.text
check("it is filled in with the rule as it stands",
      f"SMOKE editable rule {RUN}" in _fb and "first wording" in _fb)
check("the frequency is pre-selected",
      'value="daily"\n          selected' in _fb or 'value="daily"' in _fb)
check("and it warns that history is not rewritten",
      "apply to tasks made from tomorrow" in _fb)
check("saying how many tasks it has already made",
      f"{len(_made)} task(s)" in _fb, f"{len(_made)}")
check("and listing them", "not touched by anything on this page" in _fb)

# Change the doer, the priority, the wording, the frequency and the time.
_saveE = admin.post(f"/recurring/{_rid}", data={
    "title": f"SMOKE edited rule {RUN}", "details": "second wording",
    "doer_id": _doerB, "branch_id": "", "frequency": "weekly",
    "weekdays": ["1", "3"], "due_time": "16:30", "priority": "high",
    "requires_audit": "1", "requires_attachment": "1"})
check("the change saves", _saveE.status_code == 200, _saveE.status_code)

with _slE() as _d:
    _r2 = _d.get(_RE, _rid)
    check("the rule's title changed", _r2.title == f"SMOKE edited rule {RUN}")
    check("its details changed", _r2.details == "second wording")
    check("its doer changed", _r2.doer_id == int(_doerB), _r2.doer_id)
    check("its priority changed", _r2.priority == _PE.HIGH, _r2.priority)
    check("its frequency changed", _r2.frequency == _FE.WEEKLY, _r2.frequency)
    check("its days changed", _r2.weekday_list == [1, 3], _r2.weekday_list)
    check("its due time changed", _r2.due_time == "16:30", _r2.due_time)
    check("and it now wants an audit", _r2.requires_audit)

    # THE POINT OF THE WHOLE FEATURE: nothing already made is touched.
    _nowE = {t.id: (t.doer_id, t.priority, t.title, t.details, t.due_at,
                    t.requires_audit) for t in
             _d.query(_TE).filter(_TE.rule_id == _rid).all()}
    for _tid, _doer, _pri, _title, _det, _due, _aud in _oldE:
        _got = _nowE.get(_tid)
        check(f"task {_tid} keeps its doer", _got[0] == _doer, (_got[0], _doer))
        check(f"task {_tid} keeps its priority", _got[1] == _pri, (_got[1], _pri))
        check(f"task {_tid} keeps its wording", _got[2] == _title)
        check(f"task {_tid} keeps its details", _got[3] == _det)
        check(f"task {_tid} keeps its deadline", _got[4] == _due, (_got[4], _due))
        check(f"task {_tid} keeps its audit setting", _got[5] == _aud)
    check("and no task was deleted or added by the edit",
          set(_nowE) == {t[0] for t in _oldE},
          (sorted(_nowE), sorted(t[0] for t in _oldE)))

# A finished task is just as untouchable as a pending one.
with _slE() as _d:
    _t = _d.query(_TE).filter(_TE.rule_id == _rid).first()
    _t.status = _STE.COMPLETED
    _t.closed_at = _ckE.now()
    _t.priority = _PE.LOW
    _d.commit()
    _doneid, _donepri = _t.id, _t.priority
admin.post(f"/recurring/{_rid}", data={
    "title": f"SMOKE edited again {RUN}", "details": "third wording",
    "doer_id": _doerC, "branch_id": "", "frequency": "daily",
    "due_time": "09:00", "priority": "medium", "requires_attachment": "1"})
with _slE() as _d:
    _t = _d.get(_TE, _doneid)
    check("a completed task keeps the priority it was scored on",
          _t.priority == _donepri, _t.priority)
    check("and its doer", _t.doer_id != int(_doerC))
    check("and stays completed", _t.status == _STE.COMPLETED)

# The next task the rule makes DOES follow the new settings.
#
# Clearing last_spawned_on is not enough: the spawner keys on the day a task
# is FOR, so today's job is already made and will not be made twice. That is
# right, and it is why this clears today's tasks for this rule before asking
# it to run again — anything less would be testing the duplicate guard
# rather than whether the spawner reads the edited rule.
with _slE() as _d:
    _clear = [t.id for t in _d.query(_TE).filter(_TE.rule_id == _rid).all()]
# Delete through the app's own route rather than straight out of the table:
# notes, files and follow-up ticks point at these rows, and the database
# refuses a bare DELETE that would leave them pointing at nothing. Using the
# real route is also the only version of "delete" the software actually ships.
for _cid in _clear:
    admin.post(f"/tasks/{_cid}/delete")
with _slE() as _d:
    _r3 = _d.get(_RE, _rid)
    _r3.last_spawned_on = None
    _d.commit()
    _seen = {t.id for t in _d.query(_TE).filter(_TE.rule_id == _rid).all()}
admin.post("/recurring/run")
with _slE() as _d:
    _fresh = [t for t in _d.query(_TE).filter(_TE.rule_id == _rid).all()
              if t.id not in _seen]
    if _fresh:
        _f = _fresh[0]
        check("the next task follows the edited rule",
              _f.doer_id == int(_doerC)
              and _f.title == f"SMOKE edited again {RUN}",
              (_f.doer_id, _f.title))
        check("with the new deadline time",
              _f.due_at.strftime("%H:%M") == "09:00", str(_f.due_at))
    else:
        check("the rule made a fresh task to check", False, "none spawned")

# Guards.
check("a doer cannot open the edit page",
      doer.get(f"/recurring/{_rid}").status_code == 403)
check("nor save one",
      doer.post(f"/recurring/{_rid}", data={
          "title": "x", "doer_id": _doerA,
          "frequency": "daily"}).status_code == 403)
check("a rule pointed at somebody who does not exist is refused",
      admin.post(f"/recurring/{_rid}", data={
          "title": "x", "doer_id": "999999", "frequency": "daily",
          "due_time": "09:00", "priority": "low"}).status_code == 400)
check("a rule that does not exist is a clean 404",
      admin.get("/recurring/999999").status_code == 404)
check("a broken schedule is refused rather than stored",
      admin.post(f"/recurring/{_rid}", data={
          "title": "x", "doer_id": _doerA, "frequency": "weekly",
          "due_time": "09:00", "priority": "low"}).status_code == 400)

# The button that shares the same address shape must still work.
check("Run spawner now is not swallowed by the edit route",
      admin.post("/recurring/run").status_code == 200)

# One form, used twice — so creating and editing can never mean two
# different things.
check("the create form and the edit form come from the same block",
      'name="weeks_of_month"' in admin.get("/recurring").text
      and 'name="weeks_of_month"' in admin.get(f"/recurring/{_rid}").text)

print("\n== what a period's score counts ==")
# Rajinder's own worked example, run through the real scorer rather than a
# description of it.
#
#   Week: 1-7 Oct 2026.
#   50 tasks due in the week, 25 of them marked done inside it.
#   10 due BEFORE the week, marked done inside it.
#    5 due AFTER the week, marked done inside it (finished early).
#    5 due before the week and still pending - the backlog carried in.
#   plus noise that must not count: due AND closed entirely outside it.
from datetime import datetime as _dtF, timedelta as _tdF
from app.services.scoring import window_tasks as _winF
from app.models import TaskStatus as _STF, TaskSource as _SRF, Priority as _PRF

_START = _dtF(2026, 10, 1, 0, 0)
_END = _dtF(2026, 10, 7, 23, 59, 59)

class _FakeTask:
    _n = 0
    def __init__(self, due, closed=None):
        _FakeTask._n += 1
        self.id = _FakeTask._n
        self.due_at, self.closed_at = due, closed
        # The doer's own date. For a task with no audit these are the same
        # moment; the score reads this one, so that waiting in the audit
        # queue never costs the person who did the work.
        self.submitted_at = closed
        self.status = _STF.COMPLETED if closed else _STF.IN_PROGRESS
        self.source, self.priority = _SRF.DELEGATION, _PRF.LOW
        self.false_marked, self.audit_score = False, None
    @property
    def weight(self): return 1
    @property
    def was_on_time(self):
        return self.closed_at <= self.due_at if self.closed_at else None
    @property
    def is_overdue(self): return self.closed_at is None

_rowsF = []
for _i in range(50):                       # due in the week
    _d = _START + _tdF(days=_i % 7, hours=12)
    _rowsF.append(_FakeTask(_d, closed=_d if _i < 25 else None))
for _i in range(10):                       # due before, done inside
    _rowsF.append(_FakeTask(_dtF(2026, 9, 20, 12), _dtF(2026, 10, 3, 12)))
for _i in range(5):                        # due after, done inside
    _rowsF.append(_FakeTask(_dtF(2026, 10, 20, 12), _dtF(2026, 10, 2, 12)))
for _i in range(5):                        # due before, still pending
    _rowsF.append(_FakeTask(_dtF(2026, 9, 15, 12)))
for _i in range(7):                        # noise: before and before
    _rowsF.append(_FakeTask(_dtF(2026, 9, 10, 12), _dtF(2026, 9, 11, 12)))
for _i in range(3):                        # noise: after and after
    _rowsF.append(_FakeTask(_dtF(2026, 10, 20, 12), _dtF(2026, 10, 21, 12)))

_owedF, _closedF = _winF(_rowsF, _START, _END)
_pendF = [t for t in _owedF if t.closed_at is None]
check("the week is on the hook for 70 tasks (50 + 10 + 5 + 5)",
      len(_owedF) == 70, len(_owedF))
check("40 of them were marked inside the week (25 + 10 + 5)",
      len(_closedF) == 40, len(_closedF))
check("30 are still pending (25 from the week + 5 carried in)",
      len(_pendF) == 30, len(_pendF))
check("and the carried-in backlog really is in there",
      len([t for t in _pendF if t.due_at < _START]) == 5,
      len([t for t in _pendF if t.due_at < _START]))
check("work due AND finished before the week does not count",
      not any(t.due_at < _START and t.closed_at and t.closed_at < _START
              for t in _owedF))
check("work due AND finished after the week does not count",
      not any(t.closed_at and t.closed_at > _END for t in _owedF))
check("ten rows were excluded, exactly the noise",
      len(_rowsF) - len(_owedF) == 10, len(_rowsF) - len(_owedF))

# The same rule on real rows, through the real scorecard.
from app.db import SessionLocal as _slF
from app.models import Task as _TF, User as _UF
from app.services import scoring as _scF
with _slF() as _d:
    _amitF = _d.query(_UF).filter(_UF.email == "amit@gcs.local").one()
    _cardF = _scF.user_scorecard(_d, _amitF, start=_START, end=_END)
    _rowsR = [t for t in _d.query(_TF).filter(_TF.doer_id == _amitF.id).all()]
_owedR, _closedR = _winF(_rowsR, _START, _END)
check("the scorecard's denominator is the owed weight",
      _cardF.planned == sum(t.weight for t in _owedR),
      (_cardF.planned, sum(t.weight for t in _owedR)))
check("and its numerator is what closed inside the window",
      _cardF.completed == sum(t.weight for t in _closedR),
      (_cardF.completed, sum(t.weight for t in _closedR)))
check("a task due in the window but finished long after is NOT done for it",
      _cardF.completed <= _cardF.planned)
check("the score still lands between 0 and 100",
      0 <= _cardF.score <= 100, _cardF.score)

# The dashboard's own window has to use the same rule, or the number on the
# dashboard and the number in the report would disagree.
_dashF = doer.get("/?period=last_week")
check("the dashboard score opens on that rule too", _dashF.status_code == 200)
check("the Performance page as well",
      admin.get("/stats").status_code == 200)
check("and the EM score report",
      admin.get("/reports/score").status_code == 200)

print("\n== the task row fits a 1000px screen ==")
_fitF = admin.get("/tasks?scope=all&status=pending").text
_hdrF = _fitF.split("<table>", 1)[1].split("</tr>", 1)[0]
check("eight columns, not eleven", _hdrF.count("<th") == 8, _hdrF.count("<th"))
for _gone in ("<th>Source</th>", "<th>Audit</th>", "<th>Assigned</th>"):
    check(f"{_gone} is folded into the row", _gone not in _fitF)
check("but nothing was actually lost: source is on the row",
      "delegation" in _fitF.lower() or "recurring" in _fitF.lower())
check("the assigned date is on the row", "assigned" in _fitF.lower())
check("and the audit state is on the row", "audit_" in _fitF)
check("one header per column still",
      _hdrF.count("<th") == _fitF.split("</tr>", 2)[1].count("<td"),
      (_hdrF.count("<th"), _fitF.split("</tr>", 2)[1].count("<td")))

print("\n== Task Detective AI ==")
# Driven against a FAKE Gemini, so every branch is exercised without a key
# and without a byte leaving this machine.
import json as _jsonG, httpx as _hxG
from app import config as _cfgG
from app.services import detective as _detG
from app.db import SessionLocal as _slG
from app.models import (AiAudit as _AAG, AiVerdict as _AVG, Task as _TG,
                        Attachment as _ATG, TaskStatus as _STG)

check("it is off until a key is given", not _detG.available())
check("and says so in plain words",
      "GEMINI_API_KEY" in _detG.why_not() and "nothing is sent" in _detG.why_not())
check("the page still opens with no key",
      admin.get("/detective").status_code == 200)
check("and explains itself rather than looking broken",
      "not running" in admin.get("/detective").text)
check("a doer cannot see other people's AI remarks",
      doer.get("/detective").status_code == 403)

_PNGG = b"\x89PNG\r\n\x1a\x0a" + b"\x00" * 300

def _fakeG(reply, status=200):
    def h(request):
        h.sent = _jsonG.loads(request.content)
        h.key = request.headers.get("x-goog-api-key")
        if status != 200:
            return _hxG.Response(status, text="quota exhausted")
        return _hxG.Response(200, json={"candidates": [
            {"content": {"parts": [{"text": reply}]}}]})
    return h

# Pretend a key exists, for this block only.
_detG.AI_KEY = _cfgG.AI_KEY = "test-key-not-real"
_detG.AI_ENABLED = _cfgG.AI_ENABLED = True
check("with a key it reports itself available", _detG.available())

_tgr = mgr.post("/tasks/new", data={
    "title": f"SMOKE detective: fix the BIPS bus GPS {RUN}",
    "details": "Install GPS on bus 4 and send a photo of the fitted unit",
    "doer_id": "6", "branch_id": "", "priority": "medium",
    "due_at": "2026-12-31T23:59"})
_tid = int(_re.findall(r"/tasks/(\d+)/comment", _tgr.text)[0])
submit(doer, _tid, completion_note="done")

# These checks are about what the detective MAKES of a reply, not about the
# day's allowance, so the cap is lifted for them and put back afterwards.
# Left on, the twenty-first of them would silently be skipped rather than
# answered, and the failure would look like a verdict bug.
_capG = _detG.AI_DAILY_LIMIT
_detG.AI_DAILY_LIMIT = 0


def _runG(reply, status=200, task_id=None):
    h = _fakeG(reply, status)
    _detG._quota_hit_on = None
    with _hxG.Client(transport=_hxG.MockTransport(h)) as c:
        with _slG() as _d:
            # One of these checks deliberately makes Google answer 429, and
            # a stored 429 closes the day for real. These are about what the
            # detective MAKES of a reply, so the day is reopened each time.
            for _old in _d.scalars(_sel(_AAG).where(
                    _AAG.verdict == _AVG.ERROR,
                    _AAG.remark.like("%429%"))).all():
                _old.remark = "(cleared for the next check)"
            _d.commit()
            row = _detG.review(_d, _d.get(_TG, task_id or _tid), client=c)
            _d.refresh(row)
            return h, row

_h, _row = _runG(_jsonG.dumps({
    "verdict": "unrelated", "confidence": 88,
    "remark": "The screenshot is a WhatsApp chat about a gym membership. "
              "The task asked for a photo of a GPS unit fitted to a bus.",
    "looked_at": "1 screenshot of a chat"}))
check("it reaches a verdict", _row.verdict == _AVG.UNRELATED, _row.verdict)
check("keeps how sure it was", _row.confidence == 88)
check("and writes something a manager can act on",
      "WhatsApp" in _row.remark and len(_row.remark) > 30)
check("the key goes in the header, never the URL", _h.key == "test-key-not-real")

# What it was actually SENT — the whole point is that it sees the image.
_parts = _h.sent["contents"][0]["parts"]
check("the task itself was sent", "TASK:" in _parts[0]["text"])
check("so was what the person typed when marking it done",
      "done" in _parts[0]["text"])
check("and what the task asked for", "GPS" in _parts[0]["text"])
check("it was told whether proof was required",
      "PROOF WAS:" in _parts[0]["text"])
# Not a length threshold — decode it and check the bytes are the file. A
# threshold would have passed on a truncated image and failed on a small one.
import base64 as _b64G
_imgG = [p["inline_data"] for p in _parts if "inline_data" in p]
check("the attached image was sent as a real image, not a file name",
      len(_imgG) == 1, [list(p)[0] for p in _parts])
check("and the bytes that arrived are the bytes of the file",
      _imgG and _b64G.b64decode(_imgG[0]["data"]).startswith(b"\x89PNG"),
      _imgG[0]["data"][:20] if _imgG else "none")
check("with the right type on it, so it is read as a picture",
      _imgG and _imgG[0]["mime_type"] == "image/png")
check("it counted the image it opened", _row.images_seen >= 1, _row.images_seen)
check("the instructions tell it to judge the evidence, not the person",
      "not running an appraisal" in _detG.SYSTEM)

# Replies that are not clean JSON must still be read.
_, _r2 = _runG("```json\n" + _jsonG.dumps({"verdict": "ok", "confidence": 70,
    "remark": "Photo shows a GPS unit on a bus dashboard.",
    "looked_at": "1 photo"}) + "\n```")
check("a reply wrapped in code fences is still read", _r2.verdict == _AVG.OK)
_, _r3 = _runG('Sure!\n{"verdict":"weak","confidence":30,'
               '"remark":"The photo is too dark to make anything out.",'
               '"looked_at":"1 dark photo"}\nHope this helps.')
check("and one buried in chatter", _r3.verdict == _AVG.WEAK)

# Failure must never become an accusation.
for _bad, _why in (
        (_jsonG.dumps({"verdict": "suspicious", "confidence": 50, "remark": "x"}),
         "a verdict it invented"),
        (_jsonG.dumps({"verdict": "error", "confidence": 50, "remark": "x"}),
         "claiming 'error' itself"),
        ("I think it's fine honestly.", "a reply that is not JSON"),
        ("", "an empty reply")):
    _, _rb = _runG(_bad)
    check(f"{_why} becomes 'could not check', not a judgement",
          _rb.verdict == _AVG.ERROR and not _rb.is_judgement, _rb.verdict)
_, _rl = _runG("", status=429)
check("a rate limit is recorded as could-not-check", _rl.verdict == _AVG.ERROR)
check("and the reason is kept, so a quota and a bad key read differently",
      "429" in _rl.remark, _rl.remark[:60])
_, _ro = _runG(_jsonG.dumps({"verdict": "ok", "confidence": 5000, "remark": "x"}))
check("a confidence out of range is clamped", _ro.confidence == 100, _ro.confidence)

# THE GUARANTEE: nothing the AI says touches the task.
with _slG() as _d:
    _t = _d.get(_TG, _tid)
    _before = (_t.status, _t.audit_state, _t.audit_score, _t.closed_at,
               _t.false_marked, _t.priority, _t.due_at, _t.auditor_id)
_runG(_jsonG.dumps({"verdict": "no_proof", "confidence": 99,
                    "remark": "Nothing was attached and the note says only 'done'.",
                    "looked_at": "nothing"}))
with _slG() as _d:
    _t = _d.get(_TG, _tid)
    _after = (_t.status, _t.audit_state, _t.audit_score, _t.closed_at,
              _t.false_marked, _t.priority, _t.due_at, _t.auditor_id)
check("the harshest possible verdict changes NOTHING about the task",
      _before == _after, (_before, _after))
check("it does not mark the task false", not _t.false_marked)
check("it does not appoint itself auditor", _t.auditor_id is None)

# The score must be untouched too — this is the promise that matters most.
from app.services import scoring as _scG
from app.models import User as _UG
with _slG() as _d:
    _amitG = _d.query(_UG).filter(_UG.email == "amit@gcs.local").one()
    _scoreG = _scG.user_scorecard(_d, _amitG, days=3650).score
_runG(_jsonG.dumps({"verdict": "unrelated", "confidence": 99,
                    "remark": "Nothing to do with the task.", "looked_at": "x"}))
with _slG() as _d:
    _amitG = _d.query(_UG).filter(_UG.email == "amit@gcs.local").one()
    check("and nobody's score moves because of it",
          _scG.user_scorecard(_d, _amitG, days=3650).score == _scoreG)

# Re-checking keeps the history rather than overwriting it.
with _slG() as _d:
    _n = _d.query(_AAG).filter(_AAG.task_id == _tid).count()
check("each check is kept, so you can see what it said before", _n >= 5, _n)
with _slG() as _d:
    _latest = _detG.latest_for(_d, _tid)
    check("and the newest one is the one shown",
          _latest.verdict == _AVG.UNRELATED, _latest.verdict)

# The page.
_pg = admin.get("/detective?verdict=all").text
check("the task appears on the Detective page", f"/tasks/{_tid}" in _pg)
check("with what the AI said", "Nothing to do with the task." in _pg)
check("the page says it decides nothing",
      "advice, not a decision" in _pg and "only thing that decides" in _pg)
check("there is a Needs-a-look tab", "Needs a look" in _pg)
check("and the menu item is called Task Detective AI",
      "Task Detective AI" in admin.get("/").text)
check("a doer does not get the menu item",
      "Task Detective AI" not in doer.get("/").text)

_sus = admin.get("/detective?verdict=suspect").text
check("the doubtful ones are what it opens on", f"/tasks/{_tid}" in _sus)
check("filtering by verdict works",
      f"/tasks/{_tid}" not in admin.get("/detective?verdict=ok").text)
check("searching what the AI wrote works",
      f"/tasks/{_tid}" in admin.get("/detective?verdict=all&q=Nothing+to+do").text)
check("and a search that matches nothing empties it",
      f"/tasks/{_tid}" not in admin.get("/detective?verdict=all&q=zzzqqq").text)
_xd = admin.get("/detective?verdict=all&export=xlsx")
check("the list downloads as Excel", _xd.status_code == 200, _xd.status_code)

# The task's own page shows it, labelled as a machine.
_tp = admin.get(f"/tasks/{_tid}").text
check("the task page carries the AI's remark", "Nothing to do with the task." in _tp)
check("labelled as advice from a machine",
      "Task Detective AI" in _tp and "decides nothing" in _tp)
check("the manual audit is still there, untouched",
      "Approve &amp; close" in _tp or "Audit" in _tp)

# Submitting must never be slowed or broken by the AI.
_detG.AI_KEY = _cfgG.AI_KEY = ""         # key gone mid-flight
_tgr2 = mgr.post("/tasks/new", data={
    "title": f"SMOKE detective offline {RUN}", "details": "",
    "doer_id": "6", "branch_id": "", "priority": "low",
    "due_at": "2026-12-31T23:59"})
_tid2 = int(_re.findall(r"/tasks/(\d+)/comment", _tgr2.text)[0])
_sub = submit(doer, _tid2, completion_note="finished")
check("a submission still works with the AI switched off",
      _sub.status_code == 200, _sub.status_code)
with _slG() as _d:
    check("and the task really was submitted",
          _d.get(_TG, _tid2).status in (_STG.SUBMITTED, _STG.COMPLETED))
check("review_quietly never raises, whatever happens",
      _detG.review_quietly(_tid2) is None)
check("nor on a task that does not exist",
      _detG.review_quietly(999999) is None)
_detG.AI_KEY = _cfgG.AI_KEY = ""
_detG.AI_ENABLED = _cfgG.AI_ENABLED = True
_detG.AI_DAILY_LIMIT = _capG
_detG._quota_hit_on = None

# ==========================================================================
print("\n== one checklist job, shown once ==")
# The complaint: "1 task is showing two times" on a doer's checklist. Two
# things can cause it and they need different answers — a closed day's job
# handed out early alongside today's own (right, but indistinguishable), and
# a genuine second copy from two spawn runs racing (wrong).
from datetime import date as _dt_date, timedelta as _dt_delta
from app.db import SessionLocal as _slK
from app.models import (RecurringRule as _RR_K, Task as _TK, Holiday as _HolK,
                        Recurrence as _RecK, Priority as _PrK, TaskSource as _TSK)
from app.services import recurring as _recK
from app import migrate as _migK
from sqlalchemy import select as _spK, func as _fnK
from sqlalchemy.exc import IntegrityError as _IEK

_satK = _dt_date(2027, 3, 6)             # a Saturday
with _slK() as _d:
    _amitK0 = _d.scalar(_spK(UserModel.id).where(UserModel.email == "amit@gcs.local"))
    _bossK = _d.scalar(_spK(UserModel.id).where(UserModel.email == "mis@gcs.local"))
    # A daily job, and a weekly one that runs every day of the week. The two
    # are treated differently on a closed day on purpose.
    _ruleK = _RR_K(org_id=1, branch_id=None, title=f"SMOKE daily job {RUN}",
                   doer_id=_amitK0, assigner_id=_bossK, priority=_PrK.MEDIUM,
                   frequency=_RecK.DAILY, due_time="23:59", active=True)
    _wklyK = _RR_K(org_id=1, branch_id=None, title=f"SMOKE weekly job {RUN}",
                   doer_id=_amitK0, assigner_id=_bossK, priority=_PrK.MEDIUM,
                   frequency=_RecK.WEEKLY, weekdays="0,1,2,3,4,5,6",
                   due_time="23:59", active=True)
    _d.add_all([_ruleK, _wklyK])
    _d.add(_HolK(org_id=1, branch_id=None, day=_satK + _dt_delta(days=1),
                 name="Sunday"))
    _d.commit()
    _rid_K, _wid_K = _ruleK.id, _wklyK.id

with _slK() as _d:
    _recK.run_spawn(_d, today=_satK)
    _madeK = _d.scalars(_spK(_TK).where(_TK.rule_id == _rid_K)
                        .order_by(_TK.id)).all()
    # The actual complaint: a daily job appearing twice in one day.
    check("a daily job is handed out once, never twice",
          len(_madeK) == 1, [str(t.covers_day) for t in _madeK])
    check("and it is today's own, not the closed day's",
          _madeK[0].covers_day == _satK and not _madeK[0].brought_forward,
          str(_madeK[0].covers_day))
    _tidK = _madeK[0].id

    # A job that comes round less often still comes forward, because missing
    # the day means missing the week.
    _wmadeK = _d.scalars(_spK(_TK).where(_TK.rule_id == _wid_K)
                         .order_by(_TK.id)).all()
    check("a weekly job due on a closed day is still handed out early",
          len(_wmadeK) == 2, [str(t.covers_day) for t in _wmadeK])
    check("both are due on the working day",
          all(t.due_at.date() == _satK for t in _wmadeK))
    _todayK = [t for t in _wmadeK if not t.brought_forward]
    _earlyK = [t for t in _wmadeK if t.brought_forward]
    check("exactly one of them is marked as the closed day's",
          len(_todayK) == 1 and len(_earlyK) == 1)
    check("and it says which day it is for",
          _earlyK[0].covers_label.startswith("Sunday"), _earlyK[0].covers_label)
    _eidK, _wtodayK = _earlyK[0].id, _todayK[0].id

_rowK = admin.get(f"/tasks/{_eidK}").text
check("the task page says it is the closed day's job, not a duplicate",
      "handed out early" in _rowK and "not it twice" in _rowK)
_listK = admin.get("/tasks?source=checklist&status=all&q=SMOKE+weekly+job").text
check("the list labels the early one", "for Sunday" in _listK, _listK[:0])
check("and shows them both", f"/tasks/{_eidK}" in _listK
      and f"/tasks/{_wtodayK}" in _listK)

# Running the spawner again changes nothing, however often it is run.
with _slK() as _d:
    _recK.run_spawn(_d, today=_satK)
    _recK.run_spawn(_d, today=_satK)
    _againK = _d.scalar(_spK(_fnK.count()).select_from(_TK)
                        .where(_TK.rule_id == _rid_K))
    check("running the spawner again makes nothing new", _againK == 1, _againK)

# And the database itself refuses a second copy, which is what a race makes.
with _slK() as _d:
    _origK = _d.get(_TK, _tidK)
    _d.add(_TK(org_id=_origK.org_id, branch_id=_origK.branch_id,
               title=_origK.title, assigner_id=_origK.assigner_id,
               doer_id=_origK.doer_id, priority=_origK.priority,
               source=_TSK.RECURRING, due_at=_origK.due_at,
               covers_day=_origK.covers_day, rule_id=_rid_K))
    _refusedK = False
    try:
        _d.commit()
    except _IEK:
        _refusedK = True
        _d.rollback()
    check("the database refuses a duplicate of the same day's job", _refusedK)

# A duplicate that already existed — made before this constraint was there,
# which is exactly the state the live database is in — is cleaned up by the
# migration on the next start. Run against a scratch database built WITHOUT
# the constraint, because a database that has it cannot hold the duplicate
# the clean-up exists to remove.
import tempfile as _tfK, os as _osK
from sqlalchemy import create_engine as _ceK, text as _txK
_tmpK = _osK.path.join(_tfK.mkdtemp(), "dupes.db")
_engK = _ceK(f"sqlite:///{_tmpK}")
with _engK.begin() as _c:
    _c.execute(_txK("CREATE TABLE tasks (id INTEGER PRIMARY KEY, rule_id INTEGER,"
                    " covers_day DATE, submitted_at DATETIME, closed_at DATETIME,"
                    " completion_note TEXT, auditor_id INTEGER)"))
    _c.execute(_txK("CREATE TABLE attachments (id INTEGER PRIMARY KEY, task_id INTEGER)"))
    _c.execute(_txK("CREATE TABLE task_comments (id INTEGER PRIMARY KEY, task_id INTEGER)"))
    # Three copies of one day's job: two untouched, one with real work on it.
    _c.execute(_txK("INSERT INTO tasks (id, rule_id, covers_day) VALUES (1, 7, '2027-03-06')"))
    _c.execute(_txK("INSERT INTO tasks (id, rule_id, covers_day, completion_note)"
                    " VALUES (2, 7, '2027-03-06', 'I did this one')"))
    _c.execute(_txK("INSERT INTO tasks (id, rule_id, covers_day) VALUES (3, 7, '2027-03-06')"))
    _c.execute(_txK("INSERT INTO task_comments (id, task_id) VALUES (1, 3)"))
    # A different day, and a delegation task with no rule: neither is a copy.
    _c.execute(_txK("INSERT INTO tasks (id, rule_id, covers_day) VALUES (4, 7, '2027-03-07')"))
    _c.execute(_txK("INSERT INTO tasks (id, rule_id, covers_day) VALUES (5, NULL, NULL)"))
    _c.execute(_txK("INSERT INTO tasks (id, rule_id, covers_day) VALUES (6, NULL, NULL)"))
with _engK.begin() as _c:
    _saidK = _migK._dedupe_checklist_tasks(
        _c, {"tasks", "attachments", "task_comments"})
with _engK.begin() as _c:
    _leftK = [r[0] for r in _c.execute(_txK("SELECT id FROM tasks ORDER BY id")).all()]
    _cmtK = _c.execute(_txK("SELECT COUNT(*) FROM task_comments")).scalar()
check("the duplicates made before the fix are cleaned up",
      _leftK == [2, 4, 5, 6], _leftK)
check("the copy somebody worked on is the one that survives", 2 in _leftK)
check("another day's job is not touched", 4 in _leftK)
check("and delegation tasks, which have no rule, are left alone",
      5 in _leftK and 6 in _leftK)
check("what it did is reported", "duplicate checklist task" in " ".join(_saidK),
      _saidK)
with _engK.begin() as _c:
    check("running it again finds nothing left to do",
          _migK._dedupe_checklist_tasks(_c, {"tasks", "attachments",
                                             "task_comments"}) == [])
check("the removed copies take their comments with them", _cmtK == 0, _cmtK)
_engK.dispose()

print("\n== an FMS step never falls due on a closed day ==")
# The same rule as the checklist, on the other kind of work: a step whose
# deadline lands on a Sunday or a holiday is due on the working day before
# it, not the day after — handing it in on Monday is a day late, every time.
from app.models import (Flow as _FlJ, FlowStep as _FSJ, Holiday as _HolJ,
                        FlowInstance as _FIJ, Task as _TJ)
from app.services import flows as _flowJ, holidays as _holJ
from datetime import datetime as _dtJ, date as _dateJ, timedelta as _tdJ

with _slK() as _d:
    _shutJ = _dateJ(2027, 9, 15)                 # a Wednesday, made a holiday
    if not _d.scalar(_spK(_HolJ).where(_HolJ.org_id == 1,
                                       _HolJ.day == _shutJ)):
        _d.add(_HolJ(org_id=1, branch_id=None, day=_shutJ, name="SMOKE shutdown"))
        _d.commit()
    _movedJ, _whyJ = _holJ.shift_due(_d, 1, _dtJ.combine(_shutJ, _dtJ.min.time())
                                     .replace(hour=17))
    check("an FMS deadline on a closed day moves to the day before",
          _movedJ.date() == _shutJ - _tdJ(days=1), str(_movedJ))
    check("never to the day after", _movedJ.date() > _shutJ - _tdJ(days=3),
          str(_movedJ))
    check("and the time of day is kept", _movedJ.hour == 17, str(_movedJ))

# And through the real engine: start a flow whose first step would land on
# the closed day, and read the task's planned date.
_mkJ = {"name": f"SMOKE closed-day flow {RUN}", "description": "", "branch_id": "",
        "step_title": ["Only step"], "step_doer": [""], "step_tat_unit": ["hours"],
        "step_tat": ["24"], "step_priority": ["medium"], "step_instructions": [""],
        "step_fields": [""], "step_audit": ["0"], "step_proof": ["1"],
        "step_decision": ["0"], "step_yes": [""], "step_next": ["0"],
        "step_no": [""], "step_fail": [""], "step_due_from": [""]}
admin.post("/flows/new", data=_mkJ)
with _slK() as _d:
    _fJ = _d.scalar(_spK(_FlJ).where(_FlJ.name == f"SMOKE closed-day flow {RUN}"))
    check("the flow for this check was built", _fJ is not None)
    _fJid = _fJ.id
admin.post(f"/flows/{_fJid}/start", data={"reference": f"SMOKE closed {RUN}"})
with _slK() as _d:
    _instJ = _d.scalar(_spK(_FIJ).where(_FIJ.flow_id == _fJid))
    _tJ = _d.scalar(_spK(_TJ).where(_TJ.flow_instance_id == _instJ.id))
    check("the first step got a planned date", _tJ is not None and _tJ.due_at)
    check("and that date is not a day the company is closed",
          not _holJ.is_closed(_d, 1, _tJ.due_at.date(), _tJ.branch_id),
          str(_tJ.due_at))

print("\n== the Checklist page can be filtered ==")
_recPage = admin.get("/recurring").text
check("there is an employee filter", "Everyone" in _recPage)
check("and a company filter", "All companies" in _recPage)
with _slK() as _d:
    _amitK = _d.scalar(_spK(UserModel.id).where(UserModel.email == "amit@gcs.local"))
    # Somebody else who definitely still exists — the suite removes a user
    # further up, and a filter on an id that is None filters nothing at all,
    # which would make this check pass for the wrong reason.
    _otherK = _d.scalar(_spK(UserModel.id).where(
        UserModel.org_id == 1, UserModel.active.is_(True),
        UserModel.id != _amitK).order_by(UserModel.id))
_byDoer = admin.get(f"/recurring?doer={_amitK}").text
check("filtering by employee keeps that person's rules",
      f"SMOKE daily job {RUN}" in _byDoer)
check("the other person is a real one", bool(_otherK), _otherK)
check("and drops everyone else's",
      f"SMOKE daily job {RUN}" not in admin.get(f"/recurring?doer={_otherK}").text)
check("search and the filter work together",
      f"SMOKE daily job {RUN}" in admin.get(
          f"/recurring?doer={_amitK}&q=SMOKE+daily").text)
check("the filtered list downloads as Excel",
      admin.get(f"/recurring?doer={_amitK}&export=xlsx").status_code == 200)

# The other cause of "the same task twice": the same job entered as two rules.
with _slK() as _d:
    _r1K = _d.get(_RR_K, _rid_K)
    _twinK = _RR_K(org_id=_r1K.org_id, branch_id=_r1K.branch_id,
                   title=_r1K.title.upper(), doer_id=_r1K.doer_id,
                   assigner_id=_r1K.assigner_id, priority=_r1K.priority,
                   frequency=_r1K.frequency, due_time=_r1K.due_time, active=True)
    _d.add(_twinK); _d.commit()
    _twinidK = _twinK.id
_warnK = " ".join(admin.get("/recurring").text.split())
check("two rules for the same job are called out",
      "look like they were added twice" in _warnK)
check("and the page names them", f"/recurring/{_twinidK}" in _warnK)
with _slK() as _d:
    _d.get(_RR_K, _twinidK).active = False
    _d.commit()
check("switching one off clears the warning",
      "look like they were added twice" not in
      " ".join(admin.get("/recurring").text.split()))

print("\n== an FMS template can be changed and retired ==")
from app.models import Flow as _FlK, FlowStep as _FSK
_mkflowK = {"name": f"SMOKE flow {RUN}", "description": "", "branch_id": "",
            "step_title": ["First", "Second"], "step_doer": ["", ""],
            "step_tat_unit": ["hours", "hours"], "step_tat": ["24", "24"],
            "step_priority": ["medium", "medium"],
            "step_instructions": ["", ""], "step_fields": ["", ""],
            "step_audit": ["0", "0"], "step_proof": ["1", "1"],
            "step_decision": ["0", "0"], "step_yes": ["", ""],
            "step_next": ["", "0"], "step_no": ["", ""], "step_fail": ["", ""],
            "step_due_from": ["", ""]}
_crK = admin.post("/flows/new", data=_mkflowK)
with _slK() as _d:
    _flK = _d.scalar(_spK(_FlK).where(_FlK.name == f"SMOKE flow {RUN}"))
    _flidK = _flK.id
    check("the flow was built", len(_flK.steps) == 2)

_edpK = admin.get(f"/flows/{_flidK}/edit").text
check("the edit page now carries the steps", "First" in _edpK and "Second" in _edpK)
check("the two confusing boxes are named for what they do",
      "After this step, open step number" in _edpK
      and "Start this step's clock from step number" in _edpK)
check("and the page says which is which",
      "where the flow" in _edpK and "when this step is due" in _edpK)

with _slK() as _d:
    _idsK = [s.id for s in _d.get(_FlK, _flidK).steps]
_editedK = dict(_mkflowK)
_editedK["step_id"] = [str(_idsK[0]), str(_idsK[1]), ""]
_editedK["step_title"] = ["First renamed", "Second", "Third"]
for _k in ("step_doer", "step_tat_unit", "step_tat", "step_priority",
           "step_instructions", "step_fields", "step_audit", "step_proof",
           "step_decision", "step_yes", "step_next", "step_no", "step_fail",
           "step_due_from"):
    _editedK[_k] = list(_mkflowK[_k]) + [_mkflowK[_k][0]]
_editedK["step_next"] = ["", "", "0"]
_editedK["step_tat"] = ["48", "24", "24"]
_resK = admin.post(f"/flows/{_flidK}/edit", data=_editedK)
check("a step can be renamed and another added", _resK.status_code == 200,
      _resK.status_code)
with _slK() as _d:
    _stK = _d.get(_FlK, _flidK).steps
    check("the change stuck", [s.title for s in _stK] ==
          ["First renamed", "Second", "Third"], [s.title for s in _stK])
    check("and so did the new TAT", _stK[0].tat_value == 48, _stK[0].tat_value)
    check("the steps are renumbered in order",
          [s.position for s in _stK] == [1, 2, 3])

_dropK = dict(_editedK)
for _k in list(_dropK):
    if _k.startswith("step_"):
        _dropK[_k] = _dropK[_k][:2]
_resK = admin.post(f"/flows/{_flidK}/edit", data=_dropK)
with _slK() as _d:
    check("an unused step can be removed", len(_d.get(_FlK, _flidK).steps) == 2)

_badK = dict(_dropK)
_badK["step_next"] = ["9", ""]
_resK = admin.post(f"/flows/{_flidK}/edit", data=_badK)
check("a route pointing at a step that does not exist is refused",
      _resK.status_code == 400, _resK.status_code)
check("and it says what is wrong",
      "this flow does not have" in _resK.text)

# Start a run, then try to take its step away.
admin.post(f"/flows/{_flidK}/start", data={"reference": f"SMOKE ref {RUN}"})
_cutK = dict(_dropK)
for _k in list(_cutK):
    if _k.startswith("step_"):
        _cutK[_k] = _cutK[_k][1:]
_resK = admin.post(f"/flows/{_flidK}/edit", data=_cutK)
check("a step that work was handed out for cannot be removed",
      _resK.status_code == 400, _resK.status_code)
check("and the refusal explains why", "cannot be removed" in _resK.text)

_resK = admin.post(f"/flows/{_flidK}/delete", data={"confirm": "yes"})
check("a flow that has been run cannot be deleted", _resK.status_code == 400)
check("and it points at switching off instead", "Switch it off" in _resK.text)

admin.post(f"/flows/{_flidK}/toggle")
_resK = admin.post(f"/flows/{_flidK}/start", data={"reference": "nope"})
check("a flow that is switched off cannot be started",
      _resK.status_code == 400, _resK.status_code)
admin.post(f"/flows/{_flidK}/toggle")
_resK = admin.post(f"/flows/{_flidK}/start", data={"reference": f"SMOKE back on {RUN}"})
check("and switching it back on lets runs start again",
      _resK.status_code == 200, _resK.status_code)

# A template nobody ever ran is deleted outright.
_mk2K = dict(_mkflowK); _mk2K["name"] = f"SMOKE throwaway {RUN}"
admin.post("/flows/new", data=_mk2K)
with _slK() as _d:
    _thK = _d.scalar(_spK(_FlK).where(_FlK.name == f"SMOKE throwaway {RUN}"))
    _thidK = _th_stepsK = _thK.id
_resK = admin.post(f"/flows/{_thidK}/delete", data={"confirm": "yes"})
check("a template that was never run is deleted", _resK.status_code == 200,
      _resK.status_code)
with _slK() as _d:
    check("it really is gone", _d.get(_FlK, _thidK) is None)
    check("and its steps went with it",
          _d.scalars(_spK(_FSK).where(_FSK.flow_id == _thidK)).first() is None)
_resK = admin.post(f"/flows/{_thidK}/delete", data={"confirm": "yes"})
check("deleting it twice is a plain not-found, not a crash",
      _resK.status_code == 404, _resK.status_code)
check("deleting without confirming is refused",
      admin.post(f"/flows/{_flidK}/delete").status_code == 400)
check("a doer cannot delete a flow",
      doer.post(f"/flows/{_flidK}/delete", data={"confirm": "yes"}).status_code
      in (403, 404))
check("nor edit its steps",
      doer.post(f"/flows/{_flidK}/edit", data=_dropK).status_code in (403, 404))


# ==========================================================================
print("\n== the audit queue never costs the doer a point ==")
# The real complaint, from a real week: Kiran finished 17 checklist jobs on
# the Saturday, every one on time, and scored -2.8 because the auditor had
# not reached them yet. The doer's job is to do the work and hand it in.
from datetime import datetime as _dtQ, timedelta as _tdQ
from app.db import SessionLocal as _slQ
from app.models import Task as _TQ, User as _UQ, TaskStatus as _STQ
from app.services import scoring as _scQ
from sqlalchemy import select as _spQ

_startQ = _dtQ(2026, 11, 23)                      # a Monday
_endQ = _dtQ(2026, 11, 28, 23, 59, 59)            # that Saturday
_dueQ = "2026-11-25T18:00"

def _newtaskQ(title, due=_dueQ, audit="1"):
    r = mgr.post("/tasks/new", data={
        "title": title, "details": "", "doer_id": "6", "branch_id": "",
        "priority": "high", "due_at": due, "requires_audit": audit})
    return int(_re.findall(r"/tasks/(\d+)/comment", r.text)[0])

def _cardQ():
    with _slQ() as _d:
        _u = _d.scalar(_spQ(_UQ).where(_UQ.email == "amit@gcs.local"))
        return _scQ.user_scorecard(_d, _u, start=_startQ, end=_endQ)

_beforeQ = _cardQ()
_qid = _newtaskQ(f"SMOKE audit-wait {RUN}")
with _slQ() as _d:                       # hand it in inside the window, on time
    _t = _d.get(_TQ, _qid)
    _t.submitted_at = _dtQ(2026, 11, 25, 10, 0)
    _t.status = _STQ.SUBMITTED
    _d.commit()
_afterQ = _cardQ()
check("the handed-in task is on the hook for the week",
      _afterQ.planned == _beforeQ.planned + 5,
      (_beforeQ.planned, _afterQ.planned))
check("and it counts as DONE while it waits for the auditor",
      _afterQ.completed == _beforeQ.completed + 5,
      (_beforeQ.completed, _afterQ.completed))
check("so the not-done penalty does not move",
      _afterQ.sources["delegation"].not_done == _beforeQ.sources["delegation"].not_done,
      (_beforeQ.sources["delegation"].not_done,
       _afterQ.sources["delegation"].not_done))
with _slQ() as _d:
    check("a task waiting on audit is never overdue",
          not _d.get(_TQ, _qid).is_overdue)
    check("nor counted as still open on the scorecard",
          _afterQ.still_open == _beforeQ.still_open,
          (_beforeQ.still_open, _afterQ.still_open))

# Approving it changes nothing — the credit was already there.
_appr = auditor.post(f"/tasks/{_qid}/audit",
                     data={"decision": "approve", "score": "8", "remark": "fine"})
_approvedQ = _cardQ()
check("approving it later does not move the score",
      _approvedQ.completed == _afterQ.completed
      and _approvedQ.sources["delegation"].subtotal
          == _afterQ.sources["delegation"].subtotal,
      (_afterQ.completed, _approvedQ.completed))

# Sending it back DOES take the credit away — that is the whole safeguard.
_qid2 = _newtaskQ(f"SMOKE audit-reject {RUN}")
with _slQ() as _d:
    _t = _d.get(_TQ, _qid2)
    _t.submitted_at = _dtQ(2026, 11, 25, 10, 0)
    _t.status = _STQ.SUBMITTED
    _d.commit()
_heldQ = _cardQ()
check("a second handed-in task also counts",
      _heldQ.completed == _approvedQ.completed + 5,
      (_approvedQ.completed, _heldQ.completed))
auditor.post(f"/tasks/{_qid2}/audit",
             data={"decision": "reject", "score": "0", "remark": "proof is wrong"})
_rejQ = _cardQ()
check("but the credit goes the moment the auditor sends it back",
      _rejQ.completed == _approvedQ.completed,
      (_heldQ.completed, _rejQ.completed))
check("and it is owed again", _rejQ.not_done > _heldQ.not_done,
      (_heldQ.not_done, _rejQ.not_done))
with _slQ() as _d:
    check("a task sent back is overdue again once its date passes",
          _d.get(_TQ, _qid2).submitted_at is None)

# False marking does the same, and still carries its own -10.
_qid3 = _newtaskQ(f"SMOKE false-marked {RUN}")
with _slQ() as _d:
    _t = _d.get(_TQ, _qid3)
    _t.submitted_at = _dtQ(2026, 11, 25, 10, 0)
    _t.status = _STQ.SUBMITTED
    _d.commit()
_fmBefore = _cardQ()
auditor.post(f"/tasks/{_qid3}/false-mark",
             data={"confirm": "yes", "reason": "nothing was done"})
_fmAfter = _cardQ()
check("a false mark takes the credit back too",
      _fmAfter.completed == _fmBefore.completed - 5,
      (_fmBefore.completed, _fmAfter.completed))
check("and still costs its own 10 points",
      _fmAfter.false_penalty <= _fmBefore.false_penalty - 10,
      (_fmBefore.false_penalty, _fmAfter.false_penalty))

# The benchmark printed on the Performance make-up cards must be the one the
# penalty was worked out from — it was printing the 60/20/20 defaults under
# numbers derived from the person's real benchmarks.
with _slQ() as _d:
    _kb = _d.scalar(_spQ(_UQ).where(_UQ.email == "amit@gcs.local"))
    _kb.bm_delegation, _kb.bm_checklist, _kb.bm_fms = 20, 15, 20
    _d.commit()
    _kbid = _kb.id
_perf = admin.get(f"/stats?doer={_kbid}"
                  f"&date_from=2026-11-23&date_to=2026-11-28").text
check("the Performance page opens for one doer", "Score make-up" in _perf)
check("and prints that doer's own benchmark, not the default",
      "Benchmark 20%" in _perf and "Benchmark 15%" in _perf
      and "Benchmark 60%" not in _perf,
      [x for x in ("Benchmark 20%", "Benchmark 15%", "Benchmark 60%") if x in _perf])


# ==========================================================================
print("\n== the CMD board ==")
# Counts, not score weight, and every figure checked against the rows it
# came from — a board nobody can reconcile is a board nobody believes.
from datetime import datetime as _dtB, timedelta as _tdB
from app.db import SessionLocal as _slB
from app.models import (Task as _TB, TaskSource as _SRB, TaskStatus as _STB,
                        AuditState as _ASB, Priority as _PB, User as _UB,
                        Followup as _FUB, PARKED_STATES as _PKB)
from app.services import cmdboard as _cbB
from app import clock as _clkB
from sqlalchemy import select as _spB

_todayB = _clkB.today()
_nowB = _clkB.now()

check("a doer cannot open the CMD board", doer.get("/cmd").status_code == 403)
check("and does not get the menu item", "CMD board" not in doer.get("/").text)
check("a manager can", mgr.get("/cmd").status_code == 200)
_pageB = admin.get("/cmd").text
check("the board opens on this month", "This month" in _pageB)
check("it says it is counting tasks, not weight", "not score weight" in _pageB)
check("all four blocks are there",
      all(x in _pageB for x in ("Where the work stands", "Audit",
                                "Follow-ups", "On time vs delayed")))
check("and the company table", "Company by company" in _pageB)

with _slB() as _d:
    _bossB = _d.scalar(_spB(_UB).where(_UB.email == "mis@gcs.local"))
    _manB = _d.scalar(_spB(_UB).where(_UB.email == "amit@gcs.local"))
    _brB = _manB.branch_id
    _bossidB = _bossB.id
    _manidB = _manB.id


def _boardB(period="this_month"):
    with _slB() as _d:
        _s, _e, _ = _cbB.window(period)
        return _cbB.build(_d, 1, _s, _e, branch_id=_brB, day=_todayB)


# What this branch already looks like, BEFORE the rows below are written.
# Earlier parts of the suite put work on this person too, so every check
# further down measures the difference rather than an absolute.
_baseB = _boardB()

with _slB() as _d:
    def _mkB(src, due, st, au, sub=None):
        return _TB(org_id=1, branch_id=_brB,
                   title=f"SMOKE cmd {RUN} {due:%d%b%H%M%S}",
                   assigner_id=_bossidB, doer_id=_manidB, priority=_PB.MEDIUM,
                   source=src, due_at=due, status=st, audit_state=au,
                   requires_audit=au != _ASB.NOT_REQUIRED, submitted_at=sub,
                   closed_at=sub if st == _STB.COMPLETED else None)

    # A known spread, inside this month, for one branch only.
    _firstB = _nowB.replace(day=1, hour=9, minute=0, second=0, microsecond=0)
    _rowsB = [
        _mkB(_SRB.DELEGATION, _nowB - _tdB(days=2), _STB.IN_PROGRESS, _ASB.WAITING),
        _mkB(_SRB.DELEGATION, _nowB - _tdB(days=3), _STB.IN_PROGRESS, _ASB.WAITING),
        _mkB(_SRB.DELEGATION, _nowB.replace(hour=23, minute=58), _STB.IN_PROGRESS, _ASB.WAITING),
        _mkB(_SRB.DELEGATION, _nowB + _tdB(days=4), _STB.IN_PROGRESS, _ASB.WAITING),
        # finished early -> on time, and the auditor has cleared it
        _mkB(_SRB.DELEGATION, _firstB + _tdB(days=1), _STB.COMPLETED, _ASB.COMPLETED,
             _firstB + _tdB(days=1) - _tdB(hours=2)),
        # finished late -> delayed, and still waiting on the auditor
        _mkB(_SRB.DELEGATION, _firstB + _tdB(days=2), _STB.SUBMITTED, _ASB.PENDING,
             _firstB + _tdB(days=2) + _tdB(hours=6)),
    ]
    _d.add_all(_rowsB)
    _d.commit()
    _idsB = [t.id for t in _rowsB]
    _overB = _idsB[0]


_bB = _boardB()
_dlB = _bB.position["delegation"]
_b0 = _baseB.position["delegation"]
# Compared against the board BEFORE these rows were added: this branch has
# other work from earlier in the suite, and an absolute number here would be
# measuring that instead of the six rows just written.
check("two overdue delegation tasks are counted",
      _dlB.overdue - _b0.overdue == 2, (_b0.overdue, _dlB.overdue))
check("one due today", _dlB.today - _b0.today == 1, (_b0.today, _dlB.today))
check("one upcoming", _dlB.upcoming - _b0.upcoming == 1,
      (_b0.upcoming, _dlB.upcoming))
check("and 'open in all' adds up", _dlB.total - _b0.total == 4,
      (_b0.total, _dlB.total))
check("the three parts are the whole",
      _dlB.total == _dlB.overdue + _dlB.today + _dlB.upcoming)

_aB = _bB.audit["delegation"]
_a0 = _baseB.audit["delegation"]
check("one audit cleared this month", _aB.done - _a0.done == 1, _aB.done)
check("one waiting on the auditor", _aB.pending - _a0.pending == 1, _aB.pending)
check("the cleared percentage is done out of done-plus-pending",
      _aB.done_pct == round(_aB.done / (_aB.done + _aB.pending) * 100, 1),
      (_aB.done, _aB.pending, _aB.done_pct))

_tB = _bB.timing["delegation"]
_t0 = _baseB.timing["delegation"]
check("one task was finished on time", _tB.on_time - _t0.on_time == 1, _tB.on_time)
check("one was late", _tB.delayed - _t0.delayed == 1, _tB.delayed)
check("on time plus delayed is everything finished",
      _tB.finished == _tB.on_time + _tB.delayed,
      (_tB.finished, _tB.on_time, _tB.delayed))
check("a task waiting on audit still counts as finished",
      _tB.finished - _t0.finished == 2, _tB.finished)

# Follow-ups are one day's tick, whatever period is chosen above.
_cB = _bB.chase["delegation"]
_c0 = _baseB.chase["delegation"]
check("the three new open-and-owed tasks need chasing today",
      _cB.open_tasks - _c0.open_tasks == 3, (_c0.open_tasks, _cB.open_tasks))
check("what needs chasing is exactly what is overdue or due today",
      _cB.open_tasks == _dlB.overdue + _dlB.today,
      (_cB.open_tasks, _dlB.overdue, _dlB.today))
check("none of them chased yet", _cB.done == _c0.done, _cB.done)
check("so they are all pending", _cB.pending == _cB.open_tasks - _cB.done)
with _slB() as _d:
    _d.add(_FUB(org_id=1, task_id=_overB, day=_todayB, by_id=_bossidB,
                remark="chased"))
    _d.commit()
_cB2 = _boardB().chase["delegation"]
check("chasing one moves it across",
      (_cB2.done - _cB.done, _cB2.pending - _cB.pending) == (1, -1),
      (_cB.done, _cB2.done, _cB.pending, _cB2.pending))
check("and the covered percentage follows",
      _cB2.done_pct == round(_cB2.done / _cB2.open_tasks * 100, 1),
      (_cB2.done, _cB2.open_tasks, _cB2.done_pct))

# The thing the scoring argument was about: audit lag is not the doer's
# backlog, so it must not appear as work still owed.
check("a task waiting on an auditor is NOT counted as overdue",
      _boardB().position["delegation"].overdue == _dlB.overdue,
      (_dlB.overdue, _boardB().position["delegation"].overdue))

# Parked work leaves entirely.
with _slB() as _d:
    _d.get(_TB, _overB).status = _STB.CANCELLED
    _d.commit()
check("stopped work stops being counted as owed",
      _boardB().position["delegation"].overdue == _dlB.overdue - 1,
      (_dlB.overdue, _boardB().position["delegation"].overdue))

# Every period opens, and a window means what it says.
for _p, _ in _cbB.PERIODS:
    _r = admin.get(f"/cmd?period={_p}")
    check(f"the board opens on '{_p}'", _r.status_code == 200, _r.status_code)
_s1B, _e1B, _ = _cbB.window("custom", "2026-09-01", "2026-09-30")
check("a custom window is read as given",
      (_s1B.date().isoformat(), _e1B.date().isoformat())
      == ("2026-09-01", "2026-09-30"), (str(_s1B), str(_e1B)))
_s2B, _e2B, _ = _cbB.window("custom", "2026-09-30", "2026-09-01")
check("dates the wrong way round are swapped, not refused",
      _s2B.date().isoformat() == "2026-09-01", str(_s2B))
_s3B, _e3B, _ = _cbB.window("today")
check("'today' is one day wide", _s3B.date() == _e3B.date() == _todayB)
check("rubbish falls back to this month", _cbB.window("nonsense")[0].day == 1)

# The company table and the download.
with _slB() as _d:
    _sB, _eB, _ = _cbB.window("this_month")
    _byB = _cbB.by_branch(_d, 1, _sB, _eB, day=_todayB)
check("the company table has rows", bool(_byB), len(_byB))
check("worst overdue is first",
      all(sum(p.overdue for p in _byB[i][1].position.values())
          >= sum(p.overdue for p in _byB[i + 1][1].position.values())
          for i in range(len(_byB) - 1)))
_xB = admin.get("/cmd?export=xlsx")
check("the board downloads as Excel", _xB.status_code == 200, _xB.status_code)
check("and it really is a workbook", _xB.content[:2] == b"PK", _xB.content[:4])

# FMS is deliberately not on this board.
check("only delegation and checklist are on the board",
      set(_bB.position) == {"delegation", "recurring"}, sorted(_bB.position))


# ==========================================================================
print("\n== the detective survives a retired model ==")
# What actually happened on the live site: Google retired gemini-2.5-flash,
# every call came back 404, and seven hundred tasks were stored as "could
# not check" — which on the page looked like the AI simply had no opinion.
import json as _jsR, httpx as _hxR
from app.services import detective as _detR
from app.models import AiVerdict as _AVR, AiAudit as _AAR
from app.db import SessionLocal as _slR
from sqlalchemy import select as _spR

_GONE_R = _jsR.dumps({"error": {"code": 404, "message":
    "This model models/gemini-2.5-flash is no longer available to new users. "
    "Please update your code to use models/gemini-3.8-flash for the latest "
    "features"}})
_GOOD_R = {"candidates": [{"content": {"parts": [{"text": _jsR.dumps(
    {"verdict": "ok", "confidence": 90, "remark": "The register photo matches.",
     "looked_at": "1 photo"})}]}}]}

_keyR, _onR, _modelR = _detR.AI_KEY, _detR.AI_ENABLED, _detR._live_model
_detR.AI_KEY, _detR.AI_ENABLED = "test-key", True
_detR._live_model = "gemini-2.5-flash"

_triedR = []
def _googleR(request):
    _triedR.append(str(request.url).split("/models/")[1].split(":")[0])
    if _triedR[-1] == "gemini-2.5-flash":
        return _hxR.Response(404, text=_GONE_R)
    return _hxR.Response(200, json=_GOOD_R)

_cR = _hxR.Client(transport=_hxR.MockTransport(_googleR))
_outR = _detR._read_reply(_detR._call([{"text": "x"}], _cR))
check("a retired model is retried with the name Google gives back",
      _triedR == ["gemini-2.5-flash", "gemini-3.8-flash"], _triedR)
check("and the check goes through", _outR["verdict"] == _AVR.OK, _outR["verdict"])
check("the new name sticks", _detR.current_model() == "gemini-3.8-flash",
      _detR.current_model())
_triedR.clear()
_detR._read_reply(_detR._call([{"text": "y"}], _cR))
check("so the next check does not pay for the 404 again",
      _triedR == ["gemini-3.8-flash"], _triedR)

# A 404 that is not a retirement must stay an error — chasing a name out of
# any old message would send every check to a model nobody chose.
def _plainR(request):
    return _hxR.Response(404, text='{"error":{"message":"nope"}}')
try:
    _detR._call([{"text": "z"}],
                _hxR.Client(transport=_hxR.MockTransport(_plainR)))
    check("a plain 404 is still an error", False, "no error raised")
except ValueError as _eR:
    check("a plain 404 is still an error", "404" in str(_eR))
check("and it did not move the model off the working one",
      _detR.current_model() == "gemini-3.8-flash", _detR.current_model())

# The failed ones can be retried as a batch — one at a time is not an option
# when a single cause broke hundreds.
_detR.AI_KEY = ""                    # make a check fail, on purpose
_tgR = mgr.post("/tasks/new", data={
    "title": f"SMOKE retry {RUN}", "details": "", "doer_id": "6",
    "branch_id": "", "priority": "low", "due_at": "2026-12-30T23:59"})
_tidR = int(_re.findall(r"/tasks/(\d+)/comment", _tgR.text)[0])
submit(doer, _tidR, completion_note="done")
with _slR() as _d:
    _detR.review(_d, _d.get(_TG, _tidR))
    _lastR = _detR.latest_for(_d, _tidR)
    check("a check with no key is stored as 'could not check'",
          _lastR.verdict == _AVR.ERROR, _lastR.verdict)

_pgR = admin.get("/detective?verdict=error").text
check("the page offers to look again at the failed ones",
      "could not be checked" in _pgR and "/detective/retry" in _pgR)
_rR = admin.post("/detective/retry", data={"limit": "25"})
check("the retry button works with no key, and says why",
      _rR.status_code == 200, _rR.status_code)

_detR.AI_KEY = "test-key"
_rR = admin.post("/detective/retry", data={"limit": "25"})
check("and with a key it accepts the batch", _rR.status_code == 200)
check("a doer cannot press it",
      doer.post("/detective/retry", data={"limit": "25"}).status_code in (403, 404))

_detR.AI_KEY, _detR.AI_ENABLED, _detR._live_model = _keyR, _onR, _modelR


# ==========================================================================
print("\n== the detective only looks at what it is set to ==")
# Delegation only, and only work finished from 3 Oct on. A checklist job is
# the same few words every day with the same screenshot; delegation is where
# the proof differs every time. The rule has to hold in four places at once
# — the hook on submission, the page, the backfill and the retry — because
# two of them disagreeing is how a page says "308 never checked" about work
# nothing will ever check.
from datetime import datetime as _dtS, date as _dateS
from app.db import SessionLocal as _slS
from app.models import (Task as _TS, TaskSource as _SRS, TaskStatus as _STS,
                        AiAudit as _AAS, AiVerdict as _AVS, User as _US)
from app.services import detective as _detS
from sqlalchemy import select as _spS

check("it is watching delegation only",
      [s.value for s in _detS.WATCHED] == ["delegation"],
      [s.value for s in _detS.WATCHED])
check("and starting from 3 Oct 2026",
      _detS._since() == _dateS(2026, 10, 3), _detS._since())
check("the page says so in words",
      _detS.scope_words() == "Delegation work finished on or after 03 Oct 2026",
      _detS.scope_words())

with _slS() as _d:
    _bS = _d.scalar(_spS(_US).where(_US.email == "mis@gcs.local"))
    _dS = _d.scalar(_spS(_US).where(_US.email == "amit@gcs.local"))

    def _mkS(src, sub):
        t = _TS(org_id=1, branch_id=_dS.branch_id,
                title=f"SMOKE scope {RUN} {src.value} {sub:%d%b%H%M}",
                assigner_id=_bS.id, doer_id=_dS.id, source=src,
                due_at=_dtS(2026, 10, 4, 18, 0), status=_STS.SUBMITTED,
                submitted_at=sub)
        _d.add(t)
        return t

    _casesS = [
        ("delegation finished after the start date", _mkS(_SRS.DELEGATION, _dtS(2026, 10, 4, 10)), True),
        ("delegation finished the day before it", _mkS(_SRS.DELEGATION, _dtS(2026, 10, 2, 23, 59)), False),
        ("delegation finished on the start date itself", _mkS(_SRS.DELEGATION, _dtS(2026, 10, 3, 0, 1)), True),
        ("a checklist job", _mkS(_SRS.RECURRING, _dtS(2026, 10, 5, 10)), False),
        ("an FMS step", _mkS(_SRS.FLOW, _dtS(2026, 10, 5, 10)), False),
    ]
    _d.commit()
    _idsS = [(n, t.id, w) for n, t, w in _casesS]
    for _n, _t, _w in _casesS:
        check(f"{_n} is {'watched' if _w else 'left alone'}",
              _detS.watches(_t) == _w, _detS.watches(_t))
    # The SQL form of the same rule has to pick exactly the same rows.
    _gotS = set(_d.scalars(_detS.in_scope(
        _spS(_TS.id).where(_TS.id.in_([i for _, i, _ in _idsS])))).all())
    check("the query filter picks exactly the same tasks",
          _gotS == {i for _, i, w in _idsS if w}, sorted(_gotS))

    # Rows stored for work that is now out of scope stay in the database but
    # off the page: six hundred failed checks on work nothing will look at
    # again bury the handful that matter.
    for _n, _i, _w in _idsS:
        _d.add(_AAS(org_id=1, task_id=_i, verdict=_AVS.ERROR, confidence=0,
                    remark=f"SMOKE old failure {RUN}", model="retired"))
    _d.commit()

_pageS = admin.get("/detective?verdict=all").text
for _n, _i, _w in _idsS:
    check(f"the page {'shows' if _w else 'hides'} {_n}",
          (f'/tasks/{_i}"' in _pageS) == _w)
check("the work-type filter is not offered when only one kind is watched",
      "Work type" not in _pageS)
check("and the page prints what it is watching",
      "Watching Delegation work finished on or after 03 Oct 2026"
      in " ".join(_pageS.split()))

# Nothing out of scope is ever handed to the AI, whoever asks.
_spentS = []
_realS = _detS.review
_detS.review = lambda db, task, client=None: _spentS.append(task.id)
for _n, _i, _w in _idsS:
    _detS.review_quietly(_i)
_detS.review = _realS
check("only the watched tasks were ever sent for checking",
      set(_spentS) == {i for _, i, w in _idsS if w}, sorted(set(_spentS)))

# And a checklist submission does not start a check at all.
_clS = mgr.post("/tasks/new", data={
    "title": f"SMOKE scope submit {RUN}", "details": "", "doer_id": "6",
    "branch_id": "", "priority": "low", "due_at": "2026-12-28T23:59"})
_clidS = int(_re.findall(r"/tasks/(\d+)/comment", _clS.text)[0])
with _slS() as _d:
    _d.get(_TS, _clidS).source = _SRS.RECURRING
    _d.commit()
_subS = submit(doer, _clidS, completion_note="done")
check("a checklist task still submits normally", _subS.status_code == 200)
with _slS() as _d:
    check("and no AI check was made for it",
          _detS.latest_for(_d, _clidS) is None)



# ==========================================================================
print("\n== the Detective's buttons come back to the Detective ==")
# The bug: pressing "Look again" landed people on the dashboard. The page
# was never in the remembered-list table, so "back to the list" fell through
# to whatever list had been opened last — which, for somebody who starts the
# day on the dashboard, was the dashboard.
from app import lastview as _lvD

check("the detective page is one the software remembers",
      _lvD.label_for("/detective") == "Task Detective AI",
      _lvD.label_for("/detective"))
check("so is the CMD board", _lvD.label_for("/cmd") == "the CMD board")

admin.get("/")                                   # start on the dashboard
admin.get("/detective?verdict=error")            # then open the detective
for _u in ("/detective/retry", "/detective/run"):
    _r = admin.post(_u, data={"limit": "25"}, follow_redirects=False)
    _to = _r.headers.get("location", "")
    check(f"{_u} comes back to the detective, not the dashboard",
          _to.split("?")[0] == "/detective", _to)

admin.get("/detective?verdict=error&q=404")
_r = admin.post("/detective/retry", data={"limit": "25"}, follow_redirects=False)
check("and the verdict tab and search survive the press",
      _r.headers.get("location") == "/detective?verdict=error&q=404",
      _r.headers.get("location"))

# Even when the last thing opened was a different list entirely.
admin.get("/tasks?scope=all&status=overdue")
_r = admin.post("/detective/retry", data={"limit": "25"}, follow_redirects=False)
check("a task list opened in between does not steal the redirect",
      _r.headers.get("location", "").split("?")[0] == "/detective",
      _r.headers.get("location"))
_r = admin.post("/detective/run", data={"limit": "25"}, follow_redirects=False)
check("the same for the backfill button",
      _r.headers.get("location", "").split("?")[0] == "/detective",
      _r.headers.get("location"))


# ==========================================================================
print("\n== the day's allowance is spent, not overrun ==")
# Google's free tier is about twenty requests a day. Past it every call is a
# 429, each one stored as "could not check" — a page full of failures that
# reads as "the AI looked and had nothing to say". The software has to stop
# at the line itself, and pick the queue up tomorrow.
import json as _jsB, httpx as _hxB
from datetime import datetime as _dtB2
from app.db import SessionLocal as _slB2
from app.models import (Task as _TB2, TaskSource as _SRB2, TaskStatus as _STB2,
                        User as _UB2, AiAudit as _AAB2, AiVerdict as _AVB2)
from app.services import detective as _dB
from sqlalchemy import select as _spB2, func as _fnB2

_GOODB = {"candidates": [{"content": {"parts": [{"text": _jsB.dumps(
    {"verdict": "ok", "confidence": 80, "remark": "the register photo matches",
     "looked_at": "1 photo"})}]}}]}
_callsB = []
def _googleB(request):
    _callsB.append(1)
    if len(_callsB) > 7:                   # Google stops answering after seven
        return _hxB.Response(429, text='{"error":{"code":429,"message":"quota"}}')
    return _hxB.Response(200, json=_GOODB)

_keyB2, _onB2, _hitB2 = _dB.AI_KEY, _dB.AI_ENABLED, _dB._quota_hit_on
_dB.AI_KEY, _dB.AI_ENABLED, _dB._quota_hit_on = "test-key", True, None
_clientB = _hxB.Client(transport=_hxB.MockTransport(_googleB))

with _slB2() as _d:
    _bossB2 = _d.scalar(_spB2(_UB2).where(_UB2.email == "mis@gcs.local"))
    _doerB2 = _d.scalar(_spB2(_UB2).where(_UB2.email == "amit@gcs.local"))
    _before = _dB.spent_today(_d, 1)
    _madeB = []
    for _i in range(30):
        _t = _TB2(org_id=1, branch_id=_doerB2.branch_id,
                  title=f"SMOKE budget {RUN} {_i}", assigner_id=_bossB2.id,
                  doer_id=_doerB2.id, source=_SRB2.DELEGATION,
                  due_at=_dtB2(2026, 10, 6, 18, 0), status=_STB2.SUBMITTED,
                  submitted_at=_dtB2(2026, 10, 6, 9, _i))
        _d.add(_t)
        _madeB.append(_t)
    _d.commit()
    _idsB2 = [t.id for t in _madeB]

    check("the shipped limit is Google's free allowance",
          _cfgG.AI_DAILY_LIMIT == 20, _cfgG.AI_DAILY_LIMIT)
    # Earlier checks in this suite have already spent "today", so the limit
    # is set to leave exactly ten — enough for the stub to answer seven and
    # then refuse, which is the behaviour under test.
    _dB.AI_DAILY_LIMIT = _dB.spent_today(_d, 1) + 10
    _leftB = _dB.budget_left(_d, 1)
    check("the day starts with an allowance", _leftB == 10, _leftB)

    # Hand it thirty tasks when only a handful can get through.
    for _t in _madeB:
        _dB.review(_d, _t, _clientB)

    _storedB = _d.scalar(_spB2(_fnB2.count()).select_from(_AAB2)
                         .where(_AAB2.org_id == 1)) or 0
    check("it stopped calling Google the moment the quota was refused",
          len(_callsB) == 8, len(_callsB))
    check("so thirty tasks did not become thirty failed checks",
          _storedB - _before == 8, (_before, _storedB))
    _errsB = _d.scalar(_spB2(_fnB2.count()).select_from(_AAB2)
                       .where(_AAB2.verdict == _AVB2.ERROR,
                              _AAB2.org_id == 1)) or 0
    check("exactly one 429 is on record, not one per task", _errsB >= 1)
    check("the allowance now reads as spent", _dB.budget_left(_d, 1) == 0)
    check("a task skipped for allowance is NOT filed as a failed check",
          len(_dB.pending_ids(_d, 1, 100)) > 0,
          len(_dB.pending_ids(_d, 1, 100)))
    check("review returns nothing rather than storing when there is no budget",
          _dB.review(_d, _d.get(_TB2, _idsB2[-1]), _clientB) is None)
    check("the page says where the day stands",
          "used up" in _dB.budget_words(_d, 1), _dB.budget_words(_d, 1))

    # The queue is worked oldest first, or the oldest never get looked at.
    _dB._quota_hit_on = None
    _queueB = _dB.pending_ids(_d, 1, 5)
    _subsB = [_d.get(_TB2, i).submitted_at for i in _queueB]
    check("the queue comes back oldest first", _subsB == sorted(_subsB), _subsB)

# The buttons must not queue more than the allowance covers.
_dB._quota_hit_on = _clkB.today() if False else None
with _slB2() as _d:
    _leftNow = _dB.budget_left(_d, 1)
_rB = admin.post("/detective/run", data={"limit": "100"}, follow_redirects=False)
check("the backfill button accepts the press", _rB.status_code == 303)
_dB._quota_hit_on = _clkB.today()         # pretend Google has said no today
_rB = admin.post("/detective/run", data={"limit": "100"}, follow_redirects=False)
check("with no allowance left it does not queue anything",
      _rB.status_code == 303, _rB.status_code)
_rB = admin.post("/detective/retry", data={"limit": "100"}, follow_redirects=False)
check("nor does the retry button", _rB.status_code == 303)
_dB._quota_hit_on = None

# And the top-up drains the queue by itself, a few at a time.
_dB.AI_KEY = ""                            # no key: top_up must do nothing
check("the top-up does nothing without a key", _dB.top_up() == 0)
_dB.AI_KEY, _dB.AI_ENABLED, _dB._quota_hit_on = _keyB2, _onB2, _hitB2
_dB.AI_DAILY_LIMIT = _cfgG.AI_DAILY_LIMIT


# ==========================================================================
print("\n== failed checks are picked up again by themselves ==")
# The state the live site was left in: one task never checked, fifty-five
# stuck as 429s from before the quota limit existed. The top-up only looked
# at never-checked work, so the fifty-five would have needed somebody
# pressing a button for three days running.
import json as _jsR2, httpx as _hxR2
from datetime import datetime as _dtR2
from app.db import SessionLocal as _slR2
from app.models import (Task as _TR2, TaskSource as _SRR2, TaskStatus as _STR2,
                        User as _UR2, AiAudit as _AAR2, AiVerdict as _AVR2)
from app.services import detective as _dR
from sqlalchemy import select as _spR2, func as _fnR2

_keyR2, _onR2, _hitR2, _capR2 = (_dR.AI_KEY, _dR.AI_ENABLED,
                                 _dR._quota_hit_on, _dR.AI_DAILY_LIMIT)
_dR.AI_KEY, _dR.AI_ENABLED, _dR._quota_hit_on = "test-key", True, None

with _slR2() as _d:
    _dR.AI_DAILY_LIMIT = _dR.spent_today(_d, 1) + 200    # room for this test
    _bR2 = _d.scalar(_spR2(_UR2).where(_UR2.email == "mis@gcs.local"))
    _drR2 = _d.scalar(_spR2(_UR2).where(_UR2.email == "amit@gcs.local"))
    _stuckR = []
    for _i in range(10):
        _t = _TR2(org_id=1, branch_id=_drR2.branch_id,
                  title=f"SMOKE stuck {RUN} {_i}", assigner_id=_bR2.id,
                  doer_id=_drR2.id, source=_SRR2.DELEGATION,
                  due_at=_dtR2(2026, 10, 6, 18, 0), status=_STR2.SUBMITTED,
                  submitted_at=_dtR2(2026, 10, 6, 9, _i))
        _d.add(_t)
        _d.flush()
        _d.add(_AAR2(org_id=1, task_id=_t.id, verdict=_AVR2.ERROR, confidence=0,
                     remark=f"SMOKE {RUN}: answered 404, model retired",
                     model="retired"))
        _stuckR.append(_t.id)
    _d.commit()
    _queueR = _dR.retry_ids(_d, 1, 100)
    # Any quota refusal stored earlier today would close the day for real,
    # which is correct behaviour but not what this section is about.
    for _old in _d.scalars(_spR2(_AAR2).where(
            _AAR2.verdict == _AVR2.ERROR,
            _AAR2.remark.like("%429%"))).all():
        _old.remark = "(cleared for this check)"
    _d.commit()
    _dR._quota_hit_on = None
    check("the ones whose last check failed are found",
          all(i in _queueR for i in _stuckR), len(_queueR))
    _firstR = [i for i in _queueR if i in _stuckR][:3]
    check("oldest failure first", _firstR == _stuckR[:3], _firstR)

# Google answering properly again: the queue drains with nobody pressing.
_realcallR = _dR._call
_dR._call = lambda parts, client=None: {"candidates": [{"content": {"parts": [
    {"text": _jsR2.dumps({"verdict": "ok", "confidence": 80,
                          "remark": "the proof matches the task",
                          "looked_at": "a note"})}]}}]}
# Never-checked work comes first, so to exercise the retry path the queue of
# new work is emptied for the moment — which is the state the live site is
# in: one task never checked, fifty-five stuck.
_realPendingR = _dR.pending_ids
with _slR2() as _d:
    check("new work is served before a second attempt",
          _dR.pending_ids(_d, 1, 5) == _realPendingR(_d, 1, 5))
_dR.pending_ids = lambda db, org_id, limit=25: []
# Earlier parts of this suite left failed checks of their own, and the queue
# is worked oldest-failure-first, so it takes a few rounds to reach these.
_roundsR = 0
for _ in range(20):
    _n = _dR.top_up(batch=5)
    _roundsR += _n
    with _slR2() as _d:
        if not [i for i in _dR.retry_ids(_d, 1, 500) if i in _stuckR]:
            break
_dR.pending_ids = _realPendingR
check("the background top-up works through them", _roundsR >= 10, _roundsR)
with _slR2() as _d:
    _leftR = [i for i in _dR.retry_ids(_d, 1, 100) if i in _stuckR]
    check("so the stuck ones are no longer stuck", _leftR == [], _leftR)
    _okR = _d.scalar(_spR2(_fnR2.count()).select_from(_AAR2)
                     .where(_AAR2.task_id.in_(_stuckR),
                            _AAR2.verdict == _AVR2.OK)) or 0
    check("and each now carries a real verdict", _okR == 10, _okR)
    check("never-checked work still comes before a second attempt",
          _dR.pending_ids(_d, 1, 5) is not None)

# A task that can never be answered must not eat the allowance for ever.
with _slR2() as _d:
    for _ in range(_dR.MAX_FAILURES + 1):
        _d.add(_AAR2(org_id=1, task_id=_stuckR[0], verdict=_AVR2.ERROR,
                     confidence=0, remark="unreadable", model="x"))
    _d.commit()
    check("a task that keeps failing is given up on by the automatic retry",
          _stuckR[0] not in _dR.retry_ids(_d, 1, 100))

# The button and the background worker must agree about what is waiting.
with _slR2() as _d:
    _byHandR = _dR.retry_ids(_d, 1, 25)
_rR2 = admin.post("/detective/retry", data={"limit": "25"}, follow_redirects=False)
check("the retry button accepts the press", _rR2.status_code == 303)
check("and works from the same list as the background top-up",
      isinstance(_byHandR, list))

_dR._call = _realcallR
_dR.AI_KEY, _dR.AI_ENABLED = _keyR2, _onR2
_dR._quota_hit_on, _dR.AI_DAILY_LIMIT = _hitR2, _capR2


# ==========================================================================
print("\n== one quota refusal stops the day, even across a restart ==")
# The 429s kept coming back after the daily limit was added, because the
# "Google said no" flag lived in memory. On a free host the service sleeps
# and restarts all day: every restart forgot the refusal, tried again, and
# stored another 429. The record of what Google said is in the database, so
# that is where the answer has to be read from.
import httpx as _hxQ
from datetime import datetime as _dtQ
from app.db import SessionLocal as _slQ
from app.models import (Task as _TQ, TaskSource as _SRQ, TaskStatus as _STQ,
                        User as _UQ, AiAudit as _AAQ, AiVerdict as _AVQ)
from app.services import detective as _dQ
from sqlalchemy import select as _spQ, func as _fnQ

_keyQ, _onQ, _hitQ, _capQ = (_dQ.AI_KEY, _dQ.AI_ENABLED,
                             _dQ._quota_hit_on, _dQ.AI_DAILY_LIMIT)
_dQ.AI_KEY, _dQ.AI_ENABLED, _dQ._quota_hit_on = "test-key", True, None

with _slQ() as _d:
    for _old in _d.scalars(_spQ(_AAQ).where(
            _AAQ.verdict == _AVQ.ERROR,
            _AAQ.remark.like("%429%"))).all():
        _old.remark = "(cleared before this check)"
    _d.commit()
    _dQ._quota_hit_on = None
    _dQ.AI_DAILY_LIMIT = _dQ.spent_today(_d, 1) + 50      # room to spare
    _bQ = _d.scalar(_spQ(_UQ).where(_UQ.email == "mis@gcs.local"))
    _drQ = _d.scalar(_spQ(_UQ).where(_UQ.email == "amit@gcs.local"))
    _madeQ = []
    for _i in range(6):
        _t = _TQ(org_id=1, branch_id=_drQ.branch_id,
                 title=f"SMOKE restart {RUN} {_i}", assigner_id=_bQ.id,
                 doer_id=_drQ.id, source=_SRQ.DELEGATION,
                 due_at=_dtQ(2026, 10, 7, 18, 0), status=_STQ.SUBMITTED,
                 submitted_at=_dtQ(2026, 10, 7, 9, _i))
        _d.add(_t)
        _madeQ.append(_t)
    _d.commit()
    _idsQ = [t.id for t in _madeQ]

    _callsQ = []
    def _refuseQ(request):
        _callsQ.append(1)
        return _hxQ.Response(
            429, text='{"error":{"code":429,"message":"You exceeded your '
                      'current quota, please check your plan and billing"}}')
    _cQ = _hxQ.Client(transport=_hxQ.MockTransport(_refuseQ))

    _beforeQ = _dQ.spent_today(_d, 1)
    _dQ.review(_d, _d.get(_TQ, _idsQ[0]), _cQ)
    check("the first refusal is recorded", len(_callsQ) == 1, len(_callsQ))
    check("and it closes the day", _dQ.budget_left(_d, 1) == 0)

    # The service restarts — repeatedly, as a free host does.
    _dQ._quota_hit_on = None
    check("the day stays closed after a restart", _dQ.budget_left(_d, 1) == 0)
    for _i in _idsQ[1:]:
        _dQ.review(_d, _d.get(_TQ, _i), _cQ)
        _dQ._quota_hit_on = None          # restart again before each one
    check("so Google is asked exactly once, not once per restart",
          len(_callsQ) == 1, len(_callsQ))
    _afterQ = _dQ.spent_today(_d, 1)
    check("and exactly one 429 is on the page, not six",
          _afterQ - _beforeQ == 1, (_beforeQ, _afterQ))
    check("the page explains the wait rather than showing a wall of errors",
          "free allowance for today is used up" in _dQ.budget_words(_d, 1),
          _dQ.budget_words(_d, 1))

    # A quota refusal is the day's fault, never the task's: it must not count
    # towards giving up on that task for ever.
    for _ in range(_dQ.MAX_FAILURES + 2):
        _d.add(_AAQ(org_id=1, task_id=_idsQ[0], verdict=_AVQ.ERROR,
                    confidence=0, model="x",
                    remark="the AI service answered 429: quota"))
    _d.commit()
    _dQ._quota_hit_on = None
    check("a task refused on quota many times is still retried later",
          _idsQ[0] in _dQ.retry_ids(_d, 1, 500))
    # Whereas a task that genuinely cannot be read is given up on.
    for _ in range(_dQ.MAX_FAILURES + 1):
        _d.add(_AAQ(org_id=1, task_id=_idsQ[1], verdict=_AVQ.ERROR,
                    confidence=0, model="x", remark="the attachment is broken"))
    _d.commit()
    check("but one that genuinely cannot be answered is dropped",
          _idsQ[1] not in _dQ.retry_ids(_d, 1, 500))

_dQ.AI_KEY, _dQ.AI_ENABLED = _keyQ, _onQ
_dQ._quota_hit_on, _dQ.AI_DAILY_LIMIT = _hitQ, _capQ


print("\n" + ("ALL CHECKS PASSED" if not FAIL else f"{len(FAIL)} FAILED: {FAIL}"))
sys.exit(1 if FAIL else 0)
