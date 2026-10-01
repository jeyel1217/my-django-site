from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.utils.text import slugify
from django.contrib import messages
from django.db.models import Max, Count, Q

from .models import (
    Profile, Lesson, LessonProgress,
    Quiz, Question, Choice, QuizAttempt,
    Badge, UserBadge,
)
from .forms import (
    UserUpdateForm, ProfileUpdateForm, LessonForm, QuizForm, QuestionForm,
    MentorCreateForm, OTPForm, ForgotPasswordRequestForm, ResetPasswordConfirmForm,
)
from .decorators import role_required, require_verified
from .utils import generate_and_send_otp, verify_otp, can_resend, seconds_until_resend
from .models import OTPCode


# ---------------------------------------------------------
# Authentication
# ---------------------------------------------------------
def login_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard_router')

    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')
        user = authenticate(request, username=username, password=password)
        if user is not None:
            login(request, user)
            return redirect('dashboard_router')
        else:
            messages.error(request, 'Invalid username or password.')
            return render(request, 'login.html')

    return render(request, 'login.html')


def logout_view(request):
    logout(request)
    return redirect('login')


def register_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard_router')

    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        email = request.POST.get('email', '').strip()
        password = request.POST.get('password', '')

        if not username or not password:
            messages.error(request, 'Username and password are required.')
            return render(request, 'register.html')

        if not email:
            messages.error(request, 'Email is required for account verification.')
            return render(request, 'register.html')

        if User.objects.filter(username=username).exists():
            messages.error(request, 'That username is already taken.')
            return render(request, 'register.html')

        if User.objects.filter(email__iexact=email).exists():
            messages.error(request, 'This email address is already registered.')
            return render(request, 'register.html')

        user = User.objects.create_user(username=username, email=email, password=password)
        # Profile is auto-created by the post_save signal in signals.py.
        # Force it unverified — this account cannot use the app until the
        # OTP emailed below is confirmed on the Verify Email page.
        user.profile.is_verified = False
        user.profile.save(update_fields=['is_verified'])

        sent = generate_and_send_otp(user, OTPCode.PURPOSE_VERIFY_EMAIL)

        login(request, user)  # session started, but require_verified blocks real access
        if sent:
            messages.success(request, f'A verification code was sent to {email}.')
        else:
            messages.warning(
                request,
                'Account created, but we couldn\'t send the verification email right now. '
                'Use the "Resend code" button below to try again.'
            )
        return redirect('verify_email')

    return render(request, 'register.html')


# ---------------------------------------------------------
# Dashboard routing (role-based)
# ---------------------------------------------------------
@login_required
def dashboard_router(request):
    """Single entry point after login. Sends each role to its own dashboard.
    Unverified accounts get sent to the verification page instead."""
    profile, _ = Profile.objects.get_or_create(user=request.user)

    if not profile.is_verified:
        return redirect('verify_email')

    profile.update_streak()

    if profile.role == Profile.ROLE_ADMIN:
        return redirect('admin_dashboard')
    elif profile.role == Profile.ROLE_MENTOR:
        return redirect('mentor_dashboard')
    else:
        return redirect('student_dashboard')


@login_required
@role_required(Profile.ROLE_STUDENT)
def student_dashboard(request):
    profile, _ = Profile.objects.get_or_create(user=request.user)
    return render(request, 'home.html', {'profile': profile})


@login_required
@role_required(Profile.ROLE_MENTOR)
def mentor_dashboard(request):
    profile, _ = Profile.objects.get_or_create(user=request.user)
    my_lessons = Lesson.objects.filter(created_by=request.user)
    my_quizzes = Quiz.objects.filter(created_by=request.user)
    return render(request, 'mentor_dashboard.html', {
        'profile': profile,
        'my_lessons': my_lessons,
        'my_quizzes': my_quizzes,
    })


