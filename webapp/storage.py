"""Persistencia local, importación de resultados y cola de un solo proceso."""
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from transcribe import write_outputs

ROOT = Path(__file__).resolve().parents[1]
TERMINAL = {"completed", "failed", "cancelled"}


def now():
    return datetime.now(UTC).isoformat()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def atomic_json(path, data):
    path = Path(path)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    # Windows no permite reemplazar un archivo mientras otro proceso lo lee.
    for attempt in range(7):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 6:
                raise
            time.sleep(0.01 * 2 ** attempt)


def parse_srt(text):
    segments = []
    pattern = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+) --> (\d+):(\d+):(\d+)[,.](\d+)")
    for block in re.split(r"\n\s*\n", text.strip().replace("\r\n", "\n")):
        lines = block.splitlines()
        if len(lines) < 3:
            continue
        match = pattern.fullmatch(lines[1].strip())
        if not match:
            continue
        values = list(map(int, match.groups()))
        start = values[0] * 3600 + values[1] * 60 + values[2] + values[3] / 1000
        end = values[4] * 3600 + values[5] * 60 + values[6] + values[7] / 1000
        if end < start:
            continue
        segments.append({"start": start, "end": end, "text": " ".join(lines[2:]), "uncertain": False})
    return segments


class JobStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.jobs = {}
        for path in self.root.glob("*/job.json"):
            try:
                item = read_json(path)
                if item["status"] == "running":
                    item.update(status="failed", stage="Interrumpida al cerrar la aplicación", error="Vuelve a crear la transcripción para continuar.")
                    atomic_json(path, item)
                self.jobs[item["id"]] = item
                if item["status"] == "completed" and (not (path.parent / "transcript.txt").exists() or not (path.parent / "transcript.srt").exists()):
                    write_outputs(read_json(path.parent / "result.json"), [path.parent / "transcript.txt", path.parent / "transcript.srt"])
            except (OSError, ValueError, KeyError):
                continue

    def directory(self, job_id):
        if not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise KeyError(job_id)
        return self.root / job_id

    def add(self, item):
        with self.lock:
            directory = self.directory(item["id"])
            directory.mkdir(parents=True, exist_ok=True)
            atomic_json(directory / "job.json", item)
            self.jobs[item["id"]] = item.copy()

    def get(self, job_id):
        with self.lock:
            return self.jobs[job_id].copy()

    def update(self, job_id, **fields):
        with self.lock:
            item = self.get(job_id)
            item.update(fields, updated_at=now())
            self.add(item)
            return item

    def all(self):
        with self.lock:
            return sorted((item.copy() for item in self.jobs.values()), key=lambda j: j["created_at"], reverse=True)

    def import_outputs(self, output_root):
        for text_path in Path(output_root).rglob("*.txt"):
            relative = text_path.relative_to(output_root).as_posix()
            job_id = uuid.uuid5(uuid.NAMESPACE_URL, "video-to-text:" + relative).hex
            if job_id in self.jobs:
                continue
            try:
                text = text_path.read_text(encoding="utf-8").strip()
                subtitle_path = text_path.with_suffix(".srt")
                segments = parse_srt(subtitle_path.read_text(encoding="utf-8")) if subtitle_path.exists() else []
                if not segments:
                    segments = [{"start": 0, "end": 0, "text": text, "uncertain": False}]
                result = {"text": text, "segments": segments, "language": "unknown"}
                directory = self.directory(job_id)
                directory.mkdir(exist_ok=True)
                atomic_json(directory / "result.json", result)
                atomic_json(directory / "original.json", result)
                write_outputs(result, [directory / "transcript.txt", directory / "transcript.srt"])
                video = re.search(r"youtube-([A-Za-z0-9_-]{11})", text_path.stem)
                self.add({"id": job_id, "title": text_path.stem, "source_type": "import",
                          "url": f"https://www.youtube.com/watch?v={video[1]}" if video else "",
                          "model": "small" if "revision-small" in relative else "unknown",
                          "language": "unknown", "device": "unknown", "glossary": "",
                          "created_at": datetime.fromtimestamp(text_path.stat().st_mtime, UTC).isoformat(),
                          "updated_at": now(), "status": "completed", "stage": "Importada del workspace",
                          "progress": 100, "reviewed": False, "revision": 0,
                          "duration": segments[-1]["end"], "imported_from": relative, "error": None})
            except (OSError, ValueError):
                continue


class JobRunner:
    def __init__(self, store):
        self.store = store
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self.loop, daemon=True)

    def start(self):
        self.thread.start()

    @staticmethod
    def terminate(process):
        if process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=15)
        else:
            import signal
            os.killpg(process.pid, signal.SIGTERM)
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)

    def stop(self):
        self.stopping.set()
        self.thread.join(timeout=30)

    def loop(self):
        while not self.stopping.is_set():
            queued = [item for item in reversed(self.store.all()) if item["status"] == "queued"]
            if not queued:
                self.stopping.wait(0.5)
                continue
            item = queued[0]
            with self.store.lock:
                if self.store.get(item["id"])["status"] != "queued":
                    continue
                self.store.update(item["id"], status="running", stage="Preparando el entorno", progress=None)
            process = None
            try:
                directory = self.store.directory(item["id"])
                environment = os.environ.copy()
                environment.update(PYTHONUTF8="1", TMP=str(self.store.root.parent / "tmp"), TEMP=str(self.store.root.parent / "tmp"))
                options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
                with (directory / "worker.log").open("w", encoding="utf-8") as log:
                    process = subprocess.Popen([sys.executable, "-m", "webapp.worker", str(directory)],
                                               cwd=ROOT, env=environment, stdout=log, stderr=log, **options)
                    while process.poll() is None:
                        with self.store.lock:
                            cancelled = self.store.get(item["id"])["status"] == "cancelled"
                            if not cancelled and not self.stopping.is_set():
                                try:
                                    progress = read_json(directory / "progress.json")
                                    self.store.update(item["id"], **progress)
                                except (OSError, ValueError):
                                    pass
                        if cancelled or self.stopping.is_set():
                            self.terminate(process)
                            break
                        self.stopping.wait(0.5)
                with self.store.lock:
                    if self.store.get(item["id"])["status"] == "cancelled":
                        continue
                    if process.returncode == 0 and (directory / "result.json").exists():
                        result = read_json(directory / "result.json")
                        self.store.update(item["id"], status="completed", stage="Lista para revisar", progress=100,
                                          duration=result["segments"][-1]["end"] if result["segments"] else 0,
                                          language=result.get("language", item["language"]), error=None)
                    else:
                        try:
                            error = read_json(directory / "error.json")["error"]
                        except (OSError, ValueError, KeyError):
                            error = "El proceso se interrumpió. Revisa el entorno o prueba CPU / un modelo más pequeño."
                        self.store.update(item["id"], status="failed", stage="No se pudo completar", error=error, progress=None)
            except Exception as error:
                if process is not None and process.poll() is None:
                    self.terminate(process)
                self.store.update(item["id"], status="failed", stage="Error de procesamiento", error=str(error), progress=None)
