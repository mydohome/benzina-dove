# benzina-dove

Microservizio Docker che espone i prezzi carburante (benzina/gasolio, self e servito) dei distributori entro un certo raggio da un punto, usando i dati ufficiali del **MIMIT – Osservaprezzi Carburanti**. Pensato per essere integrato in **Home Assistant** tramite un sensore REST, ma include anche una dashboard web consultabile direttamente da browser.

## Caratteristiche

- Fonte dati primaria: API in tempo reale del MIMIT (`carburanti.mise.gov.it/ospzApi/search/zone`), aggiornata dai gestori entro 8 ore per legge.
- Fallback automatico sul CSV giornaliero ufficiale se l'API realtime non risponde.
- Prezzi separati per self-service e servito.
- Distributori senza alcun prezzo comunicato nascosti dalla card (configurabile).
- Dashboard HTML consultabile direttamente su `http://<host>:8099/`, con tema chiaro/scuro automatico.
- Endpoint JSON pensato per un sensore REST di Home Assistant.
- Raggio di ricerca configurabile sia via `.env` (default) sia al volo via query string.
- Nessun dato salvato su disco: tutto in cache in memoria, nessuna crescita nel tempo.

## Avvio rapido

```bash
git clone https://github.com/<tuo-utente>/benzina-dove.git
cd benzina-dove
cp .env.example .env
# modifica .env con le tue coordinate e il raggio desiderato
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
| `CENTER_LAT` | `41.9593` | Latitudine del centro di ricerca |
| `CENTER_LON` | `12.5449` | Longitudine del centro di ricerca |
| `RADIUS_KM` | `3` | Raggio di ricerca in km |
| `CACHE_TTL_SECONDS` | `21600` (6h) | Ogni quanto ricaricare i dati dalla fonte MIMIT |
| `HIDE_EMPTY_STATIONS` | `true` | Nasconde i distributori senza alcun prezzo comunicato |

Dopo aver modificato `.env`, ricrea il container (non basta un semplice `restart`, che non rilegge le variabili):
```bash
docker compose up -d
```

## Endpoint

| Endpoint | Descrizione |
|---|---|
| `GET /` | Dashboard HTML, aggiornamento automatico ogni 10 minuti |
| `GET /prezzi` | JSON con i distributori nel raggio configurato (usa la cache) |
| `GET /prezzi?radius=5` | Come sopra ma con raggio diverso da quello di default, una tantum (limiti: 0.5–50 km, non usa/aggiorna la cache principale) |
| `GET /prezzi/refresh` | Come `/prezzi` ma forza un nuovo download dalla fonte, ignorando la cache |

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
      "distanza_km": 1.13,
      "aggiornato": "2026-09-08T10:30:25+02:00",
      "benzina_self": 2.094,
      "benzina_servito": 2.399,
      "gasolio_self": 2.174,
      "gasolio_servito": 2.479
    }
  ]
}
```

## Integrazione con Home Assistant

In `sensors.yaml` (o direttamente in `configuration.yaml` sotto `sensor:`):

```yaml
- platform: rest
  name: Prezzi Carburanti
  resource: http://<IP_VM>:8099/prezzi
  value_template: "{{ value_json.aggiornato }}"
  json_attributes:
    - stations
    - aggiornato
  scan_interval: 21600
```

Card Markdown di esempio:
```yaml
type: markdown
content: |
  | Distributore | Dist. | Benzina self/servito | Gasolio self/servito |
  |---|---|---|---|
  {% for s in state_attr('sensor.prezzi_carburanti','stations') %}
  | {{ s.nome }} | {{ s.distanza_km }} km | {{ s.benzina_self }}€ / {{ s.benzina_servito }}€ | {{ s.gasolio_self }}€ / {{ s.gasolio_servito }}€ |
  {% endfor %}

  *Aggiornato: {{ state_attr('sensor.prezzi_carburanti','aggiornato') }}*
```

## Note tecniche

- **Fonte dati**: il MIMIT ha rilanciato il sito Osservaprezzi Carburanti il 20/07/2026 con una nuova app/SPA; il nuovo endpoint (`/ospzApi/search/zone`) non è documentato pubblicamente ed è stato individuato ispezionando le richieste di rete del sito ufficiale. Potrebbe cambiare senza preavviso in futuro — se `/prezzi` inizia a restituire sempre `"fonte": "CSV giornaliero (fallback)"`, controlla i log del container (`docker compose logs`) per l'errore esatto.
- **Requisiti di rete**: il container deve poter raggiungere in uscita `carburanti.mise.gov.it` e `www.mimit.gov.it` sulla porta 443.
- **Nessuna persistenza**: tutti i dati sono tenuti in RAM; riavviando il container la cache riparte vuota. Non c'è alcun file scritto su disco dall'applicazione.

## Licenza dei dati

I dati provengono dall'Osservatorio Prezzi Carburanti del Ministero delle Imprese e del Made in Italy (MIMIT), pubblicati con licenza IODL 2.0.
