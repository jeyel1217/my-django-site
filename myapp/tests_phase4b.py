from decimal import Decimal

from django.http import QueryDict
from django.urls import reverse

from . import quizzing, progress
from .models import (
    Profile, Quiz, Question, Choice, BlankAnswer, QuizAttempt, AttemptAnswer, Notification, UserBadge,
)
from .tests_phase2 import Base, FAST


def post(**kw):
    q = QueryDict(mutable=True)
    for k, v in kw.items():
        if isinstance(v, (list, tuple)):
            q.setlist(k, [str(x) for x in v])
        else:
            q[k] = str(v)
    return q


def mc(quiz_, text='Pick', right=(0,), n=3, multi=False, points=1):
    q = Question.objects.create(quiz=quiz_, text=text, qtype='MC', allow_multiple=multi, points=points)
    cs = [Choice.objects.create(question=q, text=f'c{i}', is_correct=i in right) for i in range(n)]
    return q, cs


def fill(quiz_, text='Java is a [blank] language', blanks=None, points=2, case=False):
    q = Question.objects.create(quiz=quiz_, text=text, qtype='FILL', points=points, case_sensitive=case)
    for number, answers in (blanks or {1: ['object-oriented']}).items():
        for a in answers:
            BlankAnswer.objects.create(question=q, blank_number=number, text=a)
    return q


def essay(quiz_, text='Explain OOP', points=10, guide='mention classes'):
    return Question.objects.create(quiz=quiz_, text=text, qtype='ESSAY', points=points, guide=guide)


@FAST
class P4BBase(Base):
    def setUp(self):
        super().setUp()
        self.join(self.s1, self.room1)
        self.join(self.s2, self.room2)
        self.qz = Quiz.objects.create(title='Mixed', created_by=self.m1, classroom=self.room1, is_published=True)

    def submit(self, user, quiz_, data):
        self.login(user)
        return self.client.post(reverse('quiz_detail', args=[quiz_.pk]), data)


