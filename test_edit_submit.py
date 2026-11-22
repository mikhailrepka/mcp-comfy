#!/usr/bin/env python3
"""Unit-тесты _submit/_prompt_error_text edit-сервера.

Фикс 2026-11-23: без reference_image узел NODE_REF вырезается из графа
(иначе LoadImage с дефолтным именем из шаблона даёт 400 'Invalid image file',
если файла нет во входной папке PC), а 400 от /prompt разбирается в читаемую
ошибку вместо голого "HTTP Error 400".

Запуск: ~/comfy-mcp/.venv/bin/python ~/comfy-mcp/test_edit_submit.py
Сеть не используется: urlopen патчится.
"""
import io
import json
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, "/Users/mikhailrepka/comfy-mcp")

import edit_mcp_server as edit


def _captured_prompt():
    """_submit с перехваченным urlopen: возвращает распарсенный payload /prompt."""
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["data"] = json.loads(req.data.decode())
        return io.BytesIO(b'{"prompt_id": "test-pid"}')

    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        edit._submit("test prompt", "uploaded_src.png", None, 25)
    return captured["data"]["prompt"]


def _http_error(body: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://pc/prompt", 400, "Bad Request",
        {"Content-Type": "application/json"}, io.BytesIO(body))


class SubmitGraphTests(unittest.TestCase):
    """Без reference_image референсный узел вырезан вместе со ссылками."""

    def test_no_ref_node_removed_and_no_dangling_refs(self):
        prompt = _captured_prompt()
        self.assertNotIn(edit.NODE_REF, prompt)
        for nid, node in prompt.items():
            for k, v in node.get("inputs", {}).items():
                self.assertFalse(
                    isinstance(v, list) and v[0] == edit.NODE_REF,
                    f"висячая ссылка на {edit.NODE_REF}: {nid}.{k}")
        self.assertEqual(prompt[edit.NODE_SOURCE]["inputs"]["image"], "uploaded_src.png")
        self.assertEqual(prompt[edit.NODE_PROMPT]["inputs"]["prompt"], "test prompt")
        self.assertIsInstance(prompt[edit.NODE_SAMPLER]["inputs"]["seed"], int)

    def test_ref_still_wired_when_provided(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["data"] = json.loads(req.data.decode())
            return io.BytesIO(b'{"prompt_id": "test-pid"}')

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            edit._submit("p", "uploaded_src.png", "uploaded_ref.png", 25)
        wf = captured["data"]["prompt"]
        self.assertEqual(wf[edit.NODE_REF]["inputs"]["image"], "uploaded_ref.png")
        # потребители референса на месте
        self.assertEqual(wf["459:474"]["inputs"]["images.image_2"], [edit.NODE_REF, 0])
        self.assertEqual(wf["459:485"]["inputs"]["images.image1"], [edit.NODE_REF, 0])


class PromptErrorTextTests(unittest.TestCase):
    """400 от /prompt превращается в читаемую ошибку с узлом и деталями."""

    BODY = json.dumps({
        "error": {"type": "prompt_outputs_failed_validation",
                  "message": "Prompt outputs failed validation", "details": ""},
        "node_errors": {"475": {"errors": [{
            "message": "Custom validation failed for node: image",
            "details": "Invalid image file: clothing_light_blue_denim_shirt.png",
        }], "dependent_outputs": ["461"]}},
    }).encode()

    def test_node_error_parsed(self):
        msg = edit._prompt_error_text(_http_error(self.BODY))
        self.assertIn("node 475", msg)
        self.assertIn("Invalid image file: clothing_light_blue_denim_shirt.png", msg)

    def test_unreadable_body_falls_back_to_code(self):
        msg = edit._prompt_error_text(_http_error(b"<html>oops</html>"))
        self.assertEqual(msg, "ComfyUI rejected the prompt: HTTP 400")

    def test_submit_raises_parsed_error(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=_http_error(self.BODY)) as url:
            with self.assertRaisesRegex(RuntimeError, "node 475"):
                edit._submit("p", "src.png", None, 25)
            url.assert_called_once()


class EditSeedTests(unittest.TestCase):
    """seed в edit-сервере: проброс в workflow и строка в результате."""

    def _run_submit(self, *args, **kw):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["data"] = json.loads(req.data.decode())
            return io.BytesIO(b'{"prompt_id": "test-pid"}')

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            ret = edit._submit(*args, **kw)
        return ret, captured["data"]["prompt"][edit.NODE_SAMPLER]["inputs"]["seed"]

    def test_seed_fixed(self):
        (_, seed), wf_seed = self._run_submit("p", "src.png", None, 25, seed=777)
        self.assertEqual((seed, wf_seed), (777, 777))

    def test_seed_random_when_absent(self):
        (_, seed), wf_seed = self._run_submit("p", "src.png", None, 25)
        self.assertEqual(seed, wf_seed)
        self.assertTrue(0 <= seed < 2**32)

    def test_seed_in_result_text(self):
        content = edit._content_for_result(
            {"status": "done", "path": "/x/y.png", "data": b"\x89PNG", "seed": 42})
        texts = [i.text for i in content if getattr(i, "type", None) == "text"]
        self.assertEqual(texts[0], "Edited image: /x/y.png\nSeed: 42")


if __name__ == "__main__":
    unittest.main(verbosity=2)
