# ElectriDrive 2.2.1 Cloud-QA Final — 2026-11-03

## Status and scope

**Abgebrochen; keine Freigabe.** Der Live-Konflikttest stoppte nach einem
`QaSafetyError` während der Metadatenprüfung zweier neu angelegter synthetischer
Dateien. Es gab danach keinen Sync, kein Update, keinen Retry und keine Löschung.
Die beiden neu angelegten Cloud-Dateien wurden absichtlich nicht entfernt. Ihr
Zustand wurde nach dem Sicherheitsstopp nicht erneut über Drive abgefragt.

Geprüfter Code: ElectriDrive **2.2.1**, Branch
`test/electridrive-213-cloud-final-qa` auf Basis von `main` Commit `57b7caf`.
Tag `v2.1.3` ist ein älterer Release-Stand und wurde nicht geprüft. Dieser
Bericht ist keine Abnahme von ElectriDrive 2.1.3.

## Ausgangszustand

- Vor der Arbeit war der verknüpfte Arbeitsbaum sauber; es gab keine lokalen
  Änderungen oder Stashes. Die lokalen `origin/main`-Refs waren veraltet und
  wurden gezielt von GitHub aktualisiert.
- Der ursprüngliche lokale HEAD war `d7f2859` auf
  `codex/electridrive-test-20261009-final`. PR #20 ist auf GitHub gemerged;
  inzwischen ist auch PR #21 integriert.
- Arbeitsbranch: `test/electridrive-213-cloud-final-qa`, Basis `57b7caf`.
- Drei fokussierte Offline-Regressionsfälle aus der vorherigen Analyse bleiben
  dauerhaft im Repository:
  `tests/test_qa_drive.py::test_same_name_uploads_register_and_revalidate_each_qa_file_id`,
  `tests/test_qa_drive.py::test_qa_metadata_revalidation_logs_exact_parent_guard_if_file_leaves_root`
  und `tests/test_twoway.py::test_reviewed_canonical_id_only_updates_selected_duplicate_after_reload`.
  Sie sollten dauerhaft bleiben: Sie sichern die ID-Registrierung gleichnamiger
  künstlicher Dateien, den Sicherheitsstopp bei einer Parent-Abweichung sowie
  das ausschließliche Aktualisieren der explizit gewählten Dublette ab. Sie
  verwenden ausschließlich isolierte Fakes und temporäre Verzeichnisse.
  Die Diagnoseänderungen ergänzen Regressionen für sichere Fehlermeldungen,
  redigierte Pfade, unbekannte Guard-Gründe, unterdrückten Exception-Kontext
  sowie unverändertes Logging normaler Fehler.

## Testumgebung und Sicherheitsprüfung

Verwendet wurden ausschließlich das vom Auftrag angegebene QA-Profil, der
angegebene lokale QA-Datenbereich und der bereits registrierte synthetische
Drive-Ordner. Die privaten absoluten Pfade und die Drive-ID werden in diesem
öffentlichen Repository-Bericht nicht wiederholt.

- Profil und lokaler Datenbereich waren getrennte, besitzergleiche Verzeichnisse
  mit Modus `0700`; die in der QA-Policy gespeicherten Inode-Identitäten stimmten.
- Der QA-Wrapper bestätigte den registrierten Ordner anhand ID, Name, Eltern
  und QA-Nonce. Ein ausschließlich lesender Start-Preflight listete drei
  bestehende Kindobjekte innerhalb dieses Ordners.
- Das effektive QA-Profil enthielt **0 Sync-Paare**. Ein Credential wurde lokal
  geladen; sein Scope entsprach dem vom QA-Modus verlangten Scope. Es war jedoch
  abgelaufen (`valid=False`, `expired=True`) und hatte ein Refresh-Token. Es
  wurde weder erneuert noch ein API-Aufruf ausgeführt.
- Vor dem Live-Versuch wurden bestehende lokale Testdateien inventarisiert und
  gehasht. Es wurde keine vorhandene Testdatei überschrieben oder gelöscht.
