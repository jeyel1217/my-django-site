from django.test import TestCase
from django.urls import reverse
from django.contrib.auth.models import User
from .models import Profile
class Resend(TestCase):
    def test_page_renders_both_states(self):
        u = User.objects.create_user('student_xx9', 's@gmail.com', 'Str0ng@Pass'); u.profile.is_verified = False; u.profile.save()
        self.client.login(username='student_xx9', password='Str0ng@Pass')
        r = self.client.get(reverse('verify_email')); self.assertContains(r, 'id="resend-secs"'); self.assertContains(r, 'Resend Confirmation Link')
