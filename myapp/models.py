from django.db import models
from django.contrib.auth.models import User
from django.db.models import Avg


# ---------------------------------------------------------
# PROFILE
# ---------------------------------------------------------
class Profile(models.Model):
    """Extends the built-in User with role, extra fields, and computed stats."""

    ROLE_ADMIN = 'ADMIN'
    ROLE_MENTOR = 'MENTOR'
    ROLE_STUDENT = 'STUDENT'
    ROLE_CHOICES = [
        (ROLE_ADMIN, 'Admin'),
        (ROLE_MENTOR, 'Mentor'),
        (ROLE_STUDENT, 'Student'),
    ]

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default=ROLE_STUDENT)

    bio = models.TextField(blank=True, default='')
    avatar = models.ImageField(upload_to='avatars/', blank=True, null=True)
    streak_days = models.PositiveIntegerField(default=0)
    last_active_date = models.DateField(null=True, blank=True)

    # Defaults to True so EXISTING accounts (created before this feature)
    # aren't locked out. New self-registrations explicitly set this False
    # in register_view, then must verify via OTP.
    is_verified = models.BooleanField(default=True)

    # Optional display/real name. Shown in welcome messages instead of the
    # username when it is filled in.
    name = models.CharField(max_length=50, blank=True, default='')

    # Login protection: 3 wrong passwords in a row lock the account until the
    # password is reset through the emailed verification code.
    failed_login_attempts = models.PositiveIntegerField(default=0)
    is_locked = models.BooleanField(default=False)
    locked_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.user.username} ({self.role})"

    @property
    def display_name(self):
        """Name takes priority; falls back to the username."""
        name = (self.name or '').strip()
        return name if name else self.user.username

    # --- Role helpers ---
    @property
    def is_admin(self):
        return self.role == self.ROLE_ADMIN

    @property
    def is_mentor(self):
        return self.role == self.ROLE_MENTOR

    @property
    def is_student(self):
        return self.role == self.ROLE_STUDENT

    # --- Streak tracking ---
    def update_streak(self):
        """Call this once per login/activity. Increments streak if the user was
        active yesterday, resets to 1 if they missed a day, no-ops if
        already counted today."""
        from django.utils import timezone
        from datetime import timedelta

        today = timezone.localdate()
        if self.last_active_date == today:
            return
        elif self.last_active_date == today - timedelta(days=1):
            self.streak_days += 1
        else:
            self.streak_days = 1
        self.last_active_date = today
        self.save(update_fields=['streak_days', 'last_active_date'])

    # --- Computed stats used on the student dashboard ---
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
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='created_lessons',
        help_text="The Mentor who created this lesson."
    )
    is_published = models.BooleanField(
        default=False,
        help_text="Unpublished (draft) lessons are hidden from Students."
    )

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
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='created_quizzes',
        help_text="The Mentor who created this quiz."
    )
    is_published = models.BooleanField(
        default=False,
        help_text="Unpublished (draft) quizzes are hidden from Students."
    )

    # Shown to students and printed on the paper copy.
    instructions = models.TextField(blank=True, default='')

    # Schedule. Both are optional: no start = open immediately, no deadline =
    # nothing to be "late" for. Late / on-time is always CALCULATED from these
    # and the attempt's submission time, never typed in by hand.
    start_datetime = models.DateTimeField(null=True, blank=True)
    deadline_datetime = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, null=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True, null=True, editable=False)

    def __str__(self):
        return self.title

    @property
    def total_questions(self):
        return self.questions.count()

    @property
    def has_deadline(self):
        return self.deadline_datetime is not None

    def is_not_open_yet(self, now=None):
        from django.utils import timezone
        now = now or timezone.now()
        return self.start_datetime is not None and now < self.start_datetime

    def is_past_deadline(self, now=None):
        from django.utils import timezone
        now = now or timezone.now()
        return self.deadline_datetime is not None and now > self.deadline_datetime


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
    # taken_at is stamped automatically the moment the student submits, so it
    # IS the submission time (see submitted_at below).
    taken_at = models.DateTimeField(auto_now_add=True)

    # When the student first opened the quiz (recorded in the session, saved on submit).
    started_at = models.DateTimeField(null=True, blank=True)
    # Raw result, kept so it stays correct even if the Mentor later edits the quiz.
    # Empty for attempts made before this feature existed.
    correct_count = models.PositiveIntegerField(null=True, blank=True)
    total_questions = models.PositiveIntegerField(null=True, blank=True)

    def __str__(self):
        return f"{self.user.username} - {self.quiz.title}: {self.score}%"

    @property
    def submitted_at(self):
        return self.taken_at

    @property
    def percentage(self):
        return self.score

    @property
    def is_late(self):
        """True only when the quiz has a deadline AND this was submitted after it."""
        deadline = self.quiz.deadline_datetime
        return deadline is not None and self.taken_at > deadline


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


# ---------------------------------------------------------
# OTP VERIFICATION (email verification + password reset)
# ---------------------------------------------------------
class OTPCode(models.Model):
    """A single-use, expiring one-time code. The code itself is stored
    hashed (SHA-256), never in plain text, per the security spec (§13)."""

    PURPOSE_VERIFY_EMAIL = 'VERIFY_EMAIL'
    PURPOSE_RESET_PASSWORD = 'RESET_PASSWORD'
    PURPOSE_CHOICES = [
        (PURPOSE_VERIFY_EMAIL, 'Verify Email'),
        (PURPOSE_RESET_PASSWORD, 'Reset Password'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='otp_codes')
    purpose = models.CharField(max_length=20, choices=PURPOSE_CHOICES)
    code_hash = models.CharField(max_length=64)  # sha256 hex digest
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    is_used = models.BooleanField(default=False)
    attempts = models.PositiveIntegerField(default=0)  # wrong-code attempts

    MAX_ATTEMPTS = 5

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.user.username} - {self.purpose} ({'used' if self.is_used else 'active'})"

    def is_expired(self):
        from django.utils import timezone
        return timezone.now() > self.expires_at

    def is_valid(self):
        return (not self.is_used) and (not self.is_expired()) and (self.attempts < self.MAX_ATTEMPTS)
