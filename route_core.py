"""Núcleo de direcciones y rutas para Valencia. Conserva siempre el texto original."""
import re
import time
import urllib.parse
from datetime import datetime

import requests
from geopy.geocoders import Nominatim

PUNTO_SALIDA = "Carrer de Bernat Descoll, 63, 46026 València, España"
LAT_SALIDA = 39.4529
LON_SALIDA = -0.3756
OSRM_BASE = "https://router.project-osrm.org"
USER_AGENT = "organizador-rutas-valencia/3.0 (local desktop app)"

MUNICIPIOS = {
    "PATERNA": "Paterna", "MELIANA": "Meliana",
    "RAFELBUNYOL": "Rafelbunyol", "RAFELBUÑOL": "Rafelbunyol",
    "POBLA DE FARNALS, LA": "La Pobla de Farnals", "POBLA DE FARNALS LA": "La Pobla de Farnals",
    "LA POBLA DE FARNALS": "La Pobla de Farnals", "MUSEROS": "Museros",
    "VALENCIA": "València", "VALÈNCIA": "València",
    "ALBALAT DELS SORELLS": "Albalat dels Sorells", "MASSAMAGRELL": "Massamagrell",
}


def _normalizar_nombre(s):
    s = s.replace("IBA?EZ", "IBÁÑEZ").replace("IBA¥EZ", "IBÁÑEZ").replace("IBAÑEZ", "IBÁÑEZ")
    s = s.replace("¥", "Ñ").replace("+", " ")
    s = re.sub(r"\s+", " ", s)
    return s.strip(" ,.-")


def limpiar_orden(linea):
    original = linea.strip()
    if "|" in original:
        expediente, texto = [p.strip() for p in original.split("|", 1)]
        texto = re.sub(r"^" + re.escape(expediente) + r"\s*", "", texto, count=1)
    else:
        expediente, texto = "", original

    franja_match = re.search(r"\bde\s*(\d{1,2}:\d{2})\s*a\s*(\d{1,2}:\d{2})", texto, re.I)
    franja = (franja_match.group(1), franja_match.group(2)) if franja_match else None
    fechas = re.findall(r"\b(\d{2}/\d{2}/\d{4})\b", texto)
    fecha_visita = None
    if len(fechas) >= 3:
        try: fecha_visita = datetime.strptime(fechas[2], "%d/%m/%Y").date()
        except ValueError: pass

    # El CP unido al municipio es la fuente más fiable para el municipio.
    mcp = re.search(r"\b(46\d{3})\s*-\s*([^\n]*?)(?=\s+En espera\b|\s+Pendiente\b|\s+\d{2}/\d{2}/\d{4}\b|$)", texto, re.I)
    cp = mcp.group(1) if mcp else ""
    municipio_raw = mcp.group(2).strip(" ,.-") if mcp else ""
    municipio_raw = re.sub(r"\s*-\s*VALENC.*$", "", municipio_raw, flags=re.I).strip()
    municipio_raw = _normalizar_nombre(municipio_raw)
    municipio_key = re.sub(r"\s+", " ", municipio_raw.upper())
    municipio = MUNICIPIOS.get(municipio_key, municipio_raw.title() if municipio_raw else "València")
    if not cp:
        m = re.search(r"\b(46\d{3})\b", texto)
        cp = m.group(1) if m else ""

    # Aísla el segmento de dirección antes del código postal/estado/fechas.
    corte = len(texto)
    for pat in (r"\b46\d{3}\b", r"\bEn espera\b", r"\bPendiente\b", r"\bSiniestro\b", r"\b\d{2}/\d{2}/\d{4}\b"):
        m = re.search(pat, texto, re.I)
        if m: corte = min(corte, m.start())
    cuerpo = texto[:corte]
    cuerpo = re.sub(r"\b(?:Manitas|Fontanero)\b", " ", cuerpo, flags=re.I)
    cuerpo = re.sub(r"^\s*\d+\s+", "", cuerpo)  # ID duplicado si no se separó bien
    cuerpo = _normalizar_nombre(cuerpo)

    # Prefijos exportados pueden venir encadenados: C/ AV/, C/CL., AVD., etc.
    # Quitamos primero prefijos contenedores y luego decidimos si es avenida o calle.
    cuerpo = re.sub(r"^\s*(?:(?:C/CL\.?|C/CL/|CL\.?|C/)\s*)+", "", cuerpo, flags=re.I)
    cuerpo = re.sub(r"^\s*C/\s*", "", cuerpo, flags=re.I)
    cuerpo = re.sub(r"^\s*C\s+(?=[A-ZÀ-Ý])", "", cuerpo, flags=re.I)
    cuerpo = re.sub(r"^\s*/\s*", "", cuerpo)
    if re.match(r"^(?:AVD\.?|AV/|AV\.)\s*", cuerpo, flags=re.I):
        cuerpo = re.sub(r"^(?:AVD\.?|AV/|AV\.)\s*", "Avinguda ", cuerpo, flags=re.I)
    elif not re.match(r"^(?:Calle|Carrer|Avinguda|Avenida)\b", cuerpo, flags=re.I):
        cuerpo = "Carrer " + cuerpo
    cuerpo = re.sub(r"^\s*CALLE\s+", "Calle ", cuerpo, flags=re.I)
    cuerpo = re.sub(r"^\s*AVENIDA\s+", "Avenida ", cuerpo, flags=re.I)
    cuerpo = re.sub(r"\b0+(\d+)\b", r"\1", cuerpo)
    cuerpo = re.sub(r"\s+", " ", cuerpo).strip(" ,.-")

    # Conserva calle + primer número (portal), descartando piso/puerta posteriores.
    nm = re.search(r"^(.*?\D)\s+(\d+[A-Za-z]?)\b", cuerpo)
    if nm:
        calle = f"{nm.group(1).strip()} {nm.group(2)}"
    else:
        calle = cuerpo
    calle = re.sub(r"\s+", " ", calle).strip(" ,.-")

    # Forma de búsqueda principal, además de campos separados para generar variantes.
    direccion = f"{calle}, {cp} {municipio}, España" if cp else f"{calle}, {municipio}, España"
    return {"expediente": expediente, "original": original, "calle": calle, "municipio": municipio,
            "cp": cp, "direccion_busqueda": direccion, "franja": franja, "fecha_visita": fecha_visita}


