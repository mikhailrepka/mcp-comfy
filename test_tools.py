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
import subprocess as sp
import urllib.error
from unittest import mock

sys.path.insert(0, "/Users/mikhailrepka/comfy-mcp")

import comfy_mcp_server as gen
import jobstore

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
        """Подменяет локальный jobs-реестр на время теста + глушит jobstore.put
        (иначе тесты пишут мусор в общий ~/.comfy_mcp_jobs.json)."""
        old = gen.jobs.get(job_id)
        gen.jobs[job_id] = record
        self.addCleanup(gen.jobs.pop, job_id, None)
        if old is not None:  # pragma: no cover
            gen.jobs[job_id] = old
        put = mock.patch.object(gen.jobstore, "put", return_value=None)
        put.start()
        self.addCleanup(put.stop)

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


class BatchTests(unittest.TestCase):
    """n>1: batch_size в payload, мультифайл fetch, мульти-контент, foreign_check."""

    def _capture_submit(self, *args, **kw):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["data"] = json.loads(req.data.decode())
            return _resp(b'{"prompt_id": "test-pid"}')

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            gen._submit(*args, **kw)
        return captured["data"]["prompt"]["57:13"]["inputs"]["batch_size"]

    def test_batch_size_passthrough(self):
        self.assertEqual(self._capture_submit("p", 512, 512, 8, 1, n=3), 3)

    def test_batch_size_clamped_to_4(self):
        self.assertEqual(self._capture_submit("p", 512, 512, 8, 1, n=99), 4)

    def test_fetch_image_multi_and_single(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)

        def _hist(count):
            return {"pid": {"outputs": {"9": {"images": [
                {"filename": f"v{i}.png", "subfolder": "", "type": "output"}
                for i in range(count)]}}}}

        for count, expected in ((2, [b"AAA", b"BBB"]), (1, [b"AAA"])):
            with mock.patch.object(gen, "OUTPUT_DIR", tmp), \
                    mock.patch.object(gen, "_history", return_value=_hist(count)), \
                    mock.patch("urllib.request.urlopen",
                               side_effect=[_resp(b"AAA"), _resp(b"BBB")]):
                res = gen._fetch_image("pid")
            self.assertEqual([d for _, d in res], expected)
            self.assertEqual([p for p, _ in res],
                             [os.path.join(tmp, f"v{i}.png") for i in range(count)])

    def test_content_batch_three_images(self):
        job = {"status": "done", "paths": ["/a/1.png", "/a/2.png", "/a/3.png"],
               "data": [b"\x89P1", b"\x89P2", b"\x89P3"], "seed": 7}
        content = gen._content_for_result(job)
        imgs = [i for i in content if getattr(i, "type", None) == "image"]
        texts = [i.text for i in content if getattr(i, "type", None) == "text"]
        self.assertEqual(len(imgs), 3)
        self.assertEqual(texts[0].count("Image generated:"), 3)
        self.assertTrue(texts[0].startswith("Image generated: /a/1.png"))
        self.assertTrue(texts[0].endswith("Seed: 7"))

    def test_content_single_legacy_record(self):
        # регресс: старая форма записи {path, data (bytes)} — как в jobstore у одиночных
        content = gen._content_for_result(
            {"status": "done", "path": "/x/y.png", "data": b"\x89PNG", "seed": 42})
        imgs = [i for i in content if getattr(i, "type", None) == "image"]
        self.assertEqual(len(imgs), 1)

    def test_foreign_check_batch_returns_all(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        p1, p2 = os.path.join(tmp, "1.png"), os.path.join(tmp, "2.png")
        for p, data in ((p1, b"P1"), (p2, b"P2")):
            open(p, "wb").write(data)
        rec = {"status": "done", "path": p1, "paths": [p1, p2], "seed": 3}
        with mock.patch.object(gen.jobstore, "get", return_value=rec):
            content = gen._foreign_check("job_9")
        imgs = [i for i in content if getattr(i, "type", None) == "image"]
        self.assertEqual(len(imgs), 2)


class PcResolveTests(unittest.TestCase):
    """Цепочка PC: env COMFY_PC_URL -> local.env -> дефолт."""

    def test_default_without_env_and_file(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(gen._resolve_pc(env_file="/nonexistent/local.env"),
                             "http://192.168.1.218:8188")

    def test_env_file_value(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        p = os.path.join(tmp, "local.env")
        with open(p, "w") as fh:
            fh.write("# comment\nCOMFY_PC_URL=http://10.0.0.5:8188\n")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(gen._resolve_pc(env_file=p), "http://10.0.0.5:8188")

    def test_env_wins_over_file(self):
        with mock.patch.dict(os.environ, {"COMFY_PC_URL": "http://10.0.0.9:8188"}):
            self.assertEqual(gen._resolve_pc(env_file="/nonexistent/local.env"),
                             "http://10.0.0.9:8188")

    def test_trailing_slash_stripped(self):
        self.assertEqual(gen._resolve_pc(env_value="http://x:8188/", env_file="/no"),
                         "http://x:8188")

    def test_quotes_stripped_in_env_file(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        p = os.path.join(tmp, "local.env")
        with open(p, "w") as fh:
            fh.write('COMFY_PC_URL="http://10.0.0.7:8188"\n')
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(gen._resolve_pc(env_file=p), "http://10.0.0.7:8188")


class InstallVersionTests(unittest.TestCase):
    """comfy_status печатает версию установки из файла VERSION."""

    def test_missing_file_reports_unknown(self):
        self.assertEqual(gen._install_version("/nonexistent-dir"), "unknown")

    def test_version_file_read(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(os.path.join(tmp, "VERSION"), "w") as fh:
            fh.write("abc1234\n")
        self.assertEqual(gen._install_version(tmp), "abc1234")




class InlineWaitConfigTests(unittest.TestCase):
    """FAST_WAIT/CHECK_WAIT_INLINE: дефолты безопасны для LM Studio (~60s таймаут
    клиента), переопределяются env COMFY_FAST_WAIT / COMFY_CHECK_WAIT."""

    def test_defaults(self):
        self.assertEqual(gen.FAST_WAIT, 25)
        self.assertEqual(gen.CHECK_WAIT_INLINE, 30)
        self.assertEqual(gen.CHECK_WAIT, 600)  # фоновый дедлайн не тронут

    def test_env_overrides(self):
        env = dict(os.environ)
        env["COMFY_FAST_WAIT"] = "55"
        env["COMFY_CHECK_WAIT"] = "45"
        code = ("import sys; sys.path.insert(0, %r); import comfy_mcp_server as g; "
                "print(g.FAST_WAIT, g.CHECK_WAIT_INLINE)" % os.path.dirname(gen.__file__))
        r = sp.run([sys.executable, "-I", "-c", code], env=env,
                   capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-300:])
        self.assertEqual(r.stdout.strip(), "55 45")


class InlineWaitConfigTests(unittest.TestCase):
    """FAST_WAIT/CHECK_WAIT_INLINE: дефолты безопасны для LM Studio (~60s таймаут
    клиента), переопределяются env COMFY_FAST_WAIT / COMFY_CHECK_WAIT."""

    def test_defaults(self):
        self.assertEqual(gen.FAST_WAIT, 25)
        self.assertEqual(gen.CHECK_WAIT_INLINE, 30)
        self.assertEqual(gen.CHECK_WAIT, 600)  # фоновый дедлайн не тронут

    def test_env_overrides(self):
        env = dict(os.environ)
        env["COMFY_FAST_WAIT"] = "55"
        env["COMFY_CHECK_WAIT"] = "45"
        code = ("import sys; sys.path.insert(0, %r); import comfy_mcp_server as g; "
                "print(g.FAST_WAIT, g.CHECK_WAIT_INLINE)" % os.path.dirname(gen.__file__))
        r = sp.run([sys.executable, "-I", "-c", code], env=env,
                   capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-300:])
        self.assertEqual(r.stdout.strip(), "55 45")


class PromptError400Tests(unittest.TestCase):
    """gen и upscale: 400 от /prompt разбирается в читаемую ошибку
    (тот же паттерн, что в edit/3D)."""

    BODY = json.dumps({
        "error": {"message": "Prompt outputs failed validation"},
        "node_errors": {"57:30": {"errors": [{
            "message": "Value not in list", "details": "bad_clip.safetensors"}]}},
    }).encode()

    def _http_error(self):
        return urllib.error.HTTPError("http://pc/prompt", 400, "Bad Request",
                                      {"Content-Type": "application/json"},
                                      io.BytesIO(self.BODY))

    def test_gen_submit_raises_readable(self):
        with mock.patch("urllib.request.urlopen", side_effect=self._http_error()):
            with self.assertRaisesRegex(RuntimeError, "node 57:30.*bad_clip"):
                gen._submit("p", 512, 512, 8)

    def test_upscale_submit_raises_readable(self):
        import upscale_mcp_server as up
        with mock.patch("urllib.request.urlopen", side_effect=self._http_error()):
            with self.assertRaisesRegex(RuntimeError, "node 57:30.*bad_clip"):
                up._submit("src.png", 2, None, "lanczos")


class ListResultsInlineTests(unittest.TestCase):
    """inline=true прикладывает новейшие картинки ImageContent (кап 4);
    строки 'Image ready:' идут в порядке картинок (pairing у daemon'а)."""

    def setUp(self):
        import trimesh  # noqa: F401  (убеждаемся, что PIL доступен через тесты)
        from PIL import Image
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        old = gen.OUTPUT_DIR
        gen.OUTPUT_DIR = self.tmp
        self.addCleanup(setattr, gen, "OUTPUT_DIR", old)
        self.paths = []
        for i in range(5):
            p = os.path.join(self.tmp, f"r{i}.png")
            Image.new("RGB", (16, 16), (10 * i, 20, 30)).save(p)
            os.utime(p, (time.time() - 100 * (5 - i),) * 2)
            self.paths.append(p)

    def _call(self, args):
        import asyncio
        return asyncio.run(gen.call_tool("list_results", args))

    def test_inline_attaches_up_to_four(self):
        content = self._call({"inline": True, "limit": 10})
        kinds = [getattr(i, "type", "?") for i in content]
        self.assertEqual(kinds.count("image"), 4)  # кап
        text = [i.text for i in content if getattr(i, "type", None) == "text"][0]
        self.assertEqual(text.count("Image ready:"), 4)
        # порядок: новейшие первыми
        first = text.split("Image ready: ")[1].splitlines()[0]
        self.assertEqual(first.strip().split(" ")[0], self.paths[4])

    def test_default_text_only(self):
        content = self._call({"limit": 3})
        self.assertNotIn("image", [getattr(i, "type", "?") for i in content])
        text = [i.text for i in content if getattr(i, "type", None) == "text"][0]
        self.assertNotIn("Image ready:", text)


class JobstorePollTests(unittest.TestCase):
    """jobstore.poll — честное ожидание чужой задачи (общий jobstore):
    LM Studio направляет check_image на ПОСЛЕДНИЙ зарегистрированный сервер,
    поэтому чужая проверка обязана быть эквивалентной родной (баг 2026-11-24:
    разовый sleep 5с + 'started by another server' сбивал модель)."""

    def test_poll_returns_done_immediately(self):
        with mock.patch.object(jobstore, "_load",
                               return_value={"j": {"status": "done", "path": "/x.png"}}):
            rec = jobstore.poll("j", wait=5, interval=0.1)
        self.assertEqual(rec["status"], "done")

    def test_poll_waits_until_done(self):
        states = iter([
            {"j": {"status": "queued"}},
            {"j": {"status": "queued"}},
            {"j": {"status": "done", "path": "/x.png"}},
        ])
        sleeps = []
        with mock.patch.object(jobstore, "_load", side_effect=lambda: next(states)), \
                mock.patch("time.sleep", side_effect=lambda s: sleeps.append(s)):
            rec = jobstore.poll("j", wait=30, interval=0.0)
        self.assertEqual(rec["status"], "done")
        self.assertEqual(len(sleeps), 2)

    def test_poll_timeout_returns_last_known(self):
        with mock.patch.object(jobstore, "_load",
                               return_value={"j": {"status": "queued"}}), \
                mock.patch("time.sleep", return_value=None), \
                mock.patch("time.time", side_effect=[0, 1, 2, 3, 100]):
            rec = jobstore.poll("j", wait=2, interval=1)
        self.assertEqual(rec["status"], "queued")

    def test_unknown_job_returns_none(self):
        with mock.patch.object(jobstore, "_load", return_value={}):
            self.assertIsNone(jobstore.poll("nope", wait=1, interval=0.1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
