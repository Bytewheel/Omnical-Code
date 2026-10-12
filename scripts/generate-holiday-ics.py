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


def set_rc_current():
    events = []

    def add(date, name, slug, color, note=""):
        events.append(Event(date, f"{name} ({color})", slug, note))

    for y in YEARS:
        easter = easter_gregorian(y)
        advent = _advent_first(y)
        # Advent + Christmas season
        for i, week in enumerate(["I", "II", "III", "IV"]):
            add(advent + datetime.timedelta(days=7 * i), f"Advent Sunday {week}",
                f"advent-{i + 1}", "purple",
                "The four weeks before Christmas.")
        add(datetime.date(y, 12, 8), "Immaculate Conception", "immaculate-conception",
            "white", "A solemnity.")
        add(datetime.date(y, 12, 25), "Christmas (Nativity of the Lord)", "christmas",
            "white", "The octave runs to Jan 1.")
        # Holy Family: the Sunday within the Christmas octave, else Dec 30
        hf = None
        for d in range(26, 32):
            cand = datetime.date(y, 12, d)
            if cand.weekday() == 6:
                hf = cand
                break
        add(hf or datetime.date(y, 12, 30), "Holy Family", "holy-family", "white")

        # Following year's early season (emitted for year y+1 dates within y+1's loop)
        add(datetime.date(y, 1, 1), "Mary, Mother of God", "mary-mother-of-god",
            "white", "A solemnity; the Octave Day of the Nativity.")
        add(datetime.date(y, 1, 6), "Epiphany of the Lord", "epiphany", "white",
            "Jan 6 in the universal calendar; many countries transfer it to the "
            "following Sunday.")
        baptism = _first_sunday_after(datetime.date(y, 1, 6))
        add(baptism, "Baptism of the Lord", "baptism-of-the-lord", "white",
            "The Sunday after Epiphany (the Monday after when Epiphany falls "
            "on Jan 7-8 and is transferred).")
        add(datetime.date(y, 2, 2), "Presentation of the Lord (Candlemas)", "presentation",
            "white", "A feast.")
        # Lent and Easter
        add(easter - datetime.timedelta(days=46), "Ash Wednesday", "ash-wednesday",
            "purple", "The beginning of Lent.")
        lent1 = easter - datetime.timedelta(days=42)
        for i in range(5):
            add(lent1 + datetime.timedelta(days=7 * i), f"Lent Sunday {i + 1}",
                f"lent-{i + 1}", "purple")
        add(easter - datetime.timedelta(days=7), "Palm Sunday of the Passion", "palm-sunday",
            "red", "Holy Week begins.")
        add(easter - datetime.timedelta(days=3), "Maundy Thursday (Holy Thursday)",
            "maundy-thursday", "white", "The Mass of the Lord's Supper, evening.")
        add(easter - datetime.timedelta(days=2), "Good Friday", "good-friday", "red",
            "The Passion of the Lord.")
        add(easter - datetime.timedelta(days=1), "Holy Saturday", "holy-saturday", "purple",
            "The Easter Vigil is celebrated after nightfall.")
        add(easter, "Easter Sunday", "easter", "white", "The Resurrection; the octave runs "
            "to the Second Sunday.")
        add(easter + datetime.timedelta(days=7), "Divine Mercy Sunday (Easter II)",
            "divine-mercy", "white")
        # Fixed spring feasts
        add(datetime.date(y, 3, 19), "Saint Joseph", "st-joseph", "white", "A solemnity.")
        add(datetime.date(y, 3, 25), "Annunciation of the Lord", "annunciation", "white",
            "A solemnity.")
        add(easter + datetime.timedelta(days=39), "Ascension of the Lord", "ascension",
            "white", "Thursday of the sixth week of Easter in the universal "
            "calendar; many countries move it to the following Sunday.")
        add(easter + datetime.timedelta(days=49), "Pentecost", "pentecost", "red",
            "The descent of the Holy Spirit.")
        add(easter + datetime.timedelta(days=56), "Trinity Sunday", "trinity-sunday", "white")
        add(easter + datetime.timedelta(days=60), "Corpus Christi", "corpus-christi",
            "white", "Thursday; many countries move it to Sunday.")
        add(easter + datetime.timedelta(days=68), "Sacred Heart of Jesus", "sacred-heart",
            "white")
        add(datetime.date(y, 6, 24), "Nativity of Saint John the Baptist",
            "st-john-baptist", "white", "A solemnity.")
        add(datetime.date(y, 6, 29), "Saints Peter and Paul", "sts-peter-and-paul",
            "red", "A solemnity.")
        add(datetime.date(y, 8, 6), "Transfiguration of the Lord", "transfiguration",
            "white", "A feast.")
        add(datetime.date(y, 8, 15), "Assumption of the Blessed Virgin Mary", "assumption",
            "white", "A solemnity.")
        add(datetime.date(y, 9, 14), "Exaltation of the Holy Cross", "holy-cross",
            "red", "A feast.")
        add(datetime.date(y, 9, 29), "Saints Michael, Gabriel and Raphael", "archangels",
            "white", "A feast.")
        add(datetime.date(y, 11, 1), "All Saints", "all-saints", "white", "A solemnity.")
        add(datetime.date(y, 11, 2), "All Souls' Day", "all-souls", "purple",
            "Commemoration of all the faithful departed.")
        add(datetime.date(y, 11, 9), "Dedication of the Lateran Basilica", "lateran",
            "white", "A feast.")
        # Christ the King: the last Sunday before Advent I
        ctk = advent - datetime.timedelta(days=7)
        add(ctk, "Our Lord Jesus Christ, King of the Universe", "christ-the-king",
            "white", "The 34th and last Sunday of Ordinary Time.")
        # Ordinary Sundays, numbered per the Roman books: count backwards
        # from Christ the King, and number the post-baptism/pre-Lent Sundays
        # from 1.
        ot = 34
        d = ctk - datetime.timedelta(days=7)
        while d > easter + datetime.timedelta(days=60):  # after Corpus Christi
            add(d, f"Ordinary Sunday {ot}", f"ot-{ot}", "green")
            ot -= 1
            d -= datetime.timedelta(days=7)
        # early Ordinary Time: after Baptism until Septuagesima (OF has none —
        # Lent starts at Ash Wednesday); count from 1 until the Sunday before Ash
        n = 1
        d = baptism + datetime.timedelta(days=7)
        while d < easter - datetime.timedelta(days=42):
            add(d, f"Ordinary Sunday {n}", f"ot-early-{n}", "green")
            n += 1
            d += datetime.timedelta(days=7)
    return events


