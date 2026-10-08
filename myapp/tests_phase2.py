from datetime import timedelta

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import exams
from .models import (
    Profile, TermsVersion, TermsAcceptance, Classroom, Enrollment, Lesson, Quiz, Question, Choice,
    QuizAttempt, AuditLog, generate_unique_code, normalize_code,
)

PW = 'Passw0rd!x'
FAST = override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])


def make(username, role, accepted=True):
    u = User.objects.create_user(username=username, email=f'{username}@gmail.com', password=PW)
    p = u.profile
    p.role = role
    p.is_verified = True
    p.save()
    if role == Profile.ROLE_STUDENT and accepted:
        TermsAcceptance.objects.create(user=u, terms=TermsVersion.current())
    return u


def lesson(room, title, published=True, order=1):
    return Lesson.objects.create(title=title, slug=title.lower().replace(' ', '-'), order=order, content='c',
                                 created_by=room.mentor, is_published=published, classroom=room)


def quiz(room, title, published=True):
    q = Quiz.objects.create(title=title, created_by=room.mentor, is_published=published, classroom=room)
    qu = Question.objects.create(quiz=q, text='Q?')
    Choice.objects.create(question=qu, text='right', is_correct=True)
    Choice.objects.create(question=qu, text='wrong', is_correct=False)
    return q


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = make('admin_two22', Profile.ROLE_ADMIN)
        self.m1 = make('mentor_aaaa1', Profile.ROLE_MENTOR)
        self.m2 = make('mentor_bbbb2', Profile.ROLE_MENTOR)
        self.room1 = Classroom.objects.create(name='Java 101', mentor=self.m1)
        self.room2 = Classroom.objects.create(name='Databases', mentor=self.m2)
        self.l1 = lesson(self.room1, 'Java Basics')
        self.l2 = lesson(self.room2, 'SQL Joins')
        self.draft1 = lesson(self.room1, 'Secret Draft', published=False, order=2)
        self.q1 = quiz(self.room1, 'Java Quiz')
        self.q2 = quiz(self.room2, 'SQL Quiz')
        self.s1 = make('student_aaa1', Profile.ROLE_STUDENT)
        self.s2 = make('student_bbb2', Profile.ROLE_STUDENT)

    def login(self, user):
        self.client.logout()
        self.assertTrue(self.client.login(username=user.username, password=PW))

    def join(self, student, room):
        return Enrollment.objects.create(classroom=room, student=student)


@FAST
class CodeTests(Base):
    def test_codes_unique_readable_never_numeric(self):
        codes = {generate_unique_code() for _ in range(200)}
        self.assertEqual(len(codes), 200)
        for c in codes:
            self.assertEqual(len(c), 7)
            self.assertFalse(c.isdigit())
            self.assertFalse(set(c) & set('01OI'))
        self.assertNotEqual(self.room1.code, self.room2.code)

    def test_normalize(self):
        self.assertEqual(normalize_code(' ab-c d2 '), 'ABCD2')

    def test_regenerate_old_code_dead_existing_students_stay(self):
        self.join(self.s1, self.room1)
        old = self.room1.code
        self.login(self.m1)
        self.client.post(reverse('classroom_regenerate_code', args=[self.room1.pk]))
        self.room1.refresh_from_db()
        self.assertNotEqual(self.room1.code, old)
        self.login(self.s2)
        r = self.client.post(reverse('classroom_join'), {'code': old})
        self.assertContains(r, 'not valid')
        self.login(self.s1)                                   # already inside: still has access
        self.assertEqual(self.client.get(reverse('classroom_detail', args=[self.room1.pk])).status_code, 200)


