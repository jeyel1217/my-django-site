import re
from django.utils import timezone

from django import forms
from django.contrib.auth.models import User
from .models import Profile, Lesson, Quiz, Question, Classroom, PolicySettings
from . import quizzing
from . import lessonfiles

TEXT_INPUT_CLASSES = (
    "w-full border border-slate-300 rounded-lg px-4 py-2 text-sm "
    "focus:outline-none focus:border-red-500"
)

# Dark-theme input used on the login / register / reset pages.
AUTH_INPUT_CLASSES = (
    "w-full bg-slate-700 border border-slate-600 rounded-lg px-4 py-2 "
    "text-white focus:outline-none focus:border-red-500"
)


# ---------------------------------------------------------
# SHARED VALIDATION RULES
# (registration, profile edit, Admin-created Mentors, password reset)
# ---------------------------------------------------------
USERNAME_PATTERN = re.compile(r'[A-Za-z0-9_]{8,20}')
GMAIL_PATTERN = re.compile(r'[A-Za-z0-9._+-]+@gmail\.com', re.IGNORECASE)

USERNAME_ERROR = "Username must be 8\u201320 characters and may contain letters, numbers, and underscores."
USERNAME_TAKEN_ERROR = "That username is already taken."
GMAIL_ERROR = "Please enter a valid Gmail address ending in @gmail.com."
EMAIL_TAKEN_ERROR = "This Gmail address is already registered."
PASSWORD_ERROR = (
    "Password must be at least 8 characters and include an uppercase letter, "
    "a lowercase letter, a number, and a special character."
)
NAME_LENGTH_ERROR = "Name must be 2\u201350 characters."
NAME_CHARS_ERROR = "Name may only contain letters, spaces, apostrophes, periods, and hyphens."


def password_strength(password):
    """Returns 'weak', 'medium' or 'strong'.

    The same rules are repeated in templates/password_strength.html so the
    live indicator and the server always agree.
        weak   : under 8 characters, or only 1-2 kinds of characters
        medium : 8+ characters with 3 kinds (upper / lower / number / symbol)
        strong : 8+ characters with all 4 kinds
    """
    password = password or ''
    kinds = sum([
        bool(re.search(r'[a-z]', password)),
        bool(re.search(r'[A-Z]', password)),
        bool(re.search(r'[0-9]', password)),
        bool(re.search(r'[^A-Za-z0-9]', password)),
    ])
    if len(password) < 8 or kinds <= 2:
        return 'weak'
    if kinds == 3:
        return 'medium'
    return 'strong'


def validate_strong_password(password):
    if password_strength(password) != 'strong':
        raise forms.ValidationError(PASSWORD_ERROR)
    return password


def clean_username_value(value):
    value = (value or '').strip()
    if not USERNAME_PATTERN.fullmatch(value):
        raise forms.ValidationError(USERNAME_ERROR)
    return value


def clean_gmail_value(value):
    value = (value or '').strip().lower()
    if not GMAIL_PATTERN.fullmatch(value):
        raise forms.ValidationError(GMAIL_ERROR)
    return value


def normalize_gmail(email):
    """Gmail ignores dots and '+tag' in the part before the @, so
    john.doe+x@gmail.com and johndoe@gmail.com are the SAME mailbox.
    Comparing normalised forms stops one person making several accounts."""
    local, _, domain = (email or '').strip().lower().partition('@')
    local = local.split('+', 1)[0].replace('.', '')
    return f"{local}@{domain}"


def users_with_gmail(email, exclude_pk=None):
    """All accounts already using this Gmail mailbox (case/dot/+tag safe)."""
    target = normalize_gmail(email)
    qs = User.objects.filter(email__iendswith='@gmail.com')
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    return [u for u in qs if normalize_gmail(u.email) == target]


