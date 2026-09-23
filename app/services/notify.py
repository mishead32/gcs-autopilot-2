"""WhatsApp / SMS notification queue.

Messages are written to the DB rather than sent directly, so a provider
adapter can be plugged in later without touching business logic.
"""
from sqlalchemy.orm import Session

from ..models import OutboundMessage, Task, User

TEMPLATES = {
    "task_assigned": (
        "Hi {name}, a new task has been assigned to you.\n"
        "*{title}*\nPriority: {priority}\nDue: {due}\n"
        "Open your dashboard to accept it."
    ),
    "task_due_soon": (
        "Reminder {name}: *{title}* is due at {due}. Please update the status."
    ),
    "task_overdue": (
        "{name}, task *{title}* was due {due} and is still open. Kindly close it today."
    ),
    "task_rejected": (
        "{name}, your submission for *{title}* was sent back by the auditor.\nRemark: {remark}"
    ),
    "flow_step_ready": (
        "Hi {name}, step *{title}* of flow '{flow}' ({reference}) is now yours. Due {due}."
    ),
}


def queue(db: Session, user: User, template: str, task: Task | None = None, **kw) -> None:
    if not user.phone:
        return
    body = TEMPLATES[template].format(name=user.name.split()[0], **kw)
    db.add(OutboundMessage(
        org_id=user.org_id,
        to_phone=user.phone,
        template=template,
        body=body,
        task_id=task.id if task else None,
    ))


def queue_task_assigned(db: Session, task: Task) -> None:
    queue(
        db, task.doer, "task_assigned", task,
        title=task.title,
        priority=task.priority.value.upper(),
        due=task.due_at.strftime("%d %b %Y, %I:%M %p"),
    )