def _variantes(registro):
    calle = _normalizar_nombre(registro["calle"])
    municipio = registro["municipio"]
    cp = registro["cp"]
    # Corrige nombres habituales y prueba grafías castellana/valenciana sin cambiar portal.
    base = calle
    sin_portal = re.sub(r"\s+\d+[A-Za-z]?\s*$", "", base).strip()
    variantes_calle = [base]
    if base.lower().startswith("carrer "):
        variantes_calle.append("Calle " + base[7:])
    elif base.lower().startswith("calle "):
        variantes_calle.append("Carrer " + base[6:])
    if "blasco ibáñez" in base.casefold() or "blasco ibanez" in base.casefold():
        pref = "Avinguda" if base.lower().startswith(("avinguda", "avenida")) else "Carrer"
        numero = re.search(r"\s(\d+[A-Za-z]?)$", base)
        num = " " + numero.group(1) if numero else ""
        variantes_calle += [f"Avinguda de Blasco Ibáñez{num}", f"Avenida Blasco Ibáñez{num}", f"Carrer de Blasco Ibáñez{num}"]
    if "glories valencianes" in base.casefold() or "glòries valencianes" in base.casefold():
        numero = re.search(r"\s(\d+[A-Za-z]?)$", base)
        num = " " + numero.group(1) if numero else ""
        variantes_calle += [f"Carrer de les Glòries Valencianes{num}", f"Calle Glorias Valencianas{num}", f"Glòries Valencianes{num}"]
    queries = []
    for c in variantes_calle:
        if cp: queries.append(f"{c}, {cp} {municipio}, España")
        queries.append(f"{c}, {municipio}, Valencia, España")
    if sin_portal != base:
        queries += [f"{sin_portal}, {municipio}, Valencia, España", f"{sin_portal}, {municipio}, España"]
    # Deduplica preservando el orden.
    return list(dict.fromkeys(q for q in queries if q.strip()))


