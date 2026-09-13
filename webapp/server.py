"""API y frontend servidos únicamente en la máquina local."""
from contextlib import asynccontextmanager
import importlib.metadata
from pathlib import Path
import secrets
import tempfile
import uuid

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from transcribe import render_outputs, write_outputs, youtube_id
from webapp.storage import JobRunner, JobStore, ROOT, TERMINAL, atomic_json, now, read_json

MODELS = {"tiny": "Rápido · menor precisión", "base": "Ligero", "small": "Equilibrado · recomendado", "medium": "Más detalle · mayor consumo", "turbo": "Alta capacidad · mayor consumo"}
LANGUAGES = {"auto": "Detectar idioma", "es": "Español", "en": "Inglés", "pt": "Portugués", "fr": "Francés", "de": "Alemán", "it": "Italiano"}
EXTENSIONS = {".mp3", ".mp4", ".m4a", ".wav", ".mkv", ".webm", ".mov", ".flac", ".ogg", ".aac"}
MAX_UPLOAD = 1024 * 1024 * 1024


class EditedSegment(BaseModel):
    text: str = Field(max_length=20000)


class Revision(BaseModel):
    revision: int = Field(ge=0)
    segments: list[EditedSegment] = Field(max_length=20000)
    reviewed: bool = False


def create_app(data_root=None, *, run_worker=True, import_existing=True):
    data_root = Path(data_root or ROOT / ".local")
    store = JobStore(data_root / "jobs")
    temp_root = data_root / "tmp"
    temp_root.mkdir(parents=True, exist_ok=True)
    if import_existing:
        store.import_outputs(ROOT / "outputs")
    runner = JobRunner(store)
    token = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app):
        previous_temp = tempfile.tempdir
        tempfile.tempdir = str(temp_root)
        if run_worker:
            runner.start()
        yield
        if run_worker:
            runner.stop()
        tempfile.tempdir = previous_temp

    app = FastAPI(title="Video to Text · Local", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.store = store
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        if request.method in {"POST", "PATCH", "DELETE", "PUT"}:
            if request.headers.get("x-local-token") != token:
                return JSONResponse({"detail": "Sesión local expirada. Recarga la página."}, status_code=403)
            content_length = request.headers.get("content-length", "0")
            if not content_length.isdecimal() or int(content_length) > MAX_UPLOAD + 1024 * 1024:
                return JSONResponse({"detail": "El archivo supera el límite de 1 GB."}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; media-src 'self' blob:; img-src 'self' data:; frame-ancestors 'none'; object-src 'none'"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def get_job(job_id):
        try:
            return store.get(job_id)
        except KeyError:
            raise HTTPException(404, "No se encontró la transcripción.")

    def detail(item):
        directory = store.directory(item["id"])
        item = item.copy()
        item["has_audio"] = (directory / "listen.mp3").is_file()
        if item["status"] == "completed":
            result = read_json(directory / "result.json")
            original = read_json(directory / "original.json")
            item.update(result=result, original_segments=original["segments"])
        return item

    @app.get("/api/config")
    def config():
        return {"token": token, "models": [{"id": model, "label": label, "downloaded": (ROOT / "models" / f"{model}.pt").exists()} for model, label in MODELS.items()],
                "languages": LANGUAGES, "default_model": "small", "max_upload_mb": 1024,
                "version": "1.0", "whisper_version": importlib.metadata.version("openai-whisper")}

    @app.get("/api/jobs")
    def jobs():
        return store.all()

    @app.post("/api/shutdown")
    def shutdown(background: BackgroundTasks):
        callback = getattr(app.state, "shutdown_callback", None)
        if callback is None:
            raise HTTPException(409, "Detén este servidor desde la terminal donde lo iniciaste.")
        background.add_task(callback)
        return {"message": "Cerrando la aplicación local."}

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str):
        with store.lock:
            return detail(get_job(job_id))

    @app.post("/api/jobs", status_code=201)
    async def create_job(url: str = Form(""), file: UploadFile | None = File(None),
                         model: str = Form("small"), language: str = Form("es"),
                         device: str = Form("auto"), glossary: str = Form(""), title: str = Form("")):
        url = url.strip()
        if file and not file.filename:
            await file.close()
            file = None
        if bool(url) == bool(file and file.filename):
            raise HTTPException(422, "Pega un enlace o selecciona un archivo, solo una fuente a la vez.")
        if model not in MODELS or language not in LANGUAGES or device not in {"auto", "cpu", "cuda"}:
            raise HTTPException(422, "La configuración de modelo, idioma o dispositivo no es válida.")
        if len(glossary) > 1000 or len(title) > 160:
            raise HTTPException(422, "El título o el vocabulario superan el límite permitido.")
        if sum(item["status"] not in TERMINAL for item in store.all()) >= 10:
            raise HTTPException(429, "La cola tiene 10 trabajos. Espera a que termine alguno.")
        video_id = None
        suffix = None
        if url:
            try:
                video_id = youtube_id(url)
                if video_id is None:
                    raise ValueError("Pega un enlace completo de YouTube que empiece por https://.")
            except ValueError as error:
                raise HTTPException(422, str(error))
        else:
            suffix = Path(file.filename).suffix.lower()
            if suffix not in EXTENSIONS:
                raise HTTPException(422, "Formato no admitido. Utiliza un archivo de audio o video.")
        job_id = uuid.uuid4().hex
        directory = store.directory(job_id)
        directory.mkdir()
        input_file = None
        if file:
            input_file = "input" + suffix
            try:
                size = 0
                with (directory / input_file).open("wb") as destination:
                    while chunk := await file.read(1024 * 1024):
                        size += len(chunk)
                        if size > MAX_UPLOAD:
                            raise HTTPException(413, "El archivo supera el límite de 1 GB.")
                        destination.write(chunk)
                if size == 0:
                    raise HTTPException(422, "El archivo está vacío.")
            except Exception:
                # Solo se elimina el archivo parcial creado por esta petición.
                (directory / input_file).unlink(missing_ok=True)
                directory.rmdir()
                raise
            finally:
                await file.close()
        item = {"id": job_id, "title": title.strip() or (f"YouTube · {video_id}" if video_id else Path(file.filename).stem[:160]),
                "url": f"https://www.youtube.com/watch?v={video_id}" if video_id else "", "input_file": input_file,
                "source_type": "youtube" if video_id else "file", "model": model, "language": language,
                "device": device, "glossary": glossary.strip(), "created_at": now(), "updated_at": now(),
                "status": "queued", "stage": "En cola", "progress": None, "reviewed": False,
                "revision": 0, "duration": None, "error": None}
        store.add(item)
        return item

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(job_id: str):
        with store.lock:
            item = get_job(job_id)
            if item["status"] in TERMINAL:
                raise HTTPException(409, "La transcripción ya terminó.")
            return store.update(job_id, status="cancelled", stage="Cancelada", progress=None)

    @app.patch("/api/jobs/{job_id}")
    def edit(job_id: str, body: Revision):
        with store.lock:
            item = get_job(job_id)
            if item["status"] != "completed":
                raise HTTPException(409, "La transcripción todavía no está lista para editar.")
            if body.revision != item["revision"]:
                raise HTTPException(409, "La transcripción cambió en otra pestaña. Recarga antes de editar.")
            directory = store.directory(job_id)
            result = read_json(directory / "result.json")
            if len(body.segments) != len(result["segments"]):
                raise HTTPException(422, "Debes conservar los fragmentos para mantener los tiempos de los subtítulos.")
            for segment, edited in zip(result["segments"], body.segments):
                segment["text"] = " ".join(edited.text.split())
            result["text"] = " ".join(segment["text"] for segment in result["segments"] if segment["text"])
            atomic_json(directory / "result.json", result)
            write_outputs(result, [directory / "transcript.txt", directory / "transcript.srt"])
            return detail(store.update(job_id, reviewed=body.reviewed, revision=item["revision"] + 1))

    @app.get("/api/jobs/{job_id}/download/{extension}")
    def download(job_id: str, extension: str, original: bool = False):
        with store.lock:
            item = get_job(job_id)
            if item["status"] != "completed" or extension not in {"txt", "srt"}:
                raise HTTPException(404, "El archivo no está disponible.")
            result = read_json(store.directory(job_id) / ("original.json" if original else "result.json"))
            content = render_outputs(result)[0 if extension == "txt" else 1]
            return Response(content, media_type="text/plain; charset=utf-8",
                            headers={"Content-Disposition": f'attachment; filename="transcripcion-{job_id[:8]}{"-original" if original else ""}.{extension}"'})

    @app.get("/api/jobs/{job_id}/audio")
    def audio(job_id: str):
        get_job(job_id)
        path = store.directory(job_id) / "listen.mp3"
        if not path.is_file():
            raise HTTPException(404, "Esta transcripción no tiene audio guardado.")
        return FileResponse(path, media_type="audio/mpeg")

    app.mount("/static", StaticFiles(directory=ROOT / "webapp" / "static"), name="static")

    @app.get("/")
    def index():
        return FileResponse(ROOT / "webapp" / "static" / "index.html")

    return app
