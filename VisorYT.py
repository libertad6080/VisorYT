import os
import tempfile

import streamlit as st
import yt_dlp

st.set_page_config(page_title="Reproductor de vídeo", page_icon="🎬")
st.title("🎬 Reproductor de vídeo")
st.caption("Pega una URL de YouTube y el vídeo se reproduce desde esta web, no desde YouTube.")

CACHE_DIR = os.path.join(tempfile.gettempdir(), "yt_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

MAX_DURACION = 60 * 60  # segundos; evita descargas enormes en el servidor


def es_url_de_video(url: str) -> bool:
    u = url.lower()
    return any(p in u for p in ("watch?v=", "youtu.be/", "/shorts/", "/live/", "/embed/"))


@st.cache_data(show_spinner=False, max_entries=5, ttl=3600)
def descargar(url: str, altura: int):
    opts = {
        # Vídeo y audio por separado, unidos con ffmpeg (ver packages.txt)
        "format": (
            f"bv*[height<={altura}][ext=mp4]+ba[ext=m4a]"
            f"/bv*[height<={altura}]+ba"
            f"/b[height<={altura}]/b"
        ),
        "merge_output_format": "mp4",
        # YouTube exige un runtime de JavaScript para listar todos los formatos
        "js_runtimes": {"node": {}},
        "outtmpl": os.path.join(CACHE_DIR, "%(id)s_" + str(altura) + ".%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }
    # Si tienes cookies (ver notas), descomenta:
    # opts["cookiefile"] = "cookies.txt"

    if not es_url_de_video(url):
        raise ValueError(
            "Esa URL no es de un vídeo concreto (parece un canal o una lista). "
            "Abre un vídeo en YouTube y copia su enlace: https://www.youtube.com/watch?v=..."
        )

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if info.get("_type") == "playlist":
            raise ValueError("La URL es una lista de vídeos; pega la de un solo vídeo.")
        if (info.get("duration") or 0) > MAX_DURACION:
            raise ValueError(f"El vídeo supera el máximo de {MAX_DURACION // 60} minutos.")
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


url = st.text_input("URL del vídeo de YouTube", placeholder="https://www.youtube.com/watch?v=...")
calidad = st.selectbox("Calidad máxima", [360, 480, 720], index=1, format_func=lambda x: f"{x}p")

if st.button("Cargar vídeo", type="primary") and url:
    try:
        with st.spinner("Descargando vídeo en el servidor..."):
            titulo, datos = descargar(url.strip(), calidad)
        st.subheader(titulo)
        st.video(datos, format="video/mp4")
        st.download_button("Descargar MP4", datos, file_name=f"{titulo}.mp4", mime="video/mp4")
    except Exception as e:
        st.error(f"No se pudo cargar el vídeo: {e}")
