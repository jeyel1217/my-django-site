from django.contrib import admin
from django.urls import path
from django.conf import settings
from django.conf.urls.static import static
from myapp import views

urlpatterns = [
    path('admin/', admin.site.urls),

    # Auth
    path('', views.login_view, name='root'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('register/', views.register_view, name='register'),

    # Dashboard routing (role-based)
    path('dashboard/', views.dashboard_router, name='dashboard_router'),
    path('dashboard/student/', views.student_dashboard, name='student_dashboard'),
    path('dashboard/mentor/', views.mentor_dashboard, name='mentor_dashboard'),
    path('dashboard/admin/', views.admin_dashboard, name='admin_dashboard'),
    path('home/', views.dashboard_router, name='home'),

    # Lessons (student-facing)
    path('lessons/', views.lesson_list, name='lessons'),
    path('lessons/<slug:slug>/', views.lesson_detail, name='lesson_detail'),

    # Quizzes (student-facing)
    path('quizzes/', views.quizzes_view, name='quizzes'),
    path('quizzes/<int:quiz_id>/', views.quiz_detail, name='quiz_detail'),

    # Badges
    path('badges/', views.badges_view, name='badges'),

    # Profile
    path('profile/', views.profile_view, name='profile'),
    path('profile/edit/', views.edit_profile, name='edit_profile'),

    # ---------------------------------------------------------
    # MENTOR — content management
    # ---------------------------------------------------------
    path('mentor/lessons/', views.mentor_lessons, name='mentor_lessons'),
    path('mentor/lessons/create/', views.mentor_lesson_create, name='mentor_lesson_create'),
    path('mentor/lessons/<int:pk>/edit/', views.mentor_lesson_edit, name='mentor_lesson_edit'),
    path('mentor/lessons/<int:pk>/delete/', views.mentor_lesson_delete, name='mentor_lesson_delete'),

    path('mentor/quizzes/', views.mentor_quizzes, name='mentor_quizzes'),
    path('mentor/quizzes/create/', views.mentor_quiz_create, name='mentor_quiz_create'),
    path('mentor/quizzes/<int:pk>/edit/', views.mentor_quiz_edit, name='mentor_quiz_edit'),
    path('mentor/quizzes/<int:pk>/delete/', views.mentor_quiz_delete, name='mentor_quiz_delete'),
    path('mentor/quizzes/<int:pk>/questions/', views.mentor_quiz_questions, name='mentor_quiz_questions'),
    path('mentor/quizzes/<int:pk>/questions/<int:question_id>/delete/', views.mentor_question_delete, name='mentor_question_delete'),

    # ---------------------------------------------------------
    # ADMIN — account management
    # ---------------------------------------------------------
    path('admin-panel/users/', views.admin_user_list, name='admin_user_list'),
    path('admin-panel/users/<int:user_id>/', views.admin_user_detail, name='admin_user_detail'),
    path('admin-panel/users/<int:user_id>/toggle-active/', views.admin_user_toggle_active, name='admin_user_toggle_active'),
    path('admin-panel/users/<int:user_id>/delete/', views.admin_user_delete, name='admin_user_delete'),
    path('admin-panel/mentors/create/', views.admin_mentor_create, name='admin_mentor_create'),

    # ---------------------------------------------------------
    # EMAIL VERIFICATION + PASSWORD RESET
    # ---------------------------------------------------------
    path('verify-email/', views.verify_email_view, name='verify_email'),
    path('verify-email/resend/', views.resend_verification_otp, name='resend_verification_otp'),
    path('verify-email/confirm/<int:user_id>/<str:token>/', views.verify_email_confirm_view, name='verify_email_confirm'),
    path('forgot-password/', views.forgot_password_request_view, name='forgot_password_request'),
    path('reset-password/', views.reset_password_confirm_view, name='reset_password_confirm'),
    path('reset-password/resend/', views.resend_reset_otp, name='resend_reset_otp'),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)