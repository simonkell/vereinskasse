# Vereinskasse

Eine kleine, selbst gehostete Kassenverwaltung für Vereine. Bankumsätze werden aus
CAMT-, MT940- oder flexibel zugeordneten CSV-Dateien übernommen und anschließend
kategorisiert und mit digitalen Belegen vervollständigt.

## Funktionsumfang

- CAMT.052/CAMT.053- und MT940-Import mit Duplikaterkennung
- frei zuordenbarer CSV-Import mit Vorschau und automatischer Spaltenerkennung
- bis zu drei Bankkonten und eine manuell geführte Barkasse
- Anfangsbestand und laufender Saldo je Konto
- Barbuchungen im gemeinsamen Buchungsjournal
- unveränderte Archivierung der importierten Originaldatei inklusive SHA-256-Prüfsumme
- Buchungsjournal mit Jahres- und Prüfstatusfilter
- Kategorien und Zuordnung zu den vier steuerlichen Bereichen eines gemeinnützigen Vereins
- mehrere PDF-, PNG- oder JPEG-Belege je Buchung
- Status `Beleg fehlt`, `Vollständig` oder `Kein Beleg erforderlich`
- Jahresübersicht und CSV-Export
- Splitbuchungen mit vollständiger Betragskontrolle
- unveränderliche Storno- und Korrekturketten für manuelle Barkassenbuchungen
- Jahresabschluss mit Vollständigkeitsprüfung und Änderungssperre
- eigenständig archivierbares ZIP je abgeschlossenem Jahr mit Buchungen, Belegen,
  Originalimporten, Prüfbericht und SHA-256-Manifest
- Prüfprotokoll für Importe, Beleguploads und fachliche Änderungen
- zeitlich begrenzte, widerrufbare Nur-Lese-Freigaben für Kassenprüfer
- unveränderliche Prüfungsstände, die spätere Änderungen nicht rückwirkend verändern
- responsives Webinterface, Einzelbenutzer-Anmeldung und CSRF-Schutz

Die Anwendung unterstützt den Arbeitsablauf, ersetzt aber keine steuerliche Beratung
und erhebt derzeit keinen Anspruch auf eine zertifizierte GoBD-Verfahrensumgebung.

## Veröffentlichung und Deployment

Pushes auf `main` starten den Workflow `.github/workflows/container.yml`. Er führt
zuerst die Tests aus und veröffentlicht anschließend Images für `linux/amd64` und
`linux/arm64` nach:

```text
ghcr.io/simonkell/vereinskasse:latest
ghcr.io/simonkell/vereinskasse:sha-<commit-sha>
```

`latest` ist für die automatische Aktualisierung in Arcane vorgesehen. Für einen
Rollback kann in der Arcane-`.env` bei `IMAGE_TAG` jederzeit ein unveränderlicher
SHA-Tag eingetragen werden.

## Einrichtung in Arcane

Das Image wird in GitHub Actions gebaut. Arcane baut es nicht erneut, sondern
synchronisiert die Compose-Datei, zieht das geprüfte Image und betreibt den Stack.

### 1. Öffentliches Image

Das GHCR-Paket ist öffentlich. Arcane kann das Image deshalb ohne hinterlegte
Registry-Zugangsdaten abrufen:

```text
ghcr.io/simonkell/vereinskasse:latest
```

### 2. Git-Repository verbinden

Unter `Customization → Git Repositories` das Repository
`https://github.com/simonkell/vereinskasse.git` ohne Authentifizierung eintragen.

Danach unter `Projects → From Git Repo` anlegen:

- Sync Name: `vereinskasse`
- Branch: `main`
- Compose File Path: `compose.yaml`
- Auto Sync: aktiviert

### 3. Umgebung konfigurieren

Im `.env`-Editor des Projekts folgende Werte setzen:

```dotenv
SECRET_KEY=<langer-zufaelliger-wert>
ADMIN_PASSWORD=<sicheres-passwort>
APP_PORT=8787
IMAGE_TAG=latest
MAX_UPLOAD_MB=20
COOKIE_SECURE=true
```

`COOKIE_SECURE=true` nur verwenden, wenn die Anwendung über HTTPS aufgerufen wird.
Den Secret-Key kann man auf einem beliebigen Rechner mit `openssl rand -hex 32`
erzeugen.

