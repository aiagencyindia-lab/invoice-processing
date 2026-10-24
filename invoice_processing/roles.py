"""The role ladder, in one place for the API, approvals and provisioning.

A controller is a finance role: it ranks with approver for everything on the
ladder, and alone holds what `api.auth.require_exact_role("controller")`
guards (approving agent registrations). Admins outrank it and still cannot.
"""

ROLE_RANK = {
    "viewer": 0,
    "clerk": 1,
    "approver": 2,
    "controller": 2,
    "admin": 3,
}
