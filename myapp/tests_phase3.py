from datetime import timedelta
from unittest import mock

from django.core import mail
from django.urls import reverse
from django.utils import timezone

from . import deadlines
from .models import (
    Profile, Quiz, QuizAttempt, QuizAccommodation, PolicySettings, FinalWarning,
    Notification, AccountStatusLog, AuditLog, Enrollment,
)
from .tests_phase2 import Base, FAST, quiz, make, PW


@FAST
class P3Base(Base):
    def setUp(self):
        super().setUp()
        self.join(self.s1, self.room1)
        self.join(self.s2, self.room1)
        self.now = timezone.now()
        self.q1.start_datetime = self.now - timedelta(days=10)
        self.q1.deadline_datetime = self.now - timedelta(hours=30)       # already overdue
        self.q1.save()
        self.q3 = quiz(self.room1, 'Second Quiz')
        self.q3.deadline_datetime = self.now - timedelta(hours=40)
        self.q3.save()

    def answer(self, q):
        c = q.questions.first().choices.get(is_correct=True)
        return {f'question_{q.questions.first().id}': c.id}


class DeadlineEnforcement(P3Base):
    def test_closed_after_deadline_when_late_not_allowed(self):
        self.login(self.s1)
        r = self.client.post(reverse('quiz_detail', args=[self.q1.id]), self.answer(self.q1))
        self.assertEqual(r.status_code, 403)
        self.assertFalse(QuizAttempt.objects.filter(user=self.s1, quiz=self.q1).exists())

    def test_get_shows_closed_message_no_form(self):
        self.login(self.s1)
        r = self.client.get(reverse('quiz_detail', args=[self.q1.id]))
        self.assertContains(r, 'Submissions are closed')
        self.assertNotContains(r, 'Submit Answers')

    def test_late_allowed_marks_late(self):
        self.q1.allow_late_submissions = True; self.q1.save()
        self.login(self.s1)
        r = self.client.post(reverse('quiz_detail', args=[self.q1.id]), self.answer(self.q1))
        self.assertEqual(r.status_code, 200)
        a = QuizAttempt.objects.get(user=self.s1, quiz=self.q1)
        self.assertEqual(a.deadline_at_submission, self.q1.deadline_datetime)

    def test_extension_allows_only_that_student(self):
        QuizAccommodation.objects.create(quiz=self.q1, student=self.s1, kind='EXTENSION',
                                         new_deadline=self.now + timedelta(days=1), granted_by=self.m1)
        self.login(self.s1)
        self.client.post(reverse('quiz_detail', args=[self.q1.id]), self.answer(self.q1))
        self.assertTrue(QuizAttempt.objects.filter(user=self.s1, quiz=self.q1).exists())
        self.login(self.s2)
        r = self.client.post(reverse('quiz_detail', args=[self.q1.id]), self.answer(self.q1))
        self.assertEqual(r.status_code, 403)

    def test_mentor_cannot_give_accommodation_in_other_mentors_quiz(self):
        self.login(self.m2)
        r = self.client.get(reverse('quiz_accommodation', args=[self.q1.id, self.s1.id]))
        self.assertEqual(r.status_code, 404)

    def test_student_cannot_open_accommodation_page(self):
        self.login(self.s1)
        r = self.client.get(reverse('quiz_accommodation', args=[self.q1.id, self.s1.id]))
        self.assertEqual(r.status_code, 403)

    def test_mentor_grants_extension_and_student_notified(self):
        self.login(self.m1)
        nd = (self.now + timedelta(days=2)).astimezone(timezone.get_fixed_timezone(480)).strftime('%Y-%m-%dT%H:%M')
        r = self.client.post(reverse('quiz_accommodation', args=[self.q1.id, self.s1.id]),
                             {'kind': 'EXTENSION', 'new_deadline': nd, 'reason': 'sick'})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(QuizAccommodation.objects.filter(quiz=self.q1, student=self.s1).exists())
        self.assertTrue(Notification.objects.filter(user=self.s1, kind='ACCOMMODATION').exists())

    def test_extension_must_be_later(self):
        self.login(self.m1)
        nd = (self.now - timedelta(days=5)).strftime('%Y-%m-%dT%H:%M')
        r = self.client.post(reverse('quiz_accommodation', args=[self.q1.id, self.s1.id]),
                             {'kind': 'EXTENSION', 'new_deadline': nd})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(QuizAccommodation.objects.exists())


