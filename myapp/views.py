from datetime import timedelta
from django.http import Http404
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.utils.text import slugify
from django.contrib import messages
from django.db import IntegrityError, transaction
from django.db.models import Max, Count, Q, F
from django.views.decorators.debug import sensitive_post_parameters

from .models import (
    Profile, Lesson, LessonProgress,
    Quiz, Question, Choice, QuizAttempt,
    Badge, UserBadge,
    TermsVersion, TermsAcceptance, AuditLog,
    Classroom, Enrollment, normalize_code,
    QuizAccommodation, PolicySettings, FinalWarning, Notification, AccountStatusLog,
)
from .forms import (
    UserUpdateForm, ProfileUpdateForm, LessonForm, QuizForm, QuestionForm,
    MentorCreateForm, AdminResetPasswordForm, OTPForm, ForgotPasswordRequestForm, ResetPasswordConfirmForm,
    RegisterForm, users_with_gmail, TermsPublishForm, PhotoFlagForm, ClassroomForm,
    AccommodationForm, PolicyForm,
    USERNAME_TAKEN_ERROR, EMAIL_TAKEN_ERROR,
)
from .decorators import role_required, require_verified
from .utils import (
    generate_and_send_otp, verify_otp, can_resend, seconds_until_resend,
    generate_and_send_verification_link, verify_link_token,
)
from .models import OTPCode
from django.conf import settings
from django.utils import formats
from . import exams
from . import classrooms as cls
from . import deadlines
from . import progress as prog
from . import badges as badge_rules
from .exams import local_time

# Printed at the top of paper quizzes. Change it here (or set SCHOOL_NAME in settings.py).
SCHOOL_NAME = 'COLEGIO DE SAN GABRIEL ARCANGEL'


# ---------------------------------------------------------
# Authentication
# ---------------------------------------------------------
MAX_FAILED_LOGINS = 3
GENERIC_LOGIN_ERROR = 'Invalid username or password.'
LOCKED_MESSAGE = (
    'Your account has been locked after 3 failed login attempts. '
    'Please use Forgot Password to recover your account. '
    '(Accounts created by the Admin without a Gmail: ask your Admin to unlock it.)'
)


@sensitive_post_parameters('password')
def login_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard_router')

    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')

        # Basic input validation (also avoids hashing absurdly long passwords).
        if not username or not password or len(username) > 150 or len(password) > 128:
            messages.error(request, GENERIC_LOGIN_ERROR)
            return render(request, 'login.html')

        candidate = User.objects.filter(username=username).first()
        profile = None
        if candidate is not None:
            profile, _ = Profile.objects.get_or_create(user=candidate)

        # A locked account can NOT log in, even with the correct password.
        # The only way out is Forgot Password (emailed code -> new password).
        if profile is not None and profile.is_locked:
            messages.error(request, LOCKED_MESSAGE)
            return render(request, 'login.html')

        user = authenticate(request, username=username, password=password)
        if user is not None:
            if profile is not None and profile.failed_login_attempts:
                profile.failed_login_attempts = 0
                profile.save(update_fields=['failed_login_attempts'])
            login(request, user)
            return redirect('dashboard_router')

        # Wrong password. Only real accounts have a counter; the message is the
        # same either way so it never reveals which usernames exist.
        if profile is not None:
            Profile.objects.filter(pk=profile.pk).update(
                failed_login_attempts=F('failed_login_attempts') + 1
            )
            profile.refresh_from_db()
            if profile.failed_login_attempts >= MAX_FAILED_LOGINS:
                profile.is_locked = True
                profile.locked_at = timezone.now()
                profile.save(update_fields=['is_locked', 'locked_at'])
                messages.error(request, LOCKED_MESSAGE)
                return render(request, 'login.html')

        messages.error(request, GENERIC_LOGIN_ERROR)
        return render(request, 'login.html')

    return render(request, 'login.html')


def logout_view(request):
    logout(request)
    return redirect('login')


def _is_protected_account(user):
    """True if this account must never be reclaimed/deleted by a new sign-up:
    verified accounts, staff/superusers, and anything without a profile row."""
    if user.is_staff or user.is_superuser:
        return True
    profile = Profile.objects.filter(user=user).first()
    return True if profile is None else profile.is_verified


@sensitive_post_parameters('password', 'confirm_password')
def register_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard_router')

    if request.method == 'POST':
        form = RegisterForm(request.POST)
        if form.is_valid():
            username = form.cleaned_data['username']
            email = form.cleaned_data['email']
            password = form.cleaned_data['password']

            # Who already owns this username / Gmail mailbox?
            username_owner = User.objects.filter(username__iexact=username).first()
            email_owners = users_with_gmail(email)

            # Verified (real) accounts always win. An account that was NEVER
            # verified is treated as abandoned and gets replaced, otherwise one
            # unfinished sign-up would block that username/Gmail forever.
            if username_owner and _is_protected_account(username_owner):
                form.add_error('username', USERNAME_TAKEN_ERROR)
            if any(_is_protected_account(u) for u in email_owners):
                form.add_error('email', EMAIL_TAKEN_ERROR)

            user = None
            if not form.errors:
                stale_ids = {u.pk for u in email_owners}
                if username_owner:
                    stale_ids.add(username_owner.pk)

                try:
                    with transaction.atomic():
                        if stale_ids:
                            User.objects.filter(pk__in=stale_ids).delete()
                        user = User.objects.create_user(username=username, email=email, password=password)
                        # Profile is auto-created by the post_save signal in signals.py.
                        # Force it unverified: the account cannot be used until the
                        # confirmation link emailed below is clicked.
                        user.profile.is_verified = False
                        user.profile.save(update_fields=['is_verified'])
                        # Consent is saved together with the account (all or nothing).
                        shown_terms = TermsVersion.current()
                        if shown_terms is not None:
                            TermsAcceptance.objects.create(user=user, terms=shown_terms)
                except IntegrityError:
                    # Two sign-ups for the same username at the same instant
                    # (e.g. double-click). The second one is simply told it's taken.
                    user = None
                    form.add_error('username', USERNAME_TAKEN_ERROR)

            if user is not None:
                sent = generate_and_send_verification_link(request, user)

                login(request, user)  # session started, but verification still required
                if sent:
                    messages.success(request, f'A confirmation link was sent to {email}. Click it to activate your account.')
                else:
                    messages.warning(
                        request,
                        'Account created, but we couldn\'t send the confirmation email right now. '
                        'Use the "Resend link" button below to try again.'
                    )
                return redirect('verify_email')
    else:
        form = RegisterForm()

    return render(request, 'register.html', {'form': form, 'terms': TermsVersion.current()})


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
@local_time
def student_dashboard(request):
    profile, _ = Profile.objects.get_or_create(user=request.user)
    my_classrooms = (
        cls.viewable_classrooms(request.user)
        .select_related('mentor__profile')
        .annotate(
            lesson_total=Count('lessons', filter=Q(lessons__is_published=True), distinct=True),
            quiz_total=Count('quizzes', filter=Q(quizzes__is_published=True), distinct=True),
        )
    )
    return render(request, 'home.html', {
        'profile': profile, 'my_classrooms': my_classrooms,
        'deadline_rows': _deadline_rows(request.user),
    })


