import re
import streamlit as st

st.set_page_config(page_title="Mi lista de vídeos", page_icon="▶️", layout="wide")

PATRON = re.compile(r"(https?://)?(www\.|m\.)?(youtube\.com/(watch\?v=|shorts/|live/)|youtu\.be/)[\w\-]{11}")

if "lista" not in st.session_state:
    st.session_state.lista = []  # [{"titulo": ..., "url": ...}]


def anadir(texto):
    nuevos = 0
    for linea in texto.splitlines():
        url = linea.strip()
        if not url:
            continue
        if not PATRON.search(url):
            st.sidebar.warning(f"No parece un enlace de YouTube: {url[:40]}")
            continue
        if url.startswith("youtube.") or url.startswith("www.") or url.startswith("youtu.be"):
            url = "https://" + url
        if all(v["url"] != url for v in st.session_state.lista):
            st.session_state.lista.append({"titulo": f"Vídeo {len(st.session_state.lista) + 1}", "url": url})
            nuevos += 1
    return nuevos


st.title("▶️ Mi lista de vídeos")

with st.sidebar:
    st.header("Añadir vídeos")
    texto = st.text_area("Pega uno o varios enlaces (uno por línea)", height=150)
    if st.button("➕ Añadir a la lista", type="primary", use_container_width=True):
        n = anadir(texto)
        st.success(f"{n} vídeo(s) añadido(s)") if n else st.info("No se ha añadido nada nuevo")

    if st.session_state.lista:
        st.divider()
        if st.button("🗑️ Vaciar lista", use_container_width=True):
            st.session_state.lista = []
            st.rerun()

lista = st.session_state.lista
if not lista:
    st.info("Pega un enlace de YouTube en la barra lateral para empezar.")
else:
    col_video, col_lista = st.columns([3, 1])
    with col_lista:
        st.subheader("Lista")
        idx = st.radio(
            "Elige un vídeo",
            range(len(lista)),
            format_func=lambda i: lista[i]["titulo"],
            label_visibility="collapsed",
        )
        nuevo_titulo = st.text_input("Renombrar", value=lista[idx]["titulo"], key=f"t_{idx}")
        if nuevo_titulo != lista[idx]["titulo"]:
            lista[idx]["titulo"] = nuevo_titulo
            st.rerun()
        if st.button("❌ Quitar este vídeo", use_container_width=True):
            lista.pop(idx)
            st.rerun()
    with col_video:
        if lista:
            st.subheader(lista[min(idx, len(lista) - 1)]["titulo"])
            st.video(lista[min(idx, len(lista) - 1)]["url"])

st.caption("La lista se guarda solo mientras tengas la página abierta.")
