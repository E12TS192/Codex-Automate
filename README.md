# Codex Automate

Codex Automate ist ein erster lauffaehiger Prototyp fuer ein Multi-Agent-System mit einem zentralen Orchestrator. Der Fokus liegt auf drei Dingen:

- Ein Ziel wird einmal strukturiert eingebracht.
- Der Orchestrator zerlegt es in Work Packages und vergibt sie passend an Agenten.
- Status, Blocker und Reassignments bleiben jederzeit in einem persistenten State nachvollziehbar.

Fuer Deployments ist das Projekt jetzt auf ein geteiltes Datenbankziel vorbereitet: lokal per SQLite, remote per Postgres/Neon.

## Kernideen

- Der Orchestrator ist die einzige Instanz mit globaler Sicht.
- Agenten kommunizieren nicht frei miteinander, sondern ueber strukturierte Events und Artefakte.
- Jede Assignment bekommt einen Lease. Ohne Heartbeat kann der Orchestrator Pakete neu vergeben.
- Blocker erzeugen eigene `unblock`-Pakete, statt den gesamten Fluss anzuhalten.

Mehr Details stehen in [docs/orchestrator-concept.md](/Users/alex/Projects/git/Codex Automate/docs/orchestrator-concept.md).

## Schnellstart

Projekt lokal ohne Installation ausfuehren:

```bash
python3 -m codex_automate bootstrap
python3 -m codex_automate demo --reset
python3 -m codex_automate dashboard
```

Echte Worker mit Codex CLI starten:

```bash
python3 -m codex_automate register-agent --name lead --capability orchestrator --capability planning --instruction "Du loest Blocker und planst Folgearbeit."
python3 -m codex_automate register-agent --name builder --capability backend --instruction "Du implementierst nur das dir zugewiesene Paket."
python3 -m codex_automate register-agent --name qa --capability qa --instruction "Du validierst nur das dir zugewiesene Paket."
python3 -m codex_automate submit-goal --file examples/demo_goal.json
python3 -m codex_automate autopilot --max-iterations 10
```

HTTP-Control-Plane lokal starten:

```bash
python3 -m pip install .
uvicorn app:app --reload
```

Optional als CLI installieren:

```bash
python3 -m pip install .
codex-automate demo --reset
```

## Ziel einbringen

Ein Ziel wird per JSON beschrieben. Ein Beispiel liegt unter [examples/demo_goal.json](/Users/alex/Projects/git/Codex Automate/examples/demo_goal.json).

```bash
python3 -m codex_automate bootstrap
python3 -m codex_automate register-agent --name lead --capability orchestrator --capability planning
python3 -m codex_automate register-agent --name builder --capability backend
python3 -m codex_automate register-agent --name qa --capability qa
python3 -m codex_automate submit-goal --file examples/demo_goal.json
python3 -m codex_automate autopilot --max-iterations 10
```

## Hosting

- GitHub: vorbereitet ueber [.github/workflows/ci.yml](/Users/alex/Projects/git/Codex Automate/.github/workflows/ci.yml)
- Neon / Postgres: per `CODEX_AUTOMATE_DATABASE_URL`, `DATABASE_URL` oder `POSTGRES_URL`
- Vercel: HTTP-Control-Plane ueber [app.py](/Users/alex/Projects/git/Codex Automate/app.py) und [vercel.json](/Users/alex/Projects/git/Codex Automate/vercel.json)
- Dashboard: statisch in [public/index.html](/Users/alex/Projects/git/Codex Automate/public/index.html)

Die konkrete Deploy-Reihenfolge steht in [docs/deployment.md](/Users/alex/Projects/git/Codex Automate/docs/deployment.md).

## Aktueller Stand

Implementiert:

- persistenter SQLite-State fuer Goals, Work Packages, Agenten, Assignments und Events
- umschaltbarer Datenbank-Target fuer SQLite oder Postgres/Neon
- Orchestrator-Control-Loop fuer Scheduling, Lease-Recovery und Blocker-Resolution
- echter Worker-Runner fuer `codex exec` plus shell-basierter Test-Runner
- FastAPI-Control-Plane und statisches Dashboard fuer Vercel
- CLI fuer Bootstrap, Agent-Registry, Goal-Submission, Tick, Worker-Run, Autopilot und Dashboard
- Demo-Simulation mit drei Agenten und einem absichtlich erzeugten Blocker

Noch offen:

- parallelisierte Worker-Ausfuehrung statt rein sequenziellem Autopilot-Loop
- echte Live-Verifikation gegen eine echte Neon-Datenbank
- Priorisierung ueber Business Impact, Kosten und Deadline
- Benachrichtigungen und Monitoring