@login_required
@role_required(Profile.ROLE_ADMIN)
def admin_dashboard(request):
    profile, _ = Profile.objects.get_or_create(user=request.user)
    total_users = User.objects.count()
    total_students = Profile.objects.filter(role=Profile.ROLE_STUDENT).count()
    total_mentors = Profile.objects.filter(role=Profile.ROLE_MENTOR).count()
    inactive_count = User.objects.filter(is_active=False).count()
    duplicate_email_count = (
        User.objects.exclude(email='')
        .values('email')
        .annotate(c=Count('id'))
        .filter(c__gt=1)
        .count()
    )
    return render(request, 'admin_dashboard.html', {
        'profile': profile,
        'total_users': total_users,
        'total_students': total_students,
        'total_mentors': total_mentors,
        'inactive_count': inactive_count,
        'duplicate_email_count': duplicate_email_count,
    })


# ---------------------------------------------------------
# Lessons (STUDENT-FACING — published only)
# ---------------------------------------------------------
@login_required
def lesson_list(request):
    lessons = Lesson.objects.filter(is_published=True).order_by('order')
    completed_ids = set(
        LessonProgress.objects.filter(user=request.user, completed=True)
        .values_list('lesson_id', flat=True)
    )
    for lesson in lessons:
        lesson.is_completed = lesson.id in completed_ids

    return render(request, 'lesson_list.html', {'lessons': lessons})


@login_required
def lesson_detail(request, slug):
    lesson = get_object_or_404(Lesson, slug=slug, is_published=True)
    progress, _ = LessonProgress.objects.get_or_create(user=request.user, lesson=lesson)

    if request.method == 'POST' and request.POST.get('action') == 'mark_complete':
        progress.completed = True
        progress.completed_at = timezone.now()
        progress.save()

        profile, _ = Profile.objects.get_or_create(user=request.user)
        profile.update_streak()

        messages.success(request, f'"{lesson.title}" marked as complete!')
        return redirect('lesson_detail', slug=slug)

    return render(request, 'lesson_detail.html', {'lesson': lesson, 'progress': progress})


# ---------------------------------------------------------
# Quizzes (STUDENT-FACING — published only)
# ---------------------------------------------------------
@login_required
def quizzes_view(request):
    quizzes = Quiz.objects.filter(is_published=True)

    best_scores = dict(
        QuizAttempt.objects.filter(user=request.user, quiz__in=quizzes)
        .values('quiz_id')
        .annotate(best=Max('score'))
        .values_list('quiz_id', 'best')
    )

    for quiz in quizzes:
        quiz.best_score = best_scores.get(quiz.id)

    return render(request, 'quizzes.html', {'quizzes': quizzes})


@login_required
def quiz_detail(request, quiz_id):
    quiz = get_object_or_404(Quiz, id=quiz_id, is_published=True)
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

        if score >= 50:
            profile, _ = Profile.objects.get_or_create(user=request.user)
            profile.update_streak()

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


# ===========================================================
# MENTOR — Lesson management (own content only)
# ===========================================================
def _unique_slug(title, model, exclude_pk=None):
    base = slugify(title)
    slug = base
    n = 1
    qs = model.objects.filter(slug=slug)
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    while qs.exists():
        n += 1
        slug = f"{base}-{n}"
        qs = model.objects.filter(slug=slug)
        if exclude_pk:
            qs = qs.exclude(pk=exclude_pk)
    return slug


@login_required
@role_required(Profile.ROLE_MENTOR)
def mentor_lessons(request):
    lessons = Lesson.objects.filter(created_by=request.user).order_by('order')
    return render(request, 'mentor_lessons.html', {'lessons': lessons})


@login_required
@role_required(Profile.ROLE_MENTOR)
def mentor_lesson_create(request):
    if request.method == 'POST':
        form = LessonForm(request.POST)
        if form.is_valid():
            lesson = form.save(commit=False)
            lesson.created_by = request.user
            lesson.slug = _unique_slug(lesson.title, Lesson)
            lesson.save()
            messages.success(request, f'Lesson "{lesson.title}" created.')
            return redirect('mentor_lessons')
    else:
        form = LessonForm()

    return render(request, 'mentor_lesson_form.html', {'form': form, 'is_edit': False})