`APP_PORT` ist ausschließlich der Port auf dem Docker-Host. Der interne
Container-Port bleibt immer `8000`. Wenn `8787` ebenfalls belegt ist, kann ohne
Image-Neubau jeder andere freie Host-Port eingetragen werden.

Anschließend das Projekt deployen. Arcane kann neue `latest`-Digests über seine
Image-Polling- und Auto-Update-Jobs erkennen und als Compose-Projekt aktualisieren.

## Manueller Start mit Docker Compose

Nach dem ersten erfolgreichen GitHub-Workflow kann derselbe Stack auch ohne Arcane
gestartet werden:

```sh
cp .env.example .env
docker compose up -d
```

Für einen zufälligen Secret-Key eignet sich beispielsweise:

```sh
openssl rand -hex 32
```

Für Zugriff aus dem Internet sollte ein Reverse Proxy mit TLS vorgeschaltet werden.
Port `8787` muss nicht öffentlich freigegeben werden, wenn der Proxy das interne
Docker-Netz verwendet.

## Persistenz und Backup

SQLite-Datenbank, Belege und importierte CAMT-Originaldateien liegen gemeinsam im
Volume `vereinskasse_data`. Ein Backup muss das komplette Volume enthalten. Für ein
konsistentes einfaches Offline-Backup den Container kurz stoppen, das Volume sichern
und anschließend wieder starten.

Wiederherstellungen sollten regelmäßig testweise auf einem separaten Stack geprüft
werden. Ein reiner Export der SQLite-Datei genügt nicht, weil die Belegdateien separat
im selben Volume liegen.

## Lokale Entwicklung

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export SECRET_KEY=dev-secret
export ADMIN_PASSWORD=admin
flask --app 'app:create_app()' run --debug
```

Tests:

```sh
python -m unittest discover -s tests -v
```

## Datenmodell und Nachvollziehbarkeit

Jede Bankbuchung erhält unabhängig vom Importformat einen stabilen Fingerprint aus Konto, Datum, Betrag,
Bankreferenz, Gegenpartei und Verwendungszweck. Wiederholte Kontoauszüge erzeugen so
keine doppelten Buchungen. Die Original-Kontoauszugsdatei und jeder Beleg werden mit einer
Prüfsumme gespeichert. Bankdaten lassen sich über die Oberfläche nicht verändern;
ergänzt werden nur Kategorie, Belegstatus und interne Notiz.

Bestehende Installationen werden beim Start automatisch migriert: Bereits importierte
IBANs werden als Bankkonten angelegt und vorhandene Buchungen diesen Konten zugeordnet.
Prüfungsfreigaben speichern einen Snapshot der Buchungsdaten; der geheime Link wird nur
einmal angezeigt und in der Datenbank ausschließlich als SHA-256-Prüfwert gespeichert.

Ein Geschäftsjahr lässt sich erst abschließen, wenn alle Buchungen kategorisiert und
alle Belegstatus bearbeitet wurden. Danach sind Importe, Barbuchungen, fachliche
Änderungen, Splitbuchungen und neue Belege für dieses Jahr gesperrt. Ein bewusstes
Wiederöffnen bleibt möglich und wird im Prüfprotokoll festgehalten.

Fehlerhafte manuelle Barkassenbuchungen werden nicht gelöscht oder überschrieben.
Ein Storno erzeugt eine exakt entgegengesetzte Gegenbuchung; eine Korrektur ergänzt
außerdem eine neue Ersatzbuchung. Alle drei Einträge werden dauerhaft verknüpft,
im Prüfungslink gekennzeichnet und als `korrekturen.csv` in das Jahresarchiv aufgenommen.
Importierte Bankbewegungen können nicht in der Software storniert werden, weil sie den
tatsächlichen Kontoauszug abbilden.

## Noch nicht enthalten

- direkter FinTS-/HBCI-Abruf
- mehrere Benutzer und Rollen
- PDF-Kassenbericht
- automatische Belegerkennung/OCR

Diese Punkte sollten anhand des echten Vereinsablaufs priorisiert werden, statt sie
vorab in die erste Version einzubauen.

## Lizenz

Vereinskasse ist Open Source und wird unter der [MIT-Lizenz](LICENSE) veröffentlicht.
