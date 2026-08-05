from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.contrib import messages

from .models import (
    Profile, Lesson, LessonProgress,
    Quiz, Question, QuizAttempt,
    Badge, UserBadge,
)
from .forms import UserUpdateForm, ProfileUpdateForm


# ---------------------------------------------------------
# Authentication
# ---------------------------------------------------------
def login_view(request):
    if request.user.is_authenticated:
        return redirect('home')

    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')
        user = authenticate(request, username=username, password=password)
        if user is not None:
            login(request, user)
            return redirect('home')
        else:
            messages.error(request, 'Invalid username or password.')
            return render(request, 'login.html')

    return render(request, 'login.html')


def logout_view(request):
    logout(request)
    return redirect('login')


def register_view(request):
    if request.user.is_authenticated:
        return redirect('home')

    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        email = request.POST.get('email', '').strip()
        password = request.POST.get('password', '')

        if not username or not password:
            messages.error(request, 'Username and password are required.')
            return render(request, 'register.html')

        if User.objects.filter(username=username).exists():
            messages.error(request, 'That username is already taken.')
            return render(request, 'register.html')

        user = User.objects.create_user(username=username, email=email, password=password)
        # Profile is auto-created by the post_save signal in signals.py
        login(request, user)
        return redirect('home')

    return render(request, 'register.html')


# ---------------------------------------------------------
# Dashboard
# ---------------------------------------------------------
@login_required
def home(request):
    profile, _ = Profile.objects.get_or_create(user=request.user)
    return render(request, 'home.html', {'profile': profile})


# ---------------------------------------------------------
# Lessons
# ---------------------------------------------------------
@login_required
def lesson_list(request):
    lessons = Lesson.objects.all().order_by('order')
    completed_ids = set(
        LessonProgress.objects.filter(user=request.user, completed=True)
        .values_list('lesson_id', flat=True)
    )
    for lesson in lessons:
        lesson.is_completed = lesson.id in completed_ids

    return render(request, 'lesson_list.html', {'lessons': lessons})


@login_required
def lesson_detail(request, slug):
    lesson = get_object_or_404(Lesson, slug=slug)
    progress, _ = LessonProgress.objects.get_or_create(user=request.user, lesson=lesson)

    if request.method == 'POST' and request.POST.get('action') == 'mark_complete':
        progress.completed = True
        progress.completed_at = timezone.now()
        progress.save()
        messages.success(request, f'"{lesson.title}" marked as complete!')
        return redirect('lesson_detail', slug=slug)

    return render(request, 'lesson_detail.html', {'lesson': lesson, 'progress': progress})


# ---------------------------------------------------------
# Quizzes
# ---------------------------------------------------------
@login_required
def quizzes_view(request):
    quizzes = Quiz.objects.all()
    best_scores = {
        attempt['quiz_id']: attempt['score']
        for attempt in QuizAttempt.objects.filter(user=request.user).values('quiz_id').annotate()
    }
    # Attach the user's best score per quiz for display
    for quiz in quizzes:
        attempts = QuizAttempt.objects.filter(user=request.user, quiz=quiz)
        quiz.best_score = max((a.score for a in attempts), default=None)

    return render(request, 'quizzes.html', {'quizzes': quizzes})


@login_required
def quiz_detail(request, quiz_id):
    quiz = get_object_or_404(Quiz, id=quiz_id)
    questions = quiz.questions.prefetch_related('choices').all()

    if request.method == 'POST':
        correct_count = 0
        for question in questions:
            selected_id = request.POST.get(f'question_{question.id}')
            if selected_id and question.choices.filter(id=selected_id, is_correct=True).exists():
                correct_count += 1

        total = questions.count()
        score = round((correct_count / total) * 100) if total else 0
        QuizAttempt.objects.create(user=request.user, quiz=quiz, score=score)

        check_and_award_badges(request.user)

        return render(request, 'quiz_result.html', {
            'quiz': quiz, 'score': score, 'correct_count': correct_count, 'total': total,
        })

    return render(request, 'quiz_detail.html', {'quiz': quiz, 'questions': questions})


# ---------------------------------------------------------
# Badges
# ---------------------------------------------------------
@login_required
def badges_view(request):
    all_badges = Badge.objects.all()
    earned_ids = set(UserBadge.objects.filter(user=request.user).values_list('badge_id', flat=True))
    for badge in all_badges:
        badge.is_earned = badge.id in earned_ids

    return render(request, 'badges.html', {'badges': all_badges})


def check_and_award_badges(user):
    """Very simple example badge rules — extend these as you like."""
    lessons_done = LessonProgress.objects.filter(user=user, completed=True).count()
    quizzes_taken = QuizAttempt.objects.filter(user=user).count()
    perfect_scores = QuizAttempt.objects.filter(user=user, score=100).count()

    rules = [
        ("First Steps", lessons_done >= 1),
        ("Quiz Taker", quizzes_taken >= 1),
        ("Perfectionist", perfect_scores >= 1),
        ("Dedicated Learner", lessons_done >= 5),
    ]

    for badge_name, condition_met in rules:
        if condition_met:
            badge, _ = Badge.objects.get_or_create(name=badge_name)
            UserBadge.objects.get_or_create(user=user, badge=badge)


# ---------------------------------------------------------
# Profile
# ---------------------------------------------------------
@login_required
def profile_view(request):
    profile, _ = Profile.objects.get_or_create(user=request.user)
    return render(request, 'profile.html', {'profile': profile})


@login_required
def edit_profile(request):
    profile, _ = Profile.objects.get_or_create(user=request.user)

    if request.method == 'POST':
        user_form = UserUpdateForm(request.POST, instance=request.user)
        profile_form = ProfileUpdateForm(request.POST, request.FILES, instance=profile)

        if user_form.is_valid() and profile_form.is_valid():
            user_form.save()
            profile_form.save()
            messages.success(request, 'Profile updated.')
            return redirect('profile')
        else:
            messages.error(request, 'Please fix the errors below.')
    else:
        user_form = UserUpdateForm(instance=request.user)
        profile_form = ProfileUpdateForm(instance=profile)

    return render(request, 'edit_profile.html', {
        'profile': profile,
        'user_form': user_form,
        'profile_form': profile_form,
    })