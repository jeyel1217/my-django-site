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
    list_display = ('title', 'lesson', 'total_questions', 'created_by')


class LessonAdmin(admin.ModelAdmin):
    list_display = ('order', 'title', 'slug', 'created_by')
    prepopulated_fields = {'slug': ('title',)}


class ProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'role', 'streak_days')
    list_filter = ('role',)
    search_fields = ('user__username', 'user__email')


admin.site.register(Profile, ProfileAdmin)
admin.site.register(Lesson, LessonAdmin)
admin.site.register(LessonProgress)
admin.site.register(Quiz, QuizAdmin)
admin.site.register(Question, QuestionAdmin)
admin.site.register(Choice)
admin.site.register(QuizAttempt)
admin.site.register(Badge)
admin.site.register(UserBadge)