@login_required
@role_required(Profile.ROLE_MENTOR)
def mentor_lesson_edit(request, pk):
    lesson = get_object_or_404(Lesson, pk=pk, created_by=request.user)

    if request.method == 'POST':
        form = LessonForm(request.POST, instance=lesson)
        if form.is_valid():
            updated = form.save(commit=False)
            if updated.title != lesson.title:
                updated.slug = _unique_slug(updated.title, Lesson, exclude_pk=lesson.pk)
            updated.save()
            messages.success(request, f'Lesson "{updated.title}" updated.')
            return redirect('mentor_lessons')
    else:
        form = LessonForm(instance=lesson)

    return render(request, 'mentor_lesson_form.html', {'form': form, 'is_edit': True, 'lesson': lesson})


@login_required
@role_required(Profile.ROLE_MENTOR)
def mentor_lesson_delete(request, pk):
    lesson = get_object_or_404(Lesson, pk=pk, created_by=request.user)
    if request.method == 'POST':
        title = lesson.title
        lesson.delete()
        messages.success(request, f'Lesson "{title}" deleted.')
        return redirect('mentor_lessons')
    return render(request, 'mentor_confirm_delete.html', {'object_label': f'lesson "{lesson.title}"', 'cancel_url': 'mentor_lessons'})


# ===========================================================
# MENTOR — Quiz management (own content only)
# ===========================================================
@login_required
@role_required(Profile.ROLE_MENTOR)
def mentor_quizzes(request):
    quizzes = Quiz.objects.filter(created_by=request.user)
    return render(request, 'mentor_quizzes.html', {'quizzes': quizzes})


@login_required
@role_required(Profile.ROLE_MENTOR)
def mentor_quiz_create(request):
    if request.method == 'POST':
        form = QuizForm(request.POST, mentor_user=request.user)
        if form.is_valid():
            quiz = form.save(commit=False)
            quiz.created_by = request.user
            quiz.save()
            messages.success(request, f'Quiz "{quiz.title}" created. Now add some questions.')
            return redirect('mentor_quiz_questions', pk=quiz.pk)
    else:
        form = QuizForm(mentor_user=request.user)

    return render(request, 'mentor_quiz_form.html', {'form': form, 'is_edit': False})


@login_required
@role_required(Profile.ROLE_MENTOR)
def mentor_quiz_edit(request, pk):
    quiz = get_object_or_404(Quiz, pk=pk, created_by=request.user)

    if request.method == 'POST':
        form = QuizForm(request.POST, instance=quiz, mentor_user=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, f'Quiz "{quiz.title}" updated.')
            return redirect('mentor_quizzes')
    else:
        form = QuizForm(instance=quiz, mentor_user=request.user)

    return render(request, 'mentor_quiz_form.html', {'form': form, 'is_edit': True, 'quiz': quiz})


@login_required
@role_required(Profile.ROLE_MENTOR)
def mentor_quiz_delete(request, pk):
    quiz = get_object_or_404(Quiz, pk=pk, created_by=request.user)
    if request.method == 'POST':
        title = quiz.title
        quiz.delete()
        messages.success(request, f'Quiz "{title}" deleted.')
        return redirect('mentor_quizzes')
    return render(request, 'mentor_confirm_delete.html', {'object_label': f'quiz "{quiz.title}"', 'cancel_url': 'mentor_quizzes'})


@login_required
@role_required(Profile.ROLE_MENTOR)
def mentor_quiz_questions(request, pk):
    """List existing questions for a quiz the Mentor owns, and add new ones."""
    quiz = get_object_or_404(Quiz, pk=pk, created_by=request.user)
    questions = quiz.questions.prefetch_related('choices').all()

    if request.method == 'POST':
        form = QuestionForm(request.POST)
        if form.is_valid():
            question = Question.objects.create(quiz=quiz, text=form.cleaned_data['text'])
            correct = form.cleaned_data['correct_choice']
            for i in range(1, 5):
                Choice.objects.create(
                    question=question,
                    text=form.cleaned_data[f'choice_{i}'],
                    is_correct=(str(i) == correct),
                )
            messages.success(request, 'Question added.')
            return redirect('mentor_quiz_questions', pk=quiz.pk)
    else:
        form = QuestionForm()

    return render(request, 'mentor_quiz_questions.html', {
        'quiz': quiz, 'questions': questions, 'form': form,
    })