- Der lokale QA-Synchronisationszustand blieb unverändert:
  SHA256 `bf92fcbda1f8973ab958f29c215bc9f650bcf3905a11ef25123e95eb371dab55`,
  weiterhin 7 `sync_items` und 2 `remote_nodes`.

## Bereits vorhandene Live-Ergebnisse

Die folgenden Ergebnisse stammen aus dem vorhandenen QA-Lauf und dessen lokalen
Logs, nicht aus diesem neuen Lauf. Die Logzähler stimmen mit den vom Auftrag
genannten Ergebnissen überein:

| Vorhandenes Log | Uploads | Downloads | Remote-/Local-Trash | Konflikte | Fehler |
|---|---:|---:|---:|---:|---:|
| `upload.log` | 1 | 0 | 0 / 0 | 0 | 0 |
| `download.log` | 0 | 2 | 0 / 0 | 0 | 0 |
| `twoway-upload.log` | 1 | 0 | 0 / 0 | 0 | 0 |
| `twoway-download.log` | 0 | 2 | 0 / 0 | 0 | 0 |
| `final-download.log` | 0 | 1 | 0 / 0 | 0 | 0 |

SHA256-Werte der bereits vorhandenen künstlichen Testdateien, jeweils über die
vorhandenen Upload-/Download-/Two-way-Kopien verglichen:

| QA-Datei | SHA256 |
|---|---|
| `electridrive-qa-test.txt` | `b2aa0eefb7bb8c7501c89b0b608a6dd9b80a874363a018d6c1d93afbb6060953` |
| `qa-twoway-sync-e2e-LlYM9Sr2.txt` | `81768db7b6eb0ed78417ea427f59a72300ac45bc5fb39f4e927cdc815faf9a83` |
| `qa-upload-sync-e2e-LlYM9Sr2.txt` | `ca282dfe5fbe223d8685ab99bd9f4fddf949f4c02b7eb2c5dbacdbc07e702ecd` |

Diese vorhandenen Logs belegen nicht die nachfolgend abgebrochene
Konflikt-/Wiederanlaufprüfung und identifizieren nicht unabhängig die dabei
ausgeführte Programmversion.

## Neuer Live-Konfliktversuch — abgebrochen

Der lesende Preflight war erfolgreich. Danach wurde innerhalb des registrierten
QA-Ordners ein neuer, zufälliger Testname gewählt, der in der zuvor gelisteten
Bestandsaufnahme nicht vorkam:

`electridrive-final-qa-20261103-577b7eecd84b.txt`

Unter diesem identischen Namen wurden zwei unterschiedliche künstliche Dateien
angelegt. Beide Upload-Aufrufe kehrten erfolgreich zurück. Bei der unmittelbar
folgenden Metadatenprüfung löste der QA-Wrapper `QaSafetyError` aus. Der Lauf
wurde an dieser Stelle beendet; die Ursache wurde nicht weiter untersucht.

**Bekannte Cloud-Aktionen dieses Versuchs:** zwei neue Datei-Erstellungen,
null Updates, null Löschungen. Es wurde kein Sync-Auftrag gestartet. Die zwei
künstlichen Cloud-Dateien bleiben im registrierten QA-Ordner; ihre IDs werden
hier nicht veröffentlicht. Es wurde keine Bereinigung versucht.

Nach ausdrücklicher Freigabe wurde ein einzelner lesender Inventurversuch
vorbereitet. Die lokale Vorbedingung `assert creds and creds.valid` löste eine
`AssertionError` aus, bevor der Drive-Client instanziiert oder eine Cloud-Abfrage
gesendet wurde. `valid` beschreibt die aktuelle Access-Token-Gültigkeit, nicht
ob ein geladenes Credential grundsätzlich erneuerbar ist.

Für eine rein lokale Prüfung lautet die korrigierte, nicht erneuernde
Vorbedingung:

```python
credential_usable_or_refreshable = bool(
    creds and (
        creds.valid
        or (creds.expired and bool(creds.refresh_token))
    )
)
```

