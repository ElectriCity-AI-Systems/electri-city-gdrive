# Analyse und Korrektur der Sync-Konflikte vom 29.10.2026

## Ergebnis und Sicherheitsgrenzen

Der ursprüngliche Upload-only-Sicherheitsstopp ist beabsichtigt: Mehrere
verschiedene Drive-IDs am selben lokalen Zielpfad dürfen nicht willkürlich
überschrieben werden, auch nicht bei identischem Inhalt. Es wurden Fehler in
Diagnose und Benutzerführung sowie ergänzende GUI-Randfälle korrigiert.

Diese Untersuchung erfolgte vollständig lokal. Kein Cloud-Sync und keine
Drive-Abfrage wurden gestartet, kein OAuth-Token und keine Zugangsdaten-Datei
geöffnet. Persönliche Quelldateien wurden nicht inventarisiert, gelesen oder
übertragen. Das deaktivierte Paar aus dem bereitgestellten Profil wurde nicht
aktiviert; dessen Einstellungen und Datenbank wurden nicht geöffnet oder
verändert. Reale OAuth-/Cloud-Tests aus früheren Berichten zählen ausdrücklich
nicht als Test dieser Änderungen.

Arbeitsbasis: `d64d175163204c6c8af9133c58e67874686f8745`, Branch `codex/electridrive-test-20261009-final`.
Die ursprüngliche Untersuchung erfolgte ausdrücklich **ohne Commit, Push
oder Staging-Änderung**. Mit dem anschließenden Finalisierungsauftrag sind
Commit und Push der überprüften Änderungen auf diesem bestehenden Branch
autorisiert. Es gab keinen lokalen Paketbuild und keine Änderung der installierten
Anwendung. Der vorhandene Debian-Build enthält diese neuen Quellcodeänderungen
noch nicht.

## Lokale Logbefunde und Bedeutung von Failed (4)

Untersucht wurde ausschließlich die JSONL-Logdatei
`../qa-connected/state/electridrive/electridrive.jsonl` (7 Datensätze, 2.116 Bytes).
Autorisierungsbezogene Logdetails wurden nicht ausgegeben oder kopiert. Die
vier relevanten ERROR-Einträge aus `electridrive.sync.twoway` lauten identisch:

```text
remote traversal incomplete: ambiguous upload target: duplicate remote file path
```

Ihre UTC-Zeitstempel sind am 29.10.2026:

- 16:36:48.434529
- 16:36:48.435024
- 16:36:48.435334
- 16:36:48.435603

Die Folge umfasst rund 1,074 Millisekunden. Der Code sammelt nach dem gesamten
Remote-Scan die mehrdeutigen Upload-Zielpfade. Für jeden relevanten lokalen
Zielpfad ohne gültige kanonische Drive-ID wird eine fehlgeschlagene Vorabprüfung
gemeldet. `_report_failure` erhöht `SyncReport.failed`; die bisherige GUI zeigt
unmittelbar diesen Zähler als `Failed (4)` an.

Damit zählt die 4 **vier fehlgeschlagene Prüfungen**, keine Upload-Versuche,
keine vier Wiederholungen und nicht die Zahl aller Dubletten-Kandidaten. Im
vorliegenden Meldungstyp entspricht jeder Eintrag einem mehrdeutigen lokalen
Upload-Zielpfad. Die enge Zeitfolge und der Status passen zu den vier Prüfungen
innerhalb eines Vorabscans. Da die alten Logs weder Laufkennung noch Pfade
enthalten, lassen sich Scan-Zuordnung und konkrete Dateinamen allein daraus
nicht zusätzlich verifizieren. Es gibt keinen Nachweis einer Retry-Schleife.

Der Vorabscan kehrt bei diesen Konflikten vor Dateitransfers zurück. Die GUI
konnte außerdem für jede Konfliktstelle einen eigenen Dialog öffnen und danach
noch eine allgemeine Warnung zeigen. Das sind mehrere Darstellungen eines
blockierten Ergebnisses, keine automatischen Sync-Neustarts.

Drive erlaubt unterschiedliche IDs mit gleichem Namen im selben Ordner.
Auch normalisierte Dateinamen können auf denselben lokalen Zielpfad fallen;
der Fehlertext allein unterscheidet diese Fälle nicht. Ein
frisches isoliertes Profil kann die frühere kanonische Datenbank-Zuordnung
nicht kennen. Ob dies die konkreten Dubletten hier erklärt oder wie sie
entstanden sind, ist ohne weitere Daten nicht festgestellt. Es wurde bewusst
keine Cloud-Inspektion und keine Auswahl einer realen Drive-ID vorgenommen.

## Nachgewiesene Fehler und Änderungen

