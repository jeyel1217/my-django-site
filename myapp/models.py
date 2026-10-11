from django.db import models
from .storage import get_private_storage
from django.contrib.auth.models import User
from django.db.models import Avg
import secrets


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

    # School-issued student number. Optional; students enter it themselves and
    # mentors can search by it. Not used for login.
    student_id = models.CharField(max_length=30, blank=True, default='', db_index=True)

    # Login protection: 3 wrong passwords in a row lock the account until the
    # password is reset through the emailed verification code.
    failed_login_attempts = models.PositiveIntegerField(default=0)
    is_locked = models.BooleanField(default=False)
    locked_at = models.DateTimeField(null=True, blank=True)

    # Profile-photo review (Terms rule 2). The system never judges photos
    # automatically: an Admin reviews them and can flag one with a reason, and
    # the student is asked to replace it. Uploading a new photo clears the flag.
    avatar_flagged = models.BooleanField(default=False)
    avatar_flag_reason = models.CharField(max_length=200, blank=True, default='')
    avatar_flagged_at = models.DateTimeField(null=True, blank=True)

    # Account status under the warning policy. INACTIVE is different from
    # Django's is_active: the student can still log in to read why and what to
    # do next, but every student feature is blocked until an Admin reactivates.
    ACCOUNT_ACTIVE = 'ACTIVE'
    ACCOUNT_INACTIVE = 'INACTIVE'
    account_status = models.CharField(
        max_length=10, default='ACTIVE', choices=[('ACTIVE', 'Active'), ('INACTIVE', 'Inactive')])
    deactivated_at = models.DateTimeField(null=True, blank=True)
    deactivation_reason = models.CharField(max_length=300, blank=True, default='')

    def __str__(self):
        return f"{self.user.username} ({self.role})"

    # --- Profile completion (Terms rule 1) ---
    # The fields a STUDENT must fill in. Everything else (bio) stays optional.
    REQUIRED_PROFILE_FIELDS = (
        ('name', 'Full name'),
        ('avatar', 'Profile photo'),
    )

    @property
    def missing_profile_fields(self):
        """Labels of required fields that are still empty. A photo that an
        Admin flagged counts as missing until it is replaced."""
        missing = []
        if not (self.name or '').strip():
            missing.append('Full name')
        if not self.avatar or self.avatar_flagged:
            missing.append('Profile photo')
        return missing

    @property
    def is_profile_complete(self):
        return not self.missing_profile_fields

    @property
    def profile_completion_percent(self):
        total = len(self.REQUIRED_PROFILE_FIELDS)
        return round((total - len(self.missing_profile_fields)) / total * 100)

    @property
    def display_name(self):
        """Name takes priority; falls back to the username."""
        name = (self.name or '').strip()
        return name if name else self.user.username

    # --- Role helpers ---
    @property
    def is_policy_inactive(self):
        return self.account_status == self.ACCOUNT_INACTIVE

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
    def visible_lessons(self):
        """Published lessons this person may see. A STUDENT only gets lessons from
        classrooms they have joined; Mentors/Admins keep the broader view."""
        qs = Lesson.objects.filter(is_published=True)
        if self.role == self.ROLE_STUDENT:
            qs = qs.filter(classroom__enrollments__student=self.user,
                           classroom__enrollments__is_active=True)
        return qs

    @property
    def total_lessons(self):
        return self.visible_lessons().count()

    @property
    def lessons_completed(self):
        return LessonProgress.objects.filter(
            user=self.user, completed=True, lesson__in=self.visible_lessons()
        ).count()

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
        avg = QuizAttempt.objects.filter(user=self.user, grading_status='finalized').aggregate(avg=Avg('score'))['avg']
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
    # Which classroom this lesson belongs to. Only students enrolled in that
    # classroom can see it. (Null = not assigned to any classroom yet: hidden.)
    classroom = models.ForeignKey(
        'Classroom', on_delete=models.SET_NULL, null=True, blank=True, related_name='lessons',
    )

    class Meta:
        ordering = ['order']

    def __str__(self):
        return f"{self.order}. {self.title}"


