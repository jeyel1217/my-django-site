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
from django.utils import timezone

from .models import OTPCode

OTP_LENGTH = 6
OTP_VALID_MINUTES = 10
RESEND_COOLDOWN_SECONDS = 60


def _hash_code(code):
    return hashlib.sha256(code.encode()).hexdigest()


def _generate_code():
    # 6-digit numeric code, e.g. "042917" — zero-padded so it's always 6 digits.
    return f"{secrets.randbelow(10**OTP_LENGTH):0{OTP_LENGTH}d}"


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


def generate_and_send_otp(user, purpose):
    """Creates a new OTPCode, invalidates old unused ones for the same
    purpose, and emails the code to the user. Returns True if the email
    was actually sent, False if sending failed (e.g. SMTP issue) — the
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

    if purpose == OTPCode.PURPOSE_VERIFY_EMAIL:
        subject = "Verify your JCAD CodeQuest account"
        message = (
            f"Hi {user.username},\n\n"
            f"Your verification code is: {code}\n\n"
            f"This code expires in {OTP_VALID_MINUTES} minutes.\n"
            f"If you didn't create this account, you can ignore this email."
        )
    else:  # RESET_PASSWORD
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
        # SMTP hiccup, Gmail hold, network issue, etc. Don't crash the
        # page — the code still exists in the DB, so a resend will work
        # once the underlying issue clears.
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