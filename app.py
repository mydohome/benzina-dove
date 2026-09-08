import csv, json, math, os, threading, time, urllib.parse, urllib.request
from flask import Flask, jsonify, render_template_string, request

CENTER_LAT = float(os.environ.get("CENTER_LAT", 41.9593))
CENTER_LON = float(os.environ.get("CENTER_LON", 12.5449))
RADIUS_KM  = float(os.environ.get("RADIUS_KM", 3))
CACHE_TTL  = int(os.environ.get("CACHE_TTL_SECONDS", 21600))  # 6h
HIDE_EMPTY_STATIONS = os.environ.get("HIDE_EMPTY_STATIONS", "true").lower() == "true"

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
  .wrap { max-width: 640px; margin: 0 auto; }
  h1 { font-size: 20px; font-weight: 600; margin: 0 0 4px; }
  .sub { color: var(--muted); font-size: 13px; margin: 0 0 4px; }
  .hint { color: var(--muted); font-size: 12px; margin: 0 0 20px; font-style: italic; }
  .grid { display: grid; gap: 12px; }
  .card {
    background: var(--card); border: 1px solid var(--border);
    border-radius: 14px; padding: 16px 18px;
  }
  .card-head {
    display: flex; justify-content: space-between; align-items: baseline;
    margin-bottom: 4px;
  }
  .card-head h2 { font-size: 16px; font-weight: 600; margin: 0; }
  .dist { font-size: 12px; color: var(--muted); white-space: nowrap; }
  .addr { font-size: 12px; color: var(--muted); margin: 0 0 4px; }
  .upd { font-size: 11px; color: var(--muted); margin: 0 0 12px; }
  .fuels { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  .fuel-label { font-size: 11px; text-transform: uppercase; letter-spacing: .03em; color: var(--muted); margin: 0 0 4px; }
  .fuel-price { font-size: 15px; margin: 0; }
  .fuel-price .best { color: var(--accent); font-weight: 600; }
  .fuel-price .tag { font-size: 11px; color: var(--muted); }
  .fuel-price .na { color: var(--muted); font-style: italic; font-weight: 400; font-size: 13px; }
  .empty { text-align: center; color: var(--muted); padding: 40px 0; }
  footer { text-align: center; font-size: 12px; color: var(--muted); margin-top: 24px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Prezzi carburanti nelle vicinanze</h1>
  <p class="sub">Entro {{ radius }} km &middot; fonte: {{ data.fonte }} &middot; ultimo aggiornamento: {{ data.aggiornato }}</p>
  {% if data.nascosti_senza_prezzo %}
  <p class="hint">{{ data.nascosti_senza_prezzo }} distributori nel raggio non hanno comunicato alcun prezzo e non sono mostrati.</p>
  {% endif %}
  <div class="grid">
    {% for s in data.stations %}
    <div class="card">
      <div class="card-head">
        <h2>{{ s.nome }}</h2>
        <span class="dist">{{ s.distanza_km }} km</span>
      </div>
      <p class="addr">{{ s.indirizzo }}</p>
      <p class="upd">Aggiornato: {{ s.aggiornato }}</p>
      <div class="fuels">
        <div>
          <p class="fuel-label">Benzina</p>
          <p class="fuel-price">
            {% if s.benzina_self == "n/d" %}<span class="na">non comunicato</span>{% else %}<span class="best">{{ s.benzina_self }}</span> <span class="tag">self</span>{% endif %}
            {% if s.benzina_self != "n/d" or s.benzina_servito != "n/d" %} &middot; {% endif %}
            {% if s.benzina_servito == "n/d" %}{% if s.benzina_self != "n/d" %}<span class="na">servito n/d</span>{% endif %}{% else %}{{ s.benzina_servito }} <span class="tag">serv.</span>{% endif %}
          </p>
        </div>
        <div>
          <p class="fuel-label">Gasolio</p>
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
  <footer>Dati: MIMIT (Osservaprezzi Carburanti) &middot; aggiornamento pagina ogni 10 minuti</footer>
</div>
</body>
</html>
"""


@app.route("/")
def dashboard():
    radius = parse_radius_arg()
    data = get_data(radius=radius)
    return render_template_string(PAGE_TEMPLATE, data=data, radius=radius if radius is not None else RADIUS_KM)


@app.route("/prezzi")
def prezzi():
    radius = parse_radius_arg()
    return jsonify(get_data(radius=radius))


@app.route("/prezzi/refresh")
def refresh():
    return jsonify(get_data(force=True))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8099)