class LessonProgress(models.Model):
    """Tracks one student's progress through one lesson."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='lesson_progress')
    lesson = models.ForeignKey(Lesson, on_delete=models.CASCADE, related_name='progress_entries')
    completed = models.BooleanField(default=False)
    completed_at = models.DateTimeField(null=True, blank=True)

    # Reading position inside the lesson's file (all stay empty for text-only lessons).
    started_at = models.DateTimeField(null=True, blank=True)
    last_opened_at = models.DateTimeField(null=True, blank=True)
    last_page = models.PositiveIntegerField(default=1)    # where the student stopped (for "continue reading")
    max_page = models.PositiveIntegerField(default=0)     # furthest page ever reached

    STATUS_NOT_STARTED = 'not_started'
    STATUS_IN_PROGRESS = 'in_progress'
    STATUS_COMPLETED = 'completed'

    @property
    def status(self):
        if self.completed:
            return self.STATUS_COMPLETED
        if self.started_at or self.max_page > 0:
            return self.STATUS_IN_PROGRESS
        return self.STATUS_NOT_STARTED

    class Meta:
        unique_together = ('user', 'lesson')

    def __str__(self):
        status = "done" if self.completed else "in progress"
        return f"{self.user.username} - {self.lesson.title} ({status})"


def lesson_file_name(instance, filename):
    """Stored name = random hex + the checked extension. The uploader's own file name is
    never used on disk, so it can never contain a path, a script name or a clash."""
    import os, uuid
    ext = os.path.splitext(filename)[1].lower()
    return f"lessons/{uuid.uuid4().hex}{ext}"


class LessonFile(models.Model):
    """The PDF / Word / PowerPoint file attached to a lesson (one per lesson)."""
    KIND_PDF, KIND_DOCX, KIND_PPTX = 'PDF', 'DOCX', 'PPTX'
    KIND_CHOICES = [(KIND_PDF, 'PDF'), (KIND_DOCX, 'Word'), (KIND_PPTX, 'PowerPoint')]

    # Reading copy (always a PDF, shown page by page)
    CONV_READY = 'READY'
    CONV_FAILED = 'FAILED'
    CONV_UNAVAILABLE = 'UNAVAILABLE'     # the server has no converter installed
    CONV_CHOICES = [(CONV_READY, 'Ready'), (CONV_FAILED, 'Conversion failed'), (CONV_UNAVAILABLE, 'Converter not installed')]

    lesson = models.OneToOneField(Lesson, on_delete=models.CASCADE, related_name='file')
    original_name = models.CharField(max_length=120)
    kind = models.CharField(max_length=4, choices=KIND_CHOICES)
    size = models.PositiveIntegerField(default=0)
    original = models.FileField(upload_to=lesson_file_name, storage=get_private_storage, max_length=200)
    reading_pdf = models.FileField(upload_to=lesson_file_name, storage=get_private_storage, max_length=200, blank=True, default='')
    page_count = models.PositiveIntegerField(default=0)
    conversion_status = models.CharField(max_length=12, choices=CONV_CHOICES, default=CONV_READY)
    conversion_message = models.CharField(max_length=300, blank=True, default='')
    uploaded_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.original_name} ({self.kind})"

    @property
    def is_viewable(self):
        """True when there is a PDF to show page by page."""
        return self.conversion_status == self.CONV_READY and self.page_count > 0

    @property
    def reading_file(self):
        return self.original if self.kind == self.KIND_PDF else self.reading_pdf

    @property
    def kind_label(self):
        return dict(self.KIND_CHOICES)[self.kind]


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
    classroom = models.ForeignKey(
        'Classroom', on_delete=models.SET_NULL, null=True, blank=True, related_name='quizzes',
    )
    # After the deadline students may only submit if the Mentor allows it here
    # (or grants that student an extension).
    allow_late_submissions = models.BooleanField(
        default=False,
        help_text="If ticked, students can still submit after the deadline (marked Late).",
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
    # Bumped each time the questions change.
    version = models.PositiveIntegerField(default=1)
    # With a linked lesson: students must finish that lesson before they can open this quiz.
    require_lesson_first = models.BooleanField(
        default=False, help_text="Students must complete the linked lesson before they can open this quiz.")

    def __str__(self):
        return self.title

    @property
    def total_points(self):
        from django.db.models import Sum
        return self.questions.aggregate(t=Sum('points'))['t'] or 0

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
    TYPE_MC = 'MC'
    TYPE_ESSAY = 'ESSAY'
    TYPE_FILL = 'FILL'
    TYPE_CHOICES = [
        (TYPE_MC, 'Multiple choice'),
        (TYPE_ESSAY, 'Essay'),
        (TYPE_FILL, 'Fill in the blank'),
    ]
    BLANK_TOKEN = '[blank]'     # typed by the Mentor inside the question text

    quiz = models.ForeignKey(Quiz, on_delete=models.CASCADE, related_name='questions')
    text = models.CharField(max_length=500)

    # Existing questions are all multiple choice with one correct answer worth 1 point,
    # which is exactly what these defaults describe.
    qtype = models.CharField(max_length=5, choices=TYPE_CHOICES, default=TYPE_MC)
    points = models.PositiveSmallIntegerField(default=1)
    allow_multiple = models.BooleanField(
        default=False, help_text="Multiple choice: more than one choice is correct.")
    case_sensitive = models.BooleanField(
        default=False, help_text="Fill in the blank: 'Java' and 'java' count as different answers.")
    guide = models.TextField(
        blank=True, default='', help_text="Essay: what a good answer contains (shown to the grader only).")
    updated_at = models.DateTimeField(auto_now=True, null=True, editable=False)

    def __str__(self):
        return self.text[:60]

    @property
    def is_mc(self):
        return self.qtype == self.TYPE_MC

    @property
    def is_essay(self):
        return self.qtype == self.TYPE_ESSAY

    @property
    def is_fill(self):
        return self.qtype == self.TYPE_FILL

    @property
    def blank_count(self):
        return self.text.count(self.BLANK_TOKEN)

    def text_parts(self):
        """The question text split around the blanks: ['int x = ', ';']."""
        return self.text.split(self.BLANK_TOKEN)


class Choice(models.Model):
    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name='choices')
    text = models.CharField(max_length=300)
    is_correct = models.BooleanField(default=False)

    def __str__(self):
        return self.text


class BlankAnswer(models.Model):
    """One accepted answer for one blank of a fill-in-the-blank question.
    A blank may have several accepted answers (e.g. 'int' and 'integer')."""
    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name='blank_answers')
    blank_number = models.PositiveSmallIntegerField(default=1)     # 1 = first blank
    text = models.CharField(max_length=200)

    class Meta:
        ordering = ['blank_number', 'id']

    def __str__(self):
        return f"#{self.blank_number}: {self.text}"


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
    # The deadline that applied to THIS student when they submitted (the quiz
    # deadline, or their extension). Kept so history stays correct even if the
    # Mentor edits the quiz later. Empty for older attempts.
    deadline_at_submission = models.DateTimeField(null=True, blank=True)

    # Essays are marked by a person, so an attempt can wait ("pending") with no
    # official score yet. Older attempts and all-automatic quizzes are finalized
    # the moment they are submitted.
    GRADING_FINAL = 'finalized'
    GRADING_PENDING = 'pending'
    grading_status = models.CharField(
        max_length=10, default='finalized',
        choices=[('finalized', 'Finalized'), ('pending', 'Pending grading')], db_index=True)
    earned_points = models.DecimalField(max_digits=7, decimal_places=2, null=True, blank=True)
    max_points = models.DecimalField(max_digits=7, decimal_places=2, null=True, blank=True)
    graded_at = models.DateTimeField(null=True, blank=True)
    graded_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='graded_attempts')
    # Increases whenever the Mentor edits the questions; recorded so you can tell
    # which version of the quiz a student answered.
    quiz_version = models.PositiveIntegerField(default=1)

    def __str__(self):
        return f"{self.user.username} - {self.quiz.title}: {self.score}%"

    @property
    def is_pending(self):
        return self.grading_status == self.GRADING_PENDING

    @property
    def submitted_at(self):
        return self.taken_at

    @property
    def percentage(self):
        return self.score

    @property
    def effective_deadline(self):
        return self.deadline_at_submission or self.quiz.deadline_datetime

    @property
    def is_late(self):
        """True only when a deadline applied AND this was submitted after it."""
        deadline = self.effective_deadline
        return deadline is not None and self.taken_at > deadline


class AttemptAnswer(models.Model):
    """What a student answered to one question, with a frozen copy of the question.

    The snapshot is what keeps a graded attempt correct even if the Mentor later
    edits or deletes the question: the review page and the points never depend on
    the live question."""
    attempt = models.ForeignKey(QuizAttempt, on_delete=models.CASCADE, related_name='answers')
    question = models.ForeignKey(Question, null=True, blank=True, on_delete=models.SET_NULL, related_name='attempt_answers')
    position = models.PositiveSmallIntegerField(default=1)

    # frozen copy of the question
    qtype = models.CharField(max_length=5, default=Question.TYPE_MC)
    question_text = models.CharField(max_length=500, default='')
    max_points = models.DecimalField(max_digits=6, decimal_places=2, default=1)
    snapshot = models.JSONField(default=dict, blank=True)      # choices / accepted answers / guide

    # the student's answer
    answer = models.JSONField(default=dict, blank=True)        # {'choices':[ids]} | {'text':..} | {'blanks':[..]}
    answered = models.BooleanField(default=False)

    # result
    points_awarded = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    is_correct = models.BooleanField(null=True, blank=True)    # None = not decided yet (essay)
    needs_grading = models.BooleanField(default=False)
    feedback = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['position', 'id']

    def __str__(self):
        return f"{self.attempt_id} Q{self.position} ({self.qtype})"

    @property
    def type_name(self):
        return dict(Question.TYPE_CHOICES).get(self.qtype, self.qtype)

    @property
    def status_label(self):
        if self.qtype == Question.TYPE_ESSAY and self.answered and self.needs_grading:
            return 'Pending'
        if not self.answered:
            return 'No answer'
        return 'Correct' if self.is_correct else ('Partly correct' if (self.points_awarded or 0) > 0 else 'Incorrect')

    @property
    def status_css(self):
        return {'Pending': 'bg-amber-50 text-amber-700 border-amber-200',
                'No answer': 'bg-slate-100 text-slate-500 border-slate-200',
                'Correct': 'bg-emerald-50 text-emerald-700 border-emerald-200',
                'Partly correct': 'bg-sky-50 text-sky-700 border-sky-200',
                'Incorrect': 'bg-red-50 text-red-700 border-red-200'}[self.status_label]

    def review(self):
        """What to show on a review page, built from the frozen copy of the question."""
        from .quizzing import normalize
        snap = self.snapshot or {}
        if self.qtype == Question.TYPE_MC:
            picked = set((self.answer or {}).get('choices', []))
            return [{'text': c['text'], 'correct': c['correct'], 'picked': c['id'] in picked}
                    for c in snap.get('choices', [])]
        if self.qtype == Question.TYPE_FILL:
            given = (self.answer or {}).get('blanks', [])
            out = []
            for i in range(1, int(snap.get('blank_count', len(given))) + 1):
                value = given[i - 1] if i - 1 < len(given) else ''
                accepted = snap.get('blanks', {}).get(str(i), [])
                cs = snap.get('case_sensitive', False)
                ok = bool(value) and normalize(value, cs) in [normalize(a, cs) for a in accepted]
                out.append({'n': i, 'given': value, 'accepted': accepted, 'ok': ok})
            return out
        return []


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


# ---------------------------------------------------------
# TERMS AND CONDITIONS + STUDENT CONSENT
# ---------------------------------------------------------
class TermsVersion(models.Model):
    """One published version of the Terms and Conditions.

    Old versions are never edited or deleted, so every consent record keeps
    pointing at the exact text the student agreed to.

    requires_reacceptance=True marks a MAJOR change: every student must accept
    that version before continuing. A minor version (False) only updates the
    text that is shown; students who accepted the last major version are not
    interrupted.
    """
    version = models.CharField(max_length=20, unique=True)
    title = models.CharField(max_length=120, default='Terms and Conditions')
    content = models.TextField(help_text='One rule per heading. Blank line = new paragraph.')
    requires_reacceptance = models.BooleanField(default=True)
    published_at = models.DateTimeField(auto_now_add=True)
    published_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name='published_terms'
    )

    class Meta:
        ordering = ['-id']

    def __str__(self):
        return f"Terms v{self.version}"

    @classmethod
    def current(cls):
        """Newest published version (the text people read)."""
        return cls.objects.order_by('-id').first()

    @classmethod
    def required(cls):
        """Newest MAJOR version (the one every student must have accepted)."""
        return cls.objects.filter(requires_reacceptance=True).order_by('-id').first()

    @classmethod
    def student_has_accepted(cls, user):
        required = cls.required()
        if required is None:
            return True  # nothing published yet, nothing to accept
        return TermsAcceptance.objects.filter(user=user, terms__id__gte=required.id).exists()


class TermsAcceptance(models.Model):
    """A student's recorded consent: who, which version, and when."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='terms_acceptances')
    terms = models.ForeignKey(TermsVersion, on_delete=models.PROTECT, related_name='acceptances')
    accepted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-accepted_at']
        constraints = [
            models.UniqueConstraint(fields=['user', 'terms'], name='one_acceptance_per_user_per_version'),
        ]

    def __str__(self):
        return f"{self.user.username} accepted v{self.terms.version}"


