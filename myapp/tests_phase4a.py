from datetime import timedelta

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone

from . import progress
from .models import (
    Profile, Enrollment, Lesson, LessonProgress, Quiz, QuizAttempt, AuditLog,
)
from .tests_phase2 import Base, FAST, make, quiz, lesson, PW


def attempt(user, q, score, correct, total, when=None):
    a = QuizAttempt.objects.create(user=user, quiz=q, score=score, correct_count=correct, total_questions=total)
    if when:
        QuizAttempt.objects.filter(pk=a.pk).update(taken_at=when)
    return a


@FAST
class P4Base(Base):
    def setUp(self):
        super().setUp()
        self.now = timezone.now()
        self.s1.profile.name = 'Juan Dela Cruz'; self.s1.profile.student_id = '2026-00123'; self.s1.profile.save()
        self.s2.profile.name = 'Maria Santos'; self.s2.profile.student_id = '2026-00456'; self.s2.profile.save()
        self.join(self.s1, self.room1)
        self.join(self.s2, self.room2)          # s2 belongs to the OTHER mentor


class SearchTests(P4Base):
    def results(self, user, **params):
        self.login(user)
        return self.client.get(reverse('staff_students'), params)

    def test_partial_name_searches(self):
        for term in ('Juan', 'dela', 'CRUZ', 'Juan Cruz', '00123', '2026-00123', 'student_aaa1'):
            r = self.results(self.m1, q=term)
            self.assertContains(r, 'Juan Dela Cruz', msg_prefix=term)

    def test_no_results_message(self):
        self.assertContains(self.results(self.m1, q='zzzz'), 'No students found')

    def test_mentor_cannot_find_other_mentors_students(self):
        self.assertNotContains(self.results(self.m1, q='Maria'), 'Maria Santos')
        self.assertNotContains(self.results(self.m1, q='00456'), 'Maria Santos')
        self.assertContains(self.results(self.m2, q='Maria'), 'Maria Santos')

    def test_classroom_filter_and_foreign_room_id(self):
        self.assertContains(self.results(self.m1, room=self.room1.id), 'Juan Dela Cruz')
        # asking for someone else's classroom id returns nothing, not their students
        r = self.results(self.m1, room=self.room2.id)
        self.assertNotContains(r, 'Maria Santos')
        self.assertContains(r, 'No students found')

    def test_removed_students_not_listed(self):
        Enrollment.objects.filter(student=self.s1).update(is_active=False)
        self.assertNotContains(self.results(self.m1, q='Juan'), 'Juan Dela Cruz')

    def test_pagination(self):
        for i in range(12):
            u = make(f'pageuser_{i:02d}x', Profile.ROLE_STUDENT)
            self.join(u, self.room1)
        r = self.results(self.m1)
        self.assertContains(r, 'Page 1 of 2')
        self.assertContains(self.results(self.m1, page=2), 'Page 2 of 2')

    def test_partial_fragment_has_no_layout(self):
        r = self.results(self.m1, q='Juan', partial=1)
        self.assertContains(r, 'Juan Dela Cruz')
        self.assertNotContains(r, '<html')

    def test_student_cannot_use_search(self):
        self.assertEqual(self.results(self.s1, q='Juan').status_code, 403)

    def test_admin_sees_all(self):
        r = self.results(self.admin, q='Santos')
        self.assertContains(r, 'Maria Santos')

    def test_results_show_summary_counts(self):
        l = lesson(self.room1, 'L-one'); lesson(self.room1, 'L-two', order=2)
        LessonProgress.objects.create(user=self.s1, lesson=l, completed=True)
        r = self.results(self.m1, q='Juan')
        self.assertContains(r, '1 / 3')


