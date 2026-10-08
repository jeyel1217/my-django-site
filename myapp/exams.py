"""
myapp/exams.py

Everything about exam deadlines and student progress lives here:

* Philippine time handling (so "5:00 PM" means 5:00 PM in the Philippines,
  even though the server itself runs on UTC).
* The late / on-time rule, calculated from real timestamps.
* The progress table + summary shown to Mentors and Admins.
* The status wording shown to Students.
"""

from datetime import timedelta, timezone as dt_timezone
from functools import wraps

from django.contrib.auth.models import User
from django.utils import timezone

from .models import Profile, QuizAttempt, QuizAccommodation

# ---------------------------------------------------------------------------
# Time zone
# ---------------------------------------------------------------------------
# The Philippines has no daylight saving, so a fixed UTC+8 offset is exact and
# needs no extra packages. Deadlines are stored in UTC like every other
# timestamp; this only controls how they are typed in and shown.
APP_TZ = dt_timezone(timedelta(hours=8), 'PHT')


def local_time(view):
    """Run a view (including its template rendering) in Philippine time."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        with timezone.override(APP_TZ):
            return view(request, *args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# Status wording (Mentor / Admin view)
# Every status has a symbol AND text, so colour is never the only clue.
# ---------------------------------------------------------------------------
ON_TIME = 'on_time'
LATE = 'late'
COMPLETED = 'completed'      # submitted, and the quiz has no deadline
EXEMPT = 'exempt'            # the Mentor excused this student from the quiz
NOT_TAKEN = 'not_taken'
MISSED = 'missed'            # no submission and the deadline has passed

STAFF_STATUS = {
    ON_TIME:   ('\u2713 On Time',         'bg-emerald-50 text-emerald-700 border-emerald-200'),
    LATE:      ('\u26a0 Late',            'bg-red-50 text-red-700 border-red-200'),
    COMPLETED: ('\u2713 Completed',       'bg-sky-50 text-sky-700 border-sky-200'),
    NOT_TAKEN: ('\u25cb Not Taken',       'bg-slate-100 text-slate-600 border-slate-200'),
    MISSED:    ('\u26a0 Missed Deadline', 'bg-orange-50 text-orange-700 border-orange-200'),
    EXEMPT:    ('\u2713 Exempt',         'bg-violet-50 text-violet-700 border-violet-200'),
}

# Filter buttons on the progress page (query string ?show=...)
FILTERS = [
    ('all', 'All'),
    ('completed', 'Completed'),
    ('on_time', 'On Time'),
    ('late', 'Late'),
    ('not_taken', 'Not Taken'),
]


def humanize_late(delta):
    """timedelta -> '1 day 2 hr late' / '45 min late'."""
    total_minutes = int(delta.total_seconds() // 60)
    if total_minutes < 1:
        return 'less than a minute late'
    days, rem = divmod(total_minutes, 1440)
    hours, minutes = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours:
        parts.append(f"{hours} hr")
    if minutes and not days:
        parts.append(f"{minutes} min")
    return ' '.join(parts) + ' late'


def staff_status_for_attempt(quiz, attempt):
    """ON_TIME / LATE / COMPLETED for one submission."""
    deadline = attempt.effective_deadline   # the deadline that applied to THIS student
    if deadline is None:
        return COMPLETED
    return LATE if attempt.taken_at > deadline else ON_TIME


# ---------------------------------------------------------------------------
# Who counts as a student
# ---------------------------------------------------------------------------
def active_students(classroom=None):
    """Real Student accounts: right role, still active, and e-mail verified.
    With a classroom: only students currently enrolled in THAT classroom."""
    qs = User.objects.filter(is_active=True, profile__role=Profile.ROLE_STUDENT, profile__is_verified=True)
    if classroom is not None:
        qs = qs.filter(enrollments__classroom=classroom, enrollments__is_active=True)
    return qs.select_related('profile').order_by('username')


# ---------------------------------------------------------------------------
# Progress table for ONE quiz (Mentor + Admin)
# ---------------------------------------------------------------------------
def build_progress(quiz, show='all', now=None):
    """Returns {'rows': [...], 'summary': {...}} for the given quiz.

    Policy for students who submit more than once: the FIRST submission is the
    official one for the deadline (a later retake can neither fix a late
    submission nor make an on-time one late). The score column shows their best
    result, and the number of attempts is listed next to it.
    """
    now = now or timezone.now()
    # Only the students enrolled in this quiz's classroom (none if it has no classroom).
    students = list(active_students(quiz.classroom)) if quiz.classroom_id else []
    by_user = {}
    attempts = (
        QuizAttempt.objects
        .filter(quiz=quiz, user__in=students)
        .order_by('taken_at', 'id')
    )
    for a in attempts:
        by_user.setdefault(a.user_id, []).append(a)

    past_deadline = quiz.is_past_deadline(now)
    rows = []
    summary = {'total': len(students), 'completed': 0, 'on_time': 0, 'late': 0,
               'not_taken': 0, 'missed': 0, 'exempt': 0}
    accs = {a.student_id: a for a in QuizAccommodation.objects.filter(quiz=quiz)}

    for s in students:
        mine = by_user.get(s.pk, [])
        row = {
            'user': s,
            'name': s.profile.display_name,
            'username': s.username,
            'attempts': len(mine),
            'first': None, 'best': None,
            'started_at': None, 'submitted_at': None,
            'score_text': None, 'percentage': None,
            'late_text': '', 'submitted_css': 'text-slate-700',
            'deadline_css': 'text-slate-700',
        }
        if mine:
            first = mine[0]
            best = max(mine, key=lambda a: (a.score, -a.pk))
            status = staff_status_for_attempt(quiz, first)
            row.update(first=first, best=best, started_at=first.started_at,
                       submitted_at=first.taken_at, percentage=best.score)
            if best.correct_count is not None and best.total_questions:
                row['score_text'] = f"{best.correct_count}/{best.total_questions}"
            summary['completed'] += 1
            if status == ON_TIME:
                summary['on_time'] += 1
                row['submitted_css'] = 'text-emerald-600 font-semibold'
            elif status == LATE:
                summary['late'] += 1
                row['submitted_css'] = 'text-red-600 font-semibold'
                row['late_text'] = humanize_late(first.taken_at - first.effective_deadline)
        else:
            acc = accs.get(s.pk)
            student_deadline = effective_deadline(quiz, acc)
            if acc is not None and acc.kind == QuizAccommodation.KIND_EXEMPTION:
                status = EXEMPT
                summary['exempt'] += 1
            elif student_deadline is not None and student_deadline < now:
                status = MISSED
                summary['not_taken'] += 1
                summary['missed'] += 1
                row['deadline_css'] = 'text-orange-600 font-semibold'
            else:
                status = NOT_TAKEN
                summary['not_taken'] += 1
        acc_for_row = accs.get(s.pk)
        row['accommodation'] = acc_for_row
        row['status'] = status
        row['status_label'], row['status_css'] = STAFF_STATUS[status]
        rows.append(row)

    wanted = {
        'all': None,
        'completed': {ON_TIME, LATE, COMPLETED},
        'on_time': {ON_TIME},
        'late': {LATE},
        'not_taken': {NOT_TAKEN, MISSED, EXEMPT},
    }.get(show)
    if wanted is not None:
        rows = [r for r in rows if r['status'] in wanted]

    return {'rows': rows, 'summary': summary}


def quiz_summary(quiz, now=None):
    """Just the counts (for the overview pages)."""
    return build_progress(quiz, 'all', now)['summary']


# ---------------------------------------------------------------------------
# Every submission, newest first (Admin activity log)
# ---------------------------------------------------------------------------
def submission_log(mentor_id=None, limit=100):
    qs = (
        QuizAttempt.objects
        .filter(user__profile__role=Profile.ROLE_STUDENT)
        .select_related('user__profile', 'quiz__created_by__profile')
        .order_by('-taken_at', '-id')
    )
    if mentor_id:
        qs = qs.filter(quiz__created_by_id=mentor_id)
    out = []
    for a in qs[:limit]:
        status = staff_status_for_attempt(a.quiz, a)
        label, css = STAFF_STATUS[status]
        mentor = a.quiz.created_by
        out.append({
            'attempt': a,
            'mentor': mentor.profile.display_name if mentor else '\u2014',
            'student': a.user.profile.display_name,
            'username': a.user.username,
            'status': status, 'status_label': label, 'status_css': css,
            'late_text': humanize_late(a.taken_at - a.effective_deadline) if status == LATE else '',
            'submitted_css': {'on_time': 'text-emerald-600 font-semibold',
                              'late': 'text-red-600 font-semibold'}.get(status, 'text-slate-700'),
        })
    return out


# ---------------------------------------------------------------------------
# What a STUDENT sees about their own quiz
# ---------------------------------------------------------------------------
STUDENT_STATUS = {
    'not_open':   ('\u25cb Upcoming',       'bg-slate-100 text-slate-600 border-slate-200'),
    'not_taken':  ('\u25cf Available',      'bg-sky-50 text-sky-700 border-sky-200'),
    'overdue':    ('\u26a0 Overdue',        'bg-orange-50 text-orange-700 border-orange-200'),
    'submitted':  ('\u2713 Submitted',      'bg-emerald-50 text-emerald-700 border-emerald-200'),
    'late':       ('\u26a0 Submitted Late', 'bg-red-50 text-red-700 border-red-200'),
    'exempt':     ('\u2713 Exempt',         'bg-violet-50 text-violet-700 border-violet-200'),
}


def effective_deadline(quiz, accommodation=None):
    """Per-student deadline (quiz deadline, or the student's extension)."""
    from .deadlines import effective_deadline as _eff
    return _eff(quiz, accommodation)


def student_quiz_state(quiz, first_attempt, now=None, accommodation=None):
    """Status badge for one student + one quiz. Only ever built from that
    student's OWN attempt, so nothing about other students can leak.
    `deadline` is the date that applies to THIS student (extension included);
    `closed` is True when they can no longer submit."""
    now = now or timezone.now()
    deadline = effective_deadline(quiz, accommodation)
    exempt = bool(accommodation and accommodation.kind == QuizAccommodation.KIND_EXEMPTION)
    if first_attempt is not None:
        code = 'late' if first_attempt.is_late else 'submitted'
    elif exempt:
        code = 'exempt'
    elif quiz.is_not_open_yet(now):
        code = 'not_open'
    elif deadline is not None and now > deadline:
        code = 'overdue'
    else:
        code = 'not_taken'
    label, css = STUDENT_STATUS[code]
    closed = code == 'overdue' and not quiz.allow_late_submissions
    return {'code': code, 'label': label, 'css': css, 'deadline': deadline,
            'extended': bool(deadline and quiz.deadline_datetime and deadline > quiz.deadline_datetime),
            'closed': closed}


def first_attempts_by_quiz(user, quizzes):
    """{quiz_id: that student's first attempt} for the given quizzes."""
    out = {}
    for a in QuizAttempt.objects.filter(user=user, quiz__in=quizzes).order_by('taken_at', 'id'):
        out.setdefault(a.quiz_id, a)
    return out
