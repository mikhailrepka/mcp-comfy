#!/usr/bin/env python3
"""Unit-тесты новых инструментов gen-сервера (2026-11-23):
comfy_status, cancel_job, list_results, seed.

Запуск: ~/comfy-mcp/.venv/bin/python ~/comfy-mcp/test_tools.py
Сеть и диск не трогаются: urlopen патчится, OUTPUT_DIR перекидывается на tmp.
"""
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, "/Users/mikhailrepka/comfy-mcp")

import comfy_mcp_server as gen

QUEUE_OK = json.dumps({
    "queue_running": [["0", "run-pid", {}, {}, []]],
    "queue_pending": [["0", "pend-pid", {}, {}, []]],
}).encode()


def _resp(body: bytes):
    return io.BytesIO(body)


def _url_of(req) -> str:
    """urlopen может получить и Request, и готовую строку URL."""
    return req.full_url if hasattr(req, "full_url") else req


def _method_of(req) -> str:
    return req.get_method() if hasattr(req, "get_method") else "GET"


class ComfyStatusTests(unittest.TestCase):

    def test_unreachable(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.URLError("No route to host")):
            text = gen._comfy_status_text()
        self.assertIn("unreachable", text.lower())
        self.assertIn("No route to host", text)

    def test_ok_with_queue(self):
        stats = json.dumps({
            "system": {"comfyui_version": "0.3.73", "ram_total": 34 * 2**30,
                       "ram_free": 20 * 2**30},
            "devices": [{"name": "NVIDIA GeForce RTX 4090", "vram_total": 24 * 2**30,
                         "vram_free": 18 * 2**30}],
        }).encode()
        with mock.patch("urllib.request.urlopen",
                        side_effect=[_resp(stats), _resp(QUEUE_OK)]):
            text = gen._comfy_status_text()
        self.assertIn("ComfyUI OK", text)
        self.assertIn("RTX 4090", text)
        self.assertIn("Queue: 1 running, 1 pending", text)


class CancelJobTests(unittest.TestCase):

    def _with_jobs(self, job_id, record):
        """Подменяет локальный jobs-реестр на время теста."""
        old = gen.jobs.get(job_id)
        gen.jobs[job_id] = record
        self.addCleanup(gen.jobs.pop, job_id, None)
        if old is not None:  # pragma: no cover
            gen.jobs[job_id] = old

    def test_unknown_job(self):
        with mock.patch("urllib.request.urlopen") as url:
            text = gen._cancel_job("job_nope")
        self.assertIn("Unknown job_id", text)
        url.assert_not_called()

    def test_already_done(self):
        self._with_jobs("job_1", {"status": "done", "path": "/x/y.png"})
        with mock.patch("urllib.request.urlopen") as url:
            text = gen._cancel_job("job_1")
        self.assertIn("already done", text)
        url.assert_not_called()

    def test_queued_job_deleted(self):
        self._with_jobs("job_2", {"status": "queued", "prompt_id": "pend-pid"})
        reqs = []

        def fake_urlopen(req, timeout=None):
            reqs.append(req)
            if _method_of(req) == "GET":
                return _resp(QUEUE_OK)  # статус очереди
            return _resp(b"{}")         # DELETE /queue

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            text = gen._cancel_job("job_2")
        self.assertIn("was queued", text)
        self.assertEqual(len(reqs), 2)
        self.assertTrue(_url_of(reqs[0]).endswith("/queue"))          # статус очереди
        self.assertTrue(_url_of(reqs[1]).endswith("/queue"))          # DELETE /queue
        self.assertIn("pend-pid", reqs[1].data.decode())
        self.assertEqual(gen.jobs["job_2"]["error"], "cancelled by user (cancel_job)")

    def test_running_job_interrupted(self):
        self._with_jobs("job_3", {"status": "queued", "prompt_id": "run-pid"})
        reqs = []

        def fake_urlopen(req, timeout=None):
            reqs.append(req)
            if _url_of(req).endswith("/queue"):
                return _resp(QUEUE_OK)
            return _resp(b"{}")  # /interrupt

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            text = gen._cancel_job("job_3")
        self.assertIn("was running", text)
        self.assertTrue(_url_of(reqs[1]).endswith("/interrupt"))

    def test_no_longer_in_queue(self):
        self._with_jobs("job_4", {"status": "queued", "prompt_id": "gone-pid"})
        with mock.patch("urllib.request.urlopen", side_effect=[_resp(QUEUE_OK)]):
            text = gen._cancel_job("job_4")
        self.assertIn("no longer in the ComfyUI queue", text)


class ListResultsTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        old = gen.OUTPUT_DIR
        gen.OUTPUT_DIR = self.tmp
        self.addCleanup(setattr, gen, "OUTPUT_DIR", old)

    def _mkfile(self, name, offset):
        p = os.path.join(self.tmp, name)
        open(p, "w").close()
        os.utime(p, (time.time() + offset,) * 2)

    def test_order_newest_first_and_limit(self):
        self._mkfile("a.png", -300)
        self._mkfile("b.png", -200)
        self._mkfile("c.jpg", -100)
        self._mkfile("notes.txt", -50)
        text = gen._list_results_text(limit=2)
        self.assertLess(text.index("c.jpg"), text.index("b.png"))
        self.assertNotIn("a.png", text)
        self.assertIn("+1 older", text)
        self.assertNotIn("notes.txt", text)

    def test_empty_and_missing(self):
        self.assertIn("No result images", gen._list_results_text())
        self._mkfile("z.png", -10)
        self.assertNotIn("No result images", gen._list_results_text())
        with mock.patch.object(gen, "OUTPUT_DIR", "/nonexistent-dir-xyz"):
            self.assertIn("No results folder", gen._list_results_text())


class SeedTests(unittest.TestCase):

    def _submit(self, **kw):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["data"] = json.loads(req.data.decode())
            return _resp(b'{"prompt_id": "test-pid"}')

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            ret = gen._submit(**kw)
        return ret, captured["data"]["prompt"]["57:3"]["inputs"]["seed"]

    def test_seed_fixed(self):
        (_, seed), wf_seed = self._submit(prompt="p", width=512, height=512,
                                          steps=8, seed=12345)
        self.assertEqual(seed, 12345)
        self.assertEqual(wf_seed, 12345)

    def test_seed_random_when_absent(self):
        (_, seed), wf_seed = self._submit(prompt="p", width=512, height=512, steps=8)
        self.assertEqual(seed, wf_seed)
        self.assertTrue(0 <= seed < 2**32)

    def test_seed_in_result_text(self):
        content = gen._content_for_result(
            {"status": "done", "path": "/x/y.png", "data": b"\x89PNG", "seed": 42})
        texts = [i.text for i in content if getattr(i, "type", None) == "text"]
        self.assertEqual(texts[0], "Image generated: /x/y.png\nSeed: 42")

    def test_no_seed_line_when_absent(self):
        content = gen._content_for_result(
            {"status": "done", "path": "/x/y.png", "data": b"\x89PNG"})
        texts = [i.text for i in content if getattr(i, "type", None) == "text"]
        self.assertEqual(texts[0], "Image generated: /x/y.png")


if __name__ == "__main__":
    unittest.main(verbosity=2)
