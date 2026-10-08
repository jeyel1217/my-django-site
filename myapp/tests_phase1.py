import io
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from .models import Profile, TermsVersion, TermsAcceptance, AuditLog


def photo(name='me.png'):
    buf = io.BytesIO()
    Image.new('RGB', (20, 20), 'red').save(buf, 'PNG')
    return SimpleUploadedFile(name, buf.getvalue(), content_type='image/png')


def make(username, role, verified=True, password='Passw0rd!x'):
    u = User.objects.create_user(username=username, email=f'{username}@gmail.com', password=password)
    p = u.profile
    p.role = role
    p.is_verified = verified
    p.save()
    return u


@override_settings(MEDIA_ROOT='/tmp/jcad_test_media')
class TermsTests(TestCase):
    def setUp(self):
        self.student = make('student_one', Profile.ROLE_STUDENT)
        self.mentor = make('mentor_one1', Profile.ROLE_MENTOR)
        self.admin = make('admin_one11', Profile.ROLE_ADMIN)

    def test_seeded_v1_exists(self):
        t = TermsVersion.current()
        self.assertEqual(t.version, '1.0')
        self.assertIn('Rule 1', t.content)
        self.assertIn('Rule 2', t.content)
        self.assertIn('Rule 3', t.content)

    def test_student_without_acceptance_is_blocked_everywhere(self):
        self.client.login(username='student_one', password='Passw0rd!x')
        for name in ['student_dashboard', 'lessons', 'quizzes', 'badges', 'profile', 'edit_profile', 'home']:
            r = self.client.get(reverse(name))
            self.assertRedirects(r, reverse('terms_accept'), fetch_redirect_response=False, msg_prefix=name)
        # direct URL typing as well
        self.assertRedirects(self.client.get('/quizzes/1/'), reverse('terms_accept'), fetch_redirect_response=False)

    def test_accept_page_checkbox_unticked_and_required(self):
        self.client.login(username='student_one', password='Passw0rd!x')
        r = self.client.get(reverse('terms_accept'))
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn('name="agree"', html)
        self.assertNotIn('checked', html.split('name="agree"')[1].split('>')[0])
        self.assertIn('disabled', html)  # button disabled until ticked
        # POST without the box: nothing recorded, error shown
        r = self.client.post(reverse('terms_accept'), {})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'tick the box')
        self.assertEqual(TermsAcceptance.objects.count(), 0)
        self.assertRedirects(self.client.get(reverse('profile')), reverse('terms_accept'), fetch_redirect_response=False)

    def test_accepting_records_user_version_time_and_unblocks(self):
        self.client.login(username='student_one', password='Passw0rd!x')
        r = self.client.post(reverse('terms_accept'), {'agree': 'on'})
        self.assertEqual(r.status_code, 302)
        a = TermsAcceptance.objects.get()
        self.assertEqual(a.user, self.student)
        self.assertEqual(a.terms.version, '1.0')
        self.assertIsNotNone(a.accepted_at)
        self.assertEqual(self.client.get(reverse('profile')).status_code, 200)
        # accepting twice does not duplicate
        self.client.post(reverse('terms_accept'), {'agree': 'on'})
        self.assertEqual(TermsAcceptance.objects.count(), 1)

    def test_mentor_and_admin_not_affected(self):
        for who, name in [('mentor_one1', 'mentor_dashboard'), ('admin_one11', 'admin_dashboard')]:
            self.client.logout()
            self.client.login(username=who, password='Passw0rd!x')
            self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_major_version_forces_reacceptance_minor_does_not(self):
        TermsAcceptance.objects.create(user=self.student, terms=TermsVersion.current())
        self.client.login(username='student_one', password='Passw0rd!x')
        self.assertEqual(self.client.get(reverse('profile')).status_code, 200)

        TermsVersion.objects.create(version='1.1', content='x' * 60, requires_reacceptance=False)
        self.assertEqual(self.client.get(reverse('profile')).status_code, 200)  # minor: not interrupted

        TermsVersion.objects.create(version='2.0', content='y' * 60, requires_reacceptance=True)
        self.assertRedirects(self.client.get(reverse('profile')), reverse('terms_accept'), fetch_redirect_response=False)
        self.client.post(reverse('terms_accept'), {'agree': 'on'})
        self.assertEqual(self.client.get(reverse('profile')).status_code, 200)
        self.assertEqual(TermsAcceptance.objects.filter(user=self.student).count(), 2)

    def test_register_requires_terms_and_records_consent(self):
        data = {'username': 'new_student1', 'email': 'newstudent1@gmail.com',
                'password': 'Str0ng!Passw0rd', 'confirm_password': 'Str0ng!Passw0rd'}
        r = self.client.post(reverse('register'), data)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'must read and accept')
        self.assertFalse(User.objects.filter(username='new_student1').exists())
        r = self.client.post(reverse('register'), dict(data, accept_terms='on'))
        self.assertEqual(r.status_code, 302)
        u = User.objects.get(username='new_student1')
        self.assertTrue(TermsAcceptance.objects.filter(user=u, terms__version='1.0').exists())

    def test_register_page_checkbox_not_prechecked_and_terms_public(self):
        html = self.client.get(reverse('register')).content.decode()
        box = html.split('name="accept_terms"')[1].split('>')[0]
        self.assertNotIn('checked', box)
        self.assertEqual(self.client.get(reverse('terms')).status_code, 200)

    def test_terms_html_is_escaped(self):
        TermsVersion.objects.create(version='9.9', content='## <script>alert(1)</script>\n- <b>x</b>' + 'z' * 60)
        r = self.client.get(reverse('terms'))
        self.assertNotContains(r, '<script>alert(1)</script>')
        self.assertContains(r, '&lt;script&gt;')

    def test_admin_publish_permissions_and_audit(self):
        self.client.login(username='mentor_one1', password='Passw0rd!x')
        self.assertEqual(self.client.get(reverse('admin_terms_list')).status_code, 403)
        self.assertEqual(self.client.post(reverse('admin_terms_publish'), {}).status_code, 403)
        self.client.logout()
        self.client.login(username='admin_one11', password='Passw0rd!x')
        self.assertEqual(self.client.get(reverse('admin_terms_list')).status_code, 200)
        r = self.client.post(reverse('admin_terms_publish'), {
            'version': '1.1', 'title': 'T', 'content': 'A' * 80, 'requires_reacceptance': 'on'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(TermsVersion.current().version, '1.1')
        self.assertTrue(AuditLog.objects.filter(action='terms_published').exists())
        # duplicate label rejected
        r = self.client.post(reverse('admin_terms_publish'), {'version': '1.1', 'title': 'T', 'content': 'B' * 80})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'already exists')

    def test_admin_sees_acceptance_status(self):
        self.client.login(username='admin_one11', password='Passw0rd!x')
        r = self.client.get(reverse('admin_user_detail', args=[self.student.id]))
        self.assertContains(r, 'Not accepted')
        TermsAcceptance.objects.create(user=self.student, terms=TermsVersion.current())
        r = self.client.get(reverse('admin_user_detail', args=[self.student.id]))
        self.assertContains(r, 'Accepted')
        self.assertContains(self.client.get(reverse('admin_user_list')), 'Accepted')


@override_settings(MEDIA_ROOT='/tmp/jcad_test_media')
class ProfileAndPhotoTests(TestCase):
    def setUp(self):
        self.student = make('student_two', Profile.ROLE_STUDENT)
        self.admin = make('admin_two22', Profile.ROLE_ADMIN)
        TermsAcceptance.objects.create(user=self.student, terms=TermsVersion.current())

    def test_incomplete_profile_reminder_with_button(self):
        self.client.login(username='student_two', password='Passw0rd!x')
        r = self.client.get(reverse('student_dashboard'))
        self.assertContains(r, 'Your profile is incomplete')
        self.assertContains(r, 'Full name, Profile photo')
        self.assertContains(r, reverse('edit_profile'))
        self.assertEqual(self.student.profile.profile_completion_percent, 0)

    def test_complete_profile_has_no_reminder(self):
        p = self.student.profile
        p.name = 'Juan Dela Cruz'
        p.avatar = photo()
        p.save()
        self.client.login(username='student_two', password='Passw0rd!x')
        r = self.client.get(reverse('student_dashboard'))
        self.assertNotContains(r, 'Your profile is incomplete')
        self.assertTrue(Profile.objects.get(pk=p.pk).is_profile_complete)

    def test_mentor_never_sees_student_reminder(self):
        make('mentor_two22', Profile.ROLE_MENTOR)
        self.client.login(username='mentor_two22', password='Passw0rd!x')
        self.assertNotContains(self.client.get(reverse('mentor_dashboard')), 'Your profile is incomplete')

    def test_admin_flags_photo_student_sees_notice_and_replaces(self):
        p = self.student.profile
        p.name = 'Juan Dela Cruz'
        p.avatar = photo('anime.png')
        p.save()

        self.client.login(username='admin_two22', password='Passw0rd!x')
        r = self.client.post(reverse('admin_user_flag_photo', args=[self.student.id]),
                             {'reason': 'Use a real photo of yourself.'})
        self.assertEqual(r.status_code, 302)
        p.refresh_from_db()
        self.assertTrue(p.avatar_flagged)
        self.assertFalse(p.is_profile_complete)          # flagged photo is not "complete"
        self.assertTrue(self.student.is_active)           # never deactivated
        self.assertTrue(AuditLog.objects.filter(action='photo_flagged', target_user=self.student).exists())

        self.client.logout()
        self.client.login(username='student_two', password='Passw0rd!x')
        r = self.client.get(reverse('student_dashboard'))
        self.assertContains(r, 'Please replace your profile photo')
        self.assertContains(r, 'Use a real photo of yourself.')

        # replacing the photo lifts the flag
        r = self.client.post(reverse('edit_profile'), {
            'username': 'student_two', 'email': 'student_two@gmail.com',
            'name': 'Juan Dela Cruz', 'bio': '', 'avatar': photo('real.png')})
        self.assertEqual(r.status_code, 302, getattr(r, 'context', None) and r.context['profile_form'].errors)
        p.refresh_from_db()
        self.assertFalse(p.avatar_flagged)
        self.assertTrue(p.is_profile_complete)
        self.assertNotContains(self.client.get(reverse('student_dashboard')), 'Please replace your profile photo')

    def test_non_admin_cannot_flag(self):
        p = self.student.profile
        p.avatar = photo()
        p.save()
        self.client.login(username='student_two', password='Passw0rd!x')
        r = self.client.post(reverse('admin_user_flag_photo', args=[self.student.id]), {'reason': 'nope nope'})
        self.assertEqual(r.status_code, 403)

    def test_flag_requires_reason_and_photo(self):
        self.client.login(username='admin_two22', password='Passw0rd!x')
        self.client.post(reverse('admin_user_flag_photo', args=[self.student.id]), {'reason': 'long enough reason'})
        self.assertFalse(Profile.objects.get(user=self.student).avatar_flagged)  # no photo uploaded


class PasswordToggleTests(TestCase):
    def test_toggle_included_on_every_password_page(self):
        for name in ['login', 'register']:
            self.assertContains(self.client.get(reverse(name)), "password_toggle" if False else 'class="pw-wrap"' if False else 'pw-eye', msg_prefix=name)
        admin = make('admin_three3', Profile.ROLE_ADMIN)
        mentor = make('mentor_thr33', Profile.ROLE_MENTOR)
        self.client.login(username='admin_three3', password='Passw0rd!x')
        self.assertContains(self.client.get(reverse('admin_mentor_create')), 'pw-eye')
        self.assertContains(self.client.get(reverse('admin_user_reset_password', args=[mentor.id])), 'pw-eye')
