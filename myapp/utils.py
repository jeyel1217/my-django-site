"""
myapp/utils.py

OTP (one-time code) helpers for email verification and password reset.

Security notes (matches the spec):
- Codes are 6 digits, random, expire in 10 minutes, single-use.
- Codes are stored HASHED (sha256), never in plain text.
- Resending is rate-limited to once every 60 seconds per user+purpose.
- Wrong-code attempts are capped at OTPCode.MAX_ATTEMPTS.
"""

import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.urls import reverse
from django.utils import timezone

from .models import OTPCode

OTP_LENGTH = 6
OTP_VALID_MINUTES = 10
LINK_VALID_HOURS = 24
RESEND_COOLDOWN_SECONDS = 60


def _hash_code(code):
    return hashlib.sha256(code.encode()).hexdigest()


def _generate_code():
    # 6-digit numeric code, e.g. "042917" — zero-padded so it's always 6 digits.
    return f"{secrets.randbelow(10**OTP_LENGTH):0{OTP_LENGTH}d}"


def _generate_link_token():
    # Long random URL-safe token — brute force isn't realistic at this length.
    return secrets.token_urlsafe(32)


def can_resend(user, purpose):
    """Returns True if enough time has passed since the last code was sent."""
    last = OTPCode.objects.filter(user=user, purpose=purpose).order_by('-created_at').first()
    if last is None:
        return True
    seconds_since = (timezone.now() - last.created_at).total_seconds()
    return seconds_since >= RESEND_COOLDOWN_SECONDS


def seconds_until_resend(user, purpose):
    last = OTPCode.objects.filter(user=user, purpose=purpose).order_by('-created_at').first()
    if last is None:
        return 0
    seconds_since = (timezone.now() - last.created_at).total_seconds()
    remaining = RESEND_COOLDOWN_SECONDS - seconds_since
    return max(0, int(remaining))


def generate_and_send_verification_link(request, user):
    """EMAIL VERIFICATION (registration) — sends a one-click confirmation
    link instead of a typed code. Works even if the link is opened on a
    different device/browser than the one used to register."""
    if not user.email:
        return False

    OTPCode.objects.filter(
        user=user, purpose=OTPCode.PURPOSE_VERIFY_EMAIL, is_used=False
    ).update(is_used=True)

    token = _generate_link_token()
    OTPCode.objects.create(
        user=user,
        purpose=OTPCode.PURPOSE_VERIFY_EMAIL,
        code_hash=_hash_code(token),
        expires_at=timezone.now() + timedelta(hours=LINK_VALID_HOURS),
    )

    link_path = reverse('verify_email_confirm', args=[user.id, token])
    link = request.build_absolute_uri(link_path)

    subject = "Confirm your JCAD CodeQuest account"
    message = (
        f"Hi {user.username},\n\n"
        f"Click the link below to confirm your email and activate your account:\n\n"
        f"{link}\n\n"
        f"This link expires in {LINK_VALID_HOURS} hours.\n"
        f"If you didn't create this account, you can ignore this email."
    )

    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
            fail_silently=False,
        )
    except Exception:
        return False

    return True


def verify_link_token(user, submitted_token):
    """Checks a clicked confirmation link's token. Returns (success, error)."""
    otp = OTPCode.objects.filter(
        user=user, purpose=OTPCode.PURPOSE_VERIFY_EMAIL, is_used=False
    ).order_by('-created_at').first()

    if otp is None:
        return False, "This link is no longer valid. Please request a new one."

    if otp.is_expired():
        return False, "This link has expired. Please request a new one."

    if _hash_code(submitted_token) != otp.code_hash:
        return False, "This link is invalid. Please request a new one."

    otp.is_used = True
    otp.save(update_fields=['is_used'])
    return True, None


def generate_and_send_otp(user, purpose):
    """PASSWORD RESET — sends a typed 6-digit code (proves the person has
    access to the account right now, at the moment of reset). Returns
    True if the email was actually sent, False if sending failed — the
    code is still created either way so a retry/resend can reuse it."""
    if not user.email:
        return False

    # Invalidate any previous unused codes of this purpose so only the
    # latest one is ever valid.
    OTPCode.objects.filter(user=user, purpose=purpose, is_used=False).update(is_used=True)

    code = _generate_code()
    OTPCode.objects.create(
        user=user,
        purpose=purpose,
        code_hash=_hash_code(code),
        expires_at=timezone.now() + timedelta(minutes=OTP_VALID_MINUTES),
    )

    subject = "Reset your JCAD CodeQuest password"
    message = (
        f"Hi {user.username},\n\n"
        f"Your password reset code is: {code}\n\n"
        f"This code expires in {OTP_VALID_MINUTES} minutes.\n"
        f"If you didn't request this, you can ignore this email — "
        f"your password will not be changed."
    )

    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
            fail_silently=False,
        )
    except Exception:
        return False

    return True


def verify_otp(user, purpose, submitted_code):
    """Checks a submitted code against the latest valid OTPCode for this
    user+purpose. Returns (success: bool, error_message: str|None)."""
    otp = OTPCode.objects.filter(user=user, purpose=purpose, is_used=False).order_by('-created_at').first()

    if otp is None:
        return False, "No active code found. Please request a new one."

    if otp.is_expired():
        return False, "This code has expired. Please request a new one."

    if otp.attempts >= OTPCode.MAX_ATTEMPTS:
        return False, "Too many incorrect attempts. Please request a new code."

    if _hash_code(submitted_code) != otp.code_hash:
        otp.attempts += 1
        otp.save(update_fields=['attempts'])
        remaining = OTPCode.MAX_ATTEMPTS - otp.attempts
        if remaining <= 0:
            return False, "Too many incorrect attempts. Please request a new code."
        return False, f"Incorrect code. {remaining} attempt(s) remaining."

    otp.is_used = True
    otp.save(update_fields=['is_used'])
    return True, None