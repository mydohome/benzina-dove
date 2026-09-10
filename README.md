# benzina-dove

Microservizio Docker che espone i prezzi carburante (benzina/gasolio, self e servito) dei distributori entro un certo raggio da un punto, usando i dati ufficiali del **MIMIT – Osservaprezzi Carburanti**. Pensato per essere integrato in **Home Assistant** tramite sensori REST, ma include anche una dashboard web ricca (mappa, grafici storici, tendenze) consultabile direttamente da browser.

## Caratteristiche

- Fonte dati primaria: API in tempo reale del MIMIT (`carburanti.mise.gov.it/ospzApi/search/zone`), aggiornata dai gestori entro 8 ore per legge.
- Fallback automatico sul CSV giornaliero ufficiale se l'API realtime non risponde.
- Prezzi separati per self-service e servito.
- Distributori senza alcun prezzo comunicato nascosti dalla card (configurabile).
- **Storico prezzi** su SQLite con retention automatica configurabile (default 7 giorni): il database non può mai crescere oltre la finestra impostata, a prescindere da quanto a lungo il container resti acceso.
- **Frecce di tendenza** su ogni card: confronto automatico del prezzo self con quello di ~24h prima.
- **Grafico interattivo** (Chart.js) dell'andamento prezzi degli ultimi 7 giorni, cliccando su un distributore.
- **Mini-mappa** (Leaflet + OpenStreetMap, nessuna chiave API richiesta) con il centro di ricerca, il raggio configurato e ogni distributore trovato.
- **Matching (best-effort) con la rete DKV**: incrocio geografico opzionale con il file GPX pubblico della rete DKV, per segnalare i distributori potenzialmente convenzionati.
- Dashboard HTML consultabile direttamente su `http://<host>:8099/`, con tema chiaro/scuro automatico, layout a due colonne su schermi larghi (card + grafico affiancati).
- Endpoint JSON pensati per sensori REST di Home Assistant (prezzi correnti e storico, anche per più distributori in una sola chiamata).
- Raggio di ricerca configurabile sia via `.env` (default) sia al volo via query string.

## Avvio rapido

```bash
git clone https://github.com/<tuo-utente>/benzina-dove.git
cd benzina-dove
cp .env.example .env
# modifica .env con le tue coordinate (verificate su Google Maps) e il raggio desiderato
mkdir -p data   # necessario per la persistenza dello storico prezzi
docker compose up -d --build
```

Verifica che funzioni:
```bash
curl -s http://localhost:8099/prezzi | python3 -m json.tool
```

Apri la dashboard da browser: `http://<ip-del-tuo-server>:8099/`

## Configurazione (`.env`)

| Variabile | Default | Descrizione |
|---|---|---|
| `CENTER_LAT` / `CENTER_LON` | Via Giulio Antamoro, Roma | Coordinate del centro di ricerca. **Verificale su Google Maps** (tasto destro sul punto → coordinate), non affidarti a geocoder automatici: possono avere scarti di km. |
| `RADIUS_KM` | `3` | Raggio di ricerca in km |
| `CACHE_TTL_SECONDS` | `21600` (6h) | Ogni quanto ricaricare i dati dalla fonte MIMIT |
| `HIDE_EMPTY_STATIONS` | `true` | Nasconde i distributori senza alcun prezzo comunicato |
| `HISTORY_ENABLED` | `true` | Attiva la registrazione dello storico prezzi |
| `HISTORY_RETAIN_DAYS` | `7` | Giorni di storico conservati; oltre questa finestra i dati vengono cancellati automaticamente ad ogni scrittura |
| `HISTORY_DB_PATH` | `/app/data/storico.db` | Percorso del database SQLite (deve stare sotto `/app/data`, montato come volume persistente) |
| `DKV_ENABLED` | `false` | Attiva il matching geografico con la rete DKV |
| `DKV_GPX_URL` | link ufficiale DKV | URL del file GPX della rete DKV |
| `DKV_MATCH_RADIUS_M` | `150` | Distanza massima (metri) per considerare valido un match DKV |
| `DKV_REFRESH_HOURS` | `168` (7 giorni) | Ogni quante ore riscaricare la rete DKV |

