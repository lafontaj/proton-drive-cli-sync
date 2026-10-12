"""OnCalendar systemd, sans widget. Jetons de fréquence stables (pas les libellés traduits)."""


def build_on_calendar(freq, hour, dow="Sun"):
    """``freq`` vaut daily, weekly ou hourly. ``hour`` est un entier 0–23."""
    if freq == "hourly":
        return "*-*-* *:00:00"
    hh = f"{int(hour):02d}"
    if freq == "weekly":
        return f"{dow} *-*-* {hh}:00:00"
    return f"*-*-* {hh}:00:00"


def parse_on_calendar(cal):
    """Retourne (freq, hour, dow) ou None si le format n'est pas reconnu."""
    import re
    if not cal:
        return None
    if re.fullmatch(r"\*-\*-\* \*:00:00", cal):
        return ("hourly", 0, "Sun")
    weekly = re.fullmatch(r"([A-Za-z]{3}) \*-\*-\* (\d{2}):00:00", cal)
    if weekly:
        return ("weekly", int(weekly.group(2)), weekly.group(1))
    daily = re.fullmatch(r"\*-\*-\* (\d{2}):00:00", cal)
    if daily:
        return ("daily", int(daily.group(1)), "Sun")
    return None