@FAST
class JoinTests(Base):
    def test_empty_state_and_nothing_visible(self):
        self.login(self.s1)
        r = self.client.get(reverse('student_dashboard'))
        self.assertContains(r, "haven't joined a classroom")
        self.assertContains(r, 'Add Classroom')
        self.assertNotContains(r, 'Java 101')
        self.assertNotContains(self.client.get(reverse('lessons')), 'Java Basics')
        self.assertNotContains(self.client.get(reverse('quizzes')), 'Java Quiz')

    def test_two_step_join(self):
        self.login(self.s1)
        r = self.client.post(reverse('classroom_join'), {'code': self.room1.code.lower()})
        self.assertContains(r, 'Join this classroom?')
        self.assertContains(r, 'Java 101')
        self.assertContains(r, self.m1.profile.display_name)
        self.assertEqual(Enrollment.objects.count(), 0)          # preview does not enroll
        r = self.client.post(reverse('classroom_join'), {'code': self.room1.code, 'confirm': '1'})
        self.assertRedirects(r, reverse('classroom_detail', args=[self.room1.pk]), fetch_redirect_response=False)
        e = Enrollment.objects.get()
        self.assertEqual((e.student, e.classroom, e.is_active), (self.s1, self.room1, True))
        self.assertIsNotNone(e.joined_at)
        self.assertContains(self.client.get(reverse('student_dashboard')), 'Open Classroom')

    def test_code_with_spaces_and_hyphen_works(self):
        self.login(self.s1)
        messy = ' ' + self.room1.code[:3] + '-' + self.room1.code[3:] + ' '
        self.assertContains(self.client.post(reverse('classroom_join'), {'code': messy}), 'Join this classroom?')

    def test_invalid_code_reveals_nothing(self):
        self.login(self.s1)
        r = self.client.post(reverse('classroom_join'), {'code': 'ZZZZZZZ'})
        self.assertContains(r, 'not valid or is no longer active')
        for secret in ['Java', 'Databases', 'SQL', 'Secret']:
            self.assertNotContains(r, secret)
        self.assertEqual(Enrollment.objects.count(), 0)
        # guessing the numeric ID as a code does not work either
        r = self.client.post(reverse('classroom_join'), {'code': str(self.room1.pk)})
        self.assertContains(r, 'not valid')

    def test_switched_off_code_rejected(self):
        self.room1.code_enabled = False
        self.room1.save()
        self.login(self.s1)
        self.assertContains(self.client.post(reverse('classroom_join'), {'code': self.room1.code, 'confirm': '1'}), 'not valid')
        self.assertEqual(Enrollment.objects.count(), 0)

    def test_no_duplicate_enrollment(self):
        self.login(self.s1)
        for _ in range(3):
            self.client.post(reverse('classroom_join'), {'code': self.room1.code, 'confirm': '1'})
        self.assertEqual(Enrollment.objects.filter(student=self.s1, classroom=self.room1).count(), 1)

    def test_guess_throttle(self):
        self.login(self.s1)
        for _ in range(8):
            self.client.post(reverse('classroom_join'), {'code': 'WRONGCD'})
        r = self.client.post(reverse('classroom_join'), {'code': self.room1.code, 'confirm': '1'})   # even the right code is paused
        self.assertContains(r, 'Too many wrong codes')
        self.assertEqual(Enrollment.objects.count(), 0)

    def test_only_students_join(self):
        for u in (self.m1, self.admin):
            self.login(u)
            self.assertEqual(self.client.get(reverse('classroom_join')).status_code, 403)

    def test_terms_still_required_before_joining(self):
        s = make('student_ccc3', Profile.ROLE_STUDENT, accepted=False)
        self.login(s)
        self.assertRedirects(self.client.get(reverse('classroom_join')), reverse('terms_accept'), fetch_redirect_response=False)


