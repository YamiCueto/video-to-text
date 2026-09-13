"""Inicia la aplicación en http://127.0.0.1:8765."""
import argparse
import os
from pathlib import Path


def lock_workspace():
    directory = Path(__file__).resolve().parent / ".local"
    directory.mkdir(exist_ok=True)
    handle = (directory / "server.lock").open("a+b")
    handle.seek(0)
    if not handle.read(1):
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise SystemExit("La app ya está abierta en este workspace. Abre http://127.0.0.1:8765 en tu navegador.")
    return handle

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--stop", action="store_true", help="Cerrar la instancia local en el puerto indicado")
    args = parser.parse_args()
    if args.stop:
        import json
        from urllib.error import URLError
        from urllib.request import Request, urlopen
        address = f"http://127.0.0.1:{args.port}"
        try:
            with urlopen(address + "/api/config", timeout=5) as response:
                token = json.load(response)["token"]
            request = Request(address + "/api/shutdown", data=b"", headers={"x-local-token": token}, method="POST")
            with urlopen(request, timeout=5) as response:
                print(json.load(response)["message"])
        except URLError:
            raise SystemExit("No se pudo cerrar la app. Comprueba que esté abierta en ese puerto.")
        raise SystemExit(0)
    os.environ["PYTHONUTF8"] = "1"
    with lock_workspace():
        import uvicorn

        from webapp.server import create_app
        app = create_app()
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port, access_log=False))
        app.state.shutdown_callback = lambda: setattr(server, "should_exit", True)
        server.run()