class RegisterForm(forms.Form):
    """Self-registration (Students). Passwords are never echoed back."""
    username = forms.CharField(
        widget=forms.TextInput(attrs={
            'class': AUTH_INPUT_CLASSES,
            'placeholder': 'e.g., code_warrior1',
            'autocomplete': 'username',
        }),
    )
    email = forms.CharField(
        widget=forms.TextInput(attrs={
            'class': AUTH_INPUT_CLASSES,
            'placeholder': 'yourname@gmail.com',
            'autocomplete': 'email',
        }),
    )
    password = forms.CharField(
        strip=False,
        widget=forms.PasswordInput(attrs={
            'class': AUTH_INPUT_CLASSES,
            'placeholder': 'Create a strong password',
            'autocomplete': 'new-password',
        }),
    )
    confirm_password = forms.CharField(
        strip=False,
        widget=forms.PasswordInput(attrs={
            'class': AUTH_INPUT_CLASSES,
            'placeholder': 'Re-enter your password',
            'autocomplete': 'new-password',
        }),
    )
    # Never pre-ticked: the student must tick it themselves.
    accept_terms = forms.BooleanField(
        required=True,
        error_messages={'required': 'You must read and accept the Terms and Conditions to create an account.'},
    )

    def clean_username(self):
        return clean_username_value(self.cleaned_data.get('username'))

    def clean_email(self):
        return clean_gmail_value(self.cleaned_data.get('email'))

    def clean_password(self):
        return validate_strong_password(self.cleaned_data.get('password'))

    def clean(self):
        cleaned = super().clean()
        p1 = cleaned.get('password')
        p2 = cleaned.get('confirm_password')
        if p1 and p2 and p1 != p2:
            self.add_error('confirm_password', "Passwords do not match.")
        return cleaned


class UserUpdateForm(forms.ModelForm):
    """Edit username / email on the profile page.

    Existing accounts keep whatever username/email they already have (so old
    accounts can still save their profile). Only a CHANGED value has to pass
    the new rules."""
    # Declared here (instead of Django's default EmailField) so our own clear
    # Gmail message is shown rather than Django's generic one. Optional so an
    # old account with no email can still save its profile.
    email = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={'class': TEXT_INPUT_CLASSES}),
    )

    class Meta:
        model = User
        fields = ['username', 'email']
        widgets = {
            'username': forms.TextInput(attrs={'class': TEXT_INPUT_CLASSES}),
        }

    def clean_username(self):
        username = (self.cleaned_data.get('username') or '').strip()
        if username == self.instance.username:
            return username
        username = clean_username_value(username)
        taken = User.objects.filter(username__iexact=username).exclude(pk=self.instance.pk).exists()
        if taken:
            raise forms.ValidationError(USERNAME_TAKEN_ERROR)
        return username

    def clean_email(self):
        email = (self.cleaned_data.get('email') or '').strip().lower()
        if email == (self.instance.email or '').strip().lower():
            return self.instance.email
        email = clean_gmail_value(email)
        if users_with_gmail(email, exclude_pk=self.instance.pk):
            raise forms.ValidationError(EMAIL_TAKEN_ERROR)
        return email


class ProfileUpdateForm(forms.ModelForm):
    class Meta:
        model = Profile
        fields = ['name', 'student_id', 'avatar', 'bio']
        widgets = {
            'student_id': forms.TextInput(attrs={'class': TEXT_INPUT_CLASSES, 'placeholder': 'e.g., 2026-00123', 'maxlength': 30}),
            'name': forms.TextInput(attrs={
                'class': TEXT_INPUT_CLASSES,
                'placeholder': 'e.g., John Lloyd Arnado',
            }),
            'avatar': forms.ClearableFileInput(attrs={
                'class': (
                    "w-full text-sm text-slate-600 file:mr-3 file:py-2 file:px-4 "
                    "file:rounded-lg file:border-0 file:bg-red-50 file:text-red-700 "
                    "file:font-semibold"
                )
            }),
            'bio': forms.Textarea(attrs={'class': TEXT_INPUT_CLASSES, 'rows': 4}),
        }

    def clean_student_id(self):
        sid = ' '.join((self.cleaned_data.get('student_id') or '').split())
        if sid and not all(ch.isalnum() or ch in '-_/.' for ch in sid):
            raise forms.ValidationError('Student ID can only have letters, numbers, dashes and slashes.')
        return sid

    def clean_name(self):
        # Optional field. Tidy extra spaces, then check length and characters.
        name = ' '.join((self.cleaned_data.get('name') or '').split())
        if not name:
            return ''
        if len(name) < 2 or len(name) > 50:
            raise forms.ValidationError(NAME_LENGTH_ERROR)
        if not all(ch.isalpha() or ch in " '.-" for ch in name):
            raise forms.ValidationError(NAME_CHARS_ERROR)
        return name


