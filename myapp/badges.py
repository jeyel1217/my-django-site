"""Badge rules in ONE place: what each badge means, how to earn it, and how far
along a student is. The badges page and the awarding code both read this list,
so what the page promises is exactly what the system checks."""
from .models import Badge, UserBadge, LessonProgress, QuizAttempt


def _counts(user):
    return {
        'lessons': LessonProgress.objects.filter(user=user, completed=True).count(),
        'quizzes': QuizAttempt.objects.filter(user=user).count(),
        'perfect': QuizAttempt.objects.filter(user=user, score=100).count(),
    }


# name, icon, meaning, how to earn, counter used, target
RULES = [
    {'name': 'First Steps', 'icon': 'fa-solid fa-shoe-prints', 'counter': 'lessons', 'target': 1,
     'meaning': 'You have started your learning journey.',
     'how': 'Finish 1 lesson. Open a lesson, read it to the end, and press "Mark as complete".'},
    {'name': 'Quiz Taker', 'icon': 'fa-solid fa-pen-to-square', 'counter': 'quizzes', 'target': 1,
     'meaning': 'You have tried your first quiz.',
     'how': 'Submit any 1 quiz. It does not matter what score you get.'},
    {'name': 'Perfectionist', 'icon': 'fa-solid fa-bullseye', 'counter': 'perfect', 'target': 1,
     'meaning': 'You answered every question on a quiz correctly.',
     'how': 'Score 100% on any 1 quiz.'},
    {'name': 'Dedicated Learner', 'icon': 'fa-solid fa-graduation-cap', 'counter': 'lessons', 'target': 5,
     'meaning': 'You keep learning and finishing what you start.',
     'how': 'Finish 5 lessons. Each lesson counts once, when you press "Mark as complete".'},
]
BY_NAME = {r['name']: r for r in RULES}
DEFAULT_ICON = 'fa-solid fa-medal'


def ensure_defined():
    """Make sure every built-in badge exists, so locked ones are visible too.
    Text an Admin edited in the Django admin is never overwritten."""
    for r in RULES:
        badge, created = Badge.objects.get_or_create(
            name=r['name'], defaults={'description': r['meaning'], 'icon_class': r['icon']})
        changed = False
        if not badge.description:
            badge.description = r['meaning']; changed = True
        if not badge.icon_class or badge.icon_class == DEFAULT_ICON:
            badge.icon_class = r['icon']; changed = True
        if changed:
            badge.save()


def award(user):
    """Give the user every badge whose rule is now met."""
    counts = _counts(user)
    ensure_defined()
    for r in RULES:
        if counts[r['counter']] >= r['target']:
            badge = Badge.objects.get(name=r['name'])
            UserBadge.objects.get_or_create(user=user, badge=badge)


def cards(user):
    """Everything the badges page needs, one dict per badge."""
    ensure_defined()
    counts = _counts(user)
    earned = {ub.badge_id: ub.earned_at for ub in UserBadge.objects.filter(user=user)}
    out = []
    for b in Badge.objects.all().order_by('id'):
        rule = BY_NAME.get(b.name)
        card = {'badge': b, 'earned_at': earned.get(b.id), 'is_earned': b.id in earned,
                'meaning': b.description or (rule['meaning'] if rule else ''),
                'how': rule['how'] if rule else 'This badge is given by your mentor or an administrator.',
                'target': None, 'current': None, 'pct': None, 'remaining': None}
        if rule:
            current = min(counts[rule['counter']], rule['target'])
            card.update(target=rule['target'], current=current, pct=round(current * 100 / rule['target']),
                        remaining=rule['target'] - current)
        out.append(card)
    return out
