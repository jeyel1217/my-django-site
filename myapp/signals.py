from django.db.models.signals import post_save, post_delete
from django.contrib.auth.models import User
from django.dispatch import receiver
from .models import Profile, LessonFile


@receiver(post_save, sender=User)
def create_profile(sender, instance, created, **kwargs):
    if created:
        Profile.objects.create(user=instance)


@receiver(post_delete, sender=LessonFile)
def remove_lesson_files(sender, instance, **kwargs):
    """Deleting a lesson (or its file) also deletes the stored PDF / Word / PowerPoint files."""
    from . import lessonfiles
    lessonfiles.remove_files(instance)