# ---------------------------------------------------------
# MENTOR — Lesson management
# ---------------------------------------------------------
class LessonForm(forms.ModelForm):
    """Slug is auto-generated from the title in the view, so it's not
    included here — Mentors just type a title."""
    file = forms.FileField(
        required=False, label="Lesson file (PDF, Word or PowerPoint)",
        widget=forms.ClearableFileInput(attrs={'accept': '.pdf,.docx,.pptx', 'class': 'text-sm'}))
    remove_file = forms.BooleanField(required=False, label="Remove the current file")

    def clean_file(self):
        upload = self.cleaned_data.get('file')
        if upload:
            try:
                lessonfiles.inspect_upload(upload)
            except lessonfiles.UploadRejected as exc:
                raise forms.ValidationError(str(exc))
        return upload

    class Meta:
        model = Lesson
        fields = ['classroom', 'title', 'order', 'content', 'is_published']
        widgets = {
            'classroom': forms.Select(attrs={'class': TEXT_INPUT_CLASSES}),
            'title': forms.TextInput(attrs={'class': TEXT_INPUT_CLASSES}),
            'order': forms.NumberInput(attrs={'class': TEXT_INPUT_CLASSES}),
            'content': forms.Textarea(attrs={'class': TEXT_INPUT_CLASSES, 'rows': 10}),
            'is_published': forms.CheckboxInput(attrs={'class': 'h-4 w-4'}),
        }

    def __init__(self, *args, mentor_user=None, **kwargs):
        super().__init__(*args, **kwargs)
        # A lesson must belong to one of THIS Mentor's own classrooms.
        self.fields['classroom'].required = True
        self.fields['classroom'].empty_label = 'Choose a classroom'
        self.fields['classroom'].queryset = Classroom.objects.filter(mentor=mentor_user) if mentor_user else Classroom.objects.none()