# ---------------------------------------------------------
# AUDIT LOG (important administrative actions)
# ---------------------------------------------------------
class AuditLog(models.Model):
    actor = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='audit_actions')
    action = models.CharField(max_length=60)
    target_user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='audit_targets')
    details = models.CharField(max_length=255, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.action}"

    @classmethod
    def record(cls, actor, action, target_user=None, details=''):
        return cls.objects.create(actor=actor, action=action, target_user=target_user, details=details[:255])


# ---------------------------------------------------------
# CLASSROOMS (Google Classroom style)
# ---------------------------------------------------------
# No 0/O/1/I so a code is easy to read out loud or copy from a board.
CODE_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
CODE_LENGTH = 7


def normalize_code(raw):
    """What a student typed -> the form stored in the database."""
    return ''.join(ch for ch in (raw or '').upper() if ch.isalnum())


def generate_unique_code():
    """Random joining code. Always contains letters, so it can never be mistaken
    for a numeric classroom ID, and is checked to be unused."""
    while True:
        code = ''.join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        if any(c.isalpha() for c in code) and not Classroom.objects.filter(code=code).exists():
            return code


class Classroom(models.Model):
    name = models.CharField(max_length=120)
    description = models.CharField(max_length=300, blank=True, default='')
    mentor = models.ForeignKey(User, on_delete=models.CASCADE, related_name='classrooms')

    # Joining code (separate from the numeric ID). Regenerating it makes the old
    # code stop working for NEW enrollments; students already in stay in.
    code = models.CharField(max_length=12, unique=True, default=generate_unique_code, editable=False)
    code_enabled = models.BooleanField(default=True, help_text='Turn off to stop new students joining.')
    code_updated_at = models.DateTimeField(auto_now_add=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name', 'id']

    def __str__(self):
        return f"{self.name} ({self.mentor.username})"

    @property
    def mentor_name(self):
        return self.mentor.profile.display_name

    def regenerate_code(self):
        from django.utils import timezone
        self.code = generate_unique_code()
        self.code_enabled = True
        self.code_updated_at = timezone.now()
        self.save(update_fields=['code', 'code_enabled', 'code_updated_at'])
        return self.code

    def student_enrollments(self):
        return self.enrollments.filter(is_active=True).select_related('student__profile')

    @property
    def student_count(self):
        return self.enrollments.filter(is_active=True).count()

    def has_member(self, user):
        return self.enrollments.filter(student=user, is_active=True).exists()


class Enrollment(models.Model):
    """Which student joined which classroom, and when. Removing a student only
    switches is_active off, so their quiz attempts and grades are never touched."""
    classroom = models.ForeignKey(Classroom, on_delete=models.CASCADE, related_name='enrollments')
    student = models.ForeignKey(User, on_delete=models.CASCADE, related_name='enrollments')
    joined_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)
    removed_at = models.DateTimeField(null=True, blank=True)
    removed_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')

    class Meta:
        ordering = ['student__username']
        constraints = [
            models.UniqueConstraint(fields=['classroom', 'student'], name='one_enrollment_per_student_per_classroom'),
        ]

    def __str__(self):
        return f"{self.student.username} in {self.classroom.name}"


