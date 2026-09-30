"""
myapp/decorators.py

Backend role enforcement. Per the security spec:
"Simply hiding a button or menu is not enough. The server/backend must also
verify the user's role before allowing access to protected functions."

Usage (stack UNDER @login_required so we know request.user is authenticated):

    from django.contrib.auth.decorators import login_required
    from myapp.decorators import role_required
    from myapp.models import Profile

    @login_required
    @role_required(Profile.ROLE_MENTOR)
    def create_lesson(request):
        ...

    @login_required
    @role_required(Profile.ROLE_ADMIN, Profile.ROLE_MENTOR)  # multiple roles allowed
    def some_shared_view(request):
        ...
"""

from functools import wraps
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404
from .models import Profile


def role_required(*allowed_roles):
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            profile = get_object_or_404(Profile, user=request.user)
            if profile.role not in allowed_roles:
                # 403 Forbidden — do NOT silently redirect, the spec requires
                # the backend to actively reject unauthorized access attempts.
                raise PermissionDenied(
                    "You do not have permission to access this page."
                )
            return view_func(request, *args, **kwargs)
        return _wrapped
    return decorator