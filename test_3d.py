#!/usr/bin/env python3
"""Unit-тесты comfy_3d_mcp_server (2026-11-23): построение графа под 2/3/4 вида,
fetch GLB (generic outputs scan), convert_3d (trimesh), и remove_background
edit-сервера.

Запуск: ~/comfy-mcp/.venv/bin/python ~/comfy-mcp/test_3d.py
"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, "/Users/mikhailrepka/comfy-mcp")

import comfy_3d_mcp_server as srv3d
import edit_mcp_server as edit


def _captured_wf(**kw):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["data"] = json.loads(req.data.decode())
        return io.BytesIO(b'{"prompt_id": "test-pid"}')

    with mock.patch.object(srv3d, "_upload_image",
                           side_effect=lambda p: os.path.basename(p)), \
            mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        srv3d._submit(**kw)
    return captured["data"]["prompt"]


class GraphBuildTests(unittest.TestCase):
    """Граф собирается под переданные виды; лишние узлы вырезаются/добавляются."""

    def test_two_views_front_back(self):
        wf = _captured_wf(front="f.png", back="b.png", left=None, right=None,
                          steps=20, seed=1, octree_resolution=None)
        self.assertEqual(wf[srv3d.N_FRONT]["inputs"]["image"], "f.png")
        self.assertEqual(wf[srv3d.N_BACK]["inputs"]["image"], "b.png")
        self.assertEqual(wf[srv3d.N_COND]["inputs"]["front"], ["51", 0])
        self.assertEqual(wf[srv3d.N_COND]["inputs"]["back"], [srv3d.N_ENC_BACK, 0])
        self.assertNotIn("left", wf[srv3d.N_COND]["inputs"])
        self.assertNotIn("right", wf[srv3d.N_COND]["inputs"])
        self.assertEqual(wf[srv3d.N_SAMPLER]["inputs"]["seed"], 1)
        self.assertEqual(wf[srv3d.N_SAMPLER]["inputs"]["steps"], 20)

    def test_back_omitted_cuts_orphan_nodes(self):
        wf = _captured_wf(front="f.png", back=None, left=None, right=None,
                          steps=20, seed=None, octree_resolution=None)
        self.assertNotIn(srv3d.N_BACK, wf)          # LoadImage back вырезан
        self.assertNotIn(srv3d.N_ENC_BACK, wf)      # энкодер back вырезан
        self.assertNotIn("back", wf[srv3d.N_COND]["inputs"])
        self.assertNotIn(srv3d.N_FRONT, wf.pop(srv3d.N_FRONT) and {})  # front на месте

    def test_four_views_adds_encoders(self):
        wf = _captured_wf(front="f.png", back="b.png", left="l.png", right="r.png",
                          steps=20, seed=None, octree_resolution=None)
        cond = wf[srv3d.N_COND]["inputs"]
        self.assertEqual(cond["left"], ["enc_left", 0])
        self.assertEqual(cond["right"], ["enc_right", 0])
        self.assertEqual(wf["enc_left"]["class_type"], "CLIPVisionEncode")
        self.assertEqual(wf["enc_left"]["inputs"]["image"], "l.png")
        self.assertEqual(wf["enc_right"]["inputs"]["clip_vision"], ["54", 1])

    def test_octree_passthrough(self):
        wf = _captured_wf(front="f.png", back=None, left=None, right=None,
                          steps=20, seed=None, octree_resolution=384)
        self.assertEqual(wf["61"]["inputs"]["octree_resolution"], 384)


class FetchModelTests(unittest.TestCase):
    """Generic-скан outputs: находим файловый выход SaveGLB при любом ключе."""

    def _hist(self, key="glb"):
        return {"pid": {"outputs": {key: [{"filename": "mesh/ComfyUI_00001_.glb",
                                           "subfolder": "mesh", "type": "output"}]}}}

    def test_fetch_any_key(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)

        def fake_urlopen(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else req
            if "history" in url:
                return io.BytesIO(json.dumps(self._hist("3d")).encode())
            return io.BytesIO(b"GLBDATA")  # /view

        with mock.patch.object(srv3d, "MODEL_DIR", tmp), \
                mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            res = srv3d._fetch_model("pid")
        path, data = res
        self.assertEqual(data, b"GLBDATA")
        self.assertEqual(os.path.basename(path), "ComfyUI_00001_.glb")
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), b"GLBDATA")

    def test_fetch_none_when_no_outputs(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=lambda r, timeout=None:
                        io.BytesIO(b'{"pid": {"outputs": {}}}')):
            self.assertIsNone(srv3d._fetch_model("pid"))


class Convert3dTests(unittest.TestCase):

    def test_glb_to_stl_and_obj(self):
        import trimesh
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        glb = os.path.join(tmp, "model.glb")
        trimesh.creation.icosphere(subdivisions=1, radius=1.0).export(glb)
        res = srv3d._convert_3d(glb, ["stl", "obj"])
        self.assertEqual([r[0] for r in res], ["stl", "obj"])
        for fmt, dst, note in res:
            self.assertTrue(os.path.isfile(dst), dst)
            self.assertIn("faces", note)
        stl_note = res[0][2]
        self.assertTrue("watertight" in stl_note)  # сфера — watertight

    def test_missing_file_raises_clean(self):
        with self.assertRaises(Exception):
            srv3d._convert_3d("/nonexistent/m.glb", ["stl"])


class RemoveBackgroundTests(unittest.TestCase):
    """remove_background: фиксированные промты, валидация пути и режима."""

    def _call(self, args):
        import asyncio
        return asyncio.run(edit.call_tool("remove_background", args))

    def test_unknown_mode_rejected_before_job(self):
        res = self._call({"source_image": "/whatever/exists.png", "background": "green"})
        self.assertIn("Unknown background mode", res[0].text)

    def test_missing_source_rejected(self):
        res = self._call({"source_image": "/nonexistent/x.png", "background": "white"})
        self.assertIn("not found", res[0].text)

    def _call_remove(self, args, captured):
        import asyncio

        def fake_start(prompt, source, ref, steps, job_id, seed=None):
            captured["prompt"] = prompt
            edit.jobs[job_id]["prompt_id"] = "pid"   # как реальный _start_job
            return "pid"

        with mock.patch.object(edit, "_start_job", side_effect=fake_start), \
                mock.patch.object(edit, "_poll_job", return_value=None), \
                mock.patch.object(edit.jobstore, "put", return_value=None):
            return asyncio.run(edit.call_tool("remove_background", args))

    def test_white_mode_canned_prompt(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        src = os.path.join(tmp, "s.png")
        open(src, "wb").write(b"\x89PNG fake")
        captured = {}
        res = self._call_remove({"source_image": src, "background": "white"}, captured)
        self.assertIn("Task accepted", res[0].text)
        self.assertIn("pure white background", captured["prompt"])
        self.assertNotIn("no alpha", res[0].text)

    def test_transparent_mode_warns_no_alpha(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        src = os.path.join(tmp, "s.png")
        open(src, "wb").write(b"\x89PNG fake")
        captured = {}
        res = self._call_remove({"source_image": src, "background": "transparent"}, captured)
        self.assertIn("no alpha channel", res[0].text)
        self.assertIn("Remove the background completely", captured["prompt"])




class McpRegistrationTests(unittest.TestCase):
    """list_tools зарегистрирован декоратором — LM Studio падал с 'Method not
    found' (-32601), когда декоратор отсутствовал (функции работали, тесты
    звали их напрямую и не ловили)."""

    def test_list_tools_registered(self):
        import asyncio
        # декоратор регистрирует обработчик tools/list в Server — без него
        # LM Studio отвечает '-32601: Method not found'
        names = [t.__name__ for t in srv3d.app.request_handlers]
        self.assertIn("ListToolsRequest", names)
        self.assertIn("CallToolRequest", names)
        tools = asyncio.run(srv3d.list_tools())
        self.assertEqual([t.name for t in tools],
                         ["generate_3d", "check_image", "convert_3d"])


class McpRegistrationTests(unittest.TestCase):
    """list_tools зарегистрирован декоратором — LM Studio падал с 'Method not
    found' (-32601), когда декоратор отсутствовал (функции работали, тесты
    звали их напрямую и не ловили)."""

    def test_list_tools_registered(self):
        import asyncio
        # декоратор регистрирует обработчик tools/list в Server — без него
        # LM Studio отвечает '-32601: Method not found'
        names = [t.__name__ for t in srv3d.app.request_handlers]
        self.assertIn("ListToolsRequest", names)
        self.assertIn("CallToolRequest", names)
        tools = asyncio.run(srv3d.list_tools())
        self.assertEqual([t.name for t in tools],
                         ["generate_3d", "check_image", "convert_3d"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