| Bereich | Bisher | Korrektur |
|---|---|---|
| Wiederholte Meldungen | Identische Fehlermeldung für jeden betroffenen Pfad | Eine zusammengefasste Meldung je Fehlergrund mit Prüfungsanzahl; `failed` zählt weiterhin alle Prüfungen |
| Statusanzeige | `Failed (4)` ohne Phase oder verständliche Einheit | Strukturierte Vorabscan-Phase; z. B. `Blocked (4 paths)` statt vermeintlich vier fehlgeschlagener Transfers |
| Detailanzeige | Generische, mehrfach identische Texte | Betroffene relative Pfade aus dem Scanergebnis in der Oberfläche; keine neuen Pfad-/ID-Leaks in Logs |
| Upload-only-Review | Nach einem fehlgeschlagenen Lauf konnten auch harmlose Remote-only-Dubletten angeboten werden | `blocks_sync` unterscheidet tatsächlich blockierende Konflikte; nur diese werden angeboten |
| Veraltete Kandidaten | Fehler beim erneuten Root-Lookup konnte alte Kandidaten aus einer früheren Inventur erneut anzeigen | Inventurzustand wird vor der Root-Auflösung geleert |
| Review bei deaktiviertem Paar | Erneute Konfliktprüfung erforderte wieder einen Sync-Versuch | Expliziter Button `Review conflicts` nutzt ausschließlich das letzte Ergebnis im Arbeitsspeicher, auch deaktiviert und ohne Client |
| Modale Dialogfolgen | Konfliktdialoge öffneten automatisch; Abbrechen setzte die Folge fort | Erst verständliche Diagnose, danach ausdrücklich gestarteter Review; Abbrechen beendet die Folge |
| Gleichzeitig gestartete Jobs | `Sync all` und `Sync now` konnten überlappen | Gemeinsame GUI-Sperre, auch für programmgesteuerte Aufrufe; keine zusätzliche Aktivierung deaktivierter Paare während eines Jobs |
| Zerstörte Karten | Worker-Rückmeldung griff nach einem Karten-Neuaufbau auf gelöschte Qt-Objekte zu | Ergebnis wird am Paar gespeichert und die aktuelle Darstellung neu aufgebaut; deaktiviert bleibt deaktiviert |
| Verzögerte Qt-Löschung | Entfernte Karten konnten bis zur DeferredDelete-Verarbeitung sichtbar bleiben | Alte Karten werden sofort ausgeblendet |
| Fehlertoleranz | Scheduling-/Workerfehler konnten Sperren oder frühere Erfolgs-/Konfliktanzeigen stehenlassen | Steuerelemente werden wieder freigegeben; fehlgeschlagener Gesamtauftrag ersetzt betroffene alte Anzeigen und Kandidaten |
| Gesamtwarnung | Begrenzung der ersten acht Ergebnisse konnte einen späteren Fehler ausblenden | Erst fehlgeschlagene Paare filtern, dann die Anzeige begrenzen |

Die Kandidaten werden weiterhin nie automatisch ausgewählt. Eine explizite
Auswahl ändert lediglich die lokale kanonische Zuordnung in der Anwendung;
sie aktiviert kein Paar und startet keinen Transfer. Eine fehlende/geänderte
bereits ausgewählte ID bleibt ein Sicherheitsstopp. Native Workspace-Dateien,
Richtungsregeln, versteckte Dateien und die ursprünglichen Schutzprüfungen
bleiben durch die bestehenden Tests abgesichert.

Die kleinen Anzeige-/Cache-Nachbesserungen (vollständige Fehlersammlung bei
Sync all, Fehlerstatus nach Workerabbruch, Startfehler, Aktivierung während eines
Jobs und sofortiges Ausblenden alter Karten) wurden ebenfalls mit künstlichen
Regressionen überprüft. Auswahl und Speicherung in GUI-Tests verwenden nur
synthetische `FakeSession`-Objekte; kein reales Profil wird gespeichert.

## Reproduktion und Verifikation

Alle Tests verwenden künstliche Dateien unter temporären Testverzeichnissen
und `FakeDrive` bzw. `FakeSession`. Jeder vollständige Lauf erhält neue private
HOME-/XDG-Verzeichnisse, einen absichtlich nicht verfügbaren Keyring sowie eine
Python-Socket-Sperre für externe Verbindungen. Vorhandene Profil-/Scope-Overrides
werden nicht übernommen. Es wird keine authentifizierte Anwendung gestartet.

| Prüfung | Ergebnis |
|---|---|
| Vollständige unveränderte Ausgangssuite | 262 bestanden |
| Neue Engine-Reproduktionen vor Korrektur | Drei erwartete Fehler: vierfaches generisches Logging, falsche Review-Relevanz, alte Kandidaten beim Root-Fehler |
| Weitere GUI-Reproduktionen vor Korrektur | Erwartete Fehler bei automatischen Reviews, überlappenden Jobs, gelöschten Karten, veralteter Gesamtstatusanzeige und Schedulingfehlern |
| Vier künstliche lokale Pfade mit je drei Remote-Kandidaten | `failed=4`, eine zusammengefasste Meldung, vier blockierende Pfade; alle Inhalte/IDs und Transferzähler unverändert |
| Vollständige Abschlusssuite | **278 bestanden**, keine fehlgeschlagenen Tests |
| Syntax der fünf geänderten Python-Dateien | Erfolgreich mit `ast.parse` geprüft |
| Installierte Anforderungen | `pip check`: keine defekten Anforderungen |
| Qt-Rendering mit synthetischem deaktiviertem Paar | 780 px Breite, sichtbarer Offline-Review, kein Client, kein eingereichter Auftrag; Dialog ohne Vorauswahl |
| Unabhängiger Read-only-Review des Zwischenstands | 83 fokussierte Tests bestanden, keine kritischen oder wichtigen Befunde; Cache-/Statushinweise anschließend gezielt nachgebessert |
| Profil-Schutzkontrolle | Größe, Inode und Änderungszeit von Einstellungen, Datenbank und Original-Log unverändert |
| Git-Sicherheit während der Untersuchung | HEAD unverändert, Index leer; keine Commits, Pushes oder Remote-Aktionen |

