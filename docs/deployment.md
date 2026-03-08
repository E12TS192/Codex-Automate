# Deployment

## Zielbild

- GitHub fuer Source of Truth, Pull Requests und CI
- Neon fuer den zentralen Postgres-State
- Vercel fuer Dashboard und HTTP-Control-Plane
- separater Worker-Host fuer `codex exec` und den Autopilot-Loop

Vercel ist hier bewusst nicht der Ort fuer langlaufende Worker. Die API und das Dashboard passen gut dorthin, die eigentliche Agenten-Ausfuehrung dagegen auf dedizierte Compute.

## 1. GitHub

1. `git init -b main`
2. Remote anlegen und pushen
3. GitHub Actions laeuft ueber [.github/workflows/ci.yml](/Users/alex/Projects/git/Codex Automate/.github/workflows/ci.yml)

## 2. Neon

1. Neon-Projekt und Datenbank anlegen
2. Connection String kopieren
3. in `.env` oder Host-Environment setzen:

```bash
CODEX_AUTOMATE_DATABASE_URL=postgresql://USER:PASSWORD@HOST/DBNAME?sslmode=require
```

Die App akzeptiert `CODEX_AUTOMATE_DATABASE_URL`, `DATABASE_URL` oder `POSTGRES_URL`.

## 3. Vercel

1. GitHub-Repo in Vercel importieren
2. Environment Variable setzen:

```bash
CODEX_AUTOMATE_DATABASE_URL=postgresql://USER:PASSWORD@HOST/DBNAME?sslmode=require
```

3. Deploy starten

Die Vercel-App stellt bereit:

- `/` Dashboard
- `/api/health`
- `/api/dashboard`
- `/api/goals`
- `/api/agents`
- `/api/tick`

Die aktuelle App nutzt Vercels Python/FastAPI zero-config ueber [app.py](/Users/alex/Projects/git/Codex Automate/app.py), also bewusst kein eigenes `vercel.json`.
Ohne `CODEX_AUTOMATE_DATABASE_URL` faellt sie auf `/tmp/codex_automate.sqlite3` zurueck, was nur fuer kurzfristige Tests taugt.

## 4. Worker Host

Der Worker-Host braucht dieselbe Datenbankverbindung und das Repository als Arbeitsverzeichnis. Typischer Start:

```bash
python3 -m codex_automate register-agent --name lead --capability orchestrator --capability planning --cwd /srv/codex-automate --timeout-seconds 900
python3 -m codex_automate register-agent --name qa --capability qa --cwd /srv/codex-automate --timeout-seconds 900
python3 -m codex_automate serve-workers --workspace /srv/codex-automate --poll-seconds 5
```

`serve-workers` ist der empfohlene Dauerprozess. Er fuehrt pro Poll-Zyklus `tick -> Heartbeats -> Worker-Runs` aus und kann auf einzelne Goals oder Agenten eingeschraenkt werden.

Wichtige Runner-Parameter:

- `--timeout-seconds`: harte Obergrenze fuer einen Worker-Run; haengende Runs werden als Blocker markiert
- `--heartbeat-interval-seconds`: wie oft waehrend eines laufenden Runs die Lease aktiv verlaengert wird

Fuer echten Dauerbetrieb sollte der Worker-Loop als Prozessmanager-Job oder Service laufen.
