from django import template
from django.utils.html import escape
from django.utils.safestring import mark_safe

register = template.Library()


@register.filter
def terms_format(text):
    """Turn the simple Terms format into safe HTML ('## ' heading, '- ' bullet).
    Everything is escaped first, so an Admin cannot inject HTML or scripts."""
    out, in_list = [], False

    def close_list():
        nonlocal in_list
        if in_list:
            out.append('</ul>')
            in_list = False

    for raw in (text or '').replace('\r', '').split('\n'):
        line = raw.strip()
        if not line:
            close_list()
            continue
        if line.startswith('## '):
            close_list()
            out.append('<h3 class="text-base font-extrabold text-slate-800 mt-6 mb-2">%s</h3>' % escape(line[3:]))
        elif line.startswith('- '):
            if not in_list:
                out.append('<ul class="list-disc pl-6 space-y-1 text-slate-600">')
                in_list = True
            out.append('<li>%s</li>' % escape(line[2:]))
        else:
            close_list()
            out.append('<p class="text-slate-600 mb-2">%s</p>' % escape(line))
    close_list()
    return mark_safe('\n'.join(out))
