from datetime import timedelta
from zoneinfo import ZoneInfo

from django.urls import reverse
from django.utils import timezone

from .models import Quiz
from .tests_phase2 import Base, FAST


def stamp(dt):
    # The pages show and read Philippine time.
    return dt.astimezone(ZoneInfo('Asia/Manila')).strftime('%Y-%m-%dT%H:%M')


@FAST
class DeadlineNotInThePastTests(Base):
    def data(self, **kw):
        d = {'classroom': self.room1.pk, 'title': 'DL', 'description': '', 'instructions': '', 'has_deadline': 'on'}
        d.update(kw)
        return d

    def test_past_deadline_refused_on_create(self):
        self.login(self.m1)
        for when in (timezone.now() - timedelta(days=400), timezone.now() - timedelta(hours=2)):
            r = self.client.post(reverse('mentor_quiz_create'), self.data(deadline_datetime=stamp(when)))
            self.assertEqual(r.status_code, 200)
            self.assertContains(r, 'cannot be in the past')
            self.assertFalse(Quiz.objects.filter(title='DL').exists())

    def test_future_deadline_accepted(self):
        self.login(self.m1)
        r = self.client.post(reverse('mentor_quiz_create'),
                             self.data(deadline_datetime=stamp(timezone.now() + timedelta(days=2))))
        self.assertEqual(r.status_code, 302)
        self.assertIsNotNone(Quiz.objects.get(title='DL').deadline_datetime)

    def test_no_deadline_still_fine(self):
        self.login(self.m1)
        d = self.data(); d.pop('has_deadline')
        self.assertEqual(self.client.post(reverse('mentor_quiz_create'), d).status_code, 302)

    def test_edit_cannot_move_deadline_into_the_past(self):
        self.login(self.m1)
        self.q1.deadline_datetime = timezone.now() + timedelta(days=3); self.q1.save()
        d = self.data(title=self.q1.title, deadline_datetime=stamp(timezone.now() - timedelta(days=1)))
        r = self.client.post(reverse('mentor_quiz_edit', args=[self.q1.pk]), d)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'cannot be in the past')

    def test_old_deadline_left_unchanged_does_not_block_other_edits(self):
        self.login(self.m1)
        past = (timezone.now() - timedelta(days=2)).replace(second=0, microsecond=0)
        self.q1.deadline_datetime = past; self.q1.save()
        d = self.data(title='Renamed', deadline_datetime=stamp(past))
        r = self.client.post(reverse('mentor_quiz_edit', args=[self.q1.pk]), d)
        self.assertEqual(r.status_code, 302)
        self.q1.refresh_from_db()
        self.assertEqual(self.q1.title, 'Renamed')

    def test_date_picker_starts_today(self):
        self.login(self.m1)
        r = self.client.get(reverse('mentor_quiz_create'))
        self.assertContains(r, 'min="%s' % stamp(timezone.now())[:11])
