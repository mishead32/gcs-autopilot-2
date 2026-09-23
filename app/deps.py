from fastapi import Request, HTTPException, Depends, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .db import get_db
from .models import User, Role, Right


class RedirectToLogin(Exception):
    pass


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    uid = request.session.get("uid")
    if not uid:
        raise RedirectToLogin()
    user = db.get(User, uid)
    if not user or not user.active:
        request.session.clear()
        raise RedirectToLogin()
    return user


def require_roles(*roles: Role):
    def _check(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Not allowed for your role")
        return user
    return _check


def require_right(right: Right):
    """Gate a route on a specific right rather than a whole role."""
    def _check(user: User = Depends(current_user)) -> User:
        if not user.has(right):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"You don't have the '{right.value.replace('_', ' ')}' right. "
                "Ask an admin to grant it."
            )
        return user
    return _check


# convenience aliases
manager_up = require_roles(Role.OWNER, Role.ADMIN, Role.MANAGER)
admin_up = require_roles(Role.OWNER, Role.ADMIN)


def can_view_task(user: User, task) -> bool:
    if user.role in (Role.OWNER, Role.ADMIN) or user.has(Right.VIEW_ALL_BRANCHES):
        return True
    if task.doer_id == user.id or task.assigner_id == user.id:
        return True
    if user.role == Role.MANAGER and task.branch_id == user.branch_id:
        return True
    return False
