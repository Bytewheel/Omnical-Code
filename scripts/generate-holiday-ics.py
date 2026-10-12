#!/usr/bin/env python3
"""generate-holiday-ics.py — the holiday sets the router serves.

The create-calendar modal's "add holidays" dropdown offers the sets this
script emits; selecting some merges them into the new calendar as a copy
(refreshable by UID via the recorded holidays://{set} sources — see the
rustical linked-platforms engine).

Design constraints that shaped this generator:

  - Pure stdlib Python, deterministic output (fixed DTSTAMP, sorted events,
    no clock reads): the same tree must regenerate byte-identically, so a
    re-run can never produce a surprise diff next to a review.
  - No network. Everything is computed (solar/lunar arithmetic) or carried
    as a data table (sanctorale, civil-holiday rules), so the router keeps
    serving from its own flash with the build host down.
  - Lunar calendars are the honest weak spot, stated as such per event:
    the Hebrew calendar is exactly computable (an arithmetic calendar, not
    observational); Islamic dates are TABULAR (±1 day vs local moon
    sightings — that calendar is observational by definition); Chinese and
    Hindu/Buddhist/Sikh lunar dates are computed from approximate
    new moons/tithis (±1 day; official PR-C holiday arrangements are
    announced years in advance anyway); Orthodox fixed feasts are given on the Old (Julian) reckoning,
    converted, with the New-Calendar alternative named in the description.
  - Every event is an all-day VALUE=DATE on the Gregorian civil calendar;
    where an observance begins at sundown the description says so — the
    calendar grid shows the civil day, which is how these holidays are read
    in practice.
  - Self-test: the generator refuses to emit if any anchor date it knows
    from published calendars fails to reproduce — a silently-wrong lunar
    calendar is worse than no calendar.

Output: holidays/ (holidays.toml manifest + one .ics per set), 2026-2030.

Run:  python3 scripts/generate-holiday-ics.py
"""

import datetime
import math
import os

YEARS = list(range(2026, 2031))  # 2026-2030 inclusive
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "holidays")

# ==========================================================================
# ICS engine
# ==========================================================================

FIXED_DTSTAMP = "20260101T000000Z"  # determinism: the stamp is not event data


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> list:
    """RFC 5545 §3.1: content lines fold at 75 octets."""
    out = []
    octets = 0
    chunk = []
    for ch in line:
        width = 2 if ord(ch) > 0x7F else 1
        if octets + width > 75:
            out.append("".join(chunk))
            chunk = [" "]
            octets = 1
        chunk.append(ch)
        octets += width
    out.append("".join(chunk))
    return out


class Event:
    def __init__(self, dtstart, summary, uid_slug, description="", days=1):
        self.dtstart = dtstart
        self.summary = summary
        self.uid_slug = uid_slug
        self.description = description
        self.days = days

    def uid(self, set_id):
        return f"hol-{set_id}-{self.uid_slug}-{self.dtstart:%Y%m%d}@omnical"

    def lines(self, set_id):
        dtend = self.dtstart + datetime.timedelta(days=self.days)
        out = [
            "BEGIN:VEVENT",
            f"UID:{self.uid(set_id)}",
            f"DTSTAMP:{FIXED_DTSTAMP}",
            f"DTSTART;VALUE=DATE:{self.dtstart:%Y%m%d}",
            f"DTEND;VALUE=DATE:{dtend:%Y%m%d}",
            f"SUMMARY:{_escape(self.summary)}",
        ]
        if self.description:
            out.append(f"DESCRIPTION:{_escape(self.description)}")
        out.append("END:VEVENT")
        return out


def write_set(set_id, events):
    """Emit one .ics (CRLF, folded, UID-deduped, date-sorted)."""
    events = [e for e in events if e is not None]
    events.sort(key=lambda e: (e.dtstart, e.summary))
    seen = set()
    lines = [
        "BEGIN:VCALENDAR",
        "PRODID:-//omnical//holiday-sets//EN",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
    ]
    for e in events:
        uid = e.uid(set_id)
        if uid in seen:
            continue
        seen.add(uid)
        for line in e.lines(set_id):
            lines.extend(_fold(line))
    lines.append("END:VCALENDAR")
    path = os.path.join(OUT_DIR, f"{set_id}.ics")
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write("\r\n".join(lines) + "\r\n")
    return len(seen)


# ==========================================================================
# Date helpers
# ==========================================================================

