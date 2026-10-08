"""Mentor/Admin student search and academic progress.

Everything here is computed from the database on every request. A mentor only
ever gets rows for classrooms they own (an Admin: every classroom), and a
student only appears under a classroom they are (or were) enrolled in.
"""
from django.core.paginator import Paginator
from django.db.models import Q
from django.utils import timezone

from . import exams
from .models import (
    Profile, Classroom, Enrollment, Lesson, LessonProgress, Quiz, QuizAttempt, QuizAccommodation,
)

PAGE_SIZE = 10

# --- grading status ----------------------------------------------------------
GRADED = 'finalized'
PENDING = 'pending'
GRADING_LABELS = {
    GRADED: ('✓ Finalized', 'bg-emerald-50 text-emerald-700 border-emerald-200'),
    PENDING: ('○ Pending grading', 'bg-amber-50 text-amber-700 border-amber-200'),
    None: ('— Not graded', 'bg-slate-100 text-slate-500 border-slate-200'),
}


def attempt_grading_status(attempt):
    """Finalized unless the attempt still waits for a person to grade it.
    (All current attempts are multiple choice, which is graded on submit.)"""
    return getattr(attempt, 'grading_status', None) or GRADED


def quiz_type_label(quiz):
    return 'Multiple Choice'


# --- who may be looked at ----------------------------------------------------
def scope_classrooms(user):
    """Classrooms this staff member may look into."""
    role = Profile.objects.filter(user=user).values_list('role', flat=True).first()
    if role == Profile.ROLE_ADMIN:
        return Classroom.objects.all()
    if role == Profile.ROLE_MENTOR:
        return Classroom.objects.filter(mentor=user)
    return Classroom.objects.none()


def get_enrollment_or_none(user, classroom_id, student_id):
    """The enrollment row, only if the classroom is in this person's scope."""
    return (Enrollment.objects
            .filter(classroom__in=scope_classrooms(user), classroom_id=classroom_id, student_id=student_id)
            .select_related('classroom__mentor__profile', 'student__profile').first())


# --- search ------------------------------------------------------------------
def search_enrollments(user, query='', classroom_id=None, include_removed=False):
    qs = (Enrollment.objects.filter(classroom__in=scope_classrooms(user))
          .select_related('classroom', 'student__profile'))
    if not include_removed:
        qs = qs.filter(is_active=True)
    if classroom_id:
        qs = qs.filter(classroom_id=classroom_id)
    for token in (query or '').split():
        qs = qs.filter(
            Q(student__profile__name__icontains=token) | Q(student__first_name__icontains=token)
            | Q(student__last_name__icontains=token) | Q(student__username__icontains=token)
            | Q(student__profile__student_id__icontains=token) | Q(student__email__icontains=token)
        )
    return qs.order_by('student__profile__name', 'student__username', 'classroom__name')


def students_page(user, query, classroom_id, page):
    """One page of student rows, each with a small progress summary."""
    paginator = Paginator(search_enrollments(user, query, classroom_id), PAGE_SIZE)
    page_obj = paginator.get_page(page)
    rows = []
    for e in page_obj.object_list:
        data = summary(e.student, e.classroom)
        rows.append({'enrollment': e, 'student': e.student, 'classroom': e.classroom, **data})
    return page_obj, rows


# --- progress ---------------------------------------------------------------
def _quizzes(classroom):
    return list(Quiz.objects.filter(classroom=classroom, is_published=True)
                .select_related('lesson').order_by('created_at', 'id'))


def summary(student, classroom, now=None):
    """The headline numbers for one student in one classroom."""
    now = now or timezone.now()
    lessons_total = Lesson.objects.filter(classroom=classroom, is_published=True).count()
    lessons_done = LessonProgress.objects.filter(
        user=student, completed=True, lesson__classroom=classroom, lesson__is_published=True).count()
    quizzes = _quizzes(classroom)
    firsts = exams.first_attempts_by_quiz(student, quizzes)
    accs = {a.quiz_id: a for a in QuizAccommodation.objects.filter(student=student, quiz__in=quizzes)}
    overdue = 0
    for q in quizzes:
        if q.id in firsts:
            continue
        st = exams.student_quiz_state(q, None, now, accs.get(q.id))
        if st['code'] == 'overdue':
            overdue += 1
    submitted = sum(1 for q in quizzes if q.id in firsts)
    return {'lessons_total': lessons_total, 'lessons_done': lessons_done,
            'quizzes_total': len(quizzes), 'quizzes_submitted': submitted,
            'quizzes_missing': len(quizzes) - submitted, 'overdue': overdue}


