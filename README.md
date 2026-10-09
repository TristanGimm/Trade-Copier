# GIMM · Copier Engine

Privater XAU/USD-Copier mit cTrader Open API und einem Dashboard im dunklen, violetten Stil des bestehenden GIMM Trading Journals. Für einen Pepperstone-Master und bis zu zehn eigene Follower, beispielsweise FTMO.

**Keine Demo-Website:** Konten, Positionen, Equity und Journal werden aus der cTrader-API geladen. Ohne Kontofreigaben bleiben die Datenansichten leer. Es gibt keine vorbelegten Beispieltrades oder erfundenen Renditen. Die Engine enthält echte Order-, Close- und SL/TP-Aufträge. Mit aktivierten Regeln kann sie tatsächlich handeln.

**Validierungsstand:** 70 automatisierte Protokoll-, Engine-, Schutz- und API-Tests mit isolierten Datenbanken und simulierten Brokerantworten bestanden, zusätzlich DOM-Integrationstest und echter Chromium-Browsertest (sieben Ansichten, Passwortwechsel, Abmeldung, 390-px-Mobilansicht ohne horizontalen Überlauf oder JavaScript-Fehler). Firefox bleibt als normaler Browser verwendbar; sein automatisierter Start scheiterte in dieser macOS-Umgebung. Die automatisierten Tests senden keine Brokerorders. Der Nutzer hat den grundsätzlichen lokalen Demo-Kopiervorgang bestätigt; die neuen Schließungs- und Override-Fälle müssen zusätzlich mit den eigenen Demo-Konten geprüft werden. Diese Tests belegen keine garantierte Prop-Challenge-Sicherheit.

## Architektur

```text
Firefox / Dashboard
       │ HTTPS · Login · CSRF
       ▼
Website / API (server.py) ─────────────┐
                                     │ SQLite / WAL
Copy-Engine (copier/engine.py) ────────┘
       │ Dauerhaftes TLS/TCP · Protobuf
       ├── cTrader Live (5035): Master + Live-Follower
       └── cTrader Demo (5035): optionale Broker-Demokonten
```

Website und Engine sind getrennte Prozesse. Browser schließen, Seitenwechsel oder Dashboard-Neustart stoppen keine laufende Engine. Die aktuelle Implementierung verwendet Python/asyncio, mit den offiziellen Spotware-Protobuf-Schemata und einem eigenen TLS-Transport. Es gibt keine Abhängigkeit vom alten Twisted-SDK. PostgreSQL/.NET sind für diese private Einzelinstanz nicht erforderlich; SQLite speichert Handelsabsichten und Positionszuordnungen dauerhaft. Die Engine-Dateisperre verhindert zwei aktive Handelsprozesse auf derselben Datenbank/Volume. Das ist **kein** verteilter Failover über mehrere Server; nur eine Engine-Instanz und ein lokales persistentes Volume verwenden.

## Was implementiert ist

- Einmaliger privater Administrator-Setup, Login, Logout und Passwortwechsel.
- Geschützte serverseitige Sitzungen, HttpOnly/SameSite-Cookies, CSRF-/Origin-Prüfung, PBKDF2-SHA256 mit 600.000 Iterationen, Anmeldelimit.
- cTrader-OAuth mit Handelsberechtigung, mehreren cTrader IDs und Kontoauswahl; Tokens verschlüsselt mit Fernet, Refresh-Token-Rotation.
- Echtes Kontodiscovery, Master-/Follower-Zuordnung, kontospezifische XAU/USD-Symbol- und Volumenauflösung.
- Kopieren neuer bestätigter Master-Einstiege mit festen Lots je Follower.
- SL/TP setzen, verschieben und entfernen; absolute Master-Preise nach bestätigter Follower-Eröffnung synchronisieren.
- Proportionale Teilverkäufe und Vollschließungen, einschließlich Änderungen während einer laufenden Follower-Eröffnung.
- Persistente Zuordnungen, geordnete Befehle pro Kopie, parallele Verarbeitung unterschiedlicher Follower, zentrale Request-Budgets.
- Heartbeats, Wiederverbindung, Kontoneuauthentifizierung und Positionsabgleich nach Neustart/Verbindungsabbruch.
- Keine blinden Orderwiederholungen nach einem Timeout. Unklare Aufträge bleiben sichtbar und blockieren weitere Handelsbefehle für die betroffene Kopie, bis ein Brokerzustand ihre Ausführung belegt.
- Tatsächliche Balance/Equity und offener Net-P&L in Kontowährung; fortlaufende Equity-Aufzeichnung.
- Historienimport der letzten 90 Tage und danach laufend, positionweise Trade-Auswertung, Winrate, Profit Factor, durchschnittlicher Gewinn/Verlust, realisierter Drawdown, Serien, CSV-Export.
- Kopierzuordnungen, gemessener Order-Roundtrip, Abweichungen und Aktivitätsprotokoll.
- Responsive Desktop-/Mobilansichten; automatisierte Browserprüfung mit **Chromium**, optional Firefox.
- Docker Compose für Website und Engine; optionaler Caddy-Reverse-Proxy.

