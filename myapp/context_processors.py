from .models import Profile


def student_notices(request):
    """Gives base.html what it needs for the student profile reminder."""
    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated:
        return {}
    profile = Profile.objects.filter(user=user).first()
    if profile is None or profile.role != Profile.ROLE_STUDENT:
        return {}
    from .models import Notification
    return {
        'unread_notifications': Notification.objects.filter(user=user, hidden=False, read_at__isnull=True).count(),
        'notice_missing_fields': profile.missing_profile_fields,
        'notice_photo_flagged': profile.avatar_flagged,
        'notice_photo_reason': profile.avatar_flag_reason,
    }
