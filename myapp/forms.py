from django import forms
from django.contrib.auth.models import User
from .models import Profile

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