class ProgressTests(P4Base):
    def setUp(self):
        super().setUp()
        self.la = lesson(self.room1, 'Variables', order=5); self.lb = lesson(self.room1, 'Loops', order=6)
        lesson(self.room1, 'Hidden Draft', published=False, order=7)
        LessonProgress.objects.create(user=self.s1, lesson=self.la, completed=True)
        LessonProgress.objects.create(user=self.s1, lesson=self.lb, completed=False)
        self.qa = self.q1                       # Java Quiz (room1)
        self.qb = quiz(self.room1, 'Loops Quiz')
        self.qc = quiz(self.room1, 'Arrays Quiz')
        self.qd = quiz(self.room1, 'Draft Quiz', published=False)
        self.qb.deadline_datetime = self.now - timedelta(days=2); self.qb.save()     # overdue, unsubmitted
        self.qc.deadline_datetime = self.now + timedelta(days=2); self.qc.save()     # still open
        self.qa.deadline_datetime = self.now + timedelta(days=5); self.qa.save()
        attempt(self.s1, self.qa, 90, 18, 20, self.now - timedelta(hours=3))
        attempt(self.s1, self.qa, 70, 14, 20, self.now - timedelta(hours=1))        # second, worse try

    def url(self, name, user=None, room=None, **kw):
        return reverse(name, args=[(room or self.room1).pk, (user or self.s1).pk])

    def test_numbers_are_real_and_unsubmitted_not_zero(self):
        self.login(self.m1)
        r = self.client.get(self.url('student_progress'))
        self.assertEqual(r.status_code, 200)
        d = r.context['r']
        self.assertEqual((d['lessons_done'], d['lessons_total']), (1, 3))      # base lesson + 2 new; drafts not counted
        self.assertEqual((d['quizzes_submitted'], d['quizzes_total']), (1, 3))  # draft quiz not counted
        self.assertEqual(d['overdue'], 1)
        self.assertEqual(d['average'], 90)                                      # best score; unsubmitted ignored
        row = [x for x in d['rows'] if x['quiz'] == self.qa][0]
        self.assertEqual((row['earned'], row['maximum'], row['percentage']), (18, 20, 90))
        self.assertEqual(row['attempt_count'], 2)
        self.assertEqual(row['grading'], 'finalized')

    def test_no_finalized_scores_shows_message_and_no_average(self):
        QuizAttempt.objects.all().delete()
        self.login(self.m1)
        d = self.client.get(self.url('student_progress')).context['r']
        self.assertIsNone(d['average'])
        self.assertContains(self.client.get(self.url('student_report')), 'No finalized quiz scores')

    def test_legacy_attempt_without_raw_score(self):
        QuizAttempt.objects.all().delete()
        QuizAttempt.objects.create(user=self.s1, quiz=self.qa, score=80)
        self.login(self.m1)
        d = self.client.get(self.url('student_progress')).context['r']
        row = [x for x in d['rows'] if x['quiz'] == self.qa][0]
        self.assertIsNone(row['earned']); self.assertEqual(row['percentage'], 80)
        self.assertEqual(d['average'], 80)

    def test_chart_needs_three_points(self):
        self.login(self.m1)
        self.assertFalse(self.client.get(self.url('student_progress')).context['r']['chart']['enough'])
        attempt(self.s1, self.qb, 60, 6, 10, self.now); attempt(self.s1, self.qc, 80, 8, 10, self.now)
        c = self.client.get(self.url('student_progress')).context['r']['chart']
        self.assertTrue(c['enough']); self.assertEqual(c['count'], 3)

    def test_other_mentor_gets_404_everywhere(self):
        self.login(self.m2)
        a = QuizAttempt.objects.filter(user=self.s1).first()
        for u in (self.url('student_progress'), self.url('student_report'),
                  reverse('student_attempt', args=[self.room1.pk, self.s1.pk, a.pk])):
            self.assertEqual(self.client.get(u).status_code, 404, u)

    def test_student_not_in_that_classroom_is_404(self):
        self.login(self.m1)
        self.assertEqual(self.client.get(self.url('student_progress', user=self.s2)).status_code, 404)
        self.assertEqual(self.client.get(reverse('student_progress', args=[self.room2.pk, self.s2.pk])).status_code, 404)

    def test_students_cannot_open_staff_pages(self):
        self.login(self.s1)
        self.assertEqual(self.client.get(self.url('student_progress')).status_code, 403)
        self.assertEqual(self.client.get(self.url('student_report')).status_code, 403)

    def test_attempt_from_another_student_is_404(self):
        mate = make('mate_student1', Profile.ROLE_STUDENT); self.join(mate, self.room1)
        theirs = attempt(mate, self.qa, 50, 5, 10)
        self.login(self.m1)
        self.assertEqual(self.client.get(reverse('student_attempt', args=[self.room1.pk, self.s1.pk, theirs.pk])).status_code, 404)

    def test_attempt_page_for_own_student(self):
        a = QuizAttempt.objects.filter(user=self.s1).first()
        self.login(self.m1)
        r = self.client.get(reverse('student_attempt', args=[self.room1.pk, self.s1.pk, a.pk]))
        self.assertContains(r, 'Finalized')

    def test_report_content_and_audit(self):
        self.login(self.m1)
        r = self.client.get(self.url('student_report'))
        for text in ('Student Academic Progress Report', 'Juan Dela Cruz', '2026-00123', 'Java 101',
                     'COLEGIO DE SAN GABRIEL ARCANGEL', 'Java Quiz', '18 / 20', '90%', '@page'):
            self.assertContains(r, text)
        self.assertNotContains(r, 'sidebar')
        self.assertTrue(AuditLog.objects.filter(action='progress_report_opened', target_user=self.s1).exists())

    def test_admin_can_view_report(self):
        self.login(self.admin)
        self.assertEqual(self.client.get(self.url('student_report')).status_code, 200)

    def test_removed_student_keeps_records_visible(self):
        Enrollment.objects.filter(student=self.s1).update(is_active=False)
        self.login(self.m1)
        r = self.client.get(self.url('student_progress'))
        self.assertContains(r, 'records kept')

    def test_extension_changes_overdue(self):
        from .models import QuizAccommodation
        QuizAccommodation.objects.create(quiz=self.qb, student=self.s1, kind='EXEMPTION', granted_by=self.m1)
        self.login(self.m1)
        d = self.client.get(self.url('student_progress')).context['r']
        self.assertEqual(d['overdue'], 0)


