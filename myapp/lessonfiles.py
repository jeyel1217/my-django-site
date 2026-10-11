"""Lesson files: checking an upload, storing it safely, making the page-by-page copy.

What is checked on the server for every upload (the browser is never trusted):
  * the extension is .pdf, .docx or .pptx (older .doc / .ppt are refused with a clear message)
  * the size is within the limit (LESSON_FILE_MAX_MB, default 20)
  * the file CONTENT matches its extension (PDF header; a real zip containing the right
    Word / PowerPoint parts; no macros; no zip bombs), not just the name or the MIME type
  * the stored name is random; the uploader's file name is only kept as display text, cleaned
  * the file is saved outside the public folders (see storage.py)

Word and PowerPoint files are turned into a PDF with LibreOffice so they can be shown page by
page. If LibreOffice is not installed on the server this is reported honestly as "Converter not
installed"; nothing pretends it worked.
"""
import logging
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import UploadedFile

from .models import LessonFile, LessonProgress
from .storage import private_root

log = logging.getLogger(__name__)

EXTENSIONS = {'.pdf': LessonFile.KIND_PDF, '.docx': LessonFile.KIND_DOCX, '.pptx': LessonFile.KIND_PPTX}
OLD_FORMATS = {'.doc': 'Word 97-2003 (.doc)', '.ppt': 'PowerPoint 97-2003 (.ppt)'}
MAX_PAGES = 500
MAX_ZIP_ENTRIES = 5000
MAX_ZIP_UNPACKED = 300 * 1024 * 1024
REQUIRED_PARTS = {
    LessonFile.KIND_DOCX: 'word/document.xml',
    LessonFile.KIND_PPTX: 'ppt/presentation.xml',
}


def max_bytes():
    return int(getattr(settings, 'LESSON_FILE_MAX_MB', 20)) * 1024 * 1024


class UploadRejected(ValueError):
    """The message is written for the Mentor and is shown as-is."""


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------
def clean_display_name(name):
    """Only for showing the file's name. Never used as a path."""
    base = os.path.basename((name or '').replace('\\', '/'))
    base = re.sub(r'[\x00-\x1f\x7f<>:"|?*]', '', base).strip(' .')
    stem, ext = os.path.splitext(base)
    return (stem[:90] + ext.lower())[:120] or 'lesson-file'


# ---------------------------------------------------------------------------
# Checking an upload
# ---------------------------------------------------------------------------
def _count_pdf_pages(fileobj):
    try:
        from pypdf import PdfReader
    except ImportError:      # pragma: no cover - requirements.txt installs it
        raise UploadRejected('The server is missing the PDF reader (pypdf). Ask the administrator to run: pip install -r requirements.txt')
    try:
        fileobj.seek(0)
        reader = PdfReader(fileobj)
        if reader.is_encrypted:
            raise UploadRejected('This PDF is password protected. Remove the password and upload it again.')
        pages = len(reader.pages)
    except UploadRejected:
        raise
    except Exception:
        raise UploadRejected('This file is not a readable PDF. Open it on your computer, save it again as PDF and retry.')
    finally:
        try:
            fileobj.seek(0)
        except Exception:
            pass
    if pages < 1:
        raise UploadRejected('This PDF has no pages.')
    if pages > MAX_PAGES:
        raise UploadRejected(f'This PDF has {pages} pages. The limit is {MAX_PAGES}. Split it into smaller lessons.')
    return pages


def _check_zip(fileobj, kind):
    fileobj.seek(0)
    try:
        zf = zipfile.ZipFile(fileobj)
    except zipfile.BadZipFile:
        raise UploadRejected('This file is not a valid Word / PowerPoint document (its content does not match its name).')
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_ZIP_ENTRIES or sum(i.file_size for i in infos) > MAX_ZIP_UNPACKED:
            raise UploadRejected('This document is unusually large inside and was refused for safety.')
        names = [i.filename for i in infos]
        if any(n.startswith('/') or '..' in n.split('/') for n in names):
            raise UploadRejected('This document contains unsafe file paths and was refused.')
        if '[Content_Types].xml' not in names or REQUIRED_PARTS[kind] not in names:
            raise UploadRejected('The content of this file does not match a ' + ('Word (.docx)' if kind == LessonFile.KIND_DOCX else 'PowerPoint (.pptx)') + ' document.')
        if any(n.lower().endswith('vbaproject.bin') for n in names):
            raise UploadRejected('Documents with macros are not allowed.')
    fileobj.seek(0)