## Lokale Einrichtung (ohne Deployment)

Python 3.12 wird empfohlen; die Python-Tests wurden auch mit Python 3.9 ausgeführt. Der automatisierte Browsertest verwendet standardmäßig Chromium; die Website funktioniert auch in Firefox.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/python -c "import secrets; print(secrets.token_urlsafe(32))"
.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Die beiden erzeugten Werte als `SETUP_TOKEN` und `TOKEN_ENCRYPTION_KEY` in `.env` eintragen. `.env` und Datenbank sind git-ignoriert. Der Verschlüsselungsschlüssel muss dauerhaft erhalten bleiben und getrennt von der Datenbanksicherung geschützt werden; ein anderer Schlüssel kann vorhandene Freigaben nicht entschlüsseln.

### cTrader-App registrieren

1. Im [cTrader Open API Portal](https://openapi.ctrader.com/) eine App registrieren und freischalten lassen.
2. Client-ID und Client-Secret in `.env` als `CTRADER_CLIENT_ID` / `CTRADER_CLIENT_SECRET` setzen.
3. Als Redirect-URI für die lokale Einrichtung **`http://localhost:8080/ctrader/callback`** in der cTrader-App registrieren. Für einen anderen Origin die URI entsprechend exakt anpassen. Ob der lokale HTTP-Origin für deine App zugelassen wird, bestimmt das Portal; andernfalls einen eigenen HTTPS-Testorigin verwenden.
4. `APP_ORIGIN=http://localhost:8080` ohne abschließenden Slash setzen.

Die Dateien laden `.env` beim direkten Python-Start nicht automatisch. In zwei Terminals jeweils deine vertrauenswürdige lokale `.env` laden und dann den jeweiligen Prozess starten:

```sh
set -a
source .env
set +a
.venv/bin/python server.py
```

Zweites Terminal:

```sh
set -a
source .env
set +a
.venv/bin/python -m copier.engine
```

1. `http://localhost:8080` in Firefox öffnen, E-Mail/Passwort (mindestens 12 Zeichen) und Setup-Token eingeben.
2. **Connect cTrader**. Die Konten bei Spotware mit der Berechtigung `trading` freigeben. Bei getrennten Pepperstone-/FTMO-cTrader-IDs den Ablauf für beide IDs durchführen.
3. Nach Rückleitung erscheinen unter **Settings → Available cTrader accounts** die freigegebenen Konten. Spotwares dokumentierter OAuth-Ablauf garantiert kein Zurücksenden des `state`-Parameters. Fehlt er, verlangt die Anwendung einen bewussten Abschluss der gerade gestarteten Freigabe im angemeldeten Workspace; es erfolgt kein stiller Codeaustausch. Bei vorhandener State-Rückgabe wird sie strikt geprüft. Nicht zu deiner Sitzung gehörende Freigaben werden verworfen.
4. **Add account**: Pepperstone als Master und FTMO als Follower einrichten. FTMO-Kapital wird zwingend als simuliert geführt. Den tatsächlichen XAU/USD-Symbolnamen beim Broker angeben, z. B. `XAUUSD`; Suffixe werden nicht geraten.
5. Warten, bis beide Konten synchronisiert sind. Unter **Copy Engine** Lots einstellen und den gewünschten Follower aktivieren.
6. **Enable new copies**. Ab jetzt werden **neue** bestätigte Master-Einstiege kopiert. Bereits beim Start offene Masterpositionen werden nicht nachträglich eröffnet.

`ENGINE_TRADING_ENABLED=true` erlaubt Handelsaufträge; die Datenbank startet trotzdem mit pausierten neuen Kopien. `ENGINE_TRADING_ENABLED=false` deaktiviert **alle** Handelsbefehle, auch die Verwaltung bereits kopierter Positionen. Zum normalen Pausieren deshalb den Dashboard-Schalter verwenden, nicht den Prozess deaktivieren.

## Exakte Kopiersemantik und Grenzen

- **Hedging-Konten:** Netting- und Limited-Risk-Konten werden mit Erklärung abgelehnt, weil sie die gewünschte individuelle Positionszuordnung bzw. den Einstieg ohne SL nicht zuverlässig erlauben. Der Master und alle Follower müssen Hedging unterstützen.
- **Nur XAU/USD:** Die Engine prüft zusätzlich die Basis-/Quote-Assets des gewählten Symbols. Andere Master-Instrumente werden ignoriert.
- **Feste Lots:** Jeder neu entstandenen Masterposition wird eine individuelle feste Follower-Größe zugeordnet. Ein neuer Mastertrade als neue Hedging-Position erzeugt eine neue Kopie. Größenzunahmen innerhalb derselben Positions-ID erzeugen keine zusätzlichen festen Lots; das maximale Mastervolumen wird als Referenz für spätere Teilverkäufe aktualisiert.
- **Volume:** API-Volumen wird in Hundertstel-Units angegeben. `lotSize`, Minimum, Maximum und Schritte kommen vom Broker. Gewählte Lots werden auf einen gültigen Schritt abgerundet; außerhalb der Brokergrenzen erfolgt kein Auftrag.
- **Pending Orders:** Noch nicht ausgeführte Limit-/Stop-Orders werden nicht vorgespiegelt. Erst die tatsächliche Ausführung eröffnet eine Market-Position auf dem Follower; der Preis kann abweichen. Pending-Änderungen/-Löschungen vor Ausführung erzeugen keine Followerposition.
- **Schutz beim Einstieg:** Ist der Master ohne SL/TP, wird kein künstlicher Schutz ergänzt. Gibt es bereits SL/TP, werden soweit darstellbar relative Schutzabstände beim Market-Einstieg mitgesendet. Absolute SL/TP sind bei cTrader-Market-Orders nicht erlaubt; nach dem Fill wird der bestätigte Follower auf die absoluten Master-Preise angepasst. Es gibt damit eine technische Zwischenphase; kein Versprechen atomarer, preisidentischer Ausführung.
- **SL/TP:** Auch das Entfernen wird übertragen. Follower-Trailing wird deaktiviert, damit die Master-Preise maßgeblich bleiben. Vom Master gemeldete Trailing-Änderungen werden gespiegelt. Ein abgelehnter Schutzpreis löst den Schutzstopp für den Follower aus; die Engine setzt keinen erfundenen Ersatzstop.
- **Teilverkauf:** Zielrestvolumen = ursprüngliches Follower-Volumen × aktuelles Master-Restvolumen / Referenz-Mastervolumen, auf Broker-Schritte abgerundet. Ein positiver Rest unter Mindestvolumen bleibt als Mindestrest bestehen, bis der Master vollständig schließt. Ein zu kleiner Teilverkaufsauftrag wird ausgelassen, bis eine zulässige Verringerung möglich ist.
- **Manuelle Follower-Eingriffe:** Ein vollständig unabhängig geschlossener Follower wird nie wieder eröffnet. Kleinere Follower-Volumina werden nicht wieder aufgefüllt. Nach der ersten bestätigten Synchronisation werden unabhängige SL-/TP-Änderungen auf dem Follower erkannt. Das jeweils geänderte Feld bleibt für diese Position manuell, auch bei späteren Master-Änderungen. Das andere Feld folgt weiter dem Master. Unter Position synchronisation stellt “Restore master SL/TP” beide Felder wieder auf Master-Verwaltung. Ein noch laufender Brokerauftrag ist eine Konfliktsituation: vor manuellen Änderungen seine Bestätigung abwarten. Master-Teilverkäufe und Vollschließungen bleiben aktiv; Änderungen am Follower erzeugen niemals Aufträge auf dem Master.
- **Pause:** Stoppt nur neue Einstiege. Zugeordnete Positionen erhalten weiterhin SL/TP- und Schließungsänderungen. Lot- und Follower-Aktivierungsänderungen gelten für künftige Einstiege; bestehende Kopien behalten ihre ursprüngliche Skalierung.
- **Schließungsabgleich:** Neben Ausführungsereignissen prüft ein eigener Monitor den Master etwa alle drei Sekunden, zuzüglich API-Laufzeit und Rate-Limits. Fehlende Positionen werden als geschlossen behandelt. Schließungsdeals ohne vollständiges Positionsereignis lösen zusätzlich einen Abgleich aus. Bestätigte Master-Schließungen werden auch bei inzwischen getrennter Master-Verbindung auf erreichbare Follower übertragen. Ein Teilfill der Follower-Schließung gilt nicht als vollständige Schließung.
- **Abbruch:** Nach Wiederverbindung werden bestehende Zuordnungen abgeglichen. Während einer Unterbrechung entstandene, zuvor nicht zugeordnete Masterpositionen werden nicht verspätet nachgehandelt. Der Copier kann bei Netzwerk-/Broker-Ausfällen keine lückenlose Ausführung garantieren.
- **Unklare Ausführung:** Eine unbestätigte Order wird nicht neu gesendet. Eine beim Abgleich gefundene Followerposition mit dem persistenten `gimm:`-Label wird der ursprünglichen Kopie zugeordnet. Ohne beweisbaren Brokerzustand bleibt der Fall als `uncertain` sichtbar; dann Brokerpositionen/Orders prüfen. Es gibt bewusst keinen blinden „Retry trade“-Knopf.
- **Request-Budget:** 40 normale Requests/s plus 5 reservierte Notfall-Requests/s und 4 historische Requests/s pro Verbindung, plus Heartbeat. Notfallschließungen warten nicht hinter der normalen Request-Warteschlange. Unterschiedliche Follower laufen parallel; Befehle derselben Kopie bleiben geordnet. Mehr CPU macht Broker-Ausführung nicht garantiert schneller.

## Schutzstopp und Notfallknopf

Der rote **Emergency stop** verlangt im Dashboard eine Bestätigung und speichert sofort eine dauerhafte Sperre für neue Kopien. Die Engine storniert auf **allen zugeordneten Followern**, auch deaktivierten, Pending Orders und schließt **alle Positionen, einschließlich manueller Trades und anderer Instrumente**. Der Master bleibt unverändert. Erst ein anschließender Brokerabgleich bestätigt `flat`; Absenden allein ist keine erfolgreiche Schließung. Solange der Schutzstopp aktiv ist, werden auch verspätete Fills geschlossen. Mehrfaches Klicken erzeugt keinen zweiten parallel laufenden Notfallauftrag.

Automatische Fehlerstopps betreffen alle Positionen/Orders des **betroffenen Followers** und pausieren neue Kopien global. Unter **Copy Engine → Slave protection** sind diese Startwerte einstellbar:

| Regel | Standard |
|---|---:|
| Neue Einstiege je Follower | höchstens 3 in 10 Sekunden |
| Gleichzeitige Kopien/Positionen je Follower | höchstens 5, einschließlich reservierter Einstiege |
| Signalalter vor Versand | höchstens 5 Sekunden |
| Nicht bestätigte SL-/TP-Abweichung | Schutzstopp nach 5 Sekunden |
| Position ohne SL | Schutzstopp nach 30 Sekunden |
| Ausfall/Stagnation einer benötigten Broker-/Kontoverbindung | Schutzstopp nach 10 Sekunden |

Auch ein abgelehnter SL-/TP-Auftrag, unklare Handelsbestätigung oder interner Synchronisationsfehler aktiviert den Schutzstopp. Fristen werden dauerhaft gespeichert, sodass Neustarts die Frist nicht neu beginnen lassen. Orderabsichten und Kopierzuordnungen verhindern doppelte Einstiege; vor dem tatsächlichen Socket-Schreibvorgang werden Sperre, Signalalter und Limits erneut geprüft. Der Broker kann Preise/Schließungen ablehnen: bestätigte Ablehnungen werden höchstens dreimal erneut versucht, danach ist manuelles Eingreifen erforderlich. **Unklare Schließungen werden nicht blind wiederholt.** Bleibt ihre Ausführung unbewiesen, zeigt die Oberfläche `uncertain` und verlangt Prüfung in cTrader.

Der anfängliche Einstieg ohne SL bleibt möglich, wie auf dem Master. Die 30-Sekunden-Frist ist aber eine bewusste zusätzliche Schutzregel: Setzt du länger keinen SL oder entfernst ihn länger, schließt der Follower. Es wird kein künstlicher SL gesetzt. Schutzstopps können daher vom Master abweichen und Gewinne/Verluste realisieren. Die Werte sind technische Startwerte, kein für deine Challenge berechnetes Geldrisikobudget.

Nach einem Notfall werden die betroffenen Konten fortlaufend geprüft. **Reset safety stop** geht erst bei frischen, gesunden Verbindungen, bestätigtem Nullbestand und ohne offene Ausführungsunsicherheit. Neue Kopien bleiben danach pausiert und müssen separat aktiviert werden.

Die leuchtend grüne **Connected**-Schaltfläche verlangt eine frische Website-Antwort, Worker- und Schutz-Heartbeat, aktuellen Broker-/Positionsabgleich für Master und alle aktivierten bzw. positionstragenden Konten, bestätigte Schutzpreise und keine aktiven Fehler/Sperren. Anklicken öffnet die einzelnen Prüfungen. Ein offener Browser allein beweist keine Brokerverbindung. Ein verlorener Browserzugang startet keinen Notfall; die unabhängige Engine soll auch bei geschlossenem Browser arbeiten.

**Technische Grenze:** Bei getrennter Brokerverbindung kann niemand über diese Verbindung schließen. Der Auftrag wartet dauerhaft bis zur Wiederverbindung. Sind Rechner/Engine ausgeschaltet, läuft auch die Schutzüberwachung nicht; Docker-Neustart ersetzt keinen unabhängigen Broker-Schutz. Bestehende brokerseitige SL/TP bleiben maßgeblich. Slippage, Gaps, Brokerablehnungen und FTMO-Tages-/Gesamtverlustregeln werden durch diese Mechanismen nicht garantiert beherrscht. `ENGINE_TRADING_ENABLED=false` verhindert auch Notfallschließungen und zeigt diese als wartend an.

## Verlustgrenzen: reine Anzeige je Follower

Unter **Trading accounts → Loss limits** oder in der jeweiligen Follower-Karte kannst du Startkapital, Max Loss und Max Daily Loss als Geldbeträge in **Kontowährung** sowie die Warnschwelle einstellen. Leere Verlustgrenzen deaktivieren die jeweilige Anzeige. Die Standardeinstellung warnt rot bei höchstens 20 % verbleibendem Budget. Negative Restbeträge zeigen eine bereits überschrittene Grenze. Es gibt keine automatische Sperre/Schließung aufgrund dieser Werte; auch ein Überschreiten ändert weder Kopierpause noch Orders.

- Gesamtuntergrenze = eingetragenes Startkapital − Max Loss.
- Tagesuntergrenze = Tagesstart-Balance − Daily Loss.
- Verbleibender Spielraum = aktuelle Broker-Equity − jeweilige Untergrenze.
- Tageswechsel: **00:00 Europe/Berlin**, mit Sommer-/Winterzeit. Die mitgelieferte Zeitzonendaten-Abhängigkeit unterstützt Docker-Systeme ohne eigene Zeitzonendatenbank.

Offene Ergebnisse und bereits in Balance/Net-P&L enthaltene Kosten wirken über die Equity auf den Spielraum. Noch nicht angefallene Schließungskosten sind keine bereits gebuchten Verluste. Es wird ein **festes Gesamtlimit** dargestellt, kein automatisch nachgezogener Drawdown. Ein-/Auszahlungen, Kontokorrekturen oder abweichende Challenge-Regeln musst du in deinen Referenzen berücksichtigen.

Die Tagesreferenz wird dauerhaft aus dem ersten Balance-Snapshot des jeweiligen Tages gespeichert. Beim Start um beispielsweise 14 Uhr ist das **keine historische Mitternachts-Balance**: Deshalb zeigt das Dashboard die Herkunft und bietet die manuelle Tagesstart-Balance für heute an. Prüfe diese Referenz gegen deinen Anbieter. Die manuelle Angabe gilt nur für das angezeigte aktuelle Datum; am nächsten Tag entsteht eine neue Referenz. Bei fehlender oder veralteter Equity wird die Anzeige als unvollständig/veraltet markiert.

## Lokaler Test nach diesem Update

1. In beiden bisherigen Terminals Website und Engine mit Ctrl+C stoppen. `.env`, Verschlüsselungsschlüssel und Datenbank behalten.
2. Im Projektordner `.venv/bin/pip install -r requirements.txt` ausführen.
3. In Terminal 1 die vertrauenswürdige `.env` laden (`set -a`, `source .env`, `set +a`) und `.venv/bin/python server.py` starten. In Terminal 2 ebenso laden und `.venv/bin/python -m copier.engine` starten. Neue Tabellen/Spalten werden automatisch ergänzt.
4. Firefox-Seite vollständig neu laden (Cmd+Shift+R), gegebenenfalls vorhandenen Schutzstopp erst nach bestätigtem Nullbestand zurücksetzen; neue Kopien separat aktivieren.
5. Ausschließlich Demo-Konten: neuen Trade öffnen, manuell am Master schließen; danach eigene neue Trades jeweils über Master-SL und Master-TP schließen lassen. In cTrader selbst prüfen, dass der Follower wirklich geschlossen ist.
6. Bei einer neuen Kopie den Follower-SL ändern, danach Master-TP ändern: Follower-SL soll unverändert bleiben, TP soll folgen. “Restore master SL/TP” testen. Anschließend Master vollständig schließen: Follower muss trotz manuellem SL schließen.
7. Verlustgrenzen speichern, Tagesreferenz prüfen. Für einen ungefährlichen Anzeigetest die Warnschwelle vorübergehend auf 100 % setzen und eine Grenze so wählen, dass sie oberhalb der aktuellen Equity liegt; prüfen, dass die rote Warnung erscheint, ohne Positionen zu schließen oder Kopien zu pausieren. Danach die tatsächlichen Werte zurückstellen.
8. Notfalltest nur auf Demo: offene Follower-Positionen und eine Pending Order anlegen, roten Notfallstopp bestätigen. Direkt in cTrader prüfen, dass alle Follower glatt sind und keine Pending Orders bestehen; der Master bleibt unberührt. Erst der nachfolgende Brokerabgleich darf im Dashboard `flat` bestätigen.

Bestehende technische Schutzregeln (beispielsweise die Frist ohne SL) gelten weiter. Die neuen Verlustanzeigen ändern sie nicht. Die Fristen für den manuellen Test vorher prüfen, damit ein technischer Schutzstopp nicht mit einer Verlustwarnung verwechselt wird.

## Zahlen und Analytics

- Reale Equity und simuliertes FTMO-Kapital werden getrennt ausgewiesen.
- Konten verschiedener Währung werden **nicht** ohne FX-Daten zusammengerechnet; im Dashboard die Währung auswählen. Es gibt noch keine automatische Währungsumrechnung.
- Balance und nicht realisierter Netto-P&L stammen aus der API; Equity = Balance + gemeldeter Net-P&L. Eventuelle zukünftige Schließungskommission ist gemäß cTrader nicht Teil des gemeldeten offenen Net-P&L.
- Equity-Samples beginnen mit der ersten Verbindung, in Minutenabständen. Das ist keine rückwirkende historische Equity.
- Historie startet bei 90 Tagen und wird inkrementell geladen, mit unterteilten Fenstern und Deal-ID-Deduplizierung. `All time` bedeutet die verfügbare importierte Historie, nicht die gesamte Brokerkontolaufzeit.
- Die API liefert für die Oberfläche höchstens die letzten **5.000 Deals**, **20.000 Equity-Samples**, **200 Positionszuordnungen**, **100 Befehle** und **100 Aktivitäten**. Langfristige Auswertungen können durch diese Grenzen unvollständig sein. Die Datenbank selbst behält ältere Daten. Für umfassendere Historien ist serverseitige Aggregation/Paginierung erforderlich.
- Geschlossene Trades werden je Konto und Positions-ID aus Schließungsdeals zusammengefasst. Solange eine Position noch offen ist, zählt sie nicht als abgeschlossener Trade. Nettoergebnisse folgen den vom Broker gemeldeten Schließungskomponenten.
- Max Drawdown ist Drawdown aus kumuliertem realisiertem Trading-P&L, kein intraday Equity- oder FTMO-Regel-Drawdown. Einzahlungen werden nicht als Trades/Gewinn gezählt.
- Latenzwerte sind gemessene **Order-Roundtrips** (Absenden bis Bestätigung), keine behauptete Master→Follower-Ende-zu-Ende-Latenz und kein Benchmark gegen Traders Connect.
- Kein erfundener Skill-Score, keine automatischen Empfehlungen und keine erfundenen FTMO-Verlustgrenzen. FTMO-Regelüberwachung sowie automatische Challenge-Limits sind noch nicht implementiert. Die konfigurierten technischen Schutzregeln können Follower vorzeitig schließen; sie ersetzen keine FTMO-Verlustregelberechnung.

## Docker später auf deinem Server

```sh
docker compose up -d --build
docker compose logs -f app engine
```

Compose liest `.env`. Website und Engine teilen das persistente `copier-data`-Volume. Port 8080 ist nur an `127.0.0.1` gebunden. `docker compose down` erhält Daten; `down -v` löscht sie.

### Domain / TLS

`copier-engine.gimmholding.com` passt zur Anwendung. DNS oder Deployment werden hier nicht ausgeführt.

1. A-Record `copier-engine` auf deine Server-IP setzen; AAAA nur bei funktionierender IPv6-Anbindung.
2. `APP_ORIGIN=https://copier-engine.gimmholding.com` in `.env` setzen.
3. Dieselbe Redirect-URI mit `/ctrader/callback` bei cTrader registrieren.
4. Caddy auf dem Host mit dem vorhandenen `Caddyfile` konfigurieren oder den bestehenden Reverse Proxy auf `127.0.0.1:8080` leiten. Ports 80/443 und ausgehend cTrader-TCP 5035/HTTPS 443 werden benötigt.
5. HTTPS aktiviert Secure-Cookies und HSTS automatisch. Browser-Origin und `APP_ORIGIN` müssen exakt übereinstimmen.

Bei einem Proxy innerhalb eines anderen Docker-Containers ist `127.0.0.1` dessen eigener Container. In diesem Fall ein gemeinsames Netzwerk und den Service-Namen `app:8080` benutzen; die mitgelieferte Caddy-Konfiguration geht von einem Proxy auf dem Host aus.

## Betrieb / Datensicherheit

- `/health` prüft Website und meldet, ob die Engine einen aktuellen Heartbeat hat. Compose überwacht beide Prozesse separat.
- Tokens und Client-Secret landen weder im Browser noch in Anwendungslogs. Callback-Querystrings werden aus den Website-Logs entfernt. Der vorgeschaltete Proxy sollte ebenfalls keine OAuth-Code-Querystrings protokollieren.
- Der Setup-Token schützt ausschließlich die erste Registrierung; danach gibt es keine öffentliche Registrierung. Administratorpasswort und Verschlüsselungsschlüssel sichern. Es gibt noch kein E-Mail-Passwortreset und kein MFA.
- Anmeldelimit: 10 Versuche pro IP in fünf Minuten im Prozessspeicher. Hinter einem Proxy teilen sich Nutzer die Proxy-IP; für den privaten Einzelzugang geeignet, für einen öffentlichen Mehrnutzerbetrieb zu ersetzen.
- Konsistente SQLite-Backups über SQLite-Backup-API oder während gestoppter Prozesse erstellen. Schlüssel separat sichern; Wiederherstellung prüfen.
- Nicht zwei Server mit separaten Kopien derselben Datenbank aktiv handeln lassen. Keine aktive/aktive Ausführung.
- Alte Konten aus der vorherigen Demo-Version werden in `legacy_demo_accounts` archiviert und nie als echte Brokerkonten angezeigt.
- Keine Notion-Anbindung implementiert. Das bestehende Journal wird nicht verändert.

## Tests

```sh
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -B -m unittest discover -s tests -v
node --check public/app.js
npm ci --ignore-scripts
npm run test:ui
.venv/bin/python -m playwright install chromium
.venv/bin/python tests/browser_smoke.py
```

Die Tests verwenden isolierte temporäre Datenbanken und simulierte cTrader-Antworten. Sie senden **keine** Brokerorders. Der DOM-Test prüft Einrichtung, alle sieben Seiten, Filter, CSV, Passwortänderung, persistierte Regeln, Aktivierung, Navigation und Logout. Node/jsdom wird nur für Entwicklungstests gebraucht, nicht im Docker-Laufzeitimage.

Der Chromium-Test prüft zusätzlich die Darstellung bei Desktop- und Mobilbreite und schreiben Screenshots unter `artifacts/` (git-ignoriert). Falls Playwright-Firefox in deiner Umgebung nicht startet, kann der installierte Firefox über Selenium geprüft werden:

```sh
.venv/bin/python tests/firefox_native_smoke.py
```

Selenium lädt gegebenenfalls Mozillas WebDriver. Beide Varianten verwenden frische Testprofile, nicht dein persönliches Firefox-Profil. Chromium wurde erfolgreich geprüft; Screenshots liegen in `artifacts/chromium-*.png`. Optional: `TEST_BROWSER=firefox .venv/bin/python -B tests/browser_smoke.py` nach Installation des Playwright-Firefox. Die beiden Firefox-Startwege scheiterten hier vor dem Laden der Website.

Wichtige Engine-Tests: doppelte Ereignisse, SL/TP-Setzen/Entfernen, Änderungen während Eröffnung, Schließung während Eröffnung, Teil-/Vollschließung trotz Pause, unabhängige Follower-Schließung, Mindestvolumen, alte Pending-Order-Ausführung, Netting-Ablehnung, unklare Timeouts und Wiederzuordnung.

SL-/TP-Bestätigungen ohne `clientMsgId` werden nur bei eindeutig passendem Konto, Position und vollständig bestätigtem SL-/TP-Paar zugeordnet. `ORDER_ACCEPTED` allein ist keine Bestätigung; ein passender bestätigter Positionszustand kann die Änderung dagegen belegen. Diese Fälle werden im Transporttest geprüft.

### Offizielle Protokollquellen

Die mitgelieferten `.proto`-Dateien stammen aus [Spotwares offiziellem Repository](https://github.com/spotware/openapi-proto-messages). Die generierten Python-Klassen werden im Repository mitgeliefert; Compiler nur für Änderungen erforderlich:

```sh
.venv/bin/python scripts/generate_protocol.py
```

Weitere Referenzen: [Verbindung und Heartbeat](https://help.ctrader.com/open-api/connection/), [OAuth und Kontofreigaben](https://help.ctrader.com/open-api/account-authentication/), [Order- und SL/TP-Nachrichten](https://help.ctrader.com/open-api/messages/), [Symbole, Volumen und Positionen](https://help.ctrader.com/open-api/model-messages/).

## English interface and removing accounts

The website uses English labels, validation errors, alerts, dialogs, activity messages, date formatting and CSV headers. The visible roles are **Master** and **Slave**. The internal `follower` API/database value is retained for compatibility with existing configurations; it is displayed as Slave. Existing technical activity messages are translated for display without changing historical records or user-defined account names.

Under **Trading accounts**, choose **Remove**, then confirm **Remove account**. This pauses new copies globally and disables new entries to that account while the engine checks the broker. Removal requires an online, synchronized account with no positions, no pending orders, no active copy mappings and no unresolved broker commands. A blocked removal shows its reason beside the account: resolve it in cTrader and retry Remove. No position is closed by the removal action itself.

Successful removal deletes the copier account configuration and its loss-limit configuration. Broker accounts, encrypted cTrader authorization, recorded trades, equity samples and closed copy history are retained. The account becomes available under **Add account**, where it can be added again. Removing a master releases the master slot so another authorized account can be assigned as master. New copies remain paused until explicitly enabled again. Stop/restart both local processes after this update and hard-refresh the browser with Cmd+Shift+R; schema migration runs automatically. Do not recreate `.env` or delete the database.

## GIMM server deployment

`compose.server.yaml` joins the existing `gimmholding-credentials_default` Caddy network.
The application has the unique proxy alias `trade-copier-app:8080` and exposes no host ports.
Use `docker compose -f compose.server.yaml up -d --build` from `/root/trade-copier`.
The existing proxy needs a `copier-engine.gimmholding.com` site routing to `trade-copier-app:8080`.
Keep `.env` and `data/` private and outside Git. The data directory must be writable by UID 10001.
Before migrating an existing database, stop its old engine and take a SQLite backup. Never run
both engines against separate copies of the same trading configuration. New copies should stay
paused during migration. Register `https://copier-engine.gimmholding.com/ctrader/callback` in the
cTrader application before reconnecting accounts on the public domain.
