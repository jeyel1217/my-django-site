"""Classroom access rules in one place, so every page asks the same question:
"which classrooms may THIS person see?" The answer comes from the database on
every request. Hiding a button is never the only protection."""
from django.core.cache import cache
from django.db.models import Q

from .models import Profile, Classroom, Lesson, Quiz

JOIN_MAX_FAILURES = 8          # wrong codes allowed ...
JOIN_WINDOW_SECONDS = 10 * 60  # ... per 10 minutes


def _role(user):
    p = Profile.objects.filter(user=user).only('role').first()
    return p.role if p else None


def viewable_classrooms(user):
    """Student -> classrooms they joined. Mentor -> their own. Admin -> all."""
    role = _role(user)
    if role == Profile.ROLE_ADMIN:
        return Classroom.objects.all()
    if role == Profile.ROLE_MENTOR:
        return Classroom.objects.filter(mentor=user)
    return Classroom.objects.filter(enrollments__student=user, enrollments__is_active=True)


def visible_lessons(user):
    return Lesson.objects.filter(is_published=True, classroom__in=viewable_classrooms(user))


def visible_quizzes(user):
    return Quiz.objects.filter(is_published=True, classroom__in=viewable_classrooms(user))


def ensure_default_classroom(user):
    """Every Mentor always has at least one classroom to put lessons in."""
    if Classroom.objects.filter(mentor=user).exists():
        return
    prof = Profile.objects.filter(user=user).first()
    label = (prof.display_name if prof else user.username)
    Classroom.objects.create(name=f"{label}'s Classroom", mentor=user)


# --- slow down code guessing -------------------------------------------------
def _key(user):
    return f'join_fail_{user.pk}'


def join_is_blocked(user):
    return cache.get(_key(user), 0) >= JOIN_MAX_FAILURES


def join_record_failure(user):
    k = _key(user)
    try:
        cache.add(k, 0, JOIN_WINDOW_SECONDS)
        cache.incr(k)
    except ValueError:
        cache.set(k, 1, JOIN_WINDOW_SECONDS)


def join_clear_failures(user):
    cache.delete(_key(user))