class NotificationsAndWarnings(P3Base):
    def run_job(self, now=None):
        return deadlines.run_policy(now or self.now)

    def test_overdue_and_warning_issued_once_with_24h_window(self):
        rep = self.run_job()
        self.assertEqual(rep['warnings'], 2)      # s1 and s2 are both overdue
        w = FinalWarning.objects.get(student=self.s1)
        self.assertGreaterEqual((w.expires_at - w.issued_at), timedelta(hours=24))
        again = self.run_job()                       # idempotent
        self.assertEqual(again['warnings'], 0)
        self.assertEqual(again['overdue'], 0)
        self.assertEqual(FinalWarning.objects.filter(student=self.s1).count(), 1)
        self.assertEqual(Notification.objects.filter(user=self.s1, kind='FINAL_WARNING').count(), 1)

    def test_no_deactivation_before_window_ends(self):
        self.run_job()
        rep = self.run_job(self.now + timedelta(hours=23))
        self.assertEqual(rep['deactivated'] + rep['pending_review'], 0)
        self.assertEqual(Profile.objects.get(user=self.s1).account_status, Profile.ACCOUNT_ACTIVE)

    def test_without_approval_goes_to_review_not_deactivated(self):
        self.run_job()
        rep = self.run_job(self.now + timedelta(hours=25))
        self.assertEqual(rep['pending_review'], 2)       # both students
        self.assertEqual(rep['deactivated'], 0)
        self.assertEqual(Profile.objects.get(user=self.s1).account_status, Profile.ACCOUNT_ACTIVE)

    def test_approved_policy_deactivates_and_keeps_records(self):
        p = PolicySettings.get(); p.auto_deactivation_approved = True; p.save()
        QuizAttempt.objects.create(user=self.s2, quiz=self.q2, score=100, correct_count=1, total_questions=1) if False else None
        self.run_job()
        rep = self.run_job(self.now + timedelta(hours=25))
        self.assertEqual(rep['deactivated'], 2)
        self.assertEqual(Profile.objects.get(user=self.s1).account_status, Profile.ACCOUNT_INACTIVE)
        self.assertTrue(User_exists(self.s1))
        self.assertTrue(AccountStatusLog.objects.filter(user=self.s1, action='DEACTIVATED').exists())

    def test_submitting_before_window_end_prevents_deactivation(self):
        p = PolicySettings.get(); p.auto_deactivation_approved = True; p.save()
        self.q1.allow_late_submissions = True; self.q1.save()
        self.q3.allow_late_submissions = True; self.q3.save()
        self.run_job()
        self.login(self.s1)
        self.client.post(reverse('quiz_detail', args=[self.q1.id]), self.answer(self.q1))
        self.client.post(reverse('quiz_detail', args=[self.q3.id]), self.answer(self.q3))
        self.run_job(self.now + timedelta(hours=25))
        self.assertEqual(Profile.objects.get(user=self.s1).account_status, Profile.ACCOUNT_ACTIVE)
        self.assertEqual(Profile.objects.get(user=self.s2).account_status, Profile.ACCOUNT_INACTIVE)

    def test_exemption_removes_quiz_from_overdue(self):
        QuizAccommodation.objects.create(quiz=self.q1, student=self.s1, kind='EXEMPTION', granted_by=self.m1)
        QuizAccommodation.objects.create(quiz=self.q3, student=self.s1, kind='EXEMPTION', granted_by=self.m1)
        self.assertEqual(deadlines.overdue_items(self.s1), [])

    def test_single_overdue_quiz_below_threshold_no_warning(self):
        self.q3.deadline_datetime = self.now + timedelta(days=3); self.q3.save()
        self.assertEqual(self.run_job()['warnings'], 0)

    def test_reminder_sent_once(self):
        self.q3.deadline_datetime = self.now + timedelta(hours=20); self.q3.save()
        self.run_job(); n1 = Notification.objects.filter(kind='REMINDER', user=self.s1).count()
        self.run_job()
        self.assertEqual(n1, Notification.objects.filter(kind='REMINDER', user=self.s1).count())
        self.assertGreaterEqual(n1, 1)

    def test_email_failure_recorded_and_retried(self):
        with mock.patch('myapp.deadlines.send_mail', side_effect=OSError('smtp down')):
            self.run_job()
        failed = Notification.objects.filter(email_status=Notification.EMAIL_FAILED)
        self.assertTrue(failed.exists())
        self.assertFalse(Notification.objects.filter(email_status=Notification.EMAIL_SENT).exists())
        deadlines.retry_failed_emails()
        self.assertTrue(Notification.objects.filter(email_status=Notification.EMAIL_SENT).exists())

    def test_dry_run_changes_nothing(self):
        deadlines.run_policy(self.now, dry_run=True)
        self.assertEqual(Notification.objects.count(), 0)
        self.assertEqual(FinalWarning.objects.count(), 0)