class GradingTests(P4BBase):
    def attempt_for(self, data, **kw):
        return quizzing.submit_attempt(self.s1, self.qz, post(**data), None, None)

    def test_single_choice_correct_and_wrong(self):
        q, cs = mc(self.qz)
        a = self.attempt_for({f'question_{q.id}': cs[0].id})
        self.assertEqual((a.score, a.grading_status), (100, 'finalized'))
        b = self.attempt_for({f'question_{q.id}': cs[1].id})
        self.assertEqual(b.score, 0)

    def test_choice_from_another_question_is_ignored(self):
        q, cs = mc(self.qz)
        other, ocs = mc(self.qz, 'Other', right=(0,))
        a = self.attempt_for({f'question_{q.id}': ocs[0].id})     # forged id
        row = a.answers.get(question=q)
        self.assertFalse(row.answered)
        self.assertEqual(a.score, 0)

    def test_multi_answer_needs_exact_set(self):
        q, cs = mc(self.qz, right=(0, 1), multi=True, n=4)
        full = self.attempt_for({f'question_{q.id}': [cs[0].id, cs[1].id]})
        part = self.attempt_for({f'question_{q.id}': [cs[0].id]})
        extra = self.attempt_for({f'question_{q.id}': [cs[0].id, cs[1].id, cs[2].id]})
        self.assertEqual((full.score, part.score, extra.score), (100, 0, 0))

    def test_single_choice_question_keeps_only_one_pick(self):
        q, cs = mc(self.qz, right=(0,))
        a = self.attempt_for({f'question_{q.id}': [cs[1].id, cs[0].id]})
        self.assertEqual(a.answers.get().answer['choices'], [cs[1].id])

    def test_fill_case_and_spaces_ignored_by_default(self):
        q = fill(self.qz, blanks={1: ['object oriented']})
        a = self.attempt_for({f'question_{q.id}_b1': '  Object    ORIENTED '})
        self.assertEqual(a.score, 100)

    def test_fill_case_sensitive_option(self):
        q = fill(self.qz, text='Language [blank]', blanks={1: ['Java']}, case=True)
        self.assertEqual(self.attempt_for({f'question_{q.id}_b1': 'java'}).score, 0)
        self.assertEqual(self.attempt_for({f'question_{q.id}_b1': ' Java '}).score, 100)

    def test_fill_several_accepted_answers(self):
        q = fill(self.qz, text='A [blank]', blanks={1: ['int', 'integer']})
        self.assertEqual(self.attempt_for({f'question_{q.id}_b1': 'INTEGER'}).score, 100)
        self.assertEqual(self.attempt_for({f'question_{q.id}_b1': 'long'}).score, 0)

    def test_fill_multiple_blanks_earn_points_per_blank(self):
        q = fill(self.qz, text='[blank] and [blank]', blanks={1: ['a'], 2: ['b']}, points=4)
        a = self.attempt_for({f'question_{q.id}_b1': 'a', f'question_{q.id}_b2': 'zzz'})
        row = a.answers.get()
        self.assertEqual(row.points_awarded, Decimal('2.00'))
        self.assertFalse(row.is_correct)
        self.assertEqual(a.score, 50)

    def test_essay_is_never_auto_graded(self):
        q = essay(self.qz)
        a = self.attempt_for({f'question_{q.id}': 'My long answer'})
        self.assertEqual(a.grading_status, 'pending')
        self.assertEqual(a.score, 0)
        row = a.answers.get()
        self.assertIsNone(row.points_awarded)
        self.assertTrue(row.needs_grading)

    def test_empty_essay_needs_no_grading(self):
        q = essay(self.qz)
        a = self.attempt_for({f'question_{q.id}': '   '})
        self.assertEqual((a.grading_status, a.score), ('finalized', 0))

    def test_mixed_quiz_stays_pending_until_every_essay_marked(self):
        q1, cs = mc(self.qz, points=5)
        e1 = essay(self.qz, points=10)
        e2 = essay(self.qz, 'Second', points=5)
        a = self.attempt_for({f'question_{q1.id}': cs[0].id, f'question_{e1.id}': 'x', f'question_{e2.id}': 'y'})
        self.assertEqual(a.grading_status, 'pending')
        ans = {r.question_id: r for r in a.answers.all()}
        # mark only the first essay: still pending
        quizzing.grade_essays(a, self.m1, {ans[e1.id].id: ('8', 'good'), ans[e2.id].id: ('', '')})
        a.refresh_from_db()
        self.assertEqual(a.grading_status, 'pending')
        self.assertEqual(a.score, 0)
        self.assertFalse(Notification.objects.filter(user=self.s1, kind='GRADED').exists())
        # mark the second: now final = (5 + 8 + 4) / 20
        quizzing.grade_essays(a, self.m1, {ans[e1.id].id: ('8', 'good'), ans[e2.id].id: ('4', '')})
        a.refresh_from_db()
        self.assertEqual((a.grading_status, a.score), ('finalized', 85))
        self.assertEqual(a.graded_by, self.m1)
        self.assertEqual(Notification.objects.filter(user=self.s1, kind='GRADED').count(), 1)
        self.assertEqual(ans[e1.id].__class__.objects.get(pk=ans[e1.id].pk).feedback, 'good')

    def test_marks_outside_range_are_rejected_and_nothing_saved(self):
        e = essay(self.qz, points=10)
        a = self.attempt_for({f'question_{e.id}': 'x'})
        row = a.answers.get()
        for bad in ('11', '-1', 'abc'):
            with self.assertRaises(quizzing.GradeError):
                quizzing.grade_essays(a, self.m1, {row.id: (bad, 'f')})
        a.refresh_from_db(); row.refresh_from_db()
        self.assertEqual(a.grading_status, 'pending')
        self.assertIsNone(row.points_awarded)

    def test_regrading_final_attempt_updates_score(self):
        e = essay(self.qz, points=10)
        a = self.attempt_for({f'question_{e.id}': 'x'})
        row = a.answers.get()
        quizzing.grade_essays(a, self.m1, {row.id: ('10', '')})
        a.refresh_from_db(); self.assertEqual(a.score, 100)
        quizzing.grade_essays(a, self.m1, {row.id: ('5', '')})
        a.refresh_from_db(); self.assertEqual(a.score, 50)
        self.assertEqual(Notification.objects.filter(kind='GRADED').count(), 1)    # told once


