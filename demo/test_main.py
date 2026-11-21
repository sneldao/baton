import unittest

from main import greet, word_count


class TestDemoSite(unittest.TestCase):
    def test_greet_name(self):
        self.assertEqual(greet("vibe"), "hello, vibe — a very small website generator")

    def test_word_count(self):
        self.assertEqual(word_count("the quick brown fox"), 4)

    def test_word_count_empty(self):
        self.assertEqual(word_count(""), 0)


if __name__ == "__main__":
    unittest.main()
