"""Daily job: reminders, overdue notices, final warnings, approved deactivations, e-mail retries.

Run it once a day from PythonAnywhere's Tasks tab:
    /home/JCAD/.virtualenvs/<env>/bin/python /home/JCAD/<project>/manage.py process_deadlines
It is safe to run more than once: duplicates are prevented on the server.
Use --dry-run to see what it would do without sending or changing anything.
"""
from django.core.management.base import BaseCommand
from myapp import deadlines


class Command(BaseCommand):
    help = 'Process quiz deadlines, notifications, warnings and approved deactivations.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Show what would happen; change nothing.')

    def handle(self, *args, **opts):
        rep = deadlines.run_policy(dry_run=opts['dry_run'])
        prefix = '[DRY RUN] ' if opts['dry_run'] else ''
        self.stdout.write(prefix + ', '.join(f'{k}={v}' for k, v in rep.items()))
