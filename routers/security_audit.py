"""routers/security_audit.py - READ-ONLY view of the permanent security audit trail (Phase 2, Stage 1, Task C).

GET /admin/security-audit
Permission: the "User Management" module's view permission (the module that already guards the activity-log screen). A country-restricted
dashboard user only sees entries whose target is in their own country. There is deliberately NO create / update / delete route: entries
are written by the server's own sensitive operations only, and nobody can edit or remove one through the API.
"""
from fastapi import APIRouter, Depends

from routers.admin import _check_perm, get_current_admin
from services import dashboard_country as _dc
from services import security_audit as audit

router = APIRouter(tags=["Security audit"])


@router.get("/security-audit")
def security_audit_log(action: str = "", outcome: str = "", target_id: str = "", before: str = "", limit: int = 50,
                       a=Depends(get_current_admin)):
    _check_perm(a, "User Management", "view")
    return {"entries": audit.list_entries(limit, action, outcome, target_id, _dc.scope(a), before or None)}
