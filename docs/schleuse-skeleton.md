# Schleuse-Skeleton

> Status: Entwurf, Mai 2026. Zielphase: Phase 1 (für jede neue Welt-/Anreicherungs-Schleuse). Heimat: `schleusen/_skeleton/` als Kopiervorlage, `schleusen/<name>/` für konkrete Schleusen.

Jede Welt-Anbindung in OSCAR ist eine separate Schleuse, ein eigener Container im `oscar-schleusen`-Pod, ein eigener MCP-Server. Dieses Dokument legt das Wiederholungs-Muster fest — wenn jede Schleuse anders gebaut wäre, würden wir uns die Audit-Disziplin selber sabotieren.

## Was eine Schleuse ist (Recap)

Definierter Zweck, was rausgeht, was reinkommt, geloggt. HERMES (in `oscar-brain`) ist der einzige legitime Aufrufer; Schleusen prüfen daher kein Harness intern, nur den Shared-Bearer (s.u.). Architektur-Verankerung: [oscar-architecture.md Welt-Schleusen](../oscar-architecture.md#L191).

## Repo-Layout

```
schleusen/
├── _skeleton/                      # Kopiervorlage für neue Schleusen
│   ├── server.py                   # FastMCP-Entry, registriert Tools
│   ├── config.py                   # Pydantic-Settings, liest Env-Vars
│   ├── tools/                      # Ein Modul pro MCP-Tool
│   │   ├── __init__.py
│   │   └── example.py
│   ├── tests/
│   │   └── test_example.py
│   ├── pyproject.toml
│   └── Dockerfile
├── wetter/
├── websuche/
└── cloud-llm/
```

Eine Schleuse = ein Verzeichnis = ein Container-Image = ein FastMCP-Server. Kein Mono-Image, kein Per-Tool-Switch — Isolation pro Welt-Quelle ist mehr Wert als das eingesparte MB.

## Server-Pattern (FastMCP)

```python
# schleusen/<name>/server.py
from fastmcp import FastMCP
from fastmcp.server.auth import StaticTokenVerifier
from oscar_logging import log
from .config import settings
from .tools import current_weather, forecast

auth = StaticTokenVerifier(
    tokens={settings.schleusen_bearer: {"sub": "hermes", "client_id": "oscar-brain"}}
)

mcp = FastMCP(
    name=f"oscar-schleuse-{settings.schleuse_name}",
    auth=auth,                        # akzeptiert Authorization: Bearer <SCHLEUSEN_BEARER>
)

mcp.tool()(current_weather.run)
mcp.tool()(forecast.run)

if __name__ == "__main__":
    log.info("schleuse.boot", component=settings.schleuse_name, port=settings.port)
    mcp.run(host="0.0.0.0", port=settings.port, transport="streamable-http")
```

```python
# schleusen/<name>/tools/example.py
from pydantic import BaseModel, Field
from oscar_logging import log
from ..config import settings

class CurrentWeatherInput(BaseModel):
    location: str = Field(..., description="Ortsname oder PLZ")
    units: str = Field("metric", description="metric|imperial")

class CurrentWeatherOutput(BaseModel):
    temperature_c: float
    condition: str
    fetched_at: str

async def run(input: CurrentWeatherInput, ctx) -> CurrentWeatherOutput:
    trace_id = ctx.request_context.meta.get("trace_id")
    log.info("schleuse.call", event_type="current_weather",
             trace_id=trace_id, location=input.location)
    # ... API-Call ...
    return CurrentWeatherOutput(...)
```

Convention: Tool-Funktion heißt immer `run`; Input/Output sind explizite Pydantic-Modelle (kein `**kwargs`); `trace_id` aus MCP-Context lesen, mit jedem Log mitgeben.

## Auth: Shared-Bearer

Alle Schleusen im `oscar-schleusen`-Pod akzeptieren denselben Bearer-Token (`SCHLEUSEN_BEARER`), HERMES schickt ihn bei jedem Call:

```
Authorization: Bearer <SCHLEUSEN_BEARER>
```

Erzeugung beim ServiceBay-Deploy als `type: secret` in der Template-`variables.json`, einmal pro `oscar-schleusen`-Pod, geteilt zwischen allen Containern via Pod-internem Env. Pro-Schleuse-Token wäre granulärer, ist aber bei einem 4-Personen-Haushalt Overkill — die HERMES-Harness-Schicht ist der echte Permission-Gate, der Bearer schützt nur gegen „irgendein anderer Pod auf dem Host".

## variables.json-Beispiel (Wetter-Schleuse)

```json
{
  "schleusenBearer": {
    "type": "secret",
    "label": "Schleusen-Bearer (HERMES → Schleusen)",
    "description": "Wird beim Deploy generiert und an HERMES + alle Schleusen-Container weitergegeben. Neue Generation = alle Schleusen neu deployen.",
    "generate": true,
    "required": true
  },
  "wetterApiKey": {
    "type": "secret",
    "label": "OpenWeatherMap API-Key",
    "description": "Kostenloser Tier reicht für eine Familie. Holen unter openweathermap.org/api.",
    "required": true
  },
  "wetterLanguage": {
    "type": "select",
    "label": "Sprache der Wetterberichte",
    "options": ["de", "en"],
    "default": "de"
  },
  "wetterUnits": {
    "type": "select",
    "label": "Einheiten",
    "options": ["metric", "imperial"],
    "default": "metric"
  }
}
```

Convention: Schleuse-spezifische Variablen mit `<schleuse>`-Prefix, Pod-globale (wie `schleusenBearer`) ohne. Erleichtert mit-grep „welche Variablen brauche ich für die Wetter-Schleuse".

## Pod-YAML-Integration

Jede Schleuse ist ein Container in der `template.yml` des `oscar-schleusen`-Pods:

```yaml
- name: wetter
  image: ghcr.io/mdopp/oscar-schleuse-wetter:{{version}}
  env:
    - name: SCHLEUSE_NAME
      value: wetter
    - name: PORT
      value: "8801"
    - name: SCHLEUSEN_BEARER
      valueFrom: { secretKeyRef: { name: schleusen-bearer, key: token } }
    - name: WETTER_API_KEY
      valueFrom: { secretKeyRef: { name: wetter-api-key, key: token } }
    - name: WETTER_LANGUAGE
      value: "{{wetterLanguage}}"
    - name: OSCAR_COMPONENT
      value: schleuse-wetter
  ports:
    - containerPort: 8801
```

Port-Konvention: 8800–8899 für Schleusen-MCP-Server, hochzählend pro Schleuse. HERMES kennt die Mapping-Tabelle.

## Logging

Pflicht: `shared/oscar_logging`-Lib (siehe [docs/logging.md](logging.md)). Schleusen loggen mindestens:

| Event | Level | Wann |
|---|---|---|
| `schleuse.boot` | info | beim Container-Start |
| `schleuse.call` | info | jeder Tool-Aufruf (mit `trace_id`, ohne Request-Body bei `debug_mode=false`) |
| `schleuse.external_fail` | warn | externe API gibt Fehler zurück, lokaler Fallback griff |
| `schleuse.external_error` | error | externe API unerreichbar, kein Fallback |

Request-/Response-Bodies werden nur bei `debug_mode=true` mitgeschrieben — die Lib prüft das automatisch.

## Lokales Run-Pattern

```bash
# Im Schleuse-Verzeichnis
cd schleusen/wetter

# Dev-Run gegen lokale Env-Vars
SCHLEUSE_NAME=wetter PORT=8801 SCHLEUSEN_BEARER=dev WETTER_API_KEY=... \
  python -m server

# In zweitem Terminal: FastMCP-Inspector
mcp-inspector http://localhost:8801
```

`pyproject.toml` declariert `oscar-logging` + `oscar-schleuse-base` (geteilte Lib im Repo) als Deps; per-Schleuse-Deps (z.B. `httpx`, `feedparser`) zusätzlich.

## Tests

`pytest` mit `httpx-mock` für externe-API-Calls — keine echten Calls in CI, keine echten Keys in Test-Configs. Mindest-Tests:

- Happy-Path pro Tool: Input → Mock-Response → erwartetes Output
- Externer Fehler: Mock 500 → `schleuse.external_fail` geloggt, Tool gibt sinnvolles Default zurück oder hebt klare Exception
- Auth: Request ohne Bearer → 401; falscher Bearer → 401

## Bewusst nicht jetzt

- **Per-Schleuse-Bearer.** Shared bleibt bis Phase 3+, wenn evtl. Schleusen unterschiedliche Vertrauensstufen haben sollen.
- **Rate-Limiting in der Schleuse.** Externe APIs erzwingen das selbst; bei 4 Personen kein OSCAR-eigenes Throttling nötig.
- **Caching-Layer in der Schleuse.** Wetter-Daten sind kurzlebig, Discogs-Lookups einmalig pro Material. HERMES kann konversational cachen, wenn der LLM-Schritt den gleichen Call kurz wiederholen würde.
