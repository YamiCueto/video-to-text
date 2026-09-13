import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from webapp.server import create_app
from webapp.storage import JobStore, atomic_json, read_json


class WebAppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = create_app(self.root, run_worker=False, import_existing=False)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.headers = {"x-local-token": self.client.get("/api/config").json()["token"]}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def create(self, **fields):
        data = {"url": "https://youtu.be/jNQXAC9IVRw", **fields}
        return self.client.post("/api/jobs", data=data, headers=self.headers)

    def completed(self):
        job = self.create().json()
        result = {"text": "Texto original.", "language": "es", "segments": [
            {"start": 0, "end": 1.5, "text": "Texto original.", "uncertain": True}]}
        directory = self.app.state.store.directory(job["id"])
        atomic_json(directory / "original.json", result)
        atomic_json(directory / "result.json", result)
        self.app.state.store.update(job["id"], status="completed")
        return job

    def test_home_and_default_quality(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/api/config").json()["default_model"], "small")
        job = self.create().json()
        self.assertEqual(job["model"], "small")
        self.assertEqual(job["status"], "queued")

    def test_mutations_require_local_session(self):
        self.assertEqual(self.client.post("/api/jobs", data={"url": "https://youtu.be/jNQXAC9IVRw"}).status_code, 403)
        self.assertEqual(self.client.get("/api/jobs", headers={"host": "evil.example"}).status_code, 400)

    def test_reject_invalid_inputs(self):
        for fields in ({"url": "C:/secret.mp4"}, {"model": "missing"}, {"language": "missing"}, {"device": "missing"}, {"glossary": "x" * 1001}):
            with self.subTest(fields=fields):
                self.assertEqual(self.create(**fields).status_code, 422)
        both = self.client.post("/api/jobs", data={"url": "https://youtu.be/jNQXAC9IVRw"}, files={"file": ("test.wav", b"audio")}, headers=self.headers)
        self.assertEqual(both.status_code, 422)

    def test_upload_is_confined_and_rejects_empty(self):
        response = self.client.post("/api/jobs", files={"file": ("../../sample.wav", b"audio bytes", "audio/wav")}, headers=self.headers)
        self.assertEqual(response.status_code, 201)
        item = response.json()
        self.assertEqual(item["input_file"], "input.wav")
        self.assertEqual((self.app.state.store.directory(item["id"]) / "input.wav").read_bytes(), b"audio bytes")
        self.assertEqual(self.client.post("/api/jobs", files={"file": ("empty.wav", b"")}, headers=self.headers).status_code, 422)
        self.assertEqual(len(list((self.root / "jobs").iterdir())), 1)
        self.assertEqual(self.client.post("/api/jobs", files={"file": ("script.py", b"print(1)")}, headers=self.headers).status_code, 422)

    def test_cancel_and_unavailable_download(self):
        job = self.create().json()
        self.assertEqual(self.client.get(f"/api/jobs/{job['id']}/download/txt").status_code, 404)
        response = self.client.post(f"/api/jobs/{job['id']}/cancel", headers=self.headers)
        self.assertEqual(response.json()["status"], "cancelled")
        self.assertEqual(self.client.post(f"/api/jobs/{job['id']}/cancel", headers=self.headers).status_code, 409)
        self.assertEqual(self.client.get("/api/jobs/not-a-real-id").status_code, 404)

    def test_edits_preserve_original_and_srt_times(self):
        job = self.completed()
        body = {"revision": 0, "reviewed": True, "segments": [{"text": "Corrección: agénticos y LLM."}]}
        response = self.client.patch(f"/api/jobs/{job['id']}", json=body, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["reviewed"])
        self.assertEqual(self.client.get(f"/api/jobs/{job['id']}/download/txt").text, "Corrección: agénticos y LLM.\n")
        srt = self.client.get(f"/api/jobs/{job['id']}/download/srt").text
        self.assertIn("00:00:00,000 --> 00:00:01,500", srt)
        self.assertIn("Corrección: agénticos y LLM.", srt)
        directory = self.app.state.store.directory(job["id"])
        self.assertEqual((directory / "transcript.srt").read_text(encoding="utf-8"), srt)
        self.assertEqual(self.client.get(f"/api/jobs/{job['id']}/download/txt?original=true").text, "Texto original.\n")
        self.assertEqual(self.client.patch(f"/api/jobs/{job['id']}", json=body, headers=self.headers).status_code, 409)

    def test_edit_cannot_discard_timing_structure(self):
        job = self.completed()
        response = self.client.patch(f"/api/jobs/{job['id']}", json={"revision": 0, "segments": []}, headers=self.headers)
        self.assertEqual(response.status_code, 422)

    def test_history_survives_restart_and_running_is_interrupted(self):
        job = self.create().json()
        self.app.state.store.update(job["id"], status="running")
        reloaded = JobStore(self.root / "jobs")
        self.assertEqual(reloaded.get(job["id"])["status"], "failed")
        self.assertIn("Interrumpida", reloaded.get(job["id"])["stage"])

    def test_import_existing_results_is_idempotent(self):
        outputs = self.root / "outputs"
        outputs.mkdir()
        (outputs / "youtube-jNQXAC9IVRw.txt").write_text("Hola.", encoding="utf-8")
        (outputs / "youtube-jNQXAC9IVRw.srt").write_text("1\n00:00:00,000 --> 00:00:01,500\nHola.\n", encoding="utf-8")
        store = self.app.state.store
        store.import_outputs(outputs)
        store.import_outputs(outputs)
        self.assertEqual(len(store.all()), 1)
        self.assertEqual(store.all()[0]["duration"], 1.5)
        self.assertIn("jNQXAC9IVRw", store.all()[0]["url"])

    def test_audio_range_for_seeking(self):
        job = self.completed()
        directory = self.app.state.store.directory(job["id"])
        (directory / "listen.mp3").write_bytes(b"abcdefghij")
        response = self.client.get(f"/api/jobs/{job['id']}/audio", headers={"range": "bytes=2-5"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, b"cdef")

    def test_atomic_write_retries_windows_sharing_violation(self):
        path = self.root / "progress.json"
        original_replace = Path.replace
        calls = []

        def intermittent(source, destination):
            calls.append(destination)
            if len(calls) < 3:
                raise PermissionError("Windows sharing violation")
            return original_replace(source, destination)

        with patch.object(Path, "replace", intermittent), patch("webapp.storage.time.sleep"):
            atomic_json(path, {"progress": 50})
        self.assertEqual(read_json(path), {"progress": 50})
        self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()
