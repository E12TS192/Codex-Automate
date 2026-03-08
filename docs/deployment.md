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

## 5. Worker Preflight

Vor dem Start kann der Host ueber denselben Pruefpfad validiert werden, den auch Docker und `systemd` benutzen:

```bash
python3 -m codex_automate worker-check --workspace /srv/codex-automate
```

Der Check prueft:

- Workspace existiert
- persistente Postgres-Verbindung ist konfiguriert, sofern `CODEX_AUTOMATE_REQUIRE_PERSISTENT_DB` nicht auf `0` steht
- registrierte `codex_exec`-Agenten haben eine `codex`-CLI auf `PATH`

## 6. systemd

Vorbereitete Dateien:

- Unit: [codex-automate-worker.service](/Users/alex/Projects/git/Codex Automate/deploy/systemd/codex-automate-worker.service)
- Beispiel-Umgebung: [worker.env.example](/Users/alex/Projects/git/Codex Automate/deploy/worker.env.example)
- Startskript: [start-worker-host.sh](/Users/alex/Projects/git/Codex Automate/scripts/start-worker-host.sh)

Typischer Ablauf:

```bash
sudo install -d /etc/codex-automate
sudo cp deploy/worker.env.example /etc/codex-automate/worker.env
sudo cp deploy/systemd/codex-automate-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now codex-automate-worker
```

## 7. Docker

Fuer containerisierten Betrieb liegt ein generisches Worker-Image in [Dockerfile.worker](/Users/alex/Projects/git/Codex Automate/Dockerfile.worker).

```bash
docker build -f Dockerfile.worker -t codex-automate-worker .
docker run --rm \
  -e CODEX_AUTOMATE_DATABASE_URL=postgresql://USER:PASSWORD@HOST/DBNAME?sslmode=require \
  -e CODEX_AUTOMATE_WORKSPACE=/workspace \
  -v "$PWD:/workspace" \
  codex-automate-worker
```

Wichtig: das Image installiert das Projekt selbst, aber nicht automatisch die `codex`-CLI. Fuer reine Shell-Runner reicht das aus. Fuer `codex_exec`-Agenten muss das Image oder der Host die `codex`-CLI zusaetzlich bereitstellen.

## 8. macOS launchd

Fuer diese Maschine liegt ein User-`launchd`-Pfad bereit:

- Template: [com.alex.codex-automate-worker.plist](/Users/alex/Projects/git/Codex Automate/deploy/launchd/com.alex.codex-automate-worker.plist)
- Installer: [install-launchd-worker.sh](/Users/alex/Projects/git/Codex Automate/scripts/install-launchd-worker.sh)

Der Installer:

- legt `~/Library/Application Support/CodexAutomate/worker.env` an, falls noch nicht vorhanden
- installiert das plist unter `~/Library/LaunchAgents/`
- bootstrapped und kickstarted den Worker sofort
- startet ihn bei Login und nach Neustarts automatisch wieder

Typischer Ablauf:

```bash
chmod +x scripts/install-launchd-worker.sh scripts/start-worker-host.sh
./scripts/install-launchd-worker.sh
```