Lokale Nachweise liegen privat außerhalb des Quellbaums in
`../qa-conflict-analysis-20261029/`: Red-/Green-Logs, `full-suite.log`,
`gui-render.log`, zwei synthetische PNGs und die Schutzmetadaten. Es wurden keine
realen OAuth-Dateien in dieses Verzeichnis kopiert.

Ausgeführter kompletter Testbefehl im Checkout:

```bash
python3 ../qa-conflict-analysis-20261029/run_offline.py -m pytest -q
```

Der produktive Code wurde in folgenden Dateien geändert:

- `electridrive/sync/twoway.py`
- `electridrive/ui/views/sync_view.py`
- `electridrive/ui/drive_path_review.py`

Die Regressionen stehen in `tests/test_twoway.py` und
`tests/test_sync_gui_disabled.py`. Es gibt keine Änderung am Konfigurationsformat
oder an der Datenbankschema-Version.

## Grenzen und verbleibende reale Konflikte

Die konkreten persönlichen Drive-Dubletten bleiben unangetastet und ungeklärt.
Ihre Namen und richtigen IDs stehen nicht in den bisherigen generischen Logs.
Ohne eine gesondert autorisierte Drive-Abfrage kann daraus keine belastbare
Dateiauswahl rekonstruiert werden; das deaktivierte Paar bleibt deshalb aus.

Der neue Review-Cache gilt ausschließlich für die laufende GUI-Instanz. Er wird
bei einem Neustart nicht aus den alten Logs rekonstruiert. Angezeigte Kandidaten
sind ausdrücklich der letzte Scanstand. Nach dem Speichern bleibt das frühere
Blockierergebnis historische Anzeige, bis ein separat autorisierter Sync die
Zuordnung erneut prüft. Ein erneuter ausdrücklicher Review darf die gespeicherte
Auswahl vor diesem nächsten Lauf noch ändern. Es gibt keinen automatischen
Neuversuch, keine automatische Umbindung und keine automatische Löschung.

Die Startsperre schützt diese GUI-Instanz, nicht unabhängige CLI-/Hintergrund-
Prozesse. Das Deaktivieren während eines bereits laufenden Jobs ist weiterhin
kein Abbruch dieses Jobs; die Bestätigungswarnung weist darauf hin. Eine neue
prozessübergreifende Sperrarchitektur wurde nicht eingeführt.

Keine reale Cloud-Abnahme, kein nativer Wayland-Lauf mit verbundenem Profil und
kein Paket-/Release-Build wurden für diese Änderung ausgeführt. Geprüft ist der
Quellcode dieser Änderungen mit künstlichen Offline-Daten.

## Erneute Finalisierungsprüfung

Vor dem autorisierten Commit und Push wurden der vollständige Code-/Test-Diff
und dieser Bericht erneut geprüft. Der Vergleich der Testsammlung mit der
Arbeitsbasis bestätigt genau **16 zusätzliche Regressionstestfälle**. Die
vollständige isolierte Suite bestand erneut mit **278 Tests** auf Python 3.14.4
und PySide6 6.12.0. Syntax, Importe, `pip check` und `git diff --check` sind grün.
Ein weiterer unabhängiger Read-only-Review des vollständigen Diffs bestand
ebenfalls die 278 Tests und fand keine kritischen oder wichtigen Probleme.

Veröffentlicht werden ausschließlich die drei geänderten Produktionsmodule,
die beiden Testdateien und dieser Bericht. OAuth-Dateien, persönliche
Einstellungen, Datenbanken, Logs und lokale QA-Artefakte gehören nicht dazu.
Keine produktive oder künstliche Cloud-Synchronisation wurde für diese
Finalisierung ausgeführt. Die bestehenden Profil- und Dateiauswahlen wurden
nicht verändert.

Der bestehende [Draft-PR #20](https://github.com/ElectriCity-AI-Systems/electri-city-gdrive/pull/20)
bleibt auf `main` gerichtet. Der tatsächliche CI-Status wird nach dem Push für
den neuen Head geprüft und im PR dokumentiert; frühere erfolgreiche Läufe sind
kein Nachweis für diesen neuen Commit. Kein Merge und kein öffentlicher Release.