Dopo aver modificato `.env`, ricrea il container (non basta un semplice `restart`, che non rilegge le variabili):
```bash
docker compose up -d
```

## Endpoint

| Endpoint | Descrizione |
|---|---|
| `GET /` | Dashboard HTML: mappa, card dei distributori con frecce di tendenza, grafico storico cliccabile |
| `GET /prezzi` | JSON con i distributori nel raggio configurato (usa la cache) |
| `GET /prezzi?radius=5` | Come sopra ma con raggio diverso dal default, una tantum (limiti: 0.5–50 km, non usa/aggiorna la cache principale) |
| `GET /prezzi/refresh` | Come `/prezzi` ma forza un nuovo download dalla fonte, ignorando la cache |
| `GET /storico?id=54279` | Storico di un singolo distributore (fino a `HISTORY_RETAIN_DAYS` giorni) |
| `GET /storico?ids=54279,37767,53469` | Storico di più distributori in una sola chiamata (utile per grafici in Home Assistant) |
| `GET /storico?id=54279&days=3` | Storico limitato agli ultimi N giorni (max `HISTORY_RETAIN_DAYS`) |

### Esempio risposta `/prezzi`
```json
{
  "fonte": "tempo reale (Osservaprezzi)",
  "aggiornato": "2026-09-08T10:30:25+02:00",
  "nascosti_senza_prezzo": 2,
  "stations": [
    {
      "id": 54279,
      "nome": "ALEMAN PETROLI SAS",
      "bandiera": "Q8",
      "indirizzo": "",
      "lat": 41.950322,
      "lng": 12.541186,
      "distanza_km": 1.13,
      "aggiornato": "2026-09-08T10:30:25+02:00",
      "benzina_self": 2.094,
      "benzina_servito": 2.399,
      "gasolio_self": 2.174,
      "gasolio_servito": 2.479,
      "trend_benzina": "down",
      "trend_gasolio": "stable",
      "accetta_dkv": true
    }
  ]
}
```

## Integrazione con Home Assistant

