import glob
import os
import re
import shutil
import tempfile

import streamlit as st
import yt_dlp

st.set_page_config(page_title="Reproductor de vídeo", page_icon="🎬")
st.title("🎬 Reproductor de vídeo")
st.caption("Pega una URL de YouTube y se muestra el reproductor al instante. La descarga solo se prepara si pulsas el botón.")

CACHE_DIR = os.path.join(tempfile.gettempdir(), "yt_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

MAX_DURACION = 60 * 60  # segundos; evita descargas enormes en el servidor


def es_url_de_video(url: str) -> bool:
    u = url.lower()
    return any(p in u for p in ("watch?v=", "youtu.be/", "/shorts/", "/live/", "/embed/"))


class LogCaptura:
    """Guarda avisos y errores de yt-dlp para poder mostrar la causa real."""

    def __init__(self):
        self.mensajes = []

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        self.mensajes.append(str(msg))

    def error(self, msg):
        self.mensajes.append(str(msg))


def ruta_cookies():
    try:
        cookies = st.secrets.get("youtube_cookies")
    except Exception:
        cookies = None
    if not cookies:
        return None
    ruta = os.path.join(CACHE_DIR, "cookies.txt")
    with open(ruta, "w") as f:
        f.write(cookies.strip() + "\n")
    return ruta


def formato(altura: int) -> str:
    return (
        f"bv*[height<={altura}][ext=mp4]+ba[ext=m4a]"
        f"/bv*[height<={altura}]+ba"
        f"/b[height<={altura}]"
        f"/bv*+ba/b"  # último recurso: lo mejor disponible
    )


def opciones_base(altura: int, log: LogCaptura) -> dict:
    return {
        "format": formato(altura),
        "merge_output_format": "mp4",
        "js_runtimes": {"deno": {}},
        # Permite descargar el solucionador de desafíos JS si no está instalado
        "remote_components": ["ejs:github"],
        "outtmpl": os.path.join(CACHE_DIR, "%(id)s_" + str(altura) + ".%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": False,
        "logger": log,
        "retries": 1,
        "extractor_retries": 1,
        "sleep_interval_requests": 1,
    }


def estrategias(cookies):
    """android_vr normalmente no exige PO token y no usa cookies.
    Despues el cliente por defecto, y las cookies solo como ultimo respaldo."""
    lista = [
        ("android_vr (sin cookies)",
         {"extractor_args": {"youtube": {"player_client": ["android_vr"]}}}),
        ("cliente por defecto (sin cookies)", {}),
    ]
    if cookies:
        lista.append(("cookies + cliente por defecto", {"cookiefile": cookies}))
    return lista


@st.cache_data(show_spinner=False, max_entries=5, ttl=3600)
def descargar(url: str, altura: int):
    if not es_url_de_video(url):
        raise ValueError(
            "Esa URL no es de un vídeo concreto (parece un canal o una lista). "
            "Abre un vídeo en YouTube y copia su enlace: https://www.youtube.com/watch?v=..."
        )

    cookies = ruta_cookies()
    informe = []  # (estrategia, error, avisos)

    for nombre, extra in estrategias(cookies):
        log = LogCaptura()
        opts = opciones_base(altura, log)
        opts.update(extra)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if info.get("_type") == "playlist":
                    raise ValueError("La URL es una lista de vídeos; pega la de un solo vídeo.")
                if (info.get("duration") or 0) > MAX_DURACION:
                    raise ValueError(f"El vídeo supera el máximo de {MAX_DURACION // 60} minutos.")

                reales = [f for f in info.get("formats", [])
                          if f.get("vcodec") not in (None, "none") or f.get("acodec") not in (None, "none")]
                if not reales:
                    raise yt_dlp.utils.DownloadError(
                        "YouTube solo devolvió miniaturas (sin formatos de vídeo/audio)."
                    )

                info = ydl.extract_info(url, download=True)
                descargas = info.get("requested_downloads") or []
                ruta = descargas[0]["filepath"] if descargas else ydl.prepare_filename(info)

            with open(ruta, "rb") as f:
                datos = f.read()
            try:
                os.remove(ruta)
            except OSError:
                pass
            return info.get("title", "Vídeo"), datos

        except ValueError:
            raise  # errores de validación propios: no reintentar
        except Exception as e:
            informe.append((nombre, str(e), log.mensajes[-3:]))

    detalle = "\n\n".join(
        f"• **{n}**: {e}" + (("\n  - " + "\n  - ".join(av)) if av else "")
        for n, e, av in informe
    )
    raise RuntimeError("Ninguna estrategia funcionó:\n\n" + detalle)


@st.cache_data(show_spinner=False, max_entries=10, ttl=3600)
def subtitulos_es(url: str):
    """Devuelve (titulo, tipo, idioma, extension, datos) o None si no hay subtitulos en espanol."""
    if not es_url_de_video(url):
        raise ValueError("Esa URL no es de un vídeo concreto.")

    cookies = ruta_cookies()
    errores = []
    for nombre, extra in estrategias(cookies):
        log = LogCaptura()
        base = opciones_base(360, log)
        base.update(extra)
        base.update({"skip_download": True, "format": None})
        try:
            with yt_dlp.YoutubeDL({**base, "ignore_no_formats_error": True}) as ydl:
                info = ydl.extract_info(url, download=False)

            manuales = [k for k in (info.get("subtitles") or {}) if k.startswith("es")]
            automaticos = [k for k in (info.get("automatic_captions") or {}) if k.startswith("es")]
            if manuales:
                tipo, idioma = "manual", ("es" if "es" in manuales else manuales[0])
            elif automaticos:
                tipo, idioma = "automático", ("es" if "es" in automaticos else automaticos[0])
            else:
                return None

            con_ffmpeg = shutil.which("ffmpeg") is not None
            opts = {
                **base,
                "ignore_no_formats_error": True,
                "writesubtitles": tipo == "manual",
                "writeautomaticsub": tipo != "manual",
                "subtitleslangs": [idioma],
                "subtitlesformat": "srt/vtt/best",
                "outtmpl": os.path.join(CACHE_DIR, "sub_%(id)s.%(ext)s"),
            }
            if con_ffmpeg:
                opts["postprocessors"] = [{"key": "FFmpegSubtitlesConvertor", "format": "srt", "when": "before_dl"}]
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])

            archivos = glob.glob(os.path.join(CACHE_DIR, f"sub_{info['id']}.{idioma}.*"))
            if not archivos:
                raise RuntimeError("yt-dlp no generó ningún archivo de subtítulos.")
            archivo = sorted(archivos, key=lambda a: not a.endswith(".srt"))[0]
            with open(archivo, "rb") as f:
                datos = f.read()
            ext = os.path.splitext(archivo)[1].lstrip(".")
            for a in archivos:
                try:
                    os.remove(a)
                except OSError:
                    pass
            return info.get("title", "Vídeo"), tipo, idioma, ext, datos
        except ValueError:
            raise
        except Exception as e:
            errores.append(f"• **{nombre}**: {e}" + ("\n  - " + "\n  - ".join(log.mensajes[-2:]) if log.mensajes else ""))

    raise RuntimeError("No se pudieron obtener los subtítulos:\n\n" + "\n\n".join(errores))