Diese Bedingung wurde nur mit einem Offline-Test-Double geprüft. Sie berechtigt
nicht zu einem API-Aufruf und garantiert nicht, dass Google einen späteren
Refresh akzeptiert. Es wurde kein Refresh und kein zweiter Inventurversuch
ausgeführt; der Cloud-Bestand bleibt nach den zwei Erstellungen unverifiziert.

Der lokale künstliche Fixture-Bereich bleibt ebenfalls unverändert erhalten:
`cloud-final-577b7eecd84b/`. SHA256:

| Lokale Fixture-Datei | SHA256 |
|---|---|
| `duplicate-a.bin` | `bc2adf8803f29b467e4f477056b875570d7acb55c14c3e95001d1327d5ea4538` |
| `duplicate-b.bin` | `0259ee963a1af8b2ab41b0b8d555d36a59ea4fa14bd48b5825b626c0d7702468` |
| `sync/electridrive-final-qa-20261103-577b7eecd84b.txt` | `57490fe360622d74113abde6b5e4ba458a3ee6b8565b00d3ea96dc7202d69d21` |

Keine Aussage über den unveränderten Zustand aller bestehenden Cloud-Objekte
wird nach diesem Fehler als unabhängig nachverifiziert ausgegeben. Vor dem
Fehler wurde kein API-Aufruf zum Aktualisieren oder Löschen eines bestehenden
Objekts ausgeführt.

## Offline-Prüfungen und Wiederanlauf

Die neu ergänzten Regressionen bestanden:

```text
3 passed in 0.13s
```

Die Drive-Fakes zeigen, dass zwei gleiche Namen mit unterschiedlichen IDs im
QA-Wrapper getrennt registriert werden und beide Metadatenprüfungen bestehen.
Eine danach simulierte Verschiebung einer Datei aus dem registrierten Elternordner
führt erwartungsgemäß zum `QaSafetyError`. Ein weiterer Fake-Test prüft, dass
eine in Settings serialisierte kanonische ID nach Reload nur die ausgewählte
Dublette aktualisiert; Inhalt und Zustand der zweiten Dublette bleiben gleich.

Der ursprüngliche `QaSafetyError`-Trigger ist **ungeklärt**. Die vorhandenen
Protokolle enthalten keine Exception-Nachricht oder Traceback des neuen Laufs;
der lokale Abbruch-Handler gab nur den Exception-Typ und die Phase
`create_artificial_duplicate_b` aus. Damit ist lediglich bekannt, dass beide
Upload-Aufrufe zurückgekehrt waren und der Fehler bei einer der folgenden
Metadatenprüfungen entstand. Der konkrete Guard lässt sich nicht rekonstruieren.

Codeprüfung von `QaDriveClient._remember`/`_check` und Offline-Fakes bestätigen:
Neue Datei-IDs werden im Client für ihre Elternbeziehung registriert und
validiert. Eine Dateinamenskollision allein ist keine QA-Sicherheitsverletzung.
ID-Übereinstimmung, Trash-Status, Elternpfad sowie registrierte Root-Identität,
Name, Parent und QA-Nonce werden geprüft. Die Fakes zeigen, dass zwei gleiche
Namen mit unterschiedlichen IDs korrekt nebeneinander bestehen. Eine simulierte
Verschiebung aus dem registrierten Parent löst erwartungsgemäß `QaSafetyError`
aus. Das beweist nicht, welche Bedingung im Live-Lauf fehlschlug.

Der `QaSafetyError` ist **kein bestätigter Produktfehler**. Die blockierende
Metadatenvalidierung ist eine **beabsichtigte Sicherheitsprüfung**; ob sie live
einen tatsächlichen Integritätsfehler oder einen vorübergehenden bzw.
unerwarteten Metadatenzustand erkannte, bleibt mangels Detailprotokoll offen.
Es wurde kein Produktionscode geändert und kein weiterer Cloud-Aufruf
vorgenommen.

