from django.test import TestCase
from django.urls import reverse, NoReverseMatch
from django.contrib.auth.models import User
from .models import Profile

class MentorNoGmail(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user('admin_user1', 'a@gmail.com', 'Passw0rd!x')
        self.admin.profile.role = Profile.ROLE_ADMIN; self.admin.profile.save()
        self.client.login(username='admin_user1', password='Passw0rd!x')

    def test_create_mentor_without_gmail(self):
        r = self.client.get(reverse('admin_mentor_create'))
        self.assertNotContains(r, 'GMAIL ADDRESS'); self.assertNotIn('email', r.context['form'].fields)
        r = self.client.post(reverse('admin_mentor_create'), {'username': 'mentor_one1', 'password': 'Str0ng@Pass'})
        self.assertEqual(r.status_code, 302)
        m = User.objects.get(username='mentor_one1')
        self.assertEqual(m.email, ''); self.assertEqual(m.profile.role, Profile.ROLE_MENTOR); self.assertTrue(m.profile.is_verified)
        # a weak password is still refused
        r = self.client.post(reverse('admin_mentor_create'), {'username': 'mentor_two2', 'password': 'weak'})
        self.assertEqual(r.status_code, 200); self.assertFalse(User.objects.filter(username='mentor_two2').exists())
        # two mentors with no email can both exist
        self.client.post(reverse('admin_mentor_create'), {'username': 'mentor_three', 'password': 'Str0ng@Pass'})
        self.assertTrue(User.objects.filter(username='mentor_three').exists())

    def test_mentor_logs_in_and_locks_then_admin_unlocks(self):
        self.client.post(reverse('admin_mentor_create'), {'username': 'mentor_one1', 'password': 'Str0ng@Pass'})
        m = User.objects.get(username='mentor_one1')
        self.client.logout()
        for _ in range(3):
            r = self.client.post(reverse('login'), {'username': 'mentor_one1', 'password': 'wrong'})
        self.assertContains(r, 'ask your Admin'); m.profile.refresh_from_db(); self.assertTrue(m.profile.is_locked)
        r = self.client.post(reverse('login'), {'username': 'mentor_one1', 'password': 'Str0ng@Pass'})
        self.assertContains(r, 'locked')
        # forgot password for a no-email account does not crash or leak
        r = self.client.post(reverse('forgot_password_request'), {'username_or_email': 'mentor_one1'}, follow=True)
        self.assertEqual(r.status_code, 200)
        # admin sees lock + unlocks
        self.client.login(username='admin_user1', password='Passw0rd!x')
        r = self.client.get(reverse('admin_user_detail', args=[m.id]))
        self.assertContains(r, 'Unlock Account'); self.assertContains(r, 'Reset Password')
        self.client.post(reverse('admin_user_unlock', args=[m.id]))
        m.profile.refresh_from_db(); self.assertFalse(m.profile.is_locked); self.assertEqual(m.profile.failed_login_attempts, 0)
        self.client.logout()
        r = self.client.post(reverse('login'), {'username': 'mentor_one1', 'password': 'Str0ng@Pass'})
        self.assertEqual(r.status_code, 302)

    def test_admin_reset_password(self):
        self.client.post(reverse('admin_mentor_create'), {'username': 'mentor_one1', 'password': 'Str0ng@Pass'})
        m = User.objects.get(username='mentor_one1'); m.profile.is_locked = True; m.profile.failed_login_attempts = 3; m.profile.save()
        url = reverse('admin_user_reset_password', args=[m.id])
        self.assertEqual(self.client.get(url).status_code, 200)
        r = self.client.post(url, {'new_password': 'weak', 'confirm_password': 'weak'}); self.assertEqual(r.status_code, 200)
        r = self.client.post(url, {'new_password': 'N3w@Passw0rd', 'confirm_password': 'Different1!'}); self.assertEqual(r.status_code, 200)
        r = self.client.post(url, {'new_password': 'N3w@Passw0rd', 'confirm_password': 'N3w@Passw0rd'}); self.assertEqual(r.status_code, 302)
        m.refresh_from_db(); self.assertTrue(m.check_password('N3w@Passw0rd')); self.assertFalse(m.profile.is_locked)

    def test_roles_cannot_use_admin_tools(self):
        m = User.objects.create_user('mentor_xx99', '', 'Str0ng@Pass'); m.profile.role = Profile.ROLE_MENTOR; m.profile.save()
        s = User.objects.create_user('student_xx9', 's@gmail.com', 'Str0ng@Pass')
        from myapp.models import TermsAcceptance, TermsVersion
        TermsAcceptance.objects.create(user=s, terms=TermsVersion.current())  # accepted, so the role check (403) is what we test
        for u in ('mentor_xx99', 'student_xx9'):
            self.client.logout(); self.client.login(username=u, password='Str0ng@Pass')
            self.assertEqual(self.client.get(reverse('admin_user_reset_password', args=[s.id])).status_code, 403)
            self.assertEqual(self.client.post(reverse('admin_user_unlock', args=[s.id])).status_code, 403)
        # superuser cannot be reset through the form
        su = User.objects.create_superuser('super_user1', 's@x.com', 'Str0ng@Pass')
        self.client.logout(); self.client.login(username='admin_user1', password='Passw0rd!x')
        self.assertEqual(self.client.get(reverse('admin_user_reset_password', args=[su.id])).status_code, 302)

    def test_admin_exam_monitoring_gone_mentor_kept(self):
        with self.assertRaises(NoReverseMatch): reverse('admin_exam_overview')
        r = self.client.get(reverse('admin_dashboard')); self.assertNotContains(r, 'Exam Monitoring')
        self.client.logout()
        m = User.objects.create_user('mentor_xx99', '', 'Str0ng@Pass'); m.profile.role = Profile.ROLE_MENTOR; m.profile.save()
        self.client.login(username='mentor_xx99', password='Str0ng@Pass')
        r = self.client.get(reverse('mentor_progress_overview')); self.assertEqual(r.status_code, 200); self.assertContains(r, 'Student Progress')