def geocodificar_orden(registro, pause=True):
    """Prueba Nominatim y Photon. Devuelve coordenadas y consulta ganadora o motivo claro."""
    queries = _variantes(registro)
    errores = []
    geo = Nominatim(user_agent=USER_AGENT, timeout=12)
    for q in queries:
        try:
            if pause: time.sleep(1.05)  # respetar el servicio público de Nominatim
            loc = geo.geocode(q, country_codes="es", exactly_one=True, addressdetails=True)
            if loc:
                return {"lat": float(loc.latitude), "lon": float(loc.longitude), "resultado": str(loc.address),
                        "consulta_usada": q, "proveedor": "OpenStreetMap/Nominatim"}, None
        except Exception as exc:
            errores.append(f"Nominatim: {exc}")
        # Photon como segundo proveedor: ayuda con variantes de nombres de calle.
        try:
            r = requests.get("https://photon.komoot.io/api/", params={"q": q, "limit": 3, "lang": "en"}, timeout=10,
                             headers={"User-Agent": USER_AGENT})
            r.raise_for_status()
            features = r.json().get("features", [])
            if features:
                # Prioriza coincidencias en el municipio esperado y en España.
                def score(f):
                    p = f.get("properties", {})
                    city = str(p.get("city", "") or p.get("locality", "")).casefold()
                    country = str(p.get("country", "")).casefold()
                    wanted = registro["municipio"].casefold()
                    return (2 if wanted in city or city in wanted else 0) + (1 if "spain" in country or "españa" in country else 0)
                f = max(features, key=score)
                lon, lat = f["geometry"]["coordinates"]
                props = f.get("properties", {})
                # Evita devolver resultados de otro país; si Photon no indica país, lo conservamos con advertencia.
                country = str(props.get("country", "")).casefold()
                if not country or "spain" in country or "españa" in country:
                    label = ", ".join(str(props[k]) for k in ("name", "street", "housenumber", "postcode", "city", "country") if props.get(k))
                    return {"lat": float(lat), "lon": float(lon), "resultado": label or q,
                            "consulta_usada": q, "proveedor": "Photon"}, None
        except Exception as exc:
            errores.append(f"Photon: {exc}")
    detalle = "No hubo coincidencia en Nominatim ni Photon."
    if errores:
        detalle += " Último error: " + errores[-1]
    return None, detalle


def matriz_carretara(coords):
    coord_text = ";".join(f"{lon},{lat}" for lat, lon in coords)
    r = requests.get(f"{OSRM_BASE}/table/v1/driving/{coord_text}", params={"annotations": "distance,duration"}, timeout=45)
    r.raise_for_status(); data = r.json()
    if data.get("code") != "Ok": raise RuntimeError(data.get("message", "OSRM no pudo calcular la matriz"))
    return data["distances"], data["durations"]


def optimizar_orden(distancias):
    n = len(distancias)
    if n <= 1: return list(range(n))
    pendientes = set(range(1, n)); orden = [0]
    while pendientes:
        actual = orden[-1]
        posibles = [i for i in pendientes if distancias[actual][i] is not None]
        if not posibles: orden.extend(sorted(pendientes)); break
        siguiente = min(posibles, key=lambda i: distancias[actual][i]); orden.append(siguiente); pendientes.remove(siguiente)
    mejor = orden[:]
    for _ in range(25):
        cambio = False
        for i in range(1, len(mejor)-1):
            for j in range(i+1, len(mejor)):
                a, b, c = mejor[i-1], mejor[i], mejor[j]
                old = (distancias[a][b] or 0); new = (distancias[a][c] or 0)
                if j+1 < len(mejor):
                    d = mejor[j+1]; old += distancias[c][d] or 0; new += distancias[b][d] or 0
                if new + 1 < old: mejor[i:j+1] = reversed(mejor[i:j+1]); cambio = True
        if not cambio: break
    return mejor


def enlace_google_maps(registros, origen=None):
    if not registros: return ""
    params = {"api": "1", "origin": origen or PUNTO_SALIDA, "destination": registros[-1]["direccion_busqueda"], "travelmode": "driving"}
    if len(registros) > 1: params["waypoints"] = "|".join(p["direccion_busqueda"] for p in registros[:-1])
    return "https://www.google.com/maps/dir/?" + urllib.parse.urlencode(params, safe="|, ")
