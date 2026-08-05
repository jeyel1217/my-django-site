from django.db import models
from django.contrib.auth.models import User
from django.db.models import Avg


# ---------------------------------------------------------
# PROFILE
# ---------------------------------------------------------
class Profile(models.Model):
    """Extends the built-in User with extra fields + computed stats."""
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    bio = models.TextField(blank=True, default='')
    avatar = models.ImageField(upload_to='avatars/', blank=True, null=True)
    streak_days = models.PositiveIntegerField(default=0)

    def __str__(self):
        return self.user.username

    # --- Computed stats used on the dashboard ---
    @property
    def total_lessons(self):
        return Lesson.objects.count()

    @property
    def lessons_completed(self):
        return LessonProgress.objects.filter(user=self.user, completed=True).count()

    @property
    def overall_progress(self):
        total = self.total_lessons
        if total == 0:
            return 0
        return round((self.lessons_completed / total) * 100)

    @property
    def badges_earned(self):
        return UserBadge.objects.filter(user=self.user).count()

    @property
    def quiz_score_avg(self):
        avg = QuizAttempt.objects.filter(user=self.user).aggregate(avg=Avg('score'))['avg']
        return round(avg) if avg else 0


# ---------------------------------------------------------
# LESSONS
# ---------------------------------------------------------
class Lesson(models.Model):
    title = models.CharField(max_length=200)
    slug = models.SlugField(unique=True)
    order = models.PositiveIntegerField(default=0)
    content = models.TextField(help_text="Lesson body. Supports plain text/paragraphs.")

    class Meta:
        ordering = ['order']

    def __str__(self):
        return f"{self.order}. {self.title}"


class LessonProgress(models.Model):
    """Tracks whether a specific user has completed a specific lesson."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='lesson_progress')
    lesson = models.ForeignKey(Lesson, on_delete=models.CASCADE, related_name='progress_entries')
    completed = models.BooleanField(default=False)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ('user', 'lesson')

    def __str__(self):
        status = "done" if self.completed else "in progress"
        return f"{self.user.username} - {self.lesson.title} ({status})"


# ---------------------------------------------------------
# QUIZZES
# ---------------------------------------------------------
class Quiz(models.Model):
    lesson = models.ForeignKey(Lesson, on_delete=models.CASCADE, related_name='quizzes', null=True, blank=True)
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True, default='')

    def __str__(self):
        return self.title

    @property
    def total_questions(self):
        return self.questions.count()


class Question(models.Model):
    quiz = models.ForeignKey(Quiz, on_delete=models.CASCADE, related_name='questions')
    text = models.CharField(max_length=500)

    def __str__(self):
        return self.text[:60]


class Choice(models.Model):
    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name='choices')
    text = models.CharField(max_length=300)
    is_correct = models.BooleanField(default=False)

    def __str__(self):
        return self.text


class QuizAttempt(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='quiz_attempts')
    quiz = models.ForeignKey(Quiz, on_delete=models.CASCADE, related_name='attempts')
    score = models.PositiveIntegerField(help_text="Percentage score 0-100")
    taken_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.username} - {self.quiz.title}: {self.score}%"


# ---------------------------------------------------------
# BADGES
# ---------------------------------------------------------
class Badge(models.Model):
    name = models.CharField(max_length=100)
    description = models.CharField(max_length=255, blank=True, default='')
    icon_class = models.CharField(
        max_length=100, default='fa-solid fa-medal',
        help_text="FontAwesome icon class, e.g. 'fa-solid fa-medal'"
    )

    def __str__(self):
        return self.name


class UserBadge(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='badges')
    badge = models.ForeignKey(Badge, on_delete=models.CASCADE, related_name='holders')
    earned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'badge')

    def __str__(self):
        return f"{self.user.username} earned {self.badge.name}"