class StudentIdTests(P4Base):
    def test_student_can_save_id_and_bad_chars_rejected(self):
        self.login(self.s2)
        data = {'name': 'Maria Santos', 'bio': '', 'username': self.s2.username, 'email': self.s2.email, 'student_id': '2026/77'}
        r = self.client.post(reverse('edit_profile'), data)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Profile.objects.get(user=self.s2).student_id, '2026/77')
        data['student_id'] = '<script>'
        self.assertEqual(self.client.post(reverse('edit_profile'), data).status_code, 200)
        self.assertEqual(Profile.objects.get(user=self.s2).student_id, '2026/77')


class MenuToggleTests(P4Base):
    def test_every_role_gets_the_menu_toggle(self):
        for u in (self.admin, self.m1, self.s1):
            self.login(u)
            r = self.client.get(reverse('profile'))
            self.assertContains(r, 'id="nav-toggle"', msg_prefix=u.username)
            self.assertContains(r, 'aria-controls="sidebar-nav"')


class BadgeTests(P4Base):
    def setUp(self):
        super().setUp()
        from . import badges
        self.badges = badges

    def page(self, user):
        self.login(user)
        return self.client.get(reverse('badges'))

    def test_page_shows_every_builtin_badge_with_how_to_earn(self):
        r = self.page(self.s1)
        for name in ('First Steps', 'Quiz Taker', 'Perfectionist', 'Dedicated Learner'):
            self.assertContains(r, name)
        data = r.context['cards_json']
        how = {d['name']: d['how'] for d in data}
        self.assertIn('Finish 5 lessons', how['Dedicated Learner'])
        self.assertIn('Score 100%', how['Perfectionist'])
        self.assertContains(r, 'id="badge-dialog"')

    def test_progress_toward_a_badge_is_real(self):
        for i in range(3):
            l = lesson(self.room1, f'Extra {i}', order=10 + i)
            LessonProgress.objects.create(user=self.s1, lesson=l, completed=True)
        cards = {c['badge'].name: c for c in self.badges.cards(self.s1)}
        self.assertEqual((cards['Dedicated Learner']['current'], cards['Dedicated Learner']['remaining']), (3, 2))
        self.assertTrue(cards['First Steps']['current'] == 1)

    def test_completing_a_lesson_now_awards_first_steps(self):
        l = lesson(self.room1, 'Fresh lesson', order=20)
        self.login(self.s1)
        self.client.post(reverse('lesson_detail', args=[l.slug]), {'action': 'mark_complete'})
        from .models import UserBadge
        self.assertTrue(UserBadge.objects.filter(user=self.s1, badge__name='First Steps').exists())
        data = {d['name']: d for d in self.client.get(reverse('badges')).context['cards_json']}
        self.assertTrue(data['First Steps']['earned'])
        self.assertTrue(data['First Steps']['earned_at'])
        self.assertFalse(data['Perfectionist']['earned'])

    def test_quiz_awards_quiz_taker_and_perfectionist(self):
        from .models import UserBadge
        attempt(self.s1, self.q1, 100, 1, 1)
        self.badges.award(self.s1)
        names = set(UserBadge.objects.filter(user=self.s1).values_list('badge__name', flat=True))
        self.assertEqual(names, {'Quiz Taker', 'Perfectionist'})

    def test_admin_edited_text_is_kept_and_custom_badge_explained(self):
        from .models import Badge
        self.badges.ensure_defined()
        Badge.objects.filter(name='First Steps').update(description='My own wording')
        Badge.objects.create(name='Mentor Pick', description='Chosen by the mentor', icon_class='fa-solid fa-star')
        self.badges.ensure_defined()
        self.assertEqual(Badge.objects.get(name='First Steps').description, 'My own wording')
        data = {d['name']: d for d in self.page(self.s1).context['cards_json']}
        self.assertIn('mentor or an administrator', data['Mentor Pick']['how'])
        self.assertIsNone(data['Mentor Pick']['target'])
