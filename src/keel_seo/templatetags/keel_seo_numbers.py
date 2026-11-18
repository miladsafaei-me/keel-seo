"""``{{ value|thousands }}``: 1234567 -> 1,234,567, with no dependency on django.contrib.humanize."""
from django import template

register = template.Library()


@register.filter
def thousands(value):
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return value
