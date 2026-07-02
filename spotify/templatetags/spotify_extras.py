from django import template

register = template.Library()

_KEY_NAMES = ['C', 'C♯/D♭', 'D', 'D♯/E♭', 'E', 'F', 'F♯/G♭', 'G', 'G♯/A♭', 'A', 'A♯/B♭', 'B']


@register.filter
def duration_ms(ms):
    """Convert milliseconds to m:ss format."""
    if ms is None:
        return "—"
    total_seconds = ms // 1000
    minutes = total_seconds // 60
    seconds = total_seconds % 60
    return f"{minutes}:{seconds:02d}"


@register.filter
def key_name(key_int):
    """Convert Pitch Class integer (0-11) to note name."""
    try:
        return _KEY_NAMES[int(key_int)]
    except (IndexError, ValueError, TypeError):
        return str(key_int)


@register.filter
def in_set(value, container):
    """Return True if value is in container (works with sets and any iterable)."""
    return value in container


@register.filter
def to_pct(value):
    """Convert 0-1 float to integer percentage string for use in inline styles."""
    try:
        return f"{float(value) * 100:.0f}"
    except (ValueError, TypeError):
        return "0"