# ---------------------------------------------------------
# EXTENSIONS AND EXEMPTIONS (per student, per quiz)
# ---------------------------------------------------------
class QuizAccommodation(models.Model):
    KIND_EXTENSION = 'EXTENSION'
    KIND_EXEMPTION = 'EXEMPTION'
    KIND_CHOICES = [(KIND_EXTENSION, 'Extension'), (KIND_EXEMPTION, 'Exemption')]

    quiz = models.ForeignKey(Quiz, on_delete=models.CASCADE, related_name='accommodations')
    student = models.ForeignKey(User, on_delete=models.CASCADE, related_name='quiz_accommodations')
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    new_deadline = models.DateTimeField(null=True, blank=True)
    reason = models.CharField(max_length=200, blank=True, default='')
    granted_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['quiz', 'student'], name='one_accommodation_per_student_per_quiz'),
        ]

    def __str__(self):
        return f"{self.kind} for {self.student.username} on {self.quiz.title}"


# ---------------------------------------------------------
# WARNING POLICY (one row, edited by an Admin)
# ---------------------------------------------------------
class PolicySettings(models.Model):
    reminder_hours = models.CharField(
        max_length=60, default='24,2', blank=True,
        help_text="Hours before a deadline to remind students, separated by commas. Empty = no reminders.")
    send_overdue_email = models.BooleanField(default=True)
    warn_threshold = models.PositiveIntegerField(
        default=2, help_text="A final warning is issued when a student has this many unresolved overdue quizzes.")
    warn_after_overdue_hours = models.PositiveIntegerField(
        default=24, help_text="...and the oldest overdue quiz has been overdue at least this many hours.")
    warning_window_hours = models.PositiveIntegerField(
        default=24, help_text="How long a student has to act after a final warning (minimum 24).")
    deactivate_min_unresolved = models.PositiveIntegerField(
        default=1, help_text="After the window, deactivate only if at least this many warned quizzes are still unresolved.")
    auto_deactivation_approved = models.BooleanField(
        default=False,
        help_text="OFF: after the window the case waits for an Admin to review. ON: the system deactivates automatically.")
    approved_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    approved_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @property
    def reminder_hours_list(self):
        out = []
        for part in (self.reminder_hours or '').split(','):
            part = part.strip()
            if part.isdigit() and int(part) > 0:
                out.append(int(part))
        return sorted(set(out), reverse=True)


