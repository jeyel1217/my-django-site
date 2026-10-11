"""Where lesson files live: a folder that is NOT served by the web server.

Nothing in here has a public URL. A file can only be read through the views in
lesson_views.py, which check who is asking first."""
import os
from pathlib import Path

from django.conf import settings
from django.core.files.storage import FileSystemStorage


def private_root():
    return Path(getattr(settings, 'PRIVATE_MEDIA_ROOT', Path(settings.BASE_DIR) / 'private_media'))


class PrivateStorage(FileSystemStorage):
    """Reads the folder from settings every time, so changing PRIVATE_MEDIA_ROOT takes effect at once.
    base_url=None: asking for a URL raises an error, so a template can never link to a file by accident."""

    def __init__(self):
        super().__init__(base_url=None)

    @property
    def base_location(self):
        return str(private_root())

    @property
    def location(self):
        return os.path.abspath(self.base_location)

    @property
    def base_url(self):
        return None

    def url(self, name):
        raise ValueError('Lesson files have no public URL. They are only served by the lesson views after an access check.')


def get_private_storage():
    return PrivateStorage()
