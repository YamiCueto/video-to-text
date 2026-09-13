# Video to text

Aplicación web local para transcribir, escuchar y corregir audio. Todo el código,
los modelos y la biblioteca viven en este workspace. También conserva la CLI.

## Abrir la app web

Con las dependencias instaladas, ejecuta desde la terminal de VS Code:

```powershell
.\.venv\Scripts\python.exe run_web.py
```

Abre **http://127.0.0.1:8765**. También puedes hacer doble clic en `start-web.cmd`.
Para detenerla, pulsa `Ctrl+C` en su terminal o ejecuta `stop-web.cmd`
(equivale a `.\.venv\Scripts\python.exe run_web.py --stop`). Cerrar una pestaña
no detiene el servidor. Con `--port 8766` puedes escoger otro puerto; usa el mismo
puerto junto a `--stop`. Solo se admite una instancia por workspace.

### Flujo de trabajo

1. Pega un enlace de YouTube o selecciona/arrastra un archivo local (hasta 1 GB).
2. Elige el idioma. La web usa **small** por defecto; la CLI conserva `tiny`.
3. En **Personalizar transcripción**, añade un título y vocabulario técnico si
   lo necesitas. Por ejemplo: `LLM, Codex, Claude, singleton, circuit breaker`.
   Este vocabulario guía el reconocimiento, pero no es un corrector automático.
4. Pulsa **Transcribir**. Verás las etapas de descarga, preparación, carga del
   modelo y transcripción. El porcentaje es de la etapa actual, no del proceso
   completo. La carga/descarga inicial del modelo muestra progreso indeterminado.
5. Abre el resultado en la biblioteca. Reproduce el audio o pulsa el tiempo de
   un fragmento para escucharlo. Corrige el texto y compara con el original.
6. Guarda y descarga **TXT** o **SRT**. Las correcciones afectan a ambos formatos
   sin cambiar los tiempos. Si exportas con cambios pendientes, primero se guardan.

La cola procesa un trabajo a la vez para evitar competir por memoria de GPU.
Puedes cancelarlo. Las transcripciones terminadas y las correcciones sobreviven
a reinicios; una transcripción interrumpida no continúa a mitad del audio, sino
que queda marcada como fallida para crear otra versión. Los trabajos en cola sí
se retoman al arrancar. No se eliminan automáticamente los archivos de la biblioteca.

Al iniciar, se incorporan una sola vez los TXT/SRT existentes de `outputs/`.
No se modifican esos originales. Las importaciones no incluyen audio ni registran
un modelo/idioma que no podamos determinar. Usa **Crear otra versión** para volver
a procesar el enlace y obtener audio para la revisión.

### Organización dentro del workspace

```text
run_web.py             Arranque y apagado local
start-web.cmd          Acceso rápido en Windows
stop-web.cmd           Cierre de la app
webapp/server.py       API HTTP y archivos del frontend
webapp/storage.py      Historial y cola persistente
webapp/worker.py       Procesamiento aislado por transcripción
webapp/static/         HTML, CSS y JavaScript sin compilación ni CDN
.local/jobs/<id>/      Metadatos, fuente, audio de escucha y resultados
.local/tmp/            Archivos temporales de la web
models/                Modelos de Whisper compartidos con la CLI
outputs/               Resultados previos de la CLI
```

Cada trabajo nuevo conserva `original.json` y `result.json` por separado, además
de `transcript.txt`, `transcript.srt`, `listen.mp3` y un registro `worker.log`.
Los archivos descargados desde el navegador se guardan donde tú elijas en Chrome.
`.local/`, `models/` y `outputs/` están excluidos de Git.

La app utiliza FastAPI y un frontend en JavaScript. Escucha solo en `127.0.0.1`,
sin publicación externa, login ni servicios de transcripción de pago. YouTube
y la primera descarga de cada modelo requieren internet. No está preparada
como servicio multiusuario ni para exponerse directamente en una red pública.

## Uso por terminal (CLI)

Transcribe enlaces de YouTube y archivos locales de audio o video con Whisper. Genera texto UTF-8 (`.txt`)
y subtítulos con tiempos (`.srt`) en `outputs/`. Selecciona CUDA cuando está disponible
y CPU en los demás casos. No necesita una clave de API.

## Preparación en Windows / Visual Studio Code

