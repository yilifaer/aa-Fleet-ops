from django import template

register = template.Library()


@register.filter
def is_bool(value):
    """True only for real booleans; 1, 0 and Decimal("1.00") compare equal to True/False."""
    return isinstance(value, bool)