Der `AssertionError` war ein **Test-/Diagnosefehler**, kein Produktfehler. Er
wurde mit einem Credentials-Test-Double reproduziert: Ein geladenes,
abgelaufenes Credential mit Refresh-Token hat `valid=False`, obwohl es
erneuerbar sein kann. Die Vorbedingung des Inventurtests wurde deshalb auf
„aktuell gültig oder abgelaufen mit Refresh-Token“ korrigiert. Diese Prüfung
liest nur lokale Zustandsfelder. `authenticate_interactive()` behandelt einen
solchen Fall bereits über den Refresh-Pfad; die OAuth-Lifecycle-Tests decken
das mit gemocktem Request ab. Für diese QA wurde weder die Refresh-Methode
aufgerufen noch ein Google-Client angelegt.

Bereits vorhandene Tests decken zusätzlich den Fail-closed-Preflight,
Konfliktpfad und explizite GUI-Auswahl, Abbruch ohne Speichern/Sync,
Settings-Reload, unterbrochene Downloads sowie manuelle Wiederaufnahme
unterbrochener Uploads ab.

Die vollständige Offline-Suite lief danach in einem frischen temporären
HOME/XDG-Profil ohne QA-Profil-Overrides oder Cloud-Zugriffe:

```text
646 passed in 5.83s
```

Die restlichen Phase-3-Prüfungen wurden nicht live ausgeführt. Die Suite
bestätigt Offline- und Test-Double-Verhalten, nicht die Ursache des Live-Fehlers
oder die Live-Wiederherstellung.

## Strukturierte QaSafetyError-Diagnose

Für den Offline-Follow-up wurde die Fehlerdiagnose ergänzt, ohne eine
Sicherheitsfreigabe oder einen Ablehnungszweig zu ändern. Jede bekannte
Safety-Regel protokolliert jetzt nur statische Metadaten: Fehlerkategorie,
stabile Regel-ID und bereinigte technische Begründung. Dieselben Werte stehen
als strukturierte Log-Felder `qa_safety_category`, `qa_safety_rule` und
`qa_safety_reason` bereit. Unbekannte oder nicht freigegebene Meldungstexte
werden durch eine feste generische Diagnose ersetzt; Pfade, IDs, Dateiinhalte,
Tokenwerte und rohe Ursachen werden nicht in die QaSafetyError-Diagnose
übernommen. Beim Umwandeln in einen QaSafetyError werden ursprüngliche
Ausnahmeursachen unterdrückt, damit Tracebacks keine Rohdaten aus diesen
Ursachen anhängen.

Der QA-Drive-Wrapper unterscheidet nun unter anderem nicht registrierte IDs,
Metadaten-ID-Abweichungen, gelöschte Objekte, Root-Name, Root-Parent,
Root-Nonce, Parent-Abweichungen, direkten Servicezugriff sowie gesperrte
Cloud-Such-, Listing- und Trash-Operationen. Generische Exception-Logger
unterdrücken zusätzliche Kontextdaten, wenn bereits ein `QaSafetyError`
vorliegt; normale Ausnahmen behalten ihr bisheriges Fehlerlogging. Der
Offline-Test für ein verschobenes Kind bestätigt konkret
`cloud-parent-mismatch`. Die Ursache des früheren Live-Fehlers lässt sich daraus
nicht rückwirkend ableiten. Die Tests prüfen außerdem, dass Pfade außerhalb des
QA-Bereichs und unbekannte, secret-ähnliche Grundtexte weder im Log noch in der
angezeigten Ausnahme erscheinen. Ein Regressionsfall bestätigt zusätzlich,
dass ein normaler Fehler weiterhin wie zuvor mit Traceback protokolliert wird.

**Aktuelle Offline-Validierung der Diagnoseänderungen:** gezielte QA-Suite,
72 bestanden. Die vollständige Suite lief danach mit temporärem HOME/XDG-Kontext
ohne QA-Profil-Override und ohne Cloud-Zugriffe:

```text
650 passed in 5.78s
```

## Fehlerklassifizierung