### Sensore prezzi correnti
In `sensors.yaml`, o meglio come [package](https://www.home-assistant.io/docs/configuration/packages/) dedicato (vedi sotto):
```yaml
sensor:
  - platform: rest
    name: Prezzi Carburanti
    resource: http://localhost:8099/prezzi
    value_template: "{{ value_json.aggiornato }}"
    json_attributes:
      - stations
      - aggiornato
    scan_interval: 21600
```
> Se Home Assistant gira con `network_mode: host`, usa `localhost`. Se invece HA e questo container sono su una rete Docker bridge condivisa, usa il nome del servizio (es. `http://carburanti-api:8099/prezzi`).

### Filtro preferiti
```yaml
input_text:
  distributori_preferiti:
    name: Distributori preferiti
    icon: mdi:gas-station
    max: 255

sensor:
  - platform: template
    sensors:
      prezzi_carburanti_preferiti:
        friendly_name: "Prezzi Carburanti Preferiti"
        value_template: "{{ now() }}"
        attribute_templates:
          stations: >
            {% set preferiti = (states('input_text.distributori_preferiti') or '') | replace(' ', '') %}
            {% set ids = preferiti.split(',') if preferiti else [] %}
            {% set tutte = state_attr('sensor.prezzi_carburanti', 'stations') or [] %}
            {% if ids %}
              {{ tutte | selectattr('id', 'in', ids | map('int') | list) | list }}
            {% else %}
              {{ tutte }}
            {% endif %}
```
Popola `input_text.distributori_preferiti` con gli ID separati da virgola (es. `54279,11812,6647` — recuperabili dal campo `id` di `/prezzi`).

### Card con box compatti (richiede [HTML Jinja2 Template Card](https://github.com/PiotrMachowski/Home-Assistant-Lovelace-HTML-Jinja2-Template-card) da HACS)
```yaml
type: custom:html-template-card
title: Prezzi Carburanti
ha_card: true
content: >
  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px">{% for s in state_attr('sensor.prezzi_carburanti_preferiti','stations') %}<div style="border:1px solid var(--divider-color);border-radius:10px;padding:8px 10px;background:var(--card-background-color)"><div style="font-weight:600;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-bottom:2px">{{ s.nome }}</div><div style="font-size:10px;color:var(--secondary-text-color);margin-bottom:6px">{{ s.distanza_km }} km</div><div style="font-size:12px;margin-bottom:2px">⛽ {% if s.benzina_self != "n/d" %}<b style="color:var(--primary-color)">{{ s.benzina_self }}€</b>{% else %}<span style="color:var(--secondary-text-color)">n/d</span>{% endif %}</div><div style="font-size:12px">🛢️ {% if s.gasolio_self != "n/d" %}<b style="color:var(--primary-color)">{{ s.gasolio_self }}€</b>{% else %}<span style="color:var(--secondary-text-color)">n/d</span>{% endif %}</div></div>{% endfor %}</div>
entities:
  - sensor.prezzi_carburanti_preferiti
  - sensor.prezzi_carburanti
```
> Nota: con questa card evita di andare a capo dentro il valore di `content`, altrimenti ogni ritorno a capo diventa un `<br>` indesiderato — tienilo tutto su una riga.

### Grafico storico (richiede [ApexCharts Card](https://github.com/RomRider/apexcharts-card) da HACS)
```yaml
sensor:
  - platform: rest
    name: Storico Prezzi Carburanti
    resource: http://localhost:8099/storico?ids=37767,53469,54279
    value_template: "{{ value_json.count }}"
    json_attributes:
      - rows
    scan_interval: 21600
```
```yaml
type: custom:apexcharts-card
header:
  show: true
  title: Andamento benzina self — ultimi 7 giorni
graph_span: 7d
series:
  - entity: sensor.storico_prezzi_carburanti
    name: ROMA VIA BUFALOTTA 640
    data_generator: |
      return entity.attributes.rows
        .filter(r => r.id === 37767 && r.benzina_self !== null)
        .map(r => [new Date(r.ts).getTime(), r.benzina_self]);
```
Duplica il blocco `series` per ogni distributore che vuoi confrontare, cambiando `id` e `name`.

## Note tecniche

- **Fonte dati**: il MIMIT ha rilanciato il sito Osservaprezzi Carburanti il 20/07/2026 con una nuova app/SPA; il nuovo endpoint (`/ospzApi/search/zone`) non è documentato pubblicamente ed è stato individuato ispezionando le richieste di rete del sito ufficiale. Potrebbe cambiare senza preavviso in futuro — se `/prezzi` inizia a restituire sempre `"fonte": "CSV giornaliero (fallback)"`, controlla i log del container (`docker compose logs`) per l'errore esatto.
- **Requisiti di rete**: il container deve poter raggiungere in uscita `carburanti.mise.gov.it` e `www.mimit.gov.it` sulla porta 443 (e `my.dkv-mobility.com` se `DKV_ENABLED=true`).
- **Persistenza dello storico**: richiede il volume `./data:/app/data` nel `docker-compose.yml`, altrimenti lo storico si azzera ad ogni riavvio del container.
- **Policy di ritenzione storico**: ad ogni scrittura viene eseguita una `DELETE` di tutto ciò che supera `HISTORY_RETAIN_DAYS`, seguita periodicamente da un `VACUUM` per restituire davvero lo spazio al filesystem. Dimensione attesa anche dopo mesi di attività: qualche centinaio di KB.
- **Matching DKV**: è un abbinamento geografico per prossimità (default 150m), non un collegamento ufficiale tra i due dataset — i dati MIMIT non includono metodi di pagamento. Trattalo come indicativo, non come garanzia.
- **Dashboard con CDN esterni**: Chart.js e Leaflet vengono caricati dal browser via CDN (cdnjs.cloudflare.com, unpkg.com). Se la tua rete/browser blocca questi domini, mappa e grafico non compariranno — il resto della dashboard funziona comunque.

## Licenza dei dati

I dati sui prezzi provengono dall'Osservatorio Prezzi Carburanti del Ministero delle Imprese e del Made in Italy (MIMIT), pubblicati con licenza IODL 2.0. I dati di rete DKV (se attivati) provengono dal file GPX pubblico di DKV Mobility.
