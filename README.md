# Vereinskasse

Eine kleine, selbst gehostete Kassenverwaltung für Vereine. Bankumsätze werden aus
CAMT.052- oder CAMT.053-Dateien übernommen und anschließend kategorisiert und mit
digitalen Belegen vervollständigt.

## Funktionsumfang

- CAMT.052/CAMT.053-Import mit Duplikaterkennung
- unveränderte Archivierung der importierten Originaldatei inklusive SHA-256-Prüfsumme
- Buchungsjournal mit Jahres- und Prüfstatusfilter
- Kategorien und Zuordnung zu den vier steuerlichen Bereichen eines gemeinnützigen Vereins
- mehrere PDF-, PNG- oder JPEG-Belege je Buchung
- Status `Beleg fehlt`, `Vollständig` oder `Kein Beleg erforderlich`
- Jahresübersicht und CSV-Export
- Prüfprotokoll für Importe, Beleguploads und fachliche Änderungen
- responsives Webinterface, Einzelbenutzer-Anmeldung und CSRF-Schutz

Die Anwendung unterstützt den Arbeitsablauf, ersetzt aber keine steuerliche Beratung
und erhebt derzeit keinen Anspruch auf eine zertifizierte GoBD-Verfahrensumgebung.

## Start mit Docker Compose / Arcane

1. Repository in Arcane als Compose-Projekt einbinden.
2. `.env.example` als `.env` kopieren.
3. `SECRET_KEY` mit einem langen Zufallswert und `ADMIN_PASSWORD` mit einem sicheren
   Passwort setzen.
   Wenn der Zugriff ausschließlich über HTTPS erfolgt, zusätzlich `COOKIE_SECURE=true`
   setzen.
4. Den Stack starten und Port `8080` aufrufen.

```sh
cp .env.example .env
docker compose up -d --build
```

Für einen zufälligen Secret-Key eignet sich beispielsweise:

```sh
openssl rand -hex 32
```

Für Zugriff aus dem Internet sollte ein Reverse Proxy mit TLS vorgeschaltet werden.
Port `8080` muss nicht öffentlich freigegeben werden, wenn der Proxy das interne
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

Jede Bankbuchung erhält einen stabilen Fingerprint aus Konto, Datum, Betrag,
Bankreferenz, Gegenpartei und Verwendungszweck. Wiederholte Kontoauszüge erzeugen so
keine doppelten Buchungen. Die Original-CAMT-Datei und jeder Beleg werden mit einer
Prüfsumme gespeichert. Bankdaten lassen sich über die Oberfläche nicht verändern;
ergänzt werden nur Kategorie, Belegstatus und interne Notiz.

## Noch nicht enthalten

- direkter FinTS-/HBCI-Abruf
- mehrere Benutzer und Rollen
- Splitbuchungen
- revisionssichere Korrektur-/Stornobuchungen
- ZIP-Jahresarchiv und PDF-Kassenbericht
- automatische Belegerkennung/OCR

Diese Punkte sollten anhand des echten Vereinsablaufs priorisiert werden, statt sie
vorab in die erste Version einzubauen.