@FAST
class IsolationTests(Base):
    def setUp(self):
        super().setUp()
        self.join(self.s1, self.room1)

    def test_student_sees_only_joined_classroom_content(self):
        self.login(self.s1)
        r = self.client.get(reverse('lessons'))
        self.assertContains(r, 'Java Basics')
        self.assertNotContains(r, 'SQL Joins')
        self.assertNotContains(r, 'Secret Draft')             # drafts stay hidden
        r = self.client.get(reverse('quizzes'))
        self.assertContains(r, 'Java Quiz')
        self.assertNotContains(r, 'SQL Quiz')
        r = self.client.get(reverse('classroom_detail', args=[self.room1.pk]))
        self.assertContains(r, 'Java Basics')
        self.assertContains(r, 'Java Quiz')

    def test_direct_urls_to_other_classrooms_are_404(self):
        self.login(self.s1)
        self.assertEqual(self.client.get(reverse('lesson_detail', args=[self.l2.slug])).status_code, 404)
        self.assertEqual(self.client.get(reverse('lesson_detail', args=[self.draft1.slug])).status_code, 404)
        self.assertEqual(self.client.get(reverse('quiz_detail', args=[self.q2.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse('classroom_detail', args=[self.room2.pk])).status_code, 404)
        # cannot submit an answer to a quiz of a classroom they are not in
        qu = self.q2.questions.first()
        r = self.client.post(reverse('quiz_detail', args=[self.q2.pk]), {f'question_{qu.pk}': qu.choices.first().pk})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(QuizAttempt.objects.count(), 0)
        # joined classroom works
        self.assertEqual(self.client.get(reverse('lesson_detail', args=[self.l1.slug])).status_code, 200)
        self.assertEqual(self.client.get(reverse('quiz_detail', args=[self.q1.pk])).status_code, 200)

    def test_other_student_with_no_enrollment_sees_nothing(self):
        self.login(self.s2)
        self.assertEqual(self.client.get(reverse('lesson_detail', args=[self.l1.slug])).status_code, 404)
        self.assertEqual(self.client.get(reverse('classroom_detail', args=[self.room1.pk])).status_code, 404)

    def test_dashboard_stats_count_only_joined_lessons(self):
        p = Profile.objects.get(user=self.s1)
        self.assertEqual(p.total_lessons, 1)                  # Java Basics only (not SQL, not the draft)

    def test_removal_revokes_access_but_keeps_records(self):
        self.login(self.s1)
        qu = self.q1.questions.first()
        self.client.post(reverse('quiz_detail', args=[self.q1.pk]), {f'question_{qu.pk}': qu.choices.get(is_correct=True).pk})
        self.assertEqual(QuizAttempt.objects.filter(user=self.s1).count(), 1)

        self.login(self.m1)
        r = self.client.post(reverse('classroom_remove_student', args=[self.room1.pk, self.s1.pk]))
        self.assertEqual(r.status_code, 302)
        e = Enrollment.objects.get(student=self.s1)
        self.assertFalse(e.is_active)
        self.assertEqual(e.removed_by, self.m1)
        self.assertTrue(AuditLog.objects.filter(action='student_removed_from_classroom').exists())

        self.login(self.s1)
        self.assertEqual(self.client.get(reverse('lesson_detail', args=[self.l1.slug])).status_code, 404)
        self.assertEqual(self.client.get(reverse('quiz_detail', args=[self.q1.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse('classroom_detail', args=[self.room1.pk])).status_code, 404)
        self.assertTrue(User.objects.get(pk=self.s1.pk).is_active)                 # account untouched
        self.assertEqual(QuizAttempt.objects.filter(user=self.s1).count(), 1)      # grade kept

        # cannot sneak back in with the code
        r = self.client.post(reverse('classroom_join'), {'code': self.room1.code, 'confirm': '1'})
        self.assertContains(r, 'removed from this classroom')
        self.assertFalse(Enrollment.objects.get(student=self.s1).is_active)

        # the Mentor can add them back
        self.login(self.m1)
        self.client.post(reverse('classroom_restore_student', args=[self.room1.pk, self.s1.pk]))
        self.login(self.s1)
        self.assertEqual(self.client.get(reverse('lesson_detail', args=[self.l1.slug])).status_code, 200)

    def test_progress_lists_only_enrolled_students(self):
        self.join(self.s2, self.room2)                       # s2 is in the OTHER classroom
        rows = exams.build_progress(self.q1)['rows']
        self.assertEqual([r['username'] for r in rows], ['student_aaa1'])


@FAST
class MentorAdminTests(Base):
    def test_mentor_sees_only_own_classrooms(self):
        self.login(self.m1)
        r = self.client.get(reverse('mentor_classrooms'))
        self.assertContains(r, 'Java 101')
        self.assertNotContains(r, 'Databases')
        self.assertContains(self.client.get(reverse('mentor_dashboard')), 'Java 101')

    def test_mentor_cannot_touch_other_mentors_classroom(self):
        self.join(self.s2, self.room2)
        self.login(self.m1)
        self.assertEqual(self.client.get(reverse('mentor_classroom_detail', args=[self.room2.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse('mentor_classroom_edit', args=[self.room2.pk])).status_code, 404)
        for name in ('classroom_regenerate_code', 'classroom_toggle_code'):
            self.assertEqual(self.client.post(reverse(name, args=[self.room2.pk])).status_code, 404)
        r = self.client.post(reverse('classroom_remove_student', args=[self.room2.pk, self.s2.pk]))
        self.assertEqual(r.status_code, 404)
        self.assertTrue(Enrollment.objects.get(student=self.s2).is_active)

    def test_students_cannot_use_mentor_or_admin_classroom_pages(self):
        self.login(self.s1)
        for name, args in [('mentor_classrooms', []), ('mentor_classroom_create', []), ('admin_classroom_list', []),
                           ('admin_classroom_detail', [self.room1.pk])]:
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 403, name)
        for name in ('classroom_regenerate_code', 'classroom_toggle_code'):
            self.assertEqual(self.client.post(reverse(name, args=[self.room1.pk])).status_code, 403)

    def test_create_and_edit_classroom_with_unique_code(self):
        self.login(self.m1)
        r = self.client.post(reverse('mentor_classroom_create'), {'name': 'Python Basics', 'description': 'Sec A'})
        room = Classroom.objects.get(name='Python Basics')
        self.assertRedirects(r, reverse('mentor_classroom_detail', args=[room.pk]), fetch_redirect_response=False)
        self.assertEqual(room.mentor, self.m1)
        self.assertNotIn(room.code, [self.room1.code, self.room2.code])
        self.client.post(reverse('mentor_classroom_edit', args=[room.pk]), {'name': 'Python 2', 'description': ''})
        room.refresh_from_db()
        self.assertEqual(room.name, 'Python 2')

    def test_lesson_and_quiz_must_use_own_classroom(self):
        self.login(self.m1)
        r = self.client.post(reverse('mentor_lesson_create'), {
            'classroom': self.room2.pk, 'title': 'Hack', 'order': 1, 'content': 'x', 'is_published': 'on'})
        self.assertEqual(r.status_code, 200)                                   # form error, nothing saved
        self.assertFalse(Lesson.objects.filter(title='Hack').exists())
        r = self.client.post(reverse('mentor_lesson_create'), {
            'classroom': self.room1.pk, 'title': 'Loops', 'order': 3, 'content': 'x', 'is_published': 'on'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Lesson.objects.get(title='Loops').classroom, self.room1)
        # classroom pre-selected from the classroom page
        r = self.client.get(reverse('mentor_lesson_create') + f'?classroom={self.room1.pk}')
        self.assertContains(r, f'<option value="{self.room1.pk}" selected>')
        r = self.client.get(reverse('mentor_lesson_create') + f'?classroom={self.room2.pk}')
        self.assertNotContains(r, 'Databases')                                 # not offered at all

    def test_quiz_in_classroom_and_lesson_mismatch_rejected(self):
        self.login(self.m1)
        base = {'title': 'Quiz X', 'description': '', 'instructions': '', 'is_published': 'on'}
        r = self.client.post(reverse('mentor_quiz_create'), dict(base, classroom=self.room2.pk))
        self.assertEqual(r.status_code, 200)
        self.assertFalse(Quiz.objects.filter(title='Quiz X').exists())
        other = Classroom.objects.create(name='Second', mentor=self.m1)
        r = self.client.post(reverse('mentor_quiz_create'), dict(base, classroom=other.pk, lesson=self.l1.pk))
        self.assertEqual(r.status_code, 200)                                   # lesson is in room1, quiz in 'Second'
        self.assertContains(r, 'different classroom')
        r = self.client.post(reverse('mentor_quiz_create'), dict(base, classroom=self.room1.pk, lesson=self.l1.pk))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Quiz.objects.get(title='Quiz X').classroom, self.room1)

    def test_mentor_sees_enrolled_students_and_grades(self):
        self.join(self.s1, self.room1)
        self.login(self.s1)
        qu = self.q1.questions.first()
        self.client.post(reverse('quiz_detail', args=[self.q1.pk]), {f'question_{qu.pk}': qu.choices.get(is_correct=True).pk})
        self.login(self.m1)
        r = self.client.get(reverse('mentor_classroom_detail', args=[self.room1.pk]))
        self.assertContains(r, 'student_aaa1')
        self.assertContains(r, '1/1 submitted')
        r = self.client.get(reverse('mentor_exam_progress', args=[self.q1.pk]))
        self.assertContains(r, 'student_aaa1')
        self.assertContains(r, '100')

    def test_admin_reviews_all_classrooms_and_data_checks(self):
        Lesson.objects.create(title='Loose', slug='loose', content='c', created_by=self.m1, is_published=True)  # no classroom
        self.join(self.s1, self.room1)
        self.login(self.admin)
        r = self.client.get(reverse('admin_classroom_list'))
        self.assertContains(r, 'Java 101'); self.assertContains(r, 'Databases')
        self.assertContains(r, '1 lesson(s) have no classroom')
        r = self.client.get(reverse('admin_classroom_detail', args=[self.room1.pk]))
        self.assertContains(r, 'student_aaa1')
        # admin may regenerate + remove, and it is logged
        self.client.post(reverse('classroom_regenerate_code', args=[self.room1.pk]))
        self.client.post(reverse('classroom_remove_student', args=[self.room1.pk, self.s1.pk]))
        self.assertEqual(AuditLog.objects.filter(actor=self.admin).count(), 2)

    def test_new_mentor_gets_default_classroom(self):
        self.login(self.admin)
        self.client.post(reverse('admin_mentor_create'), {'username': 'new_mentor_9', 'password': 'N3w@Passw0rd', 'confirm_password': 'N3w@Passw0rd'})
        m = User.objects.get(username='new_mentor_9')
        self.assertEqual(Classroom.objects.filter(mentor=m).count(), 1)
        # a mentor with none gets one on first visit
        m3 = make('mentor_cccc3', Profile.ROLE_MENTOR)
        self.login(m3)
        self.client.get(reverse('mentor_classrooms'))
        self.assertEqual(Classroom.objects.filter(mentor=m3).count(), 1)