def easter_gregorian(year):
    """Easter Sunday, western reckoning (Meeus/Butcher, 1583+)."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return datetime.date(year, month, day)


def easter_julian(year):
    """Orthodox Easter (Julian Paschalion) as a Gregorian civil date
    (Julian +13 days, valid 1900-2099)."""
    a = year % 4
    b = year % 7
    c = year % 19
    d = (19 * c + 15) % 30
    e = (2 * a + 4 * b - d + 34) % 7
    dd = d + e
    if dd <= 9:
        month, day = 3, 22 + dd
    else:
        month, day = 4, dd - 9
    return datetime.date(year, month, day) + datetime.timedelta(days=13)


def nth_weekday(year, month, weekday, n):
    """n-th <weekday> (Mon=0) of the month; n=-1 → last."""
    if n == -1:
        nxt = datetime.date(year + (month == 12), (month % 12) + 1, 1)
        d = nxt - datetime.timedelta(days=1)
        while d.weekday() != weekday:
            d -= datetime.timedelta(days=1)
        return d
    d = datetime.date(year, month, 1)
    while d.weekday() != weekday:
        d += datetime.timedelta(days=1)
    return d + datetime.timedelta(days=7 * (n - 1))


def observed(date, sunday_to_monday=True):
    """A fixed-date holiday falling on Saturday (and optionally Sunday) is
    observed the following Monday."""
    if date.weekday() == 5:
        return date + datetime.timedelta(days=2)
    if sunday_to_monday and date.weekday() == 6:
        return date + datetime.timedelta(days=1)
    return date


# ==========================================================================
# Hebrew calendar — arithmetic, exact.  Months and the molad are counted in
# ḥalakim (parts, 1/1080 h).  The four deḥiyyot (postponements) of Rosh
# ha-Shanah are applied per the classical rules; the civil epoch is pinned
# to the known Rosh ha-Shanah 5787 = 2026-09-11 and verified against a bank
# of published anchor dates (see SELF-TEST at the bottom).
# ==========================================================================

HALAKIM_DAY = 25920  # 24 h × 1080
HALAKIM_MONTH = 765433  # mean synodic month: 29 d 12 h 793 p
BAHARAD = 2 * HALAKIM_DAY + 5 * 1080 + 204  # the first Tishri's molad: day 2 (Mon), 5 h 204 p


def hebrew_leap(year):
    return (7 * year + 1) % 19 < 7


def _molad_tishri_parts(year):
    """Total ḥalakim from the epoch-molad origin to the molad of Tishri."""
    months = (235 * year - 234) // 19  # lunations since the first Tishri
    return BAHARAD + months * HALAKIM_MONTH


def _tishri1_day_number(year):
    """Tishri 1 of `year`, in whole days from the epoch-molad origin, after
    the four deḥiyyot (postponement rules), applied in the classical order:

      1. molad zaqen — a molad at/after the 18th hour (noon) pushes the
         candidate day to the next day;
      2. GaTaRaD — a COMMON year whose candidate day is Tuesday with the
         molad at/after 9 h 204 p would otherwise be 356 days long: push;
      3. BaTuTaKPaT — the year FOLLOWING a leap year whose candidate day is
         Monday with the molad at/after 15 h 589 p would otherwise leave the
         preceding leap year 382 days: push.  (Its zone is 15 h 589 p to
         17 h 1079 p — a Monday molad at/after the 18th hour is already
         pushed by rule 1, and the check below sees a Tuesday candidate, so
         the two rules cannot double-fire);
      4. lo ADU rosh — Tishri 1 may not fall on Sunday, Wednesday or Friday.
    """
    parts = _molad_tishri_parts(year)
    day, rem = divmod(parts, HALAKIM_DAY)
    hour, halakim = divmod(rem, 1080)
    molad_weekday = (day + 6) % 7  # 0=Sunday … 6=Saturday
    postpone = 0
    if hour >= 18:
        postpone += 1  # molad zaqen
    # GaTaRaD reads the molad's own weekday.  In the 18h-and-later zone it
    # overlaps rule 1 harmlessly: both paths land on Thursday.
    if (molad_weekday == 2 and (hour, halakim) >= (9, 204)
            and not hebrew_leap(year)):
        postpone += 1  # GaTaRaD
    # BaTuTaKPaT lives strictly below the 18th hour: a Monday molad at or
    # after noon is already pushed by rule 1, and firing twice overshoots.
    if (molad_weekday == 1 and hour < 18
            and (hour, halakim) >= (15, 589) and hebrew_leap(year - 1)):
        postpone += 1  # BaTuTaKPaT
    weekday = (day + postpone + 6) % 7
    while weekday in (0, 3, 5):  # Sunday, Wednesday, Friday
        postpone += 1
        weekday = (weekday + 1) % 7
    return day + postpone


# Civil anchor: Rosh ha-Shanah 5734 = 1973-09-27 — the Thursday before Yom
# Kippur 5734, which fell on 1973-10-06 (the date the Yom Kippur War began,
# the most cross-checked date in modern Hebrew calendrics).  The whole
# calendar is determined by the rules plus this one anchor; the self-test
# at the bottom additionally verifies 5 Iyar 5708 = 1948-05-14 (Israel's
# declaration of independence, 5 Iyar 5708 by proclamation).
_TISHRI1_ANCHOR = datetime.date(1973, 9, 27)
_TISHRI1_ANCHOR_DN = _tishri1_day_number(5734)


def rosh_hashanah(year):
    return _TISHRI1_ANCHOR + datetime.timedelta(
        days=_tishri1_day_number(year) - _TISHRI1_ANCHOR_DN)


def hebrew_year_length(year):
    return (rosh_hashanah(year + 1) - rosh_hashanah(year)).days


# Month names in calendar order; Adar I is the leap month (30 d), and in a
# leap year Adar II replaces plain Adar.
HEBREW_MONTHS = [
    ("Tishri", 30), ("Cheshvan", 0), ("Kislev", 0), ("Tevet", 29), ("Shvat", 30),
    ("Nisan", 30), ("Iyar", 29), ("Sivan", 30), ("Tammuz", 29), ("Av", 30), ("Elul", 29),
]


def hebrew_month_starts(year):
    """{month_name: civil date of its 1st} for the Hebrew year starting in
    the previous fall (i.e. the year whose Tishri 1 is `rosh_hashanah`)."""
    length = hebrew_year_length(year)
    cheshvan, kislev = {
        353: (29, 29), 354: (29, 30), 355: (30, 30),
        383: (29, 29), 384: (29, 30), 385: (30, 30),
    }[length]
    pairs = [
        ("Tishri", 30), ("Cheshvan", cheshvan), ("Kislev", kislev),
        ("Tevet", 29), ("Shvat", 30),
    ]
    if hebrew_leap(year):
        pairs += [("Adar I", 30), ("Adar II", 29)]
    else:
        pairs += [("Adar", 29)]
    pairs += [
        ("Nisan", 30), ("Iyar", 29), ("Sivan", 30),
        ("Tammuz", 29), ("Av", 30), ("Elul", 29),
    ]
    starts = {}
    d = rosh_hashanah(year)
    for name, mlen in pairs:
        starts[name] = d
        d += datetime.timedelta(days=mlen)
    # Structural check: the months must close exactly at the next Tishri.
    assert d == rosh_hashanah(year + 1), f"month lengths do not close Hebrew year {year}"
    return starts


def hebrew_date(year, month, day):
    """Civil date of `day` of `month` in Hebrew `year` (month by name; in a
    leap year use 'Adar II' — Purim's month)."""
    return hebrew_month_starts(year)[month] + datetime.timedelta(days=day - 1)


def hebrew_year_for_gregorian(gdate):
    """The Hebrew year that `gdate` (as a civil day of a festival) falls in:
    Tishri-based years start in the fall, so a January-August date belongs
    to the Hebrew year that began the previous fall."""
    y = gdate.year + 3761
    while rosh_hashanah(y) > gdate:
        y -= 1
    return y


# ==========================================================================
# Islamic calendar — TABULAR (civil) Hijri.  By construction ±1 day (often
# more, at the era's edges) against Saudi/Umm-al-Qura announcements and
# local moon sightings; every event's description says so.  The epoch is
# calibrated to the announced 1 Muharram 1447 (2026-06-16) so the
# 2026-2030 window tracks the announcements as closely as a tabular
# calendar can.
# ==========================================================================

ISLAMIC_MONTHS = ["Muharram", "Safar", "Rabi al-Awwal", "Rabi al-Thani",
                  "Jumada al-Ula", "Jumada al-Akhirah", "Rajab", "Shaban",
                  "Ramadan", "Shawwal", "Dhul-Qadah", "Dhul-Hijjah"]

# 30-year tabular cycle: 19 common (354 d) + 11 leap (355 d).  Standard
# intercalation (Kuwaiti algorithm): year y is leap when (11 y + 3) % 30 >= 19?  —
# the two classic tabular rules differ by a phase; this one is chosen so
# that Dhul-Hijjah (the Haj month) lands long in the same years as the
# announcements of the era.


def islamic_year_length(y):
    return 354 + (1 if (11 * y + 14) % 30 < 11 else 0)


def _islamic_days_before_year(y):
    """Days from the epoch (1 Muharram 1) to 1 Muharram of year y: the
    tabular leap years are 2, 5, 7, 10, 13, 16, 18, 21, 24, 26, 29 of the
    30-year cycle — count = floor((11·(y−1) + 14)/30), verified against that
    set in the self-test."""
    n = y - 1
    return n * 354 + (11 * n + 14) // 30


def _islamic_days_before_month(y, m):
    # month m (1-12): odd months 30 d, even 29 d; the leap day is in the
    # last month (Dhul-Hijjah 30).
    before = sum(30 if i % 2 else 29 for i in range(1, m))
    return before


# Civil anchor: 1 Muharram 1447 = 2026-06-16 (announced).
_ISLAMIC_ANCHOR = datetime.date(2026, 6, 16)


def islamic_to_civil(y, m, d):
    """Civil date of Hijri (y, m, d) on the calibrated tabular calendar."""
    days = _islamic_days_before_year(y) + _islamic_days_before_month(y, m) + (d - 1)
    base = _islamic_days_before_year(1447)
    return _ISLAMIC_ANCHOR + datetime.timedelta(days=days - base)


def islamic_events_in_window(hy_start, hy_end, month, day, lo, hi):
    """All civil dates of Hijri (m, d) for Hijri years hy_start..hy_end that
    land between lo and hi (inclusive)."""
    out = []
    for hy in range(hy_start, hy_end + 1):
        d = islamic_to_civil(hy, month, day)
        if lo <= d <= hi:
            out.append((hy, d))
    return out


# ==========================================================================
# Lunar arithmetic (Meeus, "Astronomical Algorithms") — new moons and solar
# longitudes, accurate to minutes/0.01°, plenty for day-level lunar dates.
# ==========================================================================

J2000 = 2451545.0


def _jd_to_date(jd):
    """JD (UT) → the Gregorian civil date containing that moment.  By
    definition the civil day of R.D. n starts at JD n + 1721424.5, so the
    date is `date.fromordinal(floor(JD − 1721424.5))` — J2000.0
    (JD 2451545.0, noon) is 2000-01-01, and the self-test asserts it."""
    return datetime.date.fromordinal(math.floor(jd - 1721424.5))


def new_moons(jd_lo, jd_hi):
    """Astronomical new moons (conjunction, UT) between two JDs."""
    # Meeus ch.49: k = lunation number; T = k/1236.85
    out = []
    k = math.floor((jd_lo - 2451550.09766) / 29.530588861) - 1
    while True:
        T = k / 1236.85
        JDE = (2451550.09766 + 29.530588861 * k
               + 0.0003258 * T * T + 0.0000000188 * T * T * T)
        # periodic corrections (the dominant ones)
        E = 1 - 0.002516 * T - 0.0000074 * T * T
        M = math.radians((2.5534 + 29.10535670 * k) % 360)   # sun mean anomaly
        Mp = math.radians((201.5643 + 385.81693528 * k) % 360)  # moon mean anomaly
        F = math.radians((160.7108 + 390.67050284 * k) % 360)  # moon argument of latitude
        Om = math.radians((124.7746 + 1.56375588 * k) % 360)   # ascending node
        A1 = math.radians((299.77 + 0.54031908 * k) % 360)
        add = (-0.40720 * math.sin(Mp)
               + 0.17241 * E * math.sin(M)
               + 0.01608 * math.sin(2 * Mp)
               + 0.01039 * math.sin(2 * F)
               + 0.00739 * math.sin(Mp - M)
               - 0.00514 * E * math.sin(Mp + M)
               + 0.00208 * E * E * math.sin(2 * M)
               - 0.00111 * math.sin(Mp - 2 * F)
               - 0.00057 * math.sin(Mp + 2 * F)
               + 0.00056 * E * math.sin(2 * Mp + M)
               - 0.00042 * math.sin(3 * Mp)
               + 0.00042 * E * math.sin(M + 2 * F)
               + 0.00038 * E * math.sin(M - 2 * F)
               - 0.00024 * E * math.sin(2 * Mp - M)
               - 0.00017 * math.sin(Om)
               - 0.00007 * math.sin(Mp + 2 * M)
               + 0.00004 * math.sin(2 * Mp - 2 * F)
               + 0.00004 * math.sin(3 * M)
               + 0.00003 * math.sin(Mp + M - 2 * F)
               + 0.00003 * math.sin(2 * Mp + 2 * F)
               - 0.00003 * math.sin(Mp + M + 2 * F)
               + 0.00003 * math.sin(Mp - M + 2 * F)
               - 0.00002 * math.sin(Mp - M - 2 * F)
               - 0.00002 * math.sin(3 * Mp + M)
               + 0.00002 * math.sin(4 * Mp))
        corr = (0.000325 * math.sin(A1)
                + 0.000165 * math.sin(math.radians((52.27 + 1.719 * k)))
                - 0.000128 * math.sin(Om)  # approx: use Om term
                )
        jd = JDE + add + corr
        if jd > jd_hi:
            break
        if jd >= jd_lo:
            out.append(jd)
        k += 1
    return out


def new_moon_before(jd):
    got = new_moons(jd - 40, jd + 2)
    return [j for j in got if j <= jd][-1]


def new_moon_after(jd):
    got = new_moons(jd, jd + 40)
    return got[0]


def _date_to_jd(d):
    y, m = d.year, d.month
    day = d.day
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4
    return (math.floor(365.25 * (y + 4716)) + math.floor(30.6001 * (m + 1))
            + day + b - 1524.5)


def _sun_longitude(jd):
    """Apparent solar longitude (deg) — Meeus ch.25 (low accuracy, 0.01°)."""
    T = (jd - J2000) / 36525.0
    L0 = 280.46646 + 36000.76983 * T + 0.0003032 * T * T
    M = math.radians((357.52911 + 35999.05029 * T - 0.0001537 * T * T) % 360)
    e = 0.016708634 - 0.000042037 * T - 0.0000001267 * T * T
    C = ((1.914602 - 0.004817 * T - 0.000014 * T * T) * math.sin(M)
         + (0.019993 - 0.000101 * T) * math.sin(2 * M)
         + 0.000289 * math.sin(3 * M))
    true_long = L0 + C
    omega = math.radians(125.04 - 1934.136 * T)
    app = true_long - 0.00569 - 0.00478 * math.sin(omega)
    return app % 360


def _moon_longitude(jd):
    """Geocentric apparent lunar longitude (deg) — Meeus ch.47, main terms
    (~0.01° is more than enough for day-level tithis)."""
    T = (jd - J2000) / 36525.0
    Lp = 218.3164477 + 481267.88123421 * T - 0.0015786 * T * T \
        + T * T * T / 538841.0 - T ** 4 / 65194000.0
    D = math.radians((297.8501921 + 445267.1114034 * T - 0.0018819 * T * T
                      + T ** 3 / 545868.0) % 360)
    M = math.radians((357.5291092 + 35999.0502909 * T - 0.0001536 * T * T
                      + T ** 3 / 24490000.0) % 360)
    Mp = math.radians((134.9633964 + 477198.8675055 * T + 0.0087414 * T * T
                       + T ** 3 / 69699.0) % 360)
    F = math.radians((93.2720950 + 483202.0175233 * T - 0.0036539 * T * T
                      + T ** 3 / 3528300.0) % 360)
    lon = (Lp
           + 6.288774 * math.sin(Mp)
           + 1.274027 * math.sin(2 * D - Mp)
           + 0.658314 * math.sin(2 * D)
           + 0.213618 * math.sin(2 * Mp)
           - 0.185116 * math.sin(M)
           - 0.114332 * math.sin(2 * F)
           + 0.058793 * math.sin(2 * D - 2 * Mp)
           + 0.057066 * math.sin(2 * D - M - Mp)
           + 0.053322 * math.sin(2 * D + Mp)
           + 0.045758 * math.sin(2 * D - M)
           - 0.040923 * math.sin(M - Mp)
           - 0.034720 * math.sin(D)
           - 0.030383 * math.sin(M + Mp)
           + 0.015327 * math.sin(2 * D - 2 * F)
           - 0.012528 * math.sin(Mp + 2 * F)
           + 0.010980 * math.sin(Mp - 2 * F)
           + 0.010675 * math.sin(4 * D - Mp)
           + 0.010034 * math.sin(3 * Mp)
           + 0.008548 * math.sin(4 * D - 2 * Mp))
    return lon % 360


def tithi_at_sunrise(d):
    """Tithi index 0..29 running at ~06:00 IST on civil date d
    (0 = new moon/shukla pratipada … 14 = purnima … 29 = amavasya)."""
    jd = _date_to_jd(d) + (6.0 - 5.5) / 24.0  # 06:00 IST = 00:30 UT
    elong = (_moon_longitude(jd) - _sun_longitude(jd)) % 360
    return int(elong // 12)


def day_containing_new_moon(lo, hi):
    """Civil date (IST) of the new moon between lo and hi: the conjunction
    day, rolled to the IST date; if the conjunction is in the first hours
    of the IST day the previous evening's convention may shift ±1 — the
    festival descriptions carry the caveat."""
    jd_lo = _date_to_jd(lo)
    jd_hi = _date_to_jd(hi) + 1.0
    moon = new_moons(jd_lo, jd_hi)
    if not moon:
        return None
    jd = moon[0]
    return _jd_to_date(jd + 5.5 / 24.0)  # IST date


def day_with_tithi(tithi, lo, hi):
    """First civil date in [lo, hi] whose sunrise tithi is `tithi`
    (the panchang convention: the festival marks the day the tithi runs at
    sunrise)."""
    d = lo
    while d <= hi:
        if tithi_at_sunrise(d) == tithi:
            return d
        d += datetime.timedelta(days=1)
    return None


# ==========================================================================
# Chinese calendar (CST = UTC+8).  Month 1 day 1 (Spring Festival) = the day
# of the SECOND new moon after the winter solstice — exact for every year in
# range except the rare leap-11th-month years (next: 2033, outside).  Solar
# terms for Qingming.  Official holiday ARRANGEMENTS (day windows, swapped
# workdays) are announced by the State Council the November before — each
# event's description says the festival day may sit inside a longer window.
# ==========================================================================

CST = 8 / 24.0


def _sun_crossing(jd_start, target_deg, max_days=5.0, tz=CST):
    """The civil date (in the given timezone, as a day fraction: CST for
    the Chinese solar terms, IST for the Indian sankrantis) on which the
    sun's apparent longitude first reaches `target_deg` after `jd_start`
    (stepped in 6-hour ticks).  The 2.5-hour difference between those two
    can move the civil day when the crossing lands near midnight."""
    prev = _sun_longitude(jd_start) % 360
    jd = jd_start
    while jd < jd_start + max_days:
        jd += 0.25
        cur = _sun_longitude(jd) % 360
        # a crossing happened iff cur wrapped past target between prev..cur
        span = (cur - prev) % 360
        before = (target_deg - prev) % 360
        if before <= span:
            return _jd_to_date(jd + tz)
        prev = cur
    return None


def winter_solstice_cst(year):
    """CST date of the December solstice (sun longitude 270°)."""
    jd_dec = _date_to_jd(datetime.date(year, 12, 18))
    # 270° is reached ~Dec 21-22; walk forward from ~December 1 (250°)
    jd = _date_to_jd(datetime.date(year, 12, 1))
    return _sun_crossing(jd, 270.0, max_days=30.0)


def chinese_new_year(year):
    """The Spring Festival day of `year`: the day (CST) of the second new
    moon after the preceding December's solstice."""
    solstice = winter_solstice_cst(year - 1)
    jd_solstice = _date_to_jd(solstice)
    moons = new_moons(jd_solstice, jd_solstice + 80.0)
    assert len(moons) >= 2, f"expected two new moons after the {year - 1} solstice"
    return _jd_to_date(moons[1] + CST)


def chinese_lunar_day(year, month, day):
    """Civil date of `day` of lunar month `month` (1-based, counting from
    the Spring Festival's own month) in the lunar year starting `year`."""
    cny_jd = _date_to_jd(chinese_new_year(year))
    if month == 1:
        moons = [cny_jd + 0.0]
    else:
        moons = new_moons(cny_jd, cny_jd + (month) * 29.6 + 5)
        assert len(moons) >= month - 1, f"not enough new moons for month {month} of lunar {year}"
        moons = moons[: month - 1]
    return _jd_to_date(moons[-1] + CST) + datetime.timedelta(days=day - 1)


def qingming(year):
    """The Qingming solar term (sun longitude 15°), early April."""
    jd = _date_to_jd(datetime.date(year, 4, 1))
    return _sun_crossing(jd, 15.0, max_days=7.0)


CHINA_NOTE = ("Official holiday windows (incl. adjusted workdays) are announced "
              "by the State Council the November before; this is the festival day "
              "itself, computed from the lunar calendar.")


def set_china():
    events = []
    for y in YEARS:
        sf = chinese_new_year(y)
        events.append(Event(sf, "Spring Festival (Chinese New Year)",
                            "spring-festival", CHINA_NOTE + " Begins at midnight; "
                            "the official break typically spans the first week of "
                            "the lunar month."))
        events.append(Event(chinese_lunar_day(y, 1, 15), "Lantern Festival",
                            "lantern", "15th day of the first lunar month. " + CHINA_NOTE))
        q = qingming(y)
        if q:
            events.append(Event(q, "Qingming Festival", "qingming",
                                "Tomb-sweeping day, the 5th solar term. " + CHINA_NOTE))
        events.append(Event(datetime.date(y, 5, 1), "Labour Day", "labour-day", CHINA_NOTE))
        events.append(Event(chinese_lunar_day(y, 5, 5), "Dragon Boat Festival",
                            "dragon-boat", "5th day of the 5th lunar month. " + CHINA_NOTE))
        events.append(Event(chinese_lunar_day(y, 8, 15), "Mid-Autumn Festival",
                            "mid-autumn", "15th day of the 8th lunar month. " + CHINA_NOTE))
        events.append(Event(datetime.date(y, 10, 1), "National Day", "national-day",
                            CHINA_NOTE + " The official break typically spans Oct 1-7."))
        events.append(Event(datetime.date(y, 1, 1), "New Year's Day", "new-year", CHINA_NOTE))
    return events


# ==========================================================================
# Civil sets: United States (federal), Canada (national), United Kingdom
# ==========================================================================

def set_us():
    events = []
    for y in YEARS:
        def add(d, name, slug, desc=""):
            events.append(Event(d, name, slug, desc))

        jan1 = datetime.date(y, 1, 1)
        add(observed(jan1), "New Year's Day", "new-year",
            "" if jan1.weekday() < 5 else "Observed on this day because Jan 1 fell on a weekend.")
        add(nth_weekday(y, 1, 0, 3), "Martin Luther King Jr. Day", "mlk-day",
            "Third Monday of January.")
        add(nth_weekday(y, 2, 0, 3), "Presidents' Day (Washington's Birthday)", "presidents-day",
            "Third Monday of February.")
        add(nth_weekday(y, 5, 0, -1), "Memorial Day", "memorial-day", "Last Monday of May.")
        juneteenth = datetime.date(y, 6, 19)
        add(observed(juneteenth), "Juneteenth", "juneteenth",
            "" if juneteenth.weekday() < 5 else "Observed on this day because Jun 19 fell on a weekend.")
        july4 = datetime.date(y, 7, 4)
        add(observed(july4), "Independence Day", "independence-day",
            "" if july4.weekday() < 5 else "Observed on this day because Jul 4 fell on a weekend.")
        add(nth_weekday(y, 9, 0, 1), "Labor Day", "labor-day", "First Monday of September.")
        add(nth_weekday(y, 10, 0, 2), "Columbus Day", "columbus-day",
            "Second Monday of October (federal; many states mark Indigenous Peoples' Day).")
        veterans = datetime.date(y, 11, 11)
        add(observed(veterans), "Veterans Day", "veterans-day",
            "" if veterans.weekday() < 5 else "Observed on this day because Nov 11 fell on a weekend.")
        add(nth_weekday(y, 11, 3, 4), "Thanksgiving", "thanksgiving",
            "Fourth Thursday of November.")
        christmas = datetime.date(y, 12, 25)
        add(observed(christmas), "Christmas Day", "christmas",
            "" if christmas.weekday() < 5 else "Observed on this day because Dec 25 fell on a weekend.")
    return events


def set_canada():
    events = []
    for y in YEARS:
        def add(d, name, slug, desc=""):
            events.append(Event(d, name, slug, desc))

        add(observed(datetime.date(y, 1, 1)), "New Year's Day", "new-year",
            "A statutory holiday in every province and territory.")
        add(nth_weekday(y, 2, 0, 3), "Family Day", "family-day",
            "Third Monday of February; a statutory holiday in most provinces "
            "(AB, ON, SK and others; not a federal holiday).")
        add(easter_gregorian(y) - datetime.timedelta(days=2), "Good Friday", "good-friday",
            "A statutory holiday in every province and territory.")
        add(easter_gregorian(y) + datetime.timedelta(days=1), "Easter Monday", "easter-monday",
            "A statutory holiday for federal employees and in some provinces.")
        # Victoria Day: the Monday preceding May 25
        vic = datetime.date(y, 5, 24)
        while vic.weekday() != 0:
            vic -= datetime.timedelta(days=1)
        add(vic, "Victoria Day", "victoria-day",
            "The Monday preceding May 25; statutory everywhere except the Atlantic provinces.")
        canada_day = datetime.date(y, 7, 1)
        add(observed(canada_day), "Canada Day", "canada-day",
            "" if canada_day.weekday() < 5 else "Observed on this day because Jul 1 fell on a weekend.")
        add(nth_weekday(y, 8, 0, 1), "Civic Holiday", "civic-holiday",
            "First Monday of August; not a statutory holiday everywhere "
            "(BC Day in British Columbia, Heritage Day in Alberta, etc.).")
        add(nth_weekday(y, 9, 0, 1), "Labour Day", "labour-day", "First Monday of September.")
        add(datetime.date(y, 9, 30), "National Day for Truth and Reconciliation", "truth-reconciliation",
            "A federal statutory holiday; observed as a statutory holiday in some provinces.")
        add(nth_weekday(y, 10, 0, 2), "Thanksgiving (Canada)", "thanksgiving",
            "Second Monday of October.")
        remembrance = datetime.date(y, 11, 11)
        add(observed(remembrance), "Remembrance Day", "remembrance-day",
            "A statutory holiday in most provinces; a federal holiday.")
        christmas = datetime.date(y, 12, 25)
        add(observed(christmas), "Christmas Day", "christmas",
            "" if christmas.weekday() < 5 else "Observed on this day because Dec 25 fell on a weekend.")
        boxing = datetime.date(y, 12, 26)
        add(observed(boxing, sunday_to_monday=False), "Boxing Day", "boxing-day",
            "Statutory in Ontario and federal; observed on the next weekday when it falls on a weekend.")
    return events


def _uk_substitute(day: datetime.date) -> datetime.date:
    """UK substitute-day rule: a bank holiday falling on a weekend moves to
    the next available weekday (per day, not merged)."""
    d = day
    while d.weekday() >= 5:
        d += datetime.timedelta(days=1)
    return d


def set_uk():
    events = []
    for y in YEARS:
        def add(d, name, slug, desc=""):
            events.append(Event(d, name, slug, desc))

        add(_uk_substitute(datetime.date(y, 1, 1)), "New Year's Day", "new-year",
            "England & Wales bank holiday (Scotland adds Jan 2).")
        add(easter_gregorian(y) - datetime.timedelta(days=2), "Good Friday", "good-friday",
            "A bank holiday throughout the United Kingdom.")
        add(easter_gregorian(y) + datetime.timedelta(days=1), "Easter Monday", "easter-monday",
            "A bank holiday in England & Wales and Northern Ireland (not Scotland).")
        add(nth_weekday(y, 5, 0, 1), "Early May Bank Holiday", "early-may",
            "First Monday of May.")
        add(nth_weekday(y, 5, 0, -1), "Spring Bank Holiday", "spring-bank",
            "Last Monday of May.")
        add(nth_weekday(y, 8, 0, -1), "Summer Bank Holiday", "summer-bank",
            "Last Monday of August (Scotland keeps the first Monday of August).")
        add(_uk_substitute(datetime.date(y, 12, 25)), "Christmas Day", "christmas",
            "England & Wales bank holiday; a weekend date moves to a substitute weekday.")
        add(_uk_substitute(datetime.date(y, 12, 26)), "Boxing Day", "boxing-day",
            "England & Wales bank holiday; a weekend date moves to a substitute weekday.")
        add(_uk_substitute(datetime.date(y, 3, 17)), "St Patrick's Day", "st-patrick",
            "A bank holiday in Northern Ireland.")
        add(_uk_substitute(datetime.date(y, 7, 12)), "Battle of the Boyne (Orangemen's Day)",
            "battle-of-the-boyne",
            "A bank holiday in Northern Ireland.")
        add(_uk_substitute(datetime.date(y, 11, 30)), "St Andrew's Day", "st-andrew",
            "A bank holiday in Scotland.")
    return events


# ==========================================================================
# Judaism — the full annual cycle.  Every observance begins at sundown the
# evening before the civil date shown; the descriptions say so.  The modern
# Israeli memorial days follow their statutory weekday-shift rules.
# ==========================================================================

JEWISH_NOTE = "Begins at sundown the evening before."


def set_judaism():
    events = []
    lo = datetime.date(YEARS[0], 1, 1)
    hi = datetime.date(YEARS[-1], 12, 31)

    def add(date, name, slug, desc=""):
        if lo <= date <= hi:
            events.append(Event(date, name, slug, JEWISH_NOTE + " " + desc if desc else JEWISH_NOTE))

    # Walk the Hebrew years that intersect the window.
    for hy in range(5786, 5793):
        s = hebrew_month_starts(hy)
        leap_y = hebrew_leap(hy)
        adar = "Adar II" if leap_y else "Adar"

        add(s["Tishri"], "Rosh Hashanah (Day 1)", "rosh-hashanah",
            "The Jewish new year; second day follows immediately.")
        add(s["Tishri"] + datetime.timedelta(days=1), "Rosh Hashanah (Day 2)", "rosh-hashanah-2",
            "The second day of Rosh Hashanah.")
        tzom_gedaliah = s["Tishri"] + datetime.timedelta(days=2)
        if tzom_gedaliah.weekday() == 5:  # fasts are not kept on Shabbat
            tzom_gedaliah += datetime.timedelta(days=1)
        add(tzom_gedaliah, "Tzom Gedaliah (Fast)", "tzom-gedaliah",
            "A minor fast from dawn to nightfall.")
        add(s["Tishri"] + datetime.timedelta(days=9), "Yom Kippur (Erev)", "yom-kippur-erev",
            "Kol Nidre is chanted this evening.")
        add(s["Tishri"] + datetime.timedelta(days=9), "Yom Kippur", "yom-kippur",
            "The Day of Atonement, a 25-hour fast.")
        add(s["Tishri"] + datetime.timedelta(days=14), "Sukkot (First Day)", "sukkot",
            "The Festival of Booths, seven days.")
        for i in range(1, 7):
            add(s["Tishri"] + datetime.timedelta(days=14 + i), f"Sukkot (Chol haMoed, Day {i})",
                f"chol-hamoed-{i}", "Intermediate day of Sukkot.")
        add(s["Tishri"] + datetime.timedelta(days=21), "Shmini Atzeret", "shmini-atzeret",
            "The eighth-day assembly.")
        add(s["Tishri"] + datetime.timedelta(days=22), "Simchat Torah", "simchat-torah",
            "The Torah cycle concludes and restarts (kept on Shmini Atzeret in Israel).")
        add(s["Kislev"] + datetime.timedelta(days=24), "Hanukkah (First Night)", "hanukkah",
            "The eight-day festival begins this evening.")
        for i in range(8):
            add(s["Kislev"] + datetime.timedelta(days=24 + i),
                f"Hanukkah (Day {i + 1})" if i else "Hanukkah (First Day)",
                "hanukkah-day" if i else "hanukkah-day1",
                "The rededication of the Temple.")
        add(s["Tevet"] + datetime.timedelta(days=9), "Asarah b'Tevet (Fast)", "asarah-btevet",
            "A minor fast from dawn to nightfall.")
        add(s["Shvat"] + datetime.timedelta(days=14), "Tu BiShvat", "tu-bishvat",
            "The new year of the trees.")
        taanit_esther = s[adar] + datetime.timedelta(days=12)
        if taanit_esther.weekday() == 5:  # moved back to Thursday
            taanit_esther -= datetime.timedelta(days=1)
        add(taanit_esther, "Ta'anit Esther (Fast)", "taanit-esther",
            "The fast before Purim.")
        add(s[adar] + datetime.timedelta(days=13), "Purim", "purim",
            "The reading of the Megillah is the evening before.")
        add(s[adar] + datetime.timedelta(days=14), "Shushan Purim", "shushan-purim",
            "Purim as kept in walled cities.")
        add(s["Nisan"] + datetime.timedelta(days=14), "Pesach (First Seder)", "pesach-seder",
            "The first Seder takes place this evening.")
        add(s["Nisan"] + datetime.timedelta(days=14), "Pesach (First Day)", "pesach",
            "The Festival of Freedom, seven days (eight outside Israel).")
        for i in range(1, 5):
            add(s["Nisan"] + datetime.timedelta(days=14 + i), f"Pesach (Chol haMoed, Day {i})",
                f"pesach-chol-{i}", "Intermediate day of Pesach.")
        add(s["Nisan"] + datetime.timedelta(days=20), "Pesach (Final Days)", "pesach-final",
            "The seventh (and outside Israel eighth) day of Pesach.")
        shoah = s["Nisan"] + datetime.timedelta(days=26)
        if shoah.weekday() in (4, 5):  # would fall Friday or Saturday
            shoah -= datetime.timedelta(days=1)
        add(shoah, "Yom HaShoah", "yom-hashoah",
            "Holocaust Remembrance Day (27 Nisan, moved to the preceding "
            "Thursday when it would fall on Friday or Saturday); siren at 10:00 "
            "in Israel.")
        # Yom HaZikaron / Yom HaAtzmaut (4/5 Iyar, shifted off Friday, Saturday
        # and Monday by the 2004 rules):
        atzmaut = s["Iyar"] + datetime.timedelta(days=4)
        if atzmaut.weekday() in (4, 5):        # Friday or Saturday → back to Thursday
            zikaron, atzmaut = atzmaut - datetime.timedelta(days=2), atzmaut - datetime.timedelta(days=1)
        elif atzmaut.weekday() == 0:           # Monday → forward to Tuesday
            zikaron, atzmaut = atzmaut, atzmaut + datetime.timedelta(days=1)
        else:
            zikaron = atzmaut - datetime.timedelta(days=1)
        add(zikaron, "Yom HaZikaron", "yom-hazikaron",
            "Israel's Remembrance Day (4 Iyar with the statutory shifts).")
        add(atzmaut, "Yom HaAtzmaut", "yom-haatzmaut",
            "Israel's Independence Day (5 Iyar with the statutory shifts).")
        add(s["Iyar"] + datetime.timedelta(days=17), "Lag BaOmer", "lag-baomer",
            "The 33rd day of the Omer, a minor festival.")
        add(s["Iyar"] + datetime.timedelta(days=27), "Yom Yerushalayim", "yom-yerushalayim",
            "Jerusalem Day.")
        add(s["Sivan"] + datetime.timedelta(days=5), "Shavuot (First Day)", "shavuot",
            "The Festival of Weeks; all-night learning begins the evening before.")
        add(s["Sivan"] + datetime.timedelta(days=6), "Shavuot (Second Day)", "shavuot-2",
            "The second day of Shavuot (kept as a single day in Israel).")
        shivaasar = s["Tammuz"] + datetime.timedelta(days=16)
        if shivaasar.weekday() == 5:
            shivaasar += datetime.timedelta(days=1)
        add(shivaasar, "Shiva Asar b'Tammuz (Fast)", "shivaasar-btammuz",
            "A minor fast from dawn to nightfall.")
        tisha = s["Av"] + datetime.timedelta(days=8)
        if tisha.weekday() == 5:
            tisha += datetime.timedelta(days=1)
        add(tisha, "Tisha B'Av (Fast)", "tisha-bav",
            "The fast of the Ninth of Av.")
        add(s["Av"] + datetime.timedelta(days=14), "Tu B'Av", "tu-bav",
            "A minor festival of love.")

    return events


# ==========================================================================
# Islam — major observances, TABULAR Hijri (±1 day vs local moon sightings).
# ==========================================================================

ISLAM_NOTE = ("Tabular Hijri date; actual dates follow local moon sightings and "
              "may differ by ±1 day.")


def set_islam():
    events = []
    lo = datetime.date(YEARS[0], 1, 1)
    hi = datetime.date(YEARS[-1], 12, 31)

    def add(date, name, slug, desc="", days=1):
        if lo <= date <= hi:
            events.append(Event(date, name, slug, ISLAM_NOTE + " " + desc if desc else ISLAM_NOTE,
                                days=days))

    for hy in range(1446, 1453):
        add(islamic_to_civil(hy, 1, 1), "Islamic New Year (1 Muharram)", "islamic-new-year")
        add(islamic_to_civil(hy, 1, 10), "Ashura (10 Muharram)", "ashura",
            "A voluntary fast day; commemorates the Exodus for Sunnis and the "
            "martyrdom of Husayn at Karbala for Shia Muslims.")
        add(islamic_to_civil(hy, 3, 12), "Mawlid al-Nabi", "mawlid",
            "The birthday of the Prophet Muhammad.")
        add(islamic_to_civil(hy, 8, 15), "Laylat al-Baraat", "laylat-baraat")
        add(islamic_to_civil(hy, 9, 1), "Ramadan begins (1 Ramadan)", "ramadan",
            "The fasting month; the first fast is the next day.")
        add(islamic_to_civil(hy, 9, 27), "Laylat al-Qadr (27 Ramadan, approximate)",
            "laylat-al-qadr",
            "The Night of Power falls in the last ten odd nights of Ramadan; the "
            "27th is the traditional observance.")
        add(islamic_to_civil(hy, 10, 1), "Eid al-Fitr", "eid-al-fitr",
            "The festival of breaking the fast; three days.", days=3)
        add(islamic_to_civil(hy, 12, 9), "Day of Arafah (9 Dhul-Hijjah)", "arafah",
            "The Hajj's day of standing; a recommended fast for non-pilgrims.")
        add(islamic_to_civil(hy, 12, 10), "Eid al-Adha", "eid-al-adha",
            "The festival of the sacrifice; with the three days of Tashreeq.", days=4)
    return events


# ==========================================================================
# Hindu, Buddhist and Sikh festival sets — three separate religions, three
# separate sets, one shared panchang-lite machinery: the civil day whose
# sunrise (06:00 IST) carries the required tithi.  Approximate by
# construction (regional panchangs and local sunrise shift dates by a day) —
# every lunar description says so.
#
# The wrinkle the windows have to survive: 2026 and 2029 both carry an
# Adhika Ashwina (the leap month falls in the Navaratri season), pushing
# every Bhadrapada/Ashvina festival ~19 days past its usual Gregorian slot —
# and the pre-leap years park the PRIOR month's purnima inside any naive
# window.  So the purnima festivals that would collide (Holi vs Magha,
# Vesak vs Chaitra) are anchored RELATIVE to Holi's moon (Phalguna:
# Magha = one lunar month before it, Vaisakha = two after), and the
# Navaratri tithis (Durga Ashtami, Vijayadashami) hang off the Ashvina
# purnima itself.  Vaisakhi is genuinely shared (the Hindu Vaisakha
# Sankranti and the Khalsa's founding are the same solar day), so it rides
# both the Hindu and the Sikh set with its own wording; Bandi Chhor Divas
# shares Diwali's Amavasya.  Sankrantis are sidereal: the crossing is
# tested against the sun's longitude minus the Lahiri ayanamsa — a purely
# tropical reading puts Makar Sankranti on Jan 10 and Vaisakhi on Apr 20
# (or nowhere) while both claim "accurate".
# ==========================================================================

INDIA_NOTE = ("Computed from the tithi running at sunrise (panchang convention, "
              "06:00 IST approximation); regional calendars may shift it ±1 day.")

# Lahiri ayanamsa, 2026-2030 (24.16°-24.20°, drifting ~0.007°/yr): one
# constant is ±0.05° across the range — well inside the day-roll.
AYANAMSA = 24.2

IST = 5.5 / 24.0  # sankranti dates roll to the Indian civil day


def _panchang_adder(events):
    """The three sets' shared `add`: out-of-range and unresolvable (None)
    dates drop, and the panchang caveat rides on every event — the solar
    sankranti festivals append their own Events instead (they are accurate
    and say so)."""
    lo, hi = datetime.date(YEARS[0], 1, 1), datetime.date(YEARS[-1], 12, 31)

    def add(date, name, slug, note=""):
        if date and lo <= date <= hi:
            events.append(Event(date, name, slug, INDIA_NOTE + " " + note if note else INDIA_NOTE))

    return add


def day_with_purnima(after, lo, hi):
    """A Purnima festival day: the first sunrise carrying the festival's
    tithi — 14 = purnima at sunrise (Magha Puja, Vesak, Asalha Puja, Raksha
    Bandhan, Guru Nanak Jayanti), 15 = the sunrise just past the full moon
    (Holi, observed the day the overnight purnima ends) — falling back to
    the other when the tithi skips sunrises entirely (a ksaya tithi: Holi
    2028's pratipada ran sunrise-to-sunrise, so no day "carried" it and the
    festival rides the adjacent purnima day)."""
    other = 14 if after == 15 else 15
    return day_with_tithi(after, lo, hi) or day_with_tithi(other, lo, hi)


TD = datetime.timedelta


def holi(y):
    """Phalguna Purnima.  The window starts in March so the Magha purnima
    (late Jan-late Feb) can never be first (2027's Feb 21 moon was the
    old set's Holi — a month early)."""
    return day_with_purnima(15, datetime.date(y, 3, 1), datetime.date(y, 3, 25))


def magha_puja(y):
    """Magha Purnima — anchored one lunar month before Holi's moon, where
    the Pausha purnima cannot wander in."""
    h = holi(y)
    return h and day_with_purnima(14, h - TD(days=34), h - TD(days=25))


def vesak(y):
    """Vaisakha Purnima — anchored two lunar months after Holi's moon, past
    the Chaitra purnima that a flat window catches first (2027: Chaitra
    Apr 20 vs Vaisakha May 20)."""
    h = holi(y)
    return h and day_with_purnima(14, h + TD(days=52), h + TD(days=66))


def asalha_puja(y):
    """Ashadha Purnima (Guru Purnima's moon; Dhamma Day to Buddhists)."""
    return day_with_purnima(14, datetime.date(y, 6, 25), datetime.date(y, 7, 25))


def raksha_bandhan(y):
    """Shravana Purnima."""
    return day_with_purnima(14, datetime.date(y, 7, 25), datetime.date(y, 8, 25))


def guru_nanak(y):
    """Kartika Purnima — Guru Nanak's birthday.  November's full moon:
    Ashvina's purnima (Oct 26 in 2026) sits outside the window, which is
    the whole point — a window starting in October grabs the wrong moon
    exactly in the years this set exists for."""
    return day_with_purnima(14, datetime.date(y, 11, 1), datetime.date(y, 11, 30))


def shivaratri(y):
    """Maha Shivaratri — Magha krishna chaturdashi at sunrise."""
    return day_with_tithi(28, datetime.date(y, 2, 10), datetime.date(y, 3, 12))


def janmashtami(y):
    """Krishna Janmashtami — the ashtami after the Bhadrapada purnima.  In
    adhika-adjacent years the pre-purnima (Shravana) ashtami sits ~4 weeks
    earlier: the window starts past it (2027's Aug 25) but before the
    earliest true date (2029's Sep 1)."""
    return day_with_tithi(22, datetime.date(y, 8, 26), datetime.date(y, 9, 25))


def ganesh_chaturthi(y):
    """Bhadrapada shukla chaturthi.  The Shravana chaturthi (~1 lunar
    month earlier) and the adhika twin (~1 month later) each land inside a
    naive window in some year of the range; this one is cut to exclude
    both (2027: Shravana Aug 5 vs true Sep 4; 2030: Shravana Aug 3 vs true
    Sep 1)."""
    return day_with_tithi(3, datetime.date(y, 8, 6), datetime.date(y, 9, 14))


def ashvina_purnima(y):
    """The purnima of (nija) Ashvina — the anchor the Navaratri tithis
    hang from.  Adhika-Ashwina years (2026, 2029 in range) push every
    Ashvina-shukla festival ~19 days past its usual window, so those tithis
    are taken RELATIVE to this purnima instead of from a fixed Gregorian
    window."""
    return day_with_purnima(14, datetime.date(y, 10, 1), datetime.date(y, 11, 1))


def durga_ashtami(y):
    p = ashvina_purnima(y)
    return p and day_with_tithi(7, p - TD(days=12), p - TD(days=4))


def vijayadashami(y):
    p = ashvina_purnima(y)
    return p and day_with_tithi(9, p - TD(days=11), p - TD(days=3))


def diwali(y):
    """The Amavasya of Ashvina (Lakshmi Puja falls on its evening)."""
    return day_with_tithi(29, datetime.date(y, 10, 18), datetime.date(y, 11, 14))


def makar_sankranti(y):
    """The sun's sidereal entry into Capricorn (270° + ayanamsa tropical)."""
    return _sun_crossing(_date_to_jd(datetime.date(y, 1, 10)), 270.0 + AYANAMSA,
                         max_days=10.0, tz=IST)


def vaisakhi(y):
    """The sun's sidereal entry into Aries (0° + ayanamsa tropical)."""
    return _sun_crossing(_date_to_jd(datetime.date(y, 4, 10)), AYANAMSA,
                         max_days=10.0, tz=IST)


def set_hindu():
    events = []
    add = _panchang_adder(events)
    for y in YEARS:
        add(holi(y), "Holi", "holi",
            "The Purnima of Phalguna; Holika Dahan is the previous evening.")
        add(shivaratri(y), "Maha Shivaratri", "shivaratri",
            "The 14th night of the waning moon of Magha.")
        add(raksha_bandhan(y), "Raksha Bandhan", "raksha-bandhan",
            "The Purnima of Shravana.")
        add(janmashtami(y), "Krishna Janmashtami", "janmashtami",
            "The 8th of the waning moon after the Bhadrapada Purnima.")
        add(ganesh_chaturthi(y), "Ganesh Chaturthi", "ganesh-chaturthi",
            "The 4th of the waxing moon of Bhadrapada.")
        add(durga_ashtami(y), "Durga Ashtami", "durga-ashtami",
            "The 8th of the waxing moon of Ashvina, within Navaratri.")
        add(vijayadashami(y), "Vijayadashami (Dussehra)", "vijayadashami",
            "The 10th of the waxing moon of Ashvina.")
        add(diwali(y), "Diwali (Lakshmi Puja)", "diwali",
            "The Amavasya of Kartika; celebrations extend five days.")
        sankranti = makar_sankranti(y)
        if sankranti:
            events.append(Event(sankranti, "Makar Sankranti / Pongal", "makar-sankranti",
                                "The sun's sidereal entry into Capricorn (solar "
                                "festival, accurate)."))
        baisakhi = vaisakhi(y)
        if baisakhi:
            events.append(Event(baisakhi, "Vaisakhi (Vaisakha Sankranti)", "vaisakhi",
                                "The sun's sidereal entry into Aries — the harvest "
                                "new year of the Punjab (solar, accurate)."))
    return events


def set_buddhist():
    events = []
    add = _panchang_adder(events)
    for y in YEARS:
        add(magha_puja(y), "Magha Puja (Sangha Day)", "magha-puja",
            "The full moon of Magha; a major Theravada observance.")
        add(vesak(y), "Vesak (Buddha Day)", "vesak",
            "The full moon of Vaisakha: birth, enlightenment and passing of the Buddha.")
        add(asalha_puja(y), "Asalha Puja (Dhamma Day)", "asalha-puja",
            "The full moon of Asalha; the first sermon.")
    return events


def set_sikh():
    events = []
    add = _panchang_adder(events)
    for y in YEARS:
        add(diwali(y), "Bandi Chhor Divas", "bandi-chhor-divas",
            "The Amavasya of Kartika, the day of Diwali: the release of "
            "Guru Hargobind from Gwalior Fort.")
        add(guru_nanak(y), "Guru Nanak Jayanti", "guru-nanak-jayanti",
            "The Kartika Purnima; the birthday of Guru Nanak.")
        add(datetime.date(y, 1, 5), "Guru Gobind Singh Jayanti", "guru-gobind-singh",
            "Kept on Jan 5 by the Nanakshahi calendar; the panchang-based "
            "observance varies by year.")
        baisakhi = vaisakhi(y)
        if baisakhi:
            events.append(Event(baisakhi, "Vaisakhi (Khalsa Founding Day)", "vaisakhi",
                                "The sun's sidereal entry into Aries — the Khalsa "
                                "was founded by Guru Gobind Singh in 1699 "
                                "(solar, accurate)."))
    return events


# ==========================================================================
# Roman Catholic, current form (Ordinary Form, General Roman Calendar).
# Sundays and movable seasons from the date of Easter; a curated list of
# solemnities and feasts; the description carries the liturgical colour.
# Transferred feasts (Epiphany/Ascension/Corpus Christi in some countries)
# are noted, not moved — the universal calendar's dates are emitted.
# ==========================================================================

def _advent_first(year):
    """Advent Sunday: the Sunday nearest to Nov 30 (between Nov 27 and Dec 3)."""
    st_andrew = datetime.date(year, 11, 30)
    offset = (st_andrew.weekday() + 1) % 7  # days since the previous Sunday
    if offset <= 3:  # nearer to the preceding Sunday
        return st_andrew - datetime.timedelta(days=offset)
    return st_andrew + datetime.timedelta(days=7 - offset)


def _rc_current_ordinary(year):
    """The Ordinary Time Sundays of the current calendar, numbered per the
    Universal Norms (verified for 2026-2030 against romcal's calendar):
    the Baptism of the Lord is the FIRST Sunday; the early segment numbers
    upward until Ash Wednesday; the late segment numbers backward from
    Christ the King = the 34th, one number per Sunday slot, consuming a
    number wherever a solemnity or a feast of the Lord in the General
    Calendar takes the Sunday (Trinity itself takes the first).  Returns
    (early, late, displaced) — the last one maps the displacing solemnity
    dates to the Sunday numbers they consumed."""
    easter = easter_gregorian(year)
    pentecost = easter + datetime.timedelta(days=49)
    trinity = pentecost + datetime.timedelta(days=7)
    ash = easter - datetime.timedelta(days=46)
    ctk = _advent_first(year) - datetime.timedelta(days=7)
    baptism = _first_sunday_after(datetime.date(year, 1, 6))
    displacers = {  # solemnities / feasts of the Lord, universal dates
        datetime.date(year, 11, 1): "All Saints",
        datetime.date(year, 8, 15): "the Assumption",
        datetime.date(year, 6, 24): "the Birth of Saint John the Baptist",
        datetime.date(year, 8, 6): "the Transfiguration",
        datetime.date(year, 9, 14): "the Exaltation of the Holy Cross",
        datetime.date(year, 2, 2): "the Presentation of the Lord",
        datetime.date(year, 11, 9): "the Dedication of the Lateran Basilica",
    }
    early, n, d = [], 2, baptism + datetime.timedelta(days=7)
    while d < ash:
        early.append((d, n))
        n += 1
        d += datetime.timedelta(days=7)
    late, num, d, displaced = [], 33, ctk - datetime.timedelta(days=7), {}
    while d > trinity:
        if d in displacers:
            displaced[d] = num  # the Sunday gives way (Table n. 59, I.3/II.5)
        else:
            late.append((d, num))
        num -= 1
        d -= datetime.timedelta(days=7)
    late.reverse()
    return early, late, displaced


def set_rc_current():
    """The current Roman calendar (universal dates; national transfers are
    noted, not moved).  Every candidate is a row and the set is resolved so
    one event per date holds: impeded solemnities go to the preceding
    Saturday (Universal Norms n. 5 — the Annunciation, impeded in Holy Week
    or the Easter octave, to the Monday after the Second Sunday of Easter);
    saints' celebrations impeded by a Sunday are omitted that year (n. 60)."""
    rows = []  # (date, name, slug, color, note, kind)

    def add(date, name, slug, color, note="", kind=""):
        rows.append((date, name, slug, color, note, kind))

    for y in YEARS:
        easter = easter_gregorian(y)
        advent = _advent_first(y)
        # Advent + Christmas season
        for i, week in enumerate(["I", "II", "III", "IV"]):
            add(advent + datetime.timedelta(days=7 * i), f"Advent Sunday {week}",
                f"advent-{i + 1}", "purple",
                "The four weeks before Christmas.", "sunday")
        add(datetime.date(y, 12, 8), "Immaculate Conception", "immaculate-conception",
            "white", "A solemnity.", "solemnity")
        add(datetime.date(y, 12, 25), "Christmas (Nativity of the Lord)", "christmas",
            "white", "The octave runs to Jan 1.", "solemnity")
        # Holy Family: the Sunday within the Christmas octave, else Dec 30
        hf = None
        for d in range(26, 32):
            cand = datetime.date(y, 12, d)
            if cand.weekday() == 6:
                hf = cand
                break
        add(hf or datetime.date(y, 12, 30), "Holy Family", "holy-family", "white",
            "", "lord")
        add(datetime.date(y, 1, 1), "Mary, Mother of God", "mary-mother-of-god",
            "white", "A solemnity; the Octave Day of the Nativity.", "solemnity")
        add(datetime.date(y, 1, 6), "Epiphany of the Lord", "epiphany", "white",
            "Jan 6 in the universal calendar; many countries transfer it to the "
            "following Sunday.", "solemnity")
        baptism = _first_sunday_after(datetime.date(y, 1, 6))
        add(baptism, "Baptism of the Lord", "baptism-of-the-lord", "white",
            "The Sunday after Epiphany (the Monday after when Epiphany falls "
            "on Jan 7-8 and is transferred).", "lord")
        add(datetime.date(y, 2, 2), "Presentation of the Lord (Candlemas)", "presentation",
            "white", "A feast.", "lord")
        # Lent and Easter
        add(easter - datetime.timedelta(days=46), "Ash Wednesday", "ash-wednesday",
            "purple", "The beginning of Lent.", "sunday")
        lent1 = easter - datetime.timedelta(days=42)
        for i in range(5):
            add(lent1 + datetime.timedelta(days=7 * i), f"Lent Sunday {i + 1}",
                f"lent-{i + 1}", "purple", "", "sunday")
        add(easter - datetime.timedelta(days=7), "Palm Sunday of the Passion", "palm-sunday",
            "red", "Holy Week begins.", "sunday")
        add(easter - datetime.timedelta(days=3), "Maundy Thursday (Holy Thursday)",
            "maundy-thursday", "white", "The Mass of the Lord's Supper, evening.", "sunday")
        add(easter - datetime.timedelta(days=2), "Good Friday", "good-friday", "red",
            "The Passion of the Lord.", "sunday")
        add(easter - datetime.timedelta(days=1), "Holy Saturday", "holy-saturday", "purple",
            "The Easter Vigil is celebrated after nightfall.", "sunday")
        add(easter, "Easter Sunday", "easter", "white", "The Resurrection; the octave runs "
            "to the Second Sunday.", "sunday")
        add(easter + datetime.timedelta(days=7), "Divine Mercy Sunday (Easter II)",
            "divine-mercy", "white", "", "sunday")
        # The Sundays of Easter (Universal Norms n. 23): II is the octave day
        # (emitted above as Divine Mercy); III..VII follow weekly.
        for i, word in [(3, "Third"), (4, "Fourth"), (5, "Fifth"), (6, "Sixth"),
                        (7, "Seventh")]:
            add(easter + datetime.timedelta(days=7 * (i - 1)),
                f"{word} Sunday of Easter", f"easter-{i}", "white", "", "sunday")
        # Fixed spring solemnities
        add(datetime.date(y, 3, 19), "Saint Joseph", "st-joseph", "white", "A solemnity.",
            "solemnity")
        add(datetime.date(y, 3, 25), "Annunciation of the Lord", "annunciation", "white",
            "A solemnity.", "solemnity")
        add(easter + datetime.timedelta(days=39), "Ascension of the Lord", "ascension",
            "white", "Thursday of the sixth week of Easter in the universal "
            "calendar; many countries move it to the following Sunday.", "solemnity")
        add(easter + datetime.timedelta(days=49), "Pentecost", "pentecost", "red",
            "The descent of the Holy Spirit.", "sunday")
        add(easter + datetime.timedelta(days=56), "Trinity Sunday", "trinity-sunday",
            "white", "", "solemnity")
        add(easter + datetime.timedelta(days=60), "Corpus Christi", "corpus-christi",
            "white", "Thursday; many countries move it to Sunday.", "solemnity")
        add(easter + datetime.timedelta(days=68), "Sacred Heart of Jesus", "sacred-heart",
            "white", "", "solemnity")
        add(datetime.date(y, 6, 24), "Nativity of Saint John the Baptist",
            "st-john-baptist", "white", "A solemnity.", "solemnity")
        add(datetime.date(y, 6, 29), "Saints Peter and Paul", "sts-peter-and-paul",
            "red", "A solemnity.", "solemnity")
        add(datetime.date(y, 5, 31), "Visitation of the Blessed Virgin Mary",
            "visitation", "white", "A feast.", "feast")
        add(datetime.date(y, 8, 6), "Transfiguration of the Lord", "transfiguration",
            "white", "A feast.", "lord")
        add(datetime.date(y, 8, 15), "Assumption of the Blessed Virgin Mary", "assumption",
            "white", "A solemnity.", "solemnity")
        add(datetime.date(y, 9, 14), "Exaltation of the Holy Cross", "holy-cross",
            "red", "A feast.", "lord")
        add(datetime.date(y, 9, 29), "Saints Michael, Gabriel and Raphael", "archangels",
            "white", "A memorial.", "memorial")
        add(datetime.date(y, 10, 18), "Saint Luke, Evangelist", "st-luke",
            "red", "A feast.", "feast")
        add(datetime.date(y, 10, 28), "Saints Simon and Jude, Apostles", "sts-simon-jude",
            "red", "A feast.", "feast")
        add(datetime.date(y, 11, 1), "All Saints", "all-saints", "white",
            "A solemnity.", "solemnity")
        add(datetime.date(y, 11, 2), "All Souls' Day", "all-souls", "purple",
            "Commemoration of all the faithful departed.", "solemnity")
        add(datetime.date(y, 11, 9), "Dedication of the Lateran Basilica", "lateran",
            "white", "A feast.", "lord")
        # Christ the King: the last Sunday before Advent I, always the 34th
        ctk = advent - datetime.timedelta(days=7)
        add(ctk, "Our Lord Jesus Christ, King of the Universe", "christ-the-king",
            "white", "The 34th and last Sunday of Ordinary Time.", "sunday")
        # Ordinary Time: the Baptism is the 1st Sunday (Norms n. 38, 6); the
        # early segment counts upward to Ash Wednesday, the late segment
        # backward from the 34th, consuming numbers where a solemnity or
        # feast of the Lord takes the Sunday.
        early, late, displaced = _rc_current_ordinary(y)
        for d, n in early:
            add(d, f"Ordinary Sunday {n}", f"ot-{n}", "green", "", "otsunday")
        for d, n in late:
            add(d, f"Ordinary Sunday {n}", f"ot-{n}", "green", "", "otsunday")
        # the solemnities that took a Sunday carry the number they consumed
        for i, r in enumerate(rows):
            if r[0].year == y and r[0] in displaced and r[5] in ("solemnity", "lord"):
                n = displaced[r[0]]
                rows[i] = (r[0], r[1], r[2], r[3],
                           ((r[4] + " ") if r[4] else "") +
                           f"Supersedes the {_ordinal(n)} Sunday of Ordinary Time "
                           "this year.", r[5])
    # --- resolve occurrence: one event per date
    events = []
    by_date = {}
    for r in rows:
        by_date.setdefault(r[0], []).append(r)
    transfers = []
    for date, rs in sorted(by_date.items()):
        if len(rs) == 1:
            r = rs[0]
            events.append(Event(r[0], f"{r[1]} ({r[3]})", r[2], r[4]))
            continue
        kinds = [r[5] for r in rs]
        if "solemnity" in kinds and "sunday" in kinds and "otsunday" not in kinds:
            # a solemnity impeded by a Sunday of Advent/Lent/Easter:
            # n. 5 — the preceding Saturday (the Annunciation, impeded in
            # Holy Week or the Easter octave: the Monday after the Second
            # Sunday of Easter)
            sol = next(r for r in rs if r[5] == "solemnity")
            sun = next(r for r in rs if r[5] == "sunday")
            sun_name = sun[1].split(" (")[0]
            if sol[2] == "annunciation":
                target = easter_gregorian(date.year) + datetime.timedelta(days=8)
                note = (f"Transferred from {date.day} {date:%b} ({sun_name}) to the "
                        "Monday after the Second Sunday of Easter.")
            else:
                target = date - datetime.timedelta(days=1)
                note = (f"Transferred from {date.day} {date:%b} ({sun_name}); observed "
                        "on the preceding Saturday per the universal norm "
                        "(General Norms 5); some conferences (e.g. the United "
                        "States) keep it on the following Monday")
                if sol[2] == "immaculate-conception":
                    note += (f", {target.year}'s Monday keeping the obligation "
                             "in the United States")
            note += "."
            transfers.append((target, sol, note))
            events.append(Event(sun[0], f"{sun[1]} ({sun[3]})", sun[2],
                                f"{sol[1]} is transferred to {target.day} {target:%b}."))
            continue
        # otherwise the higher place in the Table of Liturgical Days (n. 59)
        # keeps the day; saints' celebrations give way and are omitted that
        # year (n. 60), noted on the winner
        rank_of = {"sunday": 0, "solemnity": 1, "lord": 2, "otsunday": 3,
                   "feast": 4, "memorial": 5}
        rs_sorted = sorted(rs, key=lambda r: rank_of[r[5]])
        win, losers = rs_sorted[0], rs_sorted[1:]
        note = win[4]
        for loser in losers:
            what = "memorial" if loser[5] == "memorial" else "feast"
            clause = f"The {what} of {loser[1]} is omitted this year (the " \
                     f"{win[1].split(' (')[0]} takes precedence)."
            note = ((note + " ") if note else "") + clause
        events.append(Event(win[0], f"{win[1]} ({win[3]})", win[2], note))
    for target, sol, note in transfers:
        base = sol[4]
        events.append(Event(target, f"{sol[1]} ({sol[3]})", sol[2],
                            ((base + " ") if base else "") + note))
    return events


def _first_sunday_after(d):
    """The first Sunday STRICTLY AFTER d — never d itself.  When Jan 6 falls
    on a Sunday (2030 in range) Holy Family, the Baptism and the Anglican
    Baptism-of-Christ all move to Jan 13, not back onto the Epiphany."""
    d += datetime.timedelta(days=1)
    while d.weekday() != 6:
        d += datetime.timedelta(days=1)
    return d


def _ordinal(n):
    return f"{n}{'th' if 10 <= n % 100 < 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


_WORD_ORDINALS = [
    "First", "Second", "Third", "Fourth", "Fifth", "Sixth", "Seventh", "Eighth",
    "Ninth", "Tenth", "Eleventh", "Twelfth", "Thirteenth", "Fourteenth",
    "Fifteenth", "Sixteenth", "Seventeenth", "Eighteenth", "Nineteenth",
    "Twentieth", "Twenty-First", "Twenty-Second", "Twenty-Third",
    "Twenty-Fourth", "Twenty-Fifth", "Twenty-Sixth", "Twenty-Seventh",
    "Twenty-Eighth", "Twenty-Ninth", "Thirtieth", "Thirty-First",
    "Thirty-Second", "Thirty-Third", "Thirty-Fourth",
]


def _word(n):
    """'The Nth' as CW prints it: 'The Twenty-First Sunday after Trinity'."""
    return _WORD_ORDINALS[n - 1] if 1 <= n <= 34 else _ordinal(n)


# ==========================================================================
# Roman Catholic, 1962 Missal (Extraordinary Form) — the pre-1970 calendar:
# Septuagesima season, Passiontide, ember days, the three octaves, and the
# principal feasts of the temporal and sanctorale.  Ranks (I/II/III class)
# and liturgical colours in the descriptions.
# ==========================================================================

# Fixed days that DISPLACE a II class Sunday of the Pentecost season (and so
# consume its number): I class feasts (Code of Rubrics 1960, table n. 91 —
# rows 3/4/11 outrank the 2nd class Sundays of row 15) and II class feasts
# of the Lord (row 14, per General Rubric n. 16a).  Verified against the
# LMS/FIUV Ordines and the Divinum Officium 1962 Ordo for 2026-2030.
_RC1962_IMPEDING = [
    # (month, day, rank, feast of the Lord?)
    (8, 15, 1, False),   # Assumption
    (6, 24, 1, False),   # Nativity of St John the Baptist
    (7, 1, 1, False),    # Most Precious Blood
    (9, 29, 1, False),   # St Michael (Dedication)
    (11, 1, 1, False),   # All Saints
    (8, 6, 2, True),     # Transfiguration
    (9, 14, 2, True),    # Exaltation of the Holy Cross
    (11, 9, 2, True),    # Dedication of the Lateran
]


def _rc1962_impeded_sunday(date, year):
    """True when a Sunday slot of the Pentecost season is taken by a fixed
    feast of higher rank (Christ the King included — the last Sunday of
    October, General Rubric n. 17d)."""
    if date.month == 10:
        last_oct = datetime.date(year, 10, 31)
        while last_oct.weekday() != 6:
            last_oct -= datetime.timedelta(days=1)
        if date == last_oct:  # Christ the King, I class, on its own Sunday
            return True
    for m, d, rank, lord in _RC1962_IMPEDING:
        if date == datetime.date(year, m, d) and (rank == 1 or lord):
            return True
    return False


def sundays_1962(year):
    """The numbered Sundays of the 1962 calendar for `year`, verified against
    the Divinum Officium 1962 Ordo for every year in range.  Returns
    (sundays, consumed) where

      sundays = {date: (kind, n)} with kind in
        "epiphany"  — kept after Epiphany in January (n = 2..6),
        "pentecost" — after Trinity, numbered forward (n = 2..23; Trinity
                      itself is the 1st, emitted as its own feast),
        "resumed"   — the impeded Epiphany Sundays resumed between the 23rd
                      and the 24th after Pentecost, numbered from the end
                      (the slot nearest the 24th takes the highest number;
                      leftovers are dropped — General Rubric n. 18),
        "last"      — the 24th & Last Sunday after Pentecost, always the
                      last before Advent;

      consumed = {date: (kind, n)} for the Sunday slots whose number an
        impeding feast took instead (Christ the King, All Saints, ...).  The
        superseded Sunday's Mass is what that week's ferias use, and it is
        commemorated in the feast's office.

    Numbering stops at the 23rd; the 24th is always the last Sunday before
    Advent.  (DO 2026-2030 all reproduce exactly.)
    """
    easter = easter_gregorian(year)
    septuagesima = easter - datetime.timedelta(days=63)
    trinity = easter + datetime.timedelta(days=56)  # Pentecost + 7; the 1st
    last = _advent_first(year) - datetime.timedelta(days=7)  # the 24th
    holy_family = _first_sunday_after(datetime.date(year, 1, 6))
    out = {trinity: ("trinity", 1)}
    # January segment: Holy Family is the 1st Sunday after Epiphany (as a
    # feast); the following Sundays number upward until Septuagesima.
    d = holy_family + datetime.timedelta(days=7)
    n = 2
    while d < septuagesima:
        out[d] = ("epiphany", n)
        d += datetime.timedelta(days=7)
        n += 1
    impeded = list(range(n, 7))  # the after-Epiphany numbers unused in January
    # Pentecost season: forward numbering with consumption, 2..23.
    free = []
    consumed = {}
    d = trinity + datetime.timedelta(days=7)
    num = 2
    while d < last:
        if num <= 23:
            if _rc1962_impeded_sunday(d, year):
                consumed[d] = ("pentecost", num)
            else:
                out[d] = ("pentecost", num)
        elif not _rc1962_impeded_sunday(d, year):
            free.append(d)  # between the 23rd's slot and the 24th
        num += 1
        d += datetime.timedelta(days=7)
    for i, dd in enumerate(reversed(free)):
        if i < len(impeded):
            out[dd] = ("resumed", impeded[-1 - i])
    out[last] = ("last", 24)
    return out, consumed


class _LitDay:
    """One candidate celebration for a date, feeding the 1962 occurrence
    resolver: rank = I/II/III class, kind = sunday | feast | octave | ember |
    vigil | feria, and the flags the 1960 Code of Rubrics hangs its
    dispositions on — lord (a feast of the Lord, General Rubric 16a),
    replacement (a feast permanently assigned to a Sunday, n. 17, which
    takes the Sunday's place with all its rights and no commemoration).
    `commem` is how the day names itself inside other days' descriptions."""

    def __init__(self, date, summary, slug, rank, color, note="",
                 kind="feast", lord=False, replacement=False, commem=None):
        self.date = date
        self.summary = summary
        self.slug = slug
        self.rank = rank
        self.color = color
        self.note = note
        self.kind = kind
        self.lord = lord
        self.replacement = replacement
        self.commem = commem if commem is not None else summary

    def row(self):
        """The Tabula of liturgical days (Code of Rubrics n. 91) folded to one
        precedence value per emitted day; lower wins."""
        if self.kind == "sunday":
            return 6 if self.rank == 1 else 15
        if self.kind == "octave":
            return 10 if self.rank == 1 else 17
        if self.kind == "ember":
            return 18
        if self.kind == "vigil":
            if self.rank == 1:
                return 5 if self.slug == "christmas-vigil" else 9
            return 21
        if self.kind == "feria":
            return 7 if self.rank == 1 else 26
        if self.rank == 1:
            if "Immaculate Conception" in self.summary:
                return 4   # General Rubric 15: preferred to the Advent Sunday
            if "All Souls" in self.summary:
                return 8   # n. 91.8: yields to any Sunday occurring on it
            return 11
        return 14 if self.lord else 16

    def event(self):
        parts = [f"{['', 'I', 'II', 'III'][self.rank]} class", self.color, self.note]
        desc = ", ".join(p for p in parts if p)
        if desc:
            desc = desc[0].upper() + desc[1:]
        return Event(self.date, self.summary, self.slug, desc)


def _rc1962_resolve(year, days, consumed):
    """Group the candidates by date and apply the occurrence dispositions of
    the 1960 Code of Rubrics (nn. 14-18, 30, 69, 95-96, 111 + the Tabula of
    n. 91), all verified against the Divinum Officium Ordo for 2026-2030:

      - the higher Tabula row wins;
      - a I class feast impeded by a higher row is TRANSFERRED (n. 95) — the
        Annunciation to the Monday after Low Sunday (n. 96a) — except the
        Immaculate Conception, which beats the Advent Sunday it lands on
        (n. 15);
      - an impeded Sunday is never resumed or anticipated (n. 14): it is
        commemorated on the feast that displaced it, or (for the n. 17
        replacements and II class feasts of the Lord, n. 16a) silently
        replaced;
      - II and III class feasts lose without transference: commemorated
        where the rubrics admit it (n. 111), otherwise omitted that year.

    `consumed` is sundays_1962's map of numbered-Sunday slots taken by
    impeding feasts; their commemoration rides in the winner's note.
    """
    by_date = {}
    for d in days:
        by_date.setdefault(d.date, []).append(d)
    # dates that block a I class transfer (n. 96: "the next day following
    # which is not of the 1st or 2nd class") — every I/II class candidate,
    # plus the II class Advent ferias of 17-23 December (n. 24a), which the
    # set does not otherwise emit
    blockers = {dt for dt, cs in by_date.items() if any(c.rank <= 2 for c in cs)}
    blockers |= {datetime.date(year, 12, dd) for dd in range(17, 24)}

    events, transferred = [], []
    for date, cands in sorted(by_date.items()):
        if len(cands) > 1:
            cands.sort(key=lambda c: (c.row(), 0 if c.replacement else 1))
            win, losers = cands[0], cands[1:]
        else:
            win, losers = cands[0], []
        notes = [win.note] if win.note else []
        for loser in losers:
            if win.slug == "christmas-vigil" and loser.kind == "sunday":
                continue  # n. 30a: replaces the Fourth Sunday of Advent outright
            if loser.kind == "octave" and win.kind == "sunday":
                continue  # n. 69: the Sunday within the octave IS that day
            if loser.rank == 1 and loser.kind == "feast":
                # only I class feasts transfer (n. 95)
                if loser.slug == "annunciation-of-the-blessed-virgin-mary":
                    target = easter_gregorian(year) + datetime.timedelta(days=8)
                else:  # n. 96: the next day free of I and II class
                    target = loser.date + datetime.timedelta(days=1)
                    while target in blockers:
                        target += datetime.timedelta(days=1)
                moved = _LitDay(target, loser.summary, loser.slug, loser.rank,
                                loser.color, loser.note, kind=loser.kind,
                                lord=loser.lord, replacement=loser.replacement,
                                commem=loser.commem)
                moved.note = ((loser.note + " ") if loser.note else "") + \
                    f"Transferred from {loser.date.day} {loser.date:%b} ({win.commem})."
                transferred.append(moved)
                notes.append(f"{loser.commem} is transferred to {target.day} {target:%b}.")
                continue
            # No transference for anything else (n. 14, 95): commemorated
            # where the day admits it, otherwise omitted that year (n. 111).
            if loser.kind == "sunday":
                notes.append(f"{loser.commem} is commemorated.")
            elif loser.kind in ("octave", "ember", "vigil"):
                notes.append(f"Commemorated: {loser.commem}.")
            elif win.rank == 1 or win.kind == "sunday":
                if loser.rank == 2 and win.kind == "sunday":
                    notes.append(f"Commemorated: {loser.commem}.")  # n. 111b
                else:
                    notes.append(f"{loser.commem} is omitted this year.")
            else:
                notes.append(f"Commemorated: {loser.commem}.")  # n. 111c
        if date in consumed and not win.replacement:
            kind, n = consumed[date]
            sname = (f"{_ordinal(n)} Sunday after Pentecost" if kind == "pentecost"
                     else f"{_ordinal(n)} Sunday after Epiphany")
            if win.row() == 14:  # a II class feast of the Lord: no commemoration (n. 16a)
                notes.append(f"The {sname} gives way to this feast of the Lord.")
            else:
                notes.append(f"The {sname} is commemorated.")
        win.note = " ".join(n[0].upper() + n[1:] if n else n for n in notes)
        events.append(win.event())
    events.extend(d.event() for d in transferred)
    return events


def set_rc_1962():
    """The 1962 calendar: every candidate day is built as a _LitDay and run
    through _rc1962_resolve, so exactly one event per date results — the
    occurrence dispositions of the 1960 Code ride in the descriptions as
    commemorations, omissions and transfers."""
    days = []
    TD = datetime.timedelta

    def day(date, summary, slug, rank, color, note="", **kw):
        days.append(_LitDay(date, summary, slug, rank, color, note, **kw))

    for y in YEARS:
        easter = easter_gregorian(y)
        advent = _advent_first(y)
        pentecost = easter + TD(days=49)
        # --- Temporal: Advent through Christmastide
        for i, week in enumerate(["First", "Second", "Third", "Fourth"]):
            day(advent + TD(days=7 * i), f"Advent Sunday {['I', 'II', 'III', 'IV'][i]}",
                f"advent-{i + 1}", 1, "purple", kind="sunday",
                commem=f"The {week} Sunday of Advent")
        for i, d in enumerate(_ember_days(advent + TD(days=14))):
            day(d, f"Ember Day of Advent ({['Wednesday', 'Friday', 'Saturday'][i]})",
                f"ember-advent-{i}", 2, "purple", kind="ember",
                commem="the ember day of Advent")
        christmas_vigil = datetime.date(y, 12, 24)
        day(christmas_vigil, "Vigil of Christmas", "christmas-vigil", 1, "purple",
            "Takes the place of the Fourth Sunday of Advent when it falls "
            "thus (General Rubric 30a)." if christmas_vigil.weekday() == 6 else "",
            kind="vigil")
        day(datetime.date(y, 12, 25), "Christmas (Nativity of the Lord)", "christmas",
            1, "white", "With its octave.")
        for d in range(26, 32):
            cand = datetime.date(y, 12, d)
            if cand.weekday() == 6:
                day(cand, "Sunday within the Octave of Christmas", "christmas-octave-sunday",
                    2, "white", "General Rubric 69.", kind="sunday",
                    commem="the Sunday within the Octave of Christmas")
            day(cand, "Day within the Christmas Octave", f"christmas-octave-{d}",
                2, "white", f"Dec {d}.", kind="octave",
                commem="the day within the Octave of Christmas")
        day(datetime.date(y, 1, 1), "Circumcision of Our Lord", "circumcision",
            1, "white", "The octave day of Christmas.")
        day(datetime.date(y, 1, 6), "Epiphany of Our Lord", "epiphany", 1, "white",
            "With vigil Jan 5 and its octave.")
        # Holy Name: the Sunday between Circumcision and Epiphany (n. 17a);
        # the feast Jan 2 otherwise
        hn = None
        for d in range(2, 6):
            if datetime.date(y, 1, d).weekday() == 6:
                hn = datetime.date(y, 1, d)
                break
        day(hn or datetime.date(y, 1, 2), "Holy Name of Jesus", "holy-name",
            2, "white", kind="feast", lord=True, replacement=True)
        # Holy Family: the First Sunday after Epiphany (n. 17b)
        day(_first_sunday_after(datetime.date(y, 1, 6)), "Holy Family", "holy-family",
            2, "white", lord=True, replacement=True)
        # --- Septuagesima / Lent / Passiontide
        for name, offset in [("Septuagesima", 63), ("Sexagesima", 56), ("Quinquagesima", 49)]:
            day(easter - TD(days=offset), f"{name} Sunday", name.lower(), 2, "purple",
                kind="sunday", commem=f"{name} Sunday")
        lent1 = easter - TD(days=42)
        for i, week in enumerate(["I", "II", "III", "IV"]):
            day(lent1 + TD(days=7 * i), f"Lent Sunday {week}", f"lent-{i + 1}",
                1, "purple", kind="sunday",
                commem=f"The {['First', 'Second', 'Third', 'Fourth'][i]} Sunday of Lent")
        # Ember days: Wednesday, Friday, Saturday of the first week of Lent
        # (n. 24b) — not the pre-1960 Quinquagesima week.
        for i, d in enumerate(_ember_days(lent1)):
            day(d, f"Ember Day of Lent ({['Wednesday', 'Friday', 'Saturday'][i]})",
                f"ember-lent-{i}", 2, "purple", kind="ember",
                commem="the ember day of Lent")
        passion = easter - TD(days=14)
        day(passion, "Passion Sunday (Lent V)", "passion-sunday", 1, "purple",
            "Passiontide begins; images are veiled.", kind="sunday",
            commem="Passion Sunday")
        day(passion + TD(days=7), "Palm Sunday", "palm-sunday", 1, "purple",
            "The blessing of palms and procession.", kind="sunday",
            commem="Palm Sunday")
        day(easter - TD(days=4), "Spy Wednesday", "spy-wednesday", 1, "purple",
            kind="feria")
        day(easter - TD(days=3), "Maundy Thursday", "maundy-thursday", 1, "white",
            "In Coena Domini.", kind="feria")
        day(easter - TD(days=2), "Good Friday", "good-friday", 1, "black",
            "In Parasceve; the Mass of the Presanctified.", kind="feria")
        day(easter - TD(days=1), "Holy Saturday", "holy-saturday", 1, "purple",
            "The Easter Vigil after none.", kind="feria")
        # Easter + privileged octave
        day(easter, "Easter Sunday", "easter", 1, "white", "With its privileged octave.")
        for i in range(1, 7):
            day(easter + TD(days=i), f"Easter Octave, Day {i}", f"easter-octave-{i}",
                1, "white", kind="octave")
        day(easter + TD(days=7), "Low Sunday (Easter Octave)", "low-sunday",
            1, "white", "In Albis.", kind="sunday", commem="Low Sunday")
        # Rogation days: Monday and Tuesday before the Ascension; the
        # Wednesday is the vigil (n. 31a), which carries the rogation Mass.
        ascension = easter + TD(days=39)
        for i in range(2):
            day(ascension - TD(days=3 - i), "Rogation Day", f"rogation-{i}", 3,
                "purple", "The lesser litanies.", kind="feria",
                commem="the rogation day")
        day(ascension - TD(days=1), "Vigil of the Ascension", "ascension-vigil", 2,
            "purple", "The rogation day; the lesser litany procession precedes "
            "the vigil Mass.", kind="vigil", commem="the vigil of the Ascension")
        day(ascension, "Ascension of Our Lord", "ascension", 1, "white", "With its vigil.")
        day(pentecost - TD(days=1), "Vigil of Pentecost", "pentecost-vigil", 1,
            "red", kind="vigil")
        day(pentecost, "Pentecost (Whitsunday)", "pentecost", 1, "red",
            "With its privileged octave.")
        # Pentecost octave: every day is I class (n. 91.10); the Whitsun
        # ember Wednesday/Friday/Saturday (n. 24) fall on days 3, 5 and 6.
        for i in range(1, 7):
            ember = {3: "Wednesday", 5: "Friday", 6: "Saturday"}.get(i)
            day(pentecost + TD(days=i),
                f"Pentecost Octave, Day {i}" + (f" (Ember Day, {ember})" if ember else ""),
                f"pentecost-octave-{i}", 1, "red", kind="octave")
        day(pentecost + TD(days=7), "Trinity Sunday", "trinity-sunday", 1, "white",
            lord=True, replacement=True)  # the octave Sunday of Pentecost (n. 17c)
        corpus = pentecost + TD(days=11)  # Thursday after Trinity
        day(corpus, "Corpus Christi", "corpus-christi", 1, "white", lord=True)
        sacred = easter + TD(days=68)
        day(sacred, "Sacred Heart of Jesus", "sacred-heart", 1, "white", lord=True)
        # Christ the King: the last Sunday of October (n. 17d)
        day(nth_weekday(y, 10, 6, -1), "Our Lord Jesus Christ the King", "christ-the-king",
            1, "white", "The last Sunday of October in the 1962 calendar.",
            lord=True, replacement=True)
        # --- Sanctorale: principal feasts (a curated majors list)
        for (m, d), (name, rank, color, note) in _RC1962_SANCTORALE.items():
            if rank == "skip":
                continue
            day(datetime.date(y, m, d), name, slugify(name),
                {"I": 1, "II": 2, "III": 3}[rank.split()[0]], color, note,
                lord=name in _RC1962_LORDS,
                commem=_RC1962_COMMEM.get(name))
        # --- The numbered Sundays the Ordo prints around the feasts above
        sundays, consumed = sundays_1962(y)
        for sundate, (kind, n) in sorted(sundays.items()):
            if kind == "trinity":
                continue  # already emitted above as Trinity Sunday (the 1st)
            if kind == "epiphany":
                day(sundate, f"{_ordinal(n)} Sunday after Epiphany", f"epiphany-{n}",
                    2, "green", kind="sunday")
            elif kind == "pentecost":
                day(sundate, f"{_ordinal(n)} Sunday after Pentecost", f"pentecost-{n}",
                    2, "green", kind="sunday")
            elif kind == "resumed":
                day(sundate, f"{_ordinal(n)} Sunday after Epiphany (resumed)", f"epiphany-{n}",
                    2, "green", "Impeded by Septuagesima in January; resumed "
                    "between the 23rd and 24th Sundays after Pentecost (General Rubric 18).",
                    kind="sunday")
            else:
                day(sundate, "24th & Last Sunday after Pentecost", "pentecost-24",
                    2, "green", "Always the last Sunday before Advent.", kind="sunday")
    return _rc1962_resolve_years(days)


def _rc1962_resolve_years(days):
    """_rc1962_resolve over the whole window, year by year (the blockers and
    the consumed slots are per-year data)."""
    events = []
    for y in YEARS:
        ydays = [d for d in days if d.date.year == y]
        _, consumed = sundays_1962(y)
        events.extend(_rc1962_resolve(y, ydays, consumed))
    return events


def _ember_days(first_sunday_week):
    """Wednesday, Friday and Saturday of the week containing/after the given
    Sunday.  `first_sunday_week` is a date inside that week (usually the
    Sunday itself or a day of it)."""
    d = first_sunday_week
    while d.weekday() != 2:  # advance to the following Wednesday
        d += datetime.timedelta(days=1)
    return [d, d + datetime.timedelta(days=2), d + datetime.timedelta(days=3)]


def slugify(name):
    s = name.lower()
    for ch in ",.()'’":
        s = s.replace(ch, "")
    s = s.replace(" ", "-").replace("--", "-")
    return s[:48]


_RC1962_SANCTORALE = {
    # January
    (1, 13): ("Baptism of Our Lord", "II class", "white",
              "Fixed on the day after the Epiphany octave; the Baptism in the Jordan."),
    (1, 21): ("St Agnes, Virgin and Martyr", "III class", "red", ""),
    # February
    (2, 2): ("Purification of the BVM (Candlemas)", "II class", "white",
             "The blessing of candles and procession."),
    (2, 3): ("St Blaise, Bishop and Martyr", "III class", "red", "Blessing of throats."),
    (2, 24): ("St Matthias, Apostle", "II class", "red",
              "The 1962 date (not the medieval May 14)."),
    # March
    (3, 7): ("SS Perpetua and Felicity, Martyrs", "III class", "red", ""),
    (3, 17): ("St Patrick, Bishop and Confessor", "III class", "white", ""),
    (3, 19): ("St Joseph, Spouse of the BVM", "I class", "white",
              "Patron of the Universal Church."),
    (3, 25): ("Annunciation of the Blessed Virgin Mary", "I class", "white", ""),
    # April
    (4, 23): ("St George, Martyr", "III class", "red", ""),
    (4, 25): ("St Mark, Evangelist", "II class", "red", ""),
    # May
    (5, 1): ("St Joseph the Worker", "I class", "white",
             "The 1955 feast; May 1 is not SS Philip and James in 1962."),
    (5, 3): ("Finding of the Holy Cross", "II class", "red", ""),
    (5, 11): ("SS Philip and James, Apostles", "II class", "red",
              "The 1955 date (moved off May 1)."),
    # June
    (6, 13): ("St Anthony of Padua, Confessor and Doctor", "III class", "white", ""),
    (6, 24): ("Nativity of St John the Baptist", "I class", "white", "With vigil."),
    (6, 29): ("SS Peter and Paul, Apostles", "I class", "red", "With vigil and octave."),
    # July
    (7, 1): ("Most Precious Blood", "I class", "red", ""),
    (7, 2): ("Visitation of the Blessed Virgin Mary", "II class", "white", ""),
    (7, 22): ("St Mary Magdalene, Penitent", "III class", "white", ""),
    (7, 26): ("St Anne, Mother of the BVM", "II class", "white", ""),
    # August
    (8, 6): ("Transfiguration of Our Lord", "II class", "white", ""),
    (8, 10): ("St Lawrence, Martyr", "II class", "red", ""),
    (8, 15): ("Assumption of the Blessed Virgin Mary", "I class", "white", "With octave."),
    (8, 22): ("Immaculate Heart of the BVM", "II class", "white", ""),
    (8, 24): ("St Bartholomew, Apostle", "II class", "red", ""),
    # September
    (9, 8): ("Nativity of the Blessed Virgin Mary", "II class", "white", ""),
    (9, 14): ("Exaltation of the Holy Cross", "II class", "red", ""),
    (9, 21): ("St Matthew, Apostle and Evangelist", "II class", "red", ""),
    (9, 29): ("Dedication of St Michael the Archangel", "I class", "white", ""),
    # October
    (10, 18): ("St Luke, Evangelist", "II class", "red", ""),
    (10, 28): ("SS Simon and Jude, Apostles", "II class", "red", ""),
    # November
    (11, 1): ("All Saints", "I class", "white", "With vigil."),
    (11, 2): ("All Souls", "I class", "black", "The commemoration of all the faithful "
              "departed; three Masses."),
    (11, 9): ("Dedication of the Archbasilica of Our Saviour", "II class", "white",
              "The Lateran basilica, the mother church."),
    (11, 11): ("St Martin of Tours, Bishop and Confessor", "III class", "white", ""),
    (11, 21): ("Presentation of the Blessed Virgin Mary", "III class", "white", ""),
    (11, 30): ("St Andrew, Apostle", "II class", "red", ""),
    # December
    (12, 6): ("St Nicholas, Bishop and Confessor", "III class", "white", ""),
    (12, 8): ("Immaculate Conception", "I class", "white",
              "Patroness of the United States."),
    (12, 13): ("St Lucy, Virgin and Martyr", "III class", "red", ""),
    (12, 21): ("St Thomas, Apostle", "II class", "red", ""),
    (12, 26): ("St Stephen, First Martyr", "II class", "red", ""),
    (12, 27): ("St John, Apostle and Evangelist", "II class", "white", ""),
    (12, 28): ("Holy Innocents", "II class", "red", ""),
}

# The sanctorale's feasts of the Lord (General Rubric 16a: a I or II class
# feast of the Lord takes the place of a II class Sunday outright).
_RC1962_LORDS = {
    "Baptism of Our Lord",
    "Purification of the BVM (Candlemas)",
    "Transfiguration of Our Lord",
    "Exaltation of the Holy Cross",
    "Dedication of the Archbasilica of Our Saviour",
}

# How some days name themselves inside other days' descriptions.
_RC1962_COMMEM = {
    "Baptism of Our Lord": "the Baptism of Our Lord",
}


# ==========================================================================
# Orthodox (Julian reckoning, converted to civil dates; the New Calendar
# alternative is named in every description).  The Great Feasts, the fasting
# seasons' bounds and Holy Week.
# ==========================================================================

ORTHODOX_NOTE = ("Old (Julian) Calendar observance; churches on the New Calendar "
                 "keep fixed feasts 13 days earlier.")


def set_orthodox():
    events = []
    lo, hi = datetime.date(YEARS[0], 1, 1), datetime.date(YEARS[-1], 12, 31)

    def add(date, name, slug, note=""):
        if date and lo <= date <= hi:
            events.append(Event(date, name, slug, ORTHODOX_NOTE + " " + note if note else ORTHODOX_NOTE))

    for y in YEARS:
        # A fixed Julian feast can land in civil year y from its own y or from
        # y-1 (Julian December crosses into the next civil year) — emit both
        # candidates and let the window filter trim.
        def add_julian(m, d, name, slug, note=""):
            for base in (y - 1, y):
                add(datetime.date(base, m, d) + datetime.timedelta(days=13), name, slug, note)
        pascha = easter_julian(y)
        # Fasting seasons
        add(pascha - datetime.timedelta(days=70), "Triodion begins", "triodion",
            "The preparatory season before Great Lent (the Sunday of the "
            "Publican and the Pharisee).")
        add(pascha - datetime.timedelta(days=48), "Great Lent begins (Clean Monday)",
            "great-lent", "The Great Fast.")
        # The named Sundays of Great Lent (the five before Palm Sunday;
        # titles verified against azbyka.ru / the Triodion):
        for off, name, slug in [
            (42, "Sunday of Orthodoxy (1st Sunday of Great Lent)", "lent-1-orthodoxy"),
            (35, "St Gregory Palamas (2nd Sunday of Great Lent)", "lent-2-palamas"),
            (28, "Veneration of the Cross (3rd Sunday of Great Lent)", "lent-3-cross"),
            (21, "St John Climacus (4th Sunday of Great Lent)", "lent-4-climacus"),
            (14, "St Mary of Egypt (5th Sunday of Great Lent)", "lent-5-mary-of-egypt"),
        ]:
            add(pascha - datetime.timedelta(days=off), name, slug)
        add(pascha - datetime.timedelta(days=8), "Lazarus Saturday", "lazarus-saturday")
        add(pascha - datetime.timedelta(days=7), "Palm Sunday", "palm-sunday")
        add(pascha - datetime.timedelta(days=2), "Great and Holy Friday", "holy-friday",
            "The burial of Christ.")
        add(pascha - datetime.timedelta(days=1), "Great and Holy Saturday", "holy-saturday")
        add(pascha, "Holy Pascha (Easter)", "pascha")
        # The Sundays after Pascha (the Pentecostarion; titles per the
        # Pentecostarion and azbyka.ru — the Fathers of Nicaea are the 7th)
        for off, name, slug in [
            (7, "Antipascha (St Thomas Sunday)", "thomas-antipascha"),
            (14, "Myrrhbearers Sunday (2nd after Pascha)", "myrrhbearers"),
            (21, "Paralytic Sunday (3rd after Pascha)", "paralytic"),
            (28, "Samaritan Sunday (4th after Pascha)", "samaritan"),
            (35, "Blind Man Sunday (5th after Pascha)", "blind-man"),
            (42, "Fathers of Nicaea (7th Sunday after Pascha)", "nicaea-fathers"),
        ]:
            add(pascha + datetime.timedelta(days=off), name, slug)
        add(pascha + datetime.timedelta(days=39), "Ascension of the Lord", "ascension")
        add(pascha + datetime.timedelta(days=49), "Pentecost", "pentecost")
        add(pascha + datetime.timedelta(days=56), "All Saints Sunday", "all-saints",
            "The Sunday after Pentecost (the 1st after it); the Apostles' Fast "
            "begins the next day.")
        # Apostles' Fast: the Monday after All Saints to Jun 29 Julian (Jul 12)
        all_saints = pascha + datetime.timedelta(days=56)
        add(all_saints + datetime.timedelta(days=1), "Apostles' Fast begins", "apostles-fast",
            "Runs to Jul 12 civil (Jun 29 Julian).")
        # Dormition Fast: Aug 1-14 Julian
        add(datetime.date(y, 8, 14), "Dormition Fast begins", "dormition-fast",
            "Runs to the Dormition, Aug 28 civil (Aug 15 Julian).")
        # Nativity Fast: Nov 15 Julian
        add(datetime.date(y, 11, 28), "Nativity Fast begins", "nativity-fast",
            "Runs to Christmas, Jan 7 civil (Dec 25 Julian).")
        # Fixed Great Feasts (Julian → civil)
        add_julian(9, 8, "Nativity of the Theotokos", "theotokos-nativity")
        add_julian(9, 14, "Exaltation of the Holy Cross", "cross")
        add_julian(11, 21, "Entry of the Theotokos into the Temple", "theotokos-entry")
        add_julian(12, 25, "Nativity of Christ (Christmas)", "christmas",
                   "The second-greatest feast.")
        add_julian(1, 6, "Theophany (Epiphany) of the Lord", "theophany")
        add_julian(2, 2, "Meeting of the Lord (Presentation)", "meeting")
        add_julian(3, 25, "Annunciation of the Theotokos", "annunciation")
        add_julian(8, 6, "Transfiguration of the Lord", "transfiguration")
        add_julian(8, 15, "Dormition of the Theotokos", "dormition")
        # --- The Sundays after Pentecost, numbered as the Slavic Ordo prints
        # them (azbyka.ru, verified 2026-2030): All Saints is the 1st, and the
        # numbering runs every Sunday across the year boundary until the next
        # Triodion's Zacchaeus Sunday (Pascha − 77) inclusive.  A fixed Great
        # Feast falling on a numbered Sunday is kept instead of it, the Sunday
        # noted in the feast's description.
        great_feasts = {datetime.date(y, m, d) + datetime.timedelta(days=13)
                        for m, d in [(9, 8), (9, 14), (11, 21), (12, 25),
                                     (1, 6), (2, 2), (3, 25), (8, 6), (8, 15)]}
        great_feasts |= {datetime.date(y - 1, m, d) + datetime.timedelta(days=13)
                         for m, d in [(12, 25), (1, 6)]}
        nativity_next = datetime.date(y + 1, 1, 7)  # this season's Christmas
        if nativity_next.weekday() == 6:  # Christmas on a Sunday: no Fathers'
            forefathers = nativity_next - datetime.timedelta(days=14)
        else:
            fathers = nativity_next - datetime.timedelta(
                days=(nativity_next.weekday() + 1) % 7 or 7)
            forefathers = fathers - datetime.timedelta(days=7)
        d = all_saints + datetime.timedelta(days=7)  # the 2nd
        while d <= datetime.date(y, 12, 31):
            n = (d - all_saints).days // 7 + 1
            if d not in great_feasts:
                if d == forefathers:
                    add(d, "Sunday of the Holy Forefathers", "forefathers",
                        f"The {_ordinal(n)} Sunday after Pentecost; the second "
                        "Sunday before the Nativity.")
                elif d == fathers:
                    add(d, "Sunday before the Nativity (Holy Fathers)",
                        "nativity-fathers",
                        f"The {_ordinal(n)} Sunday after Pentecost; the Sunday "
                        "before the Nativity.")
                else:
                    add(d, f"{_ordinal(n)} Sunday after Pentecost", f"pentecost-{n}")
            else:  # the Great Feast keeps the day; note the Sunday on it
                for e in events:
                    if e.dtstart == d and e.summary in (
                            "Exaltation of the Holy Cross", "Transfiguration of the Lord",
                            "Dormition of the Theotokos", "Nativity of the Theotokos",
                            "Entry of the Theotokos into the Temple"):
                        e.description += f" The {_ordinal(n)} Sunday after " \
                                         "Pentecost is noted in its office."
            d += datetime.timedelta(days=7)
        # January/February tail: these Sundays still belong to the previous
        # season's count (All Saints of y-1), numbered through Zacchaeus
        # (Pascha − 77) inclusive; no numbering exists after Zacchaeus until
        # the next All Saints.
        all_saints_prev = easter_julian(y - 1) + datetime.timedelta(days=56)
        zacchaeus = pascha - datetime.timedelta(days=77)
        nativity_now = datetime.date(y, 1, 7)
        fathers_now = None if nativity_now.weekday() == 6 else nativity_now - \
            datetime.timedelta(days=(nativity_now.weekday() + 1) % 7 or 7)
        d = datetime.date(y, 1, 1)
        while d.weekday() != 6:
            d += datetime.timedelta(days=1)
        while d <= zacchaeus:
            n = (d - all_saints_prev).days // 7 + 1
            if d == nativity_now:  # Christmas itself (numbered this year only)
                for e in events:
                    if e.dtstart == d and e.summary == "Nativity of Christ (Christmas)":
                        e.description += (f" Christmas falls on the "
                                          f"{_ordinal(n)} Sunday after Pentecost "
                                          "this year.")
            elif d == fathers_now:
                add(d, "Sunday before the Nativity (Holy Fathers)", "nativity-fathers",
                    f"The {_ordinal(n)} Sunday after Pentecost; the Sunday "
                    "before the Nativity.")
            elif d not in great_feasts:
                add(d, f"{_ordinal(n)} Sunday after Pentecost", f"pentecost-{n}")
            d += datetime.timedelta(days=7)
    return events


# ==========================================================================
# Anglican (Book of Common Prayer): principal feasts, Holy Days and the
# Sundays named by the prayer book, on the Gregorian reckoning.
# ==========================================================================

def set_anglican():
    """The Church of England's calendar as shaped by Common Worship (2000):
    the Sundays as CW names them (the Baptism of Christ opens Epiphany; the
    Sundays before Lent run backward from the Sunday next before Lent; the
    last numbered Sunday after Trinity is 'The Last Sunday after Trinity';
    the year closes with the Sundays before Advent and Christ the King),
    plus the Rules' short precedence list.  One event per date throughout.
    CW names verified against churchofengland.org (Rules to Order the
    Christian Year) and published per-year Sunday lists."""
    TD = datetime.timedelta
    rows = []  # (date, name, slug, note, kind)

    def add(date, name, slug, note="", kind="feast"):
        rows.append((date, name, slug, note, kind))

    def sunday_after(d):
        return _first_sunday_after(d)

    for y in YEARS:
        easter = easter_gregorian(y)
        advent = _advent_first(y)
        ash = easter - TD(days=46)
        epiphany = datetime.date(y, 1, 6)
        # --- Advent
        for i, word in enumerate(["First", "Second", "Third", "Fourth"]):
            add(advent + TD(days=7 * i), f"The {word} Sunday of Advent",
                f"advent-{i + 1}", "The Church Year begins at Advent.", "closed")
        add(datetime.date(y, 12, 25), "Christmas Day", "christmas",
            "A Principal Feast.", "principal")
        add(sunday_after(datetime.date(y, 12, 25)), "The First Sunday of Christmas",
            "christmas-1", "", "ordinary")
        # --- Epiphany season: the Baptism of Christ is the First Sunday of
        # Epiphany — the Second when 6 January is itself a Sunday (CW Rule).
        # The remaining Sundays before Ash Wednesday split: the first up to
        # three (two when the Epiphany is a Sunday) are the Sundays of
        # Epiphany; the rest are the Sundays before Lent, titled backward
        # from the Sunday next before Lent.
        add(epiphany, "The Epiphany", "epiphany", "A Principal Feast.", "principal")
        baptism = sunday_after(epiphany)
        if epiphany.weekday() == 6:
            add(baptism, "The Baptism of Christ", "baptism-of-christ",
                "Kept on the Second Sunday of Epiphany, 6 January being a "
                "Sunday (CW Rules).", "ordinary")
        else:
            add(baptism, "The Baptism of Christ", "baptism-of-christ",
                "The First Sunday of Epiphany.", "ordinary")
        ash = easter - TD(days=46)
        slots = []
        d = baptism + TD(days=7)
        while d < ash:
            slots.append(d)
            d += TD(days=7)
        n_epiphany = 3 if epiphany.weekday() != 6 else 2
        start = 2 if epiphany.weekday() != 6 else 3
        for n, d in enumerate(slots[:n_epiphany], start=start):
            add(d, f"The {['Second', 'Third', 'Fourth'][n - 2]} Sunday of Epiphany",
                f"epiphany-{n}", "", "ordinary")
        for i, d in enumerate(reversed(slots[n_epiphany:])):
            title = "The Sunday next before Lent" if i == 0 else \
                f"The {_word(i + 1)} Sunday before Lent"
            add(d, title, f"before-lent-{i + 1}", "", "ordinary")
        add(datetime.date(y, 2, 2), "The Presentation of Christ in the Temple "
            "(Candlemas)", "presentation",
            "A Holy Day; CW also allows the Sunday between 28 January and "
            "3 February.", "feast")
        # --- Lent and Holy Week
        add(ash, "Ash Wednesday", "ash-wednesday", "Lent begins.", "principal")
        for i in range(5):
            add(ash + TD(days=4 + 7 * i), f"The {_word(i + 1)} Sunday of Lent",
                f"lent-{i + 1}", "", "closed")
        add(easter - TD(days=7), "The Sunday of the Passion (Palm Sunday)",
            "palm-sunday", "Holy Week begins.", "closed")
        add(easter - TD(days=3), "Maundy Thursday", "maundy-thursday",
            "The institution of the Eucharist.", "principal")
        add(easter - TD(days=2), "Good Friday", "good-friday",
            "The Passion of the Lord.", "principal")
        add(easter, "Easter Day", "easter",
            "The Queen of Feasts; the Great Fifty Days follow.", "principal")
        # --- Eastertide: the Sundays of Easter, Ascension, Pentecost
        for i in range(2, 8):
            add(easter + TD(days=7 * (i - 1)), f"The {_word(i)} Sunday of Easter",
                f"easter-{i}", "", "closed")
        add(easter + TD(days=39), "Ascension Day", "ascension",
            "A Principal Feast, kept on a Thursday.", "principal")
        add(easter + TD(days=49), "The Day of Pentecost (Whitsunday)",
            "pentecost", "A Principal Feast.", "principal")
        trinity = easter + TD(days=56)
        add(trinity, "Trinity Sunday", "trinity-sunday", "A Principal Feast.",
            "principal")
        # --- The Sundays after Trinity: numbered from the First after
        # Trinity; the last one is 'The Last Sunday after Trinity', which
        # stands before the Sunday kept as All Saints' Sunday (or, when
        # 1 November is a Sunday, before All Saints' Day itself)
        all_saints = datetime.date(y, 11, 1)
        ass_sunday = all_saints if all_saints.weekday() == 6 else \
            all_saints + TD(days=(6 - all_saints.weekday()) % 7 or 7)
        # the CW window is 30 Oct - 5 Nov; when Nov 1 is a weekday the
        # following Sunday falls in the window unless Nov 1 is Monday/Tuesday
        if all_saints.weekday() in (0, 1):  # Monday/Tuesday: the Sunday BEFORE
            ass_sunday = all_saints - TD(days=all_saints.weekday() + 1)
        d, n = trinity + TD(days=7), 1
        while d < ass_sunday - TD(days=7):
            add(d, f"The {_word(n)} Sunday after Trinity", f"trinity-{n}", "", "ordinary")
            d += TD(days=7)
            n += 1
        add(ass_sunday - TD(days=7), "The Last Sunday after Trinity",
            "trinity-last", "", "ordinary")
        add(all_saints, "All Saints' Day", "all-saints", "A Principal Feast.",
            "principal")
        if all_saints.weekday() != 6:
            add(ass_sunday, "All Saints' Sunday", "all-saints-sunday",
                "The Sunday between 30 October and 5 November (CW).", "ordinary")
        # --- The year closes: the Sundays before Advent and Christ the King
        for n, back in [(3, 21), (2, 14)]:
            add(advent - TD(days=back), f"The {_word(n)} Sunday before Advent",
                f"before-advent-{n}", "", "ordinary")
        add(advent - TD(days=7), "Christ the King (The Sunday next before Advent)",
            "christ-the-king", "The last Sunday of the year.", "principal")
        # --- Festivals
        add(datetime.date(y, 5, 31), "The Visitation", "visitation", "A Festival.")
        add(datetime.date(y, 6, 24), "The Nativity of Saint John the Baptist",
            "st-john-baptist", "A Festival.")
        add(datetime.date(y, 6, 29), "Saint Peter and Saint Paul",
            "sts-peter-and-paul", "A Festival.")
        add(datetime.date(y, 8, 15), "The Blessed Virgin Mary", "st-mary",
            "A Festival (CW also allows 8 September for pastoral reasons).")
        add(datetime.date(y, 9, 29), "Saint Michael and All Angels", "michaelmas",
            "A Festival.")
    # --- Resolve the CW Rules: a Festival coinciding with a Principal Feast
    # or Principal Holy Day, or falling on a Sunday in Advent, Lent or
    # Eastertide, transfers to the first available day (the Monday); on any
    # other Sunday it is always to be observed, kept on the day at the
    # Rules' discretion, with the Sunday observed alongside it.
    events = []
    by_date = {}
    for r in rows:
        by_date.setdefault(r[0], []).append(r)
    for date, rs in sorted(by_date.items()):
        if len(rs) == 1:
            r = rs[0]
            events.append(Event(r[0], r[1], r[2], r[3]))
            continue
        feasts = [r for r in rs if r[4] == "feast"]
        others = [r for r in rs if r[4] != "feast"]
        if len(feasts) != 1 or len(others) != 1:
            raise SystemExit(f"anglican resolver: unexpected collision on {date}: {rs}")
        f, o = feasts[0], others[0]
        if o[4] in ("principal", "closed"):
            target = date + TD(days=1)
            events.append(Event(o[0], o[1], o[2], o[3]))
            note = f"Transferred from {date.day} {date:%b} ({o[1]}; CW Rules)."
            events.append(Event(target, f[1], f[2],
                                (f[3] + " " if f[3] else "") + note))
        else:
            note = (f[3] + " " if f[3] else "") + \
                f"Falls on {o[1]}; the Sunday is always observed alongside " \
                "it, and the celebration may instead be moved to the " \
                "Monday following (CW Rules)."
            events.append(Event(f[0], f[1], f[2], note))
    return events


# ==========================================================================
# Self-test + emission
# ==========================================================================

# The 1962 numbered Sundays for every year in range — the full outcome
# tables as printed by the Divinum Officium 1962 Ordo (2026 additionally
# verified against the LMS and FIUV Ordines).  Gaps in the Pentecost run
# are slots consumed by a higher-ranked day (Christ the King, All Saints,
# Assumption, St John Baptist, Precious Blood, St Michael, Transfiguration).
_RC1962_ANCHORS = {
    2026: [
        (1, 18, "epiphany", 2), (1, 25, "epiphany", 3),
        (6, 7, "pentecost", 2), (6, 14, "pentecost", 3), (6, 21, "pentecost", 4),
        (6, 28, "pentecost", 5), (7, 5, "pentecost", 6), (7, 12, "pentecost", 7),
        (7, 19, "pentecost", 8), (7, 26, "pentecost", 9), (8, 2, "pentecost", 10),
        (8, 9, "pentecost", 11), (8, 16, "pentecost", 12), (8, 23, "pentecost", 13),
        (8, 30, "pentecost", 14), (9, 6, "pentecost", 15), (9, 13, "pentecost", 16),
        (9, 20, "pentecost", 17), (9, 27, "pentecost", 18), (10, 4, "pentecost", 19),
        (10, 11, "pentecost", 20), (10, 18, "pentecost", 21),
        (11, 8, "resumed", 5), (11, 15, "resumed", 6), (11, 22, "last", 24),
    ],
    2027: [
        (1, 17, "epiphany", 2),
        (5, 30, "pentecost", 2), (6, 6, "pentecost", 3), (6, 13, "pentecost", 4),
        (6, 20, "pentecost", 5), (6, 27, "pentecost", 6), (7, 4, "pentecost", 7),
        (7, 11, "pentecost", 8), (7, 18, "pentecost", 9), (7, 25, "pentecost", 10),
        (8, 1, "pentecost", 11), (8, 8, "pentecost", 12),
        (8, 22, "pentecost", 14), (8, 29, "pentecost", 15),
        (9, 5, "pentecost", 16), (9, 12, "pentecost", 17), (9, 19, "pentecost", 18),
        (9, 26, "pentecost", 19), (10, 3, "pentecost", 20), (10, 10, "pentecost", 21),
        (10, 17, "pentecost", 22), (10, 24, "pentecost", 23),
        (11, 7, "resumed", 5), (11, 14, "resumed", 6), (11, 21, "last", 24),
    ],
    2028: [
        (1, 16, "epiphany", 2), (1, 23, "epiphany", 3), (1, 30, "epiphany", 4),
        (2, 6, "epiphany", 5),
        (6, 18, "pentecost", 2), (6, 25, "pentecost", 3), (7, 2, "pentecost", 4),
        (7, 9, "pentecost", 5), (7, 16, "pentecost", 6), (7, 23, "pentecost", 7),
        (7, 30, "pentecost", 8), (8, 13, "pentecost", 10), (8, 20, "pentecost", 11),
        (8, 27, "pentecost", 12), (9, 3, "pentecost", 13), (9, 10, "pentecost", 14),
        (9, 17, "pentecost", 15), (9, 24, "pentecost", 16), (10, 1, "pentecost", 17),
        (10, 8, "pentecost", 18), (10, 15, "pentecost", 19), (10, 22, "pentecost", 20),
        (11, 5, "pentecost", 22), (11, 12, "pentecost", 23),
        (11, 19, "resumed", 6), (11, 26, "last", 24),
    ],
    2029: [
        (1, 14, "epiphany", 2), (1, 21, "epiphany", 3),
        (6, 3, "pentecost", 2), (6, 10, "pentecost", 3), (6, 17, "pentecost", 4),
        (7, 8, "pentecost", 7), (7, 15, "pentecost", 8), (7, 22, "pentecost", 9),
        (7, 29, "pentecost", 10), (8, 5, "pentecost", 11), (8, 12, "pentecost", 12),
        (8, 19, "pentecost", 13), (8, 26, "pentecost", 14), (9, 2, "pentecost", 15),
        (9, 9, "pentecost", 16), (9, 16, "pentecost", 17), (9, 23, "pentecost", 18),
        (9, 30, "pentecost", 19), (10, 7, "pentecost", 20), (10, 14, "pentecost", 21),
        (10, 21, "pentecost", 22),
        (11, 4, "resumed", 4), (11, 11, "resumed", 5), (11, 18, "resumed", 6),
        (11, 25, "last", 24),
    ],
    2030: [
        (1, 20, "epiphany", 2), (1, 27, "epiphany", 3), (2, 3, "epiphany", 4),
        (2, 10, "epiphany", 5),
        (6, 23, "pentecost", 2), (6, 30, "pentecost", 3), (7, 7, "pentecost", 4),
        (7, 14, "pentecost", 5), (7, 21, "pentecost", 6), (7, 28, "pentecost", 7),
        (8, 4, "pentecost", 8), (8, 11, "pentecost", 9), (8, 18, "pentecost", 10),
        (8, 25, "pentecost", 11), (9, 1, "pentecost", 12), (9, 8, "pentecost", 13),
        (9, 15, "pentecost", 14), (9, 22, "pentecost", 15),
        (10, 6, "pentecost", 17), (10, 13, "pentecost", 18), (10, 20, "pentecost", 19),
        (11, 3, "pentecost", 21), (11, 10, "pentecost", 22), (11, 17, "pentecost", 23),
        (11, 24, "last", 24),
    ],
}


MANIFEST = """\
# Generated by scripts/generate-holiday-ics.py — do not edit by hand.
# Regenerate: python3 scripts/generate-holiday-ics.py

[[groups]]
id = "countries"
label = "Countries"

[[groups.sets]]
id = "us"
label = "United States (federal)"
file = "us.ics"

[[groups.sets]]
id = "canada"
label = "Canada (national statutory)"
file = "canada.ics"

[[groups.sets]]
id = "uk"
label = "United Kingdom (bank holidays)"
file = "uk.ics"

[[groups.sets]]
id = "china"
label = "China (public festivals)"
file = "china.ics"

[[groups]]
id = "christianity"
label = "Christianity"

[[groups.sets]]
id = "rc-current"
label = "Roman Catholic (current calendar)"
file = "rc-current.ics"

[[groups.sets]]
id = "rc-1962"
label = "Roman Catholic (1962 Missal)"
file = "rc-1962.ics"

[[groups.sets]]
id = "orthodox"
label = "Orthodox (Julian calendar)"
file = "orthodox.ics"

[[groups.sets]]
id = "anglican"
label = "Anglican (Common Worship)"
file = "anglican.ics"

[[groups]]
id = "islam"
label = "Islam"

[[groups.sets]]
id = "islam"
label = "Islamic observances (tabular Hijri)"
file = "islam.ics"

[[groups]]
id = "judaism"
label = "Judaism"

[[groups.sets]]
id = "judaism"
label = "Jewish holidays (full cycle)"
file = "judaism.ics"

[[groups]]
id = "hinduism"
label = "Hinduism"

[[groups.sets]]
id = "hindu"
label = "Hindu festivals"
file = "hindu.ics"

[[groups]]
id = "buddhism"
label = "Buddhism"

[[groups.sets]]
id = "buddhist"
label = "Buddhist observances"
file = "buddhist.ics"

[[groups]]
id = "sikhism"
label = "Sikhism"

[[groups.sets]]
id = "sikh"
label = "Sikh festivals"
file = "sikh.ics"
"""


def self_test():
    """Refuse to emit if any published anchor fails to reproduce."""
    checks = [
        ("Easter (west) 2026", easter_gregorian(2026), datetime.date(2026, 4, 5)),
        ("Easter (west) 2029", easter_gregorian(2029), datetime.date(2029, 4, 1)),
        ("Orthodox Easter 2026", easter_julian(2026), datetime.date(2026, 4, 12)),
        ("Orthodox Easter 2027", easter_julian(2027), datetime.date(2027, 5, 2)),
        ("RH 5786", rosh_hashanah(5786), datetime.date(2025, 9, 23)),
        ("RH 5787", rosh_hashanah(5787), datetime.date(2026, 9, 12)),
        ("5 Iyar 5708", hebrew_date(5708, "Iyar", 5), datetime.date(1948, 5, 14)),
        ("10 Tishri 5734", hebrew_date(5734, "Tishri", 10), datetime.date(1973, 10, 6)),
        ("15 Nisan 5786", hebrew_date(5786, "Nisan", 15), datetime.date(2026, 4, 2)),
        ("15 Nisan 5787", hebrew_date(5787, "Nisan", 15), datetime.date(2027, 4, 22)),
        ("J2000 JD", _jd_to_date(2451545.0), datetime.date(2000, 1, 1)),
        ("Spring Festival 2026", chinese_new_year(2026), datetime.date(2026, 2, 17)),
        ("Spring Festival 2027", chinese_new_year(2027), datetime.date(2027, 2, 6)),
        ("Spring Festival 2028", chinese_new_year(2028), datetime.date(2028, 1, 26)),
        ("Spring Festival 2029", chinese_new_year(2029), datetime.date(2029, 2, 13)),
        ("Spring Festival 2030", chinese_new_year(2030), datetime.date(2030, 2, 3)),
        ("Eid al-Adha 1446", islamic_to_civil(1446, 12, 10), datetime.date(2026, 5, 27)),
        ("1 Muharram 1447", islamic_to_civil(1447, 1, 1), datetime.date(2026, 6, 16)),
        ("Ashura 1447", islamic_to_civil(1447, 1, 10), datetime.date(2026, 6, 25)),
        # Panchang anchors — published festival dates this machinery must
        # keep reproducing (the adhika/ksaya fixes hang off these).
        ("Makar Sankranti 2026", makar_sankranti(2026), datetime.date(2026, 1, 14)),
        ("Vaisakhi 2026", vaisakhi(2026), datetime.date(2026, 4, 14)),
        ("Holi 2026", holi(2026), datetime.date(2026, 3, 4)),
        ("Magha Purnima 2026", magha_puja(2026), datetime.date(2026, 2, 1)),
        ("Vesak 2026", vesak(2026), datetime.date(2026, 5, 1)),
        ("Janmashtami 2026", janmashtami(2026), datetime.date(2026, 9, 4)),
        ("Ganesh Chaturthi 2026", ganesh_chaturthi(2026), datetime.date(2026, 8, 16)),
        ("Dussehra 2026", vijayadashami(2026), datetime.date(2026, 10, 21)),
        ("Diwali 2026 (amavasya at sunrise)", diwali(2026), datetime.date(2026, 11, 9)),
        ("Guru Nanak Jayanti 2026", guru_nanak(2026), datetime.date(2026, 11, 24)),
    ]
    for name, got, want in checks:
        if got != want:
            raise SystemExit(f"SELF-TEST FAILED: {name}: computed {got}, published {want}")
    # rc-1962 numbered Sundays — the full Divinum-Officium-verified outcome
    # tables for every year in range (see _RC1962_ANCHORS).
    for year, rows in _RC1962_ANCHORS.items():
        computed, _consumed = sundays_1962(year)
        want = {datetime.date(year, m, d): (kind, n) for m, d, kind, n in rows}
        got = {d: k for d, k in computed.items() if k[0] != "trinity"}
        if got != want:
            bad = {str(k): (got.get(k), want.get(k))
                   for k in set(got) | set(want) if got.get(k) != want.get(k)}
            raise SystemExit(f"SELF-TEST FAILED: 1962 Sundays {year}: {bad}")
        trinity = easter_gregorian(year) + datetime.timedelta(days=56)
        if computed.get(trinity) != ("trinity", 1):
            raise SystemExit(f"SELF-TEST FAILED: 1962 Trinity slot {year}")
    if _first_sunday_after(datetime.date(2030, 1, 6)) != datetime.date(2030, 1, 13):
        raise SystemExit("SELF-TEST FAILED: Epiphany-Sunday 2030 must push "
                         "Holy Family to Jan 13")
    # rc-1962 occurrence dispositions — every vector verified against the
    # Divinum Officium 1962 Ordo (2026 additionally the LMS/FIUV Ordines):
    # (date, the summary that must own the day, a substring its description
    # must carry).  A None summary requires the date to carry NO event.
    rc62 = {}
    for e in set_rc_1962():
        if e.dtstart in rc62:
            raise SystemExit(f"SELF-TEST FAILED: two events on {e.dtstart} in rc-1962")
        rc62[e.dtstart] = e
    for date, want_summary, want_desc in [
        ("20261018", "21st Sunday after Pentecost", "Commemorated: St Luke, Evangelist."),
        ("20261101", "All Saints", "The 23rd Sunday after Pentecost is commemorated."),
        ("20270815", "Assumption of the Blessed Virgin Mary",
         "The 13th Sunday after Pentecost is commemorated."),
        ("20290624", "Nativity of St John the Baptist",
         "The 5th Sunday after Pentecost is commemorated."),
        ("20290701", "Most Precious Blood",
         "The 6th Sunday after Pentecost is commemorated."),
        ("20300929", "Dedication of St Michael the Archangel",
         "The 16th Sunday after Pentecost is commemorated."),
        ("20291028", "Our Lord Jesus Christ the King",
         "SS Simon and Jude, Apostles is omitted this year"),
        ("20280806", "Transfiguration of Our Lord",
         "The 9th Sunday after Pentecost gives way to this feast of the Lord."),
        ("20301208", "Immaculate Conception",
         "The Second Sunday of Advent is commemorated."),
        ("20301209", None, None),  # the IC is kept on its Sunday: no Monday copy
        ("20280320", "St Joseph, Spouse of the BVM", "Transferred from 19 Mar"),
        ("20280319", "Lent Sunday III", "transferred to 20 Mar"),
        ("20290409", "Annunciation of the Blessed Virgin Mary",
         "Transferred from 25 Mar (Palm Sunday)"),
        ("20270405", "Annunciation of the Blessed Virgin Mary",
         "Transferred from 25 Mar (Maundy Thursday)"),
        ("20300113", "Holy Family", "Commemorated: the Baptism of Our Lord."),
        ("20300224", "Sexagesima Sunday", "Commemorated: St Matthias, Apostle."),
        ("20280702", "4th Sunday after Pentecost",
         "Commemorated: Visitation of the Blessed Virgin Mary."),
        ("20280423", "Low Sunday (Easter Octave)",
         "St George, Martyr is omitted this year."),
        ("20281224", "Vigil of Christmas", "Fourth Sunday of Advent"),
        ("20281231", "Sunday within the Octave of Christmas", None),
        ("20291230", "Sunday within the Octave of Christmas", None),
        ("20261227", "Sunday within the Octave of Christmas",
         "Commemorated: St John, Apostle and Evangelist."),
        ("20271226", "Sunday within the Octave of Christmas",
         "Commemorated: St Stephen, First Martyr."),
        ("20260530", "Pentecost Octave, Day 6 (Ember Day, Saturday)", None),
        ("20291221", "St Thomas, Apostle", "Commemorated: the ember day of Advent."),
    ]:
        d = datetime.date(int(date[:4]), int(date[4:6]), int(date[6:]))
        e = rc62.get(d)
        if want_summary is None:
            if e is not None:
                raise SystemExit(f"SELF-TEST FAILED: {d} must carry no rc-1962 event")
            continue
        if e is None:
            raise SystemExit(f"SELF-TEST FAILED: rc-1962 {d} has no event")
        if e.summary != want_summary:
            raise SystemExit(f"SELF-TEST FAILED: rc-1962 {d} is '{e.summary}', "
                             f"expected '{want_summary}'")
        if want_desc and want_desc not in e.description:
            raise SystemExit(f"SELF-TEST FAILED: rc-1962 {d} '{e.summary}' description "
                             f"lacks '{want_desc}': {e.description}")
    # rc-current — the Ordinary Time numbering (the Baptism counts as the
    # 1st Sunday; the late segment numbers backward from Christ the King =
    # the 34th) and the occurrence vectors, verified against romcal's
    # 2026-2030 calendars (the universal calendar: romcal's US rendering
    # also moves Epiphany, Ascension and Corpus Christi to Sundays, which
    # this set notes on the fixed dates instead).
    rccur = {}
    for e in set_rc_current():
        if e.dtstart in rccur:
            raise SystemExit(f"SELF-TEST FAILED: two events on {e.dtstart} in rc-current")
        rccur[e.dtstart] = e
    for date, want_summary, want_desc in [
        ("20260118", "Ordinary Sunday 2 (green)", None),
        ("20260215", "Ordinary Sunday 6 (green)", None),
        ("20260419", "Third Sunday of Easter (white)", None),
        ("20260503", "Fifth Sunday of Easter (white)", None),
        ("20260517", "Seventh Sunday of Easter (white)", None),
        ("20260607", "Ordinary Sunday 10 (green)", None),
        ("20261011", "Ordinary Sunday 28 (green)", None),
        ("20261018", "Ordinary Sunday 29 (green)",
         "Saint Luke, Evangelist is omitted this year"),
        ("20261101", "All Saints (white)", "Supersedes the 31st Sunday"),
        ("20261108", "Ordinary Sunday 32 (green)", None),
        ("20261122", "Our Lord Jesus Christ, King of the Universe (white)", "34th"),
        ("20270815", "Assumption of the Blessed Virgin Mary (white)",
         "Supersedes the 20th Sunday"),
        ("20280318", "Saint Joseph (white)", "Transferred from 19 Mar"),
        ("20280319", "Lent Sunday 3 (purple)", "transferred to 18 Mar"),
        ("20280806", "Transfiguration of the Lord (white)",
         "Supersedes the 18th Sunday"),
        ("20290409", "Annunciation of the Lord (white)",
         "Monday after the Second Sunday of Easter"),
        ("20290624", "Nativity of Saint John the Baptist (white)",
         "Supersedes the 12th Sunday"),
        ("20291028", "Ordinary Sunday 30 (green)",
         "Saints Simon and Jude, Apostles is omitted this year"),
        ("20291224", None, None),  # the Annunciation has left Mar 25 free
        ("20301207", "Immaculate Conception (white)", "Transferred from 8 Dec"),
        ("20301208", "Advent Sunday II (purple)", "transferred to 7 Dec"),
    ]:
        d = datetime.date(int(date[:4]), int(date[4:6]), int(date[6:]))
        e = rccur.get(d)
        if want_summary is None:
            if e is not None:
                raise SystemExit(f"SELF-TEST FAILED: {d} must carry no rc-current event")
            continue
        if e is None:
            raise SystemExit(f"SELF-TEST FAILED: rc-current {d} has no event")
        if e.summary != want_summary:
            raise SystemExit(f"SELF-TEST FAILED: rc-current {d} is '{e.summary}', "
                             f"expected '{want_summary}'")
        if want_desc and want_desc not in e.description:
            raise SystemExit(f"SELF-TEST FAILED: rc-current {d} '{e.summary}' "
                             f"description lacks '{want_desc}': {e.description}")
    # anglican (Common Worship) — the Sunday names, the All Saints' Sunday
    # window and the Rules' precedence, verified against the Church of
    # England's Rules to Order the Christian Year and published per-year
    # Sunday lists (7 June 2026 is 'The First Sunday after Trinity').
    ang = {}
    for e in set_anglican():
        if e.dtstart in ang:
            raise SystemExit(f"SELF-TEST FAILED: two events on {e.dtstart} in anglican")
        ang[e.dtstart] = e
    for date, want_summary, want_desc in [
        ("20260607", "The First Sunday after Trinity", None),
        ("20261025", "The Last Sunday after Trinity", None),
        ("20260208", "The Second Sunday before Lent", None),
        ("20260215", "The Sunday next before Lent", None),
        ("20261101", "All Saints' Day", None),
        ("20261108", "The Third Sunday before Advent", None),
        ("20261122", "Christ the King (The Sunday next before Advent)", None),
        ("20271031", "All Saints' Sunday", None),
        ("20281105", "All Saints' Sunday", None),
        ("20281231", "The First Sunday of Christmas", None),
        ("20291104", "All Saints' Sunday", None),
        ("20301103", "All Saints' Sunday", None),
        ("20300113", "The Baptism of Christ",
         "Second Sunday of Epiphany"),
        ("20300120", "The Third Sunday of Epiphany", None),
        ("20300203", "The Fifth Sunday before Lent", None),
        ("20300303", "The Sunday next before Lent", None),
        ("20260412", "The Second Sunday of Easter", None),
        ("20260517", "The Seventh Sunday of Easter", None),
        ("20260601", "The Visitation", "Transferred from 31 May"),
        ("20260531", "Trinity Sunday", None),
        ("20270815", "The Blessed Virgin Mary", "Falls on The Twelfth Sunday after Trinity"),
        ("20300929", "Saint Michael and All Angels",
         "Falls on The Fifteenth Sunday after Trinity"),
    ]:
        d = datetime.date(int(date[:4]), int(date[4:6]), int(date[6:]))
        e = ang.get(d)
        if want_summary is None:
            if e is not None:
                raise SystemExit(f"SELF-TEST FAILED: {d} must carry no anglican event")
            continue
        if e is None:
            raise SystemExit(f"SELF-TEST FAILED: anglican {d} has no event")
        if e.summary != want_summary:
            raise SystemExit(f"SELF-TEST FAILED: anglican {d} is '{e.summary}', "
                             f"expected '{want_summary}'")
        if want_desc and want_desc not in e.description:
            raise SystemExit(f"SELF-TEST FAILED: anglican {d} '{e.summary}' "
                             f"description lacks '{want_desc}': {e.description}")
    # orthodox — the Pascha-cycle names and the Slavic numbering of the
    # Sundays after Pentecost (All Saints = the 1st, numbered continuously
    # across the year boundary until the next Triodion's Zacchaeus Sunday,
    # the December Nativity-cycle Sundays carrying their names alongside),
    # verified against azbyka.ru's calendar for every vector:
    orth = {}
    for e in set_orthodox():
        orth.setdefault(e.dtstart, []).append(e)
    for date, want_summary, want_desc in [
        ("20260301", "Sunday of Orthodoxy (1st Sunday of Great Lent)", None),
        ("20260329", "St Mary of Egypt (5th Sunday of Great Lent)", None),
        ("20260419", "Antipascha (St Thomas Sunday)", None),
        ("20260524", "Fathers of Nicaea (7th Sunday after Pascha)", None),
        ("20260614", "2nd Sunday after Pentecost", None),
        ("20260927", "Exaltation of the Holy Cross",
         "The 17th Sunday after Pentecost is noted in its office."),
        ("20261227", "Sunday of the Holy Forefathers",
         "The 30th Sunday after Pentecost"),
        ("20270103", "Sunday before the Nativity (Holy Fathers)",
         "The 31st Sunday after Pentecost"),
        ("20270110", "32nd Sunday after Pentecost", None),
        ("20270207", "36th Sunday after Pentecost", None),
        ("20270214", "37th Sunday after Pentecost", None),
        ("20270221", "Triodion begins", None),
        ("20281224", "Sunday of the Holy Forefathers",
         "The 29th Sunday after Pentecost"),
        ("20281231", "30th Sunday after Pentecost", None),
        ("20290107", "Nativity of Christ (Christmas)",
         "Christmas falls on the 31st Sunday after Pentecost"),
        ("20290819", "Transfiguration of the Lord",
         "The 12th Sunday after Pentecost is noted in its office."),
        ("20291230", "Sunday of the Holy Forefathers",
         "The 31st Sunday after Pentecost"),
        ("20300106", "Sunday before the Nativity (Holy Fathers)",
         "The 32nd Sunday after Pentecost"),
        ("20300428", "Holy Pascha (Easter)", None),
    ]:
        d = datetime.date(int(date[:4]), int(date[4:6]), int(date[6:]))
        es = orth.get(d, [])
        e = next((x for x in es if x.summary == want_summary), None)
        if e is None:
            raise SystemExit(f"SELF-TEST FAILED: orthodox {d} lacks "
                             f"'{want_summary}' (has {[x.summary for x in es]})")
        if want_desc and want_desc not in e.description:
            raise SystemExit(f"SELF-TEST FAILED: orthodox {d} '{e.summary}' "
                             f"description lacks '{want_desc}': {e.description}")
    # Hebrew structural sweep
    for y in range(5700, 5861):
        if hebrew_year_length(y) not in (353, 354, 355, 383, 384, 385):
            raise SystemExit(f"SELF-TEST FAILED: Hebrew year {y} length {hebrew_year_length(y)}")
        if rosh_hashanah(y).weekday() in (6, 2, 4):
            raise SystemExit(f"SELF-TEST FAILED: RH {y} on a forbidden weekday")
    print("self-test: all anchors reproduce")


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    self_test()
    sets = [
        ("us", set_us), ("canada", set_canada), ("uk", set_uk), ("china", set_china),
        ("rc-current", set_rc_current), ("rc-1962", set_rc_1962),
        ("orthodox", set_orthodox), ("anglican", set_anglican),
        ("islam", set_islam), ("judaism", set_judaism),
        ("hindu", set_hindu), ("buddhist", set_buddhist), ("sikh", set_sikh),
    ]
    print(f"writing holiday sets for {YEARS[0]}-{YEARS[-1]} into {os.path.abspath(OUT_DIR)}:")
    total = 0
    for set_id, fn in sets:
        n = write_set(set_id, fn())
        total += n
    with open(os.path.join(OUT_DIR, "holidays.toml"), "w", encoding="utf-8") as fh:
        fh.write(MANIFEST)
    print(f"  holidays.toml: {len(sets)} sets in 7 groups")
    print(f"  total events: {total}")


if __name__ == "__main__":
    main()