@login_required
@role_required(Profile.ROLE_MENTOR)
def mentor_question_delete(request, pk, question_id):
    quiz = get_object_or_404(Quiz, pk=pk, created_by=request.user)
    question = get_object_or_404(Question, pk=question_id, quiz=quiz)
    if request.method == 'POST':
        question.delete()
        messages.success(request, 'Question deleted.')
    return redirect('mentor_quiz_questions', pk=quiz.pk)


# ===========================================================
# ADMIN — Account management
# ===========================================================
@login_required
@role_required(Profile.ROLE_ADMIN)
def admin_user_list(request):
    query = request.GET.get('q', '').strip()

    users = User.objects.select_related('profile').all().order_by('-date_joined')
    if query:
        users = users.filter(Q(username__icontains=query) | Q(email__icontains=query))

    # Duplicate-account detection: flag any email address used by more than
    # one account (ignoring blank emails). Per spec §2 / §6.
    duplicate_emails = set(
        User.objects.exclude(email='')
        .values('email')
        .annotate(c=Count('id'))
        .filter(c__gt=1)
        .values_list('email', flat=True)
    )

    return render(request, 'admin_users.html', {
        'users': users,
        'query': query,
        'duplicate_emails': duplicate_emails,
    })


@login_required
@role_required(Profile.ROLE_ADMIN)
def admin_user_detail(request, user_id):
    target = get_object_or_404(User.objects.select_related('profile'), pk=user_id)
    return render(request, 'admin_user_detail.html', {'target': target})


@login_required
@role_required(Profile.ROLE_ADMIN)
def admin_user_toggle_active(request, user_id):
    target = get_object_or_404(User, pk=user_id)

    if target == request.user:
        messages.error(request, "You cannot deactivate your own account.")
        return redirect('admin_user_detail', user_id=user_id)

    if request.method == 'POST':
        target.is_active = not target.is_active
        target.save(update_fields=['is_active'])
        state = "activated" if target.is_active else "deactivated"
        messages.success(request, f'Account "{target.username}" {state}.')

    return redirect('admin_user_detail', user_id=user_id)


@login_required
@role_required(Profile.ROLE_ADMIN)
def admin_user_delete(request, user_id):
    target = get_object_or_404(User, pk=user_id)

    if target == request.user:
        messages.error(request, "You cannot delete your own account.")
        return redirect('admin_user_detail', user_id=user_id)

    if request.method == 'POST':
        username = target.username
        target.delete()
        messages.success(request, f'Account "{username}" deleted.')
        return redirect('admin_user_list')

    return render(request, 'admin_confirm_delete.html', {'target': target})


@login_required
@role_required(Profile.ROLE_ADMIN)
def admin_mentor_create(request):
    if request.method == 'POST':
        form = MentorCreateForm(request.POST)
        if form.is_valid():
            username = form.cleaned_data['username']
            email = form.cleaned_data['email']
            password = form.cleaned_data['password']

            if User.objects.filter(username=username).exists():
                messages.error(request, 'That username is already taken.')
            elif email and User.objects.filter(email__iexact=email).exists():
                messages.error(request, 'This email address is already registered.')
            else:
                user = User.objects.create_user(username=username, email=email, password=password)
                # Profile auto-created by the signal with role=STUDENT by default;
                # override it here since an Admin is explicitly creating a Mentor.
                user.profile.role = Profile.ROLE_MENTOR
                user.profile.save(update_fields=['role'])
                messages.success(request, f'Mentor account "{username}" created.')
                return redirect('admin_user_list')
    else:
        form = MentorCreateForm()

    return render(request, 'admin_mentor_create.html', {'form': form})


