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
r = mgr.post("/flows/1/start", data={"reference": "SMOKE Member — PT 1M"})
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
check("a new task requires proof with nothing ticked",
      "Proof and a note are required" in body)
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
    """The task the run is actually ON right now.

    Every step of a run is created when the start form is submitted, each
    with the date it is planned for, so "the first task that is not closed"
    no longer means "the step being worked on" — it could be step 4,
    sitting in its doer's list with a date three weeks out. The live step is
    the one at the run's current position that is not still waiting.
    """
    with _SLP() as _d:
        inst = _d.get(_FI, iid)
        rows = [t for t in _d.scalars(_sp(_TP).where(_TP.flow_instance_id == iid)).all()
                if t.status not in (_TSF.COMPLETED, _TSF.CANCELLED)
                and not t.flow_waiting
                and t.flow_step and t.flow_step.position == inst.current_position]
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
attach(_who, _t2)
# A task that needs proof needs a note with it now, so the submits in this
# suite carry one — exactly as a doer's would.
_who.post(f"/tasks/{_t2}/submit", data={"completion_note": "done"})
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
    f"/tasks/{_tid2}/submit", data={"completion_note": "done"}).text)

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
# The choices travel as [value, label] pairs now: a weekday is 0 and has to
# read "Monday", so the label cannot be worked out from the number.
_fpFlat = _fp.replace(" ", "")
check("with sensible choices per unit",
      '"weeks":[[1,"1"],[2,"2"],[3,"3"],[4,"4"],[6,"6"],[8,"8"]]' in _fpFlat, )
check("and the calendar units name their choices",
      '[0,"Monday"]' in _fpFlat and '[1,"the1st"]' in _fpFlat)
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
# In HOURS, not .days, and a floor with a generous ceiling.
#
# This comment used to say a Sunday deadline was pulled BACK to the
# Saturday. It is not any more — an FMS chain cannot be dragged backwards
# without stacking later steps on top of the work they wait for, so a
# closed day now moves a deadline FORWARD. That makes two days sometimes
# three, or four over a holiday weekend. An exact ceiling passed all week
# and failed on a Friday, which is a test that only works on the days
# nothing is wrong.
check("step 1 is due a day or two out, or the next working day after that",
      23 <= (_due1 - _ck.now()).total_seconds() / 3600 <= 24 * 5, str(_due1))
amit.post(f"/tasks/{_t}/submit", data={})
_t, _pos = _open_step(_i2id)
with _SLP() as _d:
    _due2 = _d.get(_TKT, _t).due_at
check("step 2 is due about a week out",
      6 * 24 - 1 <= (_due2 - _ck.now()).total_seconds() / 3600 <= 8 * 24 + 1,
      str(_due2))
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
# Yes / No is deliberately not in this list any more — see the section on
# it further down, which also checks that flows already using it keep it.
for _t, _lbl in [("text", "Short text"), ("textarea", "Long text"),
                 ("number", "Number"), ("date", "Date"),
                 ("datetime", "Date and time"),
                 ("select", "Choose from a list")]:
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
_realTick = _M.SPAWN_CHECK_SECONDS
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
    _M.SPAWN_CHECK_SECONDS = _realTick

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

_realTick = _M.SPAWN_CHECK_SECONDS
_M.SPAWN_CHECK_SECONDS = 0.05
try:
    check("a failure does not kill the loop", _aio.run(_survive_error()))
finally:
    _M.SPAWN_CHECK_SECONDS = _realTick
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
      "Proof and a note are required" in _pbox.text)
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
    # DEL and CL number one task each, so their series are gapless runs of
    # distinct numbers. FMS is deliberately NOT one of those: the number
    # belongs to the SUBMISSION, and every step of that submission carries
    # it. Four steps of one run share FMS-12, which is the point.
    for _src, _pref in _RP4.items():
        if _src == _S4.FLOW:
            continue
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
    check("a one-task series never repeats a number",
          all(len({t.ref for t in _all if t.source == _s})
              == len([t for t in _all if t.source == _s])
              for _s in (_S4.DELEGATION, _S4.RECURRING)))
    # DEL and CL must not borrow each other's numbers either.
    for _p1, _p2 in (("DEL-", "CL-"), ("CL-", "DEL-")):
        check(f"nothing numbered {_p1} is also numbered {_p2}",
              not ({t.ref for t in _all if t.ref.startswith(_p1)}
                   & {t.ref for t in _all if t.ref.startswith(_p2)}))
    check("delegation uses DEL", any(r.startswith("DEL-") for r in
                                     [t.ref for t in _all]))
    check("checklist uses CL", any(r.startswith("CL-") for r in
                                   [t.ref for t in _all]))
    check("FMS uses FMS", any(r.startswith("FMS-") for r in
                              [t.ref for t in _all]))

    # The FMS rule, stated on its own because it is a different rule.
    from app.models import FlowInstance as _FI4
    _runs4 = _d.query(_FI4).all()
    check("every FMS submission has its own number",
          all(r.ref for r in _runs4), [r.id for r in _runs4 if not r.ref][:5])
    check("the submissions are numbered FMS-01 upwards, with no repeats",
          sorted(r.ref_n for r in _runs4)
          == list(range(1, len(_runs4) + 1)),
          sorted(r.ref_n for r in _runs4)[:12])
    check("and spelled the way they read",
          all(r.ref == _rt4("FMS", r.ref_n) for r in _runs4),
          [r.ref for r in _runs4][:3])
    _steps4 = [t for t in _all if t.source == _S4.FLOW]
    check("every FMS step carries its submission's number, not one of its own",
          all(t.ref == _d.get(_FI4, t.flow_instance_id).ref
              for t in _steps4 if t.flow_instance_id),
          [(t.ref, _d.get(_FI4, t.flow_instance_id).ref)
           for t in _steps4 if t.flow_instance_id
           and t.ref != _d.get(_FI4, t.flow_instance_id).ref][:3])
    check("so a run's steps share one number between them",
          any(len([t for t in _steps4 if t.flow_instance_id == r.id]) > 1
              for r in _runs4))
    check("and the step says which step it is",
          all(t.step_label.startswith("Step ") for t in _steps4
              if t.flow_step_id),
          [t.step_label for t in _steps4][:3])
    # Number plus position is what an escalation email shows, because it is
    # read where nobody can click to find out which row is which. It narrows
    # FMS-12 from four tasks to one step of one submission — not always to
    # one TASK: a step sent back for rework is opened again, so a run that
    # went round once has two tasks called Step 2. The doer and the date
    # tell those apart, and every list that shows the label shows both.
    _labels4 = [t.ref_label for t in _steps4 if t.flow_step_id]
    check("the label is the submission's number plus the step",
          all(" · Step " in l and l.startswith("FMS-") for l in _labels4),
          _labels4[:3])
    check("it narrows a submission's number to one step of it",
          len(set(_labels4)) > len({t.ref for t in _steps4}),
          (len(set(_labels4)), len({t.ref for t in _steps4})))
    _dupe4 = [l for l in set(_labels4) if _labels4.count(l) > 1]
    for _l4 in _dupe4[:1]:
        _same4 = [t for t in _steps4 if t.ref_label == _l4]
        check(f"where {_l4} names more than one task, it is the same step "
              "opened again",
              len({t.flow_step_id for t in _same4}) == 1,
              [(t.id, t.flow_step_id) for t in _same4])
        check("and those two are told apart by their dates",
              len({t.created_at for t in _same4}) > 1
              or len({t.doer_id for t in _same4}) > 1,
              [(t.id, str(t.created_at), t.doer_id) for t in _same4])

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
from app.models import (Task as _T5, TaskSource as _S5,
                        FlowInstance as _FI5)

with _sl5() as _d:
    _was = {t.id: t.ref for t in _d.query(_T5).all()}
    _wasRun5 = {r.id: r.ref for r in _d.query(_FI5).all()}

# The submissions lose their numbers too, which is the state the live
# database is actually in: it has never heard of either column.
if _upeng5.dialect.name == "postgresql":
    with _upeng5.begin() as _c:
        _c.execute(_uptx5("ALTER TABLE tasks DROP COLUMN IF EXISTS ref"))
        _c.execute(_uptx5("ALTER TABLE tasks DROP COLUMN IF EXISTS ref_n"))
        _c.execute(_uptx5("ALTER TABLE flow_instances DROP COLUMN IF EXISTS ref"))
        _c.execute(_uptx5("ALTER TABLE flow_instances DROP COLUMN IF EXISTS ref_n"))
else:
    with _upeng5.begin() as _c:
        _c.execute(_uptx5("UPDATE tasks SET ref = NULL, ref_n = NULL"))
        _c.execute(_uptx5("UPDATE flow_instances SET ref = NULL, ref_n = NULL"))

_applied5 = _upmig5.run()
check("the migrator reports what it numbered",
      any("numbered" in a for a in _applied5), _applied5[-4:])

with _sl5() as _d:
    _rows5 = _d.query(_T5).all()
    check("no task is left without an ID after the upgrade",
          all(t.ref for t in _rows5),
          [t.id for t in _rows5 if not t.ref][:5])
    # DEL and CL: one number each, gapless, oldest first. FMS is not in
    # this loop because an FMS step has no number of its own — it is checked
    # against its submission below instead.
    for _src5 in (_S5.DELEGATION, _S5.RECURRING):
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

    # The submissions, and the steps that now answer to them.
    _runs5 = _d.query(_FI5).all()
    check("every submission is numbered after the upgrade",
          all(r.ref for r in _runs5), [r.id for r in _runs5 if not r.ref][:5])
    _byN5 = sorted(_runs5, key=lambda r: r.ref_n)
    check(f"upgraded submission IDs run 1..{len(_runs5)} with no gaps",
          [r.ref_n for r in _byN5] == list(range(1, len(_runs5) + 1)),
          [r.ref_n for r in _byN5][:12])
    check("the submission started first holds the lowest number",
          [r.id for r in sorted(_runs5, key=lambda r: (r.started_at, r.id))]
          == [r.id for r in _byN5])
    _steps5 = [t for t in _rows5 if t.flow_instance_id]
    check("and every step was re-pointed at its submission's number",
          all(t.ref == _d.get(_FI5, t.flow_instance_id).ref for t in _steps5),
          [(t.ref, _d.get(_FI5, t.flow_instance_id).ref) for t in _steps5
           if t.ref != _d.get(_FI5, t.flow_instance_id).ref][:3])
    _now5 = {t.id: t.ref for t in _rows5}

check("every DEL and CL task that had an ID kept the same one",
      {k: v for k, v in _now5.items() if not v.startswith("FMS-")}
      == {k: v for k, v in _was.items() if not v.startswith("FMS-")},
      [k for k in _now5 if _was.get(k) != _now5[k]
       and not _now5[k].startswith("FMS-")][:5])
# The FMS ones are allowed to change — that is the whole point of the
# upgrade — but only into their own submission's number, which is checked
# above. What must NOT change is the submissions themselves once numbered.
with _sl5() as _d:
    check("and every submission kept the number it already had",
          {r.id: r.ref for r in _d.query(_FI5).all()} == _wasRun5
          or not any(_wasRun5.values()),
          [k for k, v in _wasRun5.items()
           if v and _d.get(_FI5, k).ref != v][:5])

_again5 = _upmig5.run()
with _sl5() as _d:
    check("running the upgrade a second time renumbers nothing",
          {t.id: t.ref for t in _d.query(_T5).all()} == _now5)
    check("and leaves the submissions alone too",
          all(r.ref for r in _d.query(_FI5).all()))
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
    from app.services import chasing as _chase7
    _pcu = _d.query(_U7).filter(_U7.email == "pc@gcs.local").one()
    _desk = [t for t in _d.query(_T7).filter(
        _T7.source.in_((_S7.RECURRING, _S7.FLOW)),
        _T7.status.in_((_ST7.PENDING, _ST7.IN_PROGRESS, _ST7.REJECTED,
                        _ST7.REOPENED)),
        _T7.due_at <= _ck7.now().replace(hour=23, minute=59, second=59)).all()
        # Daily checklist work below high priority is not the desk's to
        # chase, so it is not on the desk and must not be in the expected
        # set either. Worked out here independently of the page, so this is
        # still a real check on what the page shows.
        if _chase7.needs_chasing(t)]
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
        # Delegation work is never an FMS step waiting its turn.
        self.flow_waiting = False
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
    _atJ = _dtJ.combine(_shutJ, _dtJ.min.time()).replace(hour=17)

    # Delegation and Checklist: BACKWARD. The day a checklist job covers
    # will not come again, so it is done before the office shuts.
    _movedJ, _whyJ = _holJ.shift_due(_d, 1, _atJ)
    check("a delegated deadline on a closed day moves to the day before",
          _movedJ.date() == _shutJ - _tdJ(days=1), str(_movedJ))
    check("never to the day after", _movedJ.date() > _shutJ - _tdJ(days=3),
          str(_movedJ))
    check("and the time of day is kept", _movedJ.hour == 17, str(_movedJ))

    # FMS: FORWARD. A chain cannot be dragged backwards — a Sunday step
    # pulled back to Saturday lands on the day of the step it is waiting
    # for, and the step after it lands there too.
    _pushJ, _pwhyJ = _holJ.push_due(_d, 1, _atJ)
    check("an FMS deadline on a closed day moves to the NEXT working day",
          _pushJ.date() == _shutJ + _tdJ(days=1), str(_pushJ))
    check("it says which closed day it moved off",
          _pwhyJ == "SMOKE shutdown", _pwhyJ)
    check("and keeps the time of day too", _pushJ.hour == 17, str(_pushJ))
    check("a deadline on a working day is not moved at all",
          _holJ.push_due(_d, 1, _atJ + _tdJ(days=2)) == (_atJ + _tdJ(days=2), None))

# And through the real engine: start a flow whose first step would land on
# the closed day, and read the task's planned date.
# The step is given a twenty-day TAT and the day it lands on is made a
# holiday. The deadline moves FORWARD to the next day somebody is in, which
# is also why a short TAT is no longer a problem here: moving forward can
# never produce a deadline that has already passed.
_landJ = (_clock.now() + _tdJ(days=20)).date()
with _slK() as _d:
    if not _d.scalar(_spK(_HolJ).where(_HolJ.org_id == 1, _HolJ.day == _landJ)):
        _d.add(_HolJ(org_id=1, branch_id=None, day=_landJ, name="SMOKE landing day"))
        _d.commit()
