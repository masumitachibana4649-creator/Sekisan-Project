import re

from django import template
from django.utils.html import conditional_escape
from django.utils.safestring import mark_safe


register = template.Library()


@register.filter
def sentence_breaks(value):
    text = conditional_escape(value or "")
    return mark_safe(text.replace("。", "。<br>"))


@register.filter
def room_note(value):
    text = conditional_escape(value or "-")
    text = _format_ai_confidence(text)
    return mark_safe(text.replace("。", "。<br>"))


def _format_ai_confidence(text):
    def replace(match):
        confidence = float(match.group(1))
        return f"AI信頼度: {confidence:.0%}"

    return re.sub(r"AI信頼度:\s*([01](?:\.\d+)?)", replace, text)