def _first_sunday_after(d):
    while d.weekday() != 6:
        d += datetime.timedelta(days=1)
    return d


# ==========================================================================
# Roman Catholic, 1962 Missal (Extraordinary Form) — the pre-1970 calendar:
# Septuagesima season, Passiontide, ember days, the three octaves, and the
# principal feasts of the temporal and sanctorale.  Ranks (I/II/III class)
# and liturgical colours in the descriptions.
# ==========================================================================

def set_rc_1962():
    events = []

    def add(date, name, slug, rank="", color="", note=""):
        desc = ", ".join(p for p in [rank, color, note] if p)
        if desc:
            desc = desc[0].upper() + desc[1:]
        events.append(Event(date, name, slug, desc))

    for y in YEARS:
        easter = easter_gregorian(y)
        advent = _advent_first(y)
        # --- Temporal: Advent through Christmastide
        for i, week in enumerate(["I", "II", "III", "IV"]):
            add(advent + datetime.timedelta(days=7 * i), f"Advent Sunday {week}",
                f"advent-{i + 1}", "I class", "purple")
        ember_dec = _ember_days(advent + datetime.timedelta(days=14))
        for i, d in enumerate(ember_dec):
            add(d, f"Ember Day of Advent ({['Wednesday', 'Friday', 'Saturday'][i]})",
                f"ember-advent-{i}", "III class", "purple")
        add(datetime.date(y, 12, 8), "Immaculate Conception", "immaculate-conception",
            "I class", "white", "Patroness of the United States.")
        add(datetime.date(y, 12, 25), "Christmas (Nativity of the Lord)", "christmas",
            "I class", "white", "With its octave.")
        for d in range(26, 32):
            add(datetime.date(y, 12, d), "Day within the Christmas Octave", f"christmas-octave-{d}",
                "II class", "white", f"Dec {d}.")
        # Holy Innocents etc. handled in the sanctorale below.
        add(datetime.date(y, 1, 1), "Circumcision of Our Lord", "circumcision",
            "I class", "white", "The octave day of Christmas.")
        add(datetime.date(y, 1, 6), "Epiphany of Our Lord", "epiphany", "I class", "white",
            "With vigil Jan 5 and its octave.")
        # Holy Name: Sunday within Epiphany's octave (2 Jan-5 Jan per missal: the
        # Sunday between Circumcision and Epiphany; the feast Jan 2 otherwise)
        hn = None
        for d in range(2, 6):
            cand = datetime.date(y, 1, d)
            if cand.weekday() == 6:
                hn = cand
                break
        add(hn or datetime.date(y, 1, 2), "Holy Name of Jesus", "holy-name",
            "II class", "white")
        # Holy Family: first Sunday after Epiphany
        add(_first_sunday_after(datetime.date(y, 1, 6)), "Holy Family", "holy-family",
            "II class", "white")
        # --- Septuagesima / Lent / Passiontide
        septuagesima = easter - datetime.timedelta(days=63)
        for name, offset in [("Septuagesima", 63), ("Sexagesima", 56), ("Quinquagesima", 49)]:
            add(easter - datetime.timedelta(days=offset), f"{name} Sunday",
                f"{name.lower()}", "II class", "purple")
        ash = easter - datetime.timedelta(days=46)
        add(ash - datetime.timedelta(days=3), "Quinquagesima Ember Wednesday", "ember-lent-0",
            "III class", "purple")
        add(ash - datetime.timedelta(days=1), "Quinquagesima Ember Saturday", "ember-lent-1",
            "III class", "purple")
        lent1 = easter - datetime.timedelta(days=42)
        for i, week in enumerate(["I", "II", "III", "IV"]):
            add(lent1 + datetime.timedelta(days=7 * i), f"Lent Sunday {week}",
                f"lent-{i + 1}", "I class", "purple")
        # First Sunday's ember week (Mon/Tue/Wed... actually Lent ember = week of
        # the first Sunday of Lent: Wed, Fri, Sat)
        ember_lent = _ember_days(lent1)
        for i, d in enumerate(ember_lent):
            add(d, f"Ember Day of Lent ({['Wednesday', 'Friday', 'Saturday'][i]})",
                f"ember-lent-{i}", "III class", "purple")
        # Passion Sunday = Lent V; Passiontide begins
        passion = easter - datetime.timedelta(days=14)
        add(passion, "Passion Sunday (Lent V)", "passion-sunday", "I class", "purple",
            "Passiontide begins; images are veiled.")
        add(passion + datetime.timedelta(days=7), "Palm Sunday", "palm-sunday",
            "I class", "purple", "The blessing of palms and procession.")
        add(easter - datetime.timedelta(days=4), "Spy Wednesday", "spy-wednesday",
            "II class", "purple")
        add(easter - datetime.timedelta(days=3), "Maundy Thursday", "maundy-thursday",
            "I class", "white", "In Coena Domini.")
        add(easter - datetime.timedelta(days=2), "Good Friday", "good-friday",
            "I class", "black", "In Parasceve; the Mass of the Presanctified.")
        add(easter - datetime.timedelta(days=1), "Holy Saturday", "holy-saturday",
            "I class", "purple", "The Easter Vigil after none.")
        # Easter + octave
        add(easter, "Easter Sunday", "easter", "I class", "white",
            "With its privileged octave.")
        for i in range(1, 7):
            add(easter + datetime.timedelta(days=i), f"Easter Octave, Day {i}",
                f"easter-octave-{i}", "I class", "white")
        add(easter + datetime.timedelta(days=7), "Low Sunday (Easter Octave)", "low-sunday",
            "I class", "white", "In Albis.")
        # Rogation Days: the three before Ascension
        ascension = easter + datetime.timedelta(days=39)
        for i in range(3):
            add(ascension - datetime.timedelta(days=3 - i),
                f"Rogation Day ({'Lesser' if i < 2 else 'Greater Litanies'})" if i == 2
                else "Rogation Day", f"rogation-{i}", "III class", "purple")
        add(ascension, "Ascension of Our Lord", "ascension", "I class", "white",
            "With its vigil.")
        pentecost = easter + datetime.timedelta(days=49)
        add(pentecost - datetime.timedelta(days=1), "Vigil of Pentecost", "pentecost-vigil",
            "I class", "red")
        add(pentecost, "Pentecost (Whitsunday)", "pentecost", "I class", "red",
            "With its privileged octave.")
        for i in range(1, 6):
            add(pentecost + datetime.timedelta(days=i), f"Pentecost Octave, Day {i}",
                f"pentecost-octave-{i}", "I class", "red")
        ember_pent = _ember_days(pentecost + datetime.timedelta(days=1))
        for i, d in enumerate(ember_pent):
            add(d, f"Ember Day of Pentecost ({['Wednesday', 'Friday', 'Saturday'][i]})",
                f"ember-pentecost-{i}", "III class", "red")
        # Whitsun week ember days: Wed/Fri/Sat after Pentecost — _ember_days uses
        # the week containing its argument's Wednesday; see helper.
        add(pentecost + datetime.timedelta(days=7), "Trinity Sunday", "trinity-sunday",
            "I class", "white")
        corpus = pentecost + datetime.timedelta(days=11)  # Thursday after Trinity
        add(corpus, "Corpus Christi", "corpus-christi", "I class", "white",
            "With its octave.")
        add(corpus + datetime.timedelta(days=7), "Corpus Christi Octave", "corpus-octave",
            "II class", "white")
        sacred = easter + datetime.timedelta(days=68)
        add(sacred, "Sacred Heart of Jesus", "sacred-heart", "II class", "white")
        # Christ the King: last Sunday of October
        add(nth_weekday(y, 10, 6, -1), "Our Lord Jesus Christ the King", "christ-the-king",
            "I class", "white", "The last Sunday of October in the 1962 calendar.")
        # --- Sanctorale: principal feasts (a curated majors list)
        for (m, d), (name, rank, color, note) in _RC1962_SANCTORALE.items():
            if rank == "skip":
                continue
            add(datetime.date(y, m, d), name, slugify(name), rank, color, note)
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
    (1, 21): ("St Agnes, Virgin and Martyr", "III class", "red", ""),
    (1, 28): ("St John of the Cross? (not 1962 — removed)", "skip", "skip", ""),
    (2, 3): ("St Blaise, Bishop and Martyr", "III class", "red", "Blessing of throats."),
    (3, 7): ("SS Perpetua and Felicity, Martyrs", "III class", "red", ""),
    (3, 17): ("St Patrick, Bishop and Confessor", "III class", "white", ""),
    (3, 23): ("St Joseph? (transferred from Mar 19 when in Holy Week)", "skip", "skip", ""),
    (3, 25): ("Annunciation of the Blessed Virgin Mary", "I class", "white", ""),
    (4, 23): ("St George, Martyr", "III class", "red", ""),
    (4, 25): ("St Mark, Evangelist", "II class", "red", ""),
    (5, 1): ("SS Philip and James, Apostles", "II class", "red", ""),
    (5, 3): ("Finding of the Holy Cross", "II class", "red", ""),
    (6, 13): ("St Anthony of Padua, Confessor and Doctor", "III class", "white", ""),
    (6, 24): ("Nativity of St John the Baptist", "II class", "white", "With vigil."),
    (6, 29): ("SS Peter and Paul, Apostles", "I class", "red", "With vigil and octave."),
    (7, 2): ("Visitation of the Blessed Virgin Mary", "II class", "white", ""),
    (7, 22): ("St Mary Magdalene, Penitent", "III class", "white", ""),
    (7, 26): ("St Anne, Mother of the BVM", "II class", "white", ""),
    (8, 10): ("St Lawrence, Martyr", "II class", "red", ""),
    (8, 15): ("Assumption of the Blessed Virgin Mary", "I class", "white", "With octave."),
    (8, 22): ("Immaculate Heart of the BVM", "II class", "white", ""),
    (9, 8): ("Nativity of the Blessed Virgin Mary", "II class", "white", ""),
    (9, 21): ("St Matthew, Apostle and Evangelist", "II class", "red", ""),
    (9, 29): ("Dedication of St Michael the Archangel", "I class", "white", ""),
    (10, 18): ("St Luke, Evangelist", "II class", "red", ""),
    (10, 28): ("SS Simon and Jude, Apostles", "II class", "red", ""),
    (11, 1): ("All Saints", "I class", "white", "With vigil."),
    (11, 2): ("All Souls", "I class", "black", "The commemoration of all the faithful "
              "departed; three Masses."),
    (11, 9): ("Dedication of the Archbasilica of Our Saviour", "III class", "white", ""),
    (11, 11): ("St Martin of Tours, Bishop and Confessor", "III class", "white", ""),
    (11, 21): ("Presentation of the Blessed Virgin Mary", "III class", "white", ""),
    (11, 30): ("St Andrew, Apostle", "II class", "red", ""),
    (12, 6): ("St Nicholas, Bishop and Confessor", "III class", "white", ""),
    (12, 8): ("Immaculate Conception", "I class", "white", ""),
    (12, 13): ("St Lucy, Virgin and Martyr", "III class", "red", ""),
    (12, 21): ("St Thomas, Apostle", "II class", "red", ""),
    (12, 26): ("St Stephen, First Martyr", "II class", "red", ""),
    (12, 27): ("St John, Apostle and Evangelist", "II class", "white", ""),
    (12, 28): ("Holy Innocents", "II class", "red", ""),
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
            "The preparatory season before Great Lent.")
        add(pascha - datetime.timedelta(days=48), "Great Lent begins (Clean Monday)",
            "great-lent", "The Great Fast.")
        add(pascha - datetime.timedelta(days=8), "Lazarus Saturday", "lazarus-saturday")
        add(pascha - datetime.timedelta(days=7), "Palm Sunday", "palm-sunday")
        add(pascha - datetime.timedelta(days=2), "Great and Holy Friday", "holy-friday",
            "The burial of Christ.")
        add(pascha - datetime.timedelta(days=1), "Great and Holy Saturday", "holy-saturday")
        add(pascha, "Holy Pascha (Easter)", "pascha")
        add(pascha + datetime.timedelta(days=39), "Ascension of the Lord", "ascension")
        add(pascha + datetime.timedelta(days=49), "Pentecost", "pentecost")
        add(pascha + datetime.timedelta(days=56), "All Saints Sunday", "all-saints",
            "The Sunday after Pentecost; the Apostles' Fast begins the next day.")
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
    return events