# ---------------------------------------------------------
# FINAL WARNINGS
# ---------------------------------------------------------
class FinalWarning(models.Model):
    STATUS_OPEN = 'OPEN'
    STATUS_RESOLVED = 'RESOLVED'
    STATUS_PENDING_REVIEW = 'PENDING_REVIEW'
    STATUS_DEACTIVATED = 'DEACTIVATED'
    STATUS_RESET = 'RESET'
    STATUS_CHOICES = [
        (STATUS_OPEN, 'Open (student has time to act)'),
        (STATUS_RESOLVED, 'Resolved (no action needed)'),
        (STATUS_PENDING_REVIEW, 'Waiting for Admin review'),
        (STATUS_DEACTIVATED, 'Account deactivated'),
        (STATUS_RESET, 'Reset by staff'),
    ]

    student = models.ForeignKey(User, on_delete=models.CASCADE, related_name='final_warnings')
    quizzes = models.ManyToManyField(Quiz, related_name='final_warnings')
    issued_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_OPEN)
    evaluated_at = models.DateTimeField(null=True, blank=True)
    # What the student did during the window (information for the Admin).
    activity = models.CharField(max_length=60, blank=True, default='')
    resolution_note = models.CharField(max_length=300, blank=True, default='')

    class Meta:
        ordering = ['-issued_at']

    def __str__(self):
        return f"Final warning for {self.student.username} ({self.status})"


