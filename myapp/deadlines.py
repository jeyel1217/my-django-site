"""
myapp/deadlines.py

The deadline + warning policy in one place.

  * Who may submit a quiz, and until when (late setting, extensions, exemptions).
  * Which quizzes are overdue for a student.
  * Notifications: in-portal message + e-mail, recorded, never duplicated.
  * run_policy(): the job that runs on the server (python manage.py process_deadlines):
        reminders -> overdue notices -> final warnings -> end-of-window evaluation
        -> (admin-approved) deactivation.

Nothing here depends on a browser being open. Every step is safe to run twice:
a notice has a unique key, a warning is never issued twice for the same quizzes.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.utils import formats, timezone

from .models import (
    Profile, Quiz, QuizAttempt, QuizAccommodation, PolicySettings, FinalWarning,
    Notification, AccountStatusLog, AuditLog, Enrollment,
)

log = logging.getLogger(__name__)

MAX_EMAIL_ATTEMPTS = 3

# What the student did while a final warning was open (information for the Admin).
ACT_SUBMITTED = 'Submitted the work'
ACT_EXTENDED = 'Received an extension or exemption'
ACT_LOGGED_IN = 'Logged in but did not submit'
ACT_NOT_LOGGED_IN = 'Did not log in'


# ---------------------------------------------------------------------------
# Deadline rules
# ---------------------------------------------------------------------------
def accommodations_for(user, quizzes=None):
    qs = QuizAccommodation.objects.filter(student=user)
    if quizzes is not None:
        qs = qs.filter(quiz__in=quizzes)
    return {a.quiz_id: a for a in qs}


def effective_deadline(quiz, accommodation=None):
    """The deadline that applies to ONE student: the quiz deadline, or their
    extension if the Mentor gave them a later one. None = no deadline."""
    if quiz.deadline_datetime is None:
        return None
    if (accommodation and accommodation.kind == QuizAccommodation.KIND_EXTENSION
            and accommodation.new_deadline and accommodation.new_deadline > quiz.deadline_datetime):
        return accommodation.new_deadline
    return quiz.deadline_datetime


def is_exempt(accommodation):
    return bool(accommodation and accommodation.kind == QuizAccommodation.KIND_EXEMPTION)


def submission_status(quiz, user, now=None):
    """May this student submit this quiz right now?
    Returns (allowed, deadline_for_student, closed_reason)."""
    now = now or timezone.now()
    acc = QuizAccommodation.objects.filter(quiz=quiz, student=user).first()
    deadline = effective_deadline(quiz, acc)
    if deadline is not None and now > deadline and not quiz.allow_late_submissions and not is_exempt(acc):
        return False, deadline, 'The deadline has passed and your mentor does not accept late submissions.'
    return True, deadline, ''


def student_quizzes(user):
    """Published quizzes from classrooms the student is currently in."""
    return (Quiz.objects.filter(
        is_published=True,
        classroom__enrollments__student=user, classroom__enrollments__is_active=True,
    ).select_related('classroom__mentor__profile').distinct())


def overdue_items(user, now=None):
    """Unresolved overdue quizzes for one student:
    [{'quiz', 'deadline'}] sorted oldest first. A quiz counts when it is open,
    its deadline (with any extension) has passed, the student has not submitted
    it and has no exemption."""
    now = now or timezone.now()
    quizzes = list(student_quizzes(user))
    if not quizzes:
        return []
    done = set(QuizAttempt.objects.filter(user=user, quiz__in=quizzes).values_list('quiz_id', flat=True))
    accs = accommodations_for(user, quizzes)
    out = []
    for q in quizzes:
        if q.id in done or q.is_not_open_yet(now):
            continue
        acc = accs.get(q.id)
        if is_exempt(acc):
            continue
        deadline = effective_deadline(q, acc)
        if deadline is not None and deadline < now:
            out.append({'quiz': q, 'deadline': deadline})
    out.sort(key=lambda i: i['deadline'])
    return out


def policy_students():
    """Active student accounts the policy applies to."""
    return (User.objects.filter(
        is_active=True, profile__role=Profile.ROLE_STUDENT,
        profile__is_verified=True, profile__account_status=Profile.ACCOUNT_ACTIVE,
    ).select_related('profile').order_by('id'))


# ---------------------------------------------------------------------------
# Wording helpers
# ---------------------------------------------------------------------------
def fmt_dt(dt):
    """'Oct 8, 2026 - 5:00 PM (Philippine Time)'."""
    from .exams import APP_TZ
    local = timezone.localtime(dt, APP_TZ)
    return f"{formats.date_format(local, 'M j, Y')} – {formats.date_format(local, 'g:i A')} (Philippine Time)"


def _portal_url():
    return getattr(settings, 'SITE_URL', 'http://127.0.0.1:8000').rstrip('/') + '/login/'


def _signoff():
    return f"\n\nOpen the student portal: {_portal_url()}\n\n— JCAD CodeQuest, Colegio de San Gabriel Arcangel"


def _classroom_label(quiz):
    room = quiz.classroom
    return f"{room.name} (Mentor: {room.mentor_name})" if room else "your classroom"


# ---------------------------------------------------------------------------
# Notifications: record first, then e-mail
# ---------------------------------------------------------------------------
def _try_email(note):
    """Send the e-mail for a saved notification and record the TRUE result."""
    note.email_attempts += 1
    user = note.user
    if not user.email:
        note.email_status = Notification.EMAIL_NO_ADDRESS
        note.email_error = 'This account has no email address.'
    else:
        try:
            send_mail(note.subject, note.message, None, [user.email], fail_silently=False)
            note.email_status = Notification.EMAIL_SENT
            note.email_error = ''
            note.email_sent_at = timezone.now()
        except Exception as exc:                      # never claim success when the mail server said no
            note.email_status = Notification.EMAIL_FAILED
            note.email_error = f"{type(exc).__name__}: {exc}"[:255]
            log.warning('E-mail to %s failed: %s', user.username, exc)
    note.save(update_fields=['email_status', 'email_error', 'email_attempts', 'email_sent_at'])
    return note


def notify(user, kind, subject, message, dedupe_key, quiz=None, warning=None,
           send_email=True, hidden=False):
    """Create the in-portal notification (always) and e-mail it (if wanted).
    Returns the Notification, or None when this exact notice already exists."""
    try:
        with transaction.atomic():
            note = Notification.objects.create(
                user=user, kind=kind, quiz=quiz, warning=warning, subject=subject[:200],
                message=message, dedupe_key=dedupe_key[:160], hidden=hidden,
                email_status=Notification.EMAIL_PENDING if send_email else Notification.EMAIL_NOT_SENT,
            )
    except IntegrityError:
        return None                                    # already created by an earlier / parallel run
    if send_email:
        _try_email(note)
    return note


def retry_failed_emails(now=None):
    """Temporary failures get another go (up to MAX_EMAIL_ATTEMPTS)."""
    n = 0
    for note in Notification.objects.filter(
            email_status=Notification.EMAIL_FAILED, email_attempts__lt=MAX_EMAIL_ATTEMPTS).select_related('user'):
        _try_email(note)
        n += 1
    return n


def _admins():
    return User.objects.filter(is_active=True, profile__role=Profile.ROLE_ADMIN)


def alert_admins(subject, message, key_prefix, warning=None):
    for admin in _admins():
        notify(admin, Notification.KIND_ADMIN_ALERT, subject, message,
               f"{key_prefix}:admin{admin.id}", warning=warning, send_email=bool(admin.email))


# ---------------------------------------------------------------------------
# The messages
# ---------------------------------------------------------------------------
def _name(user):
    return user.profile.display_name


def reminder_message(user, quiz, deadline, hours):
    subject = f"Reminder: \"{quiz.title}\" is due {fmt_dt(deadline)}"
    body = (
        f"Hi {_name(user)},\n\n"
        f"This is a reminder that you have not yet completed a quiz.\n\n"
        f"Quiz: {quiz.title}\n"
        f"Classroom: {_classroom_label(quiz)}\n"
        f"Due: {fmt_dt(deadline)}\n\n"
        f"Please complete it before the deadline."
    ) + _signoff()
    return subject, body


def overdue_message(user, quiz, deadline):
    if quiz.allow_late_submissions:
        late = "Your mentor allows late submissions, so you can still submit. It will be marked Late."
    else:
        late = ("Your mentor does not accept late submissions. Please contact your mentor "
                "if you have a valid reason, so they can consider an extension.")
    subject = f"Overdue: \"{quiz.title}\" was due {fmt_dt(deadline)}"
    body = (
        f"Hi {_name(user)},\n\n"
        f"You have not submitted the quiz below before its deadline.\n\n"
        f"Quiz: {quiz.title}\n"
        f"Classroom: {_classroom_label(quiz)}\n"
        f"Original deadline: {fmt_dt(deadline)}\n"
        f"Current status: OVERDUE (not submitted)\n\n"
        f"What to do next: {late}\n\n"
        f"An overdue quiz does not deactivate your account by itself."
    ) + _signoff()
    return subject, body


def final_warning_message(user, warning, items):
    lines = "\n".join(f"  - {i['quiz'].title} (original deadline: {fmt_dt(i['deadline'])})" for i in items)
    subject = f"FINAL WARNING: act before {fmt_dt(warning.expires_at)}"
    body = (
        f"Hi {_name(user)},\n\n"
        f"You still have overdue quizzes that were not submitted:\n\n{lines}\n\n"
        f"You must take action within the next {_window_hours(warning)} hours. "
        f"This warning period ends on:\n\n  {fmt_dt(warning.expires_at)}\n\n"
        f"To fix this, submit the quizzes (if your mentor allows late submissions) or contact "
        f"your mentor or administrator if there is a valid reason.\n\n"
        f"If these quizzes are still unresolved when the period ends, your account may be "
        f"deactivated under the Terms and Conditions you accepted. Your grades and records "
        f"would be kept, and an administrator can reactivate the account."
    ) + _signoff()
    return subject, body


def _window_hours(warning):
    return int(round((warning.expires_at - warning.issued_at).total_seconds() / 3600))


# ---------------------------------------------------------------------------
# The scheduled job
# ---------------------------------------------------------------------------
def run_policy(now=None, dry_run=False):
    """Run every step once. Returns a dict of counts for the report."""
    now = now or timezone.now()
    policy = PolicySettings.get()
    rep = {'reminders': 0, 'overdue': 0, 'warnings': 0, 'resolved': 0,
           'pending_review': 0, 'deactivated': 0, 'retried': 0}

    if not dry_run:
        rep['retried'] = retry_failed_emails(now)

    students = list(policy_students())
    for user in students:
        rep['reminders'] += _send_reminders(user, policy, now, dry_run)
        rep['overdue'] += _send_overdue(user, policy, now, dry_run)
    for user in students:
        if _maybe_issue_warning(user, policy, now, dry_run):
            rep['warnings'] += 1

    for warning in FinalWarning.objects.filter(status=FinalWarning.STATUS_OPEN, expires_at__lte=now) \
            .select_related('student__profile'):
        outcome = _evaluate_warning(warning, policy, now, dry_run)
        if outcome in rep:
            rep[outcome] += 1
    return rep


# --- 1. reminders -----------------------------------------------------------
def _send_reminders(user, policy, now, dry_run):
    hours = policy.reminder_hours_list
    if not hours:
        return 0
    sent = 0
    done = set(QuizAttempt.objects.filter(user=user).values_list('quiz_id', flat=True))
    accs = accommodations_for(user)
    for quiz in student_quizzes(user):
        if quiz.id in done or quiz.is_not_open_yet(now) or quiz.deadline_datetime is None:
            continue
        acc = accs.get(quiz.id)
        if is_exempt(acc):
            continue
        deadline = effective_deadline(quiz, acc)
        remaining = (deadline - now).total_seconds()
        if remaining <= 0:
            continue
        crossed = [h for h in hours if remaining <= h * 3600]
        if not crossed:
            continue
        chosen = min(crossed)               # the closest reminder that applies right now
        base = f"reminder:{quiz.id}:{user.id}:{deadline.strftime('%Y%m%d%H%M')}"
        # Larger windows that were already crossed are marked as covered, so they never arrive late.
        for h in crossed:
            if h != chosen and not dry_run:
                notify(user, Notification.KIND_REMINDER, 'covered', 'covered', f"{base}:{h}",
                       quiz=quiz, send_email=False, hidden=True)
        key = f"{base}:{chosen}"
        if Notification.objects.filter(dedupe_key=key).exists():
            continue
        if dry_run:
            sent += 1
            continue
        subject, body = reminder_message(user, quiz, deadline, chosen)
        if notify(user, Notification.KIND_REMINDER, subject, body, key, quiz=quiz):
            sent += 1
    return sent


# --- 2. overdue -------------------------------------------------------------
def _send_overdue(user, policy, now, dry_run):
    sent = 0
    for item in overdue_items(user, now):
        quiz, deadline = item['quiz'], item['deadline']
        key = f"overdue:{quiz.id}:{user.id}:{deadline.strftime('%Y%m%d%H%M')}"
        if Notification.objects.filter(dedupe_key=key).exists():
            continue
        if dry_run:
            sent += 1
            continue
        subject, body = overdue_message(user, quiz, deadline)
        if notify(user, Notification.KIND_OVERDUE, subject, body, key, quiz=quiz,
                  send_email=policy.send_overdue_email):
            sent += 1
    return sent


# --- 3. final warning -------------------------------------------------------
def _maybe_issue_warning(user, policy, now, dry_run):
    if FinalWarning.objects.filter(student=user, status__in=[
            FinalWarning.STATUS_OPEN, FinalWarning.STATUS_PENDING_REVIEW]).exists():
        return False                                  # one case at a time

    items = overdue_items(user, now)
    # A quiz that already belonged to an earlier warning never triggers another
    # one, unless staff reset that warning.
    covered = set(FinalWarning.objects.filter(student=user)
                  .exclude(status=FinalWarning.STATUS_RESET).values_list('quizzes__id', flat=True))
    eligible = [i for i in items if i['quiz'].id not in covered]
    if len(eligible) < max(policy.warn_threshold, 1):
        return False
    oldest_age = (now - eligible[0]['deadline']).total_seconds() / 3600
    if oldest_age < policy.warn_after_overdue_hours:
        return False
    if dry_run:
        return True

    window = max(policy.warning_window_hours, 24)
    with transaction.atomic():
        warning = FinalWarning.objects.create(
            student=user, issued_at=now, expires_at=now + timedelta(hours=window),
            status=FinalWarning.STATUS_OPEN,
        )
        warning.quizzes.set([i['quiz'] for i in eligible])
    subject, body = final_warning_message(user, warning, eligible)
    notify(user, Notification.KIND_FINAL_WARNING, subject, body, f"final:{warning.id}", warning=warning)
    AuditLog.record(None, 'final_warning_issued', user,
                    f"{len(eligible)} overdue quiz(zes); window ends {warning.expires_at:%Y-%m-%d %H:%M} UTC")
    return True


# --- 4. end of the warning window ------------------------------------------
def classify_activity(warning, remaining_ids):
    """What the student did while the warning was open."""
    quiz_ids = list(warning.quizzes.values_list('id', flat=True))
    submitted = QuizAttempt.objects.filter(
        user=warning.student, quiz_id__in=quiz_ids, taken_at__gte=warning.issued_at).exists()
    accs = QuizAccommodation.objects.filter(student=warning.student, quiz_id__in=quiz_ids,
                                            created_at__gte=warning.issued_at).exists()
    if not remaining_ids and submitted:
        return ACT_SUBMITTED
    if accs:
        return ACT_EXTENDED
    last = warning.student.last_login
    if last and last >= warning.issued_at:
        return ACT_LOGGED_IN
    return ACT_NOT_LOGGED_IN


def _evaluate_warning(warning, policy, now, dry_run):
    user = warning.student
    warned_ids = set(warning.quizzes.values_list('id', flat=True))
    remaining = [i for i in overdue_items(user, now) if i['quiz'].id in warned_ids]
    remaining_ids = [i['quiz'].id for i in remaining]
    activity = classify_activity(warning, remaining_ids)

    if len(remaining) < max(policy.deactivate_min_unresolved, 1):
        if not dry_run:
            warning.status = FinalWarning.STATUS_RESOLVED
            warning.evaluated_at = now
            warning.activity = activity
            warning.resolution_note = ('All warned quizzes were resolved (submitted, extended or exempt).'
                                       if not remaining else
                                       'Too few unresolved quizzes remain to meet the deactivation policy.')
            warning.save()
        return 'resolved'

    if dry_run:
        return 'deactivated' if policy.auto_deactivation_approved else 'pending_review'

    warning.evaluated_at = now
    warning.activity = activity
    if not policy.auto_deactivation_approved:
        # The Admin has not approved automatic deactivation: a person decides.
        warning.status = FinalWarning.STATUS_PENDING_REVIEW
        warning.resolution_note = 'Window ended with unresolved quizzes. Waiting for an Admin to review.'
        warning.save()
        alert_admins(
            f"Review needed: {_name(user)} did not resolve a final warning",
            f"{_name(user)} ({user.username}) still has {len(remaining)} unresolved overdue quiz(zes) after the "
            f"final warning period. Activity: {activity}.\n\nOpen the admin Monitoring page to review.",
            f"review:{warning.id}", warning=warning)
        return 'pending_review'

    deactivate_account(user, warning, actor=None, now=now, activity=activity, remaining=remaining)
    return 'deactivated'


# ---------------------------------------------------------------------------
# Deactivation / reactivation (records are never deleted)
# ---------------------------------------------------------------------------
def _case_summary(user, remaining):
    lines = [f"- {i['quiz'].title} (deadline {fmt_dt(i['deadline'])})" for i in remaining]
    warnings = FinalWarning.objects.filter(student=user).order_by('issued_at')
    wl = [f"- Final warning issued {fmt_dt(w.issued_at)}, ended {fmt_dt(w.expires_at)}: {w.get_status_display()}"
          for w in warnings]
    return "Overdue quizzes:\n" + ("\n".join(lines) or "- none listed") + "\n\nWarning history:\n" + ("\n".join(wl) or "- none")


def deactivate_account(user, warning, actor, now=None, activity='', remaining=None, reason=None):
    """Mark a student account INACTIVE. Grades, attempts and history stay untouched."""
    now = now or timezone.now()
    if remaining is None:
        remaining = overdue_items(user, now)
    reason = reason or "Unresolved overdue quizzes after the final warning period ended."
    with transaction.atomic():
        profile = Profile.objects.select_for_update().get(user=user)
        if profile.account_status == Profile.ACCOUNT_INACTIVE:
            return False
        profile.account_status = Profile.ACCOUNT_INACTIVE
        profile.deactivated_at = now
        profile.deactivation_reason = reason[:300]
        profile.save(update_fields=['account_status', 'deactivated_at', 'deactivation_reason'])
        if warning is not None:
            warning.status = FinalWarning.STATUS_DEACTIVATED
            warning.evaluated_at = warning.evaluated_at or now
            warning.activity = warning.activity or activity
            warning.resolution_note = reason[:300]
            warning.save()
        AccountStatusLog.objects.create(
            user=user, action=AccountStatusLog.ACTION_DEACTIVATED, reason=reason, warning=warning,
            actor=actor, details=_case_summary(user, remaining))
        AuditLog.record(actor, 'account_deactivated', user, reason)

    contact = getattr(settings, 'ADMIN_CONTACT_EMAIL', '') or 'your administrator'
    body = (
        f"Hi {_name(user)},\n\n"
        f"Your JCAD CodeQuest account has been deactivated.\n\n"
        f"Reason: {reason}\n\n"
        f"What this means: you cannot use the student features until an administrator reactivates "
        f"your account. Your grades, quiz attempts and records are all kept.\n\n"
        f"Next steps: contact your mentor or the administrator ({contact}) and explain your situation. "
        f"They can review your case and reactivate your account when appropriate."
    ) + _signoff()
    notify(user, Notification.KIND_DEACTIVATED, 'Your account has been deactivated', body,
           f"deactivated:{user.id}:{now.strftime('%Y%m%d%H%M%S')}", warning=warning)
    alert_admins(
        f"Account deactivated: {_name(user)}",
        f"{_name(user)} ({user.username}) was deactivated. Reason: {reason}\n\n{_case_summary(user, remaining)}",
        f"deactivated-alert:{user.id}:{now.strftime('%Y%m%d%H%M%S')}", warning=warning)
    return True


def reactivate_account(user, actor, note='', now=None):
    now = now or timezone.now()
    with transaction.atomic():
        profile = Profile.objects.select_for_update().get(user=user)
        if profile.account_status != Profile.ACCOUNT_INACTIVE:
            return False
        profile.account_status = Profile.ACCOUNT_ACTIVE
        profile.deactivated_at = None
        profile.deactivation_reason = ''
        profile.save(update_fields=['account_status', 'deactivated_at', 'deactivation_reason'])
        # The warning stays in the history (so the same quizzes cannot trigger it again).
        FinalWarning.objects.filter(student=user, status=FinalWarning.STATUS_DEACTIVATED).update(
            resolution_note='Account deactivated, then reactivated by an administrator.')
        AccountStatusLog.objects.create(
            user=user, action=AccountStatusLog.ACTION_REACTIVATED, reason=note[:300], actor=actor,
            details='Reactivated by an administrator. All records were kept.')
        AuditLog.record(actor, 'account_reactivated', user, note)
    body = (
        f"Hi {_name(user)},\n\nYour JCAD CodeQuest account has been reactivated. You can log in "
        f"and continue. Please check your overdue quizzes and complete them or talk to your mentor."
    ) + _signoff()
    notify(user, Notification.KIND_REACTIVATED, 'Your account has been reactivated', body,
           f"reactivated:{user.id}:{now.strftime('%Y%m%d%H%M%S')}")
    return True