# ==========================================================================
# Anglican (Book of Common Prayer): principal feasts, Holy Days and the
# Sundays named by the prayer book, on the Gregorian reckoning.
# ==========================================================================

def set_anglican():
    events = []

    def add(date, name, slug, note=""):
        events.append(Event(date, name, slug, note))

    for y in YEARS:
        easter = easter_gregorian(y)
        advent = _advent_first(y)
        for i, week in enumerate(["I", "II", "III", "IV"]):
            add(advent + datetime.timedelta(days=7 * i), f"Advent Sunday {week}",
                f"advent-{i + 1}", "The Church Year begins at Advent.")
        add(datetime.date(y, 12, 25), "Christmas Day", "christmas",
            "One of the Principal Feasts.")
        add(_first_sunday_after(datetime.date(y, 12, 25)), "First Sunday after Christmas",
            "christmas-1")
        add(_first_sunday_after(datetime.date(y, 1, 6)), "First Sunday after the Epiphany",
            "epiphany-1")
        add(datetime.date(y, 1, 6), "The Epiphany", "epiphany",
            "A Principal Feast.")
        add(datetime.date(y, 2, 2), "The Presentation (Candlemas)", "presentation",
            "A Holy Day.")
        add(easter - datetime.timedelta(days=46), "Ash Wednesday", "ash-wednesday",
            "Lent begins.")
        add(easter - datetime.timedelta(days=7), "The Sunday of the Passion (Palm Sunday)",
            "palm-sunday", "Holy Week begins.")
        add(easter - datetime.timedelta(days=3), "Maundy Thursday", "maundy-thursday",
            "The institution of the Eucharist.")
        add(easter - datetime.timedelta(days=2), "Good Friday", "good-friday",
            "The Passion of the Lord.")
        add(easter, "Easter Day", "easter",
            "The Queen of Feasts; the Great Fifty Days follow.")
        add(easter + datetime.timedelta(days=39), "Ascension Day", "ascension",
            "A Principal Feast, kept on a Thursday.")
        add(easter + datetime.timedelta(days=49), "The Day of Pentecost (Whitsunday)",
            "pentecost", "A Principal Feast.")
        add(easter + datetime.timedelta(days=56), "Trinity Sunday", "trinity-sunday",
            "A Principal Feast.")
        add(datetime.date(y, 5, 31), "The Visitation", "visitation", "A Holy Day.")
        add(datetime.date(y, 11, 1), "All Saints' Day", "all-saints",
            "A Principal Feast.")
        add(datetime.date(y, 11, 1) if datetime.date(y, 11, 1).weekday() != 6
            else datetime.date(y, 11, 1) + datetime.timedelta(days=7),
            "All Saints' Sunday", "all-saints-sunday")
        add(datetime.date(y, 6, 24), "The Nativity of Saint John the Baptist",
            "st-john-baptist", "A Holy Day.")
        add(datetime.date(y, 6, 29), "Saint Peter and Saint Paul", "sts-peter-and-paul",
            "A Holy Day.")
        add(datetime.date(y, 8, 15), "Saint Mary the Virgin", "st-mary",
            "A Holy Day (the church's major Marian feast).")
        add(datetime.date(y, 9, 29), "Saint Michael and All Angels", "michaelmas",
            "A Holy Day.")
        add(advent - datetime.timedelta(days=7), "Christ the King (Sunday next before Advent)",
            "christ-the-king", "The last Sunday of the year.")
    return events


# ==========================================================================
# Self-test + emission
# ==========================================================================

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
label = "Anglican (Book of Common Prayer)"
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
