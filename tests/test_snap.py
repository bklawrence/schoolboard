import unittest

from collectors.snap import parse_ics


SAMPLE = """BEGIN:VCALENDAR\r
VERSION:2.0\r
BEGIN:VEVENT\r
UID:event-123@example.com\r
DTSTART;TZID=America/Chicago:20260903T180000\r
DTEND;TZID=America/Chicago:20260903T200000\r
SUMMARY:Varsity Girls Volleyball @ Kenney Gym\r
LOCATION:Kenney Gym\r
URL:https://schools.snap.app/Uni/event/4925\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:event-456@example.com\r
DTSTART;VALUE=DATE:20260905\r
SUMMARY:Cross Country Invitational\r
LOCATION:Chrisman\r
END:VEVENT\r
END:VCALENDAR\r
"""


class SnapParserTest(unittest.TestCase):
    def test_timed_event(self):
        events = parse_ics(SAMPLE)
        event = events[0]
        self.assertEqual(event["date"], "2026-09-03")
        self.assertEqual(event["start"], "18:00")
        self.assertEqual(event["end"], "20:00")
        self.assertEqual(event["schools"], ["uni"])
        self.assertEqual(event["category"], "sport")
        self.assertEqual(event["sourceUrl"], "https://schools.snap.app/Uni/event/4925")

    def test_all_day_event(self):
        events = parse_ics(SAMPLE)
        event = events[1]
        self.assertEqual(event["date"], "2026-09-05")
        self.assertTrue(event["allDay"])
        self.assertNotIn("start", event)


if __name__ == "__main__":
    unittest.main()
