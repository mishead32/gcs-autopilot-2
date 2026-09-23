"""Seed the database with GCS demo data.

    python seed.py           # create if empty
    python seed.py --reset   # wipe and recreate
"""
import sys
import random
from datetime import datetime, timedelta

from app.db import Base, engine, SessionLocal
from app.models import (
    Organization, Branch, Department, User, Role, Flow, FlowStep, Priority,
    RecurringRule, Recurrence, Task, TaskStatus, TaskSource, Right, DEFAULT_RIGHTS
)
from app.security import hash_password
from app.services import flows as flow_svc, recurring, notify

PW = "gcs1234"


def reset():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def seed():
    Base.metadata.create_all(engine)
    db = SessionLocal()
    if db.query(Organization).count():
        print("Already seeded. Use --reset to start over.")
        return

    org = Organization(name="GCS Group", slug="gcs")
    db.add(org); db.flush()

    bz = Branch(org_id=org.id, name="Bodyzone Fitness & Spa", city="Chandigarh")
    sk = Branch(org_id=org.id, name="Spa Kora", city="Chandigarh")
    bp = Branch(org_id=org.id, name="BIPS School", city="Chandigarh")
    jh = Branch(org_id=org.id, name="GCS Jharkhand", city="Ranchi")
    ho = Branch(org_id=org.id, name="GCS HO", city="Chandigarh")
    db.add_all([bz, sk, bp, jh, ho]); db.flush()

    for b, names in [(bz, ["Sales", "Operations", "Training"]),
                     (sk, ["Front Desk", "Therapy"]),
                     (bp, ["Admissions", "Academics"]),
                     (jh, ["Operations", "Sales"]),
                     (ho, ["MIS", "Accounts", "HR"])]:
        for n in names:
            db.add(Department(org_id=org.id, branch_id=b.id, name=n))
    db.flush()

    def mk(name, email, role, branch, phone, extra_rights=(), bm=(60, 20, 20)):
        u = User(org_id=org.id, name=name, email=email, phone=phone,
                 password_hash=hash_password(PW), role=role,
                 branch_id=branch.id if branch else None)
        u.set_rights(list(DEFAULT_RIGHTS[role]) + list(extra_rights))
        u.set_benchmarks(*bm)
        db.add(u); db.flush()
        return u

    cmd     = mk("CMD Sir",        "cmd@gcs.local",     Role.OWNER,   None, "+919800000001")
    rajinder= mk("Rajinder Singh", "mis@gcs.local",     Role.ADMIN,   None, "+919800000002")
    bz_mgr  = mk("Bodyzone Manager","bz.manager@gcs.local", Role.MANAGER, bz, "+919800000003")
    sk_mgr  = mk("Spa Kora Manager","sk.manager@gcs.local", Role.MANAGER, sk, "+919800000004")
    bp_mgr  = mk("BIPS Coordinator","bips.coord@gcs.local", Role.MANAGER, bp, "+919800000005")
    # benchmarks differ by job: the front desk lives on checklists and flows,
    # a trainer mostly on delegated work
    trainer = mk("PT Trainer Amit","amit@gcs.local",    Role.DOER,    bz, "+919800000006",
                 bm=(70, 10, 20))
    frontbz = mk("Bodyzone Front Desk","front.bz@gcs.local", Role.DOER, bz, "+919800000007",
                 bm=(20, 40, 40))
    therapy = mk("Spa Therapist Neha","neha@gcs.local", Role.DOER,    sk, "+919800000008",
                 bm=(30, 40, 30))
    counsel = mk("BIPS Counsellor Priya","priya@gcs.local", Role.DOER, bp, "+919800000009",
                 bm=(40, 20, 40))
    jh_mgr  = mk("Jharkhand Manager","jh.manager@gcs.local", Role.MANAGER, jh, "+919800000010")
    jh_ops  = mk("Jharkhand Ops Ravi","ravi@gcs.local", Role.DOER, jh, "+919800000011")
    ho_acct = mk("HO Accounts Sunil","sunil@gcs.local", Role.DOER, ho, "+919800000012")
    # a doer trusted to audit and flag false marking, but not to delegate
    auditor = mk("Group Auditor Meena","meena@gcs.local", Role.DOER, ho, "+919800000013",
                 extra_rights=[Right.AUDIT_TASK, Right.REOPEN_TASK,
                               Right.FALSE_MARK, Right.VIEW_ALL_BRANCHES])

    # the two people who chase what has not been done
    pc = mk("PC Simran", "pc@gcs.local", Role.DOER, ho, "+919800000014",
            extra_rights=[Right.FOLLOWUP_CHECKLIST_FMS, Right.VIEW_ALL_BRANCHES])
    ea = mk("EA Karan", "ea@gcs.local", Role.DOER, ho, "+919800000015",
            extra_rights=[Right.FOLLOWUP_DELEGATION, Right.VIEW_ALL_BRANCHES])

    # ---------------- Flows -------------------------------------------------
    pt_flow = Flow(org_id=org.id, branch_id=bz.id,
                   name="New PT Member Onboarding",
                   description="From payment to first session — nothing gets dropped.")
    db.add(pt_flow); db.flush()
    for pos, (title, doer, tat, fields, audit, instr) in enumerate([
        ("Collect payment & issue receipt", frontbz, 4, "Receipt No, Amount, Package", False,
         "Take payment, raise receipt in the billing software, note the package sold."),
        ("Enrol face-ID & create member profile", frontbz, 8, "Member ID, Face ID Status", False,
         "Register biometric attendance and create the member record."),
        ("Assign trainer & book first session", bz_mgr, 12, "Trainer Name, First Session Date", False,
         "Match the member to an available trainer based on goal and slot."),
        ("Fitness assessment & goal sheet", trainer, 48, "Weight, Body Fat %, Goal", True,
         "Complete the assessment form and upload the signed goal sheet."),
        ("Day-7 satisfaction call", bz_mgr, 168, "Feedback Score, Remarks", True,
         "Call the member, log satisfaction out of 10 and any complaint."),
    ], start=1):
        db.add(FlowStep(flow_id=pt_flow.id, position=pos, title=title, instructions=instr,
                        default_doer_id=doer.id, tat_hours=tat, capture_fields=fields,
                        requires_audit=audit,
                        priority=Priority.HIGH if pos <= 2 else Priority.MEDIUM))

    adm_flow = Flow(org_id=org.id, branch_id=bp.id, name="BIPS Admission Enquiry",
                    description="Enquiry to admission — with a mandatory follow-up loop.")
    db.add(adm_flow); db.flush()
    for pos, (title, doer, tat, fields, audit) in enumerate([
        ("Log enquiry & qualify lead", counsel, 4, "Student Name, Class, Source", False),
        ("Schedule campus visit", counsel, 24, "Visit Date", False),
        ("Conduct visit & share fee structure", bp_mgr, 48, "Fee Quoted, Interest Level", False),
        ("Follow-up call & close decision", counsel, 72, "Decision, Reason", True),
    ], start=1):
        db.add(FlowStep(flow_id=adm_flow.id, position=pos, title=title,
                        default_doer_id=doer.id, tat_hours=tat,
                        capture_fields=fields, requires_audit=audit))

    spa_flow = Flow(org_id=org.id, branch_id=sk.id, name="Spa Membership Renewal",
                    description="Catch expiring spa memberships before they lapse.")
    db.add(spa_flow); db.flush()
    for pos, (title, doer, tat, fields) in enumerate([
        ("Flag expiring membership", sk_mgr, 8, "Member Name, Expiry Date"),
        ("Renewal call & offer", therapy, 24, "Offer Given, Response"),
        ("Collect renewal payment", sk_mgr, 72, "Receipt No, Amount"),
    ], start=1):
        db.add(FlowStep(flow_id=spa_flow.id, position=pos, title=title,
                        default_doer_id=doer.id, tat_hours=tat, capture_fields=fields))
    db.commit()

    # ---------------- Recurring rules --------------------------------------
    for title, doer, freq, day, time_, prio, audit in [
        ("Post daily sales MIS to CMD", rajinder, Recurrence.WEEKDAYS, None, "19:00", Priority.HIGH, False),
        ("Bodyzone: invalid member & face-ID report", frontbz, Recurrence.DAILY, None, "11:00", Priority.MEDIUM, False),
        ("Spa Kora: room occupancy entry", therapy, Recurrence.DAILY, None, "21:00", Priority.MEDIUM, False),
        ("Weekly PT trainer-wise performance review", bz_mgr, Recurrence.WEEKLY, 0, "12:00", Priority.HIGH, True),
        ("Monthly machine maintenance audit", bz_mgr, Recurrence.MONTHLY, 1, "16:00", Priority.HIGH, True),
        ("BIPS: irrelevant-lead quality audit", counsel, Recurrence.WEEKLY, 4, "17:00", Priority.MEDIUM, True),
        ("Jharkhand: daily collection summary", jh_ops, Recurrence.WEEKDAYS, None, "18:30", Priority.HIGH, False),
        ("HO: vendor payment run", ho_acct, Recurrence.WEEKLY, 2, "15:00", Priority.HIGH, True),
    ]:
        db.add(RecurringRule(org_id=org.id, branch_id=doer.branch_id, title=title,
                             doer_id=doer.id, assigner_id=rajinder.id, frequency=freq,
                             day_of=day, due_time=time_, priority=prio, requires_audit=audit))
    db.commit()

    # ---------------- Sample history so the dashboards aren't empty --------
    random.seed(7)
    samples = [
        ("Reconcile September PT collections", rajinder, bz_mgr, bz),
        ("Update trainer roster for next week", bz_mgr, trainer, bz),
        ("Chase 12 pending NBD follow-ups", bz_mgr, frontbz, bz),
        ("Deep-clean therapy room 3", sk_mgr, therapy, sk),
        ("Verify spa member validity list", sk_mgr, therapy, sk),
        ("Call back 8 admission enquiries from Monday", bp_mgr, counsel, bp),
        ("File August bills in dispatch register", rajinder, frontbz, bz),
        ("Fix treadmill #4 belt alignment", bz_mgr, trainer, bz),
        ("Reconcile Jharkhand cash deposits", jh_mgr, jh_ops, jh),
        ("Chase 5 overdue vendor invoices", rajinder, ho_acct, ho),
        ("Prepare HO headcount sheet", rajinder, ho_acct, ho),
        ("Jharkhand: weekly stock count", jh_mgr, jh_ops, jh),
    ]
    now = datetime.utcnow()
    for i, (title, assigner, doer, branch) in enumerate(samples):
        created = now - timedelta(days=random.randint(1, 20))
        due = created + timedelta(hours=random.choice([8, 24, 48, 72]))
        t = Task(org_id=org.id, branch_id=branch.id, title=title,
                 details="Auto-generated demo task.", assigner_id=assigner.id,
                 doer_id=doer.id, priority=random.choice(list(Priority)),
                 source=TaskSource.DELEGATION, due_at=due, created_at=created)
        if i % 3 != 0:                       # most get completed
            late = random.random() < 0.3
            t.submitted_at = due + timedelta(hours=random.randint(1, 20) if late else -random.randint(1, 6))
            t.status = TaskStatus.COMPLETED
            t.closed_at = t.submitted_at
            t.audit_score = round(random.uniform(6, 10), 1)
            t.auditor_id = assigner.id
            t.completion_note = "Done and verified."
            # one in six closed tasks is caught as a false marking
            if i % 6 == 5:
                t.false_marked = True
                t.false_marked_by_id = auditor.id
                t.false_marked_at = t.submitted_at
                t.false_mark_reason = "Register entry missing — work was not actually done."
        db.add(t)
    db.commit()

    # ---------------- Live flow runs ---------------------------------------
    flow_svc.start_flow(db, pt_flow, "Sandeep Kaur — PT 3M package", bz_mgr)
    flow_svc.start_flow(db, pt_flow, "Vikram Mehta — PT 6M package", bz_mgr)
    flow_svc.start_flow(db, adm_flow, "Aarav Sharma — Class VI enquiry", bp_mgr)
    flow_svc.start_flow(db, spa_flow, "Ritu Bansal — expiring 20 Sep", sk_mgr)

    recurring.run_spawn(db)
    db.close()

    print("Seeded GCS demo data.\n")
    print("  Sign in with any of these — password: " + PW)
    for e, r in [("cmd@gcs.local", "owner (CMD view)"),
                 ("mis@gcs.local", "admin (you)"),
                 ("bz.manager@gcs.local", "manager"),
                 ("meena@gcs.local", "doer + auditor rights"),
                 ("amit@gcs.local", "doer")]:
        print(f"    {e:26} {r}")


if __name__ == "__main__":
    if "--reset" in sys.argv:
        reset()
    seed()
