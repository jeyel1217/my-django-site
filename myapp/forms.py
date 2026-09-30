from django import forms
from django.contrib.auth.models import User
from .models import Profile, Lesson, Quiz

TEXT_INPUT_CLASSES = (
    "w-full border border-slate-300 rounded-lg px-4 py-2 text-sm "
    "focus:outline-none focus:border-red-500"
)


class UserUpdateForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ['username', 'first_name', 'last_name', 'email']
        widgets = {
            'username': forms.TextInput(attrs={'class': TEXT_INPUT_CLASSES}),
            'first_name': forms.TextInput(attrs={'class': TEXT_INPUT_CLASSES}),
            'last_name': forms.TextInput(attrs={'class': TEXT_INPUT_CLASSES}),
            'email': forms.EmailInput(attrs={'class': TEXT_INPUT_CLASSES}),
        }


class ProfileUpdateForm(forms.ModelForm):
    class Meta:
        model = Profile
        fields = ['avatar', 'bio']
        widgets = {
            'avatar': forms.ClearableFileInput(attrs={
                'class': (
                    "w-full text-sm text-slate-600 file:mr-3 file:py-2 file:px-4 "
                    "file:rounded-lg file:border-0 file:bg-red-50 file:text-red-700 "
                    "file:font-semibold"
                )
            }),
            'bio': forms.Textarea(attrs={'class': TEXT_INPUT_CLASSES, 'rows': 4}),
        }


# ---------------------------------------------------------
# MENTOR — Lesson management
# ---------------------------------------------------------
class LessonForm(forms.ModelForm):
    """Slug is auto-generated from the title in the view, so it's not
    included here — Mentors just type a title."""
    class Meta:
        model = Lesson
        fields = ['title', 'order', 'content', 'is_published']
        widgets = {
            'title': forms.TextInput(attrs={'class': TEXT_INPUT_CLASSES}),
            'order': forms.NumberInput(attrs={'class': TEXT_INPUT_CLASSES}),
            'content': forms.Textarea(attrs={'class': TEXT_INPUT_CLASSES, 'rows': 10}),
            'is_published': forms.CheckboxInput(attrs={'class': 'h-4 w-4'}),
        }


# ---------------------------------------------------------
# MENTOR — Quiz management
# ---------------------------------------------------------
class QuizForm(forms.ModelForm):
    class Meta:
        model = Quiz
        fields = ['title', 'description', 'lesson', 'is_published']
        widgets = {
            'title': forms.TextInput(attrs={'class': TEXT_INPUT_CLASSES}),
            'description': forms.Textarea(attrs={'class': TEXT_INPUT_CLASSES, 'rows': 3}),
            'lesson': forms.Select(attrs={'class': TEXT_INPUT_CLASSES}),
            'is_published': forms.CheckboxInput(attrs={'class': 'h-4 w-4'}),
        }

    def __init__(self, *args, mentor_user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['lesson'].required = False
        # A Mentor should only be able to attach a quiz to their OWN lessons.
        if mentor_user is not None:
            self.fields['lesson'].queryset = Lesson.objects.filter(created_by=mentor_user)


# ---------------------------------------------------------
# MENTOR — Question + 4 choices (fixed-choice-count form, not a ModelForm)
# ---------------------------------------------------------
class QuestionForm(forms.Form):
    text = forms.CharField(
        label="Question",
        widget=forms.TextInput(attrs={'class': TEXT_INPUT_CLASSES}),
    )
    choice_1 = forms.CharField(label="Choice 1", widget=forms.TextInput(attrs={'class': TEXT_INPUT_CLASSES}))
    choice_2 = forms.CharField(label="Choice 2", widget=forms.TextInput(attrs={'class': TEXT_INPUT_CLASSES}))
    choice_3 = forms.CharField(label="Choice 3", widget=forms.TextInput(attrs={'class': TEXT_INPUT_CLASSES}))
    choice_4 = forms.CharField(label="Choice 4", widget=forms.TextInput(attrs={'class': TEXT_INPUT_CLASSES}))
    correct_choice = forms.ChoiceField(
        label="Correct answer",
        choices=[('1', 'Choice 1'), ('2', 'Choice 2'), ('3', 'Choice 3'), ('4', 'Choice 4')],
        widget=forms.RadioSelect,
    )


# ---------------------------------------------------------
# ADMIN — Create a Mentor account
# ---------------------------------------------------------
class MentorCreateForm(forms.Form):
    """Admins create Mentor accounts directly (per spec: Mentors cannot
    self-register). Role is forced to MENTOR in the view, not chosen here."""
    username = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={'class': TEXT_INPUT_CLASSES}),
    )
    email = forms.EmailField(
        required=False,
        widget=forms.EmailInput(attrs={'class': TEXT_INPUT_CLASSES}),
    )
    password = forms.CharField(
        min_length=8,
        widget=forms.PasswordInput(attrs={'class': TEXT_INPUT_CLASSES}),
        help_text="Minimum 8 characters. Share this with the Mentor securely.",
    )