import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import Keil2Clangd as k2c

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "sample.uvprojx")


class TestOutputDir(unittest.TestCase):
    def setUp(self):
        self.parser = k2c.UvprojxParser(FIX)

    def test_output_dir_normalized(self):
        self.assertEqual(self.parser.get_output_dir(), "./Objects")

    def test_project_name(self):
        self.assertEqual(self.parser.project_name, "sample")


if __name__ == "__main__":
    unittest.main()
