"""Server-side Terms and Conditions enforcement.

A STUDENT who has not accepted the required version of the Terms is redirected
to the acceptance page on every request, so typing a URL directly cannot get
around it. Mentors and Admins are not affected.
"""
from django.shortcuts import redirect

from .models import Profile, TermsVersion

# Pages a student must still be able to reach before accepting.
EXEMPT_PREFIXES = (
    '/terms/', '/logout/', '/static/', '/media/',
    '/verify-email/', '/login/', '/forgot-password/', '/reset-password/',
)


class TermsConsentMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, 'user', None)
        if user is not None and user.is_authenticated and not request.path.startswith(EXEMPT_PREFIXES):
            profile = Profile.objects.filter(user=user).only('role').first()
            if profile is not None and profile.role == Profile.ROLE_STUDENT:
                if not TermsVersion.student_has_accepted(user):
                    return redirect('terms_accept')
        return self.get_response(request)


# A deactivated student may only see the status page, the Terms, and log out.
INACTIVE_ALLOWED = ('/account/inactive/', '/terms/', '/logout/', '/static/', '/media/', '/notifications/')


class AccountStatusMiddleware:
    """Server-side block for students whose account the warning policy deactivated.
    Records stay intact; the student can still log in to read why and what to do."""
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, 'user', None)
        if user is not None and user.is_authenticated and not request.path.startswith(INACTIVE_ALLOWED):
            profile = Profile.objects.filter(user=user).only('role', 'account_status').first()
            if (profile is not None and profile.role == Profile.ROLE_STUDENT
                    and profile.account_status == Profile.ACCOUNT_INACTIVE):
                return redirect('account_inactive')
        return self.get_response(request)
