import unittest
from pathlib import Path

from youtube_upload import UploadJob, YouTubeUploader, build_metadata, top_reactions


class TopReactionsTest(unittest.TestCase):
    def test_counts_each_word_once_per_message(self):
        texts = ["KEKW KEKW KEKW KEKW", "OMEGALUL", "OMEGALUL nice", "omg"]
        self.assertEqual(top_reactions(texts, 2), ["OMEGALUL", "KEKW"])

    def test_ignores_plain_words(self):
        self.assertEqual(top_reactions(["this is a normal sentence"]), [])

    def test_reaction_words_are_uppercased(self):
        self.assertEqual(top_reactions(["lol", "LOL"]), ["LOL"])


class BuildMetadataTest(unittest.TestCase):
    def test_title_description_tags(self):
        job = build_metadata("xqc", 84, 30, 1_700_000_000, ["KEKW", "KEKW", "OMEGALUL"], 3)
        self.assertEqual(job.title, "xqc | Chat goes wild (KEKW, OMEGALUL) #Shorts")
        self.assertIn("84 messages in 10 seconds from 3 chatters", job.description)
        self.assertIn("https://twitch.tv/xqc", job.description)
        self.assertIn("kekw", job.tags)

    def test_long_clip_is_not_a_short(self):
        job = build_metadata("xqc", 50, 400, 1_700_000_000, [], 0)
        self.assertEqual(job.title, "xqc | Chat goes wild")

    def test_title_stays_within_youtube_limit(self):
        job = build_metadata("a" * 150, 50, 30, 1_700_000_000, [], 0)
        self.assertLessEqual(len(job.title), 100)
        self.assertTrue(job.title.endswith("#Shorts"))


class UploaderTest(unittest.TestCase):
    def test_uploads_in_order_and_survives_failures(self):
        done = []

        def fake(youtube, job, category, privacy):
            if job.title == "bad":
                raise RuntimeError("boom")
            done.append((job.title, category, privacy))
            return "id"

        uploader = YouTubeUploader(None, "20", "private", uploader=fake)
        uploader.start()
        for title in ("one", "bad", "two"):
            uploader.submit(UploadJob(Path("x.mp4"), title, ""))
        uploader.finish()
        self.assertEqual(done, [("one", "20", "private"), ("two", "20", "private")])


if __name__ == "__main__":
    unittest.main()