def User_exists(u):
    from django.contrib.auth.models import User
    return User.objects.filter(pk=u.pk).exists()


class InactiveAndAdmin(P3Base):
    def deactivate(self):
        p = PolicySettings.get(); p.auto_deactivation_approved = True; p.save()
        deadlines.run_policy(self.now); deadlines.run_policy(self.now + timedelta(hours=25))

    def test_inactive_student_blocked_everywhere_but_status_page(self):
        self.deactivate()
        self.login(self.s1)
        for name in ('student_dashboard', 'quizzes', 'lessons'):
            self.assertRedirects(self.client.get(reverse(name)), reverse('account_inactive'), fetch_redirect_response=False)
        r = self.client.post(reverse('quiz_detail', args=[self.q1.id]), self.answer(self.q1))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.get(reverse('account_inactive')).status_code, 200)

    def test_admin_reactivates(self):
        self.deactivate()
        self.login(self.admin)
        self.client.post(reverse('admin_user_reactivate', args=[self.s1.id]), {'note': 'ok'})
        self.assertEqual(Profile.objects.get(user=self.s1).account_status, Profile.ACCOUNT_ACTIVE)
        self.assertTrue(AccountStatusLog.objects.filter(user=self.s1, action='REACTIVATED').exists())

    def test_only_admin_can_reactivate_or_see_monitoring(self):
        self.deactivate()
        for u in (self.m1, self.s2):
            self.login(u)
            self.assertIn(self.client.get(reverse('admin_monitoring')).status_code, (302, 403))
            self.client.post(reverse('admin_user_reactivate', args=[self.s1.id]))
        self.assertEqual(Profile.objects.get(user=self.s1).account_status, Profile.ACCOUNT_INACTIVE)

    def test_admin_monitoring_and_actions(self):
        deadlines.run_policy(self.now); deadlines.run_policy(self.now + timedelta(hours=25))
        self.login(self.admin)
        r = self.client.get(reverse('admin_monitoring'))
        self.assertEqual(r.status_code, 200)
        w = FinalWarning.objects.get(student=self.s1)
        self.assertEqual(w.status, FinalWarning.STATUS_PENDING_REVIEW)
        self.client.post(reverse('admin_warning_action', args=[w.pk]), {'action': 'deactivate'})
        self.assertEqual(Profile.objects.get(user=self.s1).account_status, Profile.ACCOUNT_INACTIVE)

    def test_policy_save_rejects_window_under_24h(self):
        self.login(self.admin)
        self.client.post(reverse('admin_policy_save'), {'reminder_hours': '24,2', 'warn_threshold': 2,
                         'warn_after_overdue_hours': 24, 'warning_window_hours': 5, 'deactivate_min_unresolved': 1})
        self.assertEqual(PolicySettings.get().warning_window_hours, 24)

    def test_student_notifications_page_marks_read(self):
        deadlines.run_policy(self.now)
        self.login(self.s1)
        self.assertEqual(self.client.get(reverse('notifications')).status_code, 200)
        self.assertFalse(Notification.objects.filter(user=self.s1, read_at__isnull=True, hidden=False).exists())

    def test_dashboard_lists_deadlines(self):
        self.login(self.s1)
        self.assertContains(self.client.get(reverse('student_dashboard')), 'Quiz Deadlines')

    def test_management_command(self):
        from django.core.management import call_command
        from io import StringIO
        out = StringIO(); call_command('process_deadlines', '--dry-run', stdout=out)
        self.assertIn('DRY RUN', out.getvalue())
