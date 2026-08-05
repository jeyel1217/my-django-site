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

    # Dashboard
    path('dashboard/', views.home, name='home'),

    # Lessons
    path('lessons/', views.lesson_list, name='lessons'),
    path('lessons/<slug:slug>/', views.lesson_detail, name='lesson_detail'),

    # Quizzes
    path('quizzes/', views.quizzes_view, name='quizzes'),
    path('quizzes/<int:quiz_id>/', views.quiz_detail, name='quiz_detail'),

    # Badges
    path('badges/', views.badges_view, name='badges'),

    # Profile
    path('profile/', views.profile_view, name='profile'),
    path('profile/edit/', views.edit_profile, name='edit_profile'),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)