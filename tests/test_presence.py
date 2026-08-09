import unittest

from config.wake import FACE_FEED_STALE_S
from vision import presence


class PresenceTestCase(unittest.TestCase):
    """presence keeps module-level state; every test starts from a clean slate."""

    def setUp(self):
        presence.reset()

    tearDown = setUp


class TestSnapshotBeforeAnyMark(PresenceTestCase):
    def test_is_unknown_not_absent(self):
        # Nothing has produced yet (camera still opening, or --no-camera). A consumer that read
        # this as "absent" would end a conversation the moment it started.
        visible, since, is_fresh = presence.snapshot(now=100.0)
        self.assertFalse(is_fresh)
        self.assertFalse(visible)
        self.assertEqual(since, float("inf"))


class TestMark(PresenceTestCase):
    def test_visible_is_fresh_and_seen_now(self):
        presence.mark(True, now=100.0)
        visible, since, is_fresh = presence.snapshot(now=100.0)
        self.assertTrue(visible)
        self.assertTrue(is_fresh)
        self.assertEqual(since, 0.0)

    def test_absent_is_fresh_with_infinite_since_when_never_seen(self):
        presence.mark(False, now=100.0)
        visible, since, is_fresh = presence.snapshot(now=100.0)
        self.assertFalse(visible)
        self.assertTrue(is_fresh, "an absent report is still proof the producer is alive")
        self.assertEqual(since, float("inf"))

    def test_since_grows_after_face_leaves(self):
        presence.mark(True, now=100.0)
        for t in (101.0, 102.0, 103.0, 104.0, 105.0):   # feed keeps running, just reporting no face
            presence.mark(False, now=t)
        visible, since, is_fresh = presence.snapshot(now=105.0)
        self.assertFalse(visible)
        self.assertTrue(is_fresh)
        self.assertAlmostEqual(since, 5.0)

    def test_reappearing_resets_since(self):
        presence.mark(True, now=100.0)
        presence.mark(False, now=101.0)
        presence.mark(True, now=104.0)
        _, since, _ = presence.snapshot(now=104.0)
        self.assertEqual(since, 0.0)

    def test_absent_does_not_move_last_seen(self):
        presence.mark(True, now=100.0)
        for t in (101.0, 102.0, 103.0):
            presence.mark(False, now=t)
        _, since, _ = presence.snapshot(now=103.0)
        self.assertAlmostEqual(since, 3.0, msg="absence must be measured from the last sighting")


class TestFreshness(PresenceTestCase):
    def test_fresh_at_exactly_the_stale_boundary(self):
        presence.mark(False, now=100.0)
        _, _, is_fresh = presence.snapshot(now=100.0 + FACE_FEED_STALE_S)
        self.assertTrue(is_fresh)

    def test_stale_just_past_the_boundary(self):
        presence.mark(False, now=100.0)
        _, _, is_fresh = presence.snapshot(now=100.0 + FACE_FEED_STALE_S + 0.01)
        self.assertFalse(is_fresh)

    def test_camera_stall_goes_unknown_while_last_report_was_visible(self):
        # face_track.py stops calling mark() entirely when the camera returns no frame, so the last
        # value sticks. Consumers must key off is_fresh, not the stale `visible`.
        presence.mark(True, now=100.0)
        visible, _, is_fresh = presence.snapshot(now=100.0 + FACE_FEED_STALE_S + 5.0)
        self.assertTrue(visible, "the stale value is whatever was last written")
        self.assertFalse(is_fresh, "...and is_fresh is what tells you not to trust it")

    def test_freshness_recovers_when_the_feed_resumes(self):
        presence.mark(False, now=100.0)
        self.assertFalse(presence.snapshot(now=110.0)[2])
        presence.mark(False, now=110.0)
        self.assertTrue(presence.snapshot(now=110.0)[2])


class TestSinceIsNeverNegative(PresenceTestCase):
    def test_clamps_on_out_of_order_timestamps(self):
        presence.mark(True, now=100.0)
        _, since, _ = presence.snapshot(now=99.0)
        self.assertEqual(since, 0.0)


class TestReset(PresenceTestCase):
    def test_returns_to_unknown(self):
        presence.mark(True, now=100.0)
        presence.reset()
        visible, since, is_fresh = presence.snapshot(now=100.0)
        self.assertFalse(visible)
        self.assertFalse(is_fresh)
        self.assertEqual(since, float("inf"))


if __name__ == "__main__":
    unittest.main()