@login_required
@role_required(Profile.ROLE_MENTOR)
def mentor_dashboard(request):
    profile, _ = Profile.objects.get_or_create(user=request.user)
    cls.ensure_default_classroom(request.user)
    my_lessons = Lesson.objects.filter(created_by=request.user)
    my_quizzes = Quiz.objects.filter(created_by=request.user)
    my_classrooms = _mentor_classroom_cards(request.user)
    return render(request, 'mentor_dashboard.html', {
        'my_classrooms': my_classrooms,
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
    """Only lessons from classrooms this person may see, grouped by classroom."""
    lessons = list(cls.visible_lessons(request.user).select_related('classroom__mentor__profile').order_by('classroom__name', 'order'))
    completed_ids = set(
        LessonProgress.objects.filter(user=request.user, completed=True)
        .values_list('lesson_id', flat=True)
    )
    groups = {}
    for lesson in lessons:
        lesson.is_completed = lesson.id in completed_ids
        groups.setdefault(lesson.classroom, []).append(lesson)

    return render(request, 'lesson_list.html', {
        'lessons': lessons,
        'groups': [{'classroom': c, 'lessons': l} for c, l in groups.items()],
    })


@login_required
def lesson_detail(request, slug):
    # 404 (not 403) for a lesson outside the person's classrooms, so its existence is not revealed.
    lesson = get_object_or_404(cls.visible_lessons(request.user), slug=slug)
    progress, _ = LessonProgress.objects.get_or_create(user=request.user, lesson=lesson)

    if request.method == 'POST' and request.POST.get('action') == 'mark_complete':
        progress.completed = True
        progress.completed_at = timezone.now()
        progress.save()

        profile, _ = Profile.objects.get_or_create(user=request.user)
        profile.update_streak()
        check_and_award_badges(request.user)

        messages.success(request, f'"{lesson.title}" marked as complete!')
        return redirect('lesson_detail', slug=slug)

    return render(request, 'lesson_detail.html', {'lesson': lesson, 'progress': progress})


# ---------------------------------------------------------
# Quizzes (STUDENT-FACING — published only)
# ---------------------------------------------------------
@login_required
@local_time
def quizzes_view(request):
    quizzes = list(cls.visible_quizzes(request.user).select_related('classroom__mentor__profile').order_by('classroom__name', 'id'))

    best_scores = dict(
        QuizAttempt.objects.filter(user=request.user, quiz__in=quizzes)
        .values('quiz_id')
        .annotate(best=Max('score'))
        .values_list('quiz_id', 'best')
    )
    # Only THIS student's own attempts are ever looked at.
    firsts = exams.first_attempts_by_quiz(request.user, quizzes)
    accs = deadlines.accommodations_for(request.user, quizzes)

    now = timezone.now()
    for quiz in quizzes:
        quiz.best_score = best_scores.get(quiz.id)
        quiz.state = exams.student_quiz_state(quiz, firsts.get(quiz.id), now, accs.get(quiz.id))

    groups = {}
    for quiz in quizzes:
        groups.setdefault(quiz.classroom, []).append(quiz)
    return render(request, 'quizzes.html', {
        'quizzes': quizzes,
        'groups': [{'classroom': c, 'quizzes': q} for c, q in groups.items()],
    })


def _read_start_time(request, quiz):
    """When this student first opened the quiz (kept in their session)."""
    raw = request.session.get(f'quiz_started_{quiz.id}')
    if not raw:
        return None
    try:
        from datetime import datetime, timezone as dt_timezone
        dt = datetime.fromisoformat(raw)
        return dt if timezone.is_aware(dt) else dt.replace(tzinfo=dt_timezone.utc)
    except (TypeError, ValueError):
        return None


@login_required
@local_time
def quiz_detail(request, quiz_id):
    quiz = get_object_or_404(cls.visible_quizzes(request.user), id=quiz_id)
    now = timezone.now()

    # Not open yet: nobody can read or submit it before the Mentor's start time.
    if quiz.is_not_open_yet(now):
        opens = formats.date_format(timezone.localtime(quiz.start_datetime), 'M j, Y \u2013 g:i A')
        messages.error(request, f'"{quiz.title}" opens on {opens}.')
        return redirect('quizzes')

    questions = quiz.questions.prefetch_related('choices').all()
    session_key = f'quiz_started_{quiz.id}'

    # Deadline rule (students only): after the deadline nobody can submit unless the
    # Mentor allows late work or gave this student an extension. Checked on the server.
    role = Profile.objects.filter(user=request.user).values_list('role', flat=True).first()
    if role == Profile.ROLE_STUDENT:
        allowed, student_deadline, closed_reason = deadlines.submission_status(quiz, request.user, now)
    else:
        allowed, student_deadline, closed_reason = True, quiz.deadline_datetime, ''
    acc = QuizAccommodation.objects.filter(quiz=quiz, student=request.user).first()

    if not allowed:
        first = exams.first_attempts_by_quiz(request.user, [quiz]).get(quiz.id)
        return render(request, 'quiz_detail.html', {
            'quiz': quiz, 'questions': [], 'closed': True, 'closed_reason': closed_reason,
            'eff_deadline': student_deadline, 'past_deadline': True,
            'state': exams.student_quiz_state(quiz, first, now, acc),
        }, status=403 if request.method == 'POST' else 200)

    if request.method == 'POST':
        correct_count = 0
        for question in questions:
            selected_id = request.POST.get(f'question_{question.id}')
            if selected_id and question.choices.filter(id=selected_id, is_correct=True).exists():
                correct_count += 1

        total = questions.count()
        score = round((correct_count / total) * 100) if total else 0
        QuizAttempt.objects.create(
            user=request.user, quiz=quiz, score=score,
            started_at=_read_start_time(request, quiz),
            correct_count=correct_count, total_questions=total,
            deadline_at_submission=student_deadline,      # the deadline that applied to this student
        )
        # taken_at (the submission time) is stamped automatically by the model.
        request.session.pop(session_key, None)

        if score >= 50:
            profile, _ = Profile.objects.get_or_create(user=request.user)
            profile.update_streak()

        check_and_award_badges(request.user)

        first = exams.first_attempts_by_quiz(request.user, [quiz]).get(quiz.id)
        return render(request, 'quiz_result.html', {
            'quiz': quiz, 'score': score, 'correct_count': correct_count, 'total': total,
            'state': exams.student_quiz_state(quiz, first, now, acc),
        })

    # Remember when the student first opened it (a refresh must not reset this).
    if session_key not in request.session:
        request.session[session_key] = now.isoformat()

    first = exams.first_attempts_by_quiz(request.user, [quiz]).get(quiz.id)
    return render(request, 'quiz_detail.html', {
        'quiz': quiz, 'questions': questions,
        'state': exams.student_quiz_state(quiz, first, now, acc),
        'eff_deadline': student_deadline,
        'past_deadline': bool(student_deadline and now > student_deadline),
    })


# ---------------------------------------------------------
# Badges
# ---------------------------------------------------------
@login_required
@local_time
def badges_view(request):
    cards = badge_rules.cards(request.user)
    data = [{'name': c['badge'].name, 'icon': c['badge'].icon_class, 'meaning': c['meaning'], 'how': c['how'],
             'earned': c['is_earned'], 'earned_at': formats.date_format(timezone.localtime(c['earned_at']), 'M j, Y') if c['earned_at'] else '',
             'target': c['target'], 'current': c['current'], 'pct': c['pct'], 'remaining': c['remaining']} for c in cards]
    return render(request, 'badges.html', {'cards': cards, 'cards_json': data})


def check_and_award_badges(user):
    """Rules live in badges.py so the page and the awarding can never disagree."""
    badge_rules.award(user)


# ---------------------------------------------------------
# Profile
# ---------------------------------------------------------
@login_required
def profile_view(request):
    profile, _ = Profile.objects.get_or_create(user=request.user)
    return render(request, 'profile.html', {'profile': profile})


@login_required
def edit_profile(request):
    # Always the logged-in user's own profile; nobody can edit someone else's.
    profile, _ = Profile.objects.get_or_create(user=request.user)

    if request.method == 'POST':
        old_email = (request.user.email or '').strip().lower()
        user_form = UserUpdateForm(request.POST, instance=request.user)
        profile_form = ProfileUpdateForm(request.POST, request.FILES, instance=profile)

        if user_form.is_valid() and profile_form.is_valid():
            user_form.save()
            saved_profile = profile_form.save(commit=False)
            if 'avatar' in request.FILES and saved_profile.avatar_flagged:
                # A replacement photo was uploaded: the flag is lifted and the
                # Admin can still review the new photo.
                saved_profile.avatar_flagged = False
                saved_profile.avatar_flag_reason = ''
                saved_profile.avatar_flagged_at = None
                AuditLog.record(request.user, 'photo_replaced', request.user, 'Student uploaded a replacement photo.')
            saved_profile.save()

            # A new email address has not been proven yet, so it goes through
            # the same confirmation-link step as a new account.
            new_email = (request.user.email or '').strip().lower()
            if new_email != old_email:
                profile.is_verified = False
                profile.save(update_fields=['is_verified'])
                sent = generate_and_send_verification_link(request, request.user)
                if sent:
                    messages.success(request, f'Profile updated. We sent a confirmation link to {request.user.email}. Click it to verify your new Gmail address.')
                else:
                    messages.warning(request, 'Profile updated, but we couldn\'t send the confirmation email. Use the "Resend link" button to try again.')
                return redirect('verify_email')

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
    lessons = Lesson.objects.filter(created_by=request.user).select_related('classroom').order_by('order')
    return render(request, 'mentor_lessons.html', {'lessons': lessons})


@login_required
@role_required(Profile.ROLE_MENTOR)
def mentor_lesson_create(request):
    cls.ensure_default_classroom(request.user)
    if request.method == 'POST':
        form = LessonForm(request.POST, mentor_user=request.user)
        if form.is_valid():
            lesson = form.save(commit=False)
            lesson.created_by = request.user
            lesson.slug = _unique_slug(lesson.title, Lesson)
            lesson.save()
            messages.success(request, f'Lesson "{lesson.title}" created.')
            return redirect('mentor_lessons')
    else:
        form = LessonForm(mentor_user=request.user, initial=_preselected_classroom(request))

    return render(request, 'mentor_lesson_form.html', {'form': form, 'is_edit': False})


@login_required
@role_required(Profile.ROLE_MENTOR)
def mentor_lesson_edit(request, pk):
    lesson = get_object_or_404(Lesson, pk=pk, created_by=request.user)

    if request.method == 'POST':
        form = LessonForm(request.POST, instance=lesson, mentor_user=request.user)
        if form.is_valid():
            updated = form.save(commit=False)
            if updated.title != lesson.title:
                updated.slug = _unique_slug(updated.title, Lesson, exclude_pk=lesson.pk)
            updated.save()
            messages.success(request, f'Lesson "{updated.title}" updated.')
            return redirect('mentor_lessons')
    else:
        form = LessonForm(instance=lesson, mentor_user=request.user)

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
@local_time
def mentor_quizzes(request):
    quizzes = Quiz.objects.filter(created_by=request.user).select_related('classroom', 'lesson')
    return render(request, 'mentor_quizzes.html', {'quizzes': quizzes})


@login_required
@role_required(Profile.ROLE_MENTOR)
@local_time
def mentor_quiz_create(request):
    cls.ensure_default_classroom(request.user)
    if request.method == 'POST':
        form = QuizForm(request.POST, mentor_user=request.user)
        if form.is_valid():
            quiz = form.save(commit=False)
            quiz.created_by = request.user
            quiz.save()
            messages.success(request, f'Quiz "{quiz.title}" created. Now add some questions.')
            return redirect('mentor_quiz_questions', pk=quiz.pk)
    else:
        form = QuizForm(mentor_user=request.user, initial=_preselected_classroom(request))

    return render(request, 'mentor_quiz_form.html', {'form': form, 'is_edit': False})


@login_required
@role_required(Profile.ROLE_MENTOR)
@local_time
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

    required = TermsVersion.required()
    accepted_ids = set()
    if required:
        accepted_ids = set(
            TermsAcceptance.objects.filter(terms__id__gte=required.id).values_list('user_id', flat=True)
        )
    users = list(users)
    for u in users:
        u.terms_accepted = u.pk in accepted_ids

    return render(request, 'admin_users.html', {
        'users': users,
        'query': query,
        'duplicate_emails': duplicate_emails,
    })


@login_required
@role_required(Profile.ROLE_ADMIN)
def admin_user_detail(request, user_id):
    target = get_object_or_404(User.objects.select_related('profile'), pk=user_id)
    required = TermsVersion.required()
    accepted_current = bool(required) and TermsAcceptance.objects.filter(
        user=target, terms__id__gte=required.id).exists()
    return render(request, 'admin_user_detail.html', {
        'target': target,
        'is_student': getattr(target.profile, 'role', None) == Profile.ROLE_STUDENT,
        'required_terms': required,
        'accepted_current_terms': accepted_current,
        'acceptances': TermsAcceptance.objects.filter(user=target).select_related('terms')[:5],
        'photo_form': PhotoFlagForm(),
        'status_log': AccountStatusLog.objects.filter(user=target).select_related('actor__profile')[:5],
    })


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
def admin_user_unlock(request, user_id):
    target = get_object_or_404(User, pk=user_id)
    if request.method == 'POST':
        Profile.objects.filter(user=target).update(
            is_locked=False, failed_login_attempts=0, locked_at=None
        )
        messages.success(request, f'Account "{target.username}" unlocked.')
    return redirect('admin_user_detail', user_id=user_id)


@sensitive_post_parameters('new_password', 'confirm_password')
@login_required
@role_required(Profile.ROLE_ADMIN)
def admin_user_reset_password(request, user_id):
    target = get_object_or_404(User.objects.select_related('profile'), pk=user_id)

    # Superuser accounts are not reset from here (use the profile page / shell).
    if target.is_superuser or target == request.user:
        messages.error(request, 'Use My Profile to manage your own or a superuser account.')
        return redirect('admin_user_detail', user_id=user_id)

    if request.method == 'POST':
        form = AdminResetPasswordForm(request.POST)
        if form.is_valid():
            target.set_password(form.cleaned_data['new_password'])
            target.save(update_fields=['password'])
            Profile.objects.filter(user=target).update(
                is_locked=False, failed_login_attempts=0, locked_at=None
            )
            messages.success(request, f'Password for "{target.username}" changed and the account unlocked.')
            return redirect('admin_user_detail', user_id=user_id)
    else:
        form = AdminResetPasswordForm()

    return render(request, 'admin_reset_password.html', {'target': target, 'form': form})


@sensitive_post_parameters('password')
@login_required
@role_required(Profile.ROLE_ADMIN)
def admin_mentor_create(request):
    if request.method == 'POST':
        form = MentorCreateForm(request.POST)
        if form.is_valid():
            username = form.cleaned_data['username']
            password = form.cleaned_data['password']

            if User.objects.filter(username__iexact=username).exists():
                form.add_error('username', USERNAME_TAKEN_ERROR)
            else:
                # No Gmail needed: the Admin creates this account, so there is
                # no sign-up email to verify (is_verified defaults to True).
                user = User.objects.create_user(username=username, password=password)
                # Profile auto-created by the signal with role=STUDENT by default;
                # override it here since an Admin is explicitly creating a Mentor.
                user.profile.role = Profile.ROLE_MENTOR
                user.profile.save(update_fields=['role'])
                cls.ensure_default_classroom(user)   # every Mentor starts with a classroom
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
    """Waiting page shown after registration — tells the user to check
    their email and click the confirmation link. No code to type here."""
    profile, _ = Profile.objects.get_or_create(user=request.user)

    if profile.is_verified:
        return redirect('dashboard_router')

    return render(request, 'verify_email.html', {
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
            sent = generate_and_send_verification_link(request, request.user)
            if sent:
                messages.success(request, 'A new confirmation link was sent to your email.')
            else:
                messages.error(request, 'Could not send the email right now. Please try again in a moment.')
        else:
            wait = seconds_until_resend(request.user, OTPCode.PURPOSE_VERIFY_EMAIL)
            messages.error(request, f'Please wait {wait} seconds before requesting another link.')

    return redirect('verify_email')


def verify_email_confirm_view(request, user_id, token):
    """The link the user clicks from their email. Works even if they're
    not logged in on this browser/device — identifies the account by
    user_id in the URL, proves ownership via the token."""
    user = get_object_or_404(User, pk=user_id)
    profile, _ = Profile.objects.get_or_create(user=user)

    if profile.is_verified:
        messages.info(request, 'This account is already verified. You can log in.')
        return redirect('login')

    success, error = verify_link_token(user, token)

    if success:
        profile.is_verified = True
        profile.save(update_fields=['is_verified'])
        if request.user.is_authenticated and request.user.id == user.id:
            messages.success(request, 'Your account is verified!')
            return redirect('dashboard_router')
        else:
            messages.success(request, 'Your account is verified! You can now log in.')
            return redirect('login')
    else:
        messages.error(request, error)
        return redirect('login')


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


@sensitive_post_parameters('new_password', 'confirm_password', 'code')
def reset_password_confirm_view(request):
    """Step 2: user enters the OTP code + new password together.
    A successful reset also UNLOCKS an account that was locked by 3 failed logins."""
    user_id = request.session.get('reset_user_id')

    if request.method == 'POST':
        form = ResetPasswordConfirmForm(request.POST)
        if form.is_valid():
            if not user_id:
                # Same wording as a wrong code, so this page can't be used to
                # discover which accounts exist.
                messages.error(request, 'The code is incorrect or has expired. Please request a new one.')
                return redirect('forgot_password_request')

            user = get_object_or_404(User, pk=user_id)
            success, error = verify_otp(user, OTPCode.PURPOSE_RESET_PASSWORD, form.cleaned_data['code'])

            if success:
                user.set_password(form.cleaned_data['new_password'])
                user.save()

                # Verified by email code -> unlock the account.
                profile, _ = Profile.objects.get_or_create(user=user)
                profile.failed_login_attempts = 0
                profile.is_locked = False
                profile.locked_at = None
                profile.save(update_fields=['failed_login_attempts', 'is_locked', 'locked_at'])

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


# ===========================================================
# EXAM MONITORING + PRINTING
# ===========================================================
def _progress_context(request, quiz, back_url, is_admin):
    show = request.GET.get('show', 'all')
    if show not in dict(exams.FILTERS):
        show = 'all'
    data = exams.build_progress(quiz, show)
    summary = exams.quiz_summary(quiz) if show != 'all' else data['summary']
    mentor = quiz.created_by
    return {
        'quiz': quiz,
        'rows': data['rows'],
        'summary': summary,
        'show': show,
        'filters': exams.FILTERS,
        'back_url': back_url,
        'is_admin': is_admin,
        'mentor_name': mentor.profile.display_name if mentor else None,
        'past_deadline': quiz.is_past_deadline(),
        'question_count': quiz.total_questions,
    }


@login_required
@role_required(Profile.ROLE_MENTOR)
@local_time
def mentor_progress_overview(request):
    """All of THIS Mentor's quizzes with a one-line summary each."""
    quizzes = Quiz.objects.filter(created_by=request.user).order_by('-id')
    items = [{'quiz': q, 'summary': exams.quiz_summary(q)} for q in quizzes]
    return render(request, 'mentor_progress.html', {'items': items})


@login_required
@role_required(Profile.ROLE_MENTOR)
@local_time
def mentor_exam_progress(request, pk):
    # Own quizzes only: another Mentor's quiz id returns 404.
    quiz = get_object_or_404(Quiz, pk=pk, created_by=request.user)
    ctx = _progress_context(request, quiz, back_url='mentor_progress_overview', is_admin=False)
    return render(request, 'exam_progress.html', ctx)


@login_required
@role_required(Profile.ROLE_MENTOR, Profile.ROLE_ADMIN)
@local_time
def mentor_quiz_print(request, pk):
    """Clean, paper-ready copy of a quiz. A Mentor can only print their own
    quizzes; an Admin may print any. ?key=1 prints the answer key instead."""
    profile = get_object_or_404(Profile, user=request.user)
    if profile.role == Profile.ROLE_ADMIN:
        quiz = get_object_or_404(Quiz, pk=pk)
    else:
        quiz = get_object_or_404(Quiz, pk=pk, created_by=request.user)

    show_key = request.GET.get('key') == '1'
    questions = []
    for number, q in enumerate(quiz.questions.prefetch_related('choices').order_by('id'), start=1):
        choices = []
        for letter, c in zip('ABCDEFGHIJ', q.choices.all().order_by('id')):
            choices.append({'letter': letter, 'text': c.text, 'correct': c.is_correct})
        questions.append({'number': number, 'text': q.text, 'choices': choices})

    return render(request, 'quiz_print.html', {
        'quiz': quiz,
        'questions': questions,
        'show_key': show_key,
        'school_name': getattr(settings, 'SCHOOL_NAME', SCHOOL_NAME),
        'subject': quiz.lesson.title if quiz.lesson else '',
        'instructions': (quiz.instructions or '').strip()
                        or 'Read each question carefully and select the best answer.',
        'total': len(questions),
    })


# ===========================================================
# TERMS AND CONDITIONS
# ===========================================================
def terms_view(request):
    """Read-only copy of the current Terms. Public, so it can be opened from the
    registration page before an account exists."""
    return render(request, 'terms.html', {'terms': TermsVersion.current()})


@login_required
def terms_accept_view(request):
    """Students must tick the box and press Accept. Nothing is pre-ticked."""
    profile, _ = Profile.objects.get_or_create(user=request.user)
    if profile.role != Profile.ROLE_STUDENT:
        return redirect('dashboard_router')
    if TermsVersion.student_has_accepted(request.user):
        return redirect('dashboard_router')

    terms = TermsVersion.current()
    error = None
    if request.method == 'POST':
        if request.POST.get('agree') != 'on':
            error = 'Please tick the box to confirm that you have read and accept the Terms and Conditions.'
        else:
            TermsAcceptance.objects.get_or_create(user=request.user, terms=terms)
            messages.success(request, f'Thank you. You accepted the Terms and Conditions (version {terms.version}).')
            return redirect('dashboard_router')

    return render(request, 'terms_accept.html', {'terms': terms, 'error': error})


@login_required
@role_required(Profile.ROLE_ADMIN)
def admin_terms_list(request):
    required = TermsVersion.required()
    students = Profile.objects.filter(role=Profile.ROLE_STUDENT).count()
    accepted = 0
    if required:
        accepted = TermsAcceptance.objects.filter(
            terms__id__gte=required.id, user__profile__role=Profile.ROLE_STUDENT
        ).values('user').distinct().count()
    versions = TermsVersion.objects.annotate(accept_count=Count('acceptances'))
    return render(request, 'admin_terms.html', {
        'versions': versions,
        'required': required,
        'students': students,
        'accepted': accepted,
        'waiting': max(students - accepted, 0),
    })


@login_required
@role_required(Profile.ROLE_ADMIN)
def admin_terms_publish(request):
    latest = TermsVersion.current()
    if request.method == 'POST':
        form = TermsPublishForm(request.POST)
        if form.is_valid():
            terms = TermsVersion.objects.create(
                version=form.cleaned_data['version'],
                title=form.cleaned_data['title'],
                content=form.cleaned_data['content'],
                requires_reacceptance=form.cleaned_data['requires_reacceptance'],
                published_by=request.user,
            )
            AuditLog.record(
                request.user, 'terms_published', None,
                f"Version {terms.version} ({'major, re-acceptance required' if terms.requires_reacceptance else 'minor'})",
            )
            messages.success(request, f'Version {terms.version} published.' + (
                ' Students must accept it before they continue.' if terms.requires_reacceptance else ''))
            return redirect('admin_terms_list')
    else:
        form = TermsPublishForm(initial={'content': latest.content if latest else ''})
    return render(request, 'admin_terms_publish.html', {'form': form, 'latest': latest})


@login_required
@role_required(Profile.ROLE_ADMIN)
def admin_user_flag_photo(request, user_id):
    """Admin asks a student to replace an inappropriate profile photo.
    Nothing is deactivated; the student sees a notice and can upload a new one."""
    target = get_object_or_404(User.objects.select_related('profile'), pk=user_id)
    if request.method != 'POST':
        return redirect('admin_user_detail', user_id=user_id)
    if target.profile.role != Profile.ROLE_STUDENT or not target.profile.avatar:
        messages.error(request, 'Only a student who has uploaded a photo can be flagged.')
        return redirect('admin_user_detail', user_id=user_id)

    form = PhotoFlagForm(request.POST)
    if form.is_valid():
        profile = target.profile
        profile.avatar_flagged = True
        profile.avatar_flag_reason = form.cleaned_data['reason']
        profile.avatar_flagged_at = timezone.now()
        profile.save(update_fields=['avatar_flagged', 'avatar_flag_reason', 'avatar_flagged_at'])
        AuditLog.record(request.user, 'photo_flagged', target, form.cleaned_data['reason'])
        messages.success(request, f'{target.username} was asked to replace the profile photo.')
    else:
        messages.error(request, ' '.join(form.errors.get('reason', ['Please give a reason.'])))
    return redirect('admin_user_detail', user_id=user_id)


@login_required
@role_required(Profile.ROLE_ADMIN)
def admin_user_clear_photo_flag(request, user_id):
    target = get_object_or_404(User.objects.select_related('profile'), pk=user_id)
    if request.method == 'POST' and target.profile.avatar_flagged:
        profile = target.profile
        profile.avatar_flagged = False
        profile.avatar_flag_reason = ''
        profile.avatar_flagged_at = None
        profile.save(update_fields=['avatar_flagged', 'avatar_flag_reason', 'avatar_flagged_at'])
        AuditLog.record(request.user, 'photo_flag_cleared', target, 'Photo approved.')
        messages.success(request, 'Photo flag cleared.')
    return redirect('admin_user_detail', user_id=user_id)


# ===========================================================
# CLASSROOMS
# ===========================================================
def _preselected_classroom(request):
    """?classroom=<id> from a classroom page pre-fills the form (own classrooms only)."""
    raw = request.GET.get('classroom', '')
    if raw.isdigit() and Classroom.objects.filter(pk=int(raw), mentor=request.user).exists():
        return {'classroom': int(raw)}
    return {}


def _mentor_classroom_cards(user):
    return (
        Classroom.objects.filter(mentor=user)
        .annotate(
            student_total=Count('enrollments', filter=Q(enrollments__is_active=True), distinct=True),
            lesson_total=Count('lessons', distinct=True),
            quiz_total=Count('quizzes', distinct=True),
        )
    )


# ---------------- Student ----------------
@login_required
@role_required(Profile.ROLE_STUDENT)
def classroom_join(request):
    """Join a classroom with a code. Two steps: (1) enter the code, (2) confirm
    the classroom name and mentor, then join. Everything is checked on the server."""
    code_value = ''
    error = None
    preview = None

    if request.method == 'POST':
        code_value = (request.POST.get('code') or '').strip()
        code = normalize_code(code_value)

        if cls.join_is_blocked(request.user):
            error = 'Too many wrong codes. Please wait a few minutes and ask your mentor to confirm the code.'
        else:
            room = None
            if code:
                room = (Classroom.objects.select_related('mentor__profile')
                        .filter(code=code, code_enabled=True, mentor__is_active=True).first())
            if room is None:
                cls.join_record_failure(request.user)
                # One message for wrong, regenerated or switched-off codes. Nothing about other classrooms is revealed.
                error = 'That code is not valid or is no longer active. Check it with your mentor and try again.'
            else:
                existing = Enrollment.objects.filter(classroom=room, student=request.user).first()
                if existing and existing.is_active:
                    messages.info(request, f'You are already in "{room.name}".')
                    return redirect('classroom_detail', pk=room.pk)
                if existing and not existing.is_active:
                    error = 'You were removed from this classroom. Please ask your mentor to add you back.'
                elif request.POST.get('confirm') == '1':
                    with transaction.atomic():
                        _, created = Enrollment.objects.get_or_create(classroom=room, student=request.user)
                    cls.join_clear_failures(request.user)
                    messages.success(request, f'You joined "{room.name}" (Mentor: {room.mentor_name}).')
                    return redirect('classroom_detail', pk=room.pk)
                else:
                    preview = room

    return render(request, 'classroom_join.html', {'code': code_value, 'error': error, 'preview': preview})


@login_required
@role_required(Profile.ROLE_STUDENT)
@local_time
def classroom_detail(request, pk):
    """One classroom: its lessons and quizzes. Not a member -> 404 (never reveals it exists)."""
    room = get_object_or_404(cls.viewable_classrooms(request.user).select_related('mentor__profile'), pk=pk)

    lessons = list(room.lessons.filter(is_published=True).order_by('order'))
    done = set(LessonProgress.objects.filter(user=request.user, completed=True, lesson__in=lessons)
               .values_list('lesson_id', flat=True))
    for lesson in lessons:
        lesson.is_completed = lesson.id in done

    quizzes = list(room.quizzes.filter(is_published=True).order_by('id'))
    best = dict(
        QuizAttempt.objects.filter(user=request.user, quiz__in=quizzes)
        .values('quiz_id').annotate(b=Max('score')).values_list('quiz_id', 'b')
    )
    firsts = exams.first_attempts_by_quiz(request.user, quizzes)
    accs = deadlines.accommodations_for(request.user, quizzes)
    now = timezone.now()
    for quiz in quizzes:
        quiz.best_score = best.get(quiz.id)
        quiz.state = exams.student_quiz_state(quiz, firsts.get(quiz.id), now, accs.get(quiz.id))

    return render(request, 'classroom_detail.html', {
        'room': room, 'lessons': lessons, 'quizzes': quizzes,
        'done_count': sum(1 for l in lessons if l.is_completed),
    })


# ---------------- Mentor ----------------
@login_required
@role_required(Profile.ROLE_MENTOR)
def mentor_classrooms(request):
    cls.ensure_default_classroom(request.user)
    return render(request, 'mentor_classrooms.html', {'classrooms': _mentor_classroom_cards(request.user)})


@login_required
@role_required(Profile.ROLE_MENTOR)
def mentor_classroom_create(request):
    if request.method == 'POST':
        form = ClassroomForm(request.POST)
        if form.is_valid():
            room = form.save(commit=False)
            room.mentor = request.user
            room.save()
            messages.success(request, f'Classroom "{room.name}" created. Share its code with your students.')
            return redirect('mentor_classroom_detail', pk=room.pk)
    else:
        form = ClassroomForm()
    return render(request, 'mentor_classroom_form.html', {'form': form, 'is_edit': False})


@login_required
@role_required(Profile.ROLE_MENTOR)
def mentor_classroom_edit(request, pk):
    room = get_object_or_404(Classroom, pk=pk, mentor=request.user)
    if request.method == 'POST':
        form = ClassroomForm(request.POST, instance=room)
        if form.is_valid():
            form.save()
            messages.success(request, 'Classroom updated.')
            return redirect('mentor_classroom_detail', pk=room.pk)
    else:
        form = ClassroomForm(instance=room)
    return render(request, 'mentor_classroom_form.html', {'form': form, 'is_edit': True, 'room': room})


@login_required
@role_required(Profile.ROLE_MENTOR)
@local_time
def mentor_classroom_detail(request, pk):
    room = get_object_or_404(Classroom, pk=pk, mentor=request.user)   # another Mentor's classroom -> 404
    return render(request, 'classroom_manage.html', _manage_context(room, is_admin=False))


def _manage_context(room, is_admin):
    enrollments = list(room.enrollments.select_related('student__profile').order_by('-is_active', 'student__username'))
    quizzes = list(room.quizzes.order_by('-id'))
    items = [{'quiz': q, 'summary': exams.quiz_summary(q)} for q in quizzes]
    return {
        'room': room, 'is_admin': is_admin, 'enrollments': enrollments,
        'active_count': sum(1 for e in enrollments if e.is_active),
        'lessons': room.lessons.order_by('order'),
        'quiz_items': items,
        'back_url': 'admin_classroom_list' if is_admin else 'mentor_classrooms',
    }


# ---------------- Mentor + Admin shared actions (POST only) ----------------
def _staff_classroom_or_404(request, pk):
    """Admin may act on any classroom; a Mentor only on their own (else 404)."""
    profile = get_object_or_404(Profile, user=request.user)
    if profile.role == Profile.ROLE_ADMIN:
        return get_object_or_404(Classroom, pk=pk), True
    return get_object_or_404(Classroom, pk=pk, mentor=request.user), False


def _back_to_classroom(room, is_admin):
    return redirect('admin_classroom_detail' if is_admin else 'mentor_classroom_detail', pk=room.pk)


@login_required
@role_required(Profile.ROLE_MENTOR, Profile.ROLE_ADMIN)
def classroom_regenerate_code(request, pk):
    room, is_admin = _staff_classroom_or_404(request, pk)
    if request.method == 'POST':
        room.regenerate_code()
        AuditLog.record(request.user, 'classroom_code_regenerated', room.mentor, f'Classroom "{room.name}" (#{room.pk})')
        messages.success(request, f'New code: {room.code}. The old code no longer works for new students; students already inside stay.')
    return _back_to_classroom(room, is_admin)


@login_required
@role_required(Profile.ROLE_MENTOR, Profile.ROLE_ADMIN)
def classroom_toggle_code(request, pk):
    room, is_admin = _staff_classroom_or_404(request, pk)
    if request.method == 'POST':
        room.code_enabled = not room.code_enabled
        room.save(update_fields=['code_enabled'])
        AuditLog.record(request.user, 'classroom_code_on' if room.code_enabled else 'classroom_code_off', room.mentor,
                        f'Classroom "{room.name}" (#{room.pk})')
        messages.success(request, 'New students can join again.' if room.code_enabled else 'The code is switched off. No new students can join.')
    return _back_to_classroom(room, is_admin)


@login_required
@role_required(Profile.ROLE_MENTOR, Profile.ROLE_ADMIN)
def classroom_remove_student(request, pk, student_id):
    room, is_admin = _staff_classroom_or_404(request, pk)
    if request.method == 'POST':
        enr = get_object_or_404(Enrollment, classroom=room, student_id=student_id)
        if enr.is_active:
            enr.is_active = False
            enr.removed_at = timezone.now()
            enr.removed_by = request.user
            enr.save(update_fields=['is_active', 'removed_at', 'removed_by'])
            AuditLog.record(request.user, 'student_removed_from_classroom', enr.student,
                            f'Classroom "{room.name}" (#{room.pk}). Grades kept.')
            messages.success(request, f'{enr.student.profile.display_name} was removed from the classroom. Their grades and quiz history are kept.')
    return _back_to_classroom(room, is_admin)


@login_required
@role_required(Profile.ROLE_MENTOR, Profile.ROLE_ADMIN)
def classroom_restore_student(request, pk, student_id):
    room, is_admin = _staff_classroom_or_404(request, pk)
    if request.method == 'POST':
        enr = get_object_or_404(Enrollment, classroom=room, student_id=student_id)
        if not enr.is_active:
            enr.is_active = True
            enr.removed_at = None
            enr.removed_by = None
            enr.save(update_fields=['is_active', 'removed_at', 'removed_by'])
            AuditLog.record(request.user, 'student_restored_to_classroom', enr.student, f'Classroom "{room.name}" (#{room.pk})')
            messages.success(request, f'{enr.student.profile.display_name} was added back.')
    return _back_to_classroom(room, is_admin)


# ---------------- Admin ----------------
@login_required
@role_required(Profile.ROLE_ADMIN)
def admin_classroom_list(request):
    rooms = (
        Classroom.objects.select_related('mentor__profile')
        .annotate(
            student_total=Count('enrollments', filter=Q(enrollments__is_active=True), distinct=True),
            lesson_total=Count('lessons', distinct=True),
            quiz_total=Count('quizzes', distinct=True),
        )
    )
    # Data checks. (Duplicate enrollments cannot exist: the database forbids them.)
    checks = {
        'non_student_enrolled': Enrollment.objects.exclude(student__profile__role=Profile.ROLE_STUDENT).count(),
        'inactive_enrolled': Enrollment.objects.filter(is_active=True, student__is_active=False).count(),
        'bad_owner': Classroom.objects.exclude(mentor__profile__role__in=[Profile.ROLE_MENTOR, Profile.ROLE_ADMIN]).count(),
        'lessons_unassigned': Lesson.objects.filter(classroom=None).count(),
        'quizzes_unassigned': Quiz.objects.filter(classroom=None).count(),
        'quiz_lesson_mismatch': Quiz.objects.exclude(lesson=None).exclude(classroom=None)
                                    .exclude(lesson__classroom=F('classroom')).count(),
    }
    return render(request, 'admin_classrooms.html', {
        'rooms': rooms,
        'checks': checks,
        'problems': sum(v for v in checks.values()),
    })


@login_required
@role_required(Profile.ROLE_ADMIN)
@local_time
def admin_classroom_detail(request, pk):
    room = get_object_or_404(Classroom.objects.select_related('mentor__profile'), pk=pk)
    return render(request, 'classroom_manage.html', _manage_context(room, is_admin=True))


# ===========================================================
# DEADLINES, NOTIFICATIONS, ACCOUNT STATUS
# ===========================================================
def _deadline_rows(user, limit=8):
    """Quizzes for the dashboard 'Quiz deadlines' card, most urgent first."""
    quizzes = list(deadlines.student_quizzes(user))
    firsts = exams.first_attempts_by_quiz(user, quizzes)
    accs = deadlines.accommodations_for(user, quizzes)
    now = timezone.now()
    order = {'overdue': 0, 'not_taken': 1, 'not_open': 2, 'late': 3, 'submitted': 3, 'exempt': 3}
    rows = []
    for q in quizzes:
        st = exams.student_quiz_state(q, firsts.get(q.id), now, accs.get(q.id))
        first = firsts.get(q.id)
        rows.append({'quiz': q, 'state': st, 'deadline': st['deadline'],
                     'submitted_at': first.taken_at if first else None})
    far = now + timedelta(days=36500)
    rows.sort(key=lambda r: (order.get(r['state']['code'], 9), r['deadline'] or far))
    return rows[:limit]


@login_required
@role_required(Profile.ROLE_STUDENT)
@local_time
def notifications_view(request):
    """The student's own notices: reminders, overdue quizzes, warnings, account changes."""
    notes = list(Notification.objects.filter(user=request.user, hidden=False).select_related('quiz')[:100])
    unread_ids = [n.id for n in notes if n.read_at is None]
    for n in notes:
        n.was_unread = n.read_at is None
    if unread_ids:
        Notification.objects.filter(id__in=unread_ids).update(read_at=timezone.now())
    return render(request, 'notifications.html', {'notes': notes})


@login_required
@local_time
def account_inactive_view(request):
    profile = get_object_or_404(Profile, user=request.user)
    if not profile.is_policy_inactive:
        return redirect('dashboard_router')
    return render(request, 'account_inactive.html', {
        'profile': profile,
        'contact': getattr(settings, 'ADMIN_CONTACT_EMAIL', ''),
        'overdue': deadlines.overdue_items(request.user),
    })


# ---------------- Mentor / Admin: extensions and exemptions ----------------
@login_required
@role_required(Profile.ROLE_MENTOR, Profile.ROLE_ADMIN)
@local_time
def quiz_accommodation(request, pk, student_id):
    """Give one student an extension or an exemption on one quiz (or take it away)."""
    profile = get_object_or_404(Profile, user=request.user)
    if profile.role == Profile.ROLE_ADMIN:
        quiz = get_object_or_404(Quiz.objects.select_related('classroom'), pk=pk)
    else:
        quiz = get_object_or_404(Quiz.objects.select_related('classroom'), pk=pk, created_by=request.user)
    if quiz.classroom_id is None:
        raise Http404('This quiz has no classroom.')
    student = get_object_or_404(User.objects.select_related('profile'), pk=student_id, profile__role=Profile.ROLE_STUDENT)
    get_object_or_404(Enrollment, classroom=quiz.classroom, student=student)     # only students of this classroom

    existing = QuizAccommodation.objects.filter(quiz=quiz, student=student).first()
    form = AccommodationForm(request.POST or None, quiz=quiz)
    back = 'mentor_exam_progress' if profile.role == Profile.ROLE_MENTOR else 'admin_monitoring'
    back_args = [quiz.pk] if profile.role == Profile.ROLE_MENTOR else []

    if request.method == 'POST':
        if request.POST.get('action') == 'remove' and existing:
            existing.delete()
            AuditLog.record(request.user, 'accommodation_removed', student, f'Quiz "{quiz.title}"')
            messages.success(request, 'Extension / exemption removed.')
            return redirect(back, *back_args)
        if form.is_valid():
            kind = form.cleaned_data['kind']
            QuizAccommodation.objects.update_or_create(
                quiz=quiz, student=student,
                defaults={'kind': kind, 'new_deadline': form.cleaned_data.get('new_deadline') if kind == 'EXTENSION' else None,
                          'reason': form.cleaned_data['reason'], 'granted_by': request.user})
            AuditLog.record(request.user, 'accommodation_granted', student, f'{kind} on "{quiz.title}"')
            if kind == 'EXTENSION':
                nd = form.cleaned_data['new_deadline']
                text = f'Your mentor extended the deadline for "{quiz.title}" to {deadlines.fmt_dt(nd)}.'
            else:
                text = f'Your mentor excused you from "{quiz.title}". You do not need to submit it.'
            deadlines.notify(student, Notification.KIND_ACCOMMODATION, 'Update on your quiz deadline',
                             f"Hi {student.profile.display_name},\n\n{text}\n\nOpen the student portal: {deadlines._portal_url()}",
                             f"accom:{quiz.id}:{student.id}:{int(timezone.now().timestamp())}", quiz=quiz)
            messages.success(request, 'Saved. The student has been notified.')
            return redirect(back, *back_args)

    return render(request, 'quiz_accommodation.html', {
        'quiz': quiz, 'student': student, 'existing': existing, 'form': form, 'back': back, 'back_args': back_args,
    })


# ---------------- Admin: monitoring, policy, reactivation ----------------
@login_required
@role_required(Profile.ROLE_ADMIN)
@local_time
def admin_monitoring(request):
    policy = PolicySettings.get()
    now = timezone.now()

    # Outstanding violations: every student with unresolved overdue quizzes.
    violations = []
    for u in deadlines.policy_students():
        items = deadlines.overdue_items(u, now)
        if items:
            violations.append({'user': u, 'items': items, 'count': len(items),
                               'warned': u.final_warnings.filter(
                                   status__in=[FinalWarning.STATUS_OPEN, FinalWarning.STATUS_PENDING_REVIEW]).first()})
    violations.sort(key=lambda v: -v['count'])

    context = {
        'policy': policy,
        'policy_form': PolicyForm(instance=policy),
        'violations': violations,
        'open_warnings': FinalWarning.objects.filter(status__in=[FinalWarning.STATUS_OPEN, FinalWarning.STATUS_PENDING_REVIEW])
                              .select_related('student__profile').prefetch_related('quizzes'),
        'recent_warnings': FinalWarning.objects.exclude(status__in=[FinalWarning.STATUS_OPEN, FinalWarning.STATUS_PENDING_REVIEW])
                              .select_related('student__profile')[:15],
        'inactive': Profile.objects.filter(account_status=Profile.ACCOUNT_INACTIVE).select_related('user'),
        'status_log': AccountStatusLog.objects.select_related('user__profile', 'actor__profile')[:15],
        'flagged_photos': Profile.objects.filter(avatar_flagged=True, role=Profile.ROLE_STUDENT).select_related('user'),
        'failed_notes': Notification.objects.filter(email_status=Notification.EMAIL_FAILED).select_related('user')[:15],
        'notes': Notification.objects.filter(hidden=False).select_related('user', 'quiz')[:25],
        'note_counts': {
            'sent': Notification.objects.filter(email_status=Notification.EMAIL_SENT).count(),
            'failed': Notification.objects.filter(email_status=Notification.EMAIL_FAILED).count(),
            'no_email': Notification.objects.filter(email_status=Notification.EMAIL_NO_ADDRESS).count(),
        },
        'last_run': request.session.get('policy_last_run'),
    }
    return render(request, 'admin_monitoring.html', context)


@login_required
@role_required(Profile.ROLE_ADMIN)
def admin_policy_save(request):
    if request.method == 'POST':
        policy = PolicySettings.get()
        was_approved = policy.auto_deactivation_approved
        form = PolicyForm(request.POST, instance=policy)
        if form.is_valid():
            saved = form.save(commit=False)
            if saved.auto_deactivation_approved and not was_approved:
                saved.approved_by = request.user
                saved.approved_at = timezone.now()
            elif not saved.auto_deactivation_approved:
                saved.approved_by = None
                saved.approved_at = None
            saved.save()
            AuditLog.record(request.user, 'policy_updated', None,
                            f"auto-deactivation {'ON' if saved.auto_deactivation_approved else 'OFF'}; "
                            f"reminders {saved.reminder_hours or 'none'}h; threshold {saved.warn_threshold}")
            messages.success(request, 'Policy saved.')
        else:
            messages.error(request, 'Policy not saved: ' + ' '.join(' '.join(e) for e in form.errors.values()))
    return redirect('admin_monitoring')


@login_required
@role_required(Profile.ROLE_ADMIN)
def admin_run_policy(request):
    """Run the same job as the scheduled task, right now (also handy for demos)."""
    if request.method == 'POST':
        rep = deadlines.run_policy()
        AuditLog.record(request.user, 'policy_run_manual', None, str(rep))
        messages.success(request, (
            f"Check finished: {rep['reminders']} reminder(s), {rep['overdue']} overdue notice(s), "
            f"{rep['warnings']} final warning(s), {rep['resolved']} resolved, "
            f"{rep['pending_review']} waiting for review, {rep['deactivated']} deactivated, "
            f"{rep['retried']} e-mail retr{'y' if rep['retried']==1 else 'ies'}."))
    return redirect('admin_monitoring')


@login_required
@role_required(Profile.ROLE_ADMIN)
def admin_warning_action(request, pk):
    """Admin decision on a warning case: deactivate now / resolve / reset."""
    warning = get_object_or_404(FinalWarning.objects.select_related('student__profile'), pk=pk)
    action = request.POST.get('action') if request.method == 'POST' else None
    note = (request.POST.get('note') or '').strip()[:300]
    user = warning.student
    if action == 'deactivate' and warning.status in (FinalWarning.STATUS_PENDING_REVIEW, FinalWarning.STATUS_OPEN):
        remaining = [i for i in deadlines.overdue_items(user) if i['quiz'].id in set(warning.quizzes.values_list('id', flat=True))]
        deadlines.deactivate_account(user, warning, actor=request.user, activity=warning.activity, remaining=remaining,
                                     reason=note or 'Deactivated by an administrator after reviewing the final warning.')
        messages.success(request, f'{user.profile.display_name} was deactivated. Records are kept.')
    elif action == 'resolve' and warning.status in (FinalWarning.STATUS_PENDING_REVIEW, FinalWarning.STATUS_OPEN):
        warning.status = FinalWarning.STATUS_RESOLVED
        warning.evaluated_at = timezone.now()
        warning.resolution_note = note or 'Resolved by an administrator. No deactivation.'
        warning.save()
        AuditLog.record(request.user, 'warning_resolved', user, warning.resolution_note)
        messages.success(request, 'Case closed without deactivation.')
    elif action == 'reset' and warning.status != FinalWarning.STATUS_RESET:
        warning.status = FinalWarning.STATUS_RESET
        warning.resolution_note = note or 'Warning process reset by an administrator.'
        warning.save()
        AuditLog.record(request.user, 'warning_reset', user, warning.resolution_note)
        messages.success(request, 'Warning reset. A new warning can be issued later if the work stays overdue.')
    return redirect('admin_monitoring')


@login_required
@role_required(Profile.ROLE_ADMIN)
def admin_user_reactivate(request, user_id):
    target = get_object_or_404(User.objects.select_related('profile'), pk=user_id)
    if request.method == 'POST':
        note = (request.POST.get('note') or '').strip()[:300]
        if deadlines.reactivate_account(target, request.user, note):
            messages.success(request, f'{target.profile.display_name} was reactivated. All records were kept.')
        else:
            messages.error(request, 'That account is not deactivated by the warning policy.')
    return redirect('admin_user_detail', user_id=user_id)


# ===========================================================
# STAFF: STUDENT SEARCH, PROGRESS DETAILS, PRINTABLE REPORT
# Mentors only reach classrooms they own; Admins reach all.
# Every lookup goes through progress.scope_classrooms(), so another
# Mentor's classroom or student is a 404, never a hidden button.
# ===========================================================
@login_required
@role_required(Profile.ROLE_MENTOR, Profile.ROLE_ADMIN)
@local_time
def staff_students(request):
    q = (request.GET.get('q') or '').strip()[:80]
    rooms = prog.scope_classrooms(request.user).select_related('mentor__profile')
    try:
        room_id = int(request.GET.get('room') or 0) or None
    except ValueError:
        room_id = None
    page_obj, rows = prog.students_page(request.user, q, room_id, request.GET.get('page'))
    ctx = {'q': q, 'room_id': room_id, 'rooms': rooms, 'page_obj': page_obj, 'rows': rows,
           'total': page_obj.paginator.count}
    if request.GET.get('partial') == '1':
        return render(request, 'students_results.html', ctx)
    return render(request, 'students.html', ctx)


def _enrollment_or_404(request, pk, student_id):
    enrollment = prog.get_enrollment_or_none(request.user, pk, student_id)
    if enrollment is None:
        raise Http404('No such student in a classroom you manage.')
    return enrollment


@login_required
@role_required(Profile.ROLE_MENTOR, Profile.ROLE_ADMIN)
@local_time
def student_progress(request, pk, student_id):
    e = _enrollment_or_404(request, pk, student_id)
    return render(request, 'student_progress.html', {
        'r': prog.full_report(e.student, e.classroom), 'enrollment': e,
    })


@login_required
@role_required(Profile.ROLE_MENTOR, Profile.ROLE_ADMIN)
@local_time
def student_report(request, pk, student_id):
    e = _enrollment_or_404(request, pk, student_id)
    AuditLog.record(request.user, 'progress_report_opened', e.student, f'Classroom "{e.classroom.name}"')
    return render(request, 'student_report.html', {
        'r': prog.full_report(e.student, e.classroom), 'enrollment': e,
        'school_name': getattr(settings, 'SCHOOL_NAME', SCHOOL_NAME),
        'generated_by': request.user.profile.display_name,
        'autoprint': request.GET.get('print') == '1',
    })


@login_required
@role_required(Profile.ROLE_MENTOR, Profile.ROLE_ADMIN)
@local_time
def student_attempt(request, pk, student_id, attempt_id):
    e = _enrollment_or_404(request, pk, student_id)
    attempt = get_object_or_404(QuizAttempt.objects.select_related('quiz'),
                                pk=attempt_id, user=e.student, quiz__classroom=e.classroom)
    return render(request, 'student_attempt.html', {
        'enrollment': e, 'attempt': attempt, 'quiz': attempt.quiz,
        'grading': prog.GRADING_LABELS[prog.attempt_grading_status(attempt)],
        'type': prog.quiz_type_label(attempt.quiz),
    })
