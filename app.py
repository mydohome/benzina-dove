import csv, io, json, math, os, sqlite3, threading, time, urllib.parse, urllib.request, zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from flask import Flask, jsonify, render_template_string, request

CENTER_LAT = float(os.environ.get("CENTER_LAT", 41.9593))
CENTER_LON = float(os.environ.get("CENTER_LON", 12.5449))
RADIUS_KM  = float(os.environ.get("RADIUS_KM", 3))
CACHE_TTL  = int(os.environ.get("CACHE_TTL_SECONDS", 21600))  # 6h
HIDE_EMPTY_STATIONS = os.environ.get("HIDE_EMPTY_STATIONS", "true").lower() == "true"
HISTORY_ENABLED    = os.environ.get("HISTORY_ENABLED", "true").lower() == "true"
HISTORY_RETAIN_DAYS = int(os.environ.get("HISTORY_RETAIN_DAYS", 7))
HISTORY_DB_PATH     = os.environ.get("HISTORY_DB_PATH", "/app/data/storico.db")

DKV_ENABLED       = os.environ.get("DKV_ENABLED", "false").lower() == "true"
DKV_GPX_URL       = os.environ.get(
    "DKV_GPX_URL",
    "https://my.dkv-mobility.com/apidnext/geo-static-content-service/v1/station-network,zip/dkv_standard_gpx.zip",
)
DKV_MATCH_RADIUS_M = float(os.environ.get("DKV_MATCH_RADIUS_M", 150))
DKV_REFRESH_HOURS  = float(os.environ.get("DKV_REFRESH_HOURS", 168))  # 7 giorni: la rete DKV cambia raramente

URL_ANAGRAFICA = "https://www.mimit.gov.it/images/exportCSV/anagrafica_impianti_attivi.csv"
URL_PREZZI     = "https://www.mimit.gov.it/images/exportCSV/prezzo_alle_8.csv"
URL_REALTIME   = "https://carburanti.mise.gov.it/ospzApi/search/zone"

app = Flask(__name__)
_cache = {"data": None, "ts": 0}
_lock = threading.Lock()

NA = "n/d"


def haversine(lat1, lon1, lat2, lon2):
    R = 6371
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def has_any_price(s):
    return any(s.get(k, NA) != NA for k in ("benzina_self", "benzina_servito", "gasolio_self", "gasolio_servito"))


# ---------------------------------------------------------------------------
# Storico prezzi: SQLite con retention automatica.
#
# POLICY DI RITENZIONE (per non saturare mai il disco):
#   - Ad OGNI scrittura, prima di inserire i nuovi dati, si cancellano tutte
#     le righe più vecchie di HISTORY_RETAIN_DAYS (default 7).
#   - Il database quindi non può mai contenere più di ~7 giorni di dati,
#     indipendentemente da quanto a lungo il container resti in esecuzione.
#   - Ogni N scritture si esegue anche un VACUUM per restituire al filesystem
#     lo spazio delle righe cancellate (SQLite non lo fa automaticamente).
#   - Dimensione attesa: con ~70 distributori, aggiornamento ogni 6h, 7
#     giorni di storico => qualche centinaio di KB. Nessun rischio di
#     saturazione anche lasciando il container acceso per mesi.
# ---------------------------------------------------------------------------
_history_lock = threading.Lock()
_writes_since_vacuum = 0