def _pct(earned, maximum):
    return round(earned * 100 / maximum) if maximum else None


def full_report(student, classroom, now=None):
    """Everything the progress page and the printable report show."""
    now = now or timezone.now()
    base = summary(student, classroom, now)
    quizzes = _quizzes(classroom)
    attempts = {}
    for a in QuizAttempt.objects.filter(user=student, quiz__in=quizzes).order_by('taken_at', 'id'):
        attempts.setdefault(a.quiz_id, []).append(a)
    accs = {a.quiz_id: a for a in QuizAccommodation.objects.filter(student=student, quiz__in=quizzes)}

    rows, finalized_pcts, chart_src = [], [], []
    for q in quizzes:
        mine = attempts.get(q.id, [])
        first = mine[0] if mine else None
        state = exams.student_quiz_state(q, first, now, accs.get(q.id))
        row = {'quiz': q, 'type': quiz_type_label(q), 'assigned': q.created_at, 'due': state['deadline'],
               'extended': state['extended'], 'state': state, 'attempt_count': len(mine),
               'first': first, 'submitted_at': first.taken_at if first else None,
               'best': None, 'earned': None, 'maximum': None, 'percentage': None,
               'grading': None, 'grading_label': GRADING_LABELS[None]}
        if mine:
            best = max(mine, key=lambda a: (a.score, -a.id))
            earned = best.correct_count
            maximum = best.total_questions
            row.update(best=best, earned=earned, maximum=maximum,
                       grading=attempt_grading_status(best))
            row['grading_label'] = GRADING_LABELS[row['grading']]
            if row['grading'] == GRADED:
                row['percentage'] = best.score          # finalized % exactly as recorded
                finalized_pcts.append(best.score)
                chart_src.append((first.taken_at, q.title, best.score))
        rows.append(row)

    chart_src.sort(key=lambda t: t[0])
    return {
        **base,
        'student': student, 'classroom': classroom, 'rows': rows, 'now': now,
        'average': round(sum(finalized_pcts) / len(finalized_pcts)) if finalized_pcts else None,
        'finalized_count': len(finalized_pcts),
        'pending_count': sum(1 for r in rows if r['grading'] == PENDING),
        'chart': chart_points(chart_src),
        'lessons_pct': _pct(base['lessons_done'], base['lessons_total']),
        'quizzes_pct': _pct(base['quizzes_submitted'], base['quizzes_total']),
        'outstanding': [r for r in rows if r['first'] is None and r['state']['code'] != 'exempt'],
    }


MIN_CHART_POINTS = 3


def chart_points(src):
    """Coordinates for the server-drawn SVG line chart (0-100% on the y axis).
    With fewer than MIN_CHART_POINTS finalized quizzes a trend line would
    mislead, so only the list of points is returned (`enough` is False)."""
    n = len(src)
    W, H, L, R, T, B = 640, 280, 46, 100, 16, 64
    X0 = L + 34                                   # keep the first point clear of the axis numbers
    pts = []
    for i, (when, title, pct) in enumerate(src):
        x = X0 + ((W - X0 - R) / 2 if n == 1 else i * (W - X0 - R) / (n - 1))
        y = T + (H - T - B) * (1 - pct / 100)
        pts.append({'x': round(x, 1), 'y': round(y, 1), 'pct': pct, 'title': title,
                    'label': title if len(title) <= 14 else title[:13] + '…', 'when': when})
    return {'enough': n >= MIN_CHART_POINTS, 'points': pts, 'path': ' '.join(f"{p['x']},{p['y']}" for p in pts),
            'W': W, 'H': H, 'L': L, 'R': R, 'T': T, 'B': B, 'count': n,
            'grid': [{'v': v, 'y': round(T + (H - T - B) * (1 - v / 100), 1)} for v in (0, 25, 50, 75, 100)]}