class SubmitViewTests(P4BBase):
    def test_duplicate_token_creates_one_attempt(self):
        q, cs = mc(self.qz)
        data = {f'question_{q.id}': cs[0].id, 'submit_token': 'abc123'}
        r1 = self.submit(self.s1, self.qz, data)
        r2 = self.client.post(reverse('quiz_detail', args=[self.qz.pk]), data)      # same session, same token
        self.assertEqual((r1.status_code, r2.status_code), (200, 200))
        self.assertEqual(QuizAttempt.objects.filter(user=self.s1, quiz=self.qz).count(), 1)

    def test_pending_page_wording_and_no_score_shown(self):
        e = essay(self.qz)
        r = self.submit(self.s1, self.qz, {f'question_{e.id}': 'text'})
        self.assertContains(r, 'Pending grading')
        self.assertNotContains(r, 'text-5xl')           # the big percentage is not shown

    def test_student_page_has_inputs_for_each_type_and_no_answer_key(self):
        q, cs = mc(self.qz, 'Choose', right=(1,))
        f = fill(self.qz, blanks={1: ['SECRETWORD']})
        e = essay(self.qz, guide='SECRETGUIDE')
        self.login(self.s1)
        r = self.client.get(reverse('quiz_detail', args=[self.qz.pk]))
        self.assertContains(r, f'name="question_{q.id}"')
        self.assertContains(r, f'name="question_{f.id}_b1"')
        self.assertContains(r, f'<textarea name="question_{e.id}"')
        self.assertContains(r, 'submit_token')
        for secret in ('SECRETWORD', 'SECRETGUIDE'):
            self.assertNotContains(r, secret)

    def test_deadline_rules_still_apply_to_essay_quizzes(self):
        from datetime import timedelta
        from django.utils import timezone
        e = essay(self.qz)
        self.qz.deadline_datetime = timezone.now() - timedelta(hours=1); self.qz.save()
        r = self.submit(self.s1, self.qz, {f'question_{e.id}': 'late'})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(QuizAttempt.objects.count(), 0)

    def test_student_cannot_open_someone_elses_result(self):
        q, cs = mc(self.qz)
        self.submit(self.s1, self.qz, {f'question_{q.id}': cs[0].id})
        a = QuizAttempt.objects.get()
        self.login(self.s1)
        self.assertEqual(self.client.get(reverse('quiz_attempt_result', args=[self.qz.pk, a.pk])).status_code, 200)
        self.client.logout()
        s3 = self.s2
        self.join(s3, self.room1)
        self.login(s3)
        self.assertEqual(self.client.get(reverse('quiz_attempt_result', args=[self.qz.pk, a.pk])).status_code, 404)


class PendingNeverCountsTests(P4BBase):
    def test_pending_is_not_zero_in_average_best_or_badges(self):
        q, cs = mc(self.qz, points=1)
        self.submit(self.s1, self.qz, {f'question_{q.id}': cs[0].id})            # 100%
        e = essay(self.qz)
        quiz2 = Quiz.objects.create(title='Essay only', created_by=self.m1, classroom=self.room1, is_published=True)
        e2 = essay(quiz2)
        self.submit(self.s1, quiz2, {f'question_{e2.id}': 'text'})                # pending
        self.assertEqual(self.s1.profile.quiz_score_avg, 100)                      # not 50
        rep = progress.full_report(self.s1, self.room1)
        self.assertEqual(rep['average'], 100)
        self.assertEqual(rep['pending_count'], 1)
        row = [r for r in rep['rows'] if r['quiz'].pk == quiz2.pk][0]
        self.assertIsNone(row['percentage'])
        self.assertEqual(row['grading'], progress.PENDING)

    def test_perfectionist_not_awarded_by_pending_attempt(self):
        quiz2 = Quiz.objects.create(title='Essay only', created_by=self.m1, classroom=self.room1, is_published=True)
        e2 = essay(quiz2)
        QuizAttempt.objects.filter(user=self.s1).delete()
        self.submit(self.s1, quiz2, {f'question_{e2.id}': 'x'})
        a = QuizAttempt.objects.get()
        QuizAttempt.objects.filter(pk=a.pk).update(score=100)        # even with a stored 100, pending must not count
        from . import badges
        badges.award(self.s1)
        self.assertFalse(UserBadge.objects.filter(user=self.s1, badge__name='Perfectionist').exists())

    def test_mentor_progress_table_shows_pending(self):
        e = essay(self.qz)
        self.submit(self.s1, self.qz, {f'question_{e.id}': 'x'})
        self.login(self.m1)
        r = self.client.get(reverse('mentor_exam_progress', args=[self.qz.pk]))
        self.assertContains(r, 'Pending grading')

    def test_quiz_list_says_pending(self):
        e = essay(self.qz)
        self.submit(self.s1, self.qz, {f'question_{e.id}': 'x'})
        self.assertContains(self.client.get(reverse('quizzes')), 'Pending grading')


