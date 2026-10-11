"""Quiz types, grading and validation: the rules live here and nowhere else.

Question types
  MC     multiple choice. One correct choice, or several ("choose all that apply").
         Several correct = all or nothing: the student must tick exactly the
         correct choices to earn the points.
  FILL   fill in the blank. The question text holds one or more "[blank]"
         markers. Every blank has one or more accepted answers. Points are split
         equally between the blanks and earned per blank.
  ESSAY  written answer. Never graded automatically: it waits for the Mentor.

Everything the student can influence is re-checked on the server; nothing here
trusts the browser.
"""
import re
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .models import (
    Quiz, Question, Choice, BlankAnswer, QuizAttempt, AttemptAnswer, Notification,
)

MAX_ESSAY_CHARS = 10000
MAX_BLANKS = 10
MAX_CHOICES = 8
MIN_CHOICES = 2
MAX_POINTS = 100

TWO = Decimal('0.01')


def _d(value):
    return Decimal(str(value)).quantize(TWO, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Comparing fill-in-the-blank answers
# ---------------------------------------------------------------------------
def normalize(text, case_sensitive=False):
    """Trim, collapse runs of spaces/tabs, and (unless the Mentor asked for exact
    case) ignore capital letters. 'Public   Static ' equals 'public static'."""
    text = re.sub(r'\s+', ' ', (text or '').strip())
    return text if case_sensitive else text.casefold()


# ---------------------------------------------------------------------------
# Snapshot of a question (frozen into every answer)
# ---------------------------------------------------------------------------
def snapshot_question(q):
    snap = {'allow_multiple': q.allow_multiple, 'case_sensitive': q.case_sensitive, 'guide': q.guide}
    if q.is_mc:
        snap['choices'] = [{'id': c.id, 'text': c.text, 'correct': c.is_correct} for c in q.choices.all().order_by('id')]
    elif q.is_fill:
        blanks = {}
        for b in q.blank_answers.all().order_by('blank_number', 'id'):
            blanks.setdefault(str(b.blank_number), []).append(b.text)
        snap['blanks'] = blanks
        snap['blank_count'] = q.blank_count
    return snap


# ---------------------------------------------------------------------------
# Reading + grading one answer from a submitted form
# ---------------------------------------------------------------------------
def read_answer(q, post):
    """Returns (answer_dict, answered, points, is_correct, needs_grading).
    points is None while an essay waits for the Mentor."""
    maxp = _d(q.points)

    if q.is_mc:
        valid = {c.id: c.is_correct for c in q.choices.all()}
        picked = []
        for raw in post.getlist(f'question_{q.id}'):
            try:
                cid = int(raw)
            except (TypeError, ValueError):
                continue
            if cid in valid and cid not in picked:        # ignore ids from other questions
                picked.append(cid)
        if not q.allow_multiple:
            picked = picked[:1]
        right = {cid for cid, ok in valid.items() if ok}
        correct = bool(picked) and set(picked) == right
        return {'choices': picked}, bool(picked), (maxp if correct else _d(0)), correct, False

    if q.is_fill:
        n = q.blank_count
        accepted = {}
        for b in q.blank_answers.all():
            accepted.setdefault(b.blank_number, []).append(normalize(b.text, q.case_sensitive))
        given, right = [], 0
        for i in range(1, n + 1):
            value = (post.get(f'question_{q.id}_b{i}') or '').strip()[:200]
            given.append(value)
            if value and normalize(value, q.case_sensitive) in accepted.get(i, []):
                right += 1
        answered = any(given)
        points = _d(maxp * right / n) if n else _d(0)
        return {'blanks': given}, answered, points, (n > 0 and right == n), False

    # essay
    text = (post.get(f'question_{q.id}') or '').strip()[:MAX_ESSAY_CHARS]
    if not text:
        return {'text': ''}, False, _d(0), False, False        # nothing written: 0, nothing to mark
    return {'text': text}, True, None, None, True


# ---------------------------------------------------------------------------
# Submitting an attempt
# ---------------------------------------------------------------------------
def percent(earned, maximum):
    maximum = Decimal(maximum or 0)
    if maximum <= 0:
        return 0
    return int((Decimal(earned or 0) * 100 / maximum).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


@transaction.atomic
def submit_attempt(user, quiz, post, started_at, deadline_at):
    """Grade what can be graded, store every answer, return the attempt."""
    questions = list(quiz.questions.prefetch_related('choices', 'blank_answers').order_by('id'))
    attempt = QuizAttempt.objects.create(
        user=user, quiz=quiz, score=0, started_at=started_at, deadline_at_submission=deadline_at,
        total_questions=len(questions), quiz_version=quiz.version,
        grading_status=QuizAttempt.GRADING_FINAL,
    )
    for pos, q in enumerate(questions, start=1):
        answer, answered, points, correct, needs = read_answer(q, post)
        AttemptAnswer.objects.create(
            attempt=attempt, question=q, position=pos, qtype=q.qtype, question_text=q.text,
            max_points=_d(q.points), snapshot=snapshot_question(q), answer=answer, answered=answered,
            points_awarded=points, is_correct=correct, needs_grading=needs,
        )
    recompute(attempt)
    return attempt


def recompute(attempt):
    """Totals and status from the stored answers. Pending while any essay is unmarked."""
    rows = list(attempt.answers.all())
    maximum = sum((r.max_points for r in rows), Decimal('0'))
    earned = sum((r.points_awarded or Decimal('0') for r in rows), Decimal('0'))
    pending = any(r.needs_grading for r in rows)
    attempt.max_points = maximum
    attempt.earned_points = earned
    attempt.correct_count = sum(1 for r in rows if r.is_correct)
    attempt.total_questions = len(rows)
    if pending:
        attempt.grading_status = QuizAttempt.GRADING_PENDING
        attempt.score = 0                      # never shown or counted while pending
    else:
        attempt.grading_status = QuizAttempt.GRADING_FINAL
        attempt.score = percent(earned, maximum)
    attempt.save(update_fields=['max_points', 'earned_points', 'correct_count', 'total_questions',
                                'grading_status', 'score'])
    return attempt


# ---------------------------------------------------------------------------
# Essay grading by the Mentor
# ---------------------------------------------------------------------------
class GradeError(ValueError):
    pass


@transaction.atomic
def grade_essays(attempt, grader, entries):
    """entries: {answer_id: (points_text, feedback)}. Blank points leave that essay pending.
    Raises GradeError (nothing saved) if any mark is not between 0 and the question's points."""
    answers = {a.id: a for a in attempt.answers.select_for_update().filter(qtype=Question.TYPE_ESSAY, answered=True)}
    staged = []
    for aid, (raw_points, feedback) in entries.items():
        a = answers.get(aid)
        if a is None:
            continue
        raw = (raw_points or '').strip()
        points = None
        if raw != '':
            try:
                points = _d(raw)
            except Exception:
                raise GradeError(f'Question {a.position}: "{raw}" is not a number.')
            if points < 0 or points > a.max_points:
                raise GradeError(f'Question {a.position}: marks must be between 0 and {a.max_points}.')
        staged.append((a, points, (feedback or '').strip()[:2000]))
    for a, points, feedback in staged:
        a.feedback = feedback
        if points is not None:
            a.points_awarded = points
            a.is_correct = points >= a.max_points
            a.needs_grading = False
        else:
            a.points_awarded = None
            a.is_correct = None
            a.needs_grading = True
        a.save(update_fields=['feedback', 'points_awarded', 'is_correct', 'needs_grading'])

    was_pending = attempt.is_pending
    recompute(attempt)
    if not attempt.is_pending:
        attempt.graded_at = timezone.now()
        attempt.graded_by = grader
        attempt.save(update_fields=['graded_at', 'graded_by'])
        if was_pending:
            Notification.objects.get_or_create(
                dedupe_key=f'graded-{attempt.pk}',
                defaults=dict(
                    user=attempt.user, kind=Notification.KIND_GRADED, quiz=attempt.quiz,
                    subject=f'Your quiz "{attempt.quiz.title}" has been graded',
                    message=f'Your mentor finished grading "{attempt.quiz.title}". Your score is {attempt.score}%.',
                    email_status=Notification.EMAIL_NOT_SENT,
                ))
    return attempt


# ---------------------------------------------------------------------------
# Mentor: saving questions
# ---------------------------------------------------------------------------
def bump_version(quiz):
    Quiz.objects.filter(pk=quiz.pk).update(version=F('version') + 1)
    quiz.refresh_from_db(fields=['version'])


@transaction.atomic
def save_question(quiz, data, instance=None):
    """data comes from QuestionEditForm.cleaned_data. Creates or replaces a question's content."""
    q = instance or Question(quiz=quiz)
    q.text = data['text']
    q.qtype = data['qtype']
    q.points = data['points']
    q.allow_multiple = data['qtype'] == Question.TYPE_MC and data['allow_multiple']
    q.case_sensitive = data['qtype'] == Question.TYPE_FILL and data['case_sensitive']
    q.guide = data['guide'] if data['qtype'] == Question.TYPE_ESSAY else ''
    q.save()
    q.choices.all().delete()
    q.blank_answers.all().delete()
    if q.is_mc:
        for text, ok in data['choices']:
            Choice.objects.create(question=q, text=text, is_correct=ok)
    elif q.is_fill:
        for number, answers in data['blanks'].items():
            for text in answers:
                BlankAnswer.objects.create(question=q, blank_number=number, text=text)
    bump_version(quiz)
    return q


# ---------------------------------------------------------------------------
# Is this quiz ready to publish?
# ---------------------------------------------------------------------------
def question_problems(q):
    out = []
    if not (q.text or '').strip():
        out.append('has no question text')
    if not 1 <= (q.points or 0) <= MAX_POINTS:
        out.append(f'needs points between 1 and {MAX_POINTS}')
    if q.is_mc:
        choices = list(q.choices.all())
        right = sum(1 for c in choices if c.is_correct)
        if len(choices) < MIN_CHOICES:
            out.append('needs at least 2 choices')
        if right == 0:
            out.append('has no correct choice')
        if right > 1 and not q.allow_multiple:
            out.append('has several correct choices but is not set to "choose all that apply"')
    elif q.is_fill:
        n = q.blank_count
        if n == 0:
            out.append('has no [blank] in the text')
        have = {b.blank_number for b in q.blank_answers.all()}
        for i in range(1, n + 1):
            if i not in have:
                out.append(f'blank {i} has no accepted answer')
    return out


def quiz_problems(quiz):
    """List of readable problems; empty means it can be published."""
    qs = list(quiz.questions.prefetch_related('choices', 'blank_answers').order_by('id'))
    if not qs:
        return ['The quiz has no questions yet.']
    out = []
    for n, q in enumerate(qs, start=1):
        for p in question_problems(q):
            out.append(f'Question {n} {p}.')
    if sum(q.points for q in qs) <= 0:
        out.append('The total points must be more than 0.')
    return out


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------
TYPE_NAMES = {Question.TYPE_MC: 'Multiple Choice', Question.TYPE_ESSAY: 'Essay', Question.TYPE_FILL: 'Fill in the Blank'}


def quiz_type_label(quiz):
    kinds = []
    for t in quiz.questions.values_list('qtype', flat=True).distinct():
        kinds.append(t)
    order = [Question.TYPE_MC, Question.TYPE_FILL, Question.TYPE_ESSAY]
    kinds = [k for k in order if k in kinds]
    if not kinds:
        return 'Multiple Choice'
    if len(kinds) == 1:
        return TYPE_NAMES[kinds[0]]
    return 'Mixed (' + ', '.join(TYPE_NAMES[k] for k in kinds) + ')'
