"""Telegram message templates for the VFS slot checker.

Edit the formatting here — this is the single place that decides what the slot
report and failure-alert messages look like. It is route-aware: every message
shows the destination's flag and a link to that portal's visa-center site, so
adding new routes does not silently mislabel messages.

The functions return a plain string; sending is done by `src.utils.telegram`.
Plain URLs render as clickable links in Telegram, so no HTML/Markdown is needed.
"""

# Destination code -> flag emoji shown before each slot line.
# Add an entry when you add a new route (falls back to no flag if missing).
DESTINATION_FLAGS = {
    "MT": "🇲🇹", "MLT": "🇲🇹",       # Malta
    "LUX": "🇱🇺", "LU": "🇱🇺",       # Luxembourg
    "CHE": "🇨🇭", "CH": "🇨🇭",       # Switzerland
    "DNK": "🇩🇰", "DK": "🇩🇰",       # Denmark
    "HUN": "🇭🇺", "HU": "🇭🇺",       # Hungary
}

# Destination code -> friendly country name, inserted into each label so the
# message reads "Abu Dhabi - Hungary - Short Stay - Business". Add an entry when
# you add a new route (falls back to the raw code if missing).
DESTINATION_NAMES = {
    "MT": "Malta", "MLT": "Malta",
    "LUX": "Luxembourg", "LU": "Luxembourg",
    "CHE": "Switzerland", "CH": "Switzerland",
    "DNK": "Denmark", "DK": "Denmark",
    "HUN": "Hungary", "HU": "Hungary",
}


def _flag(dest_code: str) -> str:
    """Flag emoji for a destination code, or '' if unknown (with no trailing space)."""
    return DESTINATION_FLAGS.get((dest_code or "").upper(), "")


def _country(dest_code: str) -> str:
    """Friendly country name for a destination code, or the raw code if unknown."""
    return DESTINATION_NAMES.get((dest_code or "").upper(), (dest_code or "").upper())


def _label_with_country(label: str, dest_code: str) -> str:
    """
    Rewrites a combo label as 'Centre - Country - SubCategory', dropping the
    middle 'category' segment (e.g. 'Short Stay').

    'Abu Dhabi - Short Stay - Business'  -> 'Abu Dhabi - Hungary - Business'
    'Dubai - SCHENGEN'                   -> 'Dubai - Hungary - SCHENGEN'
    'Abu Dhabi'                          -> 'Abu Dhabi - Hungary'
    """
    country = _country(dest_code)
    parts = [p.strip() for p in label.split(" - ") if p.strip()]
    if len(parts) >= 3:
        # centre - <category dropped> - sub  ->  centre - country - sub
        return f"{parts[0]} - {country} - {parts[-1]}"
    if len(parts) == 2:
        # centre - sub (no category)  ->  centre - country - sub
        return f"{parts[0]} - {country} - {parts[1]}"
    # single part (just a centre)
    return f"{parts[0]} - {country}" if parts else country


def slot_report(source_code: str, dest_code: str, results: list, login_url: str = "") -> str:
    """
    Builds the slot-report message for ONE route.

    Args:
        source_code / dest_code: e.g. 'AE' / 'DNK'.
        results: list of (label, message) tuples — one per combination checked.
        login_url: the portal's login URL, shown as a clickable link.

    Returns:
        The formatted message string. Edit the layout below to taste.
    """
    flag = _flag(dest_code)
    prefix = f"{flag} " if flag else ""

    lines = []
    for label, message in results:
        full_label = _label_with_country(label, dest_code)
        lines.append(f"{prefix}{full_label}:")
        lines.append(f"  {message}")
        lines.append("")
    body = "\n".join(lines).strip()

    if login_url:
        body += f"\n\nLink to visa center site ({login_url})"
    return body


def failure_alert(source_code: str, dest_code: str, error: str, attempts: int,
                  login_url: str = "") -> str:
    """Builds the 'run failed' alert message for ONE route."""
    flag = _flag(dest_code)
    prefix = f"{flag} " if flag else ""
    msg = (
        f"⚠️ {prefix}{source_code.upper()}-{dest_code.upper()} slot check FAILED "
        f"after {attempts} attempt(s).\n\nLast error:\n{error}"
    )
    if login_url:
        msg += f"\n\nLink to visa center site ({login_url})"
    return msg