class GradingScreenTests(P4BBase):
    def setUp(self):
        super().setUp()
        self.e = essay(self.qz, points=10)
        self.submit(self.s1, self.qz, {f'question_{self.e.id}': 'my essay'})
        self.attempt = QuizAttempt.objects.get()
        self.row = self.attempt.answers.get()

    def test_owner_mentor_grades(self):
        self.login(self.m1)
        self.assertContains(self.client.get(reverse('mentor_grading')), 'Java Quiz' if False else 'Mixed')
        r = self.client.get(reverse('mentor_grade_attempt', args=[self.attempt.pk]))
        self.assertContains(r, 'my essay')
        r = self.client.post(reverse('mentor_grade_attempt', args=[self.attempt.pk]),
                             {f'points_{self.row.id}': '7.5', f'feedback_{self.row.id}': 'nice'})
        self.assertRedirects(r, reverse('mentor_grading'), fetch_redirect_response=False)
        self.attempt.refresh_from_db()
        self.assertEqual((self.attempt.grading_status, self.attempt.score), ('finalized', 75))

    def test_out_of_range_mark_shows_error(self):
        self.login(self.m1)
        r = self.client.post(reverse('mentor_grade_attempt', args=[self.attempt.pk]), {f'points_{self.row.id}': '99'}, follow=True)
        self.assertContains(r, 'marks must be between 0 and 10')
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.grading_status, 'pending')

    def test_other_mentor_student_and_admin_blocked(self):
        for user in (self.m2, self.s1, self.s2):
            self.login(user)
            r = self.client.post(reverse('mentor_grade_attempt', args=[self.attempt.pk]), {f'points_{self.row.id}': '10'})
            self.assertIn(r.status_code, (302, 403, 404))
            self.assertNotEqual(r.status_code, 200)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.grading_status, 'pending')

    def test_grading_list_only_shows_own_quizzes(self):
        self.login(self.m2)
        self.assertNotContains(self.client.get(reverse('mentor_grading')), 'Mixed')

    def test_student_sees_feedback_after_grading(self):
        self.login(self.m1)
        self.client.post(reverse('mentor_grade_attempt', args=[self.attempt.pk]),
                         {f'points_{self.row.id}': '10', f'feedback_{self.row.id}': 'Well argued'})
        self.login(self.s1)
        r = self.client.get(reverse('quiz_attempt_result', args=[self.qz.pk, self.attempt.pk]))
        self.assertContains(r, 'Well argued')
        self.assertContains(r, '100%')

    def test_staff_attempt_page_shows_answers(self):
        self.login(self.m1)
        r = self.client.get(reverse('student_attempt', args=[self.room1.pk, self.s1.pk, self.attempt.pk]))
        self.assertContains(r, 'my essay')
        self.assertContains(r, 'Grade essays')