# ---------------------------------------------------------
# NOTIFICATIONS (in-portal message + email delivery record)
# ---------------------------------------------------------
class Notification(models.Model):
    KIND_REMINDER = 'REMINDER'
    KIND_OVERDUE = 'OVERDUE'
    KIND_FINAL_WARNING = 'FINAL_WARNING'
    KIND_DEACTIVATED = 'DEACTIVATED'
    KIND_REACTIVATED = 'REACTIVATED'
    KIND_ACCOMMODATION = 'ACCOMMODATION'
    KIND_ADMIN_ALERT = 'ADMIN_ALERT'
    KIND_GRADED = 'GRADED'
    KIND_CHOICES = [
        (KIND_REMINDER, 'Deadline reminder'), (KIND_OVERDUE, 'Overdue quiz'),
        (KIND_FINAL_WARNING, 'Final warning'), (KIND_DEACTIVATED, 'Account deactivated'),
        (KIND_REACTIVATED, 'Account reactivated'), (KIND_ACCOMMODATION, 'Extension / exemption'),
        (KIND_ADMIN_ALERT, 'Admin alert'), (KIND_GRADED, 'Quiz graded'),
    ]

    EMAIL_PENDING = 'PENDING'
    EMAIL_SENT = 'SENT'
    EMAIL_FAILED = 'FAILED'
    EMAIL_NO_ADDRESS = 'NO_EMAIL'
    EMAIL_NOT_SENT = 'NOT_SENT'     # by design (email switched off for this kind)
    EMAIL_CHOICES = [
        (EMAIL_PENDING, 'Pending'), (EMAIL_SENT, 'Sent'), (EMAIL_FAILED, 'Failed'),
        (EMAIL_NO_ADDRESS, 'No email address'), (EMAIL_NOT_SENT, 'Not emailed'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notifications')
    kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    quiz = models.ForeignKey(Quiz, null=True, blank=True, on_delete=models.SET_NULL, related_name='notifications')
    warning = models.ForeignKey(FinalWarning, null=True, blank=True, on_delete=models.SET_NULL, related_name='notifications')
    subject = models.CharField(max_length=200)
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)
    hidden = models.BooleanField(default=False)   # bookkeeping rows (e.g. superseded reminders)

    # Makes sure the same notice is never created twice, even if the scheduled
    # task runs twice at once.
    dedupe_key = models.CharField(max_length=160, unique=True)

    email_status = models.CharField(max_length=10, choices=EMAIL_CHOICES, default=EMAIL_PENDING)
    email_error = models.CharField(max_length=255, blank=True, default='')
    email_attempts = models.PositiveIntegerField(default=0)
    email_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at', '-id']

    def __str__(self):
        return f"{self.kind} -> {self.user.username} ({self.email_status})"


# ---------------------------------------------------------
# ACCOUNT STATUS HISTORY (deactivations and reactivations)
# ---------------------------------------------------------
class AccountStatusLog(models.Model):
    ACTION_DEACTIVATED = 'DEACTIVATED'
    ACTION_REACTIVATED = 'REACTIVATED'

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='status_log')
    action = models.CharField(max_length=12)
    reason = models.CharField(max_length=300, blank=True, default='')
    details = models.TextField(blank=True, default='')           # overdue quizzes + warning history
    warning = models.ForeignKey(FinalWarning, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    actor = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')  # empty = the system
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']
