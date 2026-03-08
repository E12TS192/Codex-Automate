# Konzept: Codex als selbststaendig arbeitendes Multi-Agent-System

## Zielbild

Der Benutzer gibt nur ein fachliches Ziel, Randbedingungen und Akzeptanzkriterien vor. Danach arbeitet ein zentraler Orchestrator-Agent mit mehreren spezialisierten Worker-Agenten. Der Orchestrator bleibt waehrend des gesamten Laufs die einzige Stelle mit globaler Sicht auf:

- Zielstatus
- offene und laufende Work Packages
- Auslastung und Gesundheitszustand aller Agenten
- Blocker, Risiken und Eskalationen

## Rollen

### 1. Orchestrator-Agent

Verantwortung:

- Ziel verstehen und in Work Packages zerlegen
- Prioritaeten setzen
- Abhaengigkeiten und Reihenfolge verwalten
- Pakete an passende Agenten vergeben
- Heartbeats, Leases und Blocker ueberwachen
- bei Problemen neue Pakete fuer Klaerung, Rework oder Review erzeugen
- finalen Fortschritt verdichten und an den Benutzer berichten

Der Orchestrator kommuniziert moeglichst wenig in Freitext. Er arbeitet auf Basis strukturierter Zustandsobjekte und Event-Deltas.

### 2. Worker-Agenten

Typische Faehigkeiten:

- `planning`
- `backend`
- `frontend`
- `qa`
- `review`
- `research`

Verhalten:

- holen oder empfangen ein klares Work Package
- senden nur Heartbeats, Statuswechsel, Blocker und Ergebnisreferenzen
- geben keine globale Wahrheit vor
- entscheiden nicht selbst ueber Prioritaeten ausserhalb ihres Pakets

## Kommunikationsmodell

Effizienz entsteht, wenn Agenten nicht im Chat miteinander verhandeln. Stattdessen gibt es einen zentralen State und kleine strukturierte Nachrichten.

### Nachrichtentypen

- `goal.created`
- `package.created`
- `package.assigned`
- `package.active`
- `package.blocked`
- `package.completed`
- `package.requeued`
- `agent.heartbeat`
- `assignment.expired`
- `orchestrator.action`

### Prinzipien

- Single source of truth: nur der zentrale State ist massgeblich.
- Delta statt Volltext: Agenten schicken nur Aenderungen.
- Pull/Poll fuer Worker: ein Agent braucht keine breite Chat-Historie.
- Artefakt-Referenzen statt langer Texte: Ergebnisse werden als Verweis oder kurze Zusammenfassung gemeldet.
- Lease statt blindem Vertrauen: ohne Heartbeat kann Arbeit neu vergeben werden.

## Datenmodell

### Goal

- Titel
- Objective
- Acceptance Criteria
- Status

### Work Package

- fachlicher Auftrag
- benoetigte Faehigkeit
- Prioritaet
- Status
- Abhaengigkeiten
- Blocker-Info
- Ergebniszusammenfassung

### Agent

- Name
- Faehigkeiten
- Status
- aktuelles Paket
- letzter Heartbeat

### Assignment

- welches Paket welchem Agenten zugeordnet ist
- Lease-Ablauf
- Ergebnis oder Abbruchgrund

### Event Log

- append-only
- nachvollziehbar und auswertbar
- Basis fuer Dashboard und Auditing

## Kontrollschleife des Orchestrators

1. Ziel annehmen und strukturieren.
2. Work Packages mit Prioritaet und Dependencies anlegen.
3. Idle-Agenten mit passenden Faehigkeiten suchen.
4. Pakete vergeben und Lease starten.
5. Heartbeats und Statuswechsel beobachten.
6. Bei Blockern ein neues `unblock`-Paket erzeugen.
7. Nach geloestem Blocker das urspruengliche Paket neu einplanen.
8. Bei abgelaufener Lease Paket requeueen und Agent degradieren.
9. Goal-Status rollupen und den Benutzer nur mit komprimierten Updates informieren.

## Warum das fuer Codex passt

Codex ist stark, wenn Aufgaben klar eingegrenzt sind. Ein Orchestrator-Agent sollte deshalb nicht versuchen, alles selbst zu erledigen, sondern:

- Kontext in kleine Pakete schneiden
- spezialisierte Agenten mit minimalem Kontext versorgen
- Ergebnisse zentral zusammenfuehren
- Fehler und Stalls aktiv behandeln

Damit sinkt Kommunikationsrauschen, und der globale Status bleibt jederzeit konsistent.

## Umsetzung in diesem Repository

Der erste Prototyp setzt genau diese Kernmechanik um:

- SQLite-State als zentrale Wahrheit
- Orchestrator-Control-Loop
- strukturierte Goal-Submission per JSON
- Worker-Simulation fuer Assignment, Blocker und Recovery

Das ist bewusst klein gehalten, aber bereits anschlussfaehig fuer echte Codex-Worker oder spaetere Queue-/Service-Architektur.

