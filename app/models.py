"""MIDAP-style data model.

Every row that belongs to a customer carries `org_id` so the same codebase can
run GCS internally today and serve multiple tenants later without a rewrite.
"""
from __future__ import annotations

import enum
from datetime import datetime, date

from sqlalchemy import (
    String, Integer, DateTime, Date, Boolean, ForeignKey, Text, Float, Enum,
    LargeBinary, UniqueConstraint, event
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------
class Role(str, enum.Enum):
    OWNER = "owner"        # CMD level - sees everything across branches
    ADMIN = "admin"        # can configure flows, users, branches
    MANAGER = "manager"    # can delegate and audit within their department
    DOER = "doer"          # executes assigned tasks


class Priority(str, enum.Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


class TaskStatus(str, enum.Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUBMITTED = "submitted"     # doer marked done, waiting on audit
    COMPLETED = "completed"
    REJECTED = "rejected"       # auditor sent it back
    REOPENED = "reopened"       # auditor pulled a closed task back open
    CANCELLED = "cancelled"


class AuditState(str, enum.Enum):
    """Where a task stands with the auditor, shown on every task.

    Kept separate from TaskStatus on purpose: a task can be COMPLETED and still
    be waiting for someone to audit it, and the doer's dashboard needs to show
    both facts at once.
    """
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    COMPLETED = "completed"


AUDIT_LABELS = {
    AuditState.NOT_REQUIRED: "Not required",
    AuditState.PENDING: "Audit pending",
    AuditState.COMPLETED: "Audit completed",
}


class HelpStatus(str, enum.Enum):
    """A help ticket one employee raises with another."""
    OPEN = "open"           # delegation task created, helper hasn't finished
    DECLINED = "declined"   # helper can't take it; the task is cancelled
    CLOSED = "closed"       # the delegation task was completed


class Right(str, enum.Enum):
    """Fine-grained rights, granted per user when the account is created.

    These sit *on top of* the role. A doer with EDIT_TASK can edit tasks they
    can already see; it never widens which rows they can see.
    """
    CREATE_TASK = "create_task"     # delegate work to others
    EDIT_TASK = "edit_task"         # change title/details/due/priority/doer
    DELETE_TASK = "delete_task"     # remove a task entirely
    AUDIT_TASK = "audit_task"       # approve / send back a submission
    REOPEN_TASK = "reopen_task"     # pull a closed task back open
    FALSE_MARK = "false_mark"       # flag a bogus completion (-10 to the doer)
    MANAGE_FLOW = "manage_flow"     # create / edit FMS templates
    MANAGE_USER = "manage_user"     # create users, set rights
    VIEW_ALL_BRANCHES = "view_all_branches"


RIGHT_LABELS = {
    Right.CREATE_TASK: "Delegate tasks",
    Right.EDIT_TASK: "Edit tasks",
    Right.DELETE_TASK: "Delete tasks",
    Right.AUDIT_TASK: "Audit submissions",
    Right.REOPEN_TASK: "Reopen closed tasks",
    Right.FALSE_MARK: "Flag false marking (−10)",
    Right.MANAGE_FLOW: "Manage FMS flows",
    Right.MANAGE_USER: "Manage users & rights",
    Right.VIEW_ALL_BRANCHES: "See all branches",
}

# What each role gets by default when a user is created.
DEFAULT_RIGHTS = {
    Role.OWNER: [r for r in Right],
    Role.ADMIN: [r for r in Right],
    Role.MANAGER: [Right.CREATE_TASK, Right.EDIT_TASK, Right.AUDIT_TASK,
                   Right.REOPEN_TASK, Right.FALSE_MARK],
    Role.DOER: [],
}


class TaskSource(str, enum.Enum):
    DELEGATION = "delegation"   # one-off task assigned by a person
    RECURRING = "recurring"     # spawned by a recurrence rule
    FLOW = "flow"               # a step inside a running flow instance


class Recurrence(str, enum.Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    WEEKDAYS = "weekdays"


# --------------------------------------------------------------------------
# Tenant / org structure
# --------------------------------------------------------------------------
class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(60), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    branches: Mapped[list["Branch"]] = relationship(back_populates="org")
    users: Mapped[list["User"]] = relationship(back_populates="org")


class Branch(Base):
    """A vertical / location. For GCS: Bodyzone, Spa Kora, BIPS."""
    __tablename__ = "branches"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    name: Mapped[str] = mapped_column(String(120))
    city: Mapped[str | None] = mapped_column(String(80), nullable=True)

    org: Mapped[Organization] = relationship(back_populates="branches")


class Department(Base):
    __tablename__ = "departments"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(120))


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("org_id", "email", name="uq_user_org_email"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id"), nullable=True)
    department_id: Mapped[int | None] = mapped_column(ForeignKey("departments.id"), nullable=True)

    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(160), index=True)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)  # WhatsApp target
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(Enum(Role), default=Role.DOER)
    # comma-separated Right values, e.g. "edit_task,delete_task"
    rights: Mapped[str] = mapped_column(Text, default="")

    # Scoring benchmark: how this person's work is expected to split across the
    # three sources. Must total 100. The weight caps how much each source can
    # cost them, so a delegation-heavy doer isn't punished equally for a
    # checklist slip. Half the weight goes to "not done", half to "not on time".
    bm_delegation: Mapped[int] = mapped_column(Integer, default=60)
    bm_checklist: Mapped[int] = mapped_column(Integer, default=20)
    bm_fms: Mapped[int] = mapped_column(Integer, default=20)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    org: Mapped[Organization] = relationship(back_populates="users")
    branch: Mapped[Branch | None] = relationship()
    department: Mapped[Department | None] = relationship()

    @property
    def right_set(self) -> set[str]:
        return {r for r in (self.rights or "").split(",") if r}

    def has(self, right: "Right") -> bool:
        """Owner and admin always hold every right; others need it granted."""
        if self.role in (Role.OWNER, Role.ADMIN):
            return True
        return right.value in self.right_set

    def set_rights(self, rights) -> None:
        self.rights = ",".join(sorted({
            r.value if isinstance(r, Right) else str(r) for r in rights
        }))

    @property
    def benchmarks(self) -> dict[str, int]:
        """TaskSource value -> benchmark percentage for this doer."""
        return {
            TaskSource.DELEGATION.value: self.bm_delegation,
            TaskSource.RECURRING.value: self.bm_checklist,
            TaskSource.FLOW.value: self.bm_fms,
        }

    @property
    def benchmark_total(self) -> int:
        return self.bm_delegation + self.bm_checklist + self.bm_fms

    def set_benchmarks(self, delegation: int, checklist: int, fms: int) -> None:
        total = delegation + checklist + fms
        if total != 100:
            raise ValueError(
                f"Benchmarks must add up to 100% — you entered {total}% "
                f"(Delegation {delegation} + Checklist {checklist} + FMS {fms})."
            )
        if min(delegation, checklist, fms) < 0:
            raise ValueError("A benchmark cannot be negative.")
        self.bm_delegation, self.bm_checklist, self.bm_fms = delegation, checklist, fms

    # --- shorthands the templates use, so views stay free of role literals ---
    @property
    def can_manage(self) -> bool:
        """Sees the Operations / Insight sections of the menu."""
        return self.role in (Role.OWNER, Role.ADMIN, Role.MANAGER)

    @property
    def has_create_task(self) -> bool:
        return self.has(Right.CREATE_TASK)

    @property
    def has_manage_flow(self) -> bool:
        return self.has(Right.MANAGE_FLOW)

    @property
    def has_manage_user(self) -> bool:
        return self.has(Right.MANAGE_USER)

    @property
    def has_edit_task(self) -> bool:
        return self.has(Right.EDIT_TASK)


# --------------------------------------------------------------------------
# Flow Management System
# --------------------------------------------------------------------------
class Flow(Base):
    """A reusable workflow template, e.g. 'New PT Member Onboarding'."""
    __tablename__ = "flows"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    steps: Mapped[list["FlowStep"]] = relationship(
        back_populates="flow", order_by="FlowStep.position", cascade="all, delete-orphan"
    )
    branch: Mapped[Branch | None] = relationship()


class FlowStep(Base):
    __tablename__ = "flow_steps"
    id: Mapped[int] = mapped_column(primary_key=True)
    flow_id: Mapped[int] = mapped_column(ForeignKey("flows.id"))
    position: Mapped[int] = mapped_column(Integer, default=1)
    title: Mapped[str] = mapped_column(String(200))
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    # who does it: a fixed user, or left blank to be picked at run time
    default_doer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    tat_hours: Mapped[int] = mapped_column(Integer, default=24)   # turnaround time
    priority: Mapped[Priority] = mapped_column(Enum(Priority), default=Priority.NORMAL)
    requires_audit: Mapped[bool] = mapped_column(Boolean, default=False)
    requires_attachment: Mapped[bool] = mapped_column(Boolean, default=True)
    # comma separated field labels the doer must fill in on completion
    capture_fields: Mapped[str | None] = mapped_column(Text, nullable=True)

    flow: Mapped[Flow] = relationship(back_populates="steps")
    default_doer: Mapped[User | None] = relationship()


class FlowInstance(Base):
    """One live run of a flow, e.g. onboarding for member #1042."""
    __tablename__ = "flow_instances"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    flow_id: Mapped[int] = mapped_column(ForeignKey("flows.id"))
    reference: Mapped[str] = mapped_column(String(200))   # member name / lead id / invoice no
    started_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    current_position: Mapped[int] = mapped_column(Integer, default=1)
    # JSON blob of field values carried across steps (MIDAP "Split FMS" carryover)
    context: Mapped[str] = mapped_column(Text, default="{}")

    flow: Mapped[Flow] = relationship()
    started_by: Mapped[User] = relationship()


# --------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------
class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id"), nullable=True)

    title: Mapped[str] = mapped_column(String(250))
    details: Mapped[str | None] = mapped_column(Text, nullable=True)

    assigner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    doer_id: Mapped[int] = mapped_column(ForeignKey("users.id"))

    priority: Mapped[Priority] = mapped_column(Enum(Priority), default=Priority.NORMAL)
    status: Mapped[TaskStatus] = mapped_column(Enum(TaskStatus), default=TaskStatus.PENDING, index=True)
    source: Mapped[TaskSource] = mapped_column(Enum(TaskSource), default=TaskSource.DELEGATION)

    due_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # links back to origin
    flow_instance_id: Mapped[int | None] = mapped_column(ForeignKey("flow_instances.id"), nullable=True)
    flow_step_id: Mapped[int | None] = mapped_column(ForeignKey("flow_steps.id"), nullable=True)
    rule_id: Mapped[int | None] = mapped_column(ForeignKey("recurring_rules.id"), nullable=True)

    # audit
    requires_audit: Mapped[bool] = mapped_column(Boolean, default=False)
    # Proof of the work, not just a claim that it was done. On by default
    # everywhere: a task closed with no evidence is the thing false-marking
    # exists to catch, and catching it after the fact is worse than
    # preventing it.
    requires_attachment: Mapped[bool] = mapped_column(Boolean, default=True)
    audit_state: Mapped[AuditState] = mapped_column(
        Enum(AuditState), default=AuditState.NOT_REQUIRED, index=True)
    audit_score: Mapped[float | None] = mapped_column(Float, nullable=True)   # 0-10 quality
    audit_remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    auditor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    audited_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    completion_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    captured_data: Mapped[str] = mapped_column(Text, default="{}")

    # false marking: auditor says the doer closed this without really doing it
    false_marked: Mapped[bool] = mapped_column(Boolean, default=False)
    false_marked_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    false_marked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    false_mark_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # reopen tracking
    reopen_count: Mapped[int] = mapped_column(Integer, default=0)
    reopened_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    reopened_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    assigner: Mapped[User] = relationship(foreign_keys=[assigner_id])
    doer: Mapped[User] = relationship(foreign_keys=[doer_id])
    auditor: Mapped[User | None] = relationship(foreign_keys=[auditor_id])
    false_marked_by: Mapped[User | None] = relationship(foreign_keys=[false_marked_by_id])
    reopened_by: Mapped[User | None] = relationship(foreign_keys=[reopened_by_id])
    branch: Mapped[Branch | None] = relationship()
    flow_instance: Mapped[FlowInstance | None] = relationship()
    flow_step: Mapped[FlowStep | None] = relationship()
    comments: Mapped[list["TaskComment"]] = relationship(
        back_populates="task", cascade="all, delete-orphan", order_by="TaskComment.created_at"
    )
    attachments: Mapped[list["Attachment"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )

    @property
    def is_overdue(self) -> bool:
        if self.status in (TaskStatus.COMPLETED, TaskStatus.CANCELLED):
            return False
        return datetime.utcnow() > self.due_at

    @property
    def was_on_time(self) -> bool | None:
        if not self.submitted_at:
            return None
        return self.submitted_at <= self.due_at

    @property
    def audit_label(self) -> str:
        return AUDIT_LABELS[self.audit_state]

    @property
    def audit_open(self) -> bool:
        return self.audit_state == AuditState.PENDING


@event.listens_for(Task, "before_insert")
def _on_task_created(mapper, connection, target: "Task") -> None:
    """Rules that must hold for every new task, whatever created it.

    Done here rather than at each creation site so delegation, checklist, FMS
    and the bulk importer all behave the same without four copies of the rule.
    """
    # Flagged for audit -> it starts as 'Audit pending'.
    if target.requires_audit and target.audit_state in (None, AuditState.NOT_REQUIRED):
        target.audit_state = AuditState.PENDING

    # A task is live the moment it is assigned. There is no "accept" step:
    # the deadline was set when the work was handed over, so the clock is
    # already running and asking the doer to press a button first only adds a
    # way to look busy without doing anything.
    if target.status in (None, TaskStatus.PENDING):
        target.status = TaskStatus.IN_PROGRESS
        target.started_at = target.started_at or datetime.utcnow()


class TaskComment(Base):
    """MIDAP 'Notes' - a chat thread hanging off each task."""
    __tablename__ = "task_comments"
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"))
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    task: Mapped[Task] = relationship(back_populates="comments")
    author: Mapped[User] = relationship()


class Attachment(Base):
    __tablename__ = "attachments"
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"))
    uploaded_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    filename: Mapped[str] = mapped_column(String(255))
    # object key in the bucket, or the file name on disk
    stored_name: Mapped[str] = mapped_column(String(512))
    size: Mapped[int] = mapped_column(Integer, default=0)
    content_type: Mapped[str] = mapped_column(String(120), default="")
    storage: Mapped[str] = mapped_column(String(10), default="local")  # s3 | local | db
    # Used only when storage == "db": the file lives in the database itself.
    # That is how a free host with no permanent disk keeps attachments across
    # restarts without a separate storage account.
    data: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    task: Mapped[Task] = relationship(back_populates="attachments")
    uploaded_by: Mapped[User] = relationship()

    @property
    def is_image(self) -> bool:
        return self.content_type.startswith("image/")

    @property
    def size_label(self) -> str:
        kb = self.size / 1024
        return f"{kb:.0f} KB" if kb < 1024 else f"{kb / 1024:.1f} MB"


class RecurringRule(Base):
    __tablename__ = "recurring_rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(250))
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    doer_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    assigner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    priority: Mapped[Priority] = mapped_column(Enum(Priority), default=Priority.NORMAL)
    frequency: Mapped[Recurrence] = mapped_column(Enum(Recurrence), default=Recurrence.DAILY)
    # weekly -> 0=Mon..6=Sun ; monthly -> day of month
    day_of: Mapped[int | None] = mapped_column(Integer, nullable=True)
    due_time: Mapped[str] = mapped_column(String(5), default="18:00")   # HH:MM local
    requires_audit: Mapped[bool] = mapped_column(Boolean, default=False)
    requires_attachment: Mapped[bool] = mapped_column(Boolean, default=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_spawned_on: Mapped[date | None] = mapped_column(Date, nullable=True)

    doer: Mapped[User] = relationship(foreign_keys=[doer_id])
    assigner: Mapped[User] = relationship(foreign_keys=[assigner_id])


class HelpTicket(Base):
    """One employee asking another for help.

    Raising a ticket creates the Delegation task immediately — the whole point
    is that asking for help *is* delegating, without a manager in the middle.
    The helper can decline, which cancels that task and tells the raiser why.
    """
    __tablename__ = "help_tickets"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    raiser_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    helper_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"), nullable=True)

    subject: Mapped[str] = mapped_column(String(250))
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[Priority] = mapped_column(Enum(Priority), default=Priority.NORMAL)
    needed_by: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[HelpStatus] = mapped_column(Enum(HelpStatus), default=HelpStatus.OPEN,
                                               index=True)
    decline_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    raiser: Mapped[User] = relationship(foreign_keys=[raiser_id])
    helper: Mapped[User] = relationship(foreign_keys=[helper_id])
    task: Mapped[Task | None] = relationship()


class Holiday(Base):
    """A day on which no work is expected.

    A holiday with no branch applies to the whole group; one with a branch
    applies only there, so Jharkhand can keep a local festival that Chandigarh
    does not. Stored as a plain date — a holiday is a whole day, never a time
    range.
    """
    __tablename__ = "holidays"
    __table_args__ = (UniqueConstraint("org_id", "branch_id", "day",
                                       name="uq_holiday_org_branch_day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id"), nullable=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    name: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    branch: Mapped[Branch | None] = relationship()

    @property
    def scope(self) -> str:
        return self.branch.name if self.branch else "All branches"


class OutboundMessage(Base):
    """Queue for WhatsApp/SMS notifications.

    Nothing is sent yet - rows are written by the app and a provider adapter
    (Gupshup / WATI / Meta Cloud API) drains the queue. Keeps the notification
    channel swappable.
    """
    __tablename__ = "outbound_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    to_phone: Mapped[str] = mapped_column(String(20))
    template: Mapped[str] = mapped_column(String(60))
    body: Mapped[str] = mapped_column(Text)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