Abre esta carpeta en VS Code y utiliza su terminal PowerShell. Requiere Python 3.11
y FFmpeg. Los comandos invocan directamente el entorno; no necesitas activarlo.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
winget install --id Gyan.FFmpeg --exact
.\.venv\Scripts\python.exe transcribe.py --check
```

Si `.venv` ya existe, omite su creación. Si no tiene pip, puedes utilizar
`uv pip install --python .venv\Scripts\python.exe -r requirements.txt`.
El programa busca FFmpeg en PATH y en su instalación de WinGet, sin modificar
la configuración global de Windows. También acepta `--ffmpeg-dir "C:\ruta\bin"`.

Para instalar la variante de PyTorch con CUDA 12.8:

```powershell
.\.venv\Scripts\python.exe -m pip install torch==2.11.0 --index-url https://download.pytorch.org/whl/cu128
```

La combinación se basa en las [instrucciones oficiales de PyTorch](https://pytorch.org/get-started/previous-versions/).
Las versiones directas están fijadas; las dependencias transitivas todavía no
están bloqueadas. El entorno inicial contenía PyTorch de desarrollo y paquetes
de descarga: instalar los requisitos no elimina paquetes adicionales.

## Transcribir

### Desde YouTube

```powershell
.\.venv\Scripts\python.exe transcribe.py "https://www.youtube.com/watch?v=ID_DEL_VIDEO" --language es
.\.venv\Scripts\python.exe transcribe.py "https://youtu.be/ID_DEL_VIDEO" --language es --model base
```

Reemplaza `ID_DEL_VIDEO` por el ID real de 11 caracteres o pega el enlace completo.
Se aceptan enlaces `watch`, `youtu.be`, `shorts`, `live` (grabaciones finalizadas)
y `embed`. Si el enlace incluye una lista, solo se procesa el video indicado.
Las listas sin video y los canales se rechazan.

El flujo es **YouTube → yt-dlp → audio temporal → Whisper → TXT y SRT**.
Se utiliza [yt-dlp](https://github.com/yt-dlp/yt-dlp#embedding-yt-dlp) como único
descargador; `pytube` no es necesario. Se elige audio sin video cuando está
disponible; en caso contrario se descarga un formato que incluya ambos.
No se utilizan los subtítulos existentes de YouTube: Whisper transcribe el audio.
Los resultados se llaman `outputs/youtube-ID_DEL_VIDEO.txt` y `.srt`.
El audio temporal se elimina al terminar, incluso si falla la transcripción.
Los parámetros de tiempo del enlace no recortan el audio: se procesa completo.

### Desde un archivo local

```powershell
.\.venv\Scripts\python.exe transcribe.py "C:\videos\clase.mp4" --language es
.\.venv\Scripts\python.exe transcribe.py "C:\audios\reunion.wav" --model base --device cpu
```

El modelo predeterminado es `tiny`. Puedes seleccionar `base` o `small` para
evaluar la calidad con tus audios. Sin `--language`, Whisper detecta el idioma.
La primera ejecución descarga el modelo a `models/`; las siguientes reutilizan
ese archivo. Se necesita internet para esa descarga, pero el audio se procesa
localmente. `--model-dir` permite usar otra carpeta de modelos existentes.

Por ejemplo, `clase.mp4` produce `outputs/clase.txt` y `outputs/clase.srt`.
No se reemplazan resultados existentes salvo que pases `--overwrite`.
Para archivos con el mismo nombre, utiliza distintos `--output-dir`.

```powershell
.\.venv\Scripts\python.exe transcribe.py --help
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## Alcance y errores habituales

- YouTube requiere internet; videos privados, eliminados o restringidos pueden fallar.
  No se admiten directos en curso ni estrenos futuros, ni se leen cookies del navegador.
- Si YouTube deja de descargar, revisa el error de yt-dlp. Su integración puede
  requerir actualizar la versión fijada en `requirements.txt` y reinstalarla;
  consulta sus [dependencias oficiales](https://github.com/yt-dlp/yt-dlp#dependencies)
  si informa que falta un motor JavaScript.
  El script habilita Deno y Node.js si están instalados en PATH.
- FFmpeg no disponible: instala FFmpeg o especifica su carpeta con `--ffmpeg-dir`.
- CUDA sin memoria: prueba `--device cpu` o un modelo más pequeño.
- Archivo ilegible o sin audio: se informa el error de decodificación.
- Las transcripciones automáticas deben revisarse, especialmente con ruido o silencio.
- `outputs/`, `models/` y archivos multimedia están excluidos de Git. Los demás
  `.txt` sí pueden versionarse. La carpeta todavía no tiene repositorio Git inicializado.

## Validación realizada

- Web: pruebas de creación, carga de archivos, permisos locales, cancelación,
  edición con conservación del original, versiones concurrentes, importación,
  reinicio del historial, reproducción por rangos y reintentos de escritura en Windows.
- Pruebas reales de la web: YouTube con `small`/CUDA y archivo WAV con `tiny`/CPU;
  reproducción y guardado de correcciones en Chrome; diseño a 390 px y escritorio.
  La selección automatizada de archivos en Chrome está limitada por el permiso
  de acceso a archivos de su extensión; la carga multipart se verificó en la API.
- Diez pruebas automatizadas: exportación, protección de archivos, validación de
  enlaces, rechazo de directos, errores de descarga y flujo URL → TXT/SRT con
  descarga y modelo simulados, incluyendo limpieza temporal ante errores.
- Prueba real de YouTube con el video público `jNQXAC9IVRw`: descarga de audio
  WebM y generación de TXT/SRT con `tiny` en CUDA. La precisión del texto sigue
  dependiendo del modelo y del audio; esta prueba comprueba el flujo técnico.
- Comprobación de dependencias con `uv pip check`: sin incompatibilidades declaradas.
- Ejecución completa con Python 3.11.16, Whisper 20250625, PyTorch 2.11.0+cu128,
  CUDA y FFmpeg: se generaron TXT y SRT a partir de una voz sintética.
- La frase de prueba fue `Hello. This is a test of local audio transcription.`;
  `tiny` produjo `Hello. This is the end of the video.`. Esto valida la ejecución
  y exportación, pero muestra que la precisión requiere evaluación con audio real
  y posiblemente un modelo mayor. No se ha validado la calidad en español.
