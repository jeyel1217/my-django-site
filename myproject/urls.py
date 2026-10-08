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

    # Classrooms (student)
    path('classrooms/join/', views.classroom_join, name='classroom_join'),
    path('classrooms/<int:pk>/', views.classroom_detail, name='classroom_detail'),

    # Classroom actions (Mentor of that classroom, or Admin; POST only)
    path('classrooms/<int:pk>/regenerate-code/', views.classroom_regenerate_code, name='classroom_regenerate_code'),
    path('classrooms/<int:pk>/toggle-code/', views.classroom_toggle_code, name='classroom_toggle_code'),
    path('classrooms/<int:pk>/students/<int:student_id>/remove/', views.classroom_remove_student, name='classroom_remove_student'),
    path('classrooms/<int:pk>/students/<int:student_id>/restore/', views.classroom_restore_student, name='classroom_restore_student'),

    # Terms and Conditions
    path('terms/', views.terms_view, name='terms'),
    path('terms/accept/', views.terms_accept_view, name='terms_accept'),

    # Profile
    path('profile/', views.profile_view, name='profile'),
    path('profile/edit/', views.edit_profile, name='edit_profile'),

    # ---------------------------------------------------------
    # MENTOR — content management
    # ---------------------------------------------------------
    path('mentor/classrooms/', views.mentor_classrooms, name='mentor_classrooms'),
    path('mentor/classrooms/create/', views.mentor_classroom_create, name='mentor_classroom_create'),
    path('mentor/classrooms/<int:pk>/', views.mentor_classroom_detail, name='mentor_classroom_detail'),
    path('mentor/classrooms/<int:pk>/edit/', views.mentor_classroom_edit, name='mentor_classroom_edit'),
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

    # Student progress + printing (Mentor)
    path('mentor/progress/', views.mentor_progress_overview, name='mentor_progress_overview'),
    path('mentor/quizzes/<int:pk>/progress/', views.mentor_exam_progress, name='mentor_exam_progress'),
    path('mentor/quizzes/<int:pk>/print/', views.mentor_quiz_print, name='mentor_quiz_print'),

    # ---------------------------------------------------------
    # ADMIN — account management
    # ---------------------------------------------------------
    path('admin-panel/users/', views.admin_user_list, name='admin_user_list'),
    path('admin-panel/users/<int:user_id>/', views.admin_user_detail, name='admin_user_detail'),
    path('admin-panel/users/<int:user_id>/toggle-active/', views.admin_user_toggle_active, name='admin_user_toggle_active'),
    path('admin-panel/users/<int:user_id>/delete/', views.admin_user_delete, name='admin_user_delete'),
    path('admin-panel/users/<int:user_id>/unlock/', views.admin_user_unlock, name='admin_user_unlock'),
    path('admin-panel/users/<int:user_id>/reset-password/', views.admin_user_reset_password, name='admin_user_reset_password'),
    path('admin-panel/users/<int:user_id>/flag-photo/', views.admin_user_flag_photo, name='admin_user_flag_photo'),
    path('admin-panel/users/<int:user_id>/clear-photo-flag/', views.admin_user_clear_photo_flag, name='admin_user_clear_photo_flag'),
    # Students: search, progress details, printable report (Mentor + Admin)
    path('staff/students/', views.staff_students, name='staff_students'),
    path('staff/classrooms/<int:pk>/students/<int:student_id>/', views.student_progress, name='student_progress'),
    path('staff/classrooms/<int:pk>/students/<int:student_id>/report/', views.student_report, name='student_report'),
    path('staff/classrooms/<int:pk>/students/<int:student_id>/attempts/<int:attempt_id>/', views.student_attempt, name='student_attempt'),
    # Deadlines, notifications, account status (Phase 3)
    path('notifications/', views.notifications_view, name='notifications'),
    path('account/inactive/', views.account_inactive_view, name='account_inactive'),
    path('mentor/quizzes/<int:pk>/accommodation/<int:student_id>/', views.quiz_accommodation, name='quiz_accommodation'),
    path('admin-panel/monitoring/', views.admin_monitoring, name='admin_monitoring'),
    path('admin-panel/monitoring/policy/', views.admin_policy_save, name='admin_policy_save'),
    path('admin-panel/monitoring/run/', views.admin_run_policy, name='admin_run_policy'),
    path('admin-panel/monitoring/warnings/<int:pk>/', views.admin_warning_action, name='admin_warning_action'),
    path('admin-panel/users/<int:user_id>/reactivate/', views.admin_user_reactivate, name='admin_user_reactivate'),
    path('admin-panel/classrooms/', views.admin_classroom_list, name='admin_classroom_list'),
    path('admin-panel/classrooms/<int:pk>/', views.admin_classroom_detail, name='admin_classroom_detail'),
    path('admin-panel/terms/', views.admin_terms_list, name='admin_terms_list'),
    path('admin-panel/terms/new/', views.admin_terms_publish, name='admin_terms_publish'),
    path('admin-panel/mentors/create/', views.admin_mentor_create, name='admin_mentor_create'),

    # Exam monitoring (Admin)

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