| Befund | Einstufung | Ergebnis |
|---|---|---|
| `QaSafetyError` nach den zwei künstlichen Datei-Erstellungen | Beabsichtigter Sicherheitsstopp; konkreter Trigger ungeklärt | Kein bestätigter Produktfehler; Detailgrund fehlt im Live-Protokoll |
| `AssertionError` der Inventur-Vorbedingung | Test-/Diagnosefehler | Zu strenge lokale `valid=True`-Annahme; kein API-Aufruf und keine Token-Erneuerung |
| Doppelte Namen mit unterschiedlichen IDs in den Fakes | Kein Produktfehler festgestellt | Beide IDs werden getrennt validiert; nur explizit ausgewähltes Duplikat wird im Sync-Test aktualisiert |
| Strukturierte `QaSafetyError`-Diagnose | Korrektur der Diagnose, kein gelockerter Schutz | Feste Kategorie, Regel-ID und bereinigte Begründung; unbekannte Meldungen werden sicher ersetzt |

## Befunde, Risiken und Freigabeempfehlung

1. **Sicherheitsstopp:** Der kontrollierte Live-Lauf erhielt nach zwei neuen
   Datei-Erstellungen einen `QaSafetyError` bei der Metadatenprüfung. Der
   ursprüngliche Safety-Stop-Grund wurde nicht erfasst. Offline-Fakes bestätigen,
   dass Duplikatnamen allein akzeptiert werden und veränderte Eltern-Metadaten
   korrekt blockiert werden; welcher Zustand live vorlag, bleibt unbekannt.
   Keine Wiederholung, Bereinigung oder weitere Cloud-Abfrage wurde vorgenommen.
2. **Offene Live-Prüfungen:** Konfliktanzeige mit echten Kandidaten,
   persistente Auswahl im Profil, Sync ausschließlich der ausgewählten Datei,
   Unverändertheit der zweiten Dublette und Live-Wiederanlauf sind nicht
   bestanden oder fehlgeschlagen, sondern **nicht ausgeführt**.
3. **Versionsstand:** Geprüft wurde `main` 2.2.1. Tag `v2.1.3` ist historisch
   und nicht Teil dieser Abnahme.
4. **Empfehlung:** Keine Freigabe, kein Merge und kein Release. Vor weiteren
   Drive-Aufrufen muss die Ursache des QA-Wrapper-Fehlers untersucht und der
   Zustand der zwei künstlichen Testdateien ausdrücklich geklärt werden.

Geheimnisse, Tokens, private Profileinstellungen und persönliche Daten sind
nicht enthalten. Seit dem ursprünglichen abgebrochenen Live-Versuch wurden für
diese Diagnoseänderungen keine Google-Drive-Aufrufe oder Cloud-Änderungen
vorgenommen.

## Code-Quality- und Abhängigkeitsprüfung für PR #22

Am 2026-11-03 wurden die zwei offenen Inline-Hinweise des GitHub-Code-Quality-
Bots in `electridrive/vfs/fuse_mount.py` geprüft. Beide beanstandeten das
direkte `raise self._error`, weil das Attribut optional ist. Die QA-Sonderfälle
wurden entfernt; `start()` und `wait()` bewahren wieder den bisherigen Vertrag:
ein im Mount-Thread gespeicherter Fehler wird als `RuntimeError(str(error))`
mit dem ursprünglichen Fehler als `__cause__` weitergegeben. Der neue
Offline-Regressionstest prüft diesen Vertrag auch für `QaSafetyError`. Die
strukturierte, bereinigte QA-Diagnose wird im Mount-Thread protokolliert, ohne
den nach außen sichtbaren Exception-Typ oder die Exception-Kette zu verändern.

Die fünf offenen Dependabot-Warnungen wurden gegen die GitHub-Advisories
geprüft. Sie betreffen ausschließlich exakte Versionen in
`packaging/requirements-release.txt`; die PR-Basis und der damalige
Release-Pin waren ebenfalls betroffen:

| Alert | Paket / alter Pin | Risiko laut Advisory | Minimaler Patch |
|---|---|---|---|
| [#7](https://github.com/ElectriCity-AI-Systems/electri-city-gdrive/security/dependabot/7), GHSA-6q48-6c7w-9gw2 | `certifi==2026.7.22` | **Critical**: der Trust Store enthielt emSign-Root-Zertifikate, zu denen kompromittierte Zwischenzertifikate existierten. Ein Angreifer mit betroffenem Schlüssel und On-Path-Position kann TLS-Verbindungen zu beliebigen Hostnamen imitieren bzw. verändern. Das CVSS-Feld der Advisory ist nicht bewertet; die Critical-Einstufung ist der GitHub-Severity-Wert. | `certifi==2026.10.19` entfernt die kompromittierten emSign-Roots und stellt nicht betroffene Roots wieder bereit. |
| [#8](https://github.com/ElectriCity-AI-Systems/electri-city-gdrive/security/dependabot/8), GHSA-fm9c-5rcf-2q5p | `pyasn1==0.6.4` | **High**, CVE-2026-119194, CVSS 7.5: quadratische CPU-Kosten in schemaloser BER/CER/DER-Container-Dekodierung mit speziell konstruiertem Input. | `pyasn1==0.6.5` |
| [#9](https://github.com/ElectriCity-AI-Systems/electri-city-gdrive/security/dependabot/9), GHSA-9m75-vc3g-4x39 | `pyasn1==0.6.4` | **High**, CVE-2026-119195, CVSS 7.5: quadratische CPU-Kosten bei konstruierten Strings und indefinite-length `ANY`. | `pyasn1==0.6.5` |
| [#10](https://github.com/ElectriCity-AI-Systems/electri-city-gdrive/security/dependabot/10), GHSA-mm57-4v3p-rchx | `pyasn1==0.6.4` | **High**, CVE-2026-119197, CVSS 7.5: quadratische CPU-Kosten beim Dekodieren binärer ASN.1-REAL-Werte. | `pyasn1==0.6.5` |
| [#11](https://github.com/ElectriCity-AI-Systems/electri-city-gdrive/security/dependabot/11), GHSA-2qm8-x737-gmw5 | `pyasn1==0.6.4` | **High**, CVE-2026-119196, CVSS 7.5: quadratische CPU-Kosten bei konstruierten BIT-STRING-Werten. | `pyasn1==0.6.5` |

Die vier `pyasn1`-Advisories betreffen Availability durch CPU-Verbrauch bei
untrusted ASN.1-Daten; sie beschreiben keine Vertraulichkeits- oder
Integritätsverletzung. Im ElectriDrive-Quellcode wurde kein direkter
`pyasn1`-Decoder-Aufruf gefunden; die Bibliothek kommt als transitive
Abhängigkeit des Google-Auth-Stacks hinein. Die konkrete Erreichbarkeit eines
betroffenen Decoders über die von ElectriDrive akzeptierten Daten ist daher
nicht bestätigt. Der Patch ist dennoch sinnvoll und kompatibel: die
festgelegte `pyasn1-modules==0.4.2`-Abhängigkeit verlangt `pyasn1>=0.6.1,<0.7.0`,
was Version 0.6.5 erfüllt. Es wurden keine größeren oder nicht betroffenen
Abhängigkeiten aktualisiert.

Die Release-Pins wurden auf `certifi==2026.10.19` und `pyasn1==0.6.5`
aktualisiert. Die Dependabot-Alerts werden auf dem Default-Branch erst nach
Merge bzw. dessen erneuter Dependency-Auswertung geschlossen; dieser PR wurde
nicht gemergt. Mit den exakten Release-Versionen in der lokalen virtuellen
Umgebung bestanden `pip check` ohne Abhängigkeitskonflikte, der Compile-Check
und die vollständige Offline-Suite (`651 passed in 5.76s`). Der fokussierte
FUSE-Test bestand mit 15 Tests. Ein separates lokales `pip-audit`/Bandit-Tool
ist in der Umgebung nicht installiert; daher wurde kein lokaler Scannerlauf
behauptet. GitHub CodeQL und die Paket-/Python-CI laufen nach dem Push auf dem
aktualisierten PR-Commit und werden im PR-Status geprüft.