# ---------------------------------------------------------
# MENTOR — Quiz management
# ---------------------------------------------------------
class QuizForm(forms.ModelForm):
    """Create / edit a quiz, including its schedule.

    Times are typed and shown in Philippine time (the view runs inside
    exams.local_time). "Has a deadline" is a real choice: unticked means the
    quiz has no deadline at all, so nothing can be late."""

    DATETIME_FORMAT = '%Y-%m-%dT%H:%M'

    has_deadline = forms.BooleanField(
        required=False,
        label="This quiz has a deadline",
        widget=forms.CheckboxInput(attrs={'class': 'h-4 w-4'}),
    )
    start_datetime = forms.DateTimeField(
        required=False,
        label="Start (optional)",
        input_formats=['%Y-%m-%dT%H:%M', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M'],
        widget=forms.DateTimeInput(attrs={'type': 'datetime-local', 'class': TEXT_INPUT_CLASSES},
                                   format='%Y-%m-%dT%H:%M'),
    )
    deadline_datetime = forms.DateTimeField(
        required=False,
        label="Deadline",
        input_formats=['%Y-%m-%dT%H:%M', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M'],
        widget=forms.DateTimeInput(attrs={'type': 'datetime-local', 'class': TEXT_INPUT_CLASSES},
                                   format='%Y-%m-%dT%H:%M'),
    )

    class Meta:
        model = Quiz
        fields = ['classroom', 'title', 'description', 'instructions', 'lesson', 'require_lesson_first', 'is_published',
                  'start_datetime', 'deadline_datetime', 'allow_late_submissions']
        widgets = {
            'classroom': forms.Select(attrs={'class': TEXT_INPUT_CLASSES}),
            'title': forms.TextInput(attrs={'class': TEXT_INPUT_CLASSES}),
            'description': forms.Textarea(attrs={'class': TEXT_INPUT_CLASSES, 'rows': 3}),
            'instructions': forms.Textarea(attrs={
                'class': TEXT_INPUT_CLASSES, 'rows': 3,
                'placeholder': 'Read each question carefully and select the best answer.',
            }),
            'lesson': forms.Select(attrs={'class': TEXT_INPUT_CLASSES}),
            'is_published': forms.CheckboxInput(attrs={'class': 'h-4 w-4'}),
            'require_lesson_first': forms.CheckboxInput(attrs={'class': 'h-4 w-4'}),
            'allow_late_submissions': forms.CheckboxInput(attrs={'class': 'h-4 w-4'}),
        }

    def __init__(self, *args, mentor_user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['lesson'].required = False
        self.fields['instructions'].required = False
        # A Mentor should only be able to attach a quiz to their OWN lessons.
        if mentor_user is not None:
            self.fields['lesson'].queryset = Lesson.objects.filter(created_by=mentor_user)
        # ...and to one of their own classrooms.
        self.fields['classroom'].required = True
        self.fields['classroom'].empty_label = 'Choose a classroom'
        self.fields['classroom'].queryset = Classroom.objects.filter(mentor=mentor_user) if mentor_user else Classroom.objects.none()
        if not self.is_bound:
            self.initial['has_deadline'] = bool(self.instance and self.instance.deadline_datetime)
        # The date picker greys out days before today (the server re-checks this in clean()).
        # Not applied to a quiz whose saved deadline is already past, so editing its other
        # fields is never blocked by the browser.
        saved = self.instance.deadline_datetime if self.instance and self.instance.pk else None
        if not (saved and saved < timezone.now()):
            self.fields['deadline_datetime'].widget.attrs['min'] = timezone.localtime(timezone.now()).strftime('%Y-%m-%dT%H:%M')

    def _deadline_unchanged(self, deadline):
        saved = self.instance.deadline_datetime if self.instance and self.instance.pk else None
        return bool(saved) and deadline == saved.replace(second=0, microsecond=0)

    def clean(self):
        cleaned = super().clean()
        start = cleaned.get('start_datetime')
        deadline = cleaned.get('deadline_datetime')

        if cleaned.get('has_deadline'):
            if not deadline and 'deadline_datetime' not in self.errors:
                self.add_error('deadline_datetime', "Please choose the deadline date and time.")
            elif deadline and deadline < timezone.now().replace(second=0, microsecond=0) and not self._deadline_unchanged(deadline):
                # Checked on the server, so it cannot be skipped by editing the page.
                self.add_error('deadline_datetime', "The deadline cannot be in the past. Choose today (a later time) or a future date.")
            elif start and deadline and deadline <= start:
                self.add_error('deadline_datetime', "The deadline must be after the start time.")
        else:
            # Deadline switched off: make sure nothing is saved.
            cleaned['deadline_datetime'] = None
            self.errors.pop('deadline_datetime', None)

        # A quiz can only be published when every question is complete.
        if cleaned.get('is_published') and self.instance and self.instance.pk:
            for problem in quizzing.quiz_problems(self.instance):
                self.add_error('is_published', problem)

        if cleaned.get('require_lesson_first') and not cleaned.get('lesson'):
            self.add_error('require_lesson_first', 'Choose a linked lesson first, or untick this.')

        # A linked lesson has to live in the same classroom as the quiz.
        lesson, classroom = cleaned.get('lesson'), cleaned.get('classroom')
        if lesson and classroom and lesson.classroom_id != classroom.pk:
            self.add_error('lesson', 'That lesson belongs to a different classroom. Pick a lesson from this classroom.')
        return cleaned


# ---------------------------------------------------------
# MENTOR — one question of any type (multiple choice, essay, fill in the blank)
# ---------------------------------------------------------
class QuestionEditForm(forms.Form):
    """Validates a question exactly as it will be graded.

    The type-specific parts (choices, accepted answers) are read straight from the
    posted data, so the page can add or remove choices freely."""
    qtype = forms.ChoiceField(choices=Question.TYPE_CHOICES)
    text = forms.CharField(max_length=500, widget=forms.Textarea(attrs={'class': TEXT_INPUT_CLASSES, 'rows': 3}))
    points = forms.IntegerField(min_value=1, max_value=quizzing.MAX_POINTS, initial=1)
    allow_multiple = forms.BooleanField(required=False)
    case_sensitive = forms.BooleanField(required=False)
    guide = forms.CharField(required=False, max_length=2000, widget=forms.Textarea(attrs={'class': TEXT_INPUT_CLASSES, 'rows': 3}))

    def clean_text(self):
        return (self.cleaned_data['text'] or '').strip()

    def clean(self):
        cleaned = super().clean()
        qtype, text = cleaned.get('qtype'), cleaned.get('text')
        if not qtype or not text:
            return cleaned

        if qtype == Question.TYPE_MC:
            texts = [t.strip() for t in self.data.getlist('choice_text')]
            correct_idx = set(self.data.getlist('choice_correct'))
            choices = [(t, str(i) in correct_idx) for i, t in enumerate(texts) if t]
            if len(choices) < quizzing.MIN_CHOICES:
                self.add_error(None, 'Add at least 2 choices.')
            elif len(choices) > quizzing.MAX_CHOICES:
                self.add_error(None, f'Use at most {quizzing.MAX_CHOICES} choices.')
            elif len({normalize_choice(t) for t, _ in choices}) != len(choices):
                self.add_error(None, 'Two choices are the same. Make each choice different.')
            else:
                right = sum(1 for _, ok in choices if ok)
                if right == 0:
                    self.add_error(None, 'Mark the correct choice.')
                elif right > 1 and not cleaned.get('allow_multiple'):
                    self.add_error(None, 'You marked several correct choices. Tick "More than one correct answer" or keep just one.')
                elif right == 1 and cleaned.get('allow_multiple'):
                    self.add_error(None, '"More than one correct answer" is ticked but only one choice is marked. Mark at least two, or untick it.')
            cleaned['choices'] = choices

        elif qtype == Question.TYPE_FILL:
            n = text.count(Question.BLANK_TOKEN)
            blanks = {}
            if n == 0:
                self.add_error('text', f'Put {Question.BLANK_TOKEN} where the student should type the answer, e.g. "Java is a {Question.BLANK_TOKEN} language."')
            elif n > quizzing.MAX_BLANKS:
                self.add_error('text', f'Use at most {quizzing.MAX_BLANKS} blanks.')
            else:
                for i in range(1, n + 1):
                    lines = [ln.strip() for ln in (self.data.get(f'blank_answers_{i}') or '').splitlines()]
                    lines = [ln for ln in lines if ln]
                    if not lines:
                        self.add_error(None, f'Blank {i} needs at least one accepted answer.')
                    blanks[i] = lines[:20]
            cleaned['blanks'] = blanks
        return cleaned


def normalize_choice(t):
    return ' '.join(t.split()).casefold()


# ---------------------------------------------------------
# ADMIN — Create a Mentor account
# ---------------------------------------------------------
class MentorCreateForm(forms.Form):
    """Admins create Mentor accounts directly (per spec: Mentors cannot
    self-register). Role is forced to MENTOR in the view, not chosen here.
    Same username / password rules as normal registration. No Gmail is
    needed: the Admin creates the account, so there is nothing to verify."""
    username = forms.CharField(
        widget=forms.TextInput(attrs={'class': TEXT_INPUT_CLASSES}),
        help_text="8\u201320 characters: letters, numbers, and underscores.",
    )
    password = forms.CharField(
        strip=False,
        widget=forms.PasswordInput(attrs={'class': TEXT_INPUT_CLASSES}),
        help_text="Share this with the Mentor securely.",
    )

    def clean_username(self):
        return clean_username_value(self.cleaned_data.get('username'))

    def clean_password(self):
        return validate_strong_password(self.cleaned_data.get('password'))


# ---------------------------------------------------------
# EMAIL VERIFICATION + PASSWORD RESET
# ---------------------------------------------------------
class OTPForm(forms.Form):
    code = forms.CharField(
        max_length=6,
        min_length=6,
        widget=forms.TextInput(attrs={
            'class': (
                "w-full bg-slate-700 border border-slate-600 rounded-lg px-4 py-2 "
                "text-white text-center text-2xl tracking-[0.5em] focus:outline-none focus:border-red-500"
            ),
            'placeholder': '000000',
            'autocomplete': 'one-time-code',
            'inputmode': 'numeric',
        }),
    )


class ForgotPasswordRequestForm(forms.Form):
    username_or_email = forms.CharField(
        label="Username or Email",
        widget=forms.TextInput(attrs={
            'class': (
                "w-full bg-slate-700 border border-slate-600 rounded-lg px-4 py-2 "
                "text-white focus:outline-none focus:border-red-500"
            ),
            'placeholder': 'Enter username or email',
        }),
    )


class ResetPasswordConfirmForm(forms.Form):
    code = forms.CharField(
        max_length=6,
        min_length=6,
        widget=forms.TextInput(attrs={
            'class': (
                "w-full bg-slate-700 border border-slate-600 rounded-lg px-4 py-2 "
                "text-white text-center text-2xl tracking-[0.5em] focus:outline-none focus:border-red-500"
            ),
            'placeholder': '000000',
            'inputmode': 'numeric',
        }),
    )
    new_password = forms.CharField(
        strip=False,
        widget=forms.PasswordInput(attrs={
            'class': AUTH_INPUT_CLASSES,
            'placeholder': 'Create a strong password',
            'autocomplete': 'new-password',
        }),
    )
    confirm_password = forms.CharField(
        strip=False,
        widget=forms.PasswordInput(attrs={
            'class': AUTH_INPUT_CLASSES,
            'placeholder': 'Re-enter new password',
            'autocomplete': 'new-password',
        }),
    )

    def clean_new_password(self):
        return validate_strong_password(self.cleaned_data.get('new_password'))

    def clean(self):
        cleaned = super().clean()
        p1 = cleaned.get('new_password')
        p2 = cleaned.get('confirm_password')
        if p1 and p2 and p1 != p2:
            self.add_error('confirm_password', "Passwords do not match.")
        return cleaned


class AdminResetPasswordForm(forms.Form):
    """Admin sets a new password for an account (used when a user has no
    Gmail on file, or is locked out). Same strength rules as everywhere."""
    new_password = forms.CharField(
        strip=False,
        widget=forms.PasswordInput(attrs={'class': TEXT_INPUT_CLASSES}),
    )
    confirm_password = forms.CharField(
        strip=False,
        widget=forms.PasswordInput(attrs={'class': TEXT_INPUT_CLASSES}),
    )

    def clean_new_password(self):
        return validate_strong_password(self.cleaned_data.get('new_password'))

    def clean(self):
        cleaned = super().clean()
        a, b = cleaned.get('new_password'), cleaned.get('confirm_password')
        if a and b and a != b:
            self.add_error('confirm_password', 'Passwords do not match.')
        return cleaned


# ---------------------------------------------------------
# TERMS AND CONDITIONS (Admin) + PHOTO REVIEW
# ---------------------------------------------------------
class TermsPublishForm(forms.Form):
    version = forms.CharField(
        max_length=20,
        widget=forms.TextInput(attrs={'class': TEXT_INPUT_CLASSES, 'placeholder': 'e.g., 1.1'}),
        help_text='A short version label such as 1.1 or 2.0.',
    )
    title = forms.CharField(
        max_length=120, initial='JCAD CodeQuest Terms and Conditions',
        widget=forms.TextInput(attrs={'class': TEXT_INPUT_CLASSES}),
    )
    content = forms.CharField(
        widget=forms.Textarea(attrs={'class': TEXT_INPUT_CLASSES, 'rows': 20}),
        help_text="Use '## ' for a heading, '- ' for a bullet, and a blank line for a new paragraph.",
    )
    requires_reacceptance = forms.BooleanField(
        required=False, initial=True,
        label='Major change: every student must read and accept this version before continuing',
    )

    def clean_version(self):
        from .models import TermsVersion
        v = self.cleaned_data['version'].strip()
        if TermsVersion.objects.filter(version__iexact=v).exists():
            raise forms.ValidationError('That version label already exists. Use a new one.')
        return v

    def clean_content(self):
        c = self.cleaned_data['content'].strip()
        if len(c) < 50:
            raise forms.ValidationError('Please write the full Terms (at least a few sentences).')
        return c


class PhotoFlagForm(forms.Form):
    reason = forms.CharField(
        max_length=200,
        widget=forms.TextInput(attrs={
            'class': TEXT_INPUT_CLASSES,
            'placeholder': 'e.g., Please use a real photo of yourself, not a cartoon character.',
        }),
    )

    def clean_reason(self):
        r = ' '.join(self.cleaned_data['reason'].split())
        if len(r) < 5:
            raise forms.ValidationError('Please explain what the student needs to fix.')
        return r


# ---------------------------------------------------------
# CLASSROOMS
# ---------------------------------------------------------
class ClassroomForm(forms.ModelForm):
    class Meta:
        model = Classroom
        fields = ['name', 'description']
        widgets = {
            'name': forms.TextInput(attrs={'class': TEXT_INPUT_CLASSES, 'placeholder': 'e.g., Introduction to Java Programming'}),
            'description': forms.TextInput(attrs={'class': TEXT_INPUT_CLASSES, 'placeholder': 'Optional: section, schedule or short note'}),
        }

    def clean_name(self):
        name = ' '.join(self.cleaned_data['name'].split())
        if len(name) < 3:
            raise forms.ValidationError('Please enter a classroom name (at least 3 characters).')
        return name


# ---------------------------------------------------------
# DEADLINES: extensions / exemptions and the warning policy
# ---------------------------------------------------------
class AccommodationForm(forms.Form):
    kind = forms.ChoiceField(
        choices=[('EXTENSION', 'Extend the deadline for this student'), ('EXEMPTION', 'Excuse this student from the quiz')],
        widget=forms.RadioSelect,
    )
    new_deadline = forms.DateTimeField(
        required=False, label='New deadline',
        input_formats=['%Y-%m-%dT%H:%M', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M'],
        widget=forms.DateTimeInput(attrs={'type': 'datetime-local', 'class': TEXT_INPUT_CLASSES}, format='%Y-%m-%dT%H:%M'),
    )
    reason = forms.CharField(
        max_length=200, required=False,
        widget=forms.TextInput(attrs={'class': TEXT_INPUT_CLASSES, 'placeholder': 'Optional reason (kept on record)'}),
    )

    def __init__(self, *args, quiz=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.quiz = quiz

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('kind') == 'EXTENSION':
            nd = cleaned.get('new_deadline')
            if not nd:
                self.add_error('new_deadline', 'Choose the new deadline date and time.')
            elif nd < timezone.now().replace(second=0, microsecond=0):
                self.add_error('new_deadline', 'The new deadline cannot be in the past.')
            elif self.quiz and self.quiz.deadline_datetime and nd <= self.quiz.deadline_datetime:
                self.add_error('new_deadline', 'The new deadline must be later than the quiz deadline.')
            elif self.quiz and not self.quiz.deadline_datetime:
                self.add_error('new_deadline', 'This quiz has no deadline, so there is nothing to extend.')
        return cleaned


class PolicyForm(forms.ModelForm):
    class Meta:
        model = PolicySettings
        fields = ['reminder_hours', 'send_overdue_email', 'warn_threshold', 'warn_after_overdue_hours',
                  'warning_window_hours', 'deactivate_min_unresolved', 'auto_deactivation_approved']
        widgets = {
            'reminder_hours': forms.TextInput(attrs={'class': TEXT_INPUT_CLASSES, 'placeholder': '24,2'}),
            'warn_threshold': forms.NumberInput(attrs={'class': TEXT_INPUT_CLASSES, 'min': 1}),
            'warn_after_overdue_hours': forms.NumberInput(attrs={'class': TEXT_INPUT_CLASSES, 'min': 0}),
            'warning_window_hours': forms.NumberInput(attrs={'class': TEXT_INPUT_CLASSES, 'min': 24}),
            'deactivate_min_unresolved': forms.NumberInput(attrs={'class': TEXT_INPUT_CLASSES, 'min': 1}),
            'send_overdue_email': forms.CheckboxInput(attrs={'class': 'h-4 w-4'}),
            'auto_deactivation_approved': forms.CheckboxInput(attrs={'class': 'h-4 w-4'}),
        }

    def clean_reminder_hours(self):
        raw = (self.cleaned_data.get('reminder_hours') or '').replace(' ', '')
        if not raw:
            return ''
        parts = raw.split(',')
        if not all(p.isdigit() and 1 <= int(p) <= 720 for p in parts):
            raise forms.ValidationError('Reminder hours must be whole numbers between 1 and 720, like 24,2.')
        return ','.join(parts)

    def clean_warning_window_hours(self):
        v = self.cleaned_data['warning_window_hours']
        if v < 24:
            raise forms.ValidationError('The warning period must be at least 24 hours.')
        return v

    def clean_warn_threshold(self):
        v = self.cleaned_data['warn_threshold']
        if v < 1:
            raise forms.ValidationError('At least 1 overdue quiz is needed for a warning.')
        return v
