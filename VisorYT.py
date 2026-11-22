import glob
import html
import json
import os
import re
import shutil
import tempfile
import time

import streamlit as st
import streamlit.components.v1 as components
import yt_dlp

st.set_page_config(page_title="Reproductor de vídeo", page_icon="🎬")
st.title("🎬 Reproductor de vídeo")
st.caption("Pega una URL de YouTube y se muestra el reproductor al instante. La descarga solo se prepara si pulsas el botón.")

CACHE_DIR = os.path.join(tempfile.gettempdir(), "yt_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

VERSION_SUBS = "subs-v8-autoclick"  # cambia este valor para forzar a ignorar la caché

MAX_DURACION = 60 * 60  # segundos; evita descargas enormes en el servidor


def es_url_de_video(url: str) -> bool:
    u = url.lower()
    return any(p in u for p in ("watch?v=", "youtu.be/", "/shorts/", "/live/", "/embed/"))


def sin_ansi(txt) -> str:
    """Quita los códigos de color de la terminal (aparecen como [0;31m) de los mensajes de yt-dlp."""
    return re.sub(r"\x1b?\[[0-9;]*m", "", str(txt))


def con_reintentos(fn, esperas=(6, 20, 45)):
    """Ejecuta fn(); si YouTube responde 429 (demasiadas peticiones), espera y vuelve a intentarlo."""
    for i in range(len(esperas) + 1):
        try:
            return fn()
        except Exception as e:
            if "429" not in str(e) or i == len(esperas):
                raise
            time.sleep(esperas[i])


try:
    import curl_cffi  # noqa: F401
    CURL_CFFI = True
    CURL_VERSION = getattr(curl_cffi, "__version__", "")
except Exception as _e:
    CURL_CFFI = False
    CURL_VERSION = ""
    CURL_MOTIVO = str(_e)


class LogCaptura:
    """Guarda avisos y errores de yt-dlp para poder mostrar la causa real."""

    def __init__(self):
        self.mensajes = []

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        self.mensajes.append(sin_ansi(msg))

    def error(self, msg):
        self.mensajes.append(sin_ansi(msg))


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


def proxy_configurado():
    try:
        return st.secrets.get("youtube_proxy") or None
    except Exception:
        return None


def opciones_base(altura: int, log: LogCaptura) -> dict:
    opts = {
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
    proxy = proxy_configurado()
    if proxy:
        opts["proxy"] = proxy
    return opts


def estrategias(cookies):
    """Primero sin cookies; con cookies solo como respaldo."""
    lista = [("cliente por defecto (sin cookies)", {})]
    if cookies:
        lista.append(("cookies + cliente por defecto", {"cookiefile": cookies}))
    return lista


def estrategias_subs(cookies):
    """Para subtítulos: con cookies primero (YouTube limita menos a sesiones con cuenta) y luego sin ellas."""
    lista = []
    if cookies:
        lista.append(("cookies + cliente por defecto", {"cookiefile": cookies}))
    lista.append(("cliente por defecto (sin cookies)", {}))
    return lista


def proxy_subs():
    """Proxy solo para subtítulos (pesan pocos KB): youtube_proxy_subs, o youtube_proxy si no existe."""
    try:
        return st.secrets.get("youtube_proxy_subs") or st.secrets.get("youtube_proxy") or None
    except Exception:
        return None


def explicar_error(detalle: str) -> str:
    """Traduce el error técnico a qué hacer."""
    t = detalle.lower()
    consejos = []
    if "impersonat" in t or not CURL_CFFI:
        consejos.append(
            "⚠️ **Falta `curl-cffi`** (YouTube lo necesita para entregar los subtítulos). "
            "Pon `yt-dlp[default,curl-cffi]` en `requirements.txt` y reinicia la app "
            "(Manage app → Reboot). Arriba debe aparecer «curl-cffi: ✅»."
        )
    if "429" in t:
        consejos.append(
            "⏳ YouTube está limitando las peticiones de este servidor (429). "
            "Espera unos minutos y vuelve a pulsar. Si pasa siempre: añade las cookies en Secrets "
            "(`youtube_cookies`) o un proxy residencial solo para subtítulos (`youtube_proxy_subs`). "
            "Ejecutar la app en tu propio ordenador también lo evita, porque usa tu IP de casa."
        )
    return "\n\n".join(consejos)


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
            informe.append((nombre, sin_ansi(e), log.mensajes[-3:]))

    detalle = "\n\n".join(
        f"• **{n}**: {e}" + (("\n  - " + "\n  - ".join(av)) if av else "")
        for n, e, av in informe
    )
    raise RuntimeError("Ninguna estrategia funcionó:\n\n" + detalle)


def _t_a_seg(t: str) -> float:
    seg = 0.0
    for parte in t.replace(",", ".").split(":"):
        seg = seg * 60 + float(parte)
    return seg


def _seg_a_srt(seg: float) -> str:
    ms = int(round(max(seg, 0) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    sg, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{sg:02d},{ms:03d}"


def _dos_lineas(txt: str, ancho: int = 42) -> str:
    """Parte el texto en dos líneas equilibradas si es largo."""
    if len(txt) <= ancho:
        return txt
    medio = len(txt) // 2
    cortes = [i for i in (txt.rfind(" ", 0, medio + 1), txt.find(" ", medio)) if i > 0]
    if not cortes:
        return txt
    corte = min(cortes, key=lambda i: abs(i - medio))
    return txt[:corte] + "\n" + txt[corte + 1:]


def _palabras_vtt(texto: str) -> list:
    """Convierte subtítulos (VTT/SRT) en párrafos de 1-2 líneas, sincronizados y sin solapes.

    Usa los tiempos reales de cada palabra (los subtítulos automáticos de YouTube en
    VTT los incluyen), quita las líneas repetidas del efecto 'rodillo' y corta los
    párrafos en las pausas del habla, en el fin de oración o al llegar al límite.
    """
    texto = texto.replace("\ufeff", "").replace("\r", "")
    patron = re.compile(r"((?:\d+:)?\d+:\d+[.,]\d+)\s*-->\s*((?:\d+:)?\d+:\d+[.,]\d+)")
    marca = re.compile(r"<(\d+:\d+:\d+[.,]\d+)>")

    # 1) Leer bloques; guardar el texto limpio y el original (con marcas de tiempo)
    cues = []
    for bloque in re.split(r"\n\s*\n", texto):
        lineas = bloque.split("\n")
        idx = next((i for i, l in enumerate(lineas) if patron.search(l)), None)
        if idx is None:
            continue
        m = patron.search(lineas[idx])
        ini, fin = _t_a_seg(m.group(1)), _t_a_seg(m.group(2))
        items = []
        for l in lineas[idx + 1:]:
            plano = html.unescape(re.sub(r"<[^>]+>", "", l))
            plano = re.sub(r"\s+", " ", plano).strip()
            if plano:
                items.append((plano, l))
        if items:
            cues.append((ini, fin, items))
    cues.sort(key=lambda c: c[0])

    # 2) Quitar las líneas que se repiten de un bloque al siguiente (efecto rodillo)
    frags = []
    prev_plain, prev_ids, prev_fin = [], [], -1e9
    for ini, fin, items in cues:
        plain = [p for p, _ in items]
        k = 0
        if ini - prev_fin < 0.1:  # solo bloques contiguos
            for n in range(min(len(prev_plain), len(plain)), 0, -1):
                if prev_plain[-n:] == plain[:n]:
                    k = n
                    break
        ids = list(prev_ids[len(prev_ids) - k:]) if k else []
        for i in ids:
            frags[i]["fin"] = max(frags[i]["fin"], fin)
        for p, raw in items[k:]:
            frags.append({"ini": ini, "fin": fin, "txt": p, "raw": raw})
            ids.append(len(frags) - 1)
        prev_plain, prev_ids, prev_fin = plain, ids, fin

    # 3) Tiempo de cada palabra: el real si el VTT lo trae, estimado si no
    palabras = []  # (inicio, fin_del_bloque, palabra)
    ultimo = 0.0
    for i, f in enumerate(frags):
        sig = frags[i + 1]["ini"] if i + 1 < len(frags) else f["fin"]
        partes = marca.split(f["raw"])
        pares = []
        if len(partes) > 1:
            t = f["ini"]
            for j, parte in enumerate(partes):
                if j % 2:
                    t = _t_a_seg(parte)
                else:
                    txt = html.unescape(re.sub(r"<[^>]+>", "", parte))
                    for w in txt.split():
                        pares.append((t, w))
        else:
            ws = f["txt"].split()
            fin_ef = min(f["fin"], max(sig, f["ini"] + 0.05))
            dur = max(fin_ef - f["ini"], 0.05)
            pares = [(f["ini"] + dur * j / len(ws), w) for j, w in enumerate(ws)]
        for t, w in pares:
            t = max(t, ultimo)
            ultimo = t
            palabras.append((t, f["fin"], w))
    return palabras


def _palabras_json3(texto: str) -> list:
    """Palabras con su instante exacto, leídas del formato json3 (el que usa el reproductor)."""
    datos = json.loads(texto)
    palabras = []
    eventos = [e for e in datos.get("events", []) if e.get("segs")]
    eventos.sort(key=lambda e: e.get("tStartMs", 0))
    ultimo = 0.0
    for ev in eventos:
        base = ev.get("tStartMs", 0) / 1000
        fin_ev = base + ev.get("dDurationMs", 0) / 1000
        items = []  # (desplazamiento_seg, palabra)
        for sg in ev["segs"]:
            off = sg.get("tOffsetMs", 0) / 1000
            for w in (sg.get("utf8") or "").replace("\n", " ").split():
                items.append((off, w))
        if not items:
            continue
        if len({o for o, _ in items}) <= 1 and len(items) > 1:
            # sin tiempos por palabra: repartir a lo largo del evento
            dur = max(fin_ev - base, 0.05)
            items = [(dur * j / len(items), w) for j, (_, w) in enumerate(items)]
        for off, w in items:
            t = max(base + off, ultimo)
            ultimo = t
            palabras.append((t, max(fin_ev, t), w))
    return palabras


INICIOS_ES = {
    "ella", "él", "el", "yo", "tú", "tu", "nosotros", "ellos", "ellas", "usted", "ustedes", "no", "sí", "y",
    "pero", "si", "que", "cuando", "como", "porque", "ya", "ahora", "hoy", "entonces", "aquí", "allí",
    "así", "mi", "mis", "su", "sus", "un", "una", "la", "las", "los", "lo", "me", "te", "se", "nos", "qué",
    "quién", "cómo", "dónde", "cuándo", "por", "para", "en", "con", "sin", "hay", "era", "fue", "estaba",
    "vamos", "dime", "ven", "mira", "oye", "dios", "todo", "todos", "nada", "nunca", "siempre", "tan",
    "aunque", "mientras", "después", "antes", "luego", "también", "tal", "esta", "este", "eso", "esa",
    "ese", "había", "tenía", "quiero", "dame", "déjame", "sé", "voy", "vas", "va", "al", "del",
}
ENLACE_ES = {"el", "la", "los", "las", "un", "una", "unos", "unas", "de", "del", "al", "con",
             "y", "e", "o", "a", "en", "que", "su", "sus", "mi", "mis", "tu", "tus", "por",
             "para", "como", "desde", "se", "me", "te", "lo", "le", "les", "sin", "sobre", "entre"}

# Inglés: la "I" va siempre en mayúscula, por eso no se usa como inicio de oración
INICIOS_EN = {
    "the", "and", "but", "so", "then", "we", "you", "he", "she", "they", "it", "this", "that", "there",
    "here", "what", "when", "where", "why", "how", "if", "as", "because", "now", "well", "yeah", "yes",
    "no", "oh", "okay", "ok", "let", "let's", "my", "our", "your", "his", "her", "their", "a", "an", "in",
    "on", "at", "for", "with", "just", "also", "all", "one", "some", "there's", "it's", "that's", "we're",
    "you're", "they're", "he's", "she's", "i'm", "i've", "i'll", "i'd", "don't", "can't", "won't",
}
ENLACE_EN = {"the", "a", "an", "of", "to", "in", "on", "at", "for", "and", "or", "but", "with", "by",
             "from", "as", "into", "about", "my", "your", "his", "her", "its", "our", "their", "than",
             "if", "when", "while", "because"}

# idioma -> (palabras que inician oración si van en mayúscula, palabras de enlace)
CONFIG_IDIOMA = {"es": (INICIOS_ES, ENLACE_ES), "en": (INICIOS_EN, ENLACE_EN)}

IDIOMAS = {"en": "inglés", "fr": "francés", "de": "alemán", "it": "italiano", "pt": "portugués",
           "ja": "japonés", "ko": "coreano", "zh": "chino", "ru": "ruso", "ca": "catalán",
           "nl": "neerlandés", "pl": "polaco", "tr": "turco", "ar": "árabe", "hi": "hindi"}


def _inicio_oracion(w: str, prev: str, inicios=INICIOS_ES) -> bool:
    """YouTube pone mayúscula al empezar una oración: es la mejor pista de dónde empieza una."""
    if prev and re.search(r"[.!?…]+['\")»”]*$", prev):
        return True
    w0 = w.lstrip("¿¡\"'(“«")
    return w0[:1].isupper() and w0.lower().strip(",.;:!?…") in inicios


def _agrupar(palabras: list, idioma: str = "es") -> str:
    """Convierte palabras con tiempo en bloques de hasta 2 líneas (un verso/frase por línea).

    - Las marcas como [Música] no cuentan como palabras ni como pausas: se ponen al final del verso.
    - Un verso empieza en una mayúscula de inicio de oración, o tras una pausa larga del habla.
    - Los versos muy largos se parten por su mejor pausa; las palabras sueltas se unen a su verso.
    - Cada bloque junta hasta 2 versos cercanos y desaparece cuando llega el siguiente.
    """
    PAUSA_FRASE = 1.5    # segundos de silencio que separan dos versos
    TOL_MARCA = 12.0     # si hay una marca [..] en medio, se tolera una pausa de hasta esto
    MAX_LINEA = 54       # caracteres máximos de un verso al partirlo (sin contar marcas)
    MAX_UNION = 72       # caracteres máximos al unir una palabra suelta con su verso
    JUNTAR_HASTA = 3.0   # pausa máxima para poner dos versos en el mismo bloque
    inicios, PALABRAS_ENLACE = CONFIG_IDIOMA.get(idioma, (set(), set()))

    # 0) Unir marcas como "[Música]" o "[Música de fondo]" en un solo token
    tokens = []  # (t, fe, texto, es_marca)
    i = 0
    while i < len(palabras):
        t, fe, w = palabras[i]
        if w.startswith("["):
            partes, j = [w], i
            while not partes[-1].endswith("]") and j + 1 < len(palabras) and len(partes) < 6:
                j += 1
                partes.append(palabras[j][2])
            if partes[-1].endswith("]"):
                tokens.append((t, fe, " ".join(partes), True))
                i = j + 1
                continue
        tokens.append((t, fe, w, False))
        i += 1

    def nueva(ws, marcas, oracion=False):
        return {"words": ws, "marcas": list(marcas), "t0": ws[0][0], "tl": ws[-1][0],
                "fe": ws[-1][1], "solo": False, "oracion": oracion}

    def recalcular(f):
        ws = f["words"]
        f["t0"], f["tl"], f["fe"] = ws[0][0], ws[-1][0], ws[-1][1]

    def largo(ws):
        return len(" ".join(x[2] for x in ws))

    def unir_marcas(a, b):
        res = list(a)
        for m in b:
            if not res or res[-1] != m:
                res.append(m)
        return res

    # 1) Construir versos: mayúscula de inicio de oración o pausa larga
    frases, cur, marca_hueco, prev_w = [], None, False, ""
    for t, fe, w, es_marca in tokens:
        if es_marca:
            if cur is not None and t - cur["tl"] <= 6.0:
                if not cur["marcas"] or cur["marcas"][-1] != w:
                    cur["marcas"].append(w)
                cur["words"][-1][3] = True  # hay una marca justo después de esta palabra
                cur["fe"] = max(cur["fe"], fe)
                marca_hueco = True
            else:
                if cur is not None:
                    frases.append(cur)
                    cur = None
                frases.append({"words": [], "marcas": [w], "t0": t, "tl": t, "fe": fe,
                               "solo": True, "oracion": False})
            continue
        inicio = _inicio_oracion(w, prev_w, inicios)
        if cur is not None:
            gap = t - cur["tl"]
            if inicio or (gap > PAUSA_FRASE and not (marca_hueco and gap <= TOL_MARCA)):
                frases.append(cur)
                cur = None
        if cur is None:
            cur = nueva([[t, fe, w, False]], [], inicio)
        else:
            cur["words"].append([t, fe, w, False])
            cur["tl"], cur["fe"] = t, fe
        marca_hueco = False
        prev_w = w
    if cur is not None:
        frases.append(cur)

    # 2) Partir los versos demasiado largos por su mejor pausa (sin cortar donde hay una marca)
    def partir(f):
        ws = f["words"]
        if f["solo"] or len(ws) < 6 or largo(ws) <= MAX_LINEA:
            return [f]
        mejor, mejor_score = None, -1e9
        for k in range(3, len(ws) - 2):
            if ws[k - 1][3]:
                continue
            izq, der = largo(ws[:k]), largo(ws[k:])
            if izq < 15 or der < 12:
                continue
            score = (ws[k][0] - ws[k - 1][0]) - abs(izq - der) / 200
            if ws[k - 1][2].lower().strip(",.;") in PALABRAS_ENLACE:
                score -= 0.5  # no terminar una línea en "la", "con", "y"...
            if score > mejor_score:
                mejor, mejor_score = k, score
        if mejor is None:
            return [f]
        # la marca va con el trozo donde apareció, no siempre con el último
        idx_marca = max((k for k, x in enumerate(ws) if x[3]), default=len(ws) - 1)
        marcas_izq = f["marcas"] if idx_marca < mejor else []
        marcas_der = [] if idx_marca < mejor else f["marcas"]
        return partir(nueva(ws[:mejor], marcas_izq, f["oracion"])) + partir(nueva(ws[mejor:], marcas_der))

    versos = []
    for f in frases:
        versos.extend(partir(f))

    # 3) Palabras de enlace al final de un verso ("por", "con", "y"...): pasan al verso siguiente
    for n in range(len(versos) - 1):
        a_, b_ = versos[n], versos[n + 1]
        while (not a_["solo"] and not b_["solo"] and len(a_["words"]) >= 3 and not a_["marcas"]
               and not b_["oracion"] and b_["t0"] - a_["tl"] <= JUNTAR_HASTA
               and a_["words"][-1][2].lower().strip(",.;:") in PALABRAS_ENLACE):
            b_["words"].insert(0, a_["words"].pop())
            recalcular(a_)
            recalcular(b_)

    # 4) Palabras sueltas (1-2 palabras): se unen al verso al que pertenecen
    def pequeno(f):
        return (not f["solo"]) and (len(f["words"]) <= 2 or largo(f["words"]) < 14)

    n = 0
    while n < len(versos):
        f = versos[n]
        if not pequeno(f):
            n += 1
            continue
        prev_v = versos[n - 1] if n > 0 and not versos[n - 1]["solo"] else None
        sig_v = versos[n + 1] if n + 1 < len(versos) and not versos[n + 1]["solo"] else None
        orden = ["sig", "prev"] if f["oracion"] else ["prev", "sig"]
        hecho = False
        for lado in orden:
            if lado == "prev" and prev_v is not None:
                if f["t0"] - prev_v["tl"] <= 8.0 and largo(prev_v["words"]) + largo(f["words"]) + 1 <= MAX_UNION:
                    prev_v["words"].extend(f["words"])
                    prev_v["marcas"] = unir_marcas(prev_v["marcas"], f["marcas"])
                    recalcular(prev_v)
                    del versos[n]
                    n = max(n - 1, 0)
                    hecho = True
                    break
            if lado == "sig" and sig_v is not None:
                if sig_v["t0"] - f["tl"] <= 8.0 and largo(sig_v["words"]) + largo(f["words"]) + 1 <= MAX_UNION:
                    sig_v["words"] = f["words"] + sig_v["words"]
                    sig_v["marcas"] = unir_marcas(f["marcas"], sig_v["marcas"])
                    sig_v["oracion"] = f["oracion"]
                    recalcular(sig_v)
                    del versos[n]
                    hecho = True
                    break
        if not hecho:
            n += 1

    # 5) Juntar de dos en dos los versos cercanos
    bloques, act = [], []
    for f in versos:
        if (act and len(act) < 2 and not f["solo"] and not act[-1]["solo"]
                and f["t0"] - act[-1]["tl"] <= JUNTAR_HASTA):
            act.append(f)
        else:
            if act:
                bloques.append(act)
            act = [f]
    if act:
        bloques.append(act)

    def texto_verso(f):
        return " ".join([x[2] for x in f["words"]] + f["marcas"]).strip()

    # 6) Escribir SRT: cada bloque desaparece cuando llega el siguiente
    salida = []
    for n, grupo in enumerate(bloques):
        ini = grupo[0]["t0"]
        ult = grupo[-1]
        fin = max(ult["tl"] + 0.6, min(ult["tl"] + 2.0, ult["fe"]))
        if n + 1 < len(bloques):
            sig = bloques[n + 1][0]["t0"]
            fin = sig if sig - fin < 1.0 else min(fin, sig)
        fin = max(fin, ini + 0.3)
        lineas = [texto_verso(f) for f in grupo]
        txt = _dos_lineas(lineas[0], 44) if len(lineas) == 1 else "\n".join(lineas)
        salida.append(f"{n + 1}\n{_seg_a_srt(ini)} --> {_seg_a_srt(fin)}\n{txt}\n")
    return "\n".join(salida)


def limpiar_subtitulos(texto: str, idioma: str = "es") -> str:
    """Convierte subtítulos (json3, VTT o SRT) en párrafos de 1-2 líneas, sincronizados y sin solapes."""
    texto = texto.lstrip("\ufeff")
    if texto.lstrip().startswith("{"):
        try:
            palabras = _palabras_json3(texto)
        except Exception:
            palabras = []
    else:
        palabras = _palabras_vtt(texto)
    return _agrupar(palabras, idioma) if palabras else ""


def elegir_pistas(info: dict) -> dict:
    """Decide qué subtítulos hay: la pista original y (si el vídeo no es en español) la traducida al español."""
    manual = list((info.get("subtitles") or {}).keys())
    auto = list((info.get("automatic_captions") or {}).keys())
    orig = next((k for k in auto if k.endswith("-orig")), None)
    lang = orig[:-5] if orig else (info.get("language") or None)
    if not lang and manual:
        lang = manual[0]
    base = lang.split("-")[0].lower() if lang else None

    def manual_de(pref):
        c = [k for k in manual if k.split("-")[0].lower() == pref]
        return ("manual", pref if pref in c else c[0]) if c else None

    def auto_es():
        c = [k for k in auto if k.split("-")[0].lower() == "es" and not k.endswith("-orig")]
        return ("automático", "es" if "es" in c else c[0]) if c else None

    pista_es = manual_de("es") or auto_es() or (("automático", orig) if orig and base == "es" else None)

    if base in (None, "es"):
        return {"base": "es", "es_video": True, "original": pista_es, "es": pista_es}

    original = manual_de(base)
    if not original and orig:
        original = ("automático", orig)
    if not original:
        c = [k for k in auto if k.split("-")[0].lower() == base]
        original = ("automático", base if base in c else c[0]) if c else None
    return {"base": base, "es_video": False, "original": original, "es": pista_es}


@st.cache_data(show_spinner=False, max_entries=10, ttl=3600)
def info_subs(url: str, version: str = ""):
    """Detecta el idioma del vídeo y qué subtítulos tiene."""
    if not es_url_de_video(url):
        raise ValueError("Esa URL no es de un vídeo concreto.")

    cookies = ruta_cookies()
    errores = []
    for nombre, extra in estrategias_subs(cookies):
        log = LogCaptura()
        opts = opciones_base(360, log)
        opts.update(extra)
        if proxy_subs():
            opts["proxy"] = proxy_subs()
        opts.update({"skip_download": True, "format": None, "ignore_no_formats_error": True})
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = con_reintentos(lambda: ydl.extract_info(url, download=False))
            res = elegir_pistas(info)
            res.update({"titulo": info.get("title", "Vídeo"), "id": info["id"]})
            return res
        except ValueError:
            raise
        except Exception as e:
            errores.append(f"• **{nombre}**: {sin_ansi(e)}" + ("\n  - " + "\n  - ".join(log.mensajes[-2:]) if log.mensajes else ""))
    detalle = "\n\n".join(errores)
    raise RuntimeError(explicar_error(detalle) + "\n\n**No se pudo consultar el vídeo:**\n\n" + detalle)


@st.cache_data(show_spinner=False, max_entries=20, ttl=3600)
def descargar_subs(url: str, video_id: str, tipo: str, clave: str, idioma_limpieza: str, version: str = ""):
    """Descarga una pista de subtítulos y la reorganiza en párrafos. Devuelve (extension, datos)."""
    cookies = ruta_cookies()
    errores = []
    for nombre, extra in estrategias_subs(cookies):
        log = LogCaptura()
        base = opciones_base(360, log)
        base.update(extra)
        if proxy_subs():
            base["proxy"] = proxy_subs()
        opts = {
            **base,
            "skip_download": True,
            "format": None,
            "ignore_no_formats_error": True,
            "writesubtitles": tipo == "manual",
            "writeautomaticsub": tipo != "manual",
            "subtitleslangs": [clave],
            "sleep_interval_subtitles": 2,
            "subtitlesformat": "json3/vtt/srt/best",
            "outtmpl": os.path.join(CACHE_DIR, "sub_%(id)s.%(ext)s"),
        }
        patron = os.path.join(CACHE_DIR, f"sub_{video_id}.{clave}.*")
        for viejo in glob.glob(patron):  # evitar que yt-dlp reutilice un archivo antiguo
            try:
                os.remove(viejo)
            except OSError:
                pass
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                con_reintentos(lambda: ydl.download([url]))
            archivos = glob.glob(patron)
            if not archivos:
                raise RuntimeError("yt-dlp no generó ningún archivo de subtítulos.")
            orden = {".json3": 0, ".vtt": 1, ".srt": 2}
            archivo = sorted(archivos, key=lambda a: orden.get(os.path.splitext(a)[1], 9))[0]
            ext = os.path.splitext(archivo)[1].lstrip(".")
            with open(archivo, "rb") as f:
                original = f.read()
            for a in archivos:
                try:
                    os.remove(a)
                except OSError:
                    pass
            limpio = limpiar_subtitulos(original.decode("utf-8", errors="replace"), idioma_limpieza)
            if limpio.strip():
                return "srt", limpio.encode("utf-8")
            if ext in ("vtt", "srt"):
                return ext, original
            raise RuntimeError("No se pudo interpretar el archivo de subtítulos.")
        except Exception as e:
            errores.append(f"• **{nombre}**: {sin_ansi(e)}" + ("\n  - " + "\n  - ".join(log.mensajes[-2:]) if log.mensajes else ""))
    detalle = "\n\n".join(errores)
    raise RuntimeError(explicar_error(detalle) + "\n\n**No se pudieron obtener los subtítulos:**\n\n" + detalle)


def boton_guardar(etiqueta: str, datos: bytes, nombre: str, mime: str, key: str):
    """Botón normal de guardar. Con on_click='ignore' (Streamlit reciente) no recarga la página al pulsarlo."""
    try:
        return st.download_button(etiqueta, datos, file_name=nombre, mime=mime, key=key, on_click="ignore")
    except TypeError:
        return st.download_button(etiqueta, datos, file_name=nombre, mime=mime, key=key)


def pulsar_automaticamente(etiqueta: str):
    """Pulsa por ti el botón de guardar en cuanto aparece, para que la descarga empiece sola.

    Se hace desde la propia página (no desde un cuadro aislado), que es lo que permite el navegador;
    además el archivo no viaja dentro de la página, así que sirve también para vídeos grandes.
    """
    components.html(
        f"""<script>
        (function () {{
          const etiqueta = {json.dumps(etiqueta)};
          let intentos = 0;
          const t = setInterval(function () {{
            intentos++;
            try {{
              const doc = window.parent.document;
              const botones = doc.querySelectorAll('[data-testid="stDownloadButton"] button, [data-testid="stDownloadButton"] a');
              for (const b of botones) {{
                if ((b.textContent || "").includes(etiqueta)) {{ clearInterval(t); b.click(); return; }}
              }}
            }} catch (e) {{ clearInterval(t); }}
            if (intentos > 60) clearInterval(t);
          }}, 250);
        }})();
        </script>""",
        height=0,
    )


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

st.caption(("🌐 Proxy activo · " if proxy_configurado() else "") + "⚙️ Motor JS (deno): " + ("✅ " + shutil.which("deno") if shutil.which("deno") else "❌ no encontrado")
           + " · ffmpeg: " + ("✅" if shutil.which("ffmpeg") else "❌ no encontrado")
           + " · curl-cffi: " + (("✅ " + CURL_VERSION) if CURL_CFFI else "❌"))
if not CURL_CFFI:
    st.warning(
        "curl-cffi no está instalado y es necesario para bajar subtítulos. "
        "Añade `yt-dlp[default,curl-cffi]` a requirements.txt (en la raíz del repositorio) y reinicia la app. "
        f"Motivo técnico: {CURL_MOTIVO}"
    )

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

    # La descarga solo se hace si el usuario la pide y se guarda sola al terminar
    if st.button("⬇️ Descargar MP4", type="primary"):
        try:
            with st.spinner("Preparando el vídeo en el servidor... puede tardar un rato; se descargará solo al terminar."):
                titulo, datos = descargar(url, calidad)
            st.session_state["video"] = {"url": url, "calidad": calidad, "titulo": titulo,
                                         "datos": datos, "auto": True, "n": time.time_ns()}
        except Exception as e:
            st.session_state.pop("video", None)
            st.error(f"No se pudo preparar la descarga: {e}")
            st.caption(f"yt-dlp versión: {yt_dlp.version.__version__}")

    listo = st.session_state.get("video")
    if listo and listo["url"] == url and listo["calidad"] == calidad:
        boton_guardar("💾 Guardar MP4", listo["datos"], f"{nombre_seguro(listo['titulo'])}.mp4",
                      "video/mp4", key=f"dl_mp4_{listo['n']}")
        if listo.pop("auto", False):
            pulsar_automaticamente("Guardar MP4")

    st.divider()

    def bajar_subs(inf: dict, cual: str):
        """Descarga la pista pedida ('original' o 'es') y la guarda sola en el navegador."""
        tipo, clave = inf[cual]
        idioma_limpieza = "es" if cual == "es" else inf["base"]
        try:
            with st.spinner("Preparando los subtítulos... puede tardar un rato; se descargarán solos al terminar."):
                ext, datos = descargar_subs(url, inf["id"], tipo, clave, idioma_limpieza, VERSION_SUBS)
            nombre = f"{nombre_seguro(inf['titulo'])}.{'es' if cual == 'es' else inf['base']}.{ext}"
            mime = "application/x-subrip" if ext == "srt" else "text/vtt"
            st.session_state["sub_fallback"] = {"url": url, "nombre": nombre, "datos": datos,
                                                "mime": mime, "auto": True, "n": time.time_ns()}
        except Exception as e:
            st.session_state.pop("sub_fallback", None)
            st.error(str(e))

    if st.button("📝 Descargar subtítulos"):
        st.session_state.pop("sub_fallback", None)
        try:
            with st.spinner("Buscando subtítulos..."):
                inf = info_subs(url, VERSION_SUBS)
            st.session_state["sub_info"] = {"url": url, "info": inf}
            if inf["es_video"] and inf["original"]:
                bajar_subs(inf, "original")  # vídeo en español: se descarga directamente
        except Exception as e:
            st.session_state.pop("sub_info", None)
            st.error(str(e))

    sub = st.session_state.get("sub_info")
    if sub and sub["url"] == url:
        inf = sub["info"]
        if inf["es_video"]:
            if not inf["original"]:
                st.info("Este vídeo no tiene subtítulos en español.")
        elif not inf["original"] and not inf["es"]:
            st.info("Este vídeo no tiene subtítulos disponibles.")
        else:
            nombre_idioma = IDIOMAS.get(inf["base"], inf["base"].upper())
            st.write(f"El vídeo está en **{nombre_idioma}**. Elige qué subtítulos quieres:")
            col1, col2 = st.columns(2)
            with col1:
                if inf["original"]:
                    if st.button(f"⬇️ Original en {nombre_idioma} (corregido)", key="sub_orig", use_container_width=True):
                        bajar_subs(inf, "original")
                else:
                    st.caption(f"No hay subtítulos en {nombre_idioma}.")
            with col2:
                if inf["es"]:
                    if st.button("🌐 Traducido al español", key="sub_es", use_container_width=True):
                        bajar_subs(inf, "es")
                else:
                    st.caption("YouTube no ofrece traducción al español para este vídeo.")

    fb = st.session_state.get("sub_fallback")
    if fb and fb["url"] == url:
        boton_guardar("💾 Guardar subtítulos", fb["datos"], fb["nombre"], fb["mime"], key=f"dl_subs_{fb['n']}")
        if fb.pop("auto", False):
            pulsar_automaticamente("Guardar subtítulos")
