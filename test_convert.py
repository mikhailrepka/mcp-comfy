#!/usr/bin/env python3
"""Unit-тесты convert_image v2 (2026-11-23): skip-логика unchanged, размеры
в ответах, list-строки с (WxH ext KB).

Запуск: ~/comfy-mcp/.venv/bin/python ~/comfy-mcp/test_convert.py
"""
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, "/Users/mikhailrepka/comfy-mcp")

from PIL import Image

import edit_mcp_server as edit
import comfy_mcp_server as gen


def _mk(path, size, color=(120, 40, 40)):
    Image.new("RGB", size, color).save(path)
    return path


class ConvertSkipTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_jpg_small_with_max_is_unchanged(self):
        p = _mk(os.path.join(self.tmp, "a.jpg"), (800, 600))
        res = edit._convert_images([p], "jpg", 90, 1920, None)
        status, path, note = res[0]
        self.assertEqual(status, "unchanged")
        self.assertEqual(path, p)           # путь не изменился
        self.assertEqual(os.listdir(self.tmp).count("a.jpg"), 1)  # перекодирования нет
        self.assertIn("800x600", note)
        self.assertIn("KB", note)

    def test_jpg_huge_downscaled(self):
        p = _mk(os.path.join(self.tmp, "big.jpg"), (5000, 3000))
        res = edit._convert_images([p], "jpg", 90, 4000, None)
        status, path, note = res[0]
        self.assertEqual(status, "ok")
        self.assertEqual(path, p)           # same-ext сжимается in place
        self.assertIn("4000x2400", note)    # уменьшен до лимита

    def test_png_to_jpg_converts_even_small(self):
        p = _mk(os.path.join(self.tmp, "b.png"), (500, 500))
        res = edit._convert_images([p], "jpg", 90, 4000, None)
        status, path, note = res[0]
        self.assertEqual(status, "ok")
        self.assertTrue(path.endswith(".jpg"))
        self.assertIn("500x500", note)

    def test_jpg_without_limits_unchanged(self):
        p = _mk(os.path.join(self.tmp, "c.jpg"), (300, 300))
        res = edit._convert_images([p], "jpg", 90, None, None)
        self.assertEqual(res[0][0], "unchanged")  # перекодирование того же формата бессмысленно

    def test_missing_file(self):
        res = edit._convert_images(["/nonexistent/x.png"], "jpg", 90, None, None)
        self.assertEqual(res[0][0], "notfound")

    def test_webp_target(self):
        p = _mk(os.path.join(self.tmp, "d.jpg"), (600, 400))
        res = edit._convert_images([p], "webp", 90, None, None)
        status, path, _ = res[0]
        self.assertEqual(status, "ok")
        self.assertTrue(path.endswith(".webp"))


class ListNotesTests(unittest.TestCase):

    def test_list_inbox_has_size_notes(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        _mk(os.path.join(tmp, "x.jpg"), (640, 480))
        with mock.patch.object(edit, "INBOX_DIR", tmp):
            lines = edit._list_inbox()
        self.assertEqual(len(lines), 1)
        self.assertIn("640x480 jpg", lines[0])
        self.assertIn("KB", lines[0])

    def test_list_results_has_size_notes(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        p = _mk(os.path.join(tmp, "y.png"), (100, 100))
        os.utime(p, (time.time(), time.time()))
        with mock.patch.object(gen, "OUTPUT_DIR", tmp):
            text = gen._list_results_text(5)
        self.assertIn("100x100 png", text)
        self.assertIn("KB", text)

    def test_img_note_tolerant_to_garbage(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        p = os.path.join(tmp, "garbage.jpg")
        open(p, "wb").write(b"not an image at all")
        note = edit._img_note(p)
        self.assertIn("KB", note)      # байты знает даже без распознавания
        self.assertNotIn("x", note.split("(")[1].split()[0] if "(" in note else "x0x")


if __name__ == "__main__":
    unittest.main(verbosity=2)