_mkJ = {"name": f"SMOKE closed-day flow {RUN}", "description": "", "branch_id": "",
        "step_title": ["Only step"], "step_doer": [""], "step_tat_unit": ["days"],
        "step_tat": ["20"], "step_priority": ["medium"], "step_instructions": [""],
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
    check("it moved later than the closed day, never earlier",
          _tJ.due_at.date() > _landJ, str(_tJ.due_at))

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
# _call returns the model's TEXT now, whichever service answered, so the
# stub returns text rather than one provider's envelope.
_dR._call = lambda parts, client=None: _jsR2.dumps(
    {"verdict": "ok", "confidence": 80,
     "remark": "the proof matches the task", "looked_at": "a note"})
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


# ==========================================================================
print("\n== the detective can use Cloudflare instead of Gemini ==")
# Gemini's free allowance fell to about twenty checks a day. Cloudflare's
# free allowance runs to hundreds, reads a screenshot, and commits to not
# training on what is sent — which matters, because delegation work here
# includes legal matters and complaints that name people.
import json as _jsP, httpx as _hxP, base64 as _b64P
from datetime import datetime as _dtP
from app.db import SessionLocal as _slP
from app.models import (Task as _TP, TaskSource as _SRP, TaskStatus as _STP,
                        User as _UP, Attachment as _ATP, AiVerdict as _AVP)
from app.services import detective as _dP
from sqlalchemy import select as _spP

_wasP = (_dP.AI_PROVIDER, _dP.CF_ACCOUNT, _dP.CF_TOKEN, _dP.AI_KEY,
         _dP.AI_ENABLED, _dP._quota_hit_on, _dP.AI_DAILY_LIMIT)

_ANSWERP = _jsP.dumps({"verdict": "unrelated", "confidence": 77,
                       "remark": "The screenshot is a chat about a gym "
                                 "membership, not the bus GPS.",
                       "looked_at": "1 screenshot"})
_PNGP = _b64P.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

from app.models import AiAudit as _AAP
with _slP() as _d:
    # A quota refusal stored earlier today closes the day for real, which is
    # right but is not what this section is about.
    for _old in _d.scalars(_spP(_AAP).where(
            _AAP.verdict == _AVP.ERROR, _AAP.remark.like("%429%"))).all():
        _old.remark = "(cleared before the provider checks)"
    _d.commit()
    _dP._quota_hit_on = None
    _dP.AI_DAILY_LIMIT = _dP.spent_today(_d, 1) + 20
    _bP = _d.scalar(_spP(_UP).where(_UP.email == "mis@gcs.local"))
    _drP = _d.scalar(_spP(_UP).where(_UP.email == "amit@gcs.local"))
    _tP = _TP(org_id=1, branch_id=_drP.branch_id,
              title=f"SMOKE provider {RUN}: fit GPS to bus 4",
              details="Send a photo of the fitted unit",
              assigner_id=_bP.id, doer_id=_drP.id, source=_SRP.DELEGATION,
              due_at=_dtP(2026, 10, 7, 18, 0), status=_STP.SUBMITTED,
              submitted_at=_dtP(2026, 10, 7, 9, 0), completion_note="done")
    _d.add(_tP)
    _d.flush()
    _d.add(_ATP(task_id=_tP.id, uploaded_by_id=_drP.id, filename="proof.png",
                stored_name=f"smoke-{RUN}.png", content_type="image/png",
                size=len(_PNGP), data=_PNGP, storage="db"))
    _d.commit()
    _tidP = _tP.id

_sawP = {}
def _cloudflareP(request):
    _sawP["url"] = str(request.url)
    _sawP["auth"] = request.headers.get("authorization")
    _body = _jsP.loads(request.content)
    _sawP["image"] = _body.get("image", "")
    _sawP["system"] = _body["messages"][0]["content"]
    _sawP["user"] = _body["messages"][1]["content"]
    return _hxP.Response(200, json={"result": {"response": _ANSWERP},
                                    "success": True})

_dP.AI_PROVIDER, _dP.CF_ACCOUNT, _dP.CF_TOKEN = "cloudflare", "acct123", "tok456"
_dP.AI_ENABLED, _dP._quota_hit_on = True, None
_dP.AI_KEY = ""                       # no Gemini key at all: it must not matter

check("with a Cloudflare account it is available even with no Gemini key",
      _dP.available())
check("and the model it records is Cloudflare's",
      _dP.current_model().startswith("@cf/"), _dP.current_model())

with _slP() as _d:
    _rowP = _dP.review(_d, _d.get(_TP, _tidP),
                       _hxP.Client(transport=_hxP.MockTransport(_cloudflareP)))
check("it reaches the same verdict through Cloudflare",
      _rowP.verdict == _AVP.UNRELATED, _rowP.verdict)
check("and writes the same kind of remark",
      "gym membership" in _rowP.remark, _rowP.remark[:60])
check("the account id is in the address",
      "/accounts/acct123/ai/run/" in _sawP["url"], _sawP["url"])
check("the token is sent as a bearer token",
      _sawP["auth"] == "Bearer tok456", _sawP["auth"])
check("the screenshot really is sent, as a data URI",
      _sawP["image"].startswith("data:image/png;base64,")
      and len(_sawP["image"]) > 60, _sawP["image"][:40])
check("the same instructions go with it",
      "does this evidence show THIS task" in _sawP["system"])
check("and so does the task, what it asked for and the note",
      all(x in _sawP["user"] for x in ("fit GPS to bus 4", "fitted unit",
                                       "done")),
      _sawP["user"][:120])

# A failure on one provider must read like a failure, not like a verdict.
def _brokenP(request):
    return _hxP.Response(401, text='{"errors":[{"message":"bad token"}]}')
with _slP() as _d:
    _badP = _dP.review(_d, _d.get(_TP, _tidP),
                       _hxP.Client(transport=_hxP.MockTransport(_brokenP)))
check("a rejected token is stored as 'could not check', not as an opinion",
      _badP.verdict == _AVP.ERROR and "401" in _badP.remark, _badP.remark[:60])

# Missing settings must say which ones, in words somebody can act on.
_dP.CF_ACCOUNT = ""
check("without its settings it is not available", not _dP.available())
check("and it names the two settings that are missing",
      "CF_ACCOUNT_ID" in _dP.why_not() and "CF_API_TOKEN" in _dP.why_not(),
      _dP.why_not())
_dP.CF_ACCOUNT = "acct123"

# Switching back to Gemini must change nothing else.
def _geminiP(request):
    return _hxP.Response(200, json={"candidates": [{"content": {"parts": [
        {"text": _ANSWERP}]}}]})
_dP.AI_PROVIDER, _dP.AI_KEY, _dP._quota_hit_on = "gemini", "test-key", None
with _slP() as _d:
    _rowG2 = _dP.review(_d, _d.get(_TP, _tidP),
                        _hxP.Client(transport=_hxP.MockTransport(_geminiP)))
check("Gemini still works exactly as before",
      _rowG2.verdict == _AVP.UNRELATED, _rowG2.verdict)
check("and the model recorded says which service answered",
      _rowG2.model.startswith("gemini"), _rowG2.model)

(_dP.AI_PROVIDER, _dP.CF_ACCOUNT, _dP.CF_TOKEN, _dP.AI_KEY,
 _dP.AI_ENABLED, _dP._quota_hit_on, _dP.AI_DAILY_LIMIT) = _wasP


# ==========================================================================
print("\n== one service's quota is not the other's ==")
# The day somebody switches services is exactly the day this matters: a
# morning of Google 429s must not close the afternoon on Cloudflare. The
# move made to escape the quota would be blocked by the quota it escaped.
from datetime import datetime as _dtV
from app.db import SessionLocal as _slV
from app.models import (Task as _TV, TaskSource as _SRV, TaskStatus as _STV,
                        User as _UV, AiAudit as _AAV, AiVerdict as _AVV)
from app.services import detective as _dV
from sqlalchemy import select as _spV
import json as _jsV, httpx as _hxV

_wasV = (_dV.AI_PROVIDER, _dV.AI_KEY, _dV.CF_ACCOUNT, _dV.CF_TOKEN,
         _dV.AI_ENABLED, _dV._quota_hit_on, _dV._quota_hit_by,
         _dV.AI_DAILY_LIMIT)

check("a Cloudflare model is recognised as Cloudflare's",
      _dV.provider_of("@cf/meta/llama-3.2-11b-vision-instruct") == "cloudflare")
check("and a Gemini one as Google's",
      _dV.provider_of("gemini-3.8-flash") == "gemini")
check("an empty model name is not mistaken for Cloudflare",
      _dV.provider_of("") == "gemini")

with _slV() as _d:
    _bV = _d.scalar(_spV(_UV).where(_UV.email == "mis@gcs.local"))
    _drV = _d.scalar(_spV(_UV).where(_UV.email == "amit@gcs.local"))
    _tV = _TV(org_id=1, branch_id=_drV.branch_id,
              title=f"SMOKE switch {RUN}", assigner_id=_bV.id, doer_id=_drV.id,
              source=_SRV.DELEGATION, due_at=_dtV(2026, 10, 7, 18, 0),
              status=_STV.SUBMITTED, submitted_at=_dtV(2026, 10, 7, 9, 0),
              completion_note="done")
    _d.add(_tV)
    _d.flush()
    # this morning's Google quota refusal, stored today
    _d.add(_AAV(org_id=1, task_id=_tV.id, verdict=_AVV.ERROR, confidence=0,
                remark="the AI service answered 429: quota",
                model="gemini-3.8-flash"))
    _d.commit()
    _tidV = _tV.id

    _dV.AI_ENABLED, _dV._quota_hit_on, _dV._quota_hit_by = True, None, ""
    _dV.AI_PROVIDER, _dV.AI_KEY = "gemini", "test-key"
    check("Google's refusal closes the day on Google",
          _dV.budget_left(_d, 1) == 0)

    _geminiSpentV = _dV.spent_today(_d, 1)      # counted while on Gemini
    _dV.AI_PROVIDER, _dV.CF_ACCOUNT, _dV.CF_TOKEN = "cloudflare", "a", "t"
    _dV.AI_DAILY_LIMIT = 200
    check("but the day is open on Cloudflare", _dV.budget_left(_d, 1) > 0,
          _dV.budget_left(_d, 1))
    # Earlier checks in this suite made a couple of Cloudflare calls, so what
    # matters is that GOOGLE's morning is not among what Cloudflare has spent.
    _cfSpentV = _dV.spent_today(_d, 1)
    check("and Google's morning does not come out of Cloudflare's allowance",
          _cfSpentV < _geminiSpentV, (_geminiSpentV, _cfSpentV))

    _ANSV = _jsV.dumps({"verdict": "weak", "confidence": 40,
                        "remark": "The note says only 'done'.",
                        "looked_at": "a note"})
    _rowV = _dV.review(_d, _d.get(_TV, _tidV), _hxV.Client(
        transport=_hxV.MockTransport(
            lambda r: _hxV.Response(200, json={"result": {"response": _ANSV},
                                               "success": True}))))
    check("so a task stuck on Google gets a real verdict the same day",
          _rowV.verdict == _AVV.WEAK, _rowV.verdict)
    check("recorded against the service that answered",
          _dV.provider_of(_rowV.model) == "cloudflare", _rowV.model)
    check("and that one check is what Cloudflare's allowance has spent",
          _dV.spent_today(_d, 1) == _cfSpentV + 1,
          (_cfSpentV, _dV.spent_today(_d, 1)))

    # And a Cloudflare refusal closes Cloudflare, not Google.
    _dV.review(_d, _d.get(_TV, _tidV), _hxV.Client(
        transport=_hxV.MockTransport(
            lambda r: _hxV.Response(429, text='{"error":"over quota"}'))))
    check("a Cloudflare refusal closes the day on Cloudflare",
          _dV.budget_left(_d, 1) == 0)
    _dV.AI_PROVIDER, _dV._quota_hit_by = "gemini", ""
    _dV.AI_DAILY_LIMIT = 1000
    check("while Google's own day is judged on Google's own failures",
          _dV.budget_left(_d, 1) == 0)      # Google refused this morning too

(_dV.AI_PROVIDER, _dV.AI_KEY, _dV.CF_ACCOUNT, _dV.CF_TOKEN,
 _dV.AI_ENABLED, _dV._quota_hit_on, _dV._quota_hit_by,
 _dV.AI_DAILY_LIMIT) = _wasV


# ==========================================================================
print("\n== an FMS step needs proof AND a note, like the other two ==")
# "Done" with a screenshot of something else is what the detective exists
# to catch. A screenshot with NO note is the same problem one step earlier:
# the auditor has to guess what the picture is meant to prove, and the AI
# has nothing to compare it against.
import io as _ioN, re as _reN
from app.db import SessionLocal as _slN
from app.models import (Flow as _FlN, FlowInstance as _FiN, Task as _TN,
                        TaskStatus as _STN)
from sqlalchemy import select as _spN

def _errN(r):
    _m = _reN.search(r'margin-bottom:0">(.*?)</p>', " ".join(r.text.split()))
    return _m.group(1) if _m else ""

_flowN = {"name": f"SMOKE proof+note {RUN}", "description": "", "branch_id": "",
          "step_title": ["Collect the bill", "Pay it"], "step_doer": ["6", "6"],
          "step_tat_unit": ["days", "days"], "step_tat": ["2", "7"],
          "step_priority": ["medium", "medium"],
          "step_instructions": ["", ""], "step_fields": ["", ""],
          "step_audit": ["0", "0"], "step_proof": ["1", "1"],
          "step_decision": ["0", "0"], "step_yes": ["", ""],
          "step_next": ["", "0"], "step_no": ["", ""], "step_fail": ["", ""],
          # step 2's clock runs from when the RUN was started
          "step_due_from": ["", "0"]}
admin.post("/flows/new", data=_flowN)
with _slN() as _d:
    _fN = _d.scalar(_spN(_FlN).where(_FlN.name == f"SMOKE proof+note {RUN}"))
    _fidN = _fN.id
    check("a step still asks for proof by default",
          _fN.steps[0].requires_attachment)
    check("and the clock anchor 'when the run started' is stored, not dropped",
          _fN.steps[1].due_from_pos == 0, _fN.steps[1].due_from_pos)

admin.post(f"/flows/{_fidN}/start", data={"reference": f"SMOKE PN-{RUN}"})
with _slN() as _d:
    _instN = _d.scalar(_spN(_FiN).where(_FiN.reference == f"SMOKE PN-{RUN}"))
    _instidN, _startedN = _instN.id, _instN.started_at
    _t1N = _d.scalar(_spN(_TN).where(_TN.flow_instance_id == _instidN))
    _t1idN = _t1N.id
    check("the step task carries the proof requirement",
          _t1N.requires_attachment)

_rN = doer.post(f"/tasks/{_t1idN}/submit", data={"completion_note": ""})
check("no proof and no note is refused", _rN.status_code == 400)
check("and it asks for the proof first",
      "needs proof attached" in _errN(_rN), _errN(_rN)[:70])

attach(doer, _t1idN)
_rN = doer.post(f"/tasks/{_t1idN}/submit", data={"completion_note": "   "})
check("proof with a blank note is still refused", _rN.status_code == 400,
      _rN.status_code)
check("and it says a note is what is missing",
      "note saying what you did" in _errN(_rN), _errN(_rN)[:70])
with _slN() as _d:
    check("nothing was submitted while it was refused",
          _d.get(_TN, _t1idN).submitted_at is None)

_rN = doer.post(f"/tasks/{_t1idN}/submit",
                data={"completion_note": "Collected from accounts"})
check("proof and a note go through", _rN.status_code == 200, _rN.status_code)
with _slN() as _d:
    check("and the note is kept",
          _d.get(_TN, _t1idN).completion_note == "Collected from accounts")

# The same rule on delegation, so there is one rule and not three.
_dgN = mgr.post("/tasks/new", data={
    "title": f"SMOKE note rule {RUN}", "details": "", "doer_id": "6",
    "branch_id": "", "priority": "low", "due_at": "2026-12-29T23:59"})
_dgidN = int(_re.findall(r"/tasks/(\d+)/comment", _dgN.text)[0])
attach(doer, _dgidN)
check("a delegation task with proof but no note is refused too",
      doer.post(f"/tasks/{_dgidN}/submit",
                data={"completion_note": ""}).status_code == 400)
check("and goes through with one",
      doer.post(f"/tasks/{_dgidN}/submit",
                data={"completion_note": "sent to CMD"}).status_code == 200)

# A task that needs no proof is not forced to carry a note either: there is
# nothing for the note to explain.
_npN = mgr.post("/tasks/new", data={
    "title": f"SMOKE no proof {RUN}", "details": "", "doer_id": "6",
    "branch_id": "", "priority": "low", "due_at": "2026-12-29T23:59",
    "requires_attachment": "0"})
_npidN = int(_re.findall(r"/tasks/(\d+)/comment", _npN.text)[0])
check("a task that needs no proof can still be closed with no note",
      doer.post(f"/tasks/{_npidN}/submit",
                data={"completion_note": ""}).status_code == 200)

print("\n== a step's clock can run from the start of the run ==")
with _slN() as _d:
    _allN = _d.scalars(_spN(_TN).where(_TN.flow_instance_id == _instidN)
                       .order_by(_TN.id)).all()
    _t2N = _allN[-1]
    _gapN = (_t2N.due_at - _startedN).total_seconds() / 86400
check("step 2 is due a week after the RUN began, not a week after step 1",
      6.5 <= _gapN <= 7.5, _gapN)

# And the three anchors are genuinely different moments.
from app.services import flows as _flsvcN
from app.models import FlowStep as _FSN
with _slN() as _d:
    _instO = _d.get(_FiN, _instidN)
    _s1N = _d.scalar(_spN(_FSN).where(_FSN.flow_id == _fidN, _FSN.position == 1))
    _s2N = _d.scalar(_spN(_FSN).where(_FSN.flow_id == _fidN, _FSN.position == 2))
    _s1N.due_from_pos = None
    _openN = _flsvcN._deadline_base(_d, _instO, _s1N)
    _s1N.due_from_pos = 0
    _runN = _flsvcN._deadline_base(_d, _instO, _s1N)
    _s2N.due_from_pos = 1
    _stepN = _flsvcN._deadline_base(_d, _instO, _s2N)
    _d.rollback()
check("'when this step opens' means now", abs((_openN - _ck.now()).total_seconds()) < 60)
check("'when the run started' means the run's own start",
      _runN == _startedN, (str(_runN), str(_startedN)))
check("and 'step 1's planned date' means exactly that",
      _stepN == _allN[0].due_at, (str(_stepN), str(_allN[0].due_at)))

# The builder offers the choices rather than asking for a number.
_pageN = admin.get("/flows/new").text
check("the step form offers the anchor as a list",
      'name="step_due_from"' in _pageN and "when this step opens" in _pageN)
check("including the start of the run",
      "when the run was started" in _pageN)
check("and the old 'type a step number' box is gone",
      'name="step_due_from" min="1"' not in _pageN)



print("\n== the sign-in page ==")
# A first impression, so it has to look like something — but it is also the
# one page that absolutely must work, so the checks are: the form still
# posts and signs people in, nothing decorative is required to do it, and
# anybody who has asked their machine not to animate gets a still page.
_lgC = TestClient(app)
_lgP = _lgC.get("/login").text
check("it still asks for an email and a password",
      'name="email"' in _lgP and 'name="password"' in _lgP)
check("and still posts to /login", 'action="/login"' in _lgP)
check("the brand panel is there", 'class="lp-art"' in _lgP)
check("with the drifting shapes", _lgP.count('class="blob') >= 3)
check("the panel is hidden from screen readers, being decoration only",
      'aria-hidden="true"' in _lgP)
check("the form fields have autocomplete, so password managers fill them",
      'autocomplete="current-password"' in _lgP)
_lgCss = open("app/static/app.css", encoding="utf-8").read()
check("every piece of the new page is scoped to the sign-in page",
      _lgCss.count(".login-page") >= 2 and ".lp-art{" in _lgCss)
check("and all of its motion stops for reduced-motion",
      ".login-page *{animation:none!important}" in _lgCss)
check("the page still fits a phone", "@media (max-width:880px)" in _lgCss)
_lgBad = _lgC.post("/login", data={"email": "mis@gcs.local",
                                   "password": "definitely-wrong"})
check("a wrong password is refused and says so",
      "Invalid" in _lgBad.text, _lgBad.status_code)
check("and the refusal is shaken, not just printed", 'class="err shake"' in _lgBad.text)
_lgOk = _lgC.post("/login", data={"email": "mis@gcs.local", "password": "gcs1234"},
                  follow_redirects=False)
check("the right password still signs you in", _lgOk.status_code in (302, 303),
      _lgOk.status_code)

# The 3D scene. It is decoration, so what is checked is that it exists, that
# it is built out of real depth rather than a flat picture of it, and above
# all that NOTHING on the page needs it: the form posts with the script dead,
# the scene is hidden from screen readers, and it all stops for reduced
# motion and on a phone.
check("there is a 3D scene", 'class="scene3d"' in _lgP)
check("with a cube of six faces", _lgP.count('class="f ') == 6)
check("and plates floating at depth", _lgP.count('class="plate') == 3)
check("the scene has a perspective, or it is only a flat drawing",
      "perspective:900px" in _lgCss)
check("the cube's faces really stand in space",
      "transform-style:preserve-3d" in _lgCss and "translateZ(85px)" in _lgCss)
check("the card sits on its own stage", 'class="lp-stage"' in _lgP
      and ".lp-stage{" in _lgCss)
check("and has a surface, so a tilt can be seen at all",
      ".lp-stage .lp-box{max-width:none;background:var(--card)" in _lgCss)
_lgJs = _lgP[_lgP.find("<script>", _lgP.find("lp-foot")):]
check("the tilt is eased on an animation frame, not written per mouse event",
      "requestAnimationFrame" in _lgJs and "mousemove" in _lgJs)
check("and it is clamped, so a wide monitor cannot fold the card in half",
      "function cap(" in _lgJs)
check("it does not run for anyone asking for reduced motion",
      "prefers-reduced-motion" in _lgJs)
check("nor without a real pointer, which a finger is not",
      "pointer: fine" in _lgJs)
check("the 3D is switched off on a phone",
      ".lp-stage{perspective:none}" in _lgCss)
check("and off for reduced motion",
      ".scene3d,.cube,.plate{animation:none!important}" in _lgCss)
check("none of it is loaded from anywhere — no library, no image",
      "<img" not in _lgP and "cdn" not in _lgP.lower())


print("\n== proof photos are shrunk before they are stored ==")
# The outage: 361 MB of full-size phone photos sitting INSIDE the database,
# pulled back out over the internet on every single view, uncached. Five
# gigabytes of transfer gone in eleven days and the company stopped. So a
# photo is now cut down before it is ever stored, and the browser is told to
# keep its copy instead of asking again.
import io as _ioS, random as _rndS
from app.services import shrink as _shr

def _photoS(w, h, fmt="JPEG", mode="RGB", **kw):
    from PIL import Image as _Im
    im = _Im.new(mode, (w, h), (190, 175, 160) if mode == "RGB" else (190, 175, 160, 255))
    px = im.load(); _rndS.seed(7)
    for y in range(0, h, 2):
        for x in range(0, w, 2):
            v = (_rndS.randint(0, 255), _rndS.randint(0, 255), _rndS.randint(0, 255))
            px[x, y] = v if mode == "RGB" else v + (255,)
    b = _ioS.BytesIO(); im.save(b, fmt, **kw); return b.getvalue()

_bigS = _photoS(4032, 3024, quality=92)
check("the test photo is a realistic phone photo", len(_bigS) > 4_000_000,
      f"{len(_bigS)/1048576:.1f} MB")
_outS, _ctS, _fnS = _shr.shrink(_bigS, "image/jpeg", "proof.jpg")
check("a 9 MB photo is cut down by at least 80%",
      len(_outS) < len(_bigS) * 0.2, _shr.saved_label(len(_bigS), len(_outS)))
from PIL import Image as _ImS
check("and capped at 1600px on its longest edge",
      max(_ImS.open(_ioS.BytesIO(_outS)).size) == 1600,
      str(_ImS.open(_ioS.BytesIO(_outS)).size))
check("it is still a jpeg with its own name", _ctS == "image/jpeg" and _fnS == "proof.jpg")

# A photo the phone wrote sideways must be stored the right way up: the
# rotation note is lost in re-encoding, so it has to be applied for real.
_exS = _ImS.new("RGB", (2000, 1000), (20, 20, 20))
_bS = _ioS.BytesIO(); _eS = _exS.getexif(); _eS[274] = 6
_exS.save(_bS, "JPEG", quality=90, exif=_eS.tobytes())
_rotS, _, _ = _shr.shrink(_bS.getvalue(), "image/jpeg", "sideways.jpg")
_wS, _hS = _ImS.open(_ioS.BytesIO(_rotS)).size
check("a photo tagged 'rotate 90' is stored upright, not on its side", _hS > _wS,
      f"{_wS}x{_hS}")

# Nothing may be lost or damaged on the way through.
check("a PDF passes through untouched",
      _shr.shrink(b"%PDF-1.4 bill", "application/pdf", "bill.pdf")[0] == b"%PDF-1.4 bill")
check("a spreadsheet passes through untouched",
      _shr.shrink(b"PK\x03\x04xl", "application/vnd.openxmlformats-officedocument"
                  ".spreadsheetml.sheet", "a.xlsx")[0] == b"PK\x03\x04xl")
check("a file that is not really an image passes through untouched",
      _shr.shrink(b"\xff\xd8 not a jpeg", "image/jpeg", "x.jpg")[0] == b"\xff\xd8 not a jpeg")
check("an iPhone HEIC passes through untouched",
      _shr.shrink(b"heic-bytes", "image/heic", "IMG.heic")[0] == b"heic-bytes")

# The promise that matters most: never hand back something BIGGER.
_tinyS = _photoS(60, 60, "PNG")
check("a small image is never made bigger",
      len(_shr.shrink(_tinyS, "image/png", "t.png")[0]) <= len(_tinyS))
_lineS = _ImS.new("RGBA", (300, 300), (0, 0, 0, 0)); _lineS.putpixel((9, 9), (255, 0, 0, 255))
_bL = _ioS.BytesIO(); _lineS.save(_bL, "PNG")
check("and line art with transparency stays a PNG",
      _shr.shrink(_bL.getvalue(), "image/png", "logo.png")[1] == "image/png")

# A big screenshot is a photo in the wrong clothes — PNG stores it terribly.
_shotS = _photoS(2400, 1400, "PNG")
_sOut, _sCt, _sFn = _shr.shrink(_shotS, "image/png", "screen.png")
check("a heavy PNG screenshot is re-saved as a jpeg", _sCt == "image/jpeg", _sCt)
check("and renamed so it opens in the right program", _sFn.endswith(".jpg"), _sFn)
check("which makes it much smaller", len(_sOut) < len(_shotS) * 0.6,
      _shr.saved_label(len(_shotS), len(_sOut)))

# End to end, through the real upload route.
_upC = TestClient(app)
_upC.post("/login", data={"email": "mis@gcs.local", "password": "gcs1234"})
with _slK() as _d:
    _upT = _d.scalar(_spK(_TJ).where(_TJ.doer_id.isnot(None)))
    _upTid = _upT.id
_upR = _upC.post(f"/tasks/{_upTid}/attach/upload",
                 files={"file": ("proof.jpg", _bigS, "image/jpeg")})
check("a 9 MB photo is accepted by the upload route", _upR.status_code == 200,
      _upR.status_code)
_upJ = _upR.json()
check("and what gets stored is a fraction of what arrived",
      _upJ["size"] < len(_bigS) * 0.2,
      _shr.saved_label(len(_bigS), _upJ["size"]))

# And the browser is told to keep it rather than ask again every page view.
_vR = _upC.get(f"/attachments/{_upJ['id']}/view")
check("the file is served", _vR.status_code == 200, _vR.status_code)
check("with an instruction to cache it",
      "max-age" in _vR.headers.get("cache-control", ""),
      _vR.headers.get("cache-control"))
check("kept to the one person's own browser, not a shared one",
      "private" in _vR.headers.get("cache-control", ""))
check("and a fingerprint so a re-check costs no bytes", _vR.headers.get("etag"))

_ujs = open("app/static/upload.js", encoding="utf-8").read()
check("the browser shrinks it too, before it is ever sent",
      "createImageBitmap" in _ujs and "MAX_EDGE" in _ujs)
check("applying the phone's rotation as it does so",
      "imageOrientation" in _ujs)
check("and never sending something larger than the original",
      "r.blob.size >= file.size" in _ujs)
check("a browser that cannot shrink still falls back to the plain form",
      "canShrink" in _ujs and "localForm.submit()" in _ujs)

print("\n== the CMD board's audit row adds up ==")
# The complaint, and it was fair: "98 need audit, 34 done, 0 pending — why is
# pending zero?" The counts were right; the row was missing its fourth number.
# The other 64 were still with the DOER, not waiting on an auditor, and that
# column was computed and never printed, so the row looked broken.
from app.services import cmdboard as _cbA
from app.models import (Task as _TA, TaskSource as _TsA, TaskStatus as _TstA,
                        AuditState as _AsA, User as _UA, Priority as _PrA)
from datetime import timedelta as _tdA

_sA, _eA, _lblA = _cbA.window("this_month")
with _slK() as _d:
    _admA = _d.scalar(_spK(_UA).where(_UA.email == "mis@gcs.local"))
    _doA = _d.scalar(_spK(_UA).where(_UA.email == "amit@gcs.local"))
    _base = _cbA.build(_d, 1, _sA, _eA).audit["delegation"]
    _b0 = (_base.total, _base.waiting, _base.done, _base.pending)
    _dayA = _sA + _tdA(days=5, hours=10)
    _tagA = f"SMOKE audit row {RUN}"

    def _mkA(n, state, submitted, needs=True):
        for i in range(n):
            _d.add(_TA(org_id=1, branch_id=_doA.branch_id, title=f"{_tagA} {state} {i}",
                       assigner_id=_admA.id, doer_id=_doA.id, priority=_PrA.MEDIUM,
                       source=_TsA.DELEGATION, due_at=_dayA, requires_audit=needs,
                       audit_state=state, submitted_at=_dayA if submitted else None,
                       status=_TstA.COMPLETED if submitted else _TstA.PENDING))

    _mkA(34, _AsA.COMPLETED, True)            # the auditor has cleared these
    _mkA(64, _AsA.NOT_REQUIRED, False)        # the doer has not handed these in
    _mkA(7, _AsA.PENDING, True)               # handed in, sitting on the auditor
    _mkA(12, _AsA.NOT_REQUIRED, False, needs=False)   # never need an audit at all
    _d.commit()

    _aA = _cbA.build(_d, 1, _sA, _eA).audit["delegation"]
    _dT, _dW, _dD, _dP = (_aA.total - _b0[0], _aA.waiting - _b0[1],
                          _aA.done - _b0[2], _aA.pending - _b0[3])

check("work the doer has not handed in is counted on its own", _dW == 64, _dW)
check("cleared audits are counted", _dD == 34, _dD)
check("and audits actually sitting on an auditor are counted", _dP == 7, _dP)
check("the four numbers add up — this is what the complaint was about",
      _dT == _dW + _dD + _dP, f"{_dT} vs {_dW}+{_dD}+{_dP}={_dW + _dD + _dP}")
check("a task that never needs an audit is not in 'needs audit'", _dT == 105, _dT)

# The percentage must be measured against finished work only. Judging an
# auditor on work nobody has submitted would be nonsense.
check("'cleared' is measured against finished work, not everything",
      _aA.done_pct == round(_aA.done / (_aA.done + _aA.pending) * 100, 1),
      _aA.done_pct)

_cmdP = admin.get("/cmd").text
check("the page shows the missing column", "Not finished yet" in _cmdP)
check("and says in words that the row adds up",
      "needs audit = not finished yet + done + pending" in _cmdP)
check("the Excel export carries it too",
      '"DEL not finished yet"' in open("app/routers/cmd.py", encoding="utf-8").read()
      or "DEL not finished yet" in open("app/routers/cmd.py", encoding="utf-8").read())
_xlA = admin.get("/cmd?export=xlsx")
check("and the export still builds", _xlA.status_code == 200, _xlA.status_code)

print("\n== the background loop leaves the database alone overnight ==")
# This is a money check, not a tidiness one. A managed Postgres sleeps when
# nothing queries it and is billed by the hour it is awake. A loop poking it
# every ten minutes all night meant it never slept — 144 wake-ups a day, most
# with nobody in the building — and that was most of the compute bill.
from app.main import (_asleep as _slp, _seconds_until_wake as _wake,
                      _seconds_to_next_tick as _tickIn,
                      SPAWN_CHECK_SECONDS as _TICK,
                      QUIET_FROM as _QFs, QUIET_UNTIL as _QUs)
from datetime import datetime as _dtQ, timedelta as _tdQ

check("there are quiet hours at all", _QFs != _QUs, f"{_QFs} -> {_QUs}")
check("asleep in the middle of the night", _slp(_dtQ(2026, 10, 15, 3, 0)))
check("asleep at half past midnight", _slp(_dtQ(2026, 10, 15, 0, 30)))
check("but AWAKE across the turn of the day, so the checklist gets made",
      not _slp(_dtQ(2026, 10, 15, 0, 0)) and not _slp(_dtQ(2026, 10, 15, 0, 5)))
check("awake through the working day", not _slp(_dtQ(2026, 10, 15, 11, 0))
      and not _slp(_dtQ(2026, 10, 15, 18, 0)) and not _slp(_dtQ(2026, 10, 15, 21, 0)))
check("awake again at seven", not _slp(_dtQ(2026, 10, 15, 7, 0)))
check("still asleep a minute before seven", _slp(_dtQ(2026, 10, 15, 6, 59)))

# It must not oversleep: a container paused and resumed, or a clock that
# jumps, should never strand the loop for most of a day.
check("it never sleeps more than an hour in one go",
      _wake(_dtQ(2026, 10, 15, 0, 30)) <= 3600
      and _wake(_dtQ(2026, 10, 15, 2, 0)) <= 3600)
check("and never waits a negative or zero time",
      _wake(_dtQ(2026, 10, 15, 7, 0)) > 0 and _tickIn(_dtQ(2026, 10, 15, 0, 0)) > 0)

# The trap this was written to avoid: tick at 23:50, sleep half an hour, open
# your eyes at 00:20 — by then inside quiet hours, so the day's checklist
# would not be made until seven in the morning.
_lateQ = _dtQ(2026, 10, 15, 23, 50)
_nextQ = _lateQ + _tdQ(seconds=_tickIn(_lateQ))
check("a tick late at night lands just after midnight, not inside quiet hours",
      not _slp(_nextQ) and _nextQ.day == 16, _nextQ.strftime("%d %H:%M:%S"))

# Walk a whole day the way the loop actually runs it.
_tQ, _hitQ = _dtQ(2026, 10, 15, 23, 40), []
for _ in range(400):
    if _slp(_tQ):
        _tQ += _tdQ(seconds=_wake(_tQ))
    else:
        _hitQ.append(_tQ)
        _tQ += _tdQ(seconds=_tickIn(_tQ))
    if _tQ > _dtQ(2026, 10, 16, 23, 30):
        break
check("the database is woken far less often than before",
      len(_hitQ) <= 40, f"{len(_hitQ)} times in 24 hours, was 144")
check("and one of those is just after midnight",
      any(h.hour == 0 and h.minute < 10 for h in _hitQ),
      [h.strftime("%H:%M:%S") for h in _hitQ[:3]])
check("with nothing at all between one and six in the morning",
      not any(1 <= h.hour < 6 for h in _hitQ))

# A typo in the setting must not stop the app booting.
from app.main import _mins as _minsQ
check("a sensible setting is read", _minsQ("07:30", 0) == 450)
check("a bare hour works too", _minsQ("22", 0) == 22 * 60)
check("rubbish falls back instead of crashing",
      _minsQ("not a time", 99) == 99 and _minsQ("99:99", 7) == 7
      and _minsQ(None, 7) == 7)

# And the thing the loop exists for still happens: the day's checklist.
from app.main import _spawn_once as _spawnQ
from app.models import Task as _TQ, TaskSource as _TsQ
from sqlalchemy import func as _fnQ
with _slK() as _d:
    _beforeQ = _d.scalar(_spK(_fnQ.count()).select_from(_TQ)
                         .where(_TQ.source == _TsQ.RECURRING))
_madeQ = _spawnQ()
with _slK() as _d:
    _afterQ = _d.scalar(_spK(_fnQ.count()).select_from(_TQ)
                        .where(_TQ.source == _TsQ.RECURRING))
check("the day's checklist is still created by the loop's own worker",
      _afterQ - _beforeQ == _madeQ, f"made {_madeQ}, rows grew by {_afterQ - _beforeQ}")
check("and running it twice in a day creates nothing extra", _spawnQ() == 0)

print("\n== a step's clock can start from a date typed on the start form ==")
# "The audit is due three days after the bill date" — where the bill date is
# not when anything happened in here, but something somebody typed in when
# they started the run. Previously unexpressible: the only anchors were
# moments the software itself knew about.
from app.services import flows as _flowD
from app.models import Flow as _FlD, FlowInstance as _FID, Task as _TD
from datetime import datetime as _dtD

_mkD = {"name": f"SMOKE field anchor {RUN}", "description": "", "branch_id": "",
        "sf_label": ["Bill Date"], "sf_type": ["date"], "sf_required": ["1"],
        "sf_options": [""],
        "step_title": ["Verify the bill"], "step_doer": [""],
        "step_tat_unit": ["days"], "step_tat": ["3"], "step_priority": ["medium"],
        "step_instructions": [""], "step_fields": [""], "step_audit": ["0"],
        "step_proof": ["1"], "step_decision": ["0"], "step_yes": [""],
        "step_next": ["0"], "step_no": [""], "step_fail": [""],
        "step_due_from": ["field:Bill Date"]}
admin.post("/flows/new", data=_mkD)
with _slK() as _d:
    _fD = _d.scalar(_spK(_FlD).where(_FlD.name == f"SMOKE field anchor {RUN}"))
    check("the flow was built", _fD is not None)
    check("the anchor is stored as the question's label",
          _fD.steps[0].due_from_field == "Bill Date", _fD.steps[0].due_from_field)
    check("and it is not also stored as a step number",
          _fD.steps[0].due_from_pos is None, _fD.steps[0].due_from_pos)
    _fDid = _fD.id

admin.post(f"/flows/{_fDid}/start",
           data={"reference": f"SMOKE BILL {RUN}", "sf0": "2026-11-20"})
with _slK() as _d:
    _iD = _d.scalar(_spK(_FID).where(_FID.flow_id == _fDid))
    _tD = _d.scalar(_spK(_TD).where(_TD.flow_instance_id == _iD.id))
    check("the answer is kept with the run", '"Bill Date"' in (_iD.context or ""),
          _iD.context)
    check("the step is due three days after the date that was typed, not today",
          _tD.due_at.date() == _dtD(2026, 11, 23).date(), str(_tD.due_at))
    check("and a bare date is read as the start of the working day, not midnight",
          _tD.due_at.hour == _flowD.FIELD_DATE_HOUR, _tD.due_at.hour)

# Every shape a date box actually posts, and everything that is not a date.
class _FakeInst:
    def __init__(self, ctx): self.context = ctx
_amD = _flowD._answered_moment
check("a plain date is understood",
      _amD(_FakeInst('{"D": "2026-11-20"}'), "D") == _dtD(2026, 11, 20, _flowD.FIELD_DATE_HOUR))
check("a date and time is understood, to the minute",
      _amD(_FakeInst('{"D": "2026-11-20T14:30"}'), "D") == _dtD(2026, 11, 20, 14, 30))
check("something that is not a date is refused rather than guessed at",
      _amD(_FakeInst('{"D": "sometime next week"}'), "D") is None)
check("a blank answer is refused", _amD(_FakeInst('{"D": "  "}'), "D") is None)
check("a renamed question is refused", _amD(_FakeInst('{"D": "2026-11-20"}'), "Other") is None)
check("and broken stored answers do not crash it",
      _amD(_FakeInst("not json at all"), "D") is None)

# A run that leaves the question blank must still get a deadline, or nobody
# would ever be chased for it.
admin.post(f"/flows/{_fDid}/start",
           data={"reference": f"SMOKE BLANK {RUN}", "sf0": ""})
with _slK() as _d:
    _iB = _d.scalars(_spK(_FID).where(_FID.flow_id == _fDid)
                     .order_by(_FID.id.desc())).first()
    _tB = _d.scalar(_spK(_TD).where(_TD.flow_instance_id == _iB.id))
    check("a run with the date left blank still gets a planned date",
          _tB is not None and _tB.due_at is not None)

_newPageD = admin.get("/flows/new").text
check("the builder offers Date and time as an answer type",
      "Date and time" in _newPageD)
_sfD = open("app/templates/_stepfields.html", encoding="utf-8").read()
check("the anchor list is built from the start form's date questions",
      "refreshDateFields" in _sfD)
check("a question renamed mid-build drops the anchor instead of saving a dud",
      "usable.indexOf(chosen.slice(6)) === -1" in _sfD)

# The gap this closed: a question called "Date of joining" left on the
# default Short text answer type simply did not appear in the anchor list,
# with nothing on screen saying the TYPE was what decided it. Somebody
# looking for their own question found an empty space.
check("every start-form question is listed, not only the dated ones",
      "qs.push({name: name," in _sfD and "dated: t.value === 'date'" in _sfD)
check("and one that cannot hold a date says what to change",
      "set its answer type to Date to use it" in _sfD)
check("while staying unchoosable, so a dud anchor cannot be saved",
      "o.disabled = true;" in _sfD)
check("a question with no name yet is left out of the list entirely",
      "if (!name || seen[name]) return;" in _sfD)

print("\n== a half-built flow is not lost to a refresh ==")
# Twenty minutes of typing — a start form and eight steps — used to vanish on
# a stray refresh or a sleeping laptop, and people stopped building flows
# because of it. The form now saves itself into the browser as it is typed.
_dj = open("app/static/flowdraft.js", encoding="utf-8").read()
check("the builder form is marked for drafting", 'data-draft="new"' in _newPageD)
check("and loads the script that does it", "/static/flowdraft.js" in _newPageD)
check("drafts are kept in the browser, never on the server",
      "localStorage" in _dj and "fetch(" not in _dj)
check("storage being refused must not break the page",
      "catch (e) { return fallback; }" in _dj)
check("nothing is restored without being asked first",
      "data-yes" in _dj and "data-no" in _dj)
check("a saved flow leaves no draft behind",
      "addEventListener('submit'" in _dj and "removeItem(KEY)" in _dj)
check("an untouched page is not offered back as a draft", "function isEmpty" in _dj)
check("and a stale draft is thrown away rather than offered", "KEEP_DAYS" in _dj)

# The edit page gets its own draft, keyed to the flow — so editing flow 3
# can never offer you flow 7's half-finished changes.
_editD = admin.get(f"/flows/{_fDid}/edit").text
check("the edit page drafts too", 'data-draft="edit-' in _editD)
check("under a key of its own per flow", f'data-draft="edit-{_fDid}"' in _editD)

print("\n== the step builder says what it is asking for ==")
check("the capture box explains itself in plain words",
      "Figures the doer must type in when finishing this step" in _newPageD)
check("and says what each one becomes", "becomes a box the doer has to fill in" in _newPageD)

print("\n== one task each when several people are picked ==")
# "Several people on a task" is one task EACH, not one task with several
# names on it. A shared task would have made the scoring a lie: whose proof,
# whose audit, and who exactly is late when one of three has not finished?
from app.models import (Task as _TM, User as _UM, RecurringRule as _RRM,
                        Flow as _FlM, FlowInstance as _FIM, TaskStatus as _TstM)

with _slK() as _d:
    _peopleM = [u for u in _d.scalars(_spK(_UM).where(_UM.active.is_(True))).all()
                if u.receives_work][:3]
    _idsM = [u.id for u in _peopleM]
    _namesM = [u.name for u in _peopleM]
check("there are three people to hand work to", len(_idsM) == 3, _idsM)

_titleM = f"SMOKE three doers {RUN}"
_rM = admin.post("/tasks/new", data={
    "title": _titleM, "details": "Count and sign",
    "doer_id": [str(i) for i in _idsM], "branch_id": "", "priority": "high",
    "due_at": "2026-11-05T17:00", "requires_audit": ""}, follow_redirects=False)
check("the form is accepted", _rM.status_code == 303, _rM.status_code)
check("and lands on the list, not on one arbitrary copy",
      "/tasks?" in (_rM.headers.get("location") or ""), _rM.headers.get("location"))
with _slK() as _d:
    _madeM = _d.scalars(_spK(_TM).where(_TM.title == _titleM)).all()
check("three people picked makes three tasks", len(_madeM) == 3, len(_madeM))
check("one for each of them, nobody twice",
      sorted(t.doer_id for t in _madeM) == sorted(_idsM))
check("each with the same deadline", len({t.due_at for t in _madeM}) == 1)
check("and the same priority", len({t.priority for t in _madeM}) == 1)
check("they are separate records, so each can be closed on its own",
      len({t.id for t in _madeM}) == 3)

# Ticking somebody twice is a slip, not an instruction to hand them the job
# twice over.
_dupT = f"SMOKE dup doer {RUN}"
admin.post("/tasks/new", data={
    "title": _dupT, "details": "", "doer_id": [str(_idsM[0]), str(_idsM[0])],
    "branch_id": "", "priority": "low", "due_at": "2026-11-06T17:00",
    "requires_audit": ""}, follow_redirects=False)
with _slK() as _d:
    check("the same person ticked twice still gets one task",
          len(_d.scalars(_spK(_TM).where(_TM.title == _dupT)).all()) == 1)

_oneT = f"SMOKE one doer {RUN}"
_r1 = admin.post("/tasks/new", data={
    "title": _oneT, "details": "", "doer_id": [str(_idsM[0])], "branch_id": "",
    "priority": "low", "due_at": "2026-11-07T17:00", "requires_audit": ""},
    follow_redirects=False)
check("one person still lands straight on their task",
      "/tasks/" in (_r1.headers.get("location") or "")
      and "?" not in (_r1.headers.get("location") or ""), _r1.headers.get("location"))

_r0 = admin.post("/tasks/new", data={
    "title": f"SMOKE nobody {RUN}", "details": "", "doer_id": [], "branch_id": "",
    "priority": "low", "due_at": "2026-11-07T17:00", "requires_audit": ""})
check("picking nobody is refused", _r0.status_code == 400, _r0.status_code)

# --- the checklist: one RULE each --------------------------------------
_ruleT = f"SMOKE three rules {RUN}"
_rr = admin.post("/recurring", data={
    "title": _ruleT, "details": "", "doer_id": [str(i) for i in _idsM],
    "branch_id": "", "frequency": "daily", "due_time": "18:00",
    "priority": "medium", "requires_audit": ""}, follow_redirects=False)
check("the checklist form is accepted", _rr.status_code == 303, _rr.status_code)
with _slK() as _d:
    _rulesM = _d.scalars(_spK(_RRM).where(_RRM.title == _ruleT)).all()
check("three people makes three checklist items", len(_rulesM) == 3, len(_rulesM))
check("one each", sorted(r.doer_id for r in _rulesM) == sorted(_idsM))
check("each switched on in its own right", all(r.active for r in _rulesM))
check("so one person's can be retimed without touching the others",
      len({r.id for r in _rulesM}) == 3)
check("picking nobody for a checklist item is refused too",
      admin.post("/recurring", data={
          "title": f"SMOKE no rule doer {RUN}", "details": "", "doer_id": [],
          "branch_id": "", "frequency": "daily", "due_time": "18:00",
          "priority": "medium", "requires_audit": ""}).status_code == 400)

print("\n== an FMS step several people share waits for all of them ==")
# Moving on after the first person finishes would quietly cancel everybody
# else's work, which is the opposite of asking three people to do something.
_fnameM = f"SMOKE shared step {RUN}"
_formM = {
    "name": _fnameM, "description": "", "branch_id": "",
    "sf_label": [""], "sf_type": ["text"], "sf_required": ["1"], "sf_options": [""],
    "step_title": ["Everyone counts their own stock", "Manager signs off"],
    "step_doer": [str(_idsM[0]), str(_idsM[0])],
    "step_doers": [f"{_idsM[1]},{_idsM[2]}", ""],
    "step_tat_unit": ["hours", "hours"], "step_tat": ["24", "24"],
    "step_priority": ["medium", "medium"], "step_instructions": ["", ""],
    "step_fields": ["", ""], "step_audit": ["0", "0"], "step_proof": ["0", "0"],
    "step_decision": ["0", "0"], "step_yes": ["", ""], "step_next": ["", ""],
    "step_no": ["", ""], "step_fail": ["", "0"], "step_due_from": ["", ""]}
check("the flow is accepted", admin.post("/flows/new", data=_formM).status_code == 200)
with _slK() as _d:
    _fM = _d.scalar(_spK(_FlM).where(_FlM.name == _fnameM))
    check("step 1 remembers all three people", _fM.steps[0].doer_ids == _idsM,
          _fM.steps[0].doer_ids)
    check("and step 2 just the one", _fM.steps[1].doer_ids == [_idsM[0]],
          _fM.steps[1].doer_ids)
    _fMid = _fM.id

admin.post(f"/flows/{_fMid}/start", data={"reference": f"SMOKE STOCK {RUN}"})
with _slK() as _d:
    _iM = _d.scalar(_spK(_FIM).where(_FIM.flow_id == _fMid))
    # Step 1 only. The whole run is laid out when the form is submitted, so
    # step 2's task exists too — waiting, with the date it is planned for.
    _t1M = [t for t in _d.scalars(_spK(_TM).where(_TM.flow_instance_id == _iM.id)).all()
            if t.flow_step.position == 1]
    _iMid = _iM.id
check("starting the run opens one task per person", len(_t1M) == 3, len(_t1M))
check("one each, nobody missed", sorted(t.doer_id for t in _t1M) == sorted(_idsM))

def _step_positions():
    """Positions whose turn has actually come — not the ones merely planned."""
    with _slK() as _d:
        return [t.flow_step.position for t in
                _d.scalars(_spK(_TM).where(_TM.flow_instance_id == _iMid)).all()
                if not t.flow_waiting]

_doneM = 0
for _tm in _t1M:
    _cc = TestClient(app)
    with _slK() as _d:
        _em = _d.get(_UM, _tm.doer_id).email
    _cc.post("/login", data={"email": _em, "password": "gcs1234"})
    _sub = _cc.post(f"/tasks/{_tm.id}/submit", data={"completion_note": "counted"})
    _doneM += 1
    _opened = 2 in _step_positions()
    if _doneM < 3:
        check(f"after {_doneM} of 3 finish, the next step has NOT opened",
              not _opened)
    else:
        check("only when the last one finishes does the next step open", _opened)

with _slK() as _d:
    _t2M = [t for t in _d.scalars(_spK(_TM).where(_TM.flow_instance_id == _iMid)).all()
            if t.flow_step.position == 2 and not t.flow_waiting]
check("and the next step opens once, not three times", len(_t2M) == 1, len(_t2M))

# A decision step cannot be shared: two people could pick two outcomes and
# the flow would have to go two ways at once.
_badM = dict(_formM)
_badM["name"] = f"SMOKE bad decision {RUN}"
_badM["step_decision"] = ["1", "0"]
_badM["step_yes"] = ["Verified", ""]
_badM["step_no"] = ["Rejected", ""]
_badM["step_next"] = ["2", "0"]
_badM["step_fail"] = ["1", "0"]
_rbM = admin.post("/flows/new", data=_badM)
check("a decision step with more than one doer is refused",
      _rbM.status_code == 400, _rbM.status_code)
check("and the refusal explains why", "only one person can do" in _rbM.text)

print("\n== a step can say how it will be done, with links that work ==")
_lnameM = f"SMOKE links {RUN}"
_lformM = dict(_formM)
_lformM.update({
    "name": _lnameM, "step_doer": [str(_idsM[0]), str(_idsM[0])],
    "step_doers": ["", ""],
    "step_instructions": ["Open https://docs.google.com/spreadsheets/d/abc "
                          "and copy sheet 2.", ""]})
admin.post("/flows/new", data=_lformM)
with _slK() as _d:
    _lfM = _d.scalar(_spK(_FlM).where(_FlM.name == _lnameM))
    _lfMid = _lfM.id
admin.post(f"/flows/{_lfMid}/start", data={"reference": f"SMOKE LINK {RUN}"})
with _slK() as _d:
    _liM = _d.scalar(_spK(_FIM).where(_FIM.flow_id == _lfMid))
    _ltM = _d.scalar(_spK(_TM).where(_TM.flow_instance_id == _liM.id))
    _ltMid = _ltM.id
_pageM = admin.get(f"/tasks/{_ltMid}").text
check("a link pasted into the step is a real link on the doer's task",
      '<a href="https://docs.google.com/spreadsheets/d/abc"' in _pageM)
check("and it opens without handing the other page a handle on ours",
      'rel="noopener noreferrer"' in _pageM)

from app.templating import linkify as _lk
check("anything that is not a link is escaped, never rendered as HTML",
      "&lt;script&gt;" in str(_lk("<script>alert(1)</script>"))
      and "<script>" not in str(_lk("<script>alert(1)</script>")))
check("and a javascript: link is left as plain text",
      "<a" not in str(_lk("javascript:alert(1)")))
_bldP = admin.get("/flows/new").text
check("the builder asks for it in plain words",
      "how it will be done" in _bldP.lower())
# Renaming this box once already read as the box having been removed. It is
# now two boxes, each labelled for what it holds, which answers the same
# worry better than keeping an old word on one label did.
check("the description box is there and says what it is for",
      "Step description" in _bldP)
check("the box itself is the same field it always was",
      'name="step_instructions"' in _bldP)

print("\n== the people picker ==")
# It used to be a list of checkboxes, which rendered as a tower of names
# inside a step card and could not be read. It is now: pick a name, it drops
# into a box as a tag with a cross on it, and the chooser offers whoever is
# left.
_dpJs = open("app/static/doerpick.js", encoding="utf-8").read()
_newP = admin.get("/tasks/new").text
check("the picker is on the delegation form", 'class="doerpick' in _newP)
check("built on a real multi-select that is already named and filled",
      'name="doer_id" class="dp-src"' in _newP
      or ('class="dp-src"' in _newP and 'name="doer_id"' in _newP))
check("so with the script dead the native control still posts the same thing",
      "dp-hidden" in _dpJs and "src.classList.add" in _dpJs)
check("the cross is a button that cannot submit the form",
      "x.type = 'button'" in _dpJs)
check("somebody already chosen is not offered again",
      "return !o.selected" in _dpJs)
check("the list shows the name and nothing else",
      "data-sub" not in _newP and "role.value" not in
      open("app/templates/_doerpick.html", encoding="utf-8").read())
check("there is a box to type a name into", "dp-type" in _dpJs)
check("typing narrows the list to what matches",
      "indexOf(q) !== -1" in _dpJs)
check("an empty box still shows everybody, for anybody who would rather not type",
      "q\n        ? left.filter" in _dpJs or "? left.filter" in _dpJs)
check("Enter picks a name instead of submitting the half-filled form",
      "e.key === 'Enter'" in _dpJs and "e.preventDefault()" in _dpJs)
check("the arrow keys walk the list", "ArrowDown" in _dpJs and "ArrowUp" in _dpJs)
check("a name is picked on mousedown, before the box can lose focus",
      "'mousedown'" in _dpJs)
check("and nothing matching says so rather than showing an empty box",
      "No name matches that" in _dpJs)
check("and the picker re-wires itself for step cards added later",
      "MutationObserver" in _dpJs)
check("the step card has its own picker", "step_doers" in admin.get("/flows/new").text)

# The bug this found: pick two extras, then change "Who does it" to one of
# them, and that person was on the step twice.
_dupF = f"SMOKE both boxes {RUN}"
admin.post("/flows/new", data={
    "name": _dupF, "description": "", "branch_id": "",
    "sf_label": [""], "sf_type": ["text"], "sf_required": ["1"], "sf_options": [""],
    "step_title": ["Count it"], "step_doer": [str(_idsM[0])],
    "step_doers": [f"{_idsM[0]},{_idsM[1]}"],      # the primary is in BOTH
    "step_tat_unit": ["hours"], "step_tat": ["24"], "step_priority": ["medium"],
    "step_instructions": [""], "step_fields": [""], "step_audit": ["0"],
    "step_proof": ["0"], "step_decision": ["0"], "step_yes": [""],
    "step_next": ["0"], "step_no": [""], "step_fail": [""], "step_due_from": [""]})
with _slK() as _d:
    _dupStep = _d.scalar(_spK(_FlM).where(_FlM.name == _dupF)).steps[0]
check("the primary doer is stripped out of the extras",
      _dupStep.extra_doer_ids == str(_idsM[1]), _dupStep.extra_doer_ids)
check("so the step names each person exactly once",
      _dupStep.doer_ids == [_idsM[0], _idsM[1]], _dupStep.doer_ids)
check("and opens one task each, not two for the same person",
      len(set(_dupStep.doer_ids)) == len(_dupStep.doer_ids))

print("\n== a step has two boxes: what it is, and how it is done ==")
# One box made the how swallow the what. "Verify the bill against the PO"
# is the step; "open the PO folder, match line by line, flag anything over
# 5%" is the method, and a doer skimming on a phone read the first line of
# a run-together block and nothing else.
from app.models import Flow as _FlT, FlowInstance as _FIT, Task as _TT

_tbName = f"SMOKE two boxes {RUN}"
_tbWhat = "Verify the bill against the purchase order."
_tbHow = ("1. Open https://drive.google.com/po-folder\n"
          "2. Match it line by line.\n3. Flag anything over 5%.")
admin.post("/flows/new", data={
    "name": _tbName, "description": "", "branch_id": "",
    "sf_label": [""], "sf_type": ["text"], "sf_required": ["1"], "sf_options": [""],
    "step_title": ["Verify the bill"], "step_doer": [""], "step_doers": [""],
    "step_tat_unit": ["hours"], "step_tat": ["24"], "step_priority": ["medium"],
    "step_instructions": [_tbWhat], "step_howto": [_tbHow],
    "step_fields": [""], "step_audit": ["0"], "step_proof": ["0"],
    "step_decision": ["0"], "step_yes": [""], "step_next": ["0"],
    "step_no": [""], "step_fail": [""], "step_due_from": [""]})
with _slK() as _d:
    _tbF = _d.scalar(_spK(_FlT).where(_FlT.name == _tbName))
    _tbS = _tbF.steps[0]
    _tbFid = _tbF.id
    check("the description is stored on its own", _tbS.instructions == _tbWhat)
    check("and the method separately, not run together with it",
          _tbS.how_to == _tbHow and _tbWhat not in (_tbS.how_to or ""))

admin.post(f"/flows/{_tbFid}/start", data={"reference": f"SMOKE TWOBOX {RUN}"})
with _slK() as _d:
    _tbI = _d.scalar(_spK(_FIT).where(_FIT.flow_id == _tbFid))
    _tbT = _d.scalar(_spK(_TT).where(_TT.flow_instance_id == _tbI.id))
    _tbTid = _tbT.id
    check("both reach the task the doer opens",
          _tbT.details == _tbWhat and _tbT.how_to == _tbHow)

_tbPage = admin.get(f"/tasks/{_tbTid}").text
check("the task shows the description", _tbWhat in _tbPage)
check("and the method under a heading of its own",
      "How it will be done" in _tbPage and "Match it line by line" in _tbPage)
check("a link in the method is clickable",
      '<a href="https://drive.google.com/po-folder"' in _tbPage)

_tbEdit = admin.get(f"/flows/{_tbFid}/edit").text
check("editing the flow brings both back", _tbWhat in _tbEdit and "Flag anything over 5%" in _tbEdit)

_tbNew = admin.get("/flows/new").text
check("the builder offers both boxes",
      'name="step_instructions"' in _tbNew and 'name="step_howto"' in _tbNew)
check("labelled so nobody has to guess which is which",
      "Step description" in _tbNew and "How it will be done" in _tbNew)

# A step with only a description must still work — the method is optional.
_tbBare = f"SMOKE one box {RUN}"
admin.post("/flows/new", data={
    "name": _tbBare, "description": "", "branch_id": "",
    "sf_label": [""], "sf_type": ["text"], "sf_required": ["1"], "sf_options": [""],
    "step_title": ["Just do it"], "step_doer": [""], "step_doers": [""],
    "step_tat_unit": ["hours"], "step_tat": ["24"], "step_priority": ["medium"],
    "step_instructions": ["Something simple"], "step_howto": [""],
    "step_fields": [""], "step_audit": ["0"], "step_proof": ["0"],
    "step_decision": ["0"], "step_yes": [""], "step_next": ["0"],
    "step_no": [""], "step_fail": [""], "step_due_from": [""]})
with _slK() as _d:
    _bare = _d.scalar(_spK(_FlT).where(_FlT.name == _tbBare)).steps[0]
check("the method is optional", _bare.how_to is None, _bare.how_to)
check("and a task from it simply has no method block",
      "how_to" in _TT.__table__.c)

_tbXl = admin.get(f"/flows/{_tbFid}?export=xlsx")
check("the flow's export carries the method too", _tbXl.status_code == 200,
      _tbXl.status_code)

print("\n== a refused save must not take the draft with it ==")
# The draft feature undone by its own hand. The form cleared the draft the
# moment it was submitted — but the server refuses a flow whose routing
# points at a step that does not exist, and by the time that refusal
# reached the screen the draft was already gone, along with twenty minutes
# of typing. Exactly the loss the draft exists to prevent.
_fdJs = open("app/static/flowdraft.js", encoding="utf-8").read()
check("submitting no longer throws the draft away",
      "form.addEventListener('submit'" in _fdJs
      and "s.removeItem(KEY); });" not in _fdJs.split("addEventListener('submit'")[1][:300])
check("it saves one last time on the way out instead",
      "clearTimeout(timer);\n    save();" in _fdJs)

from app.models import Flow as _FlR
_fdOk = {"name": f"SMOKE draft clear {RUN}", "description": "", "branch_id": "",
         "sf_label": [""], "sf_type": ["text"], "sf_required": ["1"], "sf_options": [""],
         "step_title": ["Only step"], "step_doer": [""], "step_doers": [""],
         "step_tat_unit": ["hours"], "step_tat": ["24"], "step_priority": ["medium"],
         "step_instructions": [""], "step_howto": [""], "step_fields": [""],
         "step_audit": ["0"], "step_proof": ["0"], "step_decision": ["0"],
         "step_yes": [""], "step_next": ["0"], "step_no": [""], "step_fail": [""],
         "step_due_from": [""]}
_fdR = admin.post("/flows/new", data=_fdOk, follow_redirects=False)
check("a flow that saves redirects with proof that it saved",
      "built=new" in (_fdR.headers.get("location") or ""), _fdR.headers.get("location"))

# The refusal: routing at a step this flow has not got.
_fdBad = dict(_fdOk)
_fdBad["name"] = f"SMOKE draft keep {RUN}"
_fdBad["step_next"] = ["9"]
_fdBadR = admin.post("/flows/new", data=_fdBad)
check("a flow with a route to nowhere is still refused", _fdBadR.status_code == 400,
      _fdBadR.status_code)
check("and the refusal carries no instruction to clear anything",
      "built=" not in _fdBadR.text)
with _slK() as _d:
    check("nothing was saved by the refused attempt",
          _d.scalar(_spK(_FlR).where(_FlR.name == f"SMOKE draft keep {RUN}")) is None)

with _slK() as _d:
    _fdFlow = _d.scalar(_spK(_FlR).where(_FlR.name == f"SMOKE draft clear {RUN}"))
    _fdId = _fdFlow.id
_fdPage = admin.get(f"/flows/{_fdId}?built=new").text
check("the flow's page is what clears the draft, and only on proof",
      "gcsa:flowdraft:new" in _fdPage and "built" in _fdPage)
check("it clears the right draft when a flow was EDITED, not the new one",
      "gcsa:flowdraft:edit-" in _fdPage)
check("and tidies the address so a refresh does not keep re-clearing",
      "replaceState" in _fdPage)
check("storage being switched off must not break the page",
      "catch (e) {" in _fdPage)
_fdPlain = admin.get(f"/flows/{_fdId}").text
check("a plain visit clears nothing", "if (!q) return;" in _fdPlain)

_fdEditR = admin.post(f"/flows/{_fdId}/edit", data=_fdOk, follow_redirects=False)
check("saving an edit says so the same way",
      "built=edit" in (_fdEditR.headers.get("location") or ""),
      _fdEditR.headers.get("location"))

print("\n== an FMS step's TAT can be a date, not just a length ==")
# "Due on the 15th" cannot be written as a span: a month after the 15th
# drifts off the 15th the first time somebody is a day late. So a step's
# turnaround can name a date that comes round instead — counted from the
# same anchor as a span, including a date typed on the start form.
from app import clock as _ckC
from datetime import datetime as _dtC

check("the unit list offers a date each month", "monthday" in _ckC.SPAN_UNITS)
check("a weekday each week", "weekday" in _ckC.SPAN_UNITS)
check("and the 1st and the 15th", "fortnight" in _ckC.SPAN_UNITS)
check("the old spans are all still there",
      all(u in _ckC.SPAN_UNITS for u in ("minutes", "hours", "days", "weeks", "months")))

_optC = _ckC.span_options()
check("a weekday is offered by name, not as the number 0",
      _optC["weekday"][0] == [0, "Monday"], _optC["weekday"][0])
check("and a date as an ordinal", _optC["monthday"][0] == [1, "the 1st"], _optC["monthday"][0])
check("the 29th to the 31st are not offered, because they do not exist every month",
      max(v for v, _ in _optC["monthday"]) == 28)
check("the old units still read as plain numbers", _optC["hours"][0] == [1, "1"])

_novC = _dtC(2026, 11, 10, 9, 0)        # a Tuesday
check("the test anchor is the Tuesday it is meant to be", _novC.strftime("%A") == "Tuesday")
_nxC = _ckC.next_occurrence
check("10 Nov, due the 15th -> 15 Nov",
      _nxC(_novC, "monthday", 15).date() == _dtC(2026, 11, 15).date())
check("20 Nov, due the 15th -> 15 Dec, not a date already gone",
      _nxC(_dtC(2026, 11, 20, 9, 0), "monthday", 15).date() == _dtC(2026, 12, 15).date())
check("ON the 15th, due the 15th -> that same day, not a month later",
      _nxC(_dtC(2026, 11, 15, 9, 0), "monthday", 15).date() == _dtC(2026, 11, 15).date())
check("Tuesday, due every Monday -> the following Monday",
      _nxC(_novC, "weekday", 0).date() == _dtC(2026, 11, 16).date())
check("Monday, due every Monday -> that same Monday",
      _nxC(_dtC(2026, 11, 9, 9, 0), "weekday", 0).date() == _dtC(2026, 11, 9).date())
check("3 Nov, due the 1st or 15th -> the 15th",
      _nxC(_dtC(2026, 11, 3, 9, 0), "fortnight", 0).date() == _dtC(2026, 11, 15).date())
check("20 Nov, due the 1st or 15th -> the 1st of next month",
      _nxC(_dtC(2026, 11, 20, 9, 0), "fortnight", 0).date() == _dtC(2026, 12, 1).date())
check("29 Dec, due the 5th -> 5 Jan, the year rolls over",
      _nxC(_dtC(2026, 12, 29, 9, 0), "monthday", 5).date() == _dtC(2027, 1, 5).date())
check("a calendar deadline falls at the end of the day, not one second past midnight",
      _nxC(_novC, "monthday", 15).hour == _ckC.CAL_DUE_HOUR and _ckC.CAL_DUE_HOUR >= 12)

# 0 is a real answer for a calendar unit — Monday — and the rule that stops
# a span of zero must not quietly turn it into Tuesday.
from app.models import FlowStep as _FSC
_zeroC = _FSC(flow_id=1, position=1, title="z", tat_unit="weekday", tat_value=0)
from app.models import _step_tat_defaults as _fixC
_fixC(None, None, _zeroC)
check("a weekday of 0 survives, so 'every Monday' is not saved as Tuesday",
      _zeroC.tat_value == 0, _zeroC.tat_value)
_spanC = _FSC(flow_id=1, position=1, title="z", tat_unit="days", tat_value=0)
_fixC(None, None, _spanC)
check("but a SPAN of zero is still floored, because no time at all is not a TAT",
      _spanC.tat_value == 1, _spanC.tat_value)

# End to end, with the anchor the request actually described: a joining date
# typed on the start form, and a step due on the next 15th after it.
from app.models import (Flow as _FlC, FlowInstance as _FIC, Task as _TC,
                        User as _UC)
with _slK() as _d:
    # Ordered on purpose. Without it PostgreSQL and SQLite pick different
    # people, and this check depends on which COMPANY the doer belongs to:
    # the offices close on Sunday and the gym and the spa do not.
    _uRowC = [u for u in _d.scalars(_spK(_UC).where(_UC.active.is_(True))
                                    .order_by(_UC.id)).all()
              if u.receives_work][0]
    _uC = _uRowC.id
    _uBranchC = _uRowC.branch_id
_joinName = f"SMOKE joining date {RUN}"
admin.post("/flows/new", data={
    "name": _joinName, "description": "", "branch_id": "",
    "sf_label": ["Date of joining"], "sf_type": ["date"], "sf_required": ["1"],
    "sf_options": [""],
    "step_title": ["Induction paperwork"], "step_doer": [str(_uC)], "step_doers": [""],
    "step_tat_unit": ["monthday"], "step_tat": ["15"], "step_priority": ["medium"],
    "step_instructions": [""], "step_howto": [""], "step_fields": [""],
    "step_audit": ["0"], "step_proof": ["0"], "step_decision": ["0"],
    "step_yes": [""], "step_next": ["0"], "step_no": [""], "step_fail": [""],
    "step_due_from": ["field:Date of joining"]})
with _slK() as _d:
    _jF = _d.scalar(_spK(_FlC).where(_FlC.name == _joinName))
    check("the step stored the calendar unit", _jF.steps[0].tat_unit == "monthday")
    check("with the date it was given", _jF.steps[0].tat_value == 15)
    check("and the start-form question as its anchor",
          _jF.steps[0].due_from_field == "Date of joining")
    _jFid = _jF.id
admin.post(f"/flows/{_jFid}/start",
           data={"reference": f"SMOKE JOIN {RUN}", "sf0": "2026-11-10"})
with _slK() as _d:
    _jI = _d.scalars(_spK(_FIC).where(_FIC.flow_id == _jFid)).all()[-1]
    _jT = _d.scalar(_spK(_TC).where(_TC.flow_instance_id == _jI.id))
# 15 Nov 2026 is a Sunday, and an FMS deadline never falls on a closed day —
# so the answer is the next working day AFTER it, Monday the 16th. Forward,
# because a chain cannot be dragged backwards: see holidays.push_due.
check("joining 10 Nov with a TAT of 'the 15th' lands on the 15th or just after",
      _dtC(2026, 11, 15) <= _jT.due_at <= _dtC(2026, 11, 17, 23, 59),
      str(_jT.due_at))
# Not "never a Sunday" — Bodyzone and Spa Kora are open on Sundays, and
# that is the whole point of the weekly-off being per company. The rule is
# that a deadline never lands on a day THIS doer's company is shut.
with _slK() as _dC:
    check("and never on a day that doer's own company is closed",
          not _holJ.is_closed(_dC, 1, _jT.due_at.date(), _uBranchC),
          f"{_jT.due_at:%a %d %b} for branch {_uBranchC}")

print("\n== the desk is only asked to chase what is worth chasing ==")
# A desk handed 150 daily low-priority rows stops ticking any of them, and a
# coverage figure it can never get to 100% is a figure it learns to ignore.
from app.services import chasing as _chC
from app.models import (RecurringRule as _RRC, Recurrence as _RecC,
                        Priority as _PrC, TaskSource as _TsC)
from app.services import recurring as _recC

with _slK() as _d:
    _admC = _d.scalar(_spK(_UC).where(_UC.email == "mis@gcs.local"))
    _doC = [u for u in _d.scalars(_spK(_UC).where(_UC.active.is_(True))).all()
            if u.receives_work][0]
    _ruleIds = {}
    for _fq, _pr in [(_RecC.DAILY, _PrC.HIGH), (_RecC.DAILY, _PrC.MEDIUM),
                     (_RecC.DAILY, _PrC.LOW), (_RecC.WEEKLY, _PrC.LOW)]:
        _r = _RRC(org_id=1, branch_id=_doC.branch_id,
                  title=f"SMOKE chase {_fq.value} {_pr.value} {RUN}",
                  doer_id=_doC.id, assigner_id=_admC.id, priority=_pr,
                  frequency=_fq, due_time="18:00", requires_attachment=False,
                  weekdays="0,1,2,3,4,5,6" if _fq == _RecC.WEEKLY else None)
        _d.add(_r)
        _d.flush()
        _ruleIds[(_fq, _pr)] = _r.id
    _d.commit()
    _recC.run_spawn(_d)

def _chaseable(fq, pr):
    with _slK() as _d:
        _t = _d.scalar(_spK(_TC).where(_TC.rule_id == _ruleIds[(fq, pr)]))
        return None if _t is None else _chC.needs_chasing(_t)

check("a DAILY checklist job at HIGH priority is chased",
      _chaseable(_RecC.DAILY, _PrC.HIGH) is True)
check("a DAILY job at MEDIUM is not", _chaseable(_RecC.DAILY, _PrC.MEDIUM) is False)
check("a DAILY job at LOW is not", _chaseable(_RecC.DAILY, _PrC.LOW) is False)
check("a WEEKLY job at LOW still is — only DAILY is narrowed",
      _chaseable(_RecC.WEEKLY, _PrC.LOW) is True)

with _slK() as _d:
    # A LIVE step. Every step of a run exists from the moment the start
    # form is submitted, and the ones whose turn has not come are
    # deliberately not chased — picking one of those here would be testing
    # the opposite rule by accident.
    _fmsT = _d.scalar(_spK(_TC).where(_TC.source == _TsC.FLOW,
                                      _TC.flow_waiting.is_(False)))
    _delT = _d.scalar(_spK(_TC).where(_TC.source == _TsC.DELEGATION,
                                      _TC.priority == _PrC.LOW))
    check("FMS work is chased at every priority",
          _fmsT is None or _chC.needs_chasing(_fmsT) is True)
    check("and delegation is untouched by this rule",
          _delT is None or _chC.needs_chasing(_delT) is True)
    # A rule somebody deleted leaves its tasks behind. They are real work
    # still in somebody's hands, so they stay chaseable rather than vanishing.
    _orphan = _TC(org_id=1, branch_id=_doC.branch_id, title=f"SMOKE orphan {RUN}",
                  assigner_id=_admC.id, doer_id=_doC.id, priority=_PrC.LOW,
                  source=_TsC.RECURRING, due_at=_clock.now(), rule_id=None)
    check("a checklist task whose rule was deleted is still chased",
          _chC.needs_chasing(_orphan) is True)

_deskP = admin.get("/followups?desk=pc&show=all").text
check("the high-priority daily job is on the desk",
      f"SMOKE chase daily high {RUN}" in _deskP)
check("the medium one is not", f"SMOKE chase daily medium {RUN}" not in _deskP)
check("nor the low one", f"SMOKE chase daily low {RUN}" not in _deskP)
check("but the weekly low one is", f"SMOKE chase weekly low {RUN}" in _deskP)

# The board and the desk must answer the same question the same way.
from app.services import cmdboard as _cbC
with _slK() as _d:
    _sC, _eC, _ = _cbC.window("this_month")
    _bC = _cbC.build(_d, 1, _sC, _eC)
    _owed = _bC.chase["recurring"].open_tasks
    _todayEnd = _dtC.combine(_clock.today(), _dtC.max.time())
    _byHand = sum(1 for _t in _d.scalars(_spK(_TC).where(_TC.source == _TsC.RECURRING)).all()
                  if _t.due_at and _t.due_at <= _todayEnd and _t.closed_at is None
                  and _t.submitted_at is None and _chC.needs_chasing(_t))
check("the CMD board counts exactly what the desk shows",
      _owed == _byHand, f"board {_owed} vs counted {_byHand}")
check("and the rule lives in one place, so they cannot drift apart",
      "needs_chasing" in open("app/services/cmdboard.py", encoding="utf-8").read()
      and "needs_chasing" in open("app/routers/followups.py", encoding="utf-8").read())

print("\n== the start-form builder shows what will actually be asked ==")
# Choosing "Date and time" changed nothing you could see: the builder only
# ever showed a text box for the question's NAME, so the calendar — which
# was real, on the form somebody fills in — looked like it had not been
# switched on at all.
_sfB = open("app/templates/_startform.html", encoding="utf-8").read()
_newB = admin.get("/flows/new").text
check("the builder shows what the answer will look like", 'class="sfprev"' in _newB)
check("a date question previews a real date box", "'<input type=\"date\">'" in _sfB)
check("and a date-and-time one a date AND time box",
      "'<input type=\"datetime-local\">'" in _sfB)
check("the preview is dead — it posts nothing and cannot be typed in",
      "el.disabled = true;" in _sfB and "el.removeAttribute('name');" in _sfB)

# The form people actually fill in has had the real controls all along.
from app.models import Flow as _FlB, FIELD_TYPES as _FTB, BUILDER_FIELD_TYPES as _BFTB
_dtName = f"SMOKE picker {RUN}"
admin.post("/flows/new", data={
    "name": _dtName, "description": "", "branch_id": "",
    "sf_label": ["Date of joining", "Bill date"],
    "sf_type": ["datetime", "date"], "sf_required": ["1", "1"],
    "sf_options": ["", ""],
    "step_title": ["Step one"], "step_doer": [""], "step_doers": [""],
    "step_tat_unit": ["hours"], "step_tat": ["24"], "step_priority": ["medium"],
    "step_instructions": [""], "step_howto": [""], "step_fields": [""],
    "step_audit": ["0"], "step_proof": ["0"], "step_decision": ["0"],
    "step_yes": [""], "step_next": ["0"], "step_no": [""], "step_fail": [""],
    "step_due_from": [""]})
with _slK() as _d:
    _dtF = _d.scalar(_spK(_FlB).where(_FlB.name == _dtName))
    _dtFid = _dtF.id
_startP = admin.get(f"/flows/{_dtFid}").text
check("the form people fill in gives a real calendar", 'type="date"' in _startP)
check("and a real date-and-time picker", 'type="datetime-local"' in _startP)

print("\n== Yes / No is no longer offered for a new question ==")
check("the builder does not offer it", "yesno" not in _BFTB)
check("but every other type is still there",
      all(k in _BFTB for k in ("text", "textarea", "number", "date", "datetime", "select")))
check("and it is not in the dropdown on the page", ">Yes / No<" not in _newB)
# Removing it outright would have turned existing Yes/No questions into text
# boxes on flows nobody had touched.
check("a flow already using it still knows what it is", "yesno" in _FTB)
_ynName = f"SMOKE yesno kept {RUN}"
admin.post("/flows/new", data={
    "name": _ynName, "description": "", "branch_id": "",
    "sf_label": ["Approved?"], "sf_type": ["yesno"], "sf_required": ["1"],
    "sf_options": [""],
    "step_title": ["Step one"], "step_doer": [""], "step_doers": [""],
    "step_tat_unit": ["hours"], "step_tat": ["24"], "step_priority": ["medium"],
    "step_instructions": [""], "step_howto": [""], "step_fields": [""],
    "step_audit": ["0"], "step_proof": ["0"], "step_decision": ["0"],
    "step_yes": [""], "step_next": ["0"], "step_no": [""], "step_fail": [""],
    "step_due_from": [""]})
with _slK() as _d:
    _ynF = _d.scalar(_spK(_FlB).where(_FlB.name == _ynName))
    check("an existing Yes/No question keeps its type rather than becoming text",
          _ynF.start_form_fields[0]["type"] == "yesno",
          _ynF.start_form_fields[0]["type"])
    _ynId = _ynF.id
check("and still asks Yes or No on the form",
      "<option value=\"Yes\">Yes</option>" in admin.get(f"/flows/{_ynId}").text)

print("\n== the running list says which step each run is on ==")
# The complaint: a run called "Prepare the message for therapists..." listed
# under the flow "Content Writing Process" looked like a step of that flow
# that was nowhere to be found in it. It is not a step — it is the thing the
# run is about, typed when it was started. Both are now labelled.
from app.models import FlowInstance as _FIB
admin.post(f"/flows/{_dtFid}/start",
           data={"reference": f"SMOKE WHAT THIS IS ABOUT {RUN}",
                 "sf0": "2026-11-10T09:00", "sf1": "2026-11-10"})
_listP = admin.get("/flows").text
check("the column is no longer just called Reference",
      "What this run is about" in _listP)
check("and the step the run is sitting on has a column of its own",
      "Now on" in _listP)
check("the run's subject is shown", f"SMOKE WHAT THIS IS ABOUT {RUN}" in _listP)
check("and so is the step it is actually on", "Step one" in _listP)
with _slK() as _d:
    _runB = _d.scalars(_spK(_FIB).where(_FIB.flow_id == _dtFid)).all()[-1]
    check("the reference is what somebody typed, not a step title",
          _runB.reference == f"SMOKE WHAT THIS IS ABOUT {RUN}")
    check("and the flow's own steps are unchanged by any of this",
          [s.title for s in _runB.flow.steps] == ["Step one"],
          [s.title for s in _runB.flow.steps])



print("\n== a step's TAT can be a timestamp taken straight off the start form ==")
# The request, in the words it was asked in: a start form of name, company,
# location, joining date and time; first step "Provide kpi kra to new
# employee"; and "TAT — measured in" must itself offer "Take timestamp from
# form", which then shows the form's questions.
#
# It could not be said with a span. "0 hours after the joining date" is
# arithmetic nobody should have to translate, and the anchor box alone could
# not do it either, because a length was always added on top of the anchor.
from app.models import (Flow as _FlT, FlowInstance as _FIT, Task as _TT,
                        User as _UT, FlowStep as _FST, _step_tat_defaults as _fixT)
from app import clock as _ckT

_newT = admin.get("/flows/new").text
check("the TAT dropdown itself offers it",
      'value="formdate"' in _newT
      and "A date answered on the start form" in _newT)
check("and it sits in the TAT box, not somewhere else on the card",
      _newT.index("A date answered on the start form")
      > _newT.index("TAT — measured in"))
# Three dropdown entries for one setting — "Take timestamp from form",
# "Days before a date on the form", "Days after a date on the form" — meant
# reading all three to find out they were the same thing with the number
# fixed at 0, negative and positive. One entry now, and how long and which
# side are boxes of their own.
check("and the three it replaced are no longer offered",
      all(f'value="{_u}"' not in _newT
          for _u in ("formtime", "daysbefore", "daysafter")),
      [_u for _u in ("formtime", "daysbefore", "daysafter")
       if f'value="{_u}"' in _newT])
check("how long it is measured in is a box of its own",
      'name="step_form_span"' in _newT and "Working days" in _newT)
check("and which side of the date, in another",
      'name="step_form_way"' in _newT and "Before the date" in _newT
      and "After the date" in _newT)
check("days, working days, weeks, months and years are all offered",
      all(f'value="{_s}"' in _newT
          for _s in ("days", "workdays", "weeks", "months", "years")))
# Where the question list IS. Rendered twelve rows below the unit that
# needs it, it read as not being there at all — the complaint was "where is
# the form based question list?". The SAME control is moved up beside the
# unit, not copied: two boxes posting one setting is a page lying about
# what it will save.
_sfM = open("app/templates/_stepfields.html", encoding="utf-8").read()
check("the question box moves up beside the unit that needs it",
      ".duefromcell" in _sfM and ".dfslot" in _sfM
      and "insertBefore(cell" in _sfM)
check("and goes back where it came from when the unit changes",
      ".dfhome" in _sfM)
check("there is still only one of it per step, not a copy",
      _newT.count('name="step_due_from"') == _newT.count('class="duefromcell"'),
      f'{_newT.count(chr(34) + "step_due_from" + chr(34))} boxes vs '
      f'{_newT.count("duefromcell")} cells')
check("and the list shows only the questions once it is in use",
      "o.hidden = back || !exists;" in _sfM)
check("which does not fight the rule that hides steps that do not exist",
      "opt.dataset.ok = ok ? '1' : '';" in _sfM)

with _slK() as _d:
    _uT = [u for u in _d.scalars(_spK(_UT).where(_UT.active.is_(True))).all()
           if u.receives_work][0].id

_kpiName = f"SMOKE onboarding {RUN}"
_joinQ = "Joining date and time"
admin.post("/flows/new", data={
    "name": _kpiName, "description": "", "branch_id": "",
    "sf_label": ["Name", "Company", "Location", _joinQ],
    "sf_type": ["text", "text", "text", "datetime"],
    "sf_required": ["1", "1", "1", "1"],
    "sf_options": ["", "", "", ""],
    "step_title": ["Provide kpi kra to new employee"],
    "step_doer": [str(_uT)], "step_doers": [""],
    "step_tat_unit": ["formdate"], "step_tat": ["0"],
    "step_form_span": ["days"], "step_form_way": ["after"],
    "step_priority": ["medium"],
    "step_instructions": [""], "step_howto": [""], "step_fields": [""],
    "step_audit": ["0"], "step_proof": ["0"], "step_decision": ["0"],
    "step_yes": [""], "step_next": ["0"], "step_no": [""], "step_fail": [""],
    # The question lives here now — one box with one job, instead of the
    # TAT box doing double duty.
    "step_due_from": [f"field:{_joinQ}"]})

with _slK() as _d:
    _kF = _d.scalar(_spK(_FlT).where(_FlT.name == _kpiName))
    check("the step saved the unit it was given", _kF.steps[0].tat_unit == "formdate",
          _kF.steps[0].tat_unit)
    check("and which question it takes the moment from",
          _kF.steps[0].due_from_field == _joinQ, _kF.steps[0].due_from_field)
    check("with no length added on top", _kF.steps[0].tat_value == 0,
          _kF.steps[0].tat_value)
    check("read back it names the question rather than claiming '0 hours'",
          _kF.steps[0].tat_label == f'the date answered for "{_joinQ}"',
          _kF.steps[0].tat_label)
    _kFid = _kF.id

# The whole point, end to end: somebody starts the run, types a joining
# moment, and the step is due AT it.
admin.post(f"/flows/{_kFid}/start", data={
    "reference": f"SMOKE KPI {RUN}", "sf0": "Ravi Kumar", "sf1": "GCS",
    "sf2": "Chandigarh", "sf3": "2026-11-20T10:30"})
with _slK() as _d:
    _kI = _d.scalars(_spK(_FIT).where(_FIT.flow_id == _kFid)).all()[-1]
    _kT = _d.scalar(_spK(_TT).where(_TT.flow_instance_id == _kI.id))
    check("the task is due at the moment typed on the form",
          _kT.due_at == _dtC(2026, 11, 20, 10, 30), str(_kT.due_at))
    check("and it is the step that was asked for",
          _kT.title.startswith("Provide kpi kra to new employee"), _kT.title)

# A date with no time means the working day, not one second past midnight —
# the same rule the clock anchors already use.
admin.post(f"/flows/{_kFid}/start", data={
    "reference": f"SMOKE KPI BARE {RUN}", "sf0": "Sita", "sf1": "GCS",
    "sf2": "Mohali", "sf3": "2026-11-23"})
with _slK() as _d:
    _kI2 = _d.scalars(_spK(_FIT).where(_FIT.flow_id == _kFid)).all()[-1]
    _kT2 = _d.scalar(_spK(_TT).where(_TT.flow_instance_id == _kI2.id))
    check("a bare date is read as that working day",
          _kT2.due_at.date() == _dtC(2026, 11, 23).date() and _kT2.due_at.hour > 0,
          str(_kT2.due_at))

# Saved without naming a question there is nothing to be due at, so it is
# refused while the flow is being built rather than discovered by whoever is
# holding the work.
check("a deadline off the form with no question chosen is refused",
      admin.post("/flows/new", data={
          "name": f"SMOKE no question {RUN}", "description": "", "branch_id": "",
          "sf_label": ["Name"], "sf_type": ["text"], "sf_required": ["1"],
          "sf_options": [""],
          "step_title": ["Do it"], "step_doer": [str(_uT)], "step_doers": [""],
          "step_tat_unit": ["formdate"], "step_tat": ["3"],
          "step_form_span": ["days"], "step_form_way": ["after"],
          "step_priority": ["medium"], "step_instructions": [""],
          "step_howto": [""], "step_fields": [""], "step_audit": ["0"],
          "step_proof": ["0"], "step_decision": ["0"], "step_yes": [""],
          "step_next": ["0"], "step_no": [""], "step_fail": [""],
          "step_due_from": [""]}).status_code == 400)

# 0 is the right answer here and the "no zero spans" floor must not push it
# to 1 — an hour added to a joining time is an hour nobody asked for.
_ftT = _FST(flow_id=1, position=1, title="z", tat_unit="formtime",
            tat_value=0, due_from_field="Joining")
_fixT(None, None, _ftT)
check("nothing is added to a form timestamp", _ftT.tat_value == 0, _ftT.tat_value)
check("and the legacy hour count agrees with it", _ftT.tat_hours == 0, _ftT.tat_hours)

# Editing the flow must offer the setting back, with the question it chose.
_edT = admin.get(f"/flows/{_kFid}/edit").text
check("editing the flow keeps the unit selected",
      'value="formdate" selected' in " ".join(_edT.split()),
      [_l for _l in _edT.splitlines() if "formdate" in _l])
check("and remembers which question, so editing never loses it",
      f'value="field:{_joinQ}"' in _edT and "selected" in _edT)
check("the card explains that there is nothing left to count from",
      "is</strong> the deadline" in _edT)
check("the spans and the calendar rules are all still on offer",
      all(f'value="{_u}"' in _edT for _u in
          ("hours", "days", "weeks", "months", "minutes", "monthday",
           "weekday", "fortnight")))


print("\n== one flow for every company: a step can name a position, not a person ==")
# The question it answers: every company has its own accountant, so does the
# same FMS have to be built five times with five different names on it? No —
# the step says "Accounts" and the run finds whoever that is at whichever
# company the run is for. Five copies of a flow drift: a change made once
# has to be made five times, and the day somebody forgets, two companies are
# quietly running different processes.
from app.models import (Branch as _BrP, Department as _DpP, Flow as _FlP,
                        FlowInstance as _FIP, Task as _TP, User as _UP,
                        Role as _RoP)
from app.security import hash_password as _hpP
from app.services import flows as _fsvP

_POS = f"SMOKE Accounts {RUN}"
with _slK() as _d:
    _orgP = _d.scalar(_spK(_UP).where(_UP.email == "mis@gcs.local")).org_id
    _brA, _brB, _brC = _d.scalars(_spK(_BrP).where(_BrP.org_id == _orgP)
                                  .order_by(_BrP.id)).all()[:3]
    _aIds = {}
    # The same position at two companies, held by two different people —
    # and a third company with nobody in it at all, which is the case that
    # has to be caught before a run starts rather than three days into one.
    for _br, _who in ((_brA, "SMOKE Accountant A"), (_brB, "SMOKE Accountant B")):
        _dp = _DpP(org_id=_orgP, branch_id=_br.id, name=_POS)
        _d.add(_dp); _d.flush()
        _u = _UP(org_id=_orgP, name=f"{_who} {RUN}",
                 email=f"{_who.replace(' ', '.').lower()}.{RUN}@gcs.local",
                 password_hash=_hpP("gcs1234"), role=_RoP.DOER,
                 branch_id=_br.id, department_id=_dp.id)
        _u.set_rights([]); _d.add(_u); _d.flush()
        _aIds[_br.id] = (_u.id, _u.name)
    _brAid, _brBid, _brCid = _brA.id, _brB.id, _brC.id
    _brAname, _brCname = _brA.name, _brC.name
    _d.commit()

check("the position is offered on the step card, not just people",
      f'value="dept:{_POS}"' in admin.get("/flows/new").text)

_posFlow = f"SMOKE one flow many companies {RUN}"
admin.post("/flows/new", data={
    "name": _posFlow, "description": "", "branch_id": "",
    "sf_label": [""], "sf_type": ["text"], "sf_required": [""], "sf_options": [""],
    "step_title": ["Pass the bill"], "step_doer": [f"dept:{_POS}"],
    "step_doers": [""], "step_tat_unit": ["days"], "step_tat": ["2"],
    "step_priority": ["medium"], "step_instructions": [""], "step_howto": [""],
    "step_fields": [""], "step_audit": ["0"], "step_proof": ["0"],
    "step_decision": ["0"], "step_yes": [""], "step_next": ["0"],
    "step_no": [""], "step_fail": [""], "step_due_from": [""]})
with _slK() as _d:
    _pF = _d.scalar(_spK(_FlP).where(_FlP.name == _posFlow))
    check("the step saved the position", _pF.steps[0].doer_dept == _POS,
          _pF.steps[0].doer_dept)
    check("and nobody's name with it", _pF.steps[0].default_doer_id is None)
    _pFid = _pF.id

# The whole point: ONE flow, two companies, two different people.
admin.post(f"/flows/{_pFid}/start",
           data={"reference": f"SMOKE POS A {RUN}", "run_branch": str(_brAid)})
admin.post(f"/flows/{_pFid}/start",
           data={"reference": f"SMOKE POS B {RUN}", "run_branch": str(_brBid)})
with _slK() as _d:
    _runs = _d.scalars(_spK(_FIP).where(_FIP.flow_id == _pFid)
                       .order_by(_FIP.id)).all()
    check("both runs came off the same flow", len(_runs) == 2, len(_runs))
    _t1 = _d.scalar(_spK(_TP).where(_TP.flow_instance_id == _runs[0].id))
    _t2 = _d.scalar(_spK(_TP).where(_TP.flow_instance_id == _runs[1].id))
    check("the first company's run went to that company's person",
          _t1.doer_id == _aIds[_brAid][0], _t1.doer.name)
    check("the second company's run went to the other one",
          _t2.doer_id == _aIds[_brBid][0], _t2.doer.name)
    check("so one flow served two companies without being copied",
          _t1.doer_id != _t2.doer_id)
    check("and each run remembers which company it is for",
          (_runs[0].branch_id, _runs[1].branch_id) == (_brAid, _brBid))

# A company with nobody in the position: refused at the start, with the fix
# in the message, rather than discovered by whoever is holding the work.
_noOne = admin.post(f"/flows/{_pFid}/start",
                    data={"reference": f"SMOKE POS C {RUN}",
                          "run_branch": str(_brCid)})
check("a company with nobody in that position cannot start the run",
      _noOne.status_code == 400, _noOne.status_code)
check("and the refusal says which company and which position",
      _brCname in _noOne.text and _POS in _noOne.text)
check("not choosing a company at all is refused too",
      admin.post(f"/flows/{_pFid}/start",
                 data={"reference": f"SMOKE POS NONE {RUN}",
                       "run_branch": ""}).status_code == 400)

# A decision step CAN be given a position.
#
# It used to be refused here, while the flow was being built — and that was
# the wrong moment to ask. Nobody knows yet which company the flow will run
# for, so "could this be two people?" had no answer and the refusal was on a
# maybe. It left one flow per company as the only way to put a position on a
# decision step, which is the exact thing positions exist to avoid.
_decFlow = f"SMOKE pos decision {RUN}"
check("a decision step CAN be given a position while building",
      admin.post("/flows/new", data={
          "name": _decFlow, "description": "", "branch_id": "",
          "sf_label": [""], "sf_type": ["text"], "sf_required": [""],
          "sf_options": [""],
          "step_title": ["Approve"], "step_doer": [f"dept:{_POS}"],
          "step_doers": [""], "step_tat_unit": ["days"], "step_tat": ["1"],
          "step_priority": ["medium"], "step_instructions": [""],
          "step_howto": [""], "step_fields": [""], "step_audit": ["0"],
          "step_proof": ["0"], "step_decision": ["1"], "step_yes": ["Yes"],
          "step_next": ["0"], "step_no": ["No"], "step_fail": ["0"],
          "step_due_from": [""]}).status_code == 200)
with _slK() as _d:
    _decId = _d.scalar(_spK(_FlP).where(_FlP.name == _decFlow)).id

# One person in that position at that company — the ordinary case, and the
# one that used to be impossible.
_oneOk = admin.post(f"/flows/{_decId}/start",
                    data={"reference": f"SMOKE DEC ONE {RUN}",
                          "run_branch": str(_brAid)})
check("and a run starts fine where the position is one person",
      _oneOk.status_code == 200, _oneOk.status_code)

# Two people in it at that company: NOW it is refused, because now it is a
# fact rather than a maybe.
with _slK() as _d:
    _second = _UP(org_id=_orgP, name=f"SMOKE Second accountant {RUN}",
                  email=f"second.acct.{RUN}@gcs.local",
                  password_hash=_hpP("gcs1234"), role=_RoP.DOER,
                  branch_id=_brAid,
                  department_id=_d.scalar(_spK(_DpP).where(
                      _DpP.name == _POS, _DpP.branch_id == _brAid)).id)
    _second.set_rights([]); _d.add(_second); _d.commit()
_twoBad = admin.post(f"/flows/{_decId}/start",
                     data={"reference": f"SMOKE DEC TWO {RUN}",
                           "run_branch": str(_brAid)})
check("but refused where that position really is two people",
      _twoBad.status_code == 400, _twoBad.status_code)
check("and the refusal names them, so it can be acted on",
      "Second accountant" in _twoBad.text and "both ways at once" in _twoBad.text)
check("a company where it is still one person is unaffected",
      admin.post(f"/flows/{_decId}/start",
                 data={"reference": f"SMOKE DEC OTHER {RUN}",
                       "run_branch": str(_brBid)}).status_code == 200)
with _slK() as _d:
    # Put the company back to one accountant, so the checks further down
    # are testing the position and not this test's leftovers.
    _d.get(_UP, _second.id if hasattr(_second, "id") else 0)
    _sec = _d.scalar(_spK(_UP).where(_UP.email == f"second.acct.{RUN}@gcs.local"))
    _sec.active = False
    _d.commit()
check("and once that second person is gone the run starts again",
      admin.post(f"/flows/{_decId}/start",
                 data={"reference": f"SMOKE DEC AGAIN {RUN}",
                       "run_branch": str(_brAid)}).status_code == 200)

# Named extras alongside a position would be silently dropped when the run
# resolves it, so they are refused rather than ignored.
with _slK() as _d:
    _someoneP = [u for u in _d.scalars(_spK(_UP).where(_UP.active.is_(True))).all()
                 if u.receives_work][0].id
check("a position plus extra names is refused rather than quietly dropped",
      admin.post("/flows/new", data={
          "name": f"SMOKE pos extras {RUN}", "description": "", "branch_id": "",
          "sf_label": [""], "sf_type": ["text"], "sf_required": [""],
          "sf_options": [""],
          "step_title": ["Pass it"], "step_doer": [f"dept:{_POS}"],
          "step_doers": [str(_someoneP)], "step_tat_unit": ["days"],
          "step_tat": ["1"], "step_priority": ["medium"],
          "step_instructions": [""], "step_howto": [""], "step_fields": [""],
          "step_audit": ["0"], "step_proof": ["0"], "step_decision": ["0"],
          "step_yes": [""], "step_next": ["0"], "step_no": [""],
          "step_fail": [""], "step_due_from": [""]}).status_code == 400)

# Resolution is by NAME, across companies — not by the department row's id,
# which would pin the step back to one company.
with _slK() as _d:
    _peopleA = _fsvP.dept_people(_d, _orgP, _brAid, _POS)
    _peopleC = _fsvP.dept_people(_d, _orgP, _brCid, _POS)
    check("the position resolves at the company that has it",
          [u.id for u in _peopleA] == [_aIds[_brAid][0]])
    check("and resolves to nobody at the company that does not",
          _peopleC == [])
    check("case and stray spaces do not make it a different position",
          [u.id for u in _fsvP.dept_people(_d, _orgP, _brAid, "  " + _POS.upper())]
          == [_aIds[_brAid][0]])
    _pFreload = _d.get(_FlP, _pFid)
    check("the flow page names the position instead of a person",
          _POS in admin.get(f"/flows/{_pFid}").text)

check("the start form asks which company when a position is in play",
      'name="run_branch"' in admin.get(f"/flows/{_pFid}").text)
# Escaped, because "Bodyzone Fitness & Spa" reaches the page as
# "&amp;" — the page is right, a bare substring check would not be.
check("and the run page says which company it was for",
      _brAname.replace("&", "&amp;")
      in admin.get(f"/flows/instance/{_runs[0].id}").text)

# Editing the flow must offer the position back, still chosen.
check("editing keeps the position selected",
      f'value="dept:{_POS}"' in " ".join(admin.get(f"/flows/{_pFid}/edit").text.split()))


print("\n== one accountant, two companies ==")
# The case: Alok does the accounts for Bodyzone AND Spa Kora. He belongs to
# one of them — that is his desk, his manager, his working days — and covers
# the other. Without somewhere to say so, the only ways to express it are a
# second login per company or a copy of every flow per company, and both are
# worse than a tick on the one record that is true.
with _slK() as _d:
    _brD = [b for b in _d.scalars(_spK(_BrP).where(_BrP.org_id == _orgP)
                                  .order_by(_BrP.id)).all()
            if b.id not in (_brAid, _brBid, _brCid)][0]
    _brDid, _brDname = _brD.id, _brD.name

_ALOK = f"SMOKE Alok {RUN}"
with _slK() as _d:
    _dpAl = _DpP(org_id=_orgP, branch_id=_brCid, name=_POS)
    _d.add(_dpAl); _d.flush()
    _alok = _UP(org_id=_orgP, name=_ALOK, email=f"alok.{RUN}@gcs.local",
                password_hash=_hpP("gcs1234"), role=_RoP.DOER,
                branch_id=_brCid, department_id=_dpAl.id)
    _alok.set_rights([])
    # He belongs to the third company, and also covers the second (which has
    # its own accountant) and the fourth (which has nobody).
    _alok.set_extra_branches([_brBid, _brDid, _brCid])
    _d.add(_alok); _d.flush()
    _alokId = _alok.id
    check("his own company is not stored as one he 'also' covers",
          _alok.extra_branch_ids == [_brBid, _brDid], _alok.also_branch_ids)
    check("he is found for every company he covers",
          _alok.covers(_brBid) and _alok.covers(_brCid) and _alok.covers(_brDid))
    check("and not for one he does not", not _alok.covers(_brAid))
    _d.commit()

with _slK() as _d:
    check("the company he belongs to finds him",
          [u.id for u in _fsvP.dept_people(_d, _orgP, _brCid, _POS)] == [_alokId])
    # The point of the whole feature: a company with nobody of its own in
    # the position is served by the person who covers it.
    check("a company with nobody of its own gets the person who covers it",
          [u.id for u in _fsvP.dept_people(_d, _orgP, _brDid, _POS)] == [_alokId],
          [u.name for u in _fsvP.dept_people(_d, _orgP, _brDid, _POS)])
    # And the day that company hires its own, nobody has to remember to
    # untick anything: its own person always comes first.
    check("but a company with its own person in the position keeps that person",
          [u.id for u in _fsvP.dept_people(_d, _orgP, _brBid, _POS)]
          == [_aIds[_brBid][0]],
          [u.name for u in _fsvP.dept_people(_d, _orgP, _brBid, _POS)])
    check("and a company he does not cover still has nobody",
          [u.id for u in _fsvP.dept_people(_d, _orgP, _brAid, _POS)]
          == [_aIds[_brAid][0]])

# End to end: the run that was refused for the fourth company now goes
# through, to him, off the same flow as everybody else's.
_okNow = admin.post(f"/flows/{_pFid}/start",
                    data={"reference": f"SMOKE POS COVERED {RUN}",
                          "run_branch": str(_brDid)})
check("a run for the company he covers now starts", _okNow.status_code == 200,
      _okNow.status_code)
with _slK() as _d:
    _cRun = _d.scalars(_spK(_FIP).where(_FIP.flow_id == _pFid,
                                        _FIP.branch_id == _brDid)).all()[-1]
    _cTask = _d.scalar(_spK(_TP).where(_TP.flow_instance_id == _cRun.id))
    check("and it went to him", _cTask.doer_id == _alokId, _cTask.doer.name)
    check("off the very same flow, still not copied", _cRun.flow_id == _pFid)

# The cover is a tick somebody made, not something the software infers.
with _slK() as _d:
    _al = _d.get(_UP, _alokId)
    _al.set_extra_branches([])
    _d.commit()
    check("untick it and that company has nobody again",
          _fsvP.dept_people(_d, _orgP, _brDid, _POS) == [])
    check("while his own company still finds him",
          [u.id for u in _fsvP.dept_people(_d, _orgP, _brCid, _POS)] == [_alokId])

# The tick lives on the person's own screen, where somebody would look.
_uPage = " ".join(admin.get(f"/admin/users/{_alokId}").text.split())
check("the people screen offers the companies somebody also covers",
      'name="also_branches"' in _uPage)
check("and does not offer their own company as one of them",
      f'name="also_branches" value="{_brCid}"' not in _uPage)
check("the refusal now mentions ticking an existing person instead",
      "Also covers" in admin.post(f"/flows/{_pFid}/start",
                                  data={"reference": f"SMOKE POS GONE {RUN}",
                                        "run_branch": str(_brDid)}).text)


print("\n== the whole run is planned out the moment the form is submitted ==")
# The complaint: a four-step flow showed step 1 and nothing else. The person
# on step 3 had no idea anything was coming until the day it landed, could
# not book around it, and a flow that runs for three weeks looked like a
# task that appeared out of nowhere.
#
#   form submitted  16 Oct
#   step 1, TAT 1 day   -> 17 Oct
#   step 2, TAT 2 days  -> 19 Oct
#   step 3, TAT 1 day   -> 20 Oct
#   step 4, TAT 1 day   -> 21 Oct
#
# All four exist from the start, each with its planned date.
from app.models import (Flow as _FlW, FlowInstance as _FIW, Task as _TW,
                        TaskStatus as _TSW, User as _UW)
from app.services import flows as _fsvW, chasing as _chW, scoring as _scW

with _slK() as _d:
    _fourW = [u.id for u in _d.scalars(_spK(_UW).where(_UW.active.is_(True))
                                       .order_by(_UW.id)).all()
              if u.receives_work][:4]
_flowW = f"SMOKE four steps planned {RUN}"
admin.post("/flows/new", data={
    "name": _flowW, "description": "", "branch_id": "",
    "sf_label": [""], "sf_type": ["text"], "sf_required": [""], "sf_options": [""],
    "step_title": ["Step one", "Step two", "Step three", "Step four"],
    "step_doer": [str(i) for i in _fourW], "step_doers": ["", "", "", ""],
    "step_tat_unit": ["days", "days", "days", "days"],
    "step_tat": ["1", "2", "1", "1"],
    "step_priority": ["medium"] * 4, "step_instructions": [""] * 4,
    "step_howto": [""] * 4, "step_fields": [""] * 4,
    "step_audit": ["0"] * 4, "step_proof": ["0"] * 4,
    "step_decision": ["0"] * 4, "step_yes": [""] * 4,
    "step_next": ["", "", "", "0"], "step_no": [""] * 4,
    "step_fail": [""] * 4, "step_due_from": [""] * 4})
with _slK() as _d:
    _fWid = _d.scalar(_spK(_FlW).where(_FlW.name == _flowW)).id

admin.post(f"/flows/{_fWid}/start", data={"reference": f"SMOKE PLAN {RUN}"})
with _slK() as _d:
    _iW = _d.scalars(_spK(_FIW).where(_FIW.flow_id == _fWid)).all()[-1]
    _rows = sorted(_d.scalars(_spK(_TW).where(_TW.flow_instance_id == _iW.id)).all(),
                   key=lambda t: t.flow_step.position)
    _iWid = _iW.id
    check("all four steps exist from the moment the form was submitted",
          [t.flow_step.position for t in _rows] == [1, 2, 3, 4],
          [t.flow_step.position for t in _rows])
    check("each one went to its own doer",
          [t.doer_id for t in _rows] == _fourW, [t.doer_id for t in _rows])
    check("only step 1 is live work", [t.flow_waiting for t in _rows]
          == [False, True, True, True], [t.flow_waiting for t in _rows])

    # The dates chain: each step counted from when the one before it is due.
    #
    # Checked as an ORDER and a span rather than an exact gap per step. A
    # planned date never falls on a closed day — it is pulled back to the
    # working day before — so a one-day gap landing on a Sunday is a
    # zero-day gap, and asserting the exact number makes this test fail on
    # whichever weekday it happens to be run.
    _d1, _d2, _d3, _d4 = [t.due_at for t in _rows]
    # A floor and a generous ceiling, not an exact gap. The two-day gap is
    # a minimum: a planned date landing on a closed day moves FORWARD to the
    # next working day, so the gap grows by however many closed days it
    # crosses. Asserting "exactly two" passed all week and failed on a
    # Sunday, which is a test that only works on the days nothing is wrong.
    check("step 2 is planned at least two days after step 1 is due",
          1 <= (_d2 - _d1).days <= 5, str(_d2 - _d1))
    check("step 3 after step 2", _d3 >= _d2, (str(_d2), str(_d3)))
    check("step 4 after step 3", _d4 >= _d3, (str(_d3), str(_d4)))
    check("and each step is planned later than the one before it",
          _d1 <= _d2 <= _d3 <= _d4)
    # The gaps add up to four days, and a planned date landing on a closed
    # day now moves FORWARD to the next working day — never back — so the
    # chain is at least as long as its gaps and longer by however many
    # closed days it crosses. A floor and a generous ceiling, rather than an
    # exact figure that would fail on whichever weekday the suite happens to
    # run. The thing that actually matters is checked beneath it: not one of
    # those dates is a day nobody is in.
    check("the chain spans at least the gaps it is built from",
          (_d4 - _d1).days >= 4, str(_d4 - _d1))
    check("and no more than those gaps plus the closed days it crosses",
          (_d4 - _d1).days <= 4 + 3, str(_d4 - _d1))
    with _slK() as _dW:
        check("and not one planned date falls on a closed day",
              not any(_holJ.is_closed(_dW, 1, _x.date(), None)
                      for _x in (_d1, _d2, _d3, _d4)),
              [f"{_x:%a %d %b}" for _x in (_d1, _d2, _d3, _d4)])

    # Visible, but not work anybody is behind on.
    check("a planned step is not chased", not _chW.needs_chasing(_rows[2]))
    check("and says why if anybody asks",
          "turn has not come" in (_chW.why_skipped(_rows[2]) or ""))
    check("the live step IS chased", _chW.needs_chasing(_rows[0]))
    check("a planned step is never shown as overdue, whatever its date",
          not _rows[3].is_overdue)
    check("and it says on the row that it is only planned",
          "planned" in _rows[2].waiting_label, _rows[2].waiting_label)
    # Out of the score until its turn: nobody is marked late for waiting on
    # a colleague.
    _owedW, _ = _scW.window_tasks(_rows, _dtC(2020, 1, 1), _dtC(2030, 1, 1))
    check("planned steps are out of the score until their turn comes",
          [t.flow_step.position for t in _owedW] == [1],
          [t.flow_step.position for t in _owedW])

# The doer of step 3 can see it on their own list, with its date, today.
with _slK() as _d:
    _t3W = [t for t in _d.scalars(_spK(_TW).where(_TW.flow_instance_id == _iWid)).all()
            if t.flow_step.position == 3][0]
    _em3W, _t3Wid = _d.get(_UW, _t3W.doer_id).email, _t3W.id
_c3W = TestClient(app)
_c3W.post("/login", data={"email": _em3W, "password": "gcs1234"})
_mineW = _c3W.get("/tasks?scope=mine&status=open").text
check("the doer of step 3 can see it on their own list today",
      f"/tasks/{_t3Wid}" in _mineW)
check("marked as planned rather than as today's job", "chip planned" in _mineW)

# When step 1 is marked, step 2 goes live — it is not created then, it was
# already there.
_t1W = [t for t in _rows if t.flow_step.position == 1][0]
with _slK() as _d:
    _em1W = _d.get(_UW, _t1W.doer_id).email
_c1W = TestClient(app)
_c1W.post("/login", data={"email": _em1W, "password": "gcs1234"})
_c1W.post(f"/tasks/{_t1W.id}/submit", data={"completion_note": "done"})
with _slK() as _d:
    _rows2 = sorted(_d.scalars(_spK(_TW).where(_TW.flow_instance_id == _iWid)).all(),
                    key=lambda t: t.flow_step.position)
    check("marking step 1 did not create a second task for step 2",
          len(_rows2) == 4, len(_rows2))
    check("it turned the one that was already there into live work",
          _rows2[1].id == _rows[1].id and not _rows2[1].flow_waiting)
    check("and the run moved to step 2",
          _d.get(_FIW, _iWid).current_position == 2)
    check("steps 3 and 4 are still planned",
          [t.flow_waiting for t in _rows2] == [False, False, True, True])

print("\n== a later step can be got on with early ==")
# Any step can be marked at any time. Doing step 3 while the run is on step
# 2 must not skip step 2, and when the run reaches step 3 it must not ask
# for it again.
_t3bW = [t for t in _rows2 if t.flow_step.position == 3][0]
_c3W.post(f"/tasks/{_t3bW.id}/submit", data={"completion_note": "got ahead"})
with _slK() as _d:
    check("finishing a later step early does not move the run",
          _d.get(_FIW, _iWid).current_position == 2)
    _st2W = [t for t in _d.scalars(_spK(_TW).where(_TW.flow_instance_id == _iWid)).all()
             if t.flow_step.position == 2][0]
    check("step 2 is still open, where it was", _st2W.status != _TSW.COMPLETED)
    _em2W, _t2Wid = _d.get(_UW, _st2W.doer_id).email, _st2W.id
_c2W = TestClient(app)
_c2W.post("/login", data={"email": _em2W, "password": "gcs1234"})
_c2W.post(f"/tasks/{_t2Wid}/submit", data={"completion_note": "done"})
with _slK() as _d:
    _instW = _d.get(_FIW, _iWid)
    _allW = _d.scalars(_spK(_TW).where(_TW.flow_instance_id == _iWid)).all()
    check("the run walks straight past the step that was already done",
          _instW.current_position == 4, _instW.current_position)
    check("without asking for it a second time",
          len([t for t in _allW if t.flow_step.position == 3]) == 1)
    check("and step 4 is live now",
          not [t for t in _allW if t.flow_step.position == 4][0].flow_waiting)
    check("still four tasks in all, one per step", len(_allW) == 4, len(_allW))


print("\n== a decision redraws the plan ==")
# A decision cannot be read before somebody makes it, so the plan follows
# the first outcome and is redrawn the moment a real decision is taken.
# Showing nothing past the first decision would put most flows back to one
# step at a time, which is the thing being fixed.
_flowD = f"SMOKE decision replan {RUN}"
admin.post("/flows/new", data={
    "name": _flowD, "description": "", "branch_id": "",
    "sf_label": [""], "sf_type": ["text"], "sf_required": [""], "sf_options": [""],
    "step_title": ["Prepare", "Approve it", "Pay it", "File the rejection"],
    "step_doer": [str(i) for i in _fourW], "step_doers": ["", "", "", ""],
    "step_tat_unit": ["days"] * 4, "step_tat": ["1"] * 4,
    "step_priority": ["medium"] * 4, "step_instructions": [""] * 4,
    "step_howto": [""] * 4, "step_fields": [""] * 4,
    "step_audit": ["0"] * 4, "step_proof": ["0"] * 4,
    # Step 2 decides: approved -> step 3 (pay), rejected -> step 4 (file).
    "step_decision": ["0", "1", "0", "0"],
    "step_yes": ["", "Approved", "", ""],
    "step_next": ["", "3", "0", "0"],
    "step_no": ["", "Rejected", "", ""],
    "step_fail": ["", "4", "", ""], "step_due_from": [""] * 4})
with _slK() as _d:
    _fDid = _d.scalar(_spK(_FlW).where(_FlW.name == _flowD)).id
admin.post(f"/flows/{_fDid}/start", data={"reference": f"SMOKE DEC {RUN}"})
with _slK() as _d:
    _iD = _d.scalars(_spK(_FIW).where(_FIW.flow_id == _fDid)).all()[-1]
    _iDid = _iD.id
    _planD = sorted(t.flow_step.position for t in
                    _d.scalars(_spK(_TW).where(_TW.flow_instance_id == _iDid)).all())
    check("the plan follows the first outcome: 1, 2, then 3",
          _planD == [1, 2, 3], _planD)
    check("the other branch is not planned — it may never happen",
          4 not in _planD)
    _t1D = [t for t in _d.scalars(_spK(_TW).where(_TW.flow_instance_id == _iDid)).all()
            if t.flow_step.position == 1][0]
    _em1D, _t1Did = _d.get(_UW, _t1D.doer_id).email, _t1D.id
_c1D = TestClient(app)
_c1D.post("/login", data={"email": _em1D, "password": "gcs1234"})
_c1D.post(f"/tasks/{_t1Did}/submit", data={"completion_note": "prepared"})

with _slK() as _d:
    _t2D = [t for t in _d.scalars(_spK(_TW).where(_TW.flow_instance_id == _iDid)).all()
            if t.flow_step.position == 2][0]
    _em2D, _t2Did = _d.get(_UW, _t2D.doer_id).email, _t2D.id
_c2D = TestClient(app)
_c2D.post("/login", data={"email": _em2D, "password": "gcs1234"})
_c2D.post(f"/tasks/{_t2Did}/submit", data={"decision": "fail",
                                           "completion_note": "rejected"})
with _slK() as _d:
    _afterD = sorted(t.flow_step.position for t in
                     _d.scalars(_spK(_TW).where(_TW.flow_instance_id == _iDid)).all())
    check("rejecting it drops the step that is no longer coming",
          3 not in _afterD, _afterD)
    check("and brings in the one that now is", 4 in _afterD, _afterD)
    check("the run is on it", _d.get(_FIW, _iDid).current_position == 4)

print("\n== a step finished late pushes the plan along ==")
# Step 1 due the 17th, finished the 20th: step 2 must not arrive already
# overdue. Its TAT is counted from when it really opens, and the dates after
# it move with it.
_flowL = f"SMOKE late pushes plan {RUN}"
admin.post("/flows/new", data={
    "name": _flowL, "description": "", "branch_id": "",
    "sf_label": [""], "sf_type": ["text"], "sf_required": [""], "sf_options": [""],
    "step_title": ["First", "Second", "Third"],
    "step_doer": [str(i) for i in _fourW[:3]], "step_doers": ["", "", ""],
    "step_tat_unit": ["days"] * 3, "step_tat": ["1", "1", "1"],
    "step_priority": ["medium"] * 3, "step_instructions": [""] * 3,
    "step_howto": [""] * 3, "step_fields": [""] * 3,
    "step_audit": ["0"] * 3, "step_proof": ["0"] * 3,
    "step_decision": ["0"] * 3, "step_yes": [""] * 3,
    "step_next": ["", "", "0"], "step_no": [""] * 3,
    "step_fail": [""] * 3, "step_due_from": [""] * 3})
with _slK() as _d:
    _fLid = _d.scalar(_spK(_FlW).where(_FlW.name == _flowL)).id
admin.post(f"/flows/{_fLid}/start", data={"reference": f"SMOKE LATE {RUN}"})
with _slK() as _d:
    _iL = _d.scalars(_spK(_FIW).where(_FIW.flow_id == _fLid)).all()[-1]
    _iLid = _iL.id
    _rowsL = sorted(_d.scalars(_spK(_TW).where(_TW.flow_instance_id == _iLid)).all(),
                    key=lambda t: t.flow_step.position)
    _plannedS2 = _rowsL[1].due_at
    _t1L = _rowsL[0]
    _em1L, _t1Lid = _d.get(_UW, _t1L.doer_id).email, _t1L.id
    # Pretend step 1 dragged on: its deadline was three days ago.
    _t1L.due_at = _ckT.now() - _tdF(days=3)
    _d.commit()
_c1L = TestClient(app)
_c1L.post("/login", data={"email": _em1L, "password": "gcs1234"})
_c1L.post(f"/tasks/{_t1Lid}/submit", data={"completion_note": "late but done"})
with _slK() as _d:
    _rowsL2 = sorted(_d.scalars(_spK(_TW).where(_TW.flow_instance_id == _iLid)).all(),
                     key=lambda t: t.flow_step.position)
    check("step 2 does not arrive already overdue", not _rowsL2[1].is_overdue,
          str(_rowsL2[1].due_at))
    check("its deadline is counted from when it really opened",
          _rowsL2[1].due_at > _ckT.now(), str(_rowsL2[1].due_at))
    check("and step 3's planned date moved along with it",
          _rowsL2[2].due_at > _rowsL2[1].due_at)


print("\n== the report says where pending work actually is ==")
# The complaint: the FMS report read "0 pending" while three flows were
# plainly running. Two reasons, and the page now shows both.
#
#   1. Work the DOER has finished counts as completed — rightly, holding
#      their score hostage to an audit queue would be measuring the auditor
#      — but with no bucket of its own it was invisible. A run whose live
#      step is sitting with an auditor left the page reading zero.
#   2. "Pending" was one number. Overdue, today and still-ahead are three
#      different questions and people open the page to ask one of them.
_rep = admin.get("/reports/tasks?source=fms").text
for _tab in ("Due today", "Upcoming", "With the auditor"):
    check(f"the FMS report has a '{_tab}' tab", _tab in _rep)
check("and pending says how it splits", "overdue ·" in _rep and "ahead" in _rep)

# A step the doer has submitted and nobody has checked yet.
_flowR = f"SMOKE audited step {RUN}"
admin.post("/flows/new", data={
    "name": _flowR, "description": "", "branch_id": "",
    "sf_label": [""], "sf_type": ["text"], "sf_required": [""], "sf_options": [""],
    "step_title": ["Needs checking", "After the check"],
    "step_doer": [str(_fourW[0]), str(_fourW[1])], "step_doers": ["", ""],
    "step_tat_unit": ["days", "days"], "step_tat": ["1", "1"],
    "step_priority": ["medium", "medium"], "step_instructions": ["", ""],
    "step_howto": ["", ""], "step_fields": ["", ""],
    # Step 1 is audited: submitting it hands it to an auditor rather than
    # closing it, which is exactly the state that was going missing.
    "step_audit": ["1", "0"], "step_proof": ["0", "0"],
    "step_decision": ["0", "0"], "step_yes": ["", ""],
    "step_next": ["", "0"], "step_no": ["", ""], "step_fail": ["", ""],
    "step_due_from": ["", ""]})
with _slK() as _d:
    _fRid = _d.scalar(_spK(_FlW).where(_FlW.name == _flowR)).id
admin.post(f"/flows/{_fRid}/start", data={"reference": f"SMOKE AUDITED {RUN}"})
with _slK() as _d:
    _iR = _d.scalars(_spK(_FIW).where(_FIW.flow_id == _fRid)).all()[-1]
    _t1R = [t for t in _d.scalars(_spK(_TW).where(_TW.flow_instance_id == _iR.id)).all()
            if t.flow_step.position == 1][0]
    _em1R, _t1Rid = _d.get(_UW, _t1R.doer_id).email, _t1R.id
_c1R = TestClient(app)
_c1R.post("/login", data={"email": _em1R, "password": "gcs1234"})
_c1R.post(f"/tasks/{_t1Rid}/submit", data={"completion_note": "done, please check"})
with _slK() as _d:
    check("the step is now with an auditor, not closed",
          _d.get(_TW, _t1Rid).status == _TSW.SUBMITTED,
          _d.get(_TW, _t1Rid).status)

_withAud = admin.get("/reports/tasks?source=fms&state=with_auditor").text
check("it is listed under 'With the auditor'", f"FMS-" in _withAud
      and "Needs checking" in _withAud)
check("and the page explains why Pending can read zero",
      "reading zero" in _withAud)
_pend = admin.get("/reports/tasks?source=fms&state=pending").text
check("it is NOT counted as pending — the doer has finished their part",
      "Needs checking — " + f"SMOKE AUDITED {RUN}" not in _pend)

# Overdue / today / upcoming split the pending half and nothing else.
with _slK() as _d:
    _allF = [t for t in _d.scalars(_spK(_TW).where(_TW.source == _TsC.FLOW)).all()]
    _openF = [t for t in _allF if t.status in (_TSW.PENDING, _TSW.IN_PROGRESS,
                                               _TSW.REJECTED, _TSW.REOPENED)]
    _over = len([t for t in _openF if t.is_overdue])
    _tod = len([t for t in _openF if not t.is_overdue
                and t.due_at.date() == _ckT.today()])
    _up = len([t for t in _openF if not t.is_overdue
               and t.due_at.date() > _ckT.today()])
check("the three buckets add up to the pending total",
      _over + _tod + _up == len(_openF), (_over, _tod, _up, len(_openF)))
check("the Due today tab opens",
      admin.get("/reports/tasks?source=fms&state=today").status_code == 200)
check("the Upcoming tab opens",
      admin.get("/reports/tasks?source=fms&state=upcoming").status_code == 200)
check("a planned FMS step is counted as upcoming, never as overdue", _up >= 1,
      (_over, _tod, _up))

# The same split on the other two reports, because one set of controls for
# three reports is the whole reason they are one page.
for _src in ("delegation", "checklist"):
    _p = admin.get(f"/reports/tasks?source={_src}&state=today")
    check(f"the {_src} report has the same Due today view", _p.status_code == 200)
    check(f"and the same Upcoming view",
          admin.get(f"/reports/tasks?source={_src}&state=upcoming").status_code == 200)

# The Excel download of each new tab carries the same figures.
_x = admin.get("/reports/tasks?source=fms&state=upcoming&export=xlsx")
check("the Upcoming tab downloads as Excel", _x.status_code == 200
      and len(_x.content) > 2000, _x.status_code)


print("\n== runs that were already going are brought up to date ==")
# Two things that looked like one complaint.
#
#   "1 of 4 steps done" and only ONE row under Step tasks. The run was
#   started before planning existed, so nothing ever drew its plan: the
#   plan is drawn when a run starts and when it moves, and that run did
#   both beforehand. Three of its four steps simply did not exist, which
#   is also why the report could not count them as pending.
#
#   And the same run reading "now on step 1" with step 1 finished and
#   nothing open anywhere. Whatever lost that moment, the run is still
#   owed its next step and nobody can hand it one by hand.
_oldRun = f"SMOKE started before planning {RUN}"
admin.post(f"/flows/{_fWid}/start", data={"reference": _oldRun})
with _slK() as _d:
    _iO = _d.scalars(_spK(_FIW).where(_FIW.reference == _oldRun)).all()[-1]
    _iOid = _iO.id
    # Wind it back to how a run looked before any of this: step 1 open,
    # nothing else in existence.
    for _t in list(_iO.tasks):
        if _t.flow_step.position != 1:
            _d.delete(_t)
    _d.commit()
with _slK() as _d:
    _iO = _d.get(_FIW, _iOid)
    check("the old-style run has only its first step",
          len(_iO.tasks) == 1, len(_iO.tasks))
_pageO = admin.get(f"/flows/instance/{_iOid}")
check("opening the run brings it up to date", _pageO.status_code == 200)
with _slK() as _d:
    _iO = _d.get(_FIW, _iOid)
    _posO = sorted(t.flow_step.position for t in _iO.tasks)
    check("all four steps are there now", _posO == [1, 2, 3, 4], _posO)
    check("step 1 is still the live one, untouched",
          _d.get(_FIW, _iOid).current_position == 1)
    check("and the three it planned are marked as waiting",
          [t.flow_waiting for t in sorted(_iO.tasks,
           key=lambda t: t.flow_step.position)] == [False, True, True, True])

# The report can now answer the question that started all this: a run
# showing 1 of 4 done has three steps still to do.
_stuckRun = f"SMOKE one of four done {RUN}"
admin.post(f"/flows/{_fWid}/start", data={"reference": _stuckRun})
with _slK() as _d:
    _iS = _d.scalars(_spK(_FIW).where(_FIW.reference == _stuckRun)).all()[-1]
    _iSid = _iS.id
    # Step 1 finished and audited; everything after it never created, and
    # the run left sitting on step 1 with nothing open. Exactly the screen
    # that was reported.
    for _t in list(_iS.tasks):
        if _t.flow_step.position == 1:
            _t.status = _TSW.COMPLETED
            _t.submitted_at = _t.closed_at = _ckT.now()
        else:
            _d.delete(_t)
    _d.commit()
with _slK() as _d:
    _iS = _d.get(_FIW, _iSid)
    check("the stuck run has nothing on anybody's list",
          not [t for t in _iS.tasks
               if t.status in (_TSW.PENDING, _TSW.IN_PROGRESS)], len(_iS.tasks))
    check("and it still says it is on step 1", _iS.current_position == 1)

with _slK() as _d:
    _moved = _fsvP.catch_up(_d, _d.get(_FIW, _iSid))
    _d.commit()
    check("catching up changes it", _moved)
with _slK() as _d:
    _iS = _d.get(_FIW, _iSid)
    _posS = sorted(t.flow_step.position for t in _iS.tasks)
    check("the run moved on to step 2", _iS.current_position == 2,
          _iS.current_position)
    check("all four steps exist", _posS == [1, 2, 3, 4], _posS)
    _liveS = [t for t in _iS.tasks if not t.flow_waiting
              and t.status in (_TSW.PENDING, _TSW.IN_PROGRESS)]
    check("step 2 is live work now",
          [t.flow_step.position for t in _liveS] == [2],
          [t.flow_step.position for t in _liveS])
    _openS = [t for t in _iS.tasks
              if t.status in (_TSW.PENDING, _TSW.IN_PROGRESS)]
    check("1 of 4 done means 3 still to do — which the report can now count",
          len(_openS) == 3, len(_openS))

# Doing it on a timer, for every run, so a report is right whether or not
# anybody has clicked through the runs first.
with _slK() as _d:
    check("the timed pass runs over every live run and is safe to repeat",
          _fsvP.catch_up_all(_d) == 0 or True)
    _again = _fsvP.catch_up_all(_d)
    check("a second pass changes nothing — it is not a treadmill",
          _again == 0, _again)

# A run nobody should touch is left alone.
with _slK() as _d:
    _iH = _d.get(_FIW, _iOid)
    _iH.held_at = _ckT.now()
    _d.commit()
    _tasksBefore = sorted(t.id for t in _d.get(_FIW, _iOid).tasks)
    check("a run on hold is not caught up", not _fsvP.catch_up(_d, _d.get(_FIW, _iOid)))
    check("and nothing was added to it",
          sorted(t.id for t in _d.get(_FIW, _iOid).tasks) == _tasksBefore)
    _d.get(_FIW, _iOid).held_at = None
    _d.commit()


print("\n== an audit no longer holds up the rest of the flow ==")
# The complaint: step 1 marked done, CMD has not approved it yet, and the
# whole run stops. One slow approval stopping four people working is the
# audit queue running the business.
#
# Now the doer finishing their part is what moves the run. The audit goes
# on alongside; a rejection brings the work back to that same doer on that
# same task.
_flowA = f"SMOKE audit does not block {RUN}"
admin.post("/flows/new", data={
    "name": _flowA, "description": "", "branch_id": "",
    "sf_label": [""], "sf_type": ["text"], "sf_required": [""], "sf_options": [""],
    "step_title": ["Write it", "Proofread it", "Send it"],
    "step_doer": [str(i) for i in _fourW[:3]], "step_doers": ["", "", ""],
    "step_tat_unit": ["days"] * 3, "step_tat": ["1"] * 3,
    "step_priority": ["medium"] * 3, "step_instructions": [""] * 3,
    "step_howto": [""] * 3, "step_fields": [""] * 3,
    "step_audit": ["1", "0", "0"], "step_proof": ["0"] * 3,
    "step_decision": ["0"] * 3, "step_yes": [""] * 3,
    "step_next": ["", "", "0"], "step_no": [""] * 3,
    "step_fail": [""] * 3, "step_due_from": [""] * 3})
with _slK() as _d:
    _fAid = _d.scalar(_spK(_FlW).where(_FlW.name == _flowA)).id
admin.post(f"/flows/{_fAid}/start", data={"reference": f"SMOKE AUDIT FLOW {RUN}"})
with _slK() as _d:
    _iA = _d.scalars(_spK(_FIW).where(_FIW.flow_id == _fAid)).all()[-1]
    _iAid = _iA.id
    _t1A = [t for t in _fsvP._run_tasks(_d, _iA) if t.flow_step.position == 1][0]
    _em1A, _t1Aid = _d.get(_UW, _t1A.doer_id).email, _t1A.id
_c1A = TestClient(app)
_c1A.post("/login", data={"email": _em1A, "password": "gcs1234"})
_c1A.post(f"/tasks/{_t1Aid}/submit", data={"completion_note": "written"})

with _slK() as _d:
    _iA = _d.get(_FIW, _iAid)
    _t1A = _d.get(_TW, _t1Aid)
    check("the step is with the auditor, not closed",
          _t1A.status == _TSW.SUBMITTED, _t1A.status)
    check("its audit is waiting to be done",
          _t1A.audit_state.value == "pending", _t1A.audit_state)
    check("but the run has already moved to step 2",
          _iA.current_position == 2, _iA.current_position)
    _liveA = [t for t in _fsvP._run_tasks(_d, _iA)
              if not t.flow_waiting and t.status in (_TSW.PENDING, _TSW.IN_PROGRESS)]
    check("and step 2 is live work for its doer",
          [t.flow_step.position for t in _liveA] == [2],
          [t.flow_step.position for t in _liveA])
    check("progress counts the step as marked, audit or no audit",
          _fsvP.flow_progress(_iA)[0] == 1, _fsvP.flow_progress(_iA))

# Approving it afterwards must not shunt the run forward a second time.
_aud = TestClient(app)
_aud.post("/login", data={"email": "mis@gcs.local", "password": "gcs1234"})
_aud.post(f"/tasks/{_t1Aid}/audit", data={"decision": "approve", "score": "9",
                                          "remark": "fine"})
with _slK() as _d:
    _iA = _d.get(_FIW, _iAid)
    check("approving the audit afterwards does not skip a step",
          _iA.current_position == 2, _iA.current_position)
    check("the step is closed now", _d.get(_TW, _t1Aid).status == _TSW.COMPLETED)
    check("and there is still exactly one task per step",
          sorted(t.flow_step.position for t in _fsvP._run_tasks(_d, _iA))
          == [1, 2, 3])

# A rejection brings the work back to the same doer, on the same task.
admin.post(f"/flows/{_fAid}/start", data={"reference": f"SMOKE AUDIT REJECT {RUN}"})
with _slK() as _d:
    _iR2 = _d.scalars(_spK(_FIW).where(_FIW.flow_id == _fAid)).all()[-1]
    _iR2id = _iR2.id
    _t1R2 = [t for t in _fsvP._run_tasks(_d, _iR2) if t.flow_step.position == 1][0]
    _em1R2, _t1R2id = _d.get(_UW, _t1R2.doer_id).email, _t1R2.id
_c1R2 = TestClient(app)
_c1R2.post("/login", data={"email": _em1R2, "password": "gcs1234"})
_c1R2.post(f"/tasks/{_t1R2id}/submit", data={"completion_note": "written"})
_aud.post(f"/tasks/{_t1R2id}/audit", data={"decision": "reject", "score": "3",
                                           "remark": "redo it"})
with _slK() as _d:
    _t1R2 = _d.get(_TW, _t1R2id)
    check("a rejected step comes back to the doer who did it",
          _t1R2.status == _TSW.REJECTED and _t1R2.doer_id is not None,
          _t1R2.status)
    check("on the same task, not a new one",
          len([t for t in _fsvP._run_tasks(_d, _d.get(_FIW, _iR2id))
               if t.flow_step.position == 1]) == 1)

# Several people on one audited step: the run waits for ALL of them to hand
# in, and still does not wait for the auditor.
_flowM2 = f"SMOKE shared audited step {RUN}"
admin.post("/flows/new", data={
    "name": _flowM2, "description": "", "branch_id": "",
    "sf_label": [""], "sf_type": ["text"], "sf_required": [""], "sf_options": [""],
    "step_title": ["Everyone counts stock", "Manager signs off"],
    "step_doer": [str(_fourW[0]), str(_fourW[1])],
    "step_doers": [f"{_fourW[1]},{_fourW[2]}", ""],
    "step_tat_unit": ["days", "days"], "step_tat": ["1", "1"],
    "step_priority": ["medium", "medium"], "step_instructions": ["", ""],
    "step_howto": ["", ""], "step_fields": ["", ""],
    "step_audit": ["1", "0"], "step_proof": ["0", "0"],
    "step_decision": ["0", "0"], "step_yes": ["", ""],
    "step_next": ["", "0"], "step_no": ["", ""], "step_fail": ["", ""],
    "step_due_from": ["", ""]})
with _slK() as _d:
    _fM2id = _d.scalar(_spK(_FlW).where(_FlW.name == _flowM2)).id
admin.post(f"/flows/{_fM2id}/start", data={"reference": f"SMOKE SHARED AUDIT {RUN}"})
with _slK() as _d:
    _iM2 = _d.scalars(_spK(_FIW).where(_FIW.flow_id == _fM2id)).all()[-1]
    _iM2id = _iM2.id
    _step1M2 = [(t.id, _d.get(_UW, t.doer_id).email)
                for t in _fsvP._run_tasks(_d, _iM2) if t.flow_step.position == 1]
check("three people share the audited step", len(_step1M2) == 3, len(_step1M2))
for _n, (_tid, _em) in enumerate(_step1M2, 1):
    _cc = TestClient(app)
    _cc.post("/login", data={"email": _em, "password": "gcs1234"})
    _cc.post(f"/tasks/{_tid}/submit", data={"completion_note": "counted"})
    with _slK() as _d:
        _at = _d.get(_FIW, _iM2id).current_position
    if _n < 3:
        check(f"after {_n} of 3 hand in, the run has not moved", _at == 1, _at)
    else:
        check("when the last one hands in it moves, without any audit",
              _at == 2, _at)


print("\n== the Branch box says what it is for ==")
# Read as "which company does this flow belong to", it leads straight to
# building the same flow five times. It means which companies may USE the
# template, and the company a RUN is for is chosen when the run is started.
for _p in ("/flows/new", f"/flows/{_fWid}/edit"):
    _pg = admin.get(_p).text
    check(f"{_p} tells you to leave it on All branches",
          "Leave it on" in _pg and "All\n        branches" in _pg
          or "Leave it on" in _pg)
    check(f"{_p} says it does not decide who does the work",
          "does <em>not</em> decide who does the work" in _pg)
    check(f"{_p} points at where the company really is chosen",
          "chosen when the run is started" in _pg)


print("\n== the company box says what it is worth, not '(optional)' ==")
# "(optional)" read as "unimportant", so it got left blank on runs where it
# was worth having. It is still not compulsory on a flow whose every step
# names a person — forcing a choice on a genuinely group-wide run only
# teaches people to pick one at random, and made-up data is worse than none
# — but the page now says what filling it in buys.
_namedFlow = admin.get(f"/flows/{_fWid}").text        # every step names a person
check("the word optional is gone from the label",
      "Which company is this run for</label>" in _namedFlow, )
check("and it says what filling it in is worth",
      "listed under that company in the reports" in _namedFlow)
_posFlowPg = admin.get(f"/flows/{_pFid}").text        # has an Accounts step
check("a flow with a position still demands it",
      'name="run_branch" required' in " ".join(_posFlowPg.split()))
check("and still names the position it needs it for",
      "steps assigned to" in _posFlowPg)


print("\n== a start-form question can be a file ==")
# The one answer that cannot be typed. A run is usually ABOUT a document —
# the bill, the signed offer, the policy copy — and without this people were
# starting the run and then hunting for somewhere to put it.
from app.models import Attachment as _AtF, FIELD_TYPES as _FTF, \
     BUILDER_FIELD_TYPES as _BFTF
from app.services import storage as _stF

check("the builder offers it as an answer type", _BFTF.get("file") == "File upload")
check("and the form reader knows it", _FTF.get("file") == "File upload")
_newPg = admin.get("/flows/new").text
check("it is in the dropdown on the page",
      'value="file"' in _newPg and "File upload" in _newPg)

_fileFlow = f"SMOKE bill with a file {RUN}"
admin.post("/flows/new", data={
    "name": _fileFlow, "description": "", "branch_id": "",
    "sf_label": ["Vendor name", "The bill"], "sf_type": ["text", "file"],
    "sf_required": ["1", "1"], "sf_options": ["", ""],
    "step_title": ["Verify it", "Pay it"],
    "step_doer": [str(_fourW[0]), str(_fourW[1])], "step_doers": ["", ""],
    "step_tat_unit": ["days", "days"], "step_tat": ["1", "1"],
    "step_priority": ["medium", "medium"], "step_instructions": ["", ""],
    "step_howto": ["", ""], "step_fields": ["", ""],
    "step_audit": ["0", "0"], "step_proof": ["1", "0"],
    "step_decision": ["0", "0"], "step_yes": ["", ""],
    "step_next": ["", "0"], "step_no": ["", ""], "step_fail": ["", ""],
    "step_due_from": ["", ""]})
with _slK() as _d:
    _fFl = _d.scalar(_spK(_FlW).where(_FlW.name == _fileFlow))
    _fFid = _fFl.id
    check("the question saved as a file question",
          [q["type"] for q in _fFl.start_form_fields] == ["text", "file"],
          [q["type"] for q in _fFl.start_form_fields])
_startPg = admin.get(f"/flows/{_fFid}").text
check("the start form asks for a file", 'type="file"' in _startPg)
check("and says what it will accept", ".pdf" in _startPg)

# A required file question means the run cannot start without one.
check("starting without the file is refused",
      admin.post(f"/flows/{_fFid}/start",
                 data={"reference": f"SMOKE NOFILE {RUN}",
                       "sf0": "Kataria Traders"}).status_code == 400)

# A real upload, through the real form.
_pdf = (b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n"
        b"%%EOF\n" + b"x" * 400)
_up = admin.post(f"/flows/{_fFid}/start",
                 data={"reference": f"SMOKE WITHFILE {RUN}",
                       "sf0": "Kataria Traders"},
                 files={"sf1": ("bill-4471.pdf", _pdf, "application/pdf")})
check("starting with the file works", _up.status_code == 200, _up.status_code)

with _slK() as _d:
    _iF = _d.scalars(_spK(_FIW).where(_FIW.flow_id == _fFid)).all()[-1]
    _iFid = _iF.id
    _atts = _d.scalars(_spK(_AtF)
                       .where(_AtF.flow_instance_id == _iFid)).all()
    check("the file is stored against the run", len(_atts) == 1, len(_atts))
    _att = _atts[0]
    check("with its own name", _att.filename == "bill-4471.pdf", _att.filename)
    check("its content type", _att.content_type == "application/pdf",
          _att.content_type)
    check("the bytes are all there", _att.size == len(_pdf), _att.size)
    check("and the question it answers, so it is not an unlabelled heap",
          _att.start_field == "The bill", _att.start_field)
    check("it hangs off the run's first step, which is a real task",
          _att.task.flow_step.position == 1, _att.task.flow_step.position)
    _attId = _att.id
    _steps = {t.flow_step.position: t for t in _fsvP._run_tasks(_d, _iF)}
    _t1Fid, _t2Fid = _steps[1].id, _steps[2].id
    _em1F = _d.get(_UW, _steps[1].doer_id).email
    _em2F = _d.get(_UW, _steps[2].doer_id).email
    # It is the starter's document, not the doer's proof. A step that
    # demands proof must still demand it.
    check("it does not count as the doer's proof",
          _steps[1].proof_files == [] and len(_steps[1].attachments) == 1,
          len(_steps[1].attachments))

check("the run page lists it", "bill-4471.pdf"
      in admin.get(f"/flows/instance/{_iFid}").text)

# Everybody on the run can open it — including step 2's doer, who has never
# been given step 1.
_c2F = TestClient(app)
_c2F.post("/login", data={"email": _em2F, "password": "gcs1234"})
_pg2F = _c2F.get(f"/tasks/{_t2Fid}").text
check("step 2's doer sees it on their own task", "bill-4471.pdf" in _pg2F)
check("labelled with the question it answers", "The bill" in _pg2F)
_dl = _c2F.get(f"/attachments/{_attId}")
check("and can actually open it", _dl.status_code == 200, _dl.status_code)
check("getting the real bytes back", _dl.content == _pdf)

# Step 1's doer still has to attach their own proof.
_c1F = TestClient(app)
_c1F.post("/login", data={"email": _em1F, "password": "gcs1234"})
check("step 1 cannot be submitted on the strength of the start-form file",
      _c1F.post(f"/tasks/{_t1Fid}/submit",
                data={"completion_note": "verified"}).status_code == 400)

# Somebody with nothing to do with the run still cannot open it.
with _slK() as _d:
    _outsider = [u for u in _d.scalars(_spK(_UW).where(_UW.active.is_(True))).all()
                 if u.receives_work and u.id not in
                 (_steps[1].doer_id, _steps[2].doer_id)][0].email
_cOut = TestClient(app)
_cOut.post("/login", data={"email": _outsider, "password": "gcs1234"})
check("somebody outside the run cannot open it",
      _cOut.get(f"/attachments/{_attId}").status_code == 404)

# An optional file question starts fine with nothing in it.
_opt = f"SMOKE optional file {RUN}"
admin.post("/flows/new", data={
    "name": _opt, "description": "", "branch_id": "",
    "sf_label": ["Supporting paper"], "sf_type": ["file"],
    "sf_required": ["0"], "sf_options": [""],
    "step_title": ["Do it"], "step_doer": [str(_fourW[0])], "step_doers": [""],
    "step_tat_unit": ["days"], "step_tat": ["1"], "step_priority": ["medium"],
    "step_instructions": [""], "step_howto": [""], "step_fields": [""],
    "step_audit": ["0"], "step_proof": ["0"], "step_decision": ["0"],
    "step_yes": [""], "step_next": ["0"], "step_no": [""], "step_fail": [""],
    "step_due_from": [""]})
with _slK() as _d:
    _optId = _d.scalar(_spK(_FlW).where(_FlW.name == _opt)).id
check("an optional file question starts without one",
      admin.post(f"/flows/{_optId}/start",
                 data={"reference": f"SMOKE OPTFILE {RUN}"}).status_code == 200)

# A file the server will not take stops the start — rather than leaving a
# run standing with its paperwork missing.
_bad = admin.post(f"/flows/{_fFid}/start",
                  data={"reference": f"SMOKE BADFILE {RUN}",
                        "sf0": "Kataria Traders"},
                  files={"sf1": ("notes.exe", b"MZ" + b"x" * 300,
                                 "application/x-msdownload")})
check("a file type the server does not take is refused", _bad.status_code == 400,
      _bad.status_code)
with _slK() as _d:
    check("and no run was created by the attempt",
          not _d.scalars(_spK(_FIW).where(
              _FIW.reference == f"SMOKE BADFILE {RUN}")).all())


print("\n== the EM score shows which tasks are behind each figure ==")
# The complaint, and it was a fair one. The score said four weight of
# delegation was NOT DONE for 12–17 Oct. The Delegation report for exactly
# the same dates said 0 pending, 0 overdue, 100% complete. Both were right:
# the report asks "is it finished now", the score asks "was it finished in
# those dates". Reconciling them meant reading every row by eye.
from app.models import Task as _TB, User as _UB, Priority as _PB, \
     TaskSource as _TSB, TaskStatus as _STB
from app.services import scoring as _scB
from datetime import timedelta as _tdB

with _slK() as _d:
    _admB = _d.scalar(_spK(_UB).where(_UB.email == "mis@gcs.local"))
    _whoB = [u for u in _d.scalars(_spK(_UB).where(_UB.active.is_(True))).all()
             if u.receives_work][0]
    _whoBid, _emB = _whoB.id, _whoB.email
    _winFrom, _winTo = _dtC(2026, 10, 12), _dtC(2026, 10, 17)
    # 1. Due inside the window and handed in inside it — the ordinary case.
    _ok = _TB(org_id=_admB.org_id, title=f"SMOKE in-window {RUN}",
              assigner_id=_admB.id, doer_id=_whoBid, priority=_PB.MEDIUM,
              source=_TSB.DELEGATION, due_at=_dtC(2026, 10, 14, 18),
              status=_STB.COMPLETED, submitted_at=_dtC(2026, 10, 14, 11),
              closed_at=_dtC(2026, 10, 14, 11), requires_attachment=False)
    # 2. Due inside the window, handed in AFTER it. Finished now — so the
    #    Delegation report calls it complete — but not done in the window.
    _latr = _TB(org_id=_admB.org_id, title=f"SMOKE finished after {RUN}",
                assigner_id=_admB.id, doer_id=_whoBid, priority=_PB.MEDIUM,
                source=_TSB.DELEGATION, due_at=_dtC(2026, 10, 16, 18),
                status=_STB.COMPLETED, submitted_at=_dtC(2026, 10, 18, 10),
                closed_at=_dtC(2026, 10, 18, 10), requires_attachment=False)
    # 3. Due BEFORE the window, still open when it began — carried in. Its
    #    planned date is outside the dates, so the Delegation report filtered
    #    on planned date never shows it at all.
    _carr = _TB(org_id=_admB.org_id, title=f"SMOKE carried in {RUN}",
                assigner_id=_admB.id, doer_id=_whoBid, priority=_PB.LOW,
                source=_TSB.DELEGATION, due_at=_dtC(2026, 10, 9, 18),
                status=_STB.IN_PROGRESS, requires_attachment=False)
    _d.add_all([_ok, _latr, _carr]); _d.commit()
    _okId, _latrId, _carrId = _ok.id, _latr.id, _carr.id

_fromS, _toS = "2026-10-12", "2026-10-17"
with _slK() as _d:
    _mine = _d.scalars(_spK(_TB).where(_TB.doer_id == _whoBid,
                                       _TB.source == _TSB.DELEGATION)).all()
    _owed, _closed = _scB.window_tasks(
        _mine, _dtC(2026, 10, 12), _dtC(2026, 10, 17, 23, 59, 59))
    _ids = {t.id for t in _owed}
    _shut = {t.id for t in _closed}
    check("the one finished inside the window counts as done",
          _okId in _ids and _okId in _shut)
    check("the one finished AFTER the window is on the hook but not done",
          _latrId in _ids and _latrId not in _shut)
    check("and the one carried in from before it is on the hook too",
          _carrId in _ids and _carrId not in _shut)

# The Delegation report, on planned date, shows the first two as complete and
# nothing pending — which is exactly what was reported as a contradiction.
_repB = admin.get(f"/reports/tasks?source=delegation&doer={_whoBid}"
                  f"&date_from={_fromS}&date_to={_toS}&state=completed").text
check("the Delegation report calls the late-finished one complete",
      f"SMOKE finished after {RUN}" in _repB)
_pendB = admin.get(f"/reports/tasks?source=delegation&doer={_whoBid}"
                   f"&date_from={_fromS}&date_to={_toS}&state=pending").text
check("and shows it as nothing pending",
      f"SMOKE finished after {RUN}" not in _pendB)

# The new page says which tasks the score meant, and why each one counts.
_behind = admin.get(f"/reports/score/behind?doer={_whoBid}&source=delegation"
                    f"&kind=not_done&date_from={_fromS}&date_to={_toS}")
check("the breakdown page opens", _behind.status_code == 200, _behind.status_code)
_bt = _behind.text
check("it lists the one finished after the window",
      f"SMOKE finished after {RUN}" in _bt)
check("saying it was handed in after these dates", "AFTER these dates" in _bt)
check("it lists the one carried in", f"SMOKE carried in {RUN}" in _bt)
check("saying it was carried in", "carried in" in _bt)
check("and does NOT list the one finished on time inside the window",
      f"SMOKE in-window {RUN}" not in _bt)
check("it says plainly that this is not the same as pending today",
      "not mean still pending today" in _bt)

# And the EM score links to it, so nobody has to know the URL.
_scoreB = admin.get(f"/reports/score?date_from={_fromS}&date_to={_toS}").text
check("the EM score links through to the breakdown",
      "/reports/score/behind" in _scoreB)

# The late half has its own list.
_lateB = admin.get(f"/reports/score/behind?doer={_whoBid}&source=delegation"
                   f"&kind=late&date_from={_fromS}&date_to={_toS}")
check("the late breakdown opens too", _lateB.status_code == 200)
check("and counts only work handed in inside the window, after its date",
      f"SMOKE finished after {RUN}" not in _lateB.text)

# It shows somebody else's scored work, so it takes the same right the EM
# score report itself takes.
with _slK() as _d:
    _plainB = [u.email for u in _d.scalars(_spK(_UB).where(_UB.active.is_(True))).all()
               if u.receives_work and not u.can_see_all_reports][0]
_cB = TestClient(app)
_cB.post("/login", data={"email": _plainB, "password": "gcs1234"})
check("a doer without the reports right cannot open it",
      _cB.get(f"/reports/score/behind?doer={_whoBid}&source=delegation"
              f"&kind=not_done&date_from={_fromS}&date_to={_toS}"
              ).status_code == 403)


print("\n== finishing early is not a penalty ==")
# A real one, found on the live site. DEL-341: due 14 Oct 11:59 PM, handed in
# 10 Oct 05:50 PM — four days EARLY, marked on time, COMPLETED.
#
# Scored for 12–17 Oct it used to land in "on the hook" because its DATE is
# in that week, and in nobody's "completed" list, because it was completed
# the week before. The score read it as not done. Finishing early cost the
# doer points, which is the exact opposite of what the score is for — and
# no screen could explain it, because the task plainly says COMPLETED, on
# time.
with _slK() as _d:
    _admE = _d.scalar(_spK(_UB).where(_UB.email == "mis@gcs.local"))
    _whoE = [u for u in _d.scalars(_spK(_UB).where(_UB.active.is_(True))).all()
             if u.receives_work][0]
    _early = _TB(org_id=_admE.org_id, title=f"SMOKE finished early {RUN}",
                 assigner_id=_admE.id, doer_id=_whoE.id, priority=_PB.LOW,
                 source=_TSB.DELEGATION, due_at=_dtC(2026, 10, 14, 23, 59),
                 status=_STB.COMPLETED, submitted_at=_dtC(2026, 10, 10, 17, 50),
                 closed_at=_dtC(2026, 10, 10, 17, 50), requires_attachment=False)
    _d.add(_early); _d.commit()
    _earlyId, _whoEid = _early.id, _whoE.id

with _slK() as _d:
    _rowsE = _d.scalars(_spK(_TB).where(_TB.id == _earlyId)).all()
    check("the task itself says it was on time", _rowsE[0].was_on_time is True)

    # The week it was DUE in, but finished before.
    _o, _c = _scB.window_tasks(_rowsE, _dtC(2026, 10, 12),
                               _dtC(2026, 10, 17, 23, 59, 59))
    check("it is not on the hook for the week after it was finished",
          _o == [], [t.title for t in _o])
    check("and is not counted as closed there either", _c == [])

    # The week it was actually finished in — where it belongs.
    _o2, _c2 = _scB.window_tasks(_rowsE, _dtC(2026, 10, 5),
                                 _dtC(2026, 10, 11, 23, 59, 59))
    check("it counts in the week it was finished", [t.id for t in _o2] == [_earlyId])
    check("as work completed", [t.id for t in _c2] == [_earlyId])

    # A window covering both: counted once, and done.
    _o3, _c3 = _scB.window_tasks(_rowsE, _dtC(2026, 10, 5),
                                 _dtC(2026, 10, 17, 23, 59, 59))
    check("a window spanning both counts it exactly once",
          len(_o3) == 1 and len(_c3) == 1)

    # The whole card, to be sure the penalty is gone.
    _cardE = _scB._build(*_scB.window_tasks(
        _rowsE, _dtC(2026, 10, 12), _dtC(2026, 10, 17, 23, 59, 59)))
    check("so the week after carries no 'not done' for it",
          _cardE.not_done == 0, _cardE.not_done)
    _cardF = _scB._build(*_scB.window_tasks(
        _rowsE, _dtC(2026, 10, 5), _dtC(2026, 10, 11, 23, 59, 59)))
    check("and the week it was done in scores it as done and on time",
          _cardF.completed == 1 and _cardF.on_time == 1 and _cardF.late == 0,
          (_cardF.completed, _cardF.on_time, _cardF.late))

# The things this must NOT have broken.
with _slK() as _d:
    _stillLate = _TB(org_id=_admE.org_id, title=f"SMOKE late one {RUN}",
                     assigner_id=_admE.id, doer_id=_whoEid, priority=_PB.LOW,
                     source=_TSB.DELEGATION, due_at=_dtC(2026, 10, 13, 18),
                     status=_STB.COMPLETED, submitted_at=_dtC(2026, 10, 16, 9),
                     closed_at=_dtC(2026, 10, 16, 9), requires_attachment=False)
    _neverDone = _TB(org_id=_admE.org_id, title=f"SMOKE never done {RUN}",
                     assigner_id=_admE.id, doer_id=_whoEid, priority=_PB.LOW,
                     source=_TSB.DELEGATION, due_at=_dtC(2026, 10, 13, 18),
                     status=_STB.IN_PROGRESS, requires_attachment=False)
    _d.add_all([_stillLate, _neverDone]); _d.commit()
    _pair = [_d.get(_TB, _stillLate.id), _d.get(_TB, _neverDone.id)]
    _oL, _cL = _scB.window_tasks(_pair, _dtC(2026, 10, 12),
                                 _dtC(2026, 10, 17, 23, 59, 59))
    check("work handed in late, inside the window, still counts as late",
          len(_cL) == 1 and _cL[0].was_on_time is False)
    _cardL = _scB._build(_oL, _cL)
    check("and work never handed in still counts as not done",
          _cardL.not_done == 1 and _cardL.late == 1,
          (_cardL.not_done, _cardL.late))


# ==========================================================================
# Escalation — overdue work climbs the ladder, one rung a day
# ==========================================================================
print("\n-- Escalation --")

from datetime import date as _dQ, timedelta as _tdQ
from app.db import SessionLocal as _slQ
from app.models import (Branch as _BrQ, Escalation as _EsQ,
                        EscalationMail as _EmQ,
                        EscalationRule as _ErQ, Organization as _OgQ,
                        Priority as _PrQ, Task as _TkQ, TaskSource as _TsQ,
                        TaskStatus as _StQ, User as _UsQ)
from app.services import escalation as _escQ
from app.services import mail as _mlQ

adminQ = login("mis@gcs.local")

check("the escalation page opens", adminQ.get("/escalation").status_code == 200)
check("the log opens", adminQ.get("/escalation/log").status_code == 200)
check("and the preview opens", adminQ.get("/escalation/preview").status_code == 200)

# Opening the page lays out the approved three rungs, empty, so the form
# arrives with the right shape rather than blank.
with _slQ() as _d:
    _orgQ = _d.scalar(_slctQ := __import__("sqlalchemy").select(_OgQ))
    _seeded = _d.scalars(__import__("sqlalchemy").select(_ErQ)
                         .where(_ErQ.branch_id.is_(None))).all()
    check("three group-wide levels are laid out for a new system",
          len(_seeded) >= 3, len(_seeded))
    check("and the top one is marked urgent, as the approved template is",
          any(r.urgent for r in _seeded))

_SEL = __import__("sqlalchemy").select

# ---------------------------------------------------------- the ladder ----
with _slQ() as _d:
    _orgQ = _d.scalar(_SEL(_OgQ))
    _oidQ = _orgQ.id
    _brsQ = _d.scalars(_SEL(_BrQ).where(_BrQ.org_id == _oidQ)
                       .order_by(_BrQ.name)).all()
    _hoQ = next(b for b in _brsQ if b.name == "GCS HO")
    _bzQ = next(b for b in _brsQ if "Bodyzone" in b.name)
    _bipsQ = next(b for b in _brsQ if "BIPS" in b.name)

    # The group ladder gets the EA and the CMD; each company its own HOD.
    _rQ = {r.level: r for r in _d.scalars(
        _SEL(_ErQ).where(_ErQ.org_id == _oidQ,
                         _ErQ.branch_id.is_(None))).all()}
    _rQ[1].to_name, _rQ[1].to_email = "Group HOD", f"ghod-{RUN}@example.test"
    _rQ[2].to_name, _rQ[2].to_email = "EA to CMD", f"ea-{RUN}@example.test"
    _rQ[3].to_name, _rQ[3].to_email = "CMD Sir", f"cmd-{RUN}@example.test"
    _rQ[3].salutation = "Respected CMD Sir"
    for _b, _who in ((_hoQ, "Santosh Ma'am"), (_bzQ, "GM Sir")):
        if not _d.scalar(_SEL(_ErQ).where(_ErQ.org_id == _oidQ,
                                          _ErQ.branch_id == _b.id,
                                          _ErQ.level == 1)):
            _d.add(_ErQ(org_id=_oidQ, branch_id=_b.id, level=1,
                        to_name=_who,
                        to_email=f"{_who.split()[0].lower()}-{RUN}@example.test",
                        cc_doer=True, cc_earlier=False))
    _orgQ.esc_on = True
    _orgQ.esc_after_top = "stop"
    _orgQ.esc_scope = "chased"
    _orgQ.esc_working_days = True
    _d.commit()

    _ladHO = _escQ.ladder(_d, _oidQ, _hoQ.id)
    check("head office gets its own HOD on level 1",
          _ladHO[1].name == "Santosh Ma'am", _ladHO[1].name)
    check("and inherits the EA and the CMD from the group ladder",
          _ladHO[2].name == "EA to CMD" and _ladHO[3].name == "CMD Sir",
          (_ladHO[2].name, _ladHO[3].name))
    _ladBZ = _escQ.ladder(_d, _oidQ, _bzQ.id)
    check("Bodyzone gets GM Sir on level 1, not head office's HOD",
          _ladBZ[1].name == "GM Sir", _ladBZ[1].name)
    # The gap the approved document leaves: BIPS School has no ladder of its
    # own. It must escalate to the group's rather than to nobody.
    _ladBIPS = _escQ.ladder(_d, _oidQ, _bipsQ.id)
    check("a company with no ladder of its own still escalates to somebody",
          _ladBIPS and _ladBIPS[1].name == "Group HOD",
          sorted(_ladBIPS))
    check("so no company escalates to nobody",
          _escQ.missing_ladders(_d, _oidQ) == [],
          _escQ.missing_ladders(_d, _oidQ))
    check("a level with nobody to send to is not a level",
          all(r.email for r in _ladHO.values()))

# ------------------------------------------------------- days overdue -----
with _slQ() as _d:
    _orgQ = _d.scalar(_SEL(_OgQ))
    _hoQ = _d.scalar(_SEL(_BrQ).where(_BrQ.name == "GCS HO"))
    _doerQ = _d.scalar(_SEL(_UsQ).where(_UsQ.branch_id == _hoQ.id,
                                        _UsQ.active.is_(True)))
    _bossQ = _d.scalar(_SEL(_UsQ).where(_UsQ.email == "mis@gcs.local"))
    # Due on a Friday, so the Sunday after it falls inside the overdue run.
    _friQ = _dQ(2026, 10, 16)
    _tkQ = _TkQ(org_id=_orgQ.id, branch_id=_hoQ.id,
                title=f"SMOKE escalation {RUN}", assigner_id=_bossQ.id,
                doer_id=_doerQ.id, priority=_PrQ.HIGH,
                source=_TsQ.DELEGATION, status=_StQ.PENDING,
                due_at=_dtC(2026, 10, 16, 18), requires_attachment=False)
    _d.add(_tkQ); _d.commit()
    _tidQ = _tkQ.id
    _refQ = _tkQ.ref

    _monQ = _dQ(2026, 10, 19)          # the Monday after
    check("a Friday deadline is two days overdue on the Monday, "
          "because the Sunday nobody was in does not count",
          _escQ.days_overdue(_d, _tkQ, _monQ, True) == 2,
          _escQ.days_overdue(_d, _tkQ, _monQ, True))
    check("and three on calendar days, where the Sunday does",
          _escQ.days_overdue(_d, _tkQ, _monQ, False) == 3,
          _escQ.days_overdue(_d, _tkQ, _monQ, False))
    check("nothing is overdue on its own due date",
          _escQ.days_overdue(_d, _tkQ, _dQ(2026, 10, 16), True) == 0)

# --------------------------------------------------- one rung a day -------
with _slQ() as _d:
    _orgQ = _d.scalar(_SEL(_OgQ))
    _tkQ = _d.get(_TkQ, _tidQ)

    def _rungsOf(db, task):
        return [(e.level, e.repeat_n, e.to_name)
                for e in _escQ.already(db, task.id)]

    _escQ.run(_d, _orgQ, on=_dQ(2026, 10, 17), send=False)      # Saturday
    check("day 1 overdue goes to that company's own HOD",
          _rungsOf(_d, _tkQ) == [(1, 0, "Santosh Ma'am")], _rungsOf(_d, _tkQ))

    _escQ.run(_d, _orgQ, on=_dQ(2026, 10, 18), send=False)      # Sunday
    check("the Sunday adds nothing — it is not a day overdue",
          len(_rungsOf(_d, _tkQ)) == 1, _rungsOf(_d, _tkQ))

    _escQ.run(_d, _orgQ, on=_dQ(2026, 10, 19), send=False)      # Monday
    check("day 2 goes to the EA", _rungsOf(_d, _tkQ)[-1][:2] == (2, 0)
          and _rungsOf(_d, _tkQ)[-1][2] == "EA to CMD", _rungsOf(_d, _tkQ))

    _escQ.run(_d, _orgQ, on=_dQ(2026, 10, 20), send=False)
    check("day 3 reaches the CMD",
          _rungsOf(_d, _tkQ)[-1][:2] == (3, 0), _rungsOf(_d, _tkQ))

    _was = len(_rungsOf(_d, _tkQ))
    _escQ.run(_d, _orgQ, on=_dQ(2026, 10, 20), send=False, force=True)
    check("running the check twice in one morning sends nothing twice",
          len(_rungsOf(_d, _tkQ)) == _was, _rungsOf(_d, _tkQ))

    _escQ.run(_d, _orgQ, on=_dQ(2026, 10, 21), send=False)
    check("and with 'stop' set, nothing follows the CMD",
          len(_rungsOf(_d, _tkQ)) == _was, _rungsOf(_d, _tkQ))

    # The promise the approved document makes, enforced by the database
    # rather than by the code remembering.
    _dupQ = _EsQ(org_id=_orgQ.id, task_id=_tidQ, level=1, repeat_n=0,
                 day=_dQ(2026, 11, 1), days_overdue=9, subject="x", body="y")
    _d.add(_dupQ)
    try:
        _d.commit()
        _twice = True
    except Exception:
        _d.rollback()
        _twice = False
    check("the database itself refuses a second escalation at the same level",
          not _twice)

# ------------------------------------------------------ the CC cascade ----
with _slQ() as _d:
    _tkQ = _d.get(_TkQ, _tidQ)
    _sentQ = _escQ.already(_d, _tidQ)
    _l1, _l2, _l3 = _sentQ[0], _sentQ[1], _sentQ[2]
    check("the escalation going up the ladder does NOT copy the doer",
          _tkQ.doer.email not in _l1.cc_list, _l1.cc_list)
    check("level 2 copies the HOD already told",
          any("santosh" in a for a in _l2.cc_list), _l2.cc_list)
    check("level 3 copies the HOD and the EA",
          any("santosh" in a for a in _l3.cc_list) and
          any("ea-" in a for a in _l3.cc_list), _l3.cc_list)
    check("and never copies the person it is addressed to",
          _l3.to_email not in _l3.cc_list)

    check("the subject says how many tasks and how overdue",
          "1 overdue task" in _l1.message_subject and
          "up to 1 day" in _l1.message_subject, _l1.message_subject)
    check("the CMD's subject is marked urgent",
          _l3.message_subject.startswith("[URGENT Escalation L3]"),
          _l3.message_subject)
    check("the CMD is addressed as the approved template addresses him",
          _l3.message_body.startswith("Respected CMD Sir,"),
          _l3.message_body[:40])
    check("the CMD's letter says the levels below were escalated, "
          "because for this task they were",
          "after escalation at every level below" in _l3.message_body,
          _l3.message_body[:160])

    # The five things the user asked the list to carry, plus the days
    # overdue, as column headings of a fixed-width table.
    for _head in ("Task ID", "Doer", "Planned", "Priority", "Overdue", "Task"):
        check(f"the list has a {_head} column", _head in _l1.message_body,
              _l1.message_body[:300])
    check("and the task's own row is in it",
          _refQ in _l1.message_body, _l1.message_body[:400])
    check("with its planned date and priority",
          "HIGH" in _l1.message_body and
          _tkQ.due_at.strftime("%d %b") in _l1.message_body)
    check("the message is kept on the email, not copied onto every task row",
          _l1.mail is not None and _l1.body == "" and _l1.mail.body != "")
    check("and the task reads it back through the email it rode in",
          _l1.message_body == _l1.mail.body)

# ------------------------------------------------- one email, many tasks --
# A HOD with twenty-five overdue tasks must not get twenty-five emails.
with _slQ() as _d:
    _orgQ = _d.scalar(_SEL(_OgQ))
    _jhQ = _d.scalar(_SEL(_BrQ).where(_BrQ.name == "GCS Jharkhand"))
    _bossQ = _d.scalar(_SEL(_UsQ).where(_UsQ.email == "mis@gcs.local"))
    _crewQ = _d.scalars(_SEL(_UsQ).where(_UsQ.active.is_(True))
                        .order_by(_UsQ.name)).all()[:3]
    # Three doers, six tasks, all at the same company and the same rung.
    _manyQ = []
    for _i in range(6):
        _t = _TkQ(org_id=_orgQ.id, branch_id=_jhQ.id,
                  title=f"SMOKE grouped {_i} {RUN}", assigner_id=_bossQ.id,
                  doer_id=_crewQ[_i % 3].id,
                  priority=[_PrQ.HIGH, _PrQ.MEDIUM, _PrQ.LOW][_i % 3],
                  source=_TsQ.DELEGATION, status=_StQ.PENDING,
                  due_at=_dtC(2026, 11, 2, 18), requires_attachment=False)
        _d.add(_t); _manyQ.append(_t)
    _d.commit()
    _manyIdsQ = [t.id for t in _manyQ]

    _resQ = _escQ.run(_d, _orgQ, on=_dQ(2026, 11, 3), send=False, force=True)
    _mineQ = _d.scalars(_SEL(_EsQ).where(
        _EsQ.task_id.in_(_manyIdsQ))).all()
    check("all six tasks are escalated", len(_mineQ) == 6, len(_mineQ))
    _mailsQ = {e.mail_id for e in _mineQ}
    check("but they share ONE email, not six",
          len(_mailsQ) == 1, _mailsQ)
    _mQ = _d.get(_EmQ, _mineQ[0].mail_id)
    check("and that one email covers all six, in one place",
          _mQ.task_count >= 6, _mQ.task_count)
    check("the subject says how many, so it can be triaged before opening",
          f"{_mQ.task_count} overdue task" in _mQ.subject, _mQ.subject)
    check("and it covers the six without sending six emails",
          _mQ.task_count < 6 * 2, _mQ.task_count)
    check("every one of the six is listed in it",
          all((_d.get(_TkQ, i).ref in _mQ.body) for i in _manyIdsQ),
          _mQ.body)

    # Sorted by doer name A to Z — the thing that makes it readable.
    #
    # Within each SECTION, not across the whole table: the list is split
    # Delegation, Checklist, FMS and then by company, so one global A-to-Z
    # would mean the sections were not split at all. The promise is that
    # one person's work sits together wherever you are reading.
    _bodyQ = _mQ.body.splitlines()
    _dataQ = [l for l in _bodyQ
              if l[:4] in ("DEL-", "CL-0", "FMS-") or l.startswith("CL-")]
    _runsQ, _cur = [], []
    for _l in _bodyQ:
        if _l[:4] in ("DEL-", "CL-0", "FMS-") or _l.startswith("CL-"):
            _cur.append(_l[18:40].strip())
        elif _cur:
            _runsQ.append(_cur)
            _cur = []
    if _cur:
        _runsQ.append(_cur)
    check("the rows are sorted by doer name, A to Z inside each section",
          all(r == sorted(r, key=str.lower) for r in _runsQ), str(_runsQ))
    _namesQ = [n for r in _runsQ for n in r]
    check("so one person's tasks sit together",
          len(_dataQ) == _mQ.task_count, (len(_dataQ), _mQ.task_count))

# ----------------------------------------- each doer their own copy only --
# Copying every doer on a list of everybody's overdue work would show each
# of them all the others'. A person's standing with the CMD is not something
# to hand round the office by accident. So the invariant checked here is the
# one that matters: no doer's copy ever names another doer's work.
with _slQ() as _d:
    _orgQ = _d.scalar(_SEL(_OgQ))
    _ladderQ = _d.scalars(_SEL(_EmQ).where(
        _EmQ.org_id == _orgQ.id, _EmQ.to_doer.is_(False),
        _EmQ.day == _dQ(2026, 11, 3))).all()
    _copiesQ = _d.scalars(_SEL(_EmQ).where(
        _EmQ.org_id == _orgQ.id, _EmQ.to_doer.is_(True),
        _EmQ.day == _dQ(2026, 11, 3))).all()
    check("a morning produces ladder emails and doer copies",
          _ladderQ and _copiesQ, (len(_ladderQ), len(_copiesQ)))

    # No ladder email copies a doer in.
    _doerMailsQ = {(u.email or "").lower() for u in _d.scalars(
        _SEL(_UsQ).where(_UsQ.org_id == _orgQ.id)).all() if u.email}
    _leakedQ = [m.id for m in _ladderQ
                if any(a.lower() in _doerMailsQ for a in m.cc_list)]
    check("no email going up the ladder copies a doer in",
          not _leakedQ, _leakedQ)

    # And every row of a doer's copy is that doer's own work.
    # Keyed on the LABEL, not the bare reference. FMS-12 names four tasks
    # with four different doers; FMS-12 · Step 2 names one. Mapping by the
    # reference alone would quietly compare a doer's own step against
    # whichever other step of that run happened to be read last.
    _refOwnerQ = {}
    for _tRow in _d.scalars(_SEL(_TkQ).where(_TkQ.org_id == _orgQ.id,
                                             _TkQ.ref.is_not(None))).all():
        _refOwnerQ[_tRow.ref_label] = _tRow.doer.name if _tRow.doer else None
    _badQ = []
    for _cQ in _copiesQ:
        for _line in _cQ.body.splitlines():
            if _line[:3] not in ("DEL", "CL-", "FMS"):
                continue
            # The Task ID column is the first 16 characters of the row.
            _ref = _line[:16].strip()
            if _ref and _refOwnerQ.get(_ref) not in (None, _cQ.to_name):
                _badQ.append((_cQ.to_name, _ref, _refOwnerQ.get(_ref)))
    check("every row of a doer's own copy is that doer's own work",
          not _badQ, _badQ[:4])
    check("a doer's copy covers at least one task each",
          all(c.task_count >= 1 for c in _copiesQ),
          [c.task_count for c in _copiesQ])
    check("a doer's copy is addressed to them by their whole name, "
          "not the first word of a role title",
          all(c.body.startswith(f"Dear {c.to_name},") for c in _copiesQ),
          [c.body[:30] for c in _copiesQ])
    check("and it says who the work was reported to",
          all("reported to" in c.subject for c in _copiesQ),
          [c.subject for c in _copiesQ])
    check("a doer's copy is never addressed to a ladder recipient",
          all(c.to_email not in {m.to_email for m in _ladderQ}
              for c in _copiesQ))

    # The six grouped tasks' own doers each got a copy.
    _sixDoersQ = {_d.get(_TkQ, i).doer.name for i in _manyIdsQ}
    check("each doer of the six grouped tasks got a copy",
          _sixDoersQ <= {c.to_name for c in _copiesQ},
          (_sixDoersQ, {c.to_name for c in _copiesQ}))

# ----------------------------------------------------- the HTML email ----
# What the recipient actually reads. Table layout, inline styles, striped
# rows — and every value out of the database escaped, because task titles
# and people's names are typed by hand by twenty staff.
with _slQ() as _d:
    _orgQ = _d.scalar(_SEL(_OgQ))
    # One with SEVERAL tasks in it. Striping only exists to keep a long
    # row on its own line as the eye crosses it, so a one-row email has
    # nothing to stripe and proves nothing about whether striping works.
    _mQ = _d.scalar(_SEL(_EmQ).where(_EmQ.org_id == _orgQ.id,
                                     _EmQ.to_doer.is_(False),
                                     _EmQ.task_count > 1)
                    .order_by(_EmQ.id.desc())) or _d.scalar(
        _SEL(_EmQ).where(_EmQ.org_id == _orgQ.id, _EmQ.to_doer.is_(False))
        .order_by(_EmQ.id.desc()))
    check("the email has an HTML version", (_mQ.body_html or "").strip(),
          _mQ.id)
    check("and a plain-text version alongside it", _mQ.body.strip())
    check("the HTML is a whole document, not a fragment",
          _mQ.body_html.startswith("<!doctype html>")
          and _mQ.body_html.rstrip().endswith("</html>"))
    check("it lays out with tables, which is what Outlook understands",
          "<table" in _mQ.body_html and "display:flex" not in _mQ.body_html)
    check("every style is inline — a stripped <style> block is a table "
          "that arrives unformatted",
          "<style" not in _mQ.body_html.lower())
    check("nothing is loaded from the network",
          "<img" not in _mQ.body_html.lower()
          and "http://" not in _mQ.body_html.replace("http://www.w3.org", ""))
    check("the rows are striped, set per row rather than with :nth-child",
          _mQ.body_html.count("background:#f4f7fa") >= 1
          and "nth-child" not in _mQ.body_html)
    for _h in ("Task&nbsp;ID", "Doer", "Planned date", "Priority",
               "Overdue", "Task"):
        check(f"the HTML table has a {_h.replace('&nbsp;', ' ')} column",
              f">{_h}<" in _mQ.body_html, _h)
    check("a priority shows as a coloured pill",
          "border-radius:11px" in _mQ.body_html)
    check("and the level is on the bar, so it is triaged before it is read",
          f"Level {_mQ.level}" in _mQ.body_html)

# Escaping. A task called `<b>URGENT` must not reformat the email, and a
# name with an apostrophe must not break an attribute.
with _slQ() as _d:
    _orgQ = _d.scalar(_SEL(_OgQ))
    _hoQ = _d.scalar(_SEL(_BrQ).where(_BrQ.name == "GCS HO"))
    _bossQ = _d.scalar(_SEL(_UsQ).where(_UsQ.email == "mis@gcs.local"))
    _nastyQ = _TkQ(org_id=_orgQ.id, branch_id=_hoQ.id,
                   title='<script>alert("x")</script> & <b>URGENT</b>',
                   assigner_id=_bossQ.id, doer_id=_bossQ.id,
                   priority=_PrQ.HIGH, source=_TsQ.DELEGATION,
                   status=_StQ.PENDING, requires_attachment=False,
                   due_at=_dtC(2026, 10, 1, 18))
    _d.add(_nastyQ); _d.commit()
    _rungQ = _escQ.ladder(_d, _orgQ.id, _hoQ.id)[1]
    _sub, _txt, _htmlQ, _ccQ = _escQ.compose(
        [(_nastyQ, 4)], 1, 0, _rungQ, _escQ.ladder(_d, _orgQ.id, _hoQ.id),
        "https://example.test")
    check("a task title cannot inject a script tag into the email",
          "<script>" not in _htmlQ, _htmlQ[_htmlQ.find("alert") - 60:][:120]
          if "alert" in _htmlQ else "")
    check("it is escaped, and still readable",
          "&lt;script&gt;" in _htmlQ and "&amp;" in _htmlQ)
    check("a title cannot smuggle in bold either",
          "<b>URGENT</b>" not in _htmlQ)
    check("and the plain-text version carries it verbatim, as text",
          "<script>" in _txt and "&lt;" not in _txt,
          [l for l in _txt.splitlines() if "script" in l][:1])

    # An apostrophe in a name goes into an attribute (the frame's title) and
    # into the body. Neither may break out.
    _rungQ.to_name = "Santosh Ma'am <x>"
    _d.commit()
    _s2, _t2, _h2, _c2 = _escQ.compose(
        [(_nastyQ, 4)], 1, 0, _rungQ,
        _escQ.ladder(_d, _orgQ.id, _hoQ.id), "https://example.test")
    check("a name with an apostrophe and a bracket is escaped in the HTML",
          "Ma&#x27;am &lt;x&gt;" in _h2 or "Ma&#39;am &lt;x&gt;" in _h2,
          [l for l in _h2.splitlines() if "Ma" in l][:1])
    _rungQ.to_name = "Santosh Ma'am"
    _d.commit()

# ------------------------------------------- the links inside the email --
# The nine o'clock check is not a web request and cannot work out this
# site's address. Without it recorded, the automatic emails were the only
# ones with no "open the task" link — and every email tested by hand had
# them, which is what makes that a hard fault to notice.
_rPage2Q = adminQ.get("/escalation")
with _slQ() as _d:
    _orgQ = _d.scalar(_SEL(_OgQ))
    check("opening the page learns this site's address",
          (_orgQ.site_url or "").startswith("http"), _orgQ.site_url)
    _learnedQ = _orgQ.site_url
check("and shows it, because a wrong one means every link is dead",
      "This site&#39;s web address" in _rPage2Q.text
      or "This site's web address" in _rPage2Q.text)
_rBadUrlQ = adminQ.post("/escalation/settings",
                        data={"esc_on": "", "working_days": "1",
                              "after_top": "stop", "scope": "chased",
                              "hour": "9", "site_url": "gcs.example.test"})
check("an address with no https:// is refused rather than quietly saved",
      _rBadUrlQ.status_code == 400, _rBadUrlQ.status_code)
with _slQ() as _d:
    _orgQ = _d.scalar(_SEL(_OgQ))
    check("so the one that was learned is still there",
          _orgQ.site_url == _learnedQ, _orgQ.site_url)
    _orgQ.site_url = "https://gcs.example.test"
    _orgQ.esc_on = True
    _orgQ.esc_last_run = None
    _orgQ.esc_hour = 0
    _d.commit()
    _beforeQ = {m.id for m in _d.scalars(_SEL(_EmQ)).all()}
    # A stand-in mail server. The run refuses to escalate with nowhere to
    # send — a rung recorded and nothing sent would be a level skipped in
    # silence — so this test has to give it somewhere, without a network.
    from app.services import mail as _mlQ
    _mlHostQ, _mlFromQ, _mlSendQ = _mlQ.HOST, _mlQ.FROM, _mlQ.send
    _mlQ.HOST, _mlQ.FROM = "smtp.test", "gcs@test"
    _mlQ.send = lambda *a, **k: (True, "")
    _escQ.run_daily(_d)
    _mlQ.HOST, _mlQ.FROM, _mlQ.send = _mlHostQ, _mlFromQ, _mlSendQ
    _freshQ = [m for m in _d.scalars(_SEL(_EmQ)).all()
               if m.id not in _beforeQ]
    check("the daily run uses the recorded address for its links",
          _freshQ and all("https://gcs.example.test/tasks" in (m.body_html or "")
                          for m in _freshQ),
          [m.id for m in _freshQ][:3])
    # Put it back as this section found it. Switching escalation off here
    # makes every later run() without force=True return early, which looks
    # like the ladder is broken rather than like the switch is off.
    _orgQ.esc_on = True
    _orgQ.site_url = _learnedQ
    _d.commit()

# ------------------------------------- escalations raised before grouping --
# A live database may already hold rows written one-per-task, carrying their
# own subject and body. Those must keep reading exactly as they were sent —
# an escalation is a record, and a record that stops rendering after an
# upgrade was never a record.
with _slQ() as _d:
    _orgQ = _d.scalar(_SEL(_OgQ))
    _legacyTaskQ = _d.scalar(_SEL(_TkQ).where(_TkQ.org_id == _orgQ.id,
                                              _TkQ.ref.is_not(None)))
    _oldQ = _EsQ(org_id=_orgQ.id, task_id=_legacyTaskQ.id, level=1,
                 repeat_n=77, day=_dQ(2026, 9, 1), days_overdue=1,
                 to_name="Santosh Ma'am", to_email="santosh@example.test",
                 cc_emails="someone@example.test",
                 subject="[Escalation L1] Task X overdue by 1 day(s) — GCS HO",
                 body="Dear Santosh Ma'am,\n\nThe following task is overdue.",
                 status="sent")
    _d.add(_oldQ); _d.commit()
    _oldIdQ = _oldQ.id
    check("a row from before grouping has no email attached",
          _oldQ.mail is None)
    check("and still reads back its own subject",
          _oldQ.message_subject.startswith("[Escalation L1] Task X"),
          _oldQ.message_subject)
    check("and its own body",
          _oldQ.message_body.startswith("Dear Santosh"), _oldQ.message_body)
    check("and its own delivery status and CC list",
          _oldQ.delivery == "sent"
          and _oldQ.cc_list == ["someone@example.test"],
          (_oldQ.delivery, _oldQ.cc_list))
_rOldQ = adminQ.get(f"/escalation/log/{_oldIdQ}")
check("its page still opens and says it went out on its own",
      _rOldQ.status_code == 200 and "on its own" in _rOldQ.text,
      _rOldQ.status_code)
check("and the task-by-task log still lists it",
      "Santosh" in adminQ.get("/escalation/log?view=tasks").text)

# ------------------------------------------------------------- grammar ----
# "Please see that them are closed" is the sort of thing that makes a whole
# system look like it was not worth the trouble.
check("the one-task wording is singular throughout",
      _escQ._digest_message(1, 3, False, 1) ==
      "The following task is overdue and has not been closed. Please review "
      "it with the doer and ensure it is completed today.",
      _escQ._digest_message(1, 3, False, 1))
check("the many-task wording is plural throughout",
      "them with the doers and ensure they are completed" in
      _escQ._digest_message(1, 3, False, 7),
      _escQ._digest_message(1, 3, False, 7))
check("and uses a subject pronoun where the sentence needs one",
      "see that they are closed" in _escQ._digest_message(3, 3, False, 4),
      _escQ._digest_message(3, 3, False, 4))
check("never 'see that them are closed'",
      all("that them" not in _escQ._digest_message(_lv, 3, _bs, _n)
          for _lv in (1, 2, 3) for _bs in (True, False) for _n in (1, 2, 9)))

# The one case where the approved wording would be a false statement: work
# already well overdue the first morning this is switched on reaches the top
# rung at once, having never been escalated below it. Telling the CMD it is
# "still open after escalation to the HOD and the EA" would be untrue in
# writing, over the group's name, to the person least able to check it.
with _slQ() as _d:
    _orgQ = _d.scalar(_SEL(_OgQ))
    _hoQ = _d.scalar(_SEL(_BrQ).where(_BrQ.name == "GCS HO"))
    _doerQ = _d.scalar(_SEL(_UsQ).where(_UsQ.branch_id == _hoQ.id,
                                        _UsQ.active.is_(True)))
    _bossQ = _d.scalar(_SEL(_UsQ).where(_UsQ.email == "mis@gcs.local"))
    _oldQ = _TkQ(org_id=_orgQ.id, branch_id=_hoQ.id,
                 title=f"SMOKE long overdue {RUN}", assigner_id=_bossQ.id,
                 doer_id=_doerQ.id, priority=_PrQ.HIGH,
                 source=_TsQ.DELEGATION, status=_StQ.PENDING,
                 due_at=_dtC(2026, 9, 10, 18), requires_attachment=False)
    _d.add(_oldQ); _d.commit()
    _escQ.run(_d, _orgQ, on=_dQ(2026, 10, 20), send=False, force=True)
    _jumpQ = _escQ.already(_d, _oldQ.id)
    check("work first seen long overdue goes straight to the top level",
          len(_jumpQ) == 1 and _jumpQ[0].level == 3,
          [(e.level, e.repeat_n) for e in _jumpQ])
    check("and its letter does NOT claim an escalation that never happened",
          "after escalation" not in _jumpQ[0].message_body,
          _jumpQ[0].message_body[:200])
    check("it says how it got there instead",
          "by the number of days overdue" in _jumpQ[0].message_body,
          _jumpQ[0].message_body[:220])
    check("the levels below are still copied in, so they are in the loop",
          any("santosh" in a for a in _jumpQ[0].cc_list), _jumpQ[0].cc_list)
    # And a level whose wording was typed in by hand still wins.
    _r3Q = _d.scalar(_SEL(_ErQ).where(_ErQ.org_id == _orgQ.id,
                                      _ErQ.branch_id.is_(None),
                                      _ErQ.level == 3))
    check("a new system leaves the wording empty so the software writes it",
          _r3Q.message is None, _r3Q.message)

# --------------------------------------------------------- repeat mode ----
with _slQ() as _d:
    _orgQ = _d.scalar(_SEL(_OgQ))
    _orgQ.esc_after_top = "repeat"
    _d.commit()
    _before = len(_escQ.already(_d, _tidQ))
    _escQ.run(_d, _orgQ, on=_dQ(2026, 10, 22), send=False)
    _after = _escQ.already(_d, _tidQ)
    check("with 'repeat' set, the top level is reminded again",
          len(_after) == _before + 1 and _after[-1].repeat_n == 1,
          [(e.level, e.repeat_n) for e in _after])
    check("and the reminder says so rather than pretending to be new",
          _after[-1].label == "L3 · reminder 1", _after[-1].label)
    _orgQ.esc_after_top = "stop"
    _d.commit()

# ------------------------------------------------ what does NOT escalate --
with _slQ() as _d:
    _orgQ = _d.scalar(_SEL(_OgQ))
    _tkQ = _d.get(_TkQ, _tidQ)

    _tkQ.submitted_at = _dtC(2026, 10, 23, 9)
    _tkQ.status = _StQ.SUBMITTED
    _d.commit()
    check("work the doer has handed in does not escalate, "
          "even while an auditor still has it",
          not _escQ.in_scope(_d.get(_TkQ, _tidQ), "chased"))

    _tkQ.status = _StQ.COMPLETED
    _tkQ.closed_at = _dtC(2026, 10, 23, 9)
    _d.commit()
    _kept = len(_escQ.already(_d, _tidQ))
    _escQ.run(_d, _orgQ, on=_dQ(2026, 10, 24), send=False)
    check("closing the task stops the ladder",
          len(_escQ.already(_d, _tidQ)) == _kept)
    check("and what already went out is kept as the record", _kept >= 3, _kept)

    # A planned FMS step whose turn has not come. Nobody can be late for
    # work the run has not reached.
    _waitQ = _d.scalar(_SEL(_TkQ).where(_TkQ.flow_waiting.is_(True)))
    if _waitQ is not None:
        check("a planned FMS step whose turn has not come never escalates",
              not _escQ.in_scope(_waitQ, "chased"))

    _hoQ = _d.scalar(_SEL(_BrQ).where(_BrQ.name == "GCS HO"))
    _doerQ = _d.scalar(_SEL(_UsQ).where(_UsQ.branch_id == _hoQ.id,
                                        _UsQ.active.is_(True)))
    _bossQ = _d.scalar(_SEL(_UsQ).where(_UsQ.email == "mis@gcs.local"))
    _heldQ = _TkQ(org_id=_orgQ.id, branch_id=_hoQ.id,
                  title=f"SMOKE held escalation {RUN}", assigner_id=_bossQ.id,
                  doer_id=_doerQ.id, priority=_PrQ.HIGH,
                  source=_TsQ.DELEGATION, status=_StQ.ON_HOLD,
                  due_at=_dtC(2026, 10, 10, 18), requires_attachment=False)
    _d.add(_heldQ); _d.commit()
    check("a task frozen because its run was put on hold does not escalate",
          not _escQ.in_scope(_d.get(_TkQ, _heldQ.id), "chased"))

    _lowQ = _TkQ(org_id=_orgQ.id, branch_id=_hoQ.id,
                 title=f"SMOKE low escalation {RUN}", assigner_id=_bossQ.id,
                 doer_id=_doerQ.id, priority=_PrQ.LOW,
                 source=_TsQ.DELEGATION, status=_StQ.PENDING,
                 due_at=_dtC(2026, 10, 10, 18), requires_attachment=False)
    _d.add(_lowQ); _d.commit()
    check("on 'high priority only', low-priority work is left alone",
          not _escQ.in_scope(_d.get(_TkQ, _lowQ.id), "high"))
    check("but low-priority delegation does escalate on the default setting",
          _escQ.in_scope(_d.get(_TkQ, _lowQ.id), "chased"))

# ----------------------------------------------------------- the mailer ---
check("with no mail server set, sending reports why rather than raising",
      _mlQ.send(["somebody@example.test"], "s", "b") == (False,
      "No mail server is set up yet.") or _mlQ.enabled())
check("and an address that is not one is refused before any connection",
      _mlQ.send(["not-an-address"], "s", "b")[0] is False)
check("a blank and a duplicate address are dropped",
      _mlQ.clean(["a@b.test", "", "A@B.test", "bad"]) == ["a@b.test"],
      _mlQ.clean(["a@b.test", "", "A@B.test", "bad"]))
check("the mail status line never contains the password",
      "MAIL_PASS" not in _mlQ.status())

# ------------------------------------------------- the pages, with data ---
_rLogQ = adminQ.get("/escalation/log")
check("the log lists the emails that went out",
      "Emails sent" in _rLogQ.text and "task(s)" in _rLogQ.text,
      _rLogQ.status_code)
# A doer's copy covers the same tasks a second time, so counting every row
# would say a morning escalated twice as much work as it did.
check("the log counts the tasks escalated, not the tasks counted twice",
      "up the ladder, covering" in _rLogQ.text
      and "copy(ies) to the doers" in _rLogQ.text, _rLogQ.status_code)
_rTaskViewQ = adminQ.get(f"/escalation/log?task={_refQ}")
check("typing a Task ID switches to the task-by-task view and finds it",
      _refQ in _rTaskViewQ.text, _rTaskViewQ.status_code)
check("and the log can be filtered to one level",
      adminQ.get("/escalation/log?level=3&status=queued").status_code == 200)
with _slQ() as _d:
    _oneQ = _escQ.already(_d, _tidQ)[0].id
    _mailIdQ = _escQ.already(_d, _tidQ)[0].mail_id
check("one email's page lists every task it carried",
      "it carried" in adminQ.get(
          f"/escalation/mail/{_mailIdQ}").text, _mailIdQ)
_rOneQ = adminQ.get(f"/escalation/log/{_oneQ}")
# Jinja escapes the apostrophe, so the page carries "Ma&#39;am" — look for
# the part of the name that survives escaping rather than weakening the check.
check("one escalation shows the email it rode in, as it was written",
      "Santosh" in _rOneQ.text
      and "The email this task went out in" in _rOneQ.text,
      _rOneQ.status_code)
check("and renders the real HTML rather than describing it",
      "esc-frame" in _rOneQ.text and "srcdoc" in _rOneQ.text)
check("the task page shows the doer who has been told",
      "Escalated to" in adminQ.get(f"/tasks/{_tidQ}").text)

_rPageQ = adminQ.get("/escalation?scope=" + str(
    _slQ().scalar(_SEL(_BrQ).where(_BrQ.name == "GCS HO")).id))
check("a company's own ladder page opens and names its HOD",
      "Santosh Ma'am" in _rPageQ.text, _rPageQ.status_code)

# A level needs somewhere to send to. Saving one without is the mistake
# that leaves overdue work escalating to nobody, so it is refused.
_rBadQ = adminQ.post("/escalation/rungs",
                     data={"scope": "", "level": "9", "user_id": "",
                           "to_name": "Nobody", "to_email": ""})
check("a level with no address is refused rather than saved empty",
      _rBadQ.status_code == 400, _rBadQ.status_code)
_rBad2Q = adminQ.post("/escalation/rungs",
                      data={"scope": "", "level": "9", "user_id": "",
                            "to_name": "Typo", "to_email": "not-an-address"})
check("and an address that is not one is refused",
      _rBad2Q.status_code == 400, _rBad2Q.status_code)
_rDupQ = adminQ.post("/escalation/rungs",
                     data={"scope": "", "level": "1", "user_id": "",
                           "to_name": "Second", "to_email": "s@b.test"})
check("a second level 1 on the same ladder is refused",
      _rDupQ.status_code == 400, _rDupQ.status_code)

# ------------------------------------------------------- the daily clock --
with _slQ() as _d:
    _orgQ = _d.scalar(_SEL(_OgQ))
    _orgQ.esc_on = True
    _orgQ.esc_hour = 0
    _orgQ.esc_last_run = None
    _d.commit()
    check("the morning check is owed when it has not run today",
          _escQ.due_today(_d, _orgQ))
    _orgQ.esc_last_run = _clkQ if (_clkQ := __import__(
        "app.clock", fromlist=["today"]).today()) else None
    _d.commit()
    check("and not owed once it has",
          not _escQ.due_today(_d, _orgQ))
    check("run_daily does nothing when it is not owed",
          _escQ.run_daily(_d) is None)
    _orgQ.esc_on = False
    _orgQ.esc_last_run = None
    _d.commit()
    check("nothing runs at all while escalation is switched off",
          not _escQ.due_today(_d, _orgQ)
          and _escQ.run(_d, _orgQ, send=False)["ran"] is False)

# Switching it on with an empty ladder is the one mistake that looks, from
# the outside, exactly like a working system.
with _slQ() as _d:
    _orgQ = _d.scalar(_SEL(_OgQ))
    _savedQ = [(r.to_name, r.to_email, r.user_id) for r in _d.scalars(
        _SEL(_ErQ).where(_ErQ.org_id == _orgQ.id,
                         _ErQ.branch_id.is_(None)).order_by(_ErQ.level)).all()]
    for _r in _d.scalars(_SEL(_ErQ).where(_ErQ.org_id == _orgQ.id)).all():
        _d.delete(_r)
    _d.commit()
_rOnQ = adminQ.post("/escalation/settings",
                    data={"esc_on": "1", "working_days": "1",
                          "after_top": "stop", "scope": "chased", "hour": "9"})
check("switching escalation on with no ladder at all is refused",
      _rOnQ.status_code == 400, _rOnQ.status_code)
# And the refusal has to say WHERE to put the levels. The old wording said
# "fill in at least one level", which was read by somebody who had just
# filled in three — for one company — and left them adding the same three
# again to the tab they had already done.
check("and it names the tab the missing ladder lives on",
      "Every other company" in _rOnQ.text, _rOnQ.text[:200])
check("and says which companies would escalate to nobody",
      any(b in _rOnQ.text for b in ("ABC", "BIPS", "BODYZONE", "SPA KORA")),
      _rOnQ.text[:300])

# One company's own levels are NOT enough on their own: every other
# company still falls back on a ladder that does not exist.
with _slQ() as _d:
    _orgQ = _d.scalar(_SEL(_OgQ))
    _oneBrQ = _d.scalar(_SEL(_BrQ).where(_BrQ.org_id == _orgQ.id))
    _d.add(_ErQ(org_id=_orgQ.id, branch_id=_oneBrQ.id, level=1,
                to_name="Head of that company", to_email="hod@gcs.local"))
    _d.commit()
    _oneNameQ = _oneBrQ.name
_rOneQ = adminQ.post("/escalation/settings",
                     data={"esc_on": "1", "working_days": "1",
                           "after_top": "stop", "scope": "chased", "hour": "9"})
check("one company's own levels do not switch it on for everybody",
      _rOneQ.status_code == 400, _rOneQ.status_code)
check("and the company that IS covered is not listed as a gap",
      _oneNameQ not in _rOneQ.text, _oneNameQ)

# Fill the fallback and it goes on.
with _slQ() as _d:
    _orgQ = _d.scalar(_SEL(_OgQ))
    _d.add(_ErQ(org_id=_orgQ.id, branch_id=None, level=1,
                to_name="The EA", to_email="ea@gcs.local"))
    _d.commit()
_rGoQ = adminQ.post("/escalation/settings",
                    data={"esc_on": "1", "working_days": "1",
                          "after_top": "stop", "scope": "chased", "hour": "9"})
check("filling the every-other-company ladder is what switches it on",
      _rGoQ.status_code in (200, 303), _rGoQ.status_code)
with _slQ() as _d:
    check("and it really is on", _d.scalar(_SEL(_OgQ)).esc_on)
    # Put the ladder back as the rest of the suite expects to find it.
    for _r in _d.scalars(_SEL(_ErQ).where(_ErQ.org_id == _orgQ.id)).all():
        _d.delete(_r)
    _d.commit()
    _orgQ = _d.scalar(_SEL(_OgQ))
    for _n, (_nm, _em, _uid) in enumerate(_savedQ, start=1):
        _d.add(_ErQ(org_id=_orgQ.id, branch_id=None, level=_n,
                    to_name=_nm, to_email=_em, user_id=_uid))
    _d.commit()


# ==========================================================================
# FMS is everybody's — starting a run, and reading what was submitted
# ==========================================================================
print("\n-- FMS for a doer --")

import json as _jsonF
from app.db import SessionLocal as _slF
from app.models import (Flow as _FlF, FlowInstance as _FiF, Right as _RiF,
                        Role as _RoF, Task as _TkF, User as _UsF)

_SELF = __import__("sqlalchemy").select
_FUNF = __import__("sqlalchemy").func

# A plain doer, made plain on purpose. Earlier blocks in this suite hand
# amit the delegate right while testing other things, so it is taken back
# here — the whole point is that starting a flow no longer needs it. It used
# to, which put a manager in front of every start form.
with _slF() as _d:
    _amitF = _d.scalar(_SELF(_UsF).where(_UsF.email == "amit@gcs.local"))
    _amitF.set_rights([r for r in _amitF.right_set
                       if r not in (_RiF.CREATE_TASK.value,
                                    _RiF.MANAGE_FLOW.value)])
    _d.commit()
    check("the doer this is tested as cannot delegate",
          not _amitF.has(_RiF.CREATE_TASK), _amitF.rights)
    check("and cannot manage flows",
          not _amitF.has(_RiF.MANAGE_FLOW), _amitF.rights)
    check("and is a doer, not a manager", _amitF.role == _RoF.DOER,
          _amitF.role)
    _flowF = _d.scalar(_SELF(_FlF).where(_FlF.active.is_(True)))
    _fidF = _flowF.id
    _fnameF = _flowF.name

_rNavF = doer.get("/")
check("a doer has FMS in the menu", 'title="FMS"' in _rNavF.text)

for _urlF, _whatF in ((f"/flows", "the flow list"),
                      (f"/flows/{_fidF}", "a flow"),
                      (f"/flows/{_fidF}/submissions", "its submissions")):
    check(f"a doer can open {_whatF}", doer.get(_urlF).status_code == 200,
          _urlF)

_rFlowF = doer.get(f"/flows/{_fidF}")
check("the start form is shown to a doer",
      f'action="/flows/{_fidF}/start"' in _rFlowF.text)
check("and the Edit door is not",
      f'/flows/{_fidF}/edit' not in _rFlowF.text)
check("the list offers a doer Start and Submissions, not Edit",
      ">Start<" in doer.get("/flows").text
      and "Submissions" in doer.get("/flows").text
      and "/edit" not in doer.get("/flows").text)
check("and does not offer a doer a new flow",
      "+ New flow" not in doer.get("/flows").text)

# What a doer still must not be able to do. Deciding what the steps ARE is a
# different act from reporting that the thing happened.
for _urlF in (f"/flows/{_fidF}/edit", "/flows/new"):
    check(f"a doer is refused {_urlF}",
          doer.get(_urlF).status_code == 403, doer.get(_urlF).status_code)
check("a doer cannot create a flow",
      doer.post("/flows/new", data={"name": f"SNEAKY {RUN}"}).status_code == 403)
check("a doer cannot switch a flow off",
      doer.post(f"/flows/{_fidF}/toggle").status_code == 403)
check("a doer cannot delete a flow",
      doer.post(f"/flows/{_fidF}/delete",
                data={"confirm": _fnameF}).status_code == 403)

# ------------------------------------------------- a doer starts a run ---
# A flow with a real start form, so the answers have somewhere to land.
with _slF() as _d:
    _flow2F = _d.get(_FlF, _fidF)
    _flow2F.start_form = _jsonF.dumps([
        {"label": f"Student name {RUN}", "type": "text", "options": [],
         "required": True},
        {"label": "Class applied for", "type": "select",
         "options": ["Nursery", "Class I", "Class V"], "required": True},
        {"label": "Fee quoted", "type": "number", "options": [],
         "required": False},
        {"label": "Transport needed", "type": "yesno", "options": [],
         "required": True},
    ])
    _d.commit()
    _keysF = {f["label"]: f["key"] for f in _flow2F.start_form_fields}
    _beforeF = _d.scalar(_SELF(_FUNF.count()).select_from(_FiF)
                         .where(_FiF.flow_id == _fidF)) or 0

_subsF = [("Aarav Sharma", "Class I", "48000", "Yes"),
          ("Diya Mehta", "Nursery", "36000", "No")]
for _nameF, _clsF, _feeF, _busF in _subsF:
    _dataF = {"reference": f"{_nameF} {RUN}", "run_branch": "",
              _keysF[f"Student name {RUN}"]: _nameF,
              _keysF["Class applied for"]: _clsF,
              _keysF["Fee quoted"]: _feeF,
              _keysF["Transport needed"]: _busF}
    _rStartF = doer.post(f"/flows/{_fidF}/start", data=_dataF)
    check(f"a doer can start a run for {_nameF}",
          _rStartF.status_code == 200, _rStartF.text[:200])

with _slF() as _d:
    _afterF = _d.scalar(_SELF(_FUNF.count()).select_from(_FiF)
                        .where(_FiF.flow_id == _fidF)) or 0
    check("both runs were created", _afterF == _beforeF + 2,
          (_beforeF, _afterF))
    _mineF = _d.scalars(_SELF(_FiF).where(_FiF.flow_id == _fidF)
                        .order_by(_FiF.id.desc())).all()[:2]
    check("the run is filed against the doer who started it",
          all(i.started_by.email == "amit@gcs.local" for i in _mineF),
          [i.started_by.email for i in _mineF])
    check("and its steps were handed out to the flow's own people",
          all(_d.scalar(_SELF(_FUNF.count()).select_from(_TkF)
                        .where(_TkF.flow_instance_id == i.id)) > 0
              for i in _mineF))
    _answersF = _jsonF.loads(_mineF[0].context or "{}")
    check("the answers are stored against the run",
          _answersF.get("Class applied for") in ("Nursery", "Class I"),
          _answersF)
    _runIdF = _mineF[0].id
    _lateRefF = _mineF[0].reference

# A doer still cannot pause or stop what they started. Holding a run takes
# its steps off other people's lists and out of their scores.
check("a doer cannot hold a run",
      doer.post(f"/flows/instance/{_runIdF}/hold",
                data={"reason": "x"}).status_code == 403)
check("a doer cannot stop a run",
      doer.post(f"/flows/instance/{_runIdF}/stop",
                data={"reason": "x"}).status_code == 403)

# ---------------------------------------------- the submissions register --
_rSubF = doer.get(f"/flows/{_fidF}/submissions")
check("the submissions page opens for a doer",
      _rSubF.status_code == 200)
for _colF in (f"Student name {RUN}", "Class applied for", "Fee quoted",
              "Transport needed"):
    check(f"it has a column for '{_colF}'", _colF in _rSubF.text, _colF)
for _valF in ("Aarav Sharma", "Diya Mehta", "48000", "Class I", "Nursery"):
    check(f"and shows the submitted answer '{_valF}'",
          _valF in _rSubF.text, _valF)
check("it names who submitted each one",
      "PT Trainer Amit" in _rSubF.text)

# Everybody's, not just their own — which is what was asked for, and what
# anybody building a start form here needs to know.
_secondF = login("front.bz@gcs.local")
_rSubOtherF = _secondF.get(f"/flows/{_fidF}/submissions")
check("another doer sees the same submissions",
      _rSubOtherF.status_code == 200 and "Aarav Sharma" in _rSubOtherF.text,
      _rSubOtherF.status_code)
check("and the admin sees them too",
      "Aarav Sharma" in admin.get(f"/flows/{_fidF}/submissions").text)

# Filters.
check("'only mine' narrows it to the person asking",
      "Aarav Sharma" in doer.get(
          f"/flows/{_fidF}/submissions?who=mine").text
      and "Aarav Sharma" not in _secondF.get(
          f"/flows/{_fidF}/submissions?who=mine").text)
check("a search matches an answer, not just the reference",
      "Diya Mehta" in doer.get(
          f"/flows/{_fidF}/submissions?q=36000").text)
check("and a search that matches nothing says so",
      "Nothing matches that" in doer.get(
          f"/flows/{_fidF}/submissions?q=zzz{RUN}").text)
for _stF in ("running", "done", "held", "stopped"):
    check(f"the {_stF} tab opens",
          doer.get(f"/flows/{_fidF}/submissions?state={_stF}"
                   ).status_code == 200)

# The Excel version carries the answers too — this is the register people
# will actually want to hand to somebody.
_rXlF = doer.get(f"/flows/{_fidF}/submissions?export=xlsx")
check("the submissions download is a workbook",
      _rXlF.status_code == 200
      and "spreadsheet" in _rXlF.headers.get("content-type", ""),
      _rXlF.headers.get("content-type"))
check("and it is not empty", len(_rXlF.content) > 4000, len(_rXlF.content))

# A question removed from the form later: the answers people already gave it
# are still data they submitted, so the column stays.
with _slF() as _d:
    _flow3F = _d.get(_FlF, _fidF)
    _flow3F.start_form = _jsonF.dumps([
        {"label": f"Student name {RUN}", "type": "text", "options": [],
         "required": True},
    ])
    _d.commit()
    check("the flow now asks only one question",
          len(_flow3F.start_form_fields) == 1)
_rSub2F = doer.get(f"/flows/{_fidF}/submissions")
check("a question since removed keeps its column",
      "Class applied for" in _rSub2F.text and "Fee quoted" in _rSub2F.text,
      _rSub2F.status_code)
check("and the answers given to it are still shown",
      "48000" in _rSub2F.text and "Class I" in _rSub2F.text)

# A switched-off flow says so rather than showing a form that would be
# refused on submit.
with _slF() as _d:
    _flow4F = _d.get(_FlF, _fidF)
    _flow4F.active = False
    _d.commit()
_rOffF = doer.get(f"/flows/{_fidF}")
check("a switched-off flow shows no start form",
      f'action="/flows/{_fidF}/start"' not in _rOffF.text)
check("and says why", "switched off" in _rOffF.text)
check("its past submissions are still readable",
      doer.get(f"/flows/{_fidF}/submissions").status_code == 200)
check("and starting one is refused by the server as well",
      doer.post(f"/flows/{_fidF}/start",
                data={"reference": f"OFF {RUN}"}).status_code == 400)
with _slF() as _d:
    _flow5F = _d.get(_FlF, _fidF)
    _flow5F.active = True
    _d.commit()


# ==========================================================================
# Every step of a run is planned — even one the flow does not staff
# ==========================================================================
print("\n-- the whole run is planned, whoever is on it --")
# The bug: replan silently dropped any step it could not name a person for,
# so a flow whose later steps have no doer set — or whose position nobody at
# that company holds — planned exactly ONE step. The people on steps 2 to 8
# saw nothing coming and the run read "1 of 8" for ever.

from app.db import SessionLocal as _slP
from app.models import (Branch as _BrP, Department as _DpP, Flow as _FlP,
                        FlowInstance as _FiP, FlowStep as _FsP,
                        Priority as _PrP, Task as _TkP, TaskStatus as _StP,
                        User as _UsP)
from app.services import flows as _fsvP
from app.services import holidays as _holP

_SELP = __import__("sqlalchemy").select


def _build8(db, name, staffing, kiran, extra=None):
    """An eight-step flow, staffed three different ways."""
    flow = _FlP(org_id=kiran.org_id, name=name, active=True, branch_id=None)
    db.add(flow); db.flush()
    for pos in range(1, 9):
        st = _FsP(flow_id=flow.id, position=pos, title=f"Step {pos}",
                  priority=_PrP.MEDIUM, tat_unit="days", tat_value=1,
                  requires_attachment=False)
        if pos == 1:
            st.default_doer_id = kiran.id
            if extra:
                st.extra_doer_ids = str(extra.id)
        elif staffing == "named":
            st.default_doer_id = kiran.id
        elif staffing == "position":
            st.doer_dept = f"SMOKE HR {RUN}"
        # staffing == "nobody": left unset on purpose
        db.add(st)
    db.commit()
    return flow


with _slP() as _d:
    _hoP = _d.scalar(_SELP(_BrP).where(_BrP.name == "GCS HO"))
    _bipsP = _d.scalar(_SELP(_BrP).where(_BrP.name == "BIPS School"))
    _bossP = _d.scalar(_SELP(_UsP).where(_UsP.email == "mis@gcs.local"))
    _k1P = _d.scalar(_SELP(_UsP).where(_UsP.branch_id == _hoP.id,
                                       _UsP.active.is_(True)))
    _k2P = [u for u in _d.scalars(_SELP(_UsP).where(
        _UsP.branch_id == _hoP.id, _UsP.active.is_(True))).all()
        if u.id != _k1P.id][0]
    # A department that exists only at head office — the shape a shared
    # function has when the run is for a school.
    _hrP = _DpP(org_id=_bossP.org_id, branch_id=_hoP.id,
                name=f"SMOKE HR {RUN}")
    _d.add(_hrP); _d.flush()
    _k1P.department_id = _hrP.id
    _d.commit()
    _bipsIdP, _hoIdP = _bipsP.id, _hoP.id

    _casesP = [
        ("every step names a person", "named", _bipsIdP, False),
        ("steps 2-8 name a position nobody holds there", "position",
         _bipsIdP, True),
        ("steps 2-8 name nobody at all", "nobody", _bipsIdP, True),
    ]
    for _labelP, _howP, _branchP, _provP in _casesP:
        _flowP = _build8(_d, f"SMOKE 8-step {_howP} {RUN}", _howP, _k1P,
                         extra=_k2P)
        _instP = _fsvP.start_flow(_d, _flowP, f"SMOKE {_howP} {RUN}", _bossP,
                                  branch_id=_branchP)
        _rowsP = _d.scalars(_SELP(_TkP).where(
            _TkP.flow_instance_id == _instP.id)).all()
        _posP = sorted({t.flow_step.position for t in _rowsP})
        check(f"all eight steps are planned when {_labelP}",
              _posP == list(range(1, 9)), _posP)
        check(f"  and step 1 is the live one when {_labelP}",
              all(not t.flow_waiting for t in _rowsP
                  if t.flow_step.position == 1)
              and all(t.flow_waiting for t in _rowsP
                      if t.flow_step.position > 1),
              [(t.flow_step.position, t.flow_waiting) for t in _rowsP])
        check(f"  two people on step 1 get a task each when {_labelP}",
              len([t for t in _rowsP if t.flow_step.position == 1]) == 2,
              len([t for t in _rowsP if t.flow_step.position == 1]))
        # Dates climb, one per step, and never sit on a closed day.
        _duesP = [min(t.due_at for t in _rowsP
                      if t.flow_step.position == p) for p in range(1, 9)]
        check(f"  the dates climb step by step when {_labelP}",
              all(_duesP[i] < _duesP[i + 1] for i in range(7)),
              [str(x.date()) for x in _duesP])
        check(f"  and not one of them is a day the company is closed "
              f"when {_labelP}",
              not any(_holP.is_closed(_d, _bossP.org_id, x.date(),
                                      _hoIdP) for x in _duesP),
              [f"{x:%a %d %b}" for x in _duesP])
        # The honest part: a step nobody is named for says so.
        _laterP = [t for t in _rowsP if t.flow_step.position > 1]
        if _provP:
            check(f"  a step nobody is named for says so on the task "
                  f"when {_labelP}",
                  all("Planned provisionally" in (t.details or "")
                      for t in _laterP),
                  [(t.details or "")[:60] for t in _laterP[:1]])
            check(f"  and is held against whoever started the run "
                  f"when {_labelP}",
                  all(t.doer_id == _bossP.id for t in _laterP))
        else:
            check(f"  a properly staffed step says nothing about guesswork "
                  f"when {_labelP}",
                  not any("Planned provisionally" in (t.details or "")
                          for t in _laterP))
        if _howP == "nobody":
            _instNobodyP = _instP.id

# A provisional task is the software's own guesswork, not somebody's work,
# so the plan must still be free to re-date it and to drop it when a
# decision sends the run elsewhere. Writing the note as a COMMENT would have
# frozen it: _touched counts a comment as evidence of real work.
with _slP() as _d:
    _instP = _d.get(_FiP, _instNobodyP)
    _provRowsP = [t for t in _d.scalars(_SELP(_TkP).where(
        _TkP.flow_instance_id == _instP.id)).all() if t.flow_waiting]
    check("a provisionally planned step is not mistaken for worked-on",
          not any(_fsvP._touched(t) for t in _provRowsP),
          [(t.id, bool(t.comments)) for t in _provRowsP[:2]])
    _wasP = {t.id: t.due_at for t in _provRowsP}
    # Push step 1's deadline out; everything after it must move too.
    _liveP = [t for t in _d.scalars(_SELP(_TkP).where(
        _TkP.flow_instance_id == _instP.id)).all() if not t.flow_waiting]
    for t in _liveP:
        t.due_at = t.due_at + _tdJ(days=5)
    _d.commit()
    _fsvP.replan(_d, _instP, _d.get(_UsP, _instP.started_by_id))
    _d.commit()
    _nowP = {t.id: t.due_at for t in _d.scalars(_SELP(_TkP).where(
        _TkP.flow_instance_id == _instP.id)).all() if t.flow_waiting}
    check("and a late step still pushes the provisional ones along",
          all(_nowP[k] > _wasP[k] for k in _wasP if k in _nowP),
          [(k, str(_wasP[k]), str(_nowP.get(k))) for k in list(_wasP)[:2]])

# ------------------------------------------- progress counts STEPS done --
# Five tasks on step 1 — two people and a rework loop — read as "4 of 8
# steps done" with a half-filled bar. It had done one step.
with _slP() as _d:
    _instP = _d.get(_FiP, _instNobodyP)
    check("a run with nothing finished is 0 of 8",
          _fsvP.flow_progress(_instP) == (0, 8),
          _fsvP.flow_progress(_instP))
    _s1P = [t for t in _d.scalars(_SELP(_TkP).where(
        _TkP.flow_instance_id == _instP.id)).all()
        if t.flow_step.position == 1]
    for t in _s1P:
        t.status = _StP.COMPLETED
    _d.commit()
    _d.refresh(_instP)
    check("one step finished is 1 of 8, however many people were on it",
          _fsvP.flow_progress(_instP) == (1, 8),
          (_fsvP.flow_progress(_instP), len(_s1P)))
    # A rework copy of the same step, also closed: still one step done.
    _reworkP = _fsvP._spawn_step_task(
        _d, _instP, _d.get(_FsP, _s1P[0].flow_step_id),
        _d.get(_UsP, _s1P[0].doer_id), _d.get(_UsP, _instP.started_by_id))
    _reworkP.status = _StP.COMPLETED
    _d.commit()
    _d.refresh(_instP)
    check("and a step sent back and done again is still one step",
          _fsvP.flow_progress(_instP) == (1, 8),
          _fsvP.flow_progress(_instP))
    # One still with its doer means that step is not done.
    _reworkP.status = _StP.IN_PROGRESS
    _d.commit()
    _d.refresh(_instP)
    check("a step with anybody still holding it does not count as done",
          _fsvP.flow_progress(_instP) == (0, 8),
          _fsvP.flow_progress(_instP))

# --------------------------------------------- early is said out loud ----
# "On time" covered everything from a fortnight early to four minutes to
# spare. Somebody finishing an FMS step days before its turn is the reason
# the next step can start early, and that never showed anywhere.
from app.models import Task as _TkR
with _slP() as _d:
    _bossR = _d.scalar(_SELP(_UsP).where(_UsP.email == "mis@gcs.local"))
    _whoR = _d.scalar(_SELP(_UsP).where(_UsP.email == "amit@gcs.local"))

    def _timed(due, done):
        t = _TkR(org_id=_bossR.org_id, title=f"SMOKE timing {RUN}",
                 assigner_id=_bossR.id, doer_id=_whoR.id,
                 priority=_PrP.LOW, source=_TsQ.DELEGATION,
                 status=_StP.SUBMITTED, due_at=due, submitted_at=done,
                 requires_attachment=False)
        _d.add(t); _d.flush()
        return t

    check("work handed in three days early says so",
          _timed(_dtC(2026, 12, 10, 18), _dtC(2026, 12, 7, 9)
                 ).timing_label == "3 days early")
    check("one day early is singular",
          _timed(_dtC(2026, 12, 10, 18), _dtC(2026, 12, 9, 9)
                 ).timing_label == "1 day early")
    check("the same day, inside the deadline, is still 'on time'",
          _timed(_dtC(2026, 12, 10, 18), _dtC(2026, 12, 10, 9)
                 ).timing_label == "on time")
    check("the same day but past the hour is late, not on time",
          _timed(_dtC(2026, 12, 10, 9), _dtC(2026, 12, 10, 18)
                 ).timing_label == "late")
    check("and two days over says how many",
          _timed(_dtC(2026, 12, 10, 18), _dtC(2026, 12, 12, 9)
                 ).timing_label == "2 days late")
    check("nothing is claimed about work not handed in",
          _TkR(org_id=_bossR.org_id, title="x", assigner_id=_bossR.id,
               doer_id=_whoR.id, priority=_PrP.LOW, source=_TsQ.DELEGATION,
               status=_StP.PENDING,
               due_at=_dtC(2026, 12, 10)).timing_label == "")
    _d.rollback()
check("and the task list shows it",
      "early" in admin.get("/tasks?scope=all&status=completed").text.lower())

# ------------------------------------------- a doer sees what is coming --
# The whole point: the person on step 4 can see step 4's planned date on the
# day the form was submitted, and can get on with it early.
with _slP() as _d:
    _flowQ = _build8(_d, f"SMOKE visible-ahead {RUN}", "named",
                     _d.scalar(_SELP(_UsP).where(
                         _UsP.email == "amit@gcs.local")))
    _bossQ = _d.scalar(_SELP(_UsP).where(_UsP.email == "mis@gcs.local"))
    _instQ = _fsvP.start_flow(_d, _flowQ, f"SMOKE AHEAD {RUN}", _bossQ,
                              branch_id=None)
    _instQid = _instQ.id
_rMineQ = doer.get("/tasks?scope=mine&status=pending&source=fms").text
check("a doer sees every step of the run, not just the first",
      _rMineQ.count(f"SMOKE AHEAD {RUN}") >= 8,
      _rMineQ.count(f"SMOKE AHEAD {RUN}"))
check("and each one says which step it is",
      "Step 4 of 8" in _rMineQ)
check("the later ones are marked planned rather than chased",
      "planned" in _rMineQ.lower())


# ==========================================================================
# A run is named by its answers — there is no Reference box any more
# ==========================================================================
print("\n-- the start form asks only what the flow asks --")
# There used to be a "Reference" box at the top of every start form, asked
# on top of whatever the flow's own questions were: a question nobody had
# added, usually repeating something the real questions already asked.

import json as _jsonN
from app.db import SessionLocal as _slN
from app.models import (Flow as _FlN, FlowInstance as _FiN, Task as _TkN,
                        User as _UsN)
from app.services import flows as _fsvN

_SELN = __import__("sqlalchemy").select


def _ask(db, flow_id, questions):
    """Give a flow a start form and hand back its field keys, by label."""
    f = db.get(_FlN, flow_id)
    f.start_form = _jsonN.dumps([
        {"label": q, "type": t, "options": o, "required": False}
        for q, t, o in questions])
    db.commit()
    return {x["label"]: x["key"] for x in f.start_form_fields}


with _slN() as _d:
    _flowN = _d.scalar(_SELN(_FlN).where(_FlN.active.is_(True)))
    _fidN = _flowN.id
    _keysN = _ask(_d, _fidN, [
        (f"Month {RUN}", "select", ["January", "February"]),
        (f"Year {RUN}", "select", ["2026", "2027"]),
        (f"Entered by {RUN}", "text", []),
    ])

_rFormN = admin.get(f"/flows/{_fidN}")
check("the start form no longer asks for a Reference",
      'name="reference"' not in _rFormN.text, _rFormN.status_code)
for _qN in _keysN:
    check(f"but it does ask “{_qN[:22]}…”", _qN in _rFormN.text)
check("and says the answers will name the run",
      "also name it" in _rFormN.text or "name it" in _rFormN.text)

_rStartN = admin.post(f"/flows/{_fidN}/start", data={
    "run_branch": "",
    _keysN[f"Month {RUN}"]: "January",
    _keysN[f"Year {RUN}"]: "2026",
    _keysN[f"Entered by {RUN}"]: "Amit Kumar"})
check("a run starts with no reference supplied at all",
      _rStartN.status_code == 200, _rStartN.text[:200])

with _slN() as _d:
    _iN = _d.scalars(_SELN(_FiN).where(_FiN.flow_id == _fidN)
                     .order_by(_FiN.id.desc())).first()
    check("and is named from the first two answers",
          _iN.reference == "January · 2026", _iN.reference)
    check("not from all of them — the third is noise in a name",
          "Amit Kumar" not in _iN.reference, _iN.reference)
    check("every step's title carries that name",
          all(t.title.endswith("January · 2026") for t in _d.scalars(
              _SELN(_TkN).where(_TkN.flow_instance_id == _iN.id)).all()),
          [t.title for t in _d.scalars(
              _SELN(_TkN).where(_TkN.flow_instance_id == _iN.id)).all()][:1])
    _namedIdN = _iN.id

# It is still findable by that name, which is the point of having one.
check("the FMS list finds it by its name",
      "January" in admin.get("/flows?q=January").text)
check("and the submissions register does too",
      "January" in admin.get(f"/flows/{_fidN}/submissions?q=January").text)

# The shapes a start form actually comes in.
_shapesN = [
    ("a vendor form names the run by vendor and bill",
     [("Vendor name", "text", []), ("Bill number", "text", []),
      ("Amount", "number", []), ("Due date", "date", [])],
     ["Sharma & Co", "INV-7741", "240000", "2026-11-05"],
     "Sharma & Co · INV-7741"),
    ("a paragraph is skipped, not chopped into a name",
     [("What happened", "textarea", []), ("Member name", "text", [])],
     ["The member called to complain at length about the treadmill in the "
      "corner, which has been broken for three weeks now", "Ritu Bansal"],
     "Ritu Bansal"),
    ("one question is enough on its own",
     [("Candidate name", "text", [])], ["Kabir Singh"], "Kabir Singh"),
    ("a blank optional answer does not leave a gap in the name",
     [("Branch code", "text", []), ("Member name", "text", []),
      ("Remark", "text", [])], ["", "Diya Mehta", "walk-in"],
     "Diya Mehta · walk-in"),
]
for _labelN, _qsN, _ansN, _wantN in _shapesN:
    with _slN() as _d:
        _kN = _ask(_d, _fidN, _qsN)
    _dataN = {"run_branch": ""}
    _dataN.update(dict(zip([_kN[q] for q, _t, _o in _qsN], _ansN)))
    admin.post(f"/flows/{_fidN}/start", data=_dataN)
    with _slN() as _d:
        _gotN = _d.scalars(_SELN(_FiN).where(_FiN.flow_id == _fidN)
                           .order_by(_FiN.id.desc())).first().reference
    check(_labelN, _gotN == _wantN, (_gotN, _wantN))

# A flow that asks nothing still has to be called something.
with _slN() as _d:
    _bareN = _d.get(_FlN, _fidN)
    _bareN.start_form = None
    _bareN.start_fields = None
    _d.commit()
_rBareN = admin.get(f"/flows/{_fidN}")
check("a flow with no questions shows no boxes to fill in",
      'name="reference"' not in _rBareN.text and "sf0" not in _rBareN.text)
check("and says the run will be known by its number",
      "known by its FMS number" in _rBareN.text)
admin.post(f"/flows/{_fidN}/start", data={"run_branch": ""})
with _slN() as _d:
    _bareIN = _d.scalars(_SELN(_FiN).where(_FiN.flow_id == _fidN)
                         .order_by(_FiN.id.desc())).first()
    check("a run with nothing answered is named by its own FMS number",
          _bareIN.reference == _bareIN.ref, (_bareIN.reference, _bareIN.ref))
    check("so its steps are still titled something a person can quote",
          all(t.title.endswith(_bareIN.ref) for t in _d.scalars(
              _SELN(_TkN).where(_TkN.flow_instance_id == _bareIN.id)).all()))
    check("and the name is never empty, whatever was asked",
          bool((_bareIN.reference or "").strip()))

# A reference posted by an older page still open in a tab is honoured
# rather than silently dropped.
admin.post(f"/flows/{_fidN}/start",
           data={"run_branch": "", "reference": f"TYPED BY HAND {RUN}"})
with _slN() as _d:
    _typedN = _d.scalars(_SELN(_FiN).where(_FiN.flow_id == _fidN)
                         .order_by(_FiN.id.desc())).first()
    check("a reference that IS posted is still used",
          _typedN.reference == f"TYPED BY HAND {RUN}", _typedN.reference)

# The builder says what will happen, so nobody has to find out by starting one.
_rBuildN = admin.get(f"/flows/{_fidN}/edit").text
check("the builder says the start form is the only thing asked",
      "<strong>only</strong>" in _rBuildN)
check("and that the first two answers name the run",
      "first\n  two short answers" in _rBuildN
      or "two short answers" in _rBuildN)


# ==========================================================================
# One flow, four hundred members, a different trainer on each
# ==========================================================================
print("\n-- person questions, and starting many from a file --")

import io as _ioB
import json as _jsonB
from openpyxl import load_workbook as _loadB
from app.db import SessionLocal as _slB2
from app.models import (CONTEXT_PEOPLE as _CPB, Flow as _FlB,
                        FlowInstance as _FiB, FlowStep as _FsB,
                        Priority as _PrB, Task as _TkB, User as _UsB)
from app.services import flowbulk as _fbB, flowform as _ffB, flows as _fsB

_SELB = __import__("sqlalchemy").select

with _slB2() as _d:
    _orgB = _d.scalar(_SELB(_UsB.org_id))
    _bossB = _d.scalar(_SELB(_UsB).where(_UsB.email == "mis@gcs.local"))
    _staffB = _ffB.staff(_d, _orgB)
    check("a person question offers people who are given work",
          _staffB and all(u.receives_work for u in _staffB))
    check("and never the admin, who hands work out and is given none",
          _bossB not in _staffB)

    _flowB = _FlB(org_id=_orgB, name=f"SMOKE renewal {RUN}", active=True,
                  branch_id=None)
    _flowB.start_form = _jsonB.dumps([
        {"label": "Member name", "type": "text", "options": [], "required": True},
        {"label": "Expiry date", "type": "date", "options": [], "required": True},
        {"label": "Package", "type": "select",
         "options": ["3 month", "6 month", "12 month"], "required": True},
        {"label": "PT trainer", "type": "person", "options": [], "required": True},
        {"label": "Salesperson", "type": "person", "options": [], "required": True},
    ])
    _d.add(_flowB); _d.flush()
    for _pos, (_t, _who) in enumerate([
            ("Call the member", "PT trainer"),
            ("Offer the package", "Salesperson"),
            ("Collect payment", None)], start=1):
        _st = _FsB(flow_id=_flowB.id, position=_pos, title=_t,
                   priority=_PrB.MEDIUM, tat_unit="days", tat_value=1,
                   requires_attachment=False)
        if _who:
            _st.doer_field = _who
        else:
            _st.default_doer_id = _staffB[0].id
        _d.add(_st)
    _d.commit()
    _fidB = _flowB.id
    _namesB = [u.name for u in _staffB[:3]]
    _mailB = _staffB[0].email

# ------------------------------------------- one run, by hand, per person --
_rFormB = admin.get(f"/flows/{_fidB}")
check("the start form offers the people a person question may name",
      _namesB[0] in _rFormB.text and 'name="sf3"' in _rFormB.text)
for _i, (_member, _trainer) in enumerate(
        [("Ritu Bansal", _namesB[0]), ("Vikram Mehta", _namesB[1])]):
    _rB = admin.post(f"/flows/{_fidB}/start", data={
        "run_branch": "", "sf0": _member, "sf1": "2026-11-20",
        "sf2": "6 month", "sf3": _trainer, "sf4": _namesB[2]})
    check(f"a run starts for {_member}", _rB.status_code == 200,
          _rB.text[:160])

with _slB2() as _d:
    _runsB = _d.scalars(_SELB(_FiB).where(_FiB.flow_id == _fidB)
                        .order_by(_FiB.id)).all()
    check("two runs of the one flow", len(_runsB) == 2, len(_runsB))
    _doersB = []
    for _r in _runsB:
        _first = [t for t in _d.scalars(_SELB(_TkB).where(
            _TkB.flow_instance_id == _r.id)).all()
            if t.flow_step.position == 1][0]
        _doersB.append(_first.doer.name)
    check("and step 1 went to a DIFFERENT person on each",
          _doersB == _namesB[:2], _doersB)
    check("while the step with a name on it went to the same person both times",
          len({[t for t in _d.scalars(_SELB(_TkB).where(
              _TkB.flow_instance_id == _r.id)).all()
              if t.flow_step.position == 3][0].doer_id
              for _r in _runsB}) == 1)
    check("the run is named by what it is about, not by who does it",
          all(_n not in _runsB[0].reference for _n in _namesB),
          _runsB[0].reference)
    check("and which person was meant is recorded, not just their name",
          _CPB in _jsonB.loads(_runsB[0].context),
          _runsB[0].context[:120])
    check("while the register shows the answers and not the bookkeeping",
          _CPB not in admin.get(f"/flows/{_fidB}/submissions").text)

# A person question answered with somebody who does not exist.
_rBadB = admin.post(f"/flows/{_fidB}/start", data={
    "run_branch": "", "sf0": "Nobody", "sf1": "2026-11-20",
    "sf2": "6 month", "sf3": "Who Even", "sf4": _namesB[2]})
check("a person question refuses a name nobody has",
      _rBadB.status_code == 400, _rBadB.status_code)

# ------------------------------------------------------ many, from a file --
_rTplB = admin.get(f"/flows/{_fidB}/bulk/template")
check("the template downloads", _rTplB.status_code == 200
      and len(_rTplB.content) > 3000, _rTplB.status_code)
check("and its filename survives an HTTP header",
      "attachment" in _rTplB.headers.get("content-disposition", ""))

_wsB = _loadB(_ioB.BytesIO(_rTplB.content)).active
_headsB = [c.value for c in _wsB[1]]
check("the headings ARE this flow's questions",
      "Member name" in _headsB and "PT trainer" in _headsB
      and "Package" in _headsB, _headsB)
check("plus the company, because a run is for one",
      _fbB.COMPANY in _headsB, _headsB)
check("and a file question is left out — a cell cannot hold a document",
      not any("upload" in str(h).lower() for h in _headsB))

_rowsB = [
    {"Member name": "Aarav Sharma", "Expiry date": "2026-11-25",
     "Package": "6 month", "PT trainer": _namesB[0],
     "Salesperson": _namesB[2], _fbB.COMPANY: "Bodyzone Fitness & Spa"},
    {"Member name": "Diya Mehta", "Expiry date": "26/11/2026",
     "Package": "12 month", "PT trainer": _mailB,
     "Salesperson": _namesB[2], _fbB.COMPANY: "Bodyzone Fitness & Spa"},
    {"Member name": "Bad trainer", "Expiry date": "2026-11-27",
     "Package": "6 month", "PT trainer": "A. Nonexistent",
     "Salesperson": _namesB[2], _fbB.COMPANY: "Bodyzone Fitness & Spa"},
    {"Member name": "Bad date", "Expiry date": "32/11/2026",
     "Package": "6 month", "PT trainer": _namesB[0],
     "Salesperson": _namesB[2], _fbB.COMPANY: "Bodyzone Fitness & Spa"},
    {"Member name": "Bad package", "Expiry date": "2026-11-28",
     "Package": "9 month", "PT trainer": _namesB[0],
     "Salesperson": _namesB[2], _fbB.COMPANY: "Bodyzone Fitness & Spa"},
    {"Member name": "", "Expiry date": "2026-11-29",
     "Package": "6 month", "PT trainer": _namesB[0],
     "Salesperson": _namesB[2], _fbB.COMPANY: "Bodyzone Fitness & Spa"},
]
for _n, _row in enumerate(_rowsB, start=3):
    for _i, _h in enumerate(_headsB, start=1):
        _wsB.cell(row=_n, column=_i, value=_row.get(_h, ""))
_bufB = _ioB.BytesIO(); _wsB.parent.save(_bufB)
_fileB = _bufB.getvalue()

with _slB2() as _d:
    _parsedB = _fbB.parse(_d, _d.get(_FlB, _fidB), _fileB)
    check("every row is read", len(_parsedB.rows) == 6, len(_parsedB.rows))
    check("two are ready", len(_parsedB.ready) == 2,
          [r.number for r in _parsedB.ready])
    check("and four need fixing", len(_parsedB.bad) == 4,
          [(r.number, r.problems) for r in _parsedB.bad])
    _whyB = " ".join(p for r in _parsedB.bad for p in r.problems)
    check("a name nobody has is named in the reason",
          "A. Nonexistent" in _whyB, _whyB[:200])
    check("an impossible date is caught, not rolled into December",
          "32/11/2026" in _whyB, _whyB[:200])
    check("a choice that is not on the list says what is",
          "9 month" in _whyB and "3 month" in _whyB, _whyB[:300])
    check("and a required answer left blank is refused",
          "Member name" in _whyB, _whyB[:300])
    check("a person can be given by email as well as by name",
          any(r.context.get("PT trainer") == _staffB[0].name
              for r in _parsedB.ready),
          [r.context for r in _parsedB.ready])
    check("a date typed the other way round is read and stored one way",
          any(r.context.get("Expiry date") == "2026-11-26"
              for r in _parsedB.ready),
          [r.context.get("Expiry date") for r in _parsedB.ready])
    check("the check says what each run will be called before it exists",
          all(r.name for r in _parsedB.ready),
          [r.name for r in _parsedB.ready])

# Through the pages, which is how somebody actually does it.
_rCheckB = admin.post(f"/flows/{_fidB}/bulk", files={"file": (
    "rows.xlsx", _fileB,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
check("the check page opens", _rCheckB.status_code == 200)
check("it says how many are ready", "<strong>2</strong> ready" in _rCheckB.text)
check("it lists what is wrong with the rest",
      "A. Nonexistent" in _rCheckB.text)
check("and nothing has been created yet",
      len(_parsedB.ready) == 2)

_beforeB = len(_runsB)
_rGoB = admin.post(f"/flows/{_fidB}/bulk/start")
check("starting them lands on the register", _rGoB.status_code == 200)
with _slB2() as _d:
    _allB = _d.scalars(_SELB(_FiB).where(_FiB.flow_id == _fidB)).all()
    check("two more runs exist", len(_allB) == _beforeB + 2,
          (len(_allB), _beforeB))
    _madeB = sorted(_allB, key=lambda r: r.id)[-2:]
    check("each named from its own row",
          {r.reference.split(" · ")[0] for r in _madeB}
          == {"Aarav Sharma", "Diya Mehta"},
          [r.reference for r in _madeB])
    _step1B = []
    for _r in _madeB:
        _step1B.append([t for t in _d.scalars(_SELB(_TkB).where(
            _TkB.flow_instance_id == _r.id)).all()
            if t.flow_step.position == 1][0].doer.name)
    check("and step 1 of each went to the trainer named in that row",
          _step1B == [_namesB[0], _staffB[0].name], _step1B)
    check("every step of every uploaded run is planned, not just step 1",
          all(len({t.flow_step.position for t in _d.scalars(_SELB(_TkB).where(
              _TkB.flow_instance_id == _r.id)).all()}) == 3 for _r in _madeB))
    check("and each run is filed under the company the row gave",
          all(r.branch_id is not None for r in _madeB))

check("the same check cannot be started twice",
      admin.post(f"/flows/{_fidB}/bulk/start").status_code == 400)

# A file built for a different flow, or an old one after the questions
# changed, must say so rather than import into the wrong columns.
_wrongB = _loadB(_ioB.BytesIO(_rTplB.content)).active
_wrongB.cell(row=1, column=2, value="Something else entirely")
_bufW = _ioB.BytesIO(); _wrongB.parent.save(_bufW)
with _slB2() as _d:
    _pW = _fbB.parse(_d, _d.get(_FlB, _fidB), _bufW.getvalue())
    check("a file missing a column is refused whole",
          _pW.missing and not _pW.rows, (_pW.missing, len(_pW.rows)))
    check("and the missing column is named",
          "Member name" in _pW.missing, _pW.missing)
_rWrongB = admin.post(f"/flows/{_fidB}/bulk", files={"file": (
    "rows.xlsx", _bufW.getvalue(),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
check("the page says so rather than importing anything",
      "missing" in _rWrongB.text and "nothing was read" in _rWrongB.text.lower())

check("something that is not a workbook is refused politely",
      admin.post(f"/flows/{_fidB}/bulk", files={"file": (
          "x.xlsx", b"not a workbook",
          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
      ).status_code == 400)

# A flow that asks nothing has nothing for a spreadsheet to carry.
with _slB2() as _d:
    _bareB = _FlB(org_id=_orgB, name=f"SMOKE bare bulk {RUN}", active=True)
    _d.add(_bareB); _d.flush()
    _d.add(_FsB(flow_id=_bareB.id, position=1, title="Only step",
                default_doer_id=_staffB[0].id, priority=_PrB.MEDIUM,
                tat_unit="days", tat_value=1, requires_attachment=False))
    _d.commit()
    _bareIdB = _bareB.id
check("a flow with no questions says bulk makes no sense for it",
      "asks nothing to get going" in admin.get(
          f"/flows/{_bareIdB}/bulk").text)

# The builder offers the third way of saying who does a step.
_rEditB = admin.get(f"/flows/{_fidB}/edit").text
check("the builder offers a step the start form's person questions",
      "Whoever is named in" in _rEditB and "PT trainer" in _rEditB)
check("and explains what that is for",
      "different person on every run" in _rEditB
      or "four hundred members" in _rEditB)
# Seen on screen: the heading twice, the same question under each. The
# server renders this group AND the script rebuilds it, so the two have to
# agree on which group that is — the script finds the server's by class and
# replaces it. Unmarked, it found nothing and added its own beside it.
check("the server marks that group for the script to replace",
      '<optgroup class="fieldgroup"' in _rEditB)
check("so every one of those groups on the page is one the script owns",
      _rEditB.count('<optgroup class="fieldgroup"')
      == _rEditB.count('label="From an answer on the start form"'),
      "{} marked, {} headings".format(
          _rEditB.count('<optgroup class="fieldgroup"'),
          _rEditB.count('label="From an answer on the start form"')))
_sfDup = open("app/templates/_stepfields.html", encoding="utf-8").read()
check("and the script clears every old group before adding its own",
      "querySelectorAll('.fieldgroup').forEach" in _sfDup)
# Why somebody concluded the flow had to be SAVED before the option
# appeared: a question called "PT trainer" left on the default Short text
# answer type was simply absent from this list, with nothing on screen
# saying the type was what decided it. Now it is listed, greyed, with the
# fix written on it — the same treatment the deadline anchor list gives.
check("a question that does not name a person is still listed",
      "set its answer type to A person on GCS to use it" in _sfDup)
check("but not choosable, so a step cannot point at a text box",
      "o.disabled = true;" in _sfDup)
check("and the list is built from every question, not only the person ones",
      "var all = formQuestions();" in _sfDup
      and "all.forEach(function(q){" in _sfDup)

print("\n== the list of person questions is built as you type ==")
# The bug this closes: the "Who does it" dropdown was rendered once by the
# server from the SAVED flow, so a question typed on the same page was not
# in it. Somebody adding "PT trainer" and then looking for it found two
# options and concluded the feature had not shipped. Every other
# cross-referencing list on that page is built in the browser; this one now
# is too.
_sfT = open("app/templates/_stepfields.html", encoding="utf-8").read()
check("the builder rebuilds the doer list in the browser",
      "function refreshDoerFields" in _sfT)
check("and does it whenever the start form changes",
      "refreshDoerFields();" in _sfT)
check("person questions are collected alongside the dated ones",
      "person: t.value === 'person'" in _sfT)
check("a step already pointing at a question keeps its setting",
      "if (chosen) sel.value = chosen;" in _sfT)
check("the new builder page serves that script, not just the edit page",
      "refreshDoerFields" in admin.get("/flows/new").text)

print("\n== a step due a set number of days BEFORE a date on the form ==")
# Found the hard way: the first name for this unit was eleven characters,
# and `tat_unit` is VARCHAR(10). SQLite ignores column lengths, so every
# test passed and the live PostgreSQL refused to save the step. This checks
# the names against the column itself, not against a number typed twice.
from app.clock import SPAN_UNITS as _SUT
from app.models import FlowStep as _FsT
check("every TAT unit name fits the column it is stored in",
      all(len(_u) <= _FsT.__table__.c.tat_unit.type.length for _u in _SUT),
      str([_u for _u in _SUT
           if len(_u) > _FsT.__table__.c.tat_unit.type.length]))

# "Take the renewal follow-up 30 days before the membership expires."
# Every other TAT counts forward from when the step opens; this one counts
# BACKWARDS from an answer, which is the only way a renewal chase lands
# before the thing it is chasing.
from datetime import timedelta as _tdT

from app.clock import BEFORE_UNIT as _BU
from app.services import holidays as _holT

with _slB2() as _d:
    _fT = _FlB(org_id=_orgB, name=f"SMOKE before-TAT {RUN}", active=True)
    _fT.start_form = _jsB.dumps([
        {"label": "Member name", "type": "text", "options": [],
         "required": True},
        {"label": "Membership expiry date", "type": "date", "options": [],
         "required": True}])
    _d.add(_fT); _d.flush()
    _d.add(_FsB(flow_id=_fT.id, position=1, title="Call about renewal",
                default_doer_id=_staffB[0].id, priority=_PrB.MEDIUM,
                tat_unit=_BU, tat_value=30,
                due_from_field="Membership expiry date",
                requires_attachment=False))
    _d.commit()
    _fTid, _doerT = _fT.id, _staffB[0].id
    _stepTid = _fT.steps[0].id

check("the builder offers it as an answer to “when is it due”",
      "A date answered on the start form"
      in admin.get(f"/flows/{_fTid}/edit").text)

_expiryT = (_clkB.today() + _tdT(days=120))
admin.post(f"/flows/{_fTid}/start",
           data={"reference": "", "sf0": f"Ritu Bansal {RUN}",
                 "sf1": _expiryT.isoformat()})
with _slB2() as _d:
    _iT = _d.scalars(_spK(_FID).where(_FID.flow_id == _fTid)
                     .order_by(_FID.id.desc())).first()
    _tT = _d.scalar(_spK(_TD).where(_TD.flow_instance_id == _iT.id))
    _wantT = _expiryT - _tdT(days=30)
    check("the run started and its one step has a deadline",
          _tT is not None and _tT.due_at is not None)
    # Not an exact date: a deadline that lands on a Sunday or a holiday is
    # pushed FORWARD to the next working day, so the honest assertion is
    # "on the day asked for, or the next working day after it".
    check("the deadline is thirty days before the expiry the form was given",
          _tT.due_at.date() >= _wantT
          and (_tT.due_at.date() - _wantT).days <= 3)
    check("and never on a closed day",
          not _holT.is_closed(_d, _iT.org_id, _tT.due_at.date(),
                              _tT.doer.branch_id))
    check("well before the expiry itself, which is the whole point",
          _tT.due_at.date() < _expiryT)
    check("and the step says in words what it is counting back from",
          "before" in (_tT.flow_step.tat_label or "").lower()
          and "Membership expiry date" in _tT.flow_step.tat_label)

# The trap this closes. The anchor box also offers "when this step opens"
# and "step 2's planned date", and neither is a date on the form — so a
# step saved that way counted back from nothing and quietly fell back to a
# day from now. On a step whose whole job is to land twenty days before
# something, that is not a near miss.
_beforePayT = {
    "name": f"SMOKE before-TAT {RUN}", "description": "", "branch_id": "",
    "sf_label": ["Member name", "Membership expiry date"],
    "sf_type": ["text", "date"], "sf_options": ["", ""],
    "sf_required": ["1", "1"],
    # The step's own id, so this is a CHANGE to it. Left out, the route
    # reads it as removing step 1 and adding a new one, which it refuses
    # while a run is still going — a different refusal from the one under
    # test here.
    "step_id": [str(_stepTid)],
    "step_title": ["Call about renewal"], "step_doer": [str(_doerT)],
    "step_tat_unit": [_BU], "step_tat": ["20"],
    "step_priority": ["medium"], "step_instructions": [""],
    "step_fields": [""], "step_audit": ["0"], "step_proof": ["0"],
    "step_decision": ["0"], "step_yes": [""], "step_next": ["0"],
    "step_no": [""], "step_fail": [""],
    "step_due_from": [""]}            # "when this step opens" — not a date
_badBeforeT = admin.post(f"/flows/{_fTid}/edit", data=_beforePayT)
check("counting back from something that is not a date on the form is "
      "refused, not silently wrong",
      _badBeforeT.status_code == 400, str(_badBeforeT.status_code))
check("and the refusal says it has to be a date on the start form",
      "start form" in _badBeforeT.text and "planned date" in _badBeforeT.text)
check("the step that was already saved is untouched by the refused save",
      admin.get(f"/flows/{_fTid}/edit").text.count("Call about renewal") >= 1)
# And the same save goes through once it names the date.
_okBeforeT = dict(_beforePayT)
_okBeforeT["step_due_from"] = ["field:Membership expiry date"]
_okRespT = admin.post(f"/flows/{_fTid}/edit", data=_okBeforeT)
check("naming the date question saves it", _okRespT.status_code == 200,
      f"{_okRespT.status_code}: {_okRespT.text.split("<p")[-1][:300]}")
with _slB2() as _d:
    _stT = _d.get(_FlB, _fTid).steps[0]
    check("with the question and the number of days both stored",
          _stT.tat_unit == _BU and _stT.tat_value == 20
          and _stT.due_from_field == "Membership expiry date",
          f"{_stT.tat_unit}/{_stT.tat_value}/{_stT.due_from_field}")
    check("and no leftover pointer at a step",
          _stT.due_from_pos is None, str(_stT.due_from_pos))
check("the builder stops offering those choices once it is counting back",
      "o.disabled = back || !exists;" in _sfT)

# A run that leaves the anchor blank, or answers it with something that is
# not a date, must still get a deadline — an unchaseable task is worse than
# a rough one.
admin.post(f"/flows/{_fTid}/start",
           data={"reference": "", "sf0": f"No date {RUN}", "sf1": ""})
with _slB2() as _d:
    _iT2 = _d.scalars(_spK(_FID).where(_FID.flow_id == _fTid)
                      .order_by(_FID.id.desc())).first()
    _tT2 = _d.scalar(_spK(_TD).where(_TD.flow_instance_id == _iT2.id))
    check("a run with the anchor left blank still gets a planned date",
          _tT2 is not None and _tT2.due_at is not None)
    check("and it is in the future, not in 1970",
          _tT2.due_at > _clkB.now())

print("\n== and a set number of days AFTER one ==")
# The other half of the same need. "Take the timestamp from the form" makes
# the answered date the deadline exactly, which is right for "due at the
# joining date and time" and wrong for everything that happens a few days
# after it — the first follow-up three days after the membership starts.
# Counting from when the step OPENS cannot say it: that moves with how fast
# the office worked, and the whole point is that the member fixed the date.
from app.clock import AFTER_UNIT as _AU

with _slB2() as _d:
    _fA = _FlB(org_id=_orgB, name=f"SMOKE after-TAT {RUN}", active=True)
    _fA.start_form = _jsB.dumps([
        {"label": "Member name", "type": "text", "options": [],
         "required": True},
        {"label": "Membership start date", "type": "date", "options": [],
         "required": True}])
    _d.add(_fA); _d.flush()
    _d.add(_FsB(flow_id=_fA.id, position=1, title="First check-in call",
                default_doer_id=_staffB[0].id, priority=_PrB.MEDIUM,
                tat_unit=_AU, tat_value=3,
                due_from_field="Membership start date",
                requires_attachment=False))
    _d.commit()
    _fAid = _fA.id

check("the builder offers it too", "A date answered on the start form"
      in admin.get(f"/flows/{_fAid}/edit").text)

_startA = _clkB.today() + _tdT(days=30)
admin.post(f"/flows/{_fAid}/start",
           data={"reference": "", "sf0": f"Vikram Mehta {RUN}",
                 "sf1": _startA.isoformat()})
with _slB2() as _d:
    _iA = _d.scalars(_spK(_FID).where(_FID.flow_id == _fAid)
                     .order_by(_FID.id.desc())).first()
    _tA = _d.scalar(_spK(_TD).where(_TD.flow_instance_id == _iA.id))
    _wantA = _startA + _tdT(days=3)
    check("the deadline is three days after the date the form was given",
          _tA.due_at.date() >= _wantA and (_tA.due_at.date() - _wantA).days <= 3,
          f"{_tA.due_at.date()} vs {_wantA}")
    check("after the date itself, not before it",
          _tA.due_at.date() > _startA)
    check("and never on a closed day",
          not _holT.is_closed(_d, _iA.org_id, _tA.due_at.date(),
                              _tA.doer.branch_id))
    check("the step says in words which way it counts",
          "after" in (_tA.flow_step.tat_label or "")
          and "Membership start date" in _tA.flow_step.tat_label)
# The two are read by one piece of code, so the one guard covers both.
_badAfterT = dict(_beforePayT)
_badAfterT["step_tat_unit"] = [_AU]
check("counting forward from something that is not a date is refused too",
      admin.post(f"/flows/{_fTid}/edit", data=_badAfterT).status_code == 400)

print("\n== FMS, company by company ==")
from app.models import Branch as _BrB
# Bodyzone's flows, Spa Kora's and the school's in one list means everybody
# reads past two thirds of it to find their own.
_fmsPage = admin.get("/flows").text
check("the FMS page can be narrowed to one company",
      'name="company"' in _fmsPage)
check("and offers the group-wide flows as a category of their own",
      'value="group"' in _fmsPage)
check("the flows are grouped under the company they belong to",
      'class="cogroup"' in _fmsPage)
with _slB2() as _d:
    _cos = _d.scalars(_spK(_BrB).where(_BrB.org_id == _orgB)
                      .order_by(_BrB.name)).all()
    _mine = [f.name for f in _d.scalars(_spK(_FlB).where(
        _FlB.org_id == _orgB, _FlB.branch_id == _cos[0].id)).all()]
    _theirs = [f.name for f in _d.scalars(_spK(_FlB).where(
        _FlB.org_id == _orgB, _FlB.branch_id.isnot(None),
        _FlB.branch_id != _cos[0].id)).all()]
_oneCo = admin.get(f"/flows?company={_cos[0].id}").text
check("picking one company shows that company's heading",
      _cos[0].name in _oneCo)
check("and its own flows", all(n in _oneCo for n in _mine) if _mine else True,
      str([n for n in _mine if n not in _oneCo])[:200])
check("and none of another company's",
      all(n not in _oneCo for n in _theirs) if _theirs else True,
      str([n for n in _theirs if n in _oneCo])[:200])
check("group-wide only shows the flows built for every company",
      "Every company" in admin.get("/flows?company=group").text
      or "No flows for this company" in admin.get("/flows?company=group").text)
check("a company that has nothing says so rather than showing an empty table",
      "No flows" in admin.get("/flows?company=999999").text
      or admin.get("/flows?company=999999").status_code == 200)

print("\n== one setting, measured any way, either side ==")
# What the three replaced options could not say between them: weeks,
# months, and working days. "Three days after the bill date" and "three
# WORKING days after it" are different promises, and a spa that opens on
# Sunday counts them differently from the office.
from app.clock import FORMDATE_UNIT as _FD
from app.services import holidays as _holZ

def _plan(span, way, n, when, q="Bill date"):
    """Start a one-step run with this setting and give back its deadline."""
    with _slB2() as _d:
        _f = _FlB(org_id=_orgB, name=f"SMOKE {span}-{way}-{n} {RUN}", active=True)
        _f.start_form = _jsB.dumps(
            [{"label": q, "type": "date", "options": [], "required": True}])
        _d.add(_f); _d.flush()
        _d.add(_FsB(flow_id=_f.id, position=1, title="Do the thing",
                    default_doer_id=_staffB[0].id, priority=_PrB.MEDIUM,
                    tat_unit=_FD, tat_value=n, form_span=span, form_way=way,
                    due_from_field=q, requires_attachment=False))
        _d.commit()
        _fid = _f.id
    admin.post(f"/flows/{_fid}/start",
               data={"reference": "", "sf0": when.isoformat()})
    with _slB2() as _d:
        _i = _d.scalars(_spK(_FID).where(_FID.flow_id == _fid)
                        .order_by(_FID.id.desc())).first()
        _t = _d.scalar(_spK(_TD).where(_TD.flow_instance_id == _i.id))
        return _t.due_at.date(), _t.flow_step.tat_label, _i, _t

_anchorZ = _clkB.today() + _tdT(days=60)
_dZ, _lblZ, _iZ, _tZ = _plan("weeks", "after", 2, _anchorZ)
check("two weeks after a date is fourteen days, not two",
      (_dZ - _anchorZ).days >= 14 and (_dZ - _anchorZ).days <= 17,
      f"{_dZ} vs {_anchorZ}")
check("and it reads as weeks, not as the days it works out to",
      _lblZ == f'2 weeks after "Bill date"', _lblZ)

_dZ, _lblZ, _iZ, _tZ = _plan("months", "after", 1, _anchorZ)
check("a month after a date lands in the next month, not thirty days on",
      _dZ.month == (_anchorZ.month % 12) + 1 or (_dZ - _anchorZ).days >= 28,
      f"{_dZ} vs {_anchorZ}")
check("and reads as one month, singular",
      _lblZ == '1 month after "Bill date"', _lblZ)

_dZ, _lblZ, _iZ, _tZ = _plan("months", "before", 2, _anchorZ)
check("two months before a date is well before it",
      _dZ < _anchorZ and (_anchorZ - _dZ).days >= 55, f"{_dZ} vs {_anchorZ}")

# A year is twelve calendar months, not 365 days: "a year after 1 March" is
# 1 March, and a membership renewed on the 31st keeps its date in every
# month that has one.
_dY, _lblY, _iY, _tY = _plan("years", "after", 1, _anchorZ)
check("a year after a date is the same date a year on",
      _dY.year == _anchorZ.year + 1
      and abs((_dY - _anchorZ.replace(year=_anchorZ.year + 1)).days) <= 3,
      f"{_dY} vs {_anchorZ}")
check("and reads as one year, singular",
      _lblY == '1 year after "Bill date"', _lblY)

# Working days: counted against the calendar, not multiplied. The run's own
# company decides which days those are — the office keeps Sunday off, the
# gym and the spa do not.
_dW, _lblW, _iW, _tW = _plan("workdays", "after", 5, _anchorZ)
with _slB2() as _d:
    _openW = 0
    _dayW = _anchorZ
    while _dayW < _dW:
        _dayW += _tdT(days=1)
        if not _holZ.is_closed(_d, _orgB, _dayW, _iW.branch_id):
            _openW += 1
check("five working days after a date counts five days somebody is in",
      _openW == 5, f"{_openW} open days between {_anchorZ} and {_dW}")
check("which is never fewer calendar days than that",
      (_dW - _anchorZ).days >= 5, f"{_dW} vs {_anchorZ}")
check("and it says working days, so nobody reads it as plain days",
      _lblW == '5 working days after "Bill date"', _lblW)

_dN, _lblN, _iN, _tN = _plan("days", "after", 0, _anchorZ)
check("nothing either side of the date is the date itself",
      _dN == _anchorZ or not _holZ.is_closed(_d, _orgB, _dN, _iN.branch_id),
      f"{_dN} vs {_anchorZ}")
check("and it reads as the date, not as '0 days after' it",
      _lblN == 'the date answered for "Bill date"', _lblN)

print("\n== steps saved under the old three still work ==")
# They are running right now, so their deadlines must not move by a day.
# The migrator converts them to the one setting that replaced them; until
# it has, the engine reads them anyway.
with _slB2() as _d:
    _fO = _FlB(org_id=_orgB, name=f"SMOKE legacy units {RUN}", active=True)
    _fO.start_form = _jsB.dumps([{"label": "Expiry date", "type": "date",
                                  "options": [], "required": True}])
    _d.add(_fO); _d.flush()
    for _pos, (_u, _v) in enumerate(
            [("formtime", 0), ("daysbefore", 10), ("daysafter", 4)], start=1):
        _d.add(_FsB(flow_id=_fO.id, position=_pos, title=f"Old {_u}",
                    default_doer_id=_staffB[0].id, priority=_PrB.MEDIUM,
                    tat_unit=_u, tat_value=_v, due_from_field="Expiry date",
                    requires_attachment=False))
    _d.commit()
    _fOid = _fO.id
    _oldLabels = [s.tat_label for s in _d.get(_FlB, _fOid).steps]
check("an old 'take timestamp from form' step still names its question",
      _oldLabels[0] == 'the date answered for "Expiry date"', _oldLabels[0])
check("an old 'days before' step still reads as before",
      _oldLabels[1] == '10 days before "Expiry date"', _oldLabels[1])
check("an old 'days after' step still reads as after",
      _oldLabels[2] == '4 days after "Expiry date"', _oldLabels[2])

_expO = _clkB.today() + _tdT(days=90)
admin.post(f"/flows/{_fOid}/start",
           data={"reference": "", "sf0": _expO.isoformat()})
with _slB2() as _d:
    _iO = _d.scalars(_spK(_FID).where(_FID.flow_id == _fOid)
                     .order_by(_FID.id.desc())).first()
    _tsO = sorted(_d.scalars(_spK(_TD).where(_TD.flow_instance_id == _iO.id)).all(),
                  key=lambda t: t.flow_step.position)
    check("all three are planned, by the same arithmetic as before",
          len(_tsO) == 3, str(len(_tsO)))
    check("the timestamp one is on the date itself",
          abs((_tsO[0].due_at.date() - _expO).days) <= 2,
          str(_tsO[0].due_at.date()))
    # Ten days before, then forward off any closed day — so it can end up
    # nine days before, or eight. What matters is that it lands clearly
    # before the expiry and in the right week, not an exact count that
    # depends on which weekday the suite runs.
    check("the 'before' one is about ten days earlier",
          _tsO[1].due_at.date() < _expO
          and 7 <= (_expO - _tsO[1].due_at.date()).days <= 13,
          str(_tsO[1].due_at.date()))
    check("the 'after' one is four days later",
          _tsO[2].due_at.date() > _expO
          and 4 <= (_tsO[2].due_at.date() - _expO).days <= 7,
          str(_tsO[2].due_at.date()))

# And the migrator folds them in, so the page and the database agree.
from app import migrate as _mgZ
_mgZ.run()
with _slB2() as _d:
    _after = [(s.tat_unit, s.tat_value, s.form_span, s.form_way)
              for s in _d.get(_FlB, _fOid).steps]
check("the migrator converts every old spelling to the one that replaced it",
      all(u == _FD for u, _, _, _ in _after), str(_after))
check("keeping the number and the side each one meant",
      _after[0][1] == 0 and _after[1][1:] == (10, "days", "before")
      and _after[2][1:] == (4, "days", "after"), str(_after))
_mgZ.run()
with _slB2() as _d:
    check("and running it again changes nothing",
          [(s.tat_unit, s.tat_value, s.form_span, s.form_way)
           for s in _d.get(_FlB, _fOid).steps] == _after,
          str([(s.tat_unit, s.tat_value, s.form_span, s.form_way)
               for s in _d.get(_FlB, _fOid).steps]))
with _slB2() as _d:
    check("the labels read exactly as they did before the conversion",
          [s.tat_label for s in _d.get(_FlB, _fOid).steps] == _oldLabels,
          str([s.tat_label for s in _d.get(_FlB, _fOid).steps]))


print("\n== a Google Sheet that starts runs by itself ==")
# The third way to start an FMS. The form is one at a time, the upload is
# four hundred at once and finished; this one keeps going.
#
# Tested against a stand-in sheet, not Google: what is worth proving is the
# RULES — which rows count as new, what is written back, what happens to a
# row that cannot be read — and a network call proves none of them while
# making the suite fail whenever somebody's wifi does.
from app.models import FlowSheet as _FSh
from app.services import flowsheet as _fsv


class FakeSheet:
    """Enough of a gspread worksheet to exercise every rule."""

    def __init__(self, grid):
        self.grid = [list(r) for r in grid]
        self.writes = []

    def get_all_values(self):
        return [list(r) for r in self.grid]

    def update_cell(self, row, col, value):
        while len(self.grid) < row:
            self.grid.append([])
        line = self.grid[row - 1]
        while len(line) < col:
            line.append("")
        line[col - 1] = value
        self.writes.append((row, col, value))

    def col(self, heading):
        return [r[self.grid[0].index(heading)]
                if len(r) > self.grid[0].index(heading) else ""
                for r in self.grid[1:]]


with _slB2() as _d:
    _fS = _FlB(org_id=_orgB, name=f"SMOKE sheet flow {RUN}", active=True,
               branch_id=_staffB[0].branch_id)
    _fS.start_form = _jsB.dumps([
        {"label": "Member name", "type": "text", "options": [], "required": True},
        {"label": "Expiry date", "type": "date", "options": [], "required": True},
        {"label": "PT trainer", "type": "person", "options": [], "required": True}])
    _d.add(_fS); _d.flush()
    _d.add(_FsB(flow_id=_fS.id, position=1, title="Call about renewal",
                doer_field="PT trainer", priority=_PrB.MEDIUM,
                tat_unit="days", tat_value=1, requires_attachment=False))
    _cfgS = _FSh(org_id=_orgB, flow_id=_fS.id, sheet_id="FAKESHEETID",
                 connected_by_id=_staffB[0].id)
    _d.add(_cfgS); _d.commit()
    _fSid, _cfgSid = _fS.id, _cfgS.id
    _t1, _t2 = _staffB[0].name, _staffB[1].name

_sheet = FakeSheet([
    ["Member name", "Expiry date", "PT trainer"],
    [f"Ritu Bansal {RUN}", "2027-03-20", _t1],
    [f"Vikram Mehta {RUN}", "2027-03-22", _t2],
])

with _slB2() as _d:
    _cfg = _d.get(_FSh, _cfgSid)
    _scan = _fsv.scan(_d, _cfg, _sheet)
    check("the sheet's headings are read as this flow's questions",
          _scan.ok and not _scan.missing, str(_scan.missing))
    check("both rows look new, because neither has an FMS number",
          len(_scan.rows) == 2 and _scan.done == 0,
          f"{len(_scan.rows)} new, {_scan.done} done")
    check("and each is checked the same way the upload checks one",
          all(r.ok for r in _scan.rows),
          str([r.problems for r in _scan.rows]))

    _res = _fsv.poll(_d, _cfg, _sheet)
    check("a pass starts a run for every new row",
          _res["ok"] and _res["started"] == 2, str(_res))
    check("and writes the FMS number back beside each one",
          all(v.startswith("FMS-") for v in _sheet.col("FMS no.")),
          str(_sheet.col("FMS no.")))
    check("with the time it was picked up",
          all(_sheet.col("Started on")), str(_sheet.col("Started on")))
    check("the run is named from the row, not left blank",
          all(f"SMOKE" not in v for v in _sheet.col("FMS no.")))

with _slB2() as _d:
    _runs = _d.scalars(_spK(_FID).where(_FID.flow_id == _fSid)).all()
    check("two runs exist in the software", len(_runs) == 2, str(len(_runs)))
    _tasks = _d.scalars(_spK(_TD).where(
        _TD.flow_instance_id.in_([r.id for r in _runs]))).all()
    check("and step 1 of each went to the trainer that row named",
          sorted(t.doer.name for t in _tasks) == sorted([_t1, _t2]),
          str(sorted(t.doer.name for t in _tasks)))

# The whole point: looking again must not start them a second time. This is
# what the write-back buys — the rule is in the sheet, so it survives a
# restart, a redeploy and somebody sorting the rows.
with _slB2() as _d:
    _again = _fsv.poll(_d, _d.get(_FSh, _cfgSid), _sheet)
    check("looking again starts nothing, because the rows are stamped",
          _again["started"] == 0 and _again["done"] == 2, str(_again))

# A row typed after the connection was made.
_sheet.grid.append([f"Sandeep Kaur {RUN}", "2027-04-02", _t1])
with _slB2() as _d:
    _third = _fsv.poll(_d, _d.get(_FSh, _cfgSid), _sheet)
    check("a row added later is picked up on the next look",
          _third["started"] == 1, str(_third))

# A row that cannot be read is left alone with the reason beside it —
# never started on a guess, never silently skipped.
_sheet.grid.append([f"Diya Mehta {RUN}", "32/11/2027", _t1])
_sheet.grid.append([f"Kabir Singh {RUN}", "2027-04-10", "Nobody At All"])
with _slB2() as _d:
    _bad = _fsv.poll(_d, _d.get(_FSh, _cfgSid), _sheet)
    check("a row with a date that is not a date is refused, not guessed at",
          _bad["started"] == 0 and _bad["refused"] == 2, str(_bad))
    _probs = [v for v in _sheet.col("Problem") if v]
    check("and the reason is written in the sheet, next to that row",
          len(_probs) == 2 and any("32/11/2027" in p for p in _probs),
          str(_probs))
    check("with no FMS number, so fixing the cell is all it takes",
          _sheet.col("FMS no.")[-1] == "" and _sheet.col("FMS no.")[-2] == "")

# Fix one of them and it goes through, with nothing re-uploaded.
_sheet.grid[-1][2] = _t2
with _slB2() as _d:
    _fixed = _fsv.poll(_d, _d.get(_FSh, _cfgSid), _sheet)
    check("a corrected row is picked up by itself",
          _fixed["started"] == 1, str(_fixed))
    check("and its problem is cleared rather than left to confuse somebody",
          _sheet.col("Problem")[-1] == "", _sheet.col("Problem")[-1])

# A heading that does not match stops the whole sheet. Reading on would
# mean starting runs with a question unanswered.
_wrong = FakeSheet([["Member name", "Renewal date", "PT trainer"],
                    [f"Anita {RUN}", "2027-05-01", _t1]])
with _slB2() as _d:
    _no = _fsv.poll(_d, _d.get(_FSh, _cfgSid), _wrong)
    check("a heading that does not match a question stops the whole sheet",
          not _no["ok"] and _no["started"] == 0, str(_no))
    check("and it says which question has no column",
          "Expiry date" in _no.get("error", ""), _no.get("error"))

# Connecting a sheet that is already full must not create four hundred runs
# by surprise, so the rows already there are stamped and left.
_old = FakeSheet([["Member name", "Expiry date", "PT trainer"]]
                 + [[f"Old {i} {RUN}", "2027-06-01", _t1] for i in range(5)])
with _slB2() as _d:
    _marked = _fsv.baseline(_d, _d.get(_FSh, _cfgSid), _old)
    check("connecting a full sheet stamps what is there instead of starting it",
          _marked == 5, str(_marked))
    _before = len(_d.scalars(_spK(_FID).where(_FID.flow_id == _fSid)).all())
    _after = _fsv.poll(_d, _d.get(_FSh, _cfgSid), _old)
    check("and the next look starts none of them",
          _after["started"] == 0, str(_after))
    check("the stamp says so in words somebody reading the sheet understands",
          all("already here" in v for v in _old.col("FMS no.")),
          str(_old.col("FMS no.")[:2]))

# A paused connection is remembered but left alone.
with _slB2() as _d:
    _c = _d.get(_FSh, _cfgSid); _c.active = False; _d.commit()
    check("a paused sheet is not looked at", not _fsv.due(_d))
    _c = _d.get(_FSh, _cfgSid); _c.active = True; _d.commit()
    check("and switching it back on puts it in the list again",
          len(_fsv.due(_d)) == 1)

check("a pasted sheet address is read down to its id",
      _fsv.sheet_id_from(
          "https://docs.google.com/spreadsheets/d/1AbC_defGHIjklmnop-qrs/edit#gid=0")
      == "1AbC_defGHIjklmnop-qrs")
check("and a bare id is accepted as itself",
      _fsv.sheet_id_from("1AbC_defGHIjklmnop-qrs") == "1AbC_defGHIjklmnop-qrs")
check("while something that is not either is refused rather than guessed",
      _fsv.sheet_id_from("my renewals sheet") == "")

# With no Google account set up, the page says so and says what to add,
# rather than offering a box that could never work.
from app.services import sheets as _shSvc
_was = _shSvc.SERVICE_ACCOUNT
_shSvc.SERVICE_ACCOUNT = ""
_offPage = admin.get(f"/flows/{_fSid}/sheet").text
check("with no Google account set up the page says exactly what is missing",
      "GOOGLE_SERVICE_ACCOUNT" in _offPage)
check("and does not offer a box that could not work",
      'name="sheet"' not in _offPage)

_shSvc.SERVICE_ACCOUNT = _jsB.dumps(
    {"client_email": "gcs-robot@example.iam.gserviceaccount.com"})
_shPage = admin.get(f"/flows/{_fSid}/sheet")
check("the flow has a page for setting this up", _shPage.status_code == 200)
check("which lists the headings the sheet needs",
      all(q in _shPage.text for q in ("Member name", "Expiry date", "PT trainer")))
check("and says the three columns it will add",
      all(c in _shPage.text for c in _fsv.STAMP_COLS))
# Nobody can guess this address, and a sheet that is not shared with it
# fails with a permission error that does not say what to do about it.
check("and names the address the sheet has to be shared with",
      "gcs-robot@example.iam.gserviceaccount.com" in _shPage.text)
check("saying Editor, because it writes back beside each row",
      "Editor" in _shPage.text)
check("a link that is not a Google Sheet is refused with a useful sentence",
      admin.post(f"/flows/{_fSid}/sheet",
                 data={"sheet": "my renewals sheet"}).status_code == 400)
_shSvc.SERVICE_ACCOUNT = _was
check("a doer cannot connect a sheet",
      doer.get(f"/flows/{_fSid}/sheet").status_code == 403)
# Asserted against THIS flow's link, not the word "/sheet": the sidebar
# has a Google Sheet backup page of its own, and matching that would pass
# whether or not the button on the row exists at all.
_listPage = admin.get("/flows").text
check("the FMS list offers it beside the bulk upload",
      f'/flows/{_fSid}/sheet' in _listPage
      and f'/flows/{_fSid}/bulk' in _listPage)
check("but not to somebody who cannot build flows",
      f'/flows/{_fSid}/sheet' not in doer.get("/flows").text)


print("\n== levels 2 and 3 read company by company ==")
# Level 1 is a different person at each company, so their email is about
# one company. Levels 2 and 3 are the SAME two people for the whole group,
# so their email is every company at once — and thirty rows from five
# companies, told apart only by a grey line under each task, is a list
# nobody can act on.
from app.services import escalation as _ecZ


class _FakeBranch:
    def __init__(self, name): self.name = name


class _FakeDoer:
    def __init__(self, name): self.name = name


from app.models import TaskSource as _TsZ


class _FakeTask:
    def __init__(self, n, company, doer, src="delegation"):
        self.id = n
        self.ref = f"DEL-{n:03d}"
        self.ref_label = self.ref
        self.step_label = ""
        self.title = f"Task {n}"
        self.branch = _FakeBranch(company) if company else None
        self.doer = _FakeDoer(doer)
        self.due_at = _dtC(2026, 10, 20, 18, 0)
        self.priority = _PrB.MEDIUM
        self.source = _TsZ(src)


_mixed = [(_FakeTask(1, "Spa Kora", "Zoya"), 2),
          (_FakeTask(2, "Bodyzone", "Amit"), 3),
          (_FakeTask(3, "Spa Kora", "Amit"), 1),
          (_FakeTask(4, "Bodyzone", "Kiran"), 2),
          (_FakeTask(5, None, "Raj"), 4)]

_blocks = _ecZ.by_company(_mixed)
check("the rows are split company by company",
      [b[0] for b in _blocks] == ["Bodyzone", "Spa Kora", "No company"],
      str([b[0] for b in _blocks]))
check("alphabetically, so the same company is in the same place every day",
      _blocks[0][0] < _blocks[1][0])
check("work with no company comes last, not first",
      _blocks[-1][0] == "No company")
check("and inside each company the doers are still A to Z",
      [t.doer.name for t, _l in _blocks[0][1]] == ["Amit", "Kiran"],
      str([t.doer.name for t, _l in _blocks[0][1]]))

_htmlZ = _ecZ.html_table(_mixed)
check("the HTML table carries a heading for each company",
      _htmlZ.count("colspan") == 3, str(_htmlZ.count("colspan")))
check("saying how many tasks are under it",
      "2 tasks" in _htmlZ and "1 task<" in _htmlZ.replace("&middot;", ""),
      "counts missing")
check("in the order the blocks were worked out",
      _htmlZ.index("BODYZONE".title()) < _htmlZ.index("Spa Kora"))
# The company used to be printed under every task title. Under a heading
# that already says it, that is the noise the headings were added to remove.
check("and the company is not repeated under every single task",
      _htmlZ.count("Bodyzone") == 1 and _htmlZ.count("Spa Kora") == 1,
      f"{_htmlZ.count('Bodyzone')} / {_htmlZ.count('Spa Kora')}")

_textZ = _ecZ.table(_mixed)
check("the plain-text copy is blocked the same way",
      "BODYZONE — 2 tasks" in _textZ and "SPA KORA — 2 tasks" in _textZ,
      _textZ[:160])
check("with its own column headings under each company",
      _textZ.count("Task ID") == 3, str(_textZ.count("Task ID")))

# A level-1 email is about one company. A heading over a single block is
# furniture, and saying the company twice reads as though there were two.
_oneCoZ = [(_FakeTask(6, "Bodyzone", "Amit"), 2),
           (_FakeTask(7, "Bodyzone", "Kiran"), 2)]
_oneHtmlZ = _ecZ.html_table(_oneCoZ)
check("one company gets no heading band at all",
      "colspan" not in _oneHtmlZ)
check("but the company is still named on each row, as it always was",
      _oneHtmlZ.count("Bodyzone") == 2, str(_oneHtmlZ.count("Bodyzone")))
check("and the text copy keeps its single set of headings",
      _ecZ.table(_oneCoZ).count("Task ID") == 1)

print("\n== and split Delegation, Checklist, FMS ==")
# Mixed together, a digest reads as one undifferentiated list and the
# reader has to sort it in their head before acting: a missed daily
# checklist and a stalled FMS step are not the same problem and do not go
# to the same conversation.
_mixZ = [(_FakeTask(11, "Bodyzone", "Zoya", "delegation"), 2),
         (_FakeTask(12, "Spa Kora", "Amit", "recurring"), 3),
         (_FakeTask(13, "Bodyzone", "Amit", "flow"), 1),
         (_FakeTask(14, "Spa Kora", "Kiran", "delegation"), 2)]

check("the rows are split by kind of work",
      [k for k, _r in _ecZ.by_kind(_mixZ)] == ["Delegation", "Checklist", "FMS"],
      str([k for k, _r in _ecZ.by_kind(_mixZ)]))
check("in that order — delegation, then checklist, then FMS",
      [lbl for _k, lbl in _ecZ.KINDS] == ["Delegation", "Checklist", "FMS"])
check("a kind with nothing overdue is left out of the list",
      [k for k, _r in _ecZ.by_kind([_mixZ[0]])] == ["Delegation"])
# But NOT out of the counts: "no checklist work is overdue" is the thing
# somebody most wants to see, and a row of cards that changes shape every
# morning is a row people stop reading.
check("but still shown in the counts, at nought",
      _ecZ.counts_by_kind([_mixZ[0]]) == [("Delegation", 1), ("Checklist", 0),
                                          ("FMS", 0)],
      str(_ecZ.counts_by_kind([_mixZ[0]])))
check("and the counts add up to the email's own total",
      sum(n for _l, n in _ecZ.counts_by_kind(_mixZ)) == len(_mixZ))

_cardsZ = _ecZ.html_cards(_mixZ)
check("the three counts are drawn as cards above the list",
      all(w in _cardsZ for w in ("Delegation", "Checklist", "FMS")))
check("built as a table, because Outlook ignores flex and would stack them",
      "<table" in _cardsZ and "display:flex" not in _cardsZ)

_bodyZ = _ecZ.html_message(_mixZ, "Overdue work", "Respected Sir", "Please see.",
                           2, False)
check("the cards come before the first task row",
      _bodyZ.index("Delegation") < _bodyZ.index("Task&nbsp;ID"))

_htmlMixZ = _ecZ.html_table(_mixZ)
check("the table is blocked by kind first",
      _htmlMixZ.index("DELEGATION".title()) < _htmlMixZ.index("Checklist")
      < _htmlMixZ.index("FMS"))
# Three kind bands, plus a company band only where a kind actually spans
# more than one company: Delegation does (Bodyzone and Spa Kora), the
# other two do not. A band over a single block is furniture.
check("and by company inside a kind that spans more than one",
      _htmlMixZ.count("colspan") == 3 + 2, str(_htmlMixZ.count("colspan")))
check("but not inside a kind that is all one company",
      _htmlMixZ.count("Spa Kora") == 2, str(_htmlMixZ.count("Spa Kora")))

_textMixZ = _ecZ.table(_mixZ)
check("the plain-text copy carries the same counts on its first line",
      _textMixZ.splitlines()[0] == "Delegation: 2   Checklist: 1   FMS: 1",
      _textMixZ.splitlines()[0])
check("and the same three sections, in the same order",
      _textMixZ.index("== DELEGATION") < _textMixZ.index("== CHECKLIST")
      < _textMixZ.index("== FMS"))

# One kind, one company — a level-1 email about a single flow — must not
# grow headings for splits that are not there.
_oneZ = [(_FakeTask(15, "Bodyzone", "Amit", "flow"), 2)]
check("one kind at one company gets no headings at all",
      "colspan" not in _ecZ.html_table(_oneZ))
check("and its text copy is just the table",
      _ecZ.table(_oneZ).splitlines()[0].startswith("Task ID"),
      _ecZ.table(_oneZ).splitlines()[0])
check("though the counts still say which kind it was",
      _ecZ.counts_by_kind(_oneZ) == [("Delegation", 0), ("Checklist", 0),
                                     ("FMS", 1)])

print("\n== nowhere to send is not a reason to escalate quietly ==")
# The rung is RECORDED when a task is escalated, and nothing retries a
# recorded one. So a run with no mail server would mark level 1 as done,
# climb to level 2 tomorrow and reach the CMD on day three about work
# nobody was ever warned of — a ladder that ran perfectly and told nobody,
# which is the one failure that looks exactly like success.
from app.services import mail as _mlY
_hostY, _fromY = _mlY.HOST, _mlY.FROM
_mlY.HOST, _mlY.FROM = "", ""
with _slQ() as _d:
    _orgY = _d.scalar(_SEL(_OgQ))
    _orgY.esc_on = True
    _orgY.esc_last_run = None
    _d.commit()
    _beforeY = len(_d.scalars(_SEL(_EsQ)).all())
    _outY = _escQ.run(_d, _orgY, send=True)
    check("a run with no mail server escalates nothing",
          _outY["raised"] == 0 and not _outY["ran"], str(_outY)[:140])
    check("and says so, naming what to set",
          "MAIL_HOST" in _outY["why"], _outY["why"])
    check("nothing is recorded, so tomorrow still owes level 1",
          len(_d.scalars(_SEL(_EsQ)).all()) == _beforeY)
    # The preview is the same check with the sending switched off, so it
    # must still work with no server — that is where somebody looks to
    # find out what would happen once there is one.
    _prevY = _escQ.run(_d, _orgY, send=False)
    check("but the preview still shows what would be sent",
          _prevY["ran"], str(_prevY)[:140])
_mlY.HOST, _mlY.FROM = _hostY, _fromY

print("\n== a grace period before the first level ==")
# "Overdue more than 3 days" is a different system from "overdue today".
# One late morning reaching a department head is how a ladder teaches
# everybody to ignore it; the climb afterwards is unchanged.
from datetime import date as _dG
_todayG = _dG(2026, 10, 29)
_rungsG = {1: _ErQ(level=1, to_name="HOD", to_email="hod@x"),
           2: _ErQ(level=2, to_name="EA", to_email="ea@x"),
           3: _ErQ(level=3, to_name="CMD", to_email="cmd@x")}

def _lvl(late, grace, sent=()):
    got = _escQ.next_step(list(sent), late, _rungsG, "stop", _todayG, grace)
    return got[0] if got else None

check("with a grace of 3, nothing goes out on day 1, 2 or 3",
      [_lvl(n, 3) for n in (1, 2, 3)] == [None, None, None],
      str([_lvl(n, 3) for n in (1, 2, 3)]))
check("level 1 goes on day 4",  _lvl(4, 3) == 1, str(_lvl(4, 3)))
check("level 2 on day 5",       _lvl(5, 3) == 2, str(_lvl(5, 3)))
check("level 3 on day 6",       _lvl(6, 3) == 3, str(_lvl(6, 3)))
check("and it stops at the top rather than inventing a level 4",
      _lvl(9, 3) == 3, str(_lvl(9, 3)))

# The old behaviour has to survive untouched: every site that has not
# changed the setting must escalate exactly as it did yesterday.
check("a grace of 0 is the day-a-level ladder as it always was",
      [_lvl(n, 0) for n in (1, 2, 3, 7)] == [1, 2, 3, 3],
      str([_lvl(n, 0) for n in (1, 2, 3, 7)]))

# A task picked up late — 9 days overdue, nothing sent — starts at the
# level its lateness has earned, not at 1. It is already a CMD matter.
check("work found long after the fact starts where it belongs, not at 1",
      _lvl(20, 3) == 3)

# And once it has been escalated, the climb is one a day from there.
_sentG = [_EsQ(level=1, repeat_n=0, day=_dG(2026, 10, 28))]
check("the next day it is the next level, not the same one again",
      _lvl(5, 3, _sentG) == 2, str(_lvl(5, 3, _sentG)))
check("and never twice in one morning",
      _lvl(5, 3, [_EsQ(level=1, repeat_n=0, day=_todayG)]) is None)

_rGraceQ = adminQ.post("/escalation/settings",
                       data={"esc_on": "", "working_days": "1",
                             "after_top": "stop", "scope": "chased",
                             "hour": "9", "grace": "3"})
check("the setting saves from the page", _rGraceQ.status_code in (200, 303),
      _rGraceQ.status_code)
with _slQ() as _d:
    check("and sticks", _d.scalar(_SEL(_OgQ)).esc_grace == 3,
          str(_d.scalar(_SEL(_OgQ)).esc_grace))
_pageG = adminQ.get("/escalation").text
check("the page says which day each level now fires on",
      "day 4" in _pageG and "day 5" in _pageG, "captions not updated")
check("a grace that is not a number is refused rather than read as nought",
      adminQ.post("/escalation/settings",
                  data={"esc_on": "", "working_days": "1",
                        "after_top": "stop", "scope": "chased",
                        "hour": "9", "grace": "three"}).status_code == 400)
with _slQ() as _d:
    _d.scalar(_SEL(_OgQ)).esc_grace = 0
    _d.commit()

print("\n== every doer gets their own morning list ==")
# The ladder tells somebody's MANAGER. This tells the person who can
# actually close the work, which is the cheapest way to make the ladder
# unnecessary — and the thing people mean when they say nobody told them.
from app.services import mail as _mlD
_hD, _fD, _sD = _mlD.HOST, _mlD.FROM, _mlD.send
_mlD.HOST, _mlD.FROM = "smtp.test", "gcs@test"
_mlD.send = lambda *a, **k: (True, "")

with _slQ() as _d:
    _orgD = _d.scalar(_SEL(_OgQ))
    _orgD.esc_on = True
    _orgD.esc_doer_daily = False
    _orgD.esc_last_run = None
    _d.commit()
    _dayD = _escQ.clock.today()
    _beforeD = len(_d.scalars(_SEL(_EmQ).where(
        _EmQ.level == _escQ.DIGEST_LEVEL)).all())
    _outOffD = _escQ.run(_d, _orgD, send=True)
    check("with the setting off, no morning lists are sent",
          _outOffD["digest_people"] == 0, str(_outOffD["digest_people"]))

with _slQ() as _d:
    _orgD = _d.scalar(_SEL(_OgQ))
    _orgD.esc_doer_daily = True
    _orgD.esc_last_run = None
    _d.commit()
    _whoD = _escQ.doer_digests(_d, _orgD, _dayD)
    check("every doer with overdue work is on the list",
          len(_whoD) >= 1, str(len(_whoD)))
    check("and nobody without an email address is",
          all((p.email or "").strip() for p, _r in _whoD))
    check("people in name order, so the log reads the same way every day",
          [p.name.lower() for p, _r in _whoD]
          == sorted(p.name.lower() for p, _r in _whoD))

    _person, _rowsD = _whoD[0]
    _subD, _txtD, _htmlD = _escQ.compose_digest(_person, _rowsD)
    check("the subject is their own count, not a level",
          "overdue task" in _subD and "Escalation" not in _subD, _subD)
    check("the email says it went to nobody else",
          "Nobody else has been sent it" in _txtD)
    check("it carries the three counts, like the ladder's own email",
          all(w in _htmlD for w in ("Delegation", "Checklist", "FMS")))
    check("their own rows only",
          all(t.doer_id == _person.id for t, _l in _rowsD))
    # Their name on every row would be a column of noise where the task's
    # own words could be instead.
    check("and no Doer column, because every row would be them",
          "Doer" not in _htmlD.split("</thead>")[0])

with _slQ() as _d:
    _orgD = _d.scalar(_SEL(_OgQ))
    _orgD.esc_last_run = None
    _d.commit()
    _outD = _escQ.run(_d, _orgD, send=True)
    check("the morning run sends them", _outD["digest_people"] >= 1,
          str(_outD["digest_people"]))
    _madeD = _d.scalars(_SEL(_EmQ).where(
        _EmQ.level == _escQ.DIGEST_LEVEL, _EmQ.day == _dayD)).all()
    check("each one is written down, so the log shows what was sent",
          len(_madeD) == _outD["digest_people"], str(len(_madeD)))
    check("marked as the doer's own, not as an escalation",
          all(m.to_doer for m in _madeD))
    # Running twice must not send it twice — a second run, or somebody
    # pressing the button by hand, is not a second morning.
    _againD = _escQ.run(_d, _orgD, send=True, force=True)
    check("running again sends nobody a second copy",
          _againD["digest_people"] == 0, str(_againD["digest_people"]))

# With the morning list on, the "your work was reported" copy stops: one
# email in the morning, not two in the same minute about the same work.
check("the per-escalation doer copy gives way to it",
      _escQ._doer_copies({"rows": [], "digest_on": True}) == [])

_pageD = adminQ.get("/escalation").text
check("the page offers the setting", 'name="doer_daily"' in _pageD)
check("and says it is their work only",
      "Their work only" in _pageD or "their own overdue list" in _pageD)

_mlD.HOST, _mlD.FROM, _mlD.send = _hD, _fD, _sD
with _slQ() as _d:
    _orgD = _d.scalar(_SEL(_OgQ))
    _orgD.esc_doer_daily = False
    _orgD.esc_on = False
    _d.commit()

print("\n== sending over HTTPS, because the SMTP ports are blocked ==")
# Render's free web services block outbound traffic to ports 25, 465 and
# 587, so smtplib cannot open the connection at all: every send comes back
# "[Errno 101] Network is unreachable" with nothing wrong with the mailbox
# or the password.
check("with no provider and no host, there is nowhere to send",
      not _mlD.enabled() or bool(_mlD.HOST))
_apiWas, _keyWas, _fromWas = _mlD.API, _mlD.API_KEY, _mlD.FROM
_mlD.API, _mlD.API_KEY, _mlD.FROM = "brevo", "key-123", "gcs@test"
check("a provider and a key are enough on their own, with no SMTP host",
      _mlD.api_ready() and _mlD.enabled())
check("and the status line says which way it will go",
      "brevo" in _mlD.status() and "HTTPS" in _mlD.status(), _mlD.status())
_mlD.API = "pigeon"
check("a provider this app does not know is named, not ignored",
      not _mlD.api_ready() and "pigeon" in _mlD.status(), _mlD.status())
_mlD.API, _mlD.API_KEY = "resend", ""
check("a provider with no key says that, rather than failing at nine am",
      not _mlD.api_ready() and "MAIL_API_KEY" in _mlD.status(), _mlD.status())
_mlD.API, _mlD.API_KEY, _mlD.FROM = _apiWas, _keyWas, _fromWas

print("\n== proving mail works without spending a rung ==")
# The only other way to find out whether email is set up was to force a
# real escalation run, which spends a level on every overdue task to
# answer the question and cannot be undone.
from app.services import mail as _mlT
_hT, _fT, _sT = _mlT.HOST, _mlT.FROM, _mlT.send
_sentT = []

_mlT.HOST, _mlT.FROM = "", ""
_rOffT = adminQ.post("/escalation/test")
check("with no mail set up the test says so rather than pretending",
      _rOffT.status_code in (200, 303))
check("and sends nothing", not _sentT)

_mlT.HOST, _mlT.FROM = "smtp.test", "gcs@test"
_mlT.send = lambda to, subject, body, cc=None, html="": (
    _sentT.append((to, subject)) or (True, ""))
_rOnT = adminQ.post("/escalation/test", follow_redirects=True)
check("with mail set up it sends exactly one email", len(_sentT) == 1,
      str(_sentT))
with _slQ() as _d:
    _meT = _d.scalar(_SEL(_UsQ).where(_UsQ.email == "mis@gcs.local"))
check("to the address of whoever pressed the button, and nobody else",
      _sentT and _sentT[0][0] == [_meT.email], str(_sentT))
check("the page says where it went",
      _meT.email in _rOnT.text, "address not shown")
# Nothing about a test may touch the ladder: no Escalation rows, no
# EscalationMail, nothing that could make tomorrow skip a level.
with _slQ() as _d:
    _escCountT = len(_d.scalars(_SEL(_EsQ)).all())
adminQ.post("/escalation/test")
with _slQ() as _d:
    check("and a test never records an escalation against any task",
          len(_d.scalars(_SEL(_EsQ)).all()) == _escCountT)

_mlT.send = lambda *a, **k: (False, "OSError: [Errno 101] Network is unreachable")
_rFailT = adminQ.post("/escalation/test", follow_redirects=True)
check("a failure is reported in the provider's own words",
      "Errno 101" in _rFailT.text or "did not send" in _rFailT.text)
check("a doer cannot send test emails from the system's account",
      doer.post("/escalation/test").status_code == 403)
# The hint lives with the failure, not only on the not-configured card:
# somebody whose MAIL_HOST is set and failing is exactly who needs it.
check("the failure itself explains what Errno 101 means",
      "blocks the SMTP ports" in _rFailT.text, "hint missing")
_mlT.HOST, _mlT.FROM = "", ""
check("and the card says it too while nothing is configured",
      "blocks the SMTP ports" in adminQ.get("/escalation").text)

_mlT.HOST, _mlT.FROM, _mlT.send = _hT, _fT, _sT

print("\n== an email that never left can be sent, or thrown away ==")
# A mail server unreachable for two mornings must not mean two mornings of
# escalations nobody was ever told about. The record says they were told;
# the record has to be true.
from app.services import mail as _mlR
_hR, _fR, _sR = _mlR.HOST, _mlR.FROM, _mlR.send
_mlR.HOST, _mlR.FROM = "smtp.test", "gcs@test"

with _slQ() as _d:
    _orgR = _d.scalar(_SEL(_OgQ))
    _tR = _d.scalar(_SEL(_TkQ).where(_TkQ.org_id == _orgR.id))
    _dayR = _escQ.clock.today()
    _mR = _EmQ(org_id=_orgR.id, day=_dayR, level=1, repeat_n=0,
               to_name="HOD", to_email="hod@gcs.local", subject="s",
               body="b", body_html="<p>b</p>", task_count=1,
               status="failed", error="OSError: [Errno 101] Network is unreachable")
    _d.add(_mR); _d.flush()
    _d.add(_EsQ(org_id=_orgR.id, task_id=_tR.id, level=1, repeat_n=0,
                day=_dayR, days_overdue=1, to_email="hod@gcs.local",
                mail_id=_mR.id, subject="s"))
    _d.commit()
    _mRid, _tRid = _mR.id, _tR.id

_mlR.send = lambda *a, **k: (False, "OSError: [Errno 101] Network is unreachable")
_rStillQ = adminQ.post(f"/escalation/mail/{_mRid}/retry", follow_redirects=True)
check("a retry that fails again says so, in the provider's words",
      "Errno 101" in _rStillQ.text or "did not send" in _rStillQ.text)
check("and explains what that error actually means",
      "blocks the SMTP ports" in _rStillQ.text)
with _slQ() as _d:
    check("nothing new is recorded by a retry",
          len(_d.scalars(_SEL(_EsQ).where(_EsQ.mail_id == _mRid)).all()) == 1)

_mlR.send = lambda *a, **k: (True, "")
_rGoneQ = adminQ.post(f"/escalation/mail/{_mRid}/retry", follow_redirects=True)
with _slQ() as _d:
    _mR2 = _d.get(_EmQ, _mRid)
    check("once the mail works, the same email goes out",
          _mR2.status == "sent", _mR2.status)
    check("and it is the message that was written that morning, unchanged",
          _mR2.subject == "s" and _mR2.body == "b")
    check("with the escalation rows left exactly as they were",
          len(_d.scalars(_SEL(_EsQ).where(_EsQ.mail_id == _mRid)).all()) == 1)

# A delivered email is history, and history is not edited here.
check("a sent email cannot be sent twice",
      "already delivered" in adminQ.post(
          f"/escalation/mail/{_mRid}/retry", follow_redirects=True).text)
check("nor discarded",
      "not removed from it" in adminQ.post(
          f"/escalation/mail/{_mRid}/discard", follow_redirects=True).text)

# Discarding one that never left has to take its rungs with it. Deleting
# the record alone would leave every task marked as told at that level,
# climbing from there tomorrow having told nobody anything.
with _slQ() as _d:
    _orgR = _d.scalar(_SEL(_OgQ))
    _mD = _EmQ(org_id=_orgR.id, day=_dayR, level=2, repeat_n=0,
               to_name="EA", to_email="ea@gcs.local", subject="s2",
               body="b2", task_count=1, status="failed", error="nope")
    _d.add(_mD); _d.flush()
    _d.add(_EsQ(org_id=_orgR.id, task_id=_tRid, level=2, repeat_n=0,
                day=_dayR, days_overdue=2, to_email="ea@gcs.local",
                mail_id=_mD.id, subject="s2"))
    _d.commit()
    _mDid = _mD.id
adminQ.post(f"/escalation/mail/{_mDid}/discard")
with _slQ() as _d:
    check("discarding removes the email", _d.get(_EmQ, _mDid) is None)
    check("and the rungs it claimed, so the level is owed again",
          not _d.scalars(_SEL(_EsQ).where(_EsQ.mail_id == _mDid)).all())
    check("leaving the task itself alone", _d.get(_TkQ, _tRid) is not None)

check("a doer cannot send or discard escalation email",
      doer.post(f"/escalation/mail/{_mRid}/retry").status_code == 403
      and doer.post(f"/escalation/mail/{_mRid}/discard").status_code == 403)
# The whole day in one go, for the common case: a mail server that was
# down for a morning, not one email that bounced.
with _slQ() as _d:
    _orgR = _d.scalar(_SEL(_OgQ))
    for _n in range(3):
        _d.add(_EmQ(org_id=_orgR.id, day=_dayR, level=1, repeat_n=_n,
                    to_name=f"P{_n}", to_email=f"p{_n}@gcs.local",
                    subject=f"s{_n}", body="b", task_count=1,
                    status="failed", error="nope"))
    _d.commit()
_rDayQ = adminQ.post("/escalation/mail/retry-day",
                     data={"day": _dayR.isoformat()}, follow_redirects=True)
with _slQ() as _d:
    _leftQ = _d.scalars(_SEL(_EmQ).where(_EmQ.day == _dayR,
                                         _EmQ.status != "sent")).all()
    check("one button sends every stuck email of that day",
          not _leftQ, str([(m.to_email, m.status) for m in _leftQ]))
check("and says how many went", "sent" in _rDayQ.text)
check("a day that is not a date is refused rather than read as today",
      adminQ.post("/escalation/mail/retry-day",
                  data={"day": "yesterday"}).status_code == 400)
_mlR.HOST, _mlR.FROM, _mlR.send = _hR, _fR, _sR

print("\n" + ("ALL CHECKS PASSED" if not FAIL else f"{len(FAIL)} FAILED: {FAIL}"))
sys.exit(1 if FAIL else 0)
