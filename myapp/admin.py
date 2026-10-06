from django.contrib import admin
from .models import (
    Profile, Lesson, LessonProgress,
    Quiz, Question, Choice, QuizAttempt,
    Badge, UserBadge,
)


class ChoiceInline(admin.TabularInline):
    model = Choice
    extra = 3


class QuestionAdmin(admin.ModelAdmin):
    inlines = [ChoiceInline]
    list_display = ('text', 'quiz')


class QuizAdmin(admin.ModelAdmin):
    list_display = ('title', 'lesson', 'total_questions', 'created_by', 'is_published', 'start_datetime', 'deadline_datetime')
    list_filter = ('is_published',)


class LessonAdmin(admin.ModelAdmin):
    list_display = ('order', 'title', 'slug', 'created_by')
    prepopulated_fields = {'slug': ('title',)}


class ProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'name', 'role', 'is_verified', 'is_locked', 'failed_login_attempts')
    list_filter = ('role', 'is_verified', 'is_locked')
    search_fields = ('user__username', 'user__email', 'name')
    # To unlock someone manually: untick "is locked" and set the attempts to 0.


admin.site.register(Profile, ProfileAdmin)
admin.site.register(Lesson, LessonAdmin)
admin.site.register(LessonProgress)
admin.site.register(Quiz, QuizAdmin)
admin.site.register(Question, QuestionAdmin)
admin.site.register(Choice)
admin.site.register(QuizAttempt)
admin.site.register(Badge)
admin.site.register(UserBadge)
