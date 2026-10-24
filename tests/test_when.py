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
