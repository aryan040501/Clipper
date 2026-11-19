import argparse
import unittest

from clip_watcher import (
    BucketCounter,
    ChatMessage,
    ClipPlanner,
    SpikeDetector,
    build_chat_srt,
    build_filter,
    parse_facecam,
    parse_privmsg,
    srt_time,
)


class ParsePrivmsgTest(unittest.TestCase):
    def test_plain_message(self):
        line = ":alice!alice@alice.tmi.twitch.tv PRIVMSG #xqc :KEKW what"
        self.assertEqual(parse_privmsg(line), ("alice", "KEKW what"))

    def test_tagged_message(self):
        line = "@badge-info=;color=#FF0000 :bob!bob@bob.tmi.twitch.tv PRIVMSG #xqc :hi"
        self.assertEqual(parse_privmsg(line), ("bob", "hi"))

    def test_other_commands_are_ignored(self):
        self.assertIsNone(parse_privmsg(":tmi.twitch.tv 001 justinfan12345 :Welcome, GLHF!"))
        self.assertIsNone(parse_privmsg("PING :tmi.twitch.tv"))


class BucketCounterTest(unittest.TestCase):
    def test_closes_buckets_including_empty_ones(self):
        counter = BucketCounter(10, now=103)
        counter.add(4)
        self.assertEqual(counter.tick(109.9), [])
        self.assertEqual(counter.tick(131), [(100, 4), (110, 0), (120, 0)])


class SpikeDetectorTest(unittest.TestCase):
    def make(self):
        return SpikeDetector(window_buckets=18, factor=2.5, min_messages=10, warmup_buckets=6)

    def test_no_spike_during_warmup(self):
        detector = self.make()
        self.assertFalse(any(detector.add(100) for _ in range(6)))

    def test_spike_needs_factor_and_minimum(self):
        detector = self.make()
        for _ in range(18):
            detector.add(8)
        self.assertFalse(detector.add(19))  # < 2.5 x 8
        self.assertTrue(detector.add(20))  # == 2.5 x 8

        quiet = self.make()
        for _ in range(18):
            quiet.add(1)
        self.assertFalse(quiet.add(9))  # 9x the median but under --min
        self.assertTrue(quiet.add(10))


class ClipPlannerTest(unittest.TestCase):
    def test_back_to_back_spikes_merge(self):
        planner = ClipPlanner(before=20, after=10, max_len=60, wait=15)
        clip = planner.on_spike(1000, 30)
        self.assertIsNotNone(clip)
        self.assertIsNone(planner.on_spike(1010, 50))
        self.assertEqual((clip.wall_start, clip.wall_end, clip.peak), (980, 1020, 50))

    def test_merge_respects_max_len(self):
        planner = ClipPlanner(before=20, after=10, max_len=45, wait=15)
        clip = planner.on_spike(1000, 30)
        for t in (1010, 1020, 1030):
            planner.on_spike(t, 30)
        self.assertEqual(clip.wall_end, 1025)

    def test_due_waits_and_new_clip_skips_cut_footage(self):
        planner = ClipPlanner(before=20, after=10, max_len=60, wait=15)
        planner.on_spike(1000, 30)
        self.assertEqual(planner.due(1020), [])
        self.assertEqual(len(planner.due(1035)), 1)
        later = planner.on_spike(1025, 30)  # would start at 1005, but 1010 is already cut
        self.assertEqual((later.wall_start, later.wall_end), (1010, 1035))


class FacecamTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_facecam("0:0.425:0.2:0.225"), (0.0, 0.425, 0.2, 0.225))

    def test_parse_rejects_bad_values(self):
        for bad in ("1:2:3", "a:b:c:d", "0.9:0:0.5:0.5", "0:0:0:0.5"):
            with self.assertRaises(argparse.ArgumentTypeError):
                parse_facecam(bad)

    def test_filter_defaults_to_center_crop(self):
        self.assertEqual(build_filter(None), ("-vf", "crop=trunc(ih*9/16/2)*2:ih"))

    def test_filter_stacks_face_over_game(self):
        flag, graph = build_filter((0.0, 0.4, 0.2, 0.2))
        self.assertEqual(flag, "-filter_complex")
        self.assertIn("crop=iw*0.2:ih*0.2:iw*0.0:ih*0.4", graph)
        self.assertIn("vstack", graph)


class SrtTest(unittest.TestCase):
    def test_srt_time(self):
        self.assertEqual(srt_time(3725.5), "01:02:05,500")

    def test_chat_cues_stack_and_stay_inside_the_clip(self):
        messages = [
            ChatMessage(95, "early", "not in clip"),
            ChatMessage(101, "a", "first"),
            ChatMessage(102, "b", "second"),
            ChatMessage(110, "c", "third"),
            ChatMessage(131, "late", "not in clip"),
        ]
        srt = build_chat_srt(messages, clip_start=100, clip_end=130)
        self.assertEqual(
            srt,
            "1\n00:00:01,000 --> 00:00:02,000\na: first\n\n"
            "2\n00:00:02,000 --> 00:00:06,000\na: first\nb: second\n\n"
            "3\n00:00:10,000 --> 00:00:14,000\nc: third\n",
        )


if __name__ == "__main__":
    unittest.main()
