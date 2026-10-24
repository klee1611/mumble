from datetime import datetime
from zoneinfo import ZoneInfo

from mumble.structure import resolve_when

TZ = ZoneInfo("America/Toronto")
# Saturday, Oct 24 2026, 5:18 AM in Toronto
RECORDED = datetime(2026, 10, 24, 5, 18, tzinfo=TZ)


def local(dt):
    return dt.astimezone(TZ).replace(tzinfo=None)


def test_day_and_clock_time():
    t = "I have a meeting with Priya on Tuesday at 10am about the hackathon."
    assert local(resolve_when("Tuesday at 10am", t, RECORDED)) == datetime(2026, 10, 27, 10, 0)


def test_day_only_uses_nearby_part_of_day():
    t = "pick up the dry cleaning tomorrow after work"
    assert local(resolve_when("tomorrow", t, RECORDED)) == datetime(2026, 10, 25, 18, 0)


def test_day_only_defaults_to_nine():
    t = "email the landlord tomorrow"
    assert local(resolve_when("tomorrow", t, RECORDED)) == datetime(2026, 10, 25, 9, 0)


def test_part_of_day_in_phrase():
    t = "call mom tomorrow morning"
    assert local(resolve_when("tomorrow morning", t, RECORDED)) == datetime(2026, 10, 25, 9, 0)


def test_phrase_the_speaker_never_said_is_dropped():
    t = "call the dentist about my tooth"
    assert resolve_when("Monday at 3pm", t, RECORDED) is None


def test_empty_phrase():
    assert resolve_when("", "anything", RECORDED) is None


WED_EVENING = datetime(2026, 10, 21, 18, 20, tzinfo=TZ)


def test_bare_hour_is_afternoon():
    t = "Priya says the meetup is Thursday at 6 at the library"
    assert local(resolve_when("Thursday at 6", t, WED_EVENING)) == datetime(2026, 10, 22, 18, 0)


def test_bare_hour_with_minutes():
    t = "dentist Friday at 2:30"
    assert local(resolve_when("Friday at 2:30", t, WED_EVENING)) == datetime(2026, 10, 23, 14, 30)


def test_bare_morning_hour():
    t = "standup tomorrow at 9"
    assert local(resolve_when("tomorrow at 9", t, WED_EVENING)) == datetime(2026, 10, 22, 9, 0)


def test_explicit_meridiem_untouched():
    t = "call at 6am on Friday"
    assert local(resolve_when("6am on Friday", t, WED_EVENING)) == datetime(2026, 10, 23, 6, 0)


def test_before_a_day_is_that_day():
    t = "pick up mom's gift before Saturday"
    assert local(resolve_when("before Saturday", t, WED_EVENING)) == datetime(2026, 10, 24, 9, 0)


def test_dates_are_not_hours():
    t = "renew the passport by November 1st"
    assert local(resolve_when("by November 1st", t, WED_EVENING)) == datetime(2026, 11, 1, 9, 0)


def test_clock_time_across_the_dst_change():
    # Recorded in EDT, happens after clocks go back (Nov 1 2026).
    t = "flu shot November 3rd at 10am"
    when = resolve_when("November 3rd at 10am", t, WED_EVENING)
    assert local(when) == datetime(2026, 11, 3, 10, 0)
    assert when.utcoffset().total_seconds() == -5 * 3600  # EST


def test_before_a_month_is_the_first():
    t = "renew my passport before December"
    assert local(resolve_when("before December", t, WED_EVENING)) == datetime(2026, 12, 1, 9, 0)