# ===========================================================
# EMAIL VERIFICATION
# ===========================================================
@login_required
def verify_email_view(request):
    profile, _ = Profile.objects.get_or_create(user=request.user)

    if profile.is_verified:
        return redirect('dashboard_router')

    if request.method == 'POST':
        form = OTPForm(request.POST)
        if form.is_valid():
            success, error = verify_otp(request.user, OTPCode.PURPOSE_VERIFY_EMAIL, form.cleaned_data['code'])
            if success:
                profile.is_verified = True
                profile.save(update_fields=['is_verified'])
                messages.success(request, 'Your account is verified!')
                return redirect('dashboard_router')
            else:
                messages.error(request, error)
    else:
        form = OTPForm()

    return render(request, 'verify_email.html', {
        'form': form,
        'email': request.user.email,
        'resend_wait': seconds_until_resend(request.user, OTPCode.PURPOSE_VERIFY_EMAIL),
    })


@login_required
def resend_verification_otp(request):
    profile, _ = Profile.objects.get_or_create(user=request.user)
    if profile.is_verified:
        return redirect('dashboard_router')

    if request.method == 'POST':
        if can_resend(request.user, OTPCode.PURPOSE_VERIFY_EMAIL):
            sent = generate_and_send_otp(request.user, OTPCode.PURPOSE_VERIFY_EMAIL)
            if sent:
                messages.success(request, 'A new code was sent to your email.')
            else:
                messages.error(request, 'Could not send the email right now. Please try again in a moment.')
        else:
            wait = seconds_until_resend(request.user, OTPCode.PURPOSE_VERIFY_EMAIL)
            messages.error(request, f'Please wait {wait} seconds before requesting another code.')

    return redirect('verify_email')


# ===========================================================
# FORGOT PASSWORD (OTP-based)
# ===========================================================
def forgot_password_request_view(request):
    """Step 1: user enters username or email. We deliberately show the
    SAME message whether or not the account exists, per spec §13 —
    this avoids revealing which usernames/emails are registered."""
    if request.method == 'POST':
        form = ForgotPasswordRequestForm(request.POST)
        if form.is_valid():
            identifier = form.cleaned_data['username_or_email']
            user = User.objects.filter(username=identifier).first() \
                or User.objects.filter(email__iexact=identifier).first()

            if user and user.email:
                generate_and_send_otp(user, OTPCode.PURPOSE_RESET_PASSWORD)
                request.session['reset_user_id'] = user.id

            messages.success(
                request,
                'If an account matches that username or email, a reset code has been sent.'
            )
            return redirect('reset_password_confirm')
    else:
        form = ForgotPasswordRequestForm()

    return render(request, 'forgot_password.html', {'form': form})


def reset_password_confirm_view(request):
    """Step 2: user enters the OTP code + new password together."""
    user_id = request.session.get('reset_user_id')

    if request.method == 'POST':
        form = ResetPasswordConfirmForm(request.POST)
        if form.is_valid():
            if not user_id:
                messages.error(request, 'Session expired. Please start again.')
                return redirect('forgot_password_request')

            user = get_object_or_404(User, pk=user_id)
            success, error = verify_otp(user, OTPCode.PURPOSE_RESET_PASSWORD, form.cleaned_data['code'])

            if success:
                user.set_password(form.cleaned_data['new_password'])
                user.save()
                del request.session['reset_user_id']
                messages.success(request, 'Password reset! You can now log in.')
                return redirect('login')
            else:
                messages.error(request, error)
    else:
        form = ResetPasswordConfirmForm()

    return render(request, 'reset_password.html', {'form': form})


def resend_reset_otp(request):
    user_id = request.session.get('reset_user_id')
    if request.method == 'POST' and user_id:
        user = get_object_or_404(User, pk=user_id)
        if can_resend(user, OTPCode.PURPOSE_RESET_PASSWORD):
            sent = generate_and_send_otp(user, OTPCode.PURPOSE_RESET_PASSWORD)
            if sent:
                messages.success(request, 'A new code was sent to your email.')
            else:
                messages.error(request, 'Could not send the email right now. Please try again in a moment.')
        else:
            wait = seconds_until_resend(user, OTPCode.PURPOSE_RESET_PASSWORD)
            messages.error(request, f'Please wait {wait} seconds before requesting another code.')

    return redirect('reset_password_confirm')