def _history_db():
    os.makedirs(os.path.dirname(HISTORY_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(HISTORY_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS prezzi_storico (
            ts TEXT NOT NULL,
            station_id INTEGER NOT NULL,
            nome TEXT,
            bandiera TEXT,
            distanza_km REAL,
            benzina_self REAL,
            benzina_servito REAL,
            gasolio_self REAL,
            gasolio_servito REAL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_storico_station ON prezzi_storico(station_id, ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_storico_ts ON prezzi_storico(ts)")
    return conn


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def save_history_snapshot(data):
    """Salva uno snapshot dei prezzi correnti, applicando subito la retention."""
    if not HISTORY_ENABLED:
        return
    global _writes_since_vacuum
    stations = data.get("stations", [])
    if not stations:
        return

    now = datetime.now(timezone.utc).isoformat()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=HISTORY_RETAIN_DAYS)).isoformat()

    with _history_lock:
        try:
            conn = _history_db()
            with conn:
                # Retention: cancella SEMPRE prima di scrivere, così il DB
                # non supera mai la finestra configurata.
                conn.execute("DELETE FROM prezzi_storico WHERE ts < ?", (cutoff,))
                conn.executemany(
                    """INSERT INTO prezzi_storico
                       (ts, station_id, nome, bandiera, distanza_km,
                        benzina_self, benzina_servito, gasolio_self, gasolio_servito)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [
                        (
                            now, s.get("id"), s.get("nome"), s.get("bandiera"), s.get("distanza_km"),
                            _to_float(s.get("benzina_self")), _to_float(s.get("benzina_servito")),
                            _to_float(s.get("gasolio_self")), _to_float(s.get("gasolio_servito")),
                        )
                        for s in stations
                    ],
                )
            _writes_since_vacuum += 1
            if _writes_since_vacuum >= 20:  # ogni ~5 giorni con refresh ogni 6h
                conn.execute("VACUUM")
                _writes_since_vacuum = 0
            conn.close()
        except Exception as e:
            print(f"[carburanti-api] scrittura storico fallita: {e!r}", flush=True)


def read_history(station_id=None, station_ids=None, days=None):
    days = min(days or HISTORY_RETAIN_DAYS, HISTORY_RETAIN_DAYS)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _history_lock:
        try:
            conn = _history_db()
            if station_ids:
                placeholders = ",".join("?" * len(station_ids))
                rows = conn.execute(
                    f"""SELECT ts, station_id, nome, bandiera, distanza_km,
                               benzina_self, benzina_servito, gasolio_self, gasolio_servito
                        FROM prezzi_storico WHERE station_id IN ({placeholders}) AND ts >= ?
                        ORDER BY station_id ASC, ts ASC""",
                    (*station_ids, cutoff),
                ).fetchall()
            elif station_id is not None:
                rows = conn.execute(
                    """SELECT ts, station_id, nome, bandiera, distanza_km,
                              benzina_self, benzina_servito, gasolio_self, gasolio_servito
                       FROM prezzi_storico WHERE station_id = ? AND ts >= ?
                       ORDER BY ts ASC""",
                    (station_id, cutoff),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT ts, station_id, nome, bandiera, distanza_km,
                              benzina_self, benzina_servito, gasolio_self, gasolio_servito
                       FROM prezzi_storico WHERE ts >= ?
                       ORDER BY ts ASC LIMIT 5000""",
                    (cutoff,),
                ).fetchall()
            conn.close()
        except Exception as e:
            print(f"[carburanti-api] lettura storico fallita: {e!r}", flush=True)
            return []

    cols = ["ts", "id", "nome", "bandiera", "distanza_km",
            "benzina_self", "benzina_servito", "gasolio_self", "gasolio_servito"]
    return [dict(zip(cols, r)) for r in rows]


# ---------------------------------------------------------------------------
# Fonte 1 (primaria): API "tempo reale" di Osservaprezzi Carburanti.
# Endpoint della nuova SPA lanciata dal MIMIT il 20/07/2026 (sostituisce il
# vecchio /OssPrezziSearch/ricerca/position, ormai dismesso).
# ---------------------------------------------------------------------------
FUEL_NAME_MAP = {"benzina": "benzina", "gasolio": "gasolio"}


def fetch_realtime(radius=None):
    radius = radius if radius is not None else RADIUS_KM
    body = json.dumps({
        "points": [{"lat": CENTER_LAT, "lng": CENTER_LON}],
        "radius": radius,
    }).encode("utf-8")

    req = urllib.request.Request(
        URL_REALTIME,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; HomeAssistantFuelCard/1.0)",
            "Referer": "https://carburanti.mise.gov.it/ospzSearch/",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        payload_json = json.loads(r.read().decode("utf-8"))

    if not payload_json.get("success"):
        raise RuntimeError("risposta API non valida")

    stations = []
    for d in payload_json.get("results", []):
        prezzi = {}
        for c in d.get("fuels", []):
            nome_carb = (c.get("name") or "").strip().lower()
            tipo = FUEL_NAME_MAP.get(nome_carb)
            if tipo is None:
                continue  # ignora HVO, GPL, Blue Diesel, ecc.
            modo = "self" if c.get("isSelf") else "servito"
            prezzi.setdefault(tipo, {})[modo] = c.get("price", NA)

        try:
            dist = round(float(d.get("distance", 0)), 2)
        except (TypeError, ValueError):
            dist = None

        s = {
            "id": d.get("id"),
            "nome": (d.get("name") or "").strip(),
            "bandiera": (d.get("brand") or "").strip(),
            "indirizzo": (d.get("address") or "").strip(),
            "lat": (d.get("location") or {}).get("lat"),
            "lng": (d.get("location") or {}).get("lng"),
            "distanza_km": dist,
            "aggiornato": d.get("insertDate", NA),
        }
        for tipo in ("benzina", "gasolio"):
            s[f"{tipo}_self"] = prezzi.get(tipo, {}).get("self", NA)
            s[f"{tipo}_servito"] = prezzi.get(tipo, {}).get("servito", NA)
        stations.append(s)

    stations.sort(key=lambda s: s["distanza_km"] if s["distanza_km"] is not None else 999)
    aggiornato = max((s["aggiornato"] for s in stations if s["aggiornato"] != NA), default=NA)
    return {"fonte": "tempo reale (Osservaprezzi)", "aggiornato": aggiornato, "stations": stations}


# ---------------------------------------------------------------------------
# Fonte 2 (fallback): CSV giornaliero, usato solo se l'API real-time non
# risponde.
# ---------------------------------------------------------------------------
def fetch_csv_lines(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        text = r.read().decode("utf-8", errors="ignore")
    lines = text.splitlines()
    estrazione = lines[0].strip() if lines else NA
    header_idx = 0
    for i, l in enumerate(lines[:3]):
        if "idImpianto" in l:
            header_idx = i
            break
    rows = list(csv.DictReader(lines[header_idx:], delimiter="|"))
    return rows, estrazione


def fetch_csv_fallback(radius=None):
    radius = radius if radius is not None else RADIUS_KM
    anagrafica, _ = fetch_csv_lines(URL_ANAGRAFICA)
    vicini = {}
    for r in anagrafica:
        try:
            lat, lon = float(r["Latitudine"]), float(r["Longitudine"])
        except (ValueError, KeyError):
            continue
        d = haversine(CENTER_LAT, CENTER_LON, lat, lon)
        if d <= radius:
            vicini[r["idImpianto"].strip()] = {
                "id": r["idImpianto"].strip(),
                "nome": r.get("Nome Impianto", "").strip() or r.get("Bandiera", "").strip(),
                "bandiera": r.get("Bandiera", "").strip(),
                "indirizzo": r.get("Indirizzo", "").strip(),
                "lat": lat,
                "lng": lon,
                "distanza_km": round(d, 2),
                "prezzi": {},
                "aggiornato": NA,
            }

    if not vicini:
        return {"fonte": "CSV giornaliero (fallback)", "aggiornato": NA, "stations": []}

    prezzi, estrazione_prezzi = fetch_csv_lines(URL_PREZZI)
    for r in prezzi:
        idimp = r.get("idImpianto", "").strip()
        if idimp not in vicini:
            continue
        carb = (r.get("descCarburante") or "").strip().lower()
        if "benzina" not in carb and "gasolio" not in carb:
            continue
        tipo = "benzina" if "benzina" in carb else "gasolio"
        modo = "self" if r.get("isSelf", "").strip() == "1" else "servito"
        vicini[idimp]["prezzi"].setdefault(tipo, {})[modo] = (r.get("prezzo") or NA).strip()
        data_riga = r.get("dtComu", "").strip()
        if data_riga and (vicini[idimp]["aggiornato"] == NA or data_riga > vicini[idimp]["aggiornato"]):
            vicini[idimp]["aggiornato"] = data_riga

    stations = sorted(vicini.values(), key=lambda s: s["distanza_km"])
    for s in stations:
        for tipo in ("benzina", "gasolio"):
            s[f"{tipo}_self"] = s["prezzi"].get(tipo, {}).get("self", NA)
            s[f"{tipo}_servito"] = s["prezzi"].get(tipo, {}).get("servito", NA)
        del s["prezzi"]

    aggiornato = max((s["aggiornato"] for s in stations if s["aggiornato"] != NA), default=estrazione_prezzi)
    return {"fonte": "CSV giornaliero (fallback)", "aggiornato": aggiornato, "stations": stations}


def get_price_24h_ago(station_id):
    """Cerca nello storico lo snapshot più vicino a 24h fa (finestra +/-4h,
    per tollerare lo sfasamento dei cicli di refresh). None se non trovato."""
    if not HISTORY_ENABLED:
        return None
    try:
        station_id = int(station_id)
    except (TypeError, ValueError):
        return None
    target = datetime.now(timezone.utc) - timedelta(hours=24)
    window_start = (target - timedelta(hours=4)).isoformat()
    window_end = (target + timedelta(hours=4)).isoformat()
    with _history_lock:
        try:
            conn = _history_db()
            row = conn.execute(
                """SELECT benzina_self, gasolio_self, ts FROM prezzi_storico
                   WHERE station_id = ? AND ts >= ? AND ts <= ?
                   ORDER BY ABS(julianday(ts) - julianday(?)) ASC LIMIT 1""",
                (station_id, window_start, window_end, target.isoformat()),
            ).fetchone()
            conn.close()
        except Exception:
            return None
    if not row:
        return None
    return {"benzina_self": row[0], "gasolio_self": row[1]}


def compute_trend(previous_value, current_value):
    if previous_value is None:
        return None
    try:
        cur = float(current_value)
    except (TypeError, ValueError):
        return None
    if cur > previous_value:
        return "up"
    if cur < previous_value:
        return "down"
    return "stable"


def add_trends(stations):
    for s in stations:
        prev = get_price_24h_ago(s.get("id"))
        s["trend_benzina"] = compute_trend(prev["benzina_self"], s.get("benzina_self")) if prev else None
        s["trend_gasolio"] = compute_trend(prev["gasolio_self"], s.get("gasolio_self")) if prev else None
    return stations


# ---------------------------------------------------------------------------
# Matching DKV: la rete DKV non è nei dati MIMIT (che riguardano solo prezzi).
# Scarichiamo il file GPX pubblico della rete DKV e incrociamo per prossimità
# geografica con i nostri distributori. È un match "a distanza", non un
# collegamento ufficiale tra i due dataset — vedi avvertenza nel README.
# ---------------------------------------------------------------------------
_dkv_cache = {"points": None, "ts": 0}
_dkv_lock = threading.Lock()


def _fetch_dkv_points():
    """Scarica lo zip GPX DKV, lo estrae in memoria, restituisce lista di (lat, lon, nome).
    Filtra subito a un'area larga intorno al nostro centro di ricerca (+/- ~1 grado,
    circa 100km) per non tenere in memoria tutta la rete europea (~69.000 punti)."""
    req = urllib.request.Request(
        DKV_GPX_URL,
        headers={"User-Agent": "Mozilla/5.0 (compatible; HomeAssistantFuelCard/1.0)"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()

    # Il file scaricato può essere uno zip contenente il .gpx, oppure il gpx diretto:
    # gestiamo entrambi i casi senza assumere nulla.
    gpx_bytes = None
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            gpx_names = [n for n in z.namelist() if n.lower().endswith(".gpx")]
            if not gpx_names:
                raise RuntimeError("nessun file .gpx trovato nello zip DKV")
            gpx_bytes = z.read(gpx_names[0])
    except zipfile.BadZipFile:
        gpx_bytes = raw  # non era uno zip, proviamo a parsarlo direttamente come GPX

    root = ET.fromstring(gpx_bytes)
    # Il namespace GPX standard è http://www.topografix.com/GPX/1/1, ma gestiamo
    # anche l'assenza di namespace per sicurezza.
    ns = {"g": "http://www.topografix.com/GPX/1/1"}
    wpts = root.findall(".//g:wpt", ns) or root.findall(".//wpt")

    lat_min, lat_max = CENTER_LAT - 1.0, CENTER_LAT + 1.0
    lon_min, lon_max = CENTER_LON - 1.0, CENTER_LON + 1.0

    points = []
    for wpt in wpts:
        try:
            lat = float(wpt.get("lat"))
            lon = float(wpt.get("lon"))
        except (TypeError, ValueError):
            continue
        if not (lat_min <= lat <= lat_max and lon_min <= lon <= lon_max):
            continue
        name_el = wpt.find("g:name", ns) if wpt.find("g:name", ns) is not None else wpt.find("name")
        nome = name_el.text if name_el is not None else ""
        points.append((lat, lon, nome or ""))
    return points


def get_dkv_points():
    if not DKV_ENABLED:
        return None
    with _dkv_lock:
        stale = (time.time() - _dkv_cache["ts"]) > DKV_REFRESH_HOURS * 3600
        if _dkv_cache["points"] is None or stale:
            try:
                _dkv_cache["points"] = _fetch_dkv_points()
                _dkv_cache["ts"] = time.time()
                print(f"[carburanti-api] DKV: {len(_dkv_cache['points'])} stazioni in zona caricate", flush=True)
            except Exception as e:
                print(f"[carburanti-api] download rete DKV fallito: {e!r}", flush=True)
                if _dkv_cache["points"] is None:
                    _dkv_cache["points"] = []  # evita di ritentare ad ogni richiesta in caso di errore persistente
        return _dkv_cache["points"]


def add_dkv_flags(stations):
    points = get_dkv_points()
    if points is None:
        return stations  # DKV disabilitato: non tocchiamo i dati
    for s in stations:
        lat, lon = s.get("lat"), s.get("lng")
        if lat is None or lon is None or not points:
            s["accetta_dkv"] = None
            continue
        match = any(haversine(lat, lon, p[0], p[1]) * 1000 <= DKV_MATCH_RADIUS_M for p in points)
        s["accetta_dkv"] = match
    return stations


def build_data(radius=None):
    try:
        data = fetch_realtime(radius)
    except Exception as e:
        import traceback
        print(f"[carburanti-api] fetch_realtime() fallita: {e!r}", flush=True)
        traceback.print_exc()
        data = fetch_csv_fallback(radius)

    totale = len(data["stations"])
    if HIDE_EMPTY_STATIONS:
        data["stations"] = [s for s in data["stations"] if has_any_price(s)]
    data["nascosti_senza_prezzo"] = totale - len(data["stations"])
    data["stations"] = add_trends(data["stations"])
    data["stations"] = add_dkv_flags(data["stations"])
    return data


def get_data(force=False, radius=None):
    # Un raggio esplicito e diverso dal default non usa la cache principale:
    # è una richiesta "una tantum" (es. ?radius=5 da browser), niente da
    # tenere in memoria a lungo.
    if radius is not None and radius != RADIUS_KM:
        return build_data(radius)

    with _lock:
        if force or _cache["data"] is None or (time.time() - _cache["ts"]) > CACHE_TTL:
            _cache["data"] = build_data()
            _cache["ts"] = time.time()
            save_history_snapshot(_cache["data"])
        return _cache["data"]


def parse_radius_arg():
    raw = request.args.get("radius")
    if raw is None:
        return None
    try:
        r = float(raw)
    except ValueError:
        return None
    return max(0.5, min(r, 50))  # limiti di buon senso: 0.5-50 km


PAGE_TEMPLATE = """
<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="600">
<title>Prezzi carburanti</title>
<style>
  :root {
    --bg: #f4f3ef; --card: #ffffff; --border: #e4e2da;
    --text: #1c1c1a; --muted: #6f6e68; --accent: #0f6e56;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #1c1c1a; --card: #262624; --border: #3a3a37;
      --text: #f4f3ef; --muted: #a3a29c; --accent: #5dcaa5;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 24px 16px 48px;
    background: var(--bg); color: var(--text);
    font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
  }
  .wrap { max-width: 960px; margin: 0 auto; }
  h1 { font-size: 20px; font-weight: 600; margin: 0 0 4px; }
  .sub { color: var(--muted); font-size: 13px; margin: 0 0 4px; }
  .hint { color: var(--muted); font-size: 12px; margin: 0 0 20px; font-style: italic; }
  .layout { display: flex; gap: 20px; align-items: flex-start; }
  .cards-col { flex: 1 1 58%; min-width: 0; }
  .grid { display: grid; gap: 12px; }
  .card {
    background: var(--card); border: 1px solid var(--border);
    border-radius: 14px; padding: 16px 18px;
    cursor: pointer; transition: border-color .15s;
  }
  .card:hover { border-color: var(--accent); }
  .card-head {
    display: flex; justify-content: space-between; align-items: baseline;
    margin-bottom: 4px;
  }
  .card-head h2 { font-size: 16px; font-weight: 600; margin: 0; }
  .dist { font-size: 12px; color: var(--muted); white-space: nowrap; }
  .addr { font-size: 12px; color: var(--muted); margin: 0 0 4px; }
  .upd { font-size: 11px; color: var(--muted); margin: 0 0 12px; }
  .fuels { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  .fuel-label { font-size: 11px; text-transform: uppercase; letter-spacing: .03em; color: var(--muted); margin: 0 0 4px; display: flex; align-items: center; gap: 4px; }
  .fuel-price { font-size: 15px; margin: 0; }
  .fuel-price .best { color: var(--accent); font-weight: 600; }
  .fuel-price .tag { font-size: 11px; color: var(--muted); }
  .fuel-price .na { color: var(--muted); font-style: italic; font-weight: 400; font-size: 13px; }
  .trend { font-size: 10px; font-weight: 700; }
  .trend-up { color: #d64545; }
  .trend-down { color: #2f9e5c; }
  .dkv-badge {
    display: inline-block; font-size: 9px; font-weight: 700; letter-spacing: .03em;
    color: #ffffff; background: #1c4e9e; border-radius: 4px; padding: 1px 5px;
    vertical-align: middle;
  }
  .empty { text-align: center; color: var(--muted); padding: 40px 0; }
  footer { text-align: center; font-size: 12px; color: var(--muted); margin-top: 24px; }
  .chart-col {
    flex: 1 1 40%; position: sticky; top: 20px;
    background: var(--card); border: 1px solid var(--border);
    border-radius: 14px; padding: 16px 18px;
  }
  .chart-col h2 { font-size: 15px; font-weight: 600; margin: 0 0 10px; }
  .chart-empty { color: var(--muted); font-size: 13px; text-align: center; padding: 30px 0; display: none; }
  #chartCanvas { max-height: 320px; }
  .map-box {
    margin: 4px 0 20px; border: 1px solid var(--border); border-radius: 14px;
    overflow: hidden;
  }
  #miniMap { height: 260px; width: 100%; background: var(--card); }
  .leaflet-popup-content { font-size: 13px; }
  @media (max-width: 720px) {
    .layout { flex-direction: column; }
    .chart-col { position: static; width: 100%; }
  }
</style>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
</head>
<body>
<div class="wrap">
  <h1>Prezzi carburanti nelle vicinanze</h1>
  <p class="sub">Entro {{ radius }} km &middot; fonte: {{ data.fonte }} &middot; ultimo aggiornamento: {{ data.aggiornato }}</p>
  {% if data.nascosti_senza_prezzo %}
  <p class="hint">{{ data.nascosti_senza_prezzo }} distributori nel raggio non hanno comunicato alcun prezzo e non sono mostrati.</p>
  {% endif %}

  <div class="map-box">
    <div id="miniMap"></div>
  </div>

  <div class="layout">
    <div class="cards-col">
      <div class="grid">
        {% for s in data.stations %}
        <div class="card" data-id="{{ s.id }}" data-nome="{{ s.nome }}" onclick="loadHistory({{ s.id }}, '{{ s.nome | replace(\"'\", \"\") }}')">
          <div class="card-head">
            <h2>{{ s.nome }}{% if s.accetta_dkv %} <span class="dkv-badge" title="Distributore nella rete DKV (match geografico, non ufficiale)">DKV</span>{% endif %}</h2>
            <span class="dist">{{ s.distanza_km }} km</span>
          </div>
          <p class="addr">{{ s.indirizzo }}</p>
          <p class="upd">Aggiornato: {{ s.aggiornato }}</p>
          <div class="fuels">
            <div>
              <p class="fuel-label">Benzina{% if s.trend_benzina == "up" %} <span class="trend trend-up">&#9650;</span>{% elif s.trend_benzina == "down" %} <span class="trend trend-down">&#9660;</span>{% endif %}</p>
              <p class="fuel-price">
                {% if s.benzina_self == "n/d" %}<span class="na">non comunicato</span>{% else %}<span class="best">{{ s.benzina_self }}</span> <span class="tag">self</span>{% endif %}
                {% if s.benzina_self != "n/d" or s.benzina_servito != "n/d" %} &middot; {% endif %}
                {% if s.benzina_servito == "n/d" %}{% if s.benzina_self != "n/d" %}<span class="na">servito n/d</span>{% endif %}{% else %}{{ s.benzina_servito }} <span class="tag">serv.</span>{% endif %}
              </p>
            </div>
            <div>
              <p class="fuel-label">Gasolio{% if s.trend_gasolio == "up" %} <span class="trend trend-up">&#9650;</span>{% elif s.trend_gasolio == "down" %} <span class="trend trend-down">&#9660;</span>{% endif %}</p>
              <p class="fuel-price">
                {% if s.gasolio_self == "n/d" %}<span class="na">non comunicato</span>{% else %}<span class="best">{{ s.gasolio_self }}</span> <span class="tag">self</span>{% endif %}
                {% if s.gasolio_self != "n/d" or s.gasolio_servito != "n/d" %} &middot; {% endif %}
                {% if s.gasolio_servito == "n/d" %}{% if s.gasolio_self != "n/d" %}<span class="na">servito n/d</span>{% endif %}{% else %}{{ s.gasolio_servito }} <span class="tag">serv.</span>{% endif %}
              </p>
            </div>
          </div>
        </div>
        {% else %}
        <div class="empty">Nessun distributore con prezzi disponibili nel raggio impostato.</div>
        {% endfor %}
      </div>
    </div>

    <div class="chart-col">
      <h2 id="chartTitle">Andamento prezzi &mdash; tocca un distributore</h2>
      <canvas id="chartCanvas"></canvas>
      <p class="chart-empty" id="chartEmpty">Nessuno storico disponibile ancora per questo distributore (ci vuole almeno un giorno di raccolta dati).</p>
    </div>
  </div>

  <footer>Dati: MIMIT (Osservaprezzi Carburanti) &middot; aggiornamento pagina ogni 10 minuti &middot; le frecce confrontano il prezzo con quello di ~24h fa</footer>
</div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
(function initMiniMap() {
  const centerLat = {{ center_lat }};
  const centerLon = {{ center_lon }};
  const radiusKm = {{ radius }};
  const stations = {{ data.stations | tojson }};

  const map = L.map('miniMap', { scrollWheelZoom: false }).setView([centerLat, centerLon], 14);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; OpenStreetMap contributors',
    maxZoom: 19
  }).addTo(map);

  // Raggio di ricerca configurato in .env
  L.circle([centerLat, centerLon], {
    radius: radiusKm * 1000, color: '#0f6e56', weight: 1, fillOpacity: 0.05
  }).addTo(map);

  // Centro (coordinate da .env)
  L.circleMarker([centerLat, centerLon], {
    radius: 8, color: '#0f6e56', fillColor: '#0f6e56', fillOpacity: 1, weight: 2
  }).addTo(map).bindPopup('Centro ricerca (.env)');

  // Distributori
  stations.forEach(function (s) {
    if (s.lat == null || s.lng == null) return;
    const popup = '<b>' + s.nome + '</b><br>' +
      'Benzina self: ' + (s.benzina_self !== 'n/d' ? s.benzina_self + '€' : 'n/d') + '<br>' +
      'Gasolio self: ' + (s.gasolio_self !== 'n/d' ? s.gasolio_self + '€' : 'n/d') + '<br>' +
      s.distanza_km + ' km';
    L.circleMarker([s.lat, s.lng], {
      radius: 6, color: '#d64545', fillColor: '#d64545', fillOpacity: 0.85, weight: 1
    }).addTo(map).bindPopup(popup);
  });
})();
</script>

<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.5.1/chart.umd.min.js"></script>
<script>
let chartInstance = null;

async function loadHistory(id, nome) {
  const title = document.getElementById('chartTitle');
  const emptyMsg = document.getElementById('chartEmpty');
  const canvas = document.getElementById('chartCanvas');
  title.textContent = 'Andamento prezzi — ' + nome;
  emptyMsg.style.display = 'none';
  canvas.style.display = 'block';

  let payload;
  try {
    const resp = await fetch('/storico?id=' + encodeURIComponent(id));
    payload = await resp.json();
  } catch (e) {
    emptyMsg.textContent = 'Errore nel caricamento dello storico.';
    emptyMsg.style.display = 'block';
    canvas.style.display = 'none';
    return;
  }

  const rows = payload.rows || [];
  if (!rows.length) {
    emptyMsg.style.display = 'block';
    canvas.style.display = 'none';
    return;
  }

  const labels = rows.map(r => new Date(r.ts).toLocaleString('it-IT', {day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit'}));
  const benzina = rows.map(r => r.benzina_self);
  const gasolio = rows.map(r => r.gasolio_self);

  const style = getComputedStyle(document.body);
  const accent = style.getPropertyValue('--accent').trim();
  const muted = style.getPropertyValue('--muted').trim();

  if (chartInstance) chartInstance.destroy();
  chartInstance = new Chart(canvas, {
    type: 'line',
    data: {
      labels: labels,
      datasets: [
        { label: 'Benzina self', data: benzina, borderColor: accent, backgroundColor: 'transparent', tension: 0.25, spanGaps: true },
        { label: 'Gasolio self', data: gasolio, borderColor: muted, backgroundColor: 'transparent', tension: 0.25, spanGaps: true },
      ]
    },
    options: {
      responsive: true,
      plugins: { legend: { labels: { color: muted } } },
      scales: {
        x: { ticks: { color: muted, maxTicksLimit: 7 }, grid: { color: 'transparent' } },
        y: { ticks: { color: muted }, grid: { color: muted + '22' } }
      }
    }
  });
}
</script>
</body>
</html>
"""


@app.route("/")
def dashboard():
    radius = parse_radius_arg()
    data = get_data(radius=radius)
    return render_template_string(
        PAGE_TEMPLATE, data=data,
        radius=radius if radius is not None else RADIUS_KM,
        center_lat=CENTER_LAT, center_lon=CENTER_LON,
    )


@app.route("/prezzi")
def prezzi():
    radius = parse_radius_arg()
    return jsonify(get_data(radius=radius))


@app.route("/prezzi/refresh")
def refresh():
    return jsonify(get_data(force=True))


@app.route("/storico")
def storico():
    """
    /storico                     -> ultimi HISTORY_RETAIN_DAYS giorni, tutti i distributori
    /storico?id=54279            -> solo quel distributore
    /storico?ids=54279,37767,53469 -> più distributori in una sola risposta (utile per grafici HA)
    /storico?id=54279&days=3     -> ultimi 3 giorni (max HISTORY_RETAIN_DAYS)
    """
    if not HISTORY_ENABLED:
        return jsonify({"enabled": False, "rows": []})

    station_id = request.args.get("id", type=int)
    ids_raw = request.args.get("ids")
    station_ids = None
    if ids_raw:
        station_ids = []
        for part in ids_raw.split(","):
            part = part.strip()
            if part.isdigit():
                station_ids.append(int(part))
    days = request.args.get("days", type=int)
    rows = read_history(station_id=station_id, station_ids=station_ids, days=days)
    return jsonify({
        "enabled": True,
        "retain_days": HISTORY_RETAIN_DAYS,
        "count": len(rows),
        "rows": rows,
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8099)
