"""Lesson files: viewing, downloading, saving reading progress.

Every view here decides WHO may touch the file on the server. Hiding a button in the page is
never the rule. A person who is not allowed gets a 404, so the existence of the lesson is not revealed."""
import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import classrooms as cls
from . import lessonfiles
from .decorators import role_required
from .models import Lesson, LessonFile, LessonProgress, Profile


def _role(user):
    return Profile.objects.filter(user=user).values_list('role', flat=True).first()


def readable_lesson(user, slug):
    """The lesson, only if this person may read it: a student of its classroom (published
    lessons only), its own Mentor (drafts too), or an Admin."""
    role = _role(user)
    if role == Profile.ROLE_ADMIN:
        qs = Lesson.objects.all()
    elif role == Profile.ROLE_MENTOR:
        qs = Lesson.objects.filter(Q(created_by=user) | Q(classroom__mentor=user))
    else:
        qs = cls.visible_lessons(user)
    return get_object_or_404(qs, slug=slug)


def _file_or_404(lesson):
    lf = LessonFile.objects.filter(lesson=lesson).first()
    if lf is None:
        raise Http404
    return lf


def _no_cache(resp):
    resp['Cache-Control'] = 'private, no-store'
    resp['X-Content-Type-Options'] = 'nosniff'
    return resp


@login_required
def lesson_file_stream(request, slug):
    """The page-by-page copy (always a PDF), for the viewer."""
    lesson = readable_lesson(request.user, slug)
    lf = _file_or_404(lesson)
    if not lf.is_viewable:
        raise Http404
    resp = FileResponse(lf.reading_file.open('rb'), content_type='application/pdf')
    resp['Content-Disposition'] = 'inline; filename="lesson.pdf"'
    resp['Content-Security-Policy'] = "default-src 'none'; sandbox"
    return _no_cache(resp)


@login_required
@role_required(Profile.ROLE_MENTOR, Profile.ROLE_ADMIN)
def mentor_lesson_file_download(request, pk):
    """The original upload. Only the lesson's own Mentor (or an Admin)."""
    lesson = _owned_lesson(request, pk)
    lf = _file_or_404(lesson)
    resp = FileResponse(lf.original.open('rb'), as_attachment=True, filename=lf.original_name)
    return _no_cache(resp)


def _owned_lesson(request, pk):
    if _role(request.user) == Profile.ROLE_ADMIN:
        return get_object_or_404(Lesson, pk=pk)
    return get_object_or_404(Lesson, pk=pk, created_by=request.user)


@login_required
@role_required(Profile.ROLE_MENTOR)
@require_POST
def mentor_lesson_file_retry(request, pk):
    lesson = get_object_or_404(Lesson, pk=pk, created_by=request.user)
    lf = _file_or_404(lesson)
    lessonfiles.convert(lf)
    lf.refresh_from_db()
    if lf.conversion_status == LessonFile.CONV_READY:
        messages.success(request, f'The reading copy is ready ({lf.page_count} pages).')
    else:
        messages.error(request, lf.conversion_message or 'The conversion did not work.')
    return redirect('mentor_lesson_edit', pk=lesson.pk)


@login_required
@role_required(Profile.ROLE_MENTOR)
@require_POST
def mentor_lesson_file_remove(request, pk):
    lesson = get_object_or_404(Lesson, pk=pk, created_by=request.user)
    lf = LessonFile.objects.filter(lesson=lesson).first()
    if lf:
        lf.delete()
        messages.success(request, 'The file was removed from this lesson.')
    return redirect('mentor_lesson_edit', pk=lesson.pk)


@login_required
@require_POST
def lesson_progress_save(request, slug):
    """Called by the viewer each time a student turns to a page.

    Rules, all checked here:
      * students only (a Mentor's preview never writes progress)
      * the page must exist in the file (the page count comes from the server, not the browser)
      * a student can move at most ONE page beyond the furthest page already reached,
        so the last page can only be reached by passing through the pages in order"""
    lesson = readable_lesson(request.user, slug)
    if _role(request.user) != Profile.ROLE_STUDENT:
        return JsonResponse({'ok': False, 'error': 'Only students have reading progress.'}, status=403)
    lf = LessonFile.objects.filter(lesson=lesson).first()
    if lf is None or not lf.is_viewable:
        return JsonResponse({'ok': False, 'error': 'This lesson has no readable file.'}, status=400)
    try:
        page = int(json.loads(request.body[:500] or b'{}')['page'])
    except (ValueError, KeyError, TypeError):
        return JsonResponse({'ok': False, 'error': 'Bad request.'}, status=400)
    if not 1 <= page <= lf.page_count:
        return JsonResponse({'ok': False, 'error': 'That page does not exist.'}, status=400)

    prog, _ = LessonProgress.objects.get_or_create(user=request.user, lesson=lesson)
    if page > prog.max_page + 1:
        return JsonResponse({'ok': False, 'error': 'Read the pages in order.', 'max_page': prog.max_page}, status=409)
    now = timezone.now()
    prog.started_at = prog.started_at or now
    prog.last_opened_at = now
    prog.last_page = page
    prog.max_page = max(prog.max_page, page)
    prog.save(update_fields=['started_at', 'last_opened_at', 'last_page', 'max_page'])
    return JsonResponse({
        'ok': True, 'last_page': prog.last_page, 'max_page': prog.max_page, 'total': lf.page_count,
        'can_complete': prog.max_page >= lf.page_count, 'completed': prog.completed,
    })