def inspect_upload(upload: UploadedFile):
    """Returns (kind, page_count_or_0). Raises UploadRejected with a readable reason."""
    name = (upload.name or '')
    ext = os.path.splitext(name)[1].lower()
    if ext in OLD_FORMATS:
        raise UploadRejected(f'{OLD_FORMATS[ext]} files are not supported. Save it as .docx / .pptx or export a PDF.')
    if ext not in EXTENSIONS:
        raise UploadRejected('Only PDF, Word (.docx) and PowerPoint (.pptx) files can be uploaded.')
    if upload.size is None or upload.size <= 0:
        raise UploadRejected('This file is empty.')
    if upload.size > max_bytes():
        raise UploadRejected(f'This file is {upload.size / 1048576:.1f} MB. The limit is {max_bytes() // 1048576} MB.')
    kind = EXTENSIONS[ext]

    upload.seek(0)
    head = upload.read(1024)
    upload.seek(0)
    if kind == LessonFile.KIND_PDF:
        if b'%PDF-' not in head:
            raise UploadRejected('This file is not a real PDF (its content does not match its name).')
        return kind, _count_pdf_pages(upload)
    if not head.startswith(b'PK\x03\x04'):
        raise UploadRejected('This file is not a real Word / PowerPoint document (its content does not match its name).')
    _check_zip(upload, kind)
    return kind, 0


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------
def _delete_stored(field):
    try:
        if field and field.name:
            field.storage.delete(field.name)
    except Exception:       # a missing file must never break a request
        log.warning('could not delete %s', getattr(field, 'name', ''))


def remove_files(lesson_file):
    _delete_stored(lesson_file.original)
    _delete_stored(lesson_file.reading_pdf)


def attach(lesson, upload, user):
    """Validate and store `upload` as the lesson's file (replacing any earlier one). Returns the LessonFile."""
    kind, pages = inspect_upload(upload)

    old = LessonFile.objects.filter(lesson=lesson).first()
    if old is not None:
        remove_files(old)
    lf = old or LessonFile(lesson=lesson)
    lf.original_name = clean_display_name(upload.name)
    lf.kind = kind
    lf.size = upload.size
    lf.uploaded_by = user
    lf.reading_pdf = ''
    lf.page_count = pages
    lf.conversion_status = LessonFile.CONV_READY
    lf.conversion_message = ''
    upload.seek(0)
    lf.original.save(upload.name, upload, save=False)       # name is replaced by lesson_file_name()
    lf.save()

    if kind != LessonFile.KIND_PDF:
        convert(lf)
    # Students who were mid-way through the OLD file start the new one from page 1; completed lessons stay completed.
    LessonProgress.objects.filter(lesson=lesson, completed=False).update(last_page=1, max_page=0)
    return lf


# ---------------------------------------------------------------------------
# Word / PowerPoint -> PDF
# ---------------------------------------------------------------------------
def find_converter():
    configured = getattr(settings, 'LIBREOFFICE_PATH', '')
    candidates = [configured] if configured else []
    candidates += [shutil.which('soffice'), shutil.which('libreoffice'),
                   r'C:\Program Files\LibreOffice\program\soffice.exe',
                   r'C:\Program Files (x86)\LibreOffice\program\soffice.exe']
    for c in candidates:
        if c and Path(c).exists():
            return c
    return None


def convert(lf):
    """Make (or retry) the PDF reading copy of a Word / PowerPoint file. Never raises."""
    if lf.kind == LessonFile.KIND_PDF:
        return lf
    exe = find_converter()
    if exe is None:
        return _mark(lf, LessonFile.CONV_UNAVAILABLE,
                     'This server has no document converter (LibreOffice), so Word and PowerPoint files cannot be shown page by page. Upload a PDF instead.')
    timeout = int(getattr(settings, 'LESSON_CONVERT_TIMEOUT', 120))
    try:
        with tempfile.TemporaryDirectory(prefix='lesson-conv-') as tmp:
            tmp = Path(tmp)
            src = tmp / f'input{os.path.splitext(lf.original.name)[1].lower()}'
            with lf.original.open('rb') as fin, open(src, 'wb') as fout:
                shutil.copyfileobj(fin, fout)
            out = tmp / 'out'
            out.mkdir()
            profile = (tmp / 'profile').as_uri()
            proc = subprocess.run(
                [exe, '--headless', '--norestore', '--nolockcheck', '--nodefault', f'-env:UserInstallation={profile}',
                 '--convert-to', 'pdf', '--outdir', str(out), str(src)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, cwd=str(tmp),
            )
            pdf = out / 'input.pdf'
            if proc.returncode != 0 or not pdf.exists() or pdf.stat().st_size == 0:
                raise RuntimeError('LibreOffice did not produce a PDF')
            with open(pdf, 'rb') as fh:
                pages = _count_pdf_pages(fh)
            _delete_stored(lf.reading_pdf)
            with open(pdf, 'rb') as fh:
                lf.reading_pdf.save('reading.pdf', ContentFile(fh.read()), save=False)
        lf.page_count = pages
        return _mark(lf, LessonFile.CONV_READY, '')
    except subprocess.TimeoutExpired:
        return _mark(lf, LessonFile.CONV_FAILED, f'Converting took longer than {timeout} seconds. Try a smaller file, or upload a PDF.')
    except UploadRejected as exc:
        return _mark(lf, LessonFile.CONV_FAILED, str(exc))
    except Exception as exc:
        log.exception('conversion failed for lesson file %s', lf.pk)
        return _mark(lf, LessonFile.CONV_FAILED, 'The document could not be converted. Try saving it again, or upload a PDF.')


def _mark(lf, status, message):
    lf.conversion_status = status
    lf.conversion_message = message[:300]
    if status != LessonFile.CONV_READY:
        lf.page_count = 0
    lf.save()
    return lf