class EditorTests(P4BBase):
    def url(self):
        return reverse('mentor_quiz_questions', args=[self.qz.pk])

    def add(self, **fields):
        self.login(self.m1)
        base = {'qtype': 'MC', 'text': 'What?', 'points': '1'}
        base.update(fields)
        return self.client.post(self.url(), base)

    def test_create_each_type(self):
        r = self.add(qtype='MC', choice_text=['a', 'b', 'c'], choice_correct=['1'])
        self.assertEqual(r.status_code, 302)
        r = self.add(qtype='FILL', text='Java is [blank]', blank_answers_1='fun\nFUN!', points='3')
        self.assertEqual(r.status_code, 302)
        r = self.add(qtype='ESSAY', text='Discuss', points='10', guide='g')
        self.assertEqual(r.status_code, 302)
        types = list(self.qz.questions.order_by('id').values_list('qtype', flat=True))
        self.assertEqual(types, ['MC', 'FILL', 'ESSAY'])
        self.assertEqual(self.qz.total_points, 14)
        fq = self.qz.questions.get(qtype='FILL')
        self.assertEqual(list(fq.blank_answers.values_list('text', flat=True)), ['fun', 'FUN!'])

    def test_multi_answer_creation(self):
        r = self.add(qtype='MC', allow_multiple='on', choice_text=['a', 'b', 'c'], choice_correct=['0', '2'])
        self.assertEqual(r.status_code, 302)
        q = self.qz.questions.get()
        self.assertTrue(q.allow_multiple)
        self.assertEqual(q.choices.filter(is_correct=True).count(), 2)

    def test_validation_errors_save_nothing(self):
        cases = [
            dict(qtype='MC', choice_text=['a', 'b'], ),                                         # nothing marked correct
            dict(qtype='MC', choice_text=['a', 'b', 'c'], choice_correct=['0', '1']),           # two right, not multi
            dict(qtype='MC', allow_multiple='on', choice_text=['a', 'b'], choice_correct=['0']),  # multi with one right
            dict(qtype='MC', choice_text=['only'], choice_correct=['0']),                       # one choice
            dict(qtype='MC', choice_text=['same', 'Same ', 'c'], choice_correct=['0']),         # duplicate
            dict(qtype='FILL', text='no blank here'),                                           # no [blank]
            dict(qtype='FILL', text='x [blank] y [blank]', blank_answers_1='a'),                # blank 2 empty
            dict(qtype='ESSAY', points='0'),                                                    # bad points
            dict(qtype='ESSAY', points='101'),
            dict(qtype='MC', text='   ', choice_text=['a', 'b'], choice_correct=['0']),         # empty text
        ]
        for c in cases:
            r = self.add(**c)
            self.assertEqual(r.status_code, 200, c)
        self.assertEqual(self.qz.questions.count(), 0)

    def test_other_mentor_cannot_add_edit_or_delete(self):
        q, cs = mc(self.qz)
        self.login(self.m2)
        self.assertEqual(self.client.get(self.url()).status_code, 404)
        self.assertEqual(self.client.post(self.url(), {'qtype': 'ESSAY', 'text': 'x', 'points': 1}).status_code, 404)
        self.assertEqual(self.client.get(reverse('mentor_question_edit', args=[self.qz.pk, q.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse('mentor_question_delete', args=[self.qz.pk, q.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse('mentor_quiz_preview', args=[self.qz.pk])).status_code, 404)
        self.assertTrue(Question.objects.filter(pk=q.pk).exists())

    def test_students_cannot_use_editor(self):
        self.login(self.s1)
        self.assertNotEqual(self.client.get(self.url()).status_code, 200)

    def test_edit_form_prefills_and_saves(self):
        q = fill(self.qz, text='A [blank] B', blanks={1: ['x', 'y']})
        self.login(self.m1)
        r = self.client.get(reverse('mentor_question_edit', args=[self.qz.pk, q.pk]))
        self.assertContains(r, 'A [blank] B')
        r = self.client.post(reverse('mentor_question_edit', args=[self.qz.pk, q.pk]),
                             {'qtype': 'FILL', 'text': 'A [blank] B', 'points': '5', 'blank_answers_1': 'z'})
        self.assertEqual(r.status_code, 302)
        q.refresh_from_db()
        self.assertEqual(q.points, 5)
        self.assertEqual(list(q.blank_answers.values_list('text', flat=True)), ['z'])

    def test_editing_never_changes_a_graded_attempt(self):
        q, cs = mc(self.qz, 'Original wording', right=(0,))
        self.submit(self.s1, self.qz, {f'question_{q.id}': cs[0].id})
        a = QuizAttempt.objects.get()
        self.assertEqual(a.score, 100)
        v = Quiz.objects.get(pk=self.qz.pk).version
        # mentor rewrites the question so that choice 1 is now the right one, then deletes it
        self.login(self.m1)
        self.client.post(reverse('mentor_question_edit', args=[self.qz.pk, q.pk]),
                         {'qtype': 'MC', 'text': 'New wording', 'points': '9', 'choice_text': ['p', 'q'], 'choice_correct': ['1']})
        self.assertGreater(Quiz.objects.get(pk=self.qz.pk).version, v)
        a.refresh_from_db()
        row = a.answers.get()
        self.assertEqual((a.score, a.quiz_version), (100, v))
        self.assertEqual(row.question_text, 'Original wording')
        self.assertEqual(row.max_points, Decimal('1.00'))
        self.client.post(reverse('mentor_question_delete', args=[self.qz.pk, q.pk]))
        row.refresh_from_db(); a.refresh_from_db()
        self.assertIsNone(row.question_id)
        self.assertEqual((a.score, row.question_text), (100, 'Original wording'))

    def test_cannot_publish_incomplete_quiz(self):
        draft = Quiz.objects.create(title='Draft', created_by=self.m1, classroom=self.room1, is_published=False)
        self.login(self.m1)

        def edit(published):
            data = {'classroom': self.room1.pk, 'title': 'Draft', 'description': '', 'instructions': ''}
            if published:
                data['is_published'] = 'on'
            return self.client.post(reverse('mentor_quiz_edit', args=[draft.pk]), data)

        r = edit(True)
        self.assertEqual(r.status_code, 200)                      # no questions -> refused
        self.assertFalse(Quiz.objects.get(pk=draft.pk).is_published)
        q = Question.objects.create(quiz=draft, text='Q', qtype='MC')
        Choice.objects.create(question=q, text='a', is_correct=False)
        Choice.objects.create(question=q, text='b', is_correct=False)
        self.assertEqual(edit(True).status_code, 200)             # no correct choice -> refused
        Choice.objects.filter(question=q, text='a').update(is_correct=True)
        self.assertEqual(edit(True).status_code, 302)
        self.assertTrue(Quiz.objects.get(pk=draft.pk).is_published)

    def test_editor_page_lists_problems_and_total_points(self):
        Question.objects.create(quiz=self.qz, text='Broken fill', qtype='FILL', points=4)
        essay(self.qz, points=6)
        self.login(self.m1)
        r = self.client.get(self.url())
        self.assertContains(r, 'Question 1 has no [blank] in the text')
        self.assertContains(r, '10 points in total')

    def test_deleting_last_question_returns_quiz_to_draft(self):
        q, _ = mc(self.qz)
        self.login(self.m1)
        self.client.post(reverse('mentor_question_delete', args=[self.qz.pk, q.pk]))
        self.assertFalse(Quiz.objects.get(pk=self.qz.pk).is_published)

    def test_preview_and_print_cover_every_type(self):
        mc(self.qz, 'MCQ'); fill(self.qz, 'Fill [blank]'); essay(self.qz, 'ESSAYQ')
        self.login(self.m1)
        r = self.client.get(reverse('mentor_quiz_preview', args=[self.qz.pk]))
        self.assertContains(r, 'Preview')
        self.assertNotContains(r, 'Submit Answers')
        r = self.client.get(reverse('mentor_quiz_print', args=[self.qz.pk]))
        self.assertContains(r, 'ESSAYQ'); self.assertContains(r, '____')
        self.assertEqual(self.client.get(reverse('mentor_quiz_print', args=[self.qz.pk]) + '?key=1').status_code, 200)


class MigrationCompatTests(P4BBase):
    def test_old_style_questions_and_attempts_still_work(self):
        # a question created the old way (no qtype given) is multiple choice, 1 point
        q = Question.objects.create(quiz=self.qz, text='Old')
        self.assertEqual((q.qtype, q.points, q.allow_multiple), ('MC', 1, False))
        # an attempt created the old way is finalized, and its detail page still renders
        a = QuizAttempt.objects.create(user=self.s1, quiz=self.qz, score=80, correct_count=4, total_questions=5)
        self.assertEqual(a.grading_status, 'finalized')
        self.login(self.m1)
        r = self.client.get(reverse('student_attempt', args=[self.room1.pk, self.s1.pk, a.pk]))
        self.assertContains(r, 'were not recorded')
        self.assertEqual(progress.quiz_type_label(self.qz), 'Multiple Choice')