# --- Interfaz ---------------------------------------------------------------
try:
    _c = st.secrets.get("youtube_cookies")
except Exception:
    _c = None
if _c:
    _n = sum(1 for l in _c.splitlines() if l.strip() and not l.startswith("#"))
    st.caption(f"🍪 Cookies detectadas: {_n} líneas")
else:
    st.caption("🍪 No se han detectado cookies en Secrets")

st.caption("⚙️ Motor JS (deno): " + ("✅ " + shutil.which("deno") if shutil.which("deno") else "❌ no encontrado")
           + " · ffmpeg: " + ("✅" if shutil.which("ffmpeg") else "❌ no encontrado"))

url = st.text_input(
    "URL del vídeo de YouTube", placeholder="https://www.youtube.com/watch?v=..."
).strip()


def nombre_seguro(titulo: str) -> str:
    limpio = re.sub(r'[\\/:*?"<>|]+', "", titulo).strip()
    return limpio or "video"


if url and not es_url_de_video(url):
    st.warning(
        "Esa URL no es de un vídeo concreto (parece un canal o una lista). "
        "Copia el enlace de un vídeo: https://www.youtube.com/watch?v=..."
    )

if url and es_url_de_video(url):
    # Reproductor incrustado: aparece al instante, sin descargar nada
    st.video(url)

    st.divider()
    calidad = st.selectbox("Calidad de la descarga", [360, 480, 720], index=1, format_func=lambda x: f"{x}p")

    # La descarga solo se hace si el usuario la pide
    if st.button("⬇️ Preparar descarga MP4", type="primary"):
        try:
            with st.spinner("Preparando el archivo en el servidor..."):
                titulo, datos = descargar(url, calidad)
            st.session_state["video"] = {"url": url, "calidad": calidad, "titulo": titulo, "datos": datos}
        except Exception as e:
            st.session_state.pop("video", None)
            st.error(f"No se pudo preparar la descarga: {e}")
            st.caption(f"yt-dlp versión: {yt_dlp.version.__version__}")

    listo = st.session_state.get("video")
    if listo and listo["url"] == url and listo["calidad"] == calidad:
        st.success(f"Listo: {listo['titulo']}")
        st.download_button(
            "💾 Guardar MP4",
            listo["datos"],
            file_name=f"{nombre_seguro(listo['titulo'])}.mp4",
            mime="video/mp4",
            type="primary",
        )

    st.divider()
    if st.button("📝 Buscar subtítulos en español"):
        try:
            with st.spinner("Buscando subtítulos..."):
                res = subtitulos_es(url)
            st.session_state["subs"] = {"url": url, "res": res}
        except Exception as e:
            st.session_state.pop("subs", None)
            st.error(f"No se pudieron obtener los subtítulos: {e}")

    subs = st.session_state.get("subs")
    if subs and subs["url"] == url:
        if subs["res"] is None:
            st.info("Este vídeo no tiene subtítulos en español.")
        else:
            titulo_s, tipo, idioma, ext, datos_s = subs["res"]
            st.success(f"Subtítulos en español ({tipo}, {idioma}) disponibles")
            if tipo == "automático":
                st.caption("Son subtítulos generados o traducidos automáticamente, pueden tener errores.")
            st.download_button(
                f"💾 Guardar subtítulos (.{ext})",
                datos_s,
                file_name=f"{nombre_seguro(titulo_s)}.{idioma}.{ext}",
                mime="application/x-subrip" if ext == "srt" else "text/vtt",
                type="primary",
            )
