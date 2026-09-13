SO STARTEN SIE VM AUTOMATIC
===========================

**VM Automatic (Video Merger Automatic)** ist die automatische Begleit-Anwendung zu VideoMerger.
Sie beobachtet einen Inbox-Ordner, erkennt komplette Audio-/Script-Jobs, wartet auf Dateistabilität,
wartet die Jobs nacheinander in einer persistenten Queue auf, randomisiert pro Job die Reihenfolge der
berechtigten VideoMerger-Clips und startet dann **den bestehenden VideoMerger-Workflow** – unverändert.

> **Wichtig:** VM Automatic ersetzt VideoMerger nicht. Es handelt sich um eine zweite, additive
> Anwendung im selben Repository. VideoMerger bleibt vollständig eigenständig nutzbar; keine seiner
> Funktionen wurde entfernt oder verändert. VM Automatic steuert nur **WANN** und **MIT WELCHEM JOB**
> der bestehende Renderer läuft – **WIE** gerendert wird, entscheidet weiterhin ausschließlich
> VideoMerger mit Ihrer Master-Konfiguration.

---

## 1 · Installation

VM Automatic verwendet die **gleiche** Python-Umgebung wie VideoMerger (eine `.venv`, keine zweite).

1. Führen Sie zuerst das gewohnte VideoMerger-Setup aus: `setup_windows.ps1`
   (richtet `.venv` und das lokale FFmpeg/FFprobe unter `tools\ffmpeg\bin` ein).
2. Führen Sie dann das VM-Automatic-Setup aus:

   ```
   .\setup_vm_automatic.ps1
   ```

   Das installiert die zusätzliche Abhängigkeit `watchdog` (ereignisgesteuerter Dateiwächter),
   prüft FFmpeg, führt einen Import-Selbsttest durch und legt optional einen Desktop-Shortcut an
   (`-NoShortcut`, um den Shortcut zu überspringen).

## 2 · VM Automatic starten / stoppen

**Starten:**

- Desktop-Shortcut „VM Automatic starten“, oder
- `.\VM Automatic starten.cmd`, oder
- `powershell -File .\run_vm_automatic.ps1` (für Konsolendetails: `.\run_vm_automatic.ps1 -Console`)

**Stoppen:** einfach das VM-Automatic-Fenster schließen (Bestätigungsdialog, falls etwas läuft) oder
den Button **⏹ Stop Watching** klicken und das Fenster schließen.

**Automation AN oder AUS – immer sichtbar:**

- Zustand `WATCHING` (grün): VM Automatic beobachtet den Watch-Ordner und verarbeitet die Queue.
- Zustand `STOPPED` (grau): VM Automatic ist **komplett aus** – es wird nichts beobachtet und nichts
  verarbeitet. Es gibt keinen „stillen Hintergrundbetrieb“. Nach einem Stoppen wird automatisch
  weiterverarbeitet, sobald Sie wieder **▶ Start Watching** drücken (der Zustand wird zwischengespeichert).

## 3 · Erste Einrichtung

1. **VideoMerger einmalig konfigurieren** wie gewohnt (Aspect, Übergänge, Musik, Untertitel,
   Intro/Outro, Qualität, …). Diese Master-Konfiguration (`config\settings.json`) ist für VM
   Automatic die maßgebliche Konfiguration und wird von VM Automatic **nur gelesen, niemals geändert**.
2. **VM Automatic starten** und in der GUI prüfen:
   - **Watch Folder (Inbox):** Zielordner, in den Sie die Jobs legen (Standard: `<Projekt>\vm_inbox`).
   - **Output Folder:** Standard: `<Projekt>\output`. Der Watch-Ordner und der Output-Ordner dürfen
     nicht identisch sein oder Unterordner voneinander sein (Schutz vor Output → Watcher → neuer Job
     → Output-Endlosschleifen).
   - **Clip-Pool-Ordner:** die Video-Bibliothek, die VideoMerger rendert (Standard: `<Projekt>\input`
     – derselbe Standard-Input-Ordner von VideoMerger). Wenn Sie in VideoMerger eine andere
     Bibliothek wählen, tragen Sie sie hier als Clip-Pool ein.
3. Auf **▶ Start Watching** klicken.

## 4 · Unterstützte Job-Struktur & Benennung

Ein Job ist ein **komplettes logisches Audio-/Script-Paket** im Watch-Ordner (nur direkte Dateien,
keine Unterordner). Die Benennung folgt der bestehenden VideoMerger-Konvention (gleicher
Dateinamen-Stamm, unterschiedliche Endung – exakt wie die automatische Script-Zuordnung im
VideoMerger-GUI):

```
Topic_001.mp3   (Voiceover)   →  wird erkannt: „Detected new job: Topic_001“ – Status: WAITING
Topic_001.txt   (Script)      →  nach Stabilität beider Dateien: READY → QUEUED → RUNNING
```

- **Audio-Formate:** alle von VideoMerger unterstützten (`mp3, wav, m4a, aac, flac, ogg, opus`).
- **Script-Formate:** alle von VideoMerger unterstützten (`txt, text, md`).
- Die Zuordnung ist **deterministisch** über den normalisierten Dateinamen-Stamm
  (Groß-/Kleinschreibung egal: `Topic_001.mp3` + `topic_001.txt` = Job `topic_001`).
  `Topic_001.mp3` + `Topic_2.txt` werden **niemals** versehentlich gepaart.
- **Standard-Regel:** Ein Job ist erst fertig, wenn **Audio UND Script** stabil vorliegen
  (der kanonische VideoMerger-Workflow ist Voiceover + Script → Untertitel). Liegt nur das Audio
  vor, bleibt der Job im Warte-Zustand, bis das passende Script erscheint.
  (Optional: In der Konfiguration `require_script` auf `false` setzen, um reine Voiceover-Jobs ohne
  Untertitel zu erlauben.)
- Mehrere Dateien mit gleichem Namen (z. B. `Job_001.mp3` **und** `Job_001.wav`): die erste
  (natürliche Reihenfolge) gilt, die weiteren werden als Konflikt im Log vermerkt.
- Temporäre/versteckte Dateien (`.tmp`, `.part`, `.crdownload`, `~$…`, `hidden…`) und
  VideoMerger-Ausgabennamen (`MainVideo_…`, `FinalVideo_…`, `merged_…`) werden **nie** als Jobs
  behandelt. Ihre **eigenen Ausgaben** werden zusätzlich über den bestehenden
  `GeneratedOutputStore` von der Clip-Auswahl ausgeschlossen – eine Output-Feedback-Schleife ist
  dadurch doppelt ausgeschlossen.

## 5 · Stabilitätsprüfung (Dateien werden nicht „halb kopiert“ verarbeitet)

Nur weil ein Dateisystem-Ereignis eintrifft, beginnt VM Automatic **nicht** automatisch. Eine Datei
gilt erst als bereit, wenn für die konfigurierte **Stabilitätsdauer** (Standard 8 s, Bereich
1–300 s):

- die Datei existiert und eine normale Datei ist,
- die Größe > 0 ist,
- **Größe und Änderungszeit sich nicht mehr ändern**,
- die Datei geöffnet und gelesen werden kann,
- sie nicht gesperrt ist (sofern das Betriebssystem das erkennen kann; auf Windows wird der
  Schreibzugriff getestet).

Dabei wird nur mit Metadaten (Größe + Änderungszeit) gearbeitet – **keine** Hash-Berechnung großer
Dateien, dadurch bleibt die Anwendung im Leerlauf sehr leicht. Läuft das Kopieren noch, wird die
Stabilitätsfrist bei jeder Änderung automatisch zurückgesetzt
(`COPYING → WAITING_FOR_STABILITY → STABLE → READY → QUEUED → RUNNING`).

## 6 · Sofort-Ereignisse + 5-Minuten-Sicherheits-Scan

**Primär:** VM Automatic nutzt einen ereignisgesteuerten Dateiwächter
(`watchdog`; auf Windows ReadDirectoryChangesW) – neue Jobs starten also **sofort** nach
Stabilität, nicht erst nach einem Timer.

**Sekundär:** alle **300 Sekunden** (konfigurierbar, Button **Scan Now** für sofort) führt VM
Automatic einen Reconciliation-Scan durch: Der Watch-Ordner wird gescannt, der Dateizustand mit dem
persistierten Job-Zustand abgeglichen, verpasste Jobs werden erkannt und in die Queue aufgenommen,
abgeschlossene Jobs bleiben abgeschlossen (Duplikatschutz), geänderte Eingaben von wartenden Jobs
werden neu stabilisiert. Der Scan ist ein Sicherheitsnetz, **keine** Verzögerung.

**Startreihenfolge (jeder Start):** Persistenter Zustand laden → unterbrochene Jobs wiederherstellen
→ **sofortiger** Reconciliation-Scan → Dateiwächter starten → Queue verarbeiten.

## 7 · Queue

- **Nacheinander, immer nur ein Job** (kein paralleles Rendering – Schutz vor FFmpeg-/CPU-/GPU- und
  Cache-Kollisionen sowie doppelten Ausgaben).
- Deterministische Reihenfolge: natürliche Sortierung der Job-Ids (`001 → 002 → … → 010`).
- Zustände: `NEW → WAITING_FOR_STABILITY → READY → QUEUED → RUNNING → SUCCEEDED`, bei Problemen
  `RETRY_PENDING`, `FAILED`, nach einem Crash `INTERRUPTED`.
- **Duplikatschutz:** Die Job-Identität ist der normalisierte Namenstamm; derselbe Job kann nicht
  zweimal in der Queue stehen. Ein `SUCCEEDED`-Job wird **niemals** erneut verarbeitet – auch nicht,
  wenn der 5-Minuten-Scan seine Dateien wieder sieht.
- **Pause Queue / Resume Queue** hält die Queue an, ohne laufende Jobs zu stören.

## 8 · Randomisierung pro Job (Kernfunktion)

Bevor jeder **neue** Job gerendert wird:

1. Der berechtigte Clip-Pool wird mit den **bestehenden VideoMerger-Regeln** aufgelöst
   (Clip-Pool-Ordner, unterstützte Formate, Ausschlüsse, Ihre gespeicherte aktive Reihenfolge).
2. Es wird **genau das** ausgeführt, was im VideoMerger-GUI „Randomize Order“ macht – dieselbe
   unvoreingenommene Fisher-Yates-Permutation (`randomize_order`) – aber **nur job-lokal**.
3. Die Zufallsfolge gehört ausschließlich zu diesem Job (zusammen mit ihrem **Seed** gespeichert)
   und wird für das Rendering verwendet.

Folgerichtig:

- **Neuer Job → neuer Seed → neue Clip-Reihenfolge** (Job 001: A→F→C→D→B, Job 002: D→B→A→F→C).
- **Retry** verwendet standardmäßig denselben Seed (dieselbe Folge – reproduzierbar).
- **Retry + Randomize Again** erzeugt ausdrücklich eine neue Zufallsfolge.
- Ihre **gespeicherte manuelle/aktivte Reihenfolge in VideoMerger bleibt unverändert** – die
  Randomisierung wird nicht in Ihre Master-Konfiguration geschrieben.
- Randomisierung bedeutet **nur** Clip-Reihenfolge: keine Ordnerumgehungen, keine Qualitätsfilter-
  Umgehungen, keine Clips außerhalb Ihres Pools, keine Content-Analyse. Die bestehende
  Clip-Kontinuitätsschutzes (keine unbeabsichtigten A→A-Wiederholungen, korrekte Loop-/Hold-
  Grenzwerte) bleibt aktiv, weil der bestehende Renderer unverändert genutzt wird.

## 9 · Master-Konfiguration von VideoMerger

`MASTER-CONFIG (config\settings.json)` + `JOB-INPUT (Audio+Script)` + `JOB-LOKALE CLIP-FOLGE`
= `JOB-AUSFÜHRUNG`.

VM Automatic darf bei einem Job **nur** diese zwei Dinge ändern:

- der job-spezifische Input (Voiceover + Script),
- die job-lokale, randomisierte Clip-Reihenfolge.

Alles andere kommt unverändert aus Ihrer VideoMerger-Master-Konfiguration – **nicht** geändert wird
u. a.: Untertitel-Stil/-Schrift/-Größe/-Position/-Animation, Sprache, Long-Form-Musik, Shorts-Musik,
Musiklautstärke, Ducking, Transitionstyp/-dauer, Output-Presets, Qualität, Auflösung, Framerate,
Codecs, Original-Audio, Watermark, Intro, Outro, Bild-Einfügung, Duration Before Merge, Loop-Modus,
Hold Last Frame, Debug-Overlay, „Continue After Alignment Warning“. Beide Schalter bleiben beim
Standard **AUS**, solange Sie sie in VideoMerger nicht selbst aktivieren.

## 10 · Long-Form / Shorts-Verhalten

VM Automatic respektiert den konfigurierten Workflow:

- **Outputs = Auto (Standard):** genau der Aspect Ihrer Master-Konfiguration
  (`16:9` = Long-Form, `9:16` = Shorts).
- **Long-Form / Shorts / Long-Form + Shorts:** wählbar in der VM-Automatic-GUI (bzw. Konfiguration).
  Bei „+“ läuft der **bestehende** Workflow einfach zweimal – einmal pro Aspect – mit derselben
  job-lokalen Clip-Folge. Es gibt keine zweite, parallele Output-Implementierung.
- **Workflow = Auto (Standard):** „One-Click Complete“ (Final Video mit Intro/Outro/Quote), wenn in
  Ihrer Master-Konfiguration Intro und/oder Outro und/oder eine aktive Quote-Karte zugewiesen sind;
  andernfalls „Main Video“ (Stage 1). Auch hier: nur bestehende VideoMerger-Einstiegspunkte
  (`create_main` / `create_complete`) werden verwendet.
- Die getrennten Einstellungen (z. B. eigene Shorts-Musik, Shorts-Übergänge, Shorts-Untertitel-
  Profil) gelten, wie Sie sie in VideoMerger gespeichert haben – VM Automatic „vermischt“ nichts und
  legt keine geteilten Werte über Ihre Profile.

## 11 · Ausgaben

- Standard: `<Output-Ordner>\<JobName>\` (eigener Unterordner pro Job, sauber trennbar;
  abschaltbar per `job_subfolders`).
- Die Dateinamen bleiben die gewohnten VideoMerger-Namen (`MainVideo_16x9.mp4`,
  `FinalVideo_16x9.mp4`, `_no_subtitles`-Varianten, SRT, VTT, YouTube-Metadaten …).
- **Erfolg = verifiziert:** Ein Job wird nur `SUCCEEDED`, wenn (1) der VideoMerger-Workflow
  erfolgreich ausgeführt wurde, (2) die erwarteten Dateien existieren, (3) sie nicht leer sind,
  (4) sie lesbar sind und (5) sie stabil (Größe/Zeitstempel) sind. „FFmpeg ist gestartet“ zählt
  ausdrücklich nicht.
- **Eingabedateien bleiben standardmäßig unverändert** im Watch-Ordner (nichts wird gelöscht oder
  verschoben). Optional: „Beendete Eingaben archivieren“ (Standard AUS) verschiebt die fertigen
  Eingaben in `vm_archive\`.

## 12 · Fehler & Wiederholungen (Retry)

- Fehler werden protokolliert, der Job bleibt bestehen (Eingaben werden gewahrt).
- Standard: **max. 3 Versuche** (konfigurierbar 1–20). Automatisch:
  `FAILED-Versuch → RETRY_PENDING → nächster Versuch … → FAILED (muss bearbeitet werden)`.
  Unendliche automatische Wiederholungen sind ausgeschlossen.
- Manuell: **Retry** (gleicher Seed = gleiche Clip-Folge) und **Retry + Randomize Again**
  (neue Zufallsfolge) – jederzeit aus der GUI, auch nach `FAILED`.
- Ein `SUCCEEDED`-Job lässt sich **nicht** erneut auslösen (Duplikatschutz).

## 13 · Logs

- Datei-Log: `logs\vm_automatic_JJJJ-MM-TT.log` (Button **Open Logs**).
- GUI-Log: letzte Ereignisse live im Fenster.
- Jeder Job protokolliert: Job-ID, Eingabedateien, Erkannt-/Stabil-/Queue-Zeit, Seed, Clip-Folge,
  Workflow/Aspect, Render-Start/-Ende, Ausgaben, Ergebnis, Fehlerdetails (zusätzlich als
  maschinenlesbarer Verlauf im Zustands-Journal des Jobs).
- Beispiel-Zeilen:

  ```
  Detected new job: Topic_004
  VM Automatic: Waiting for stability – Topic_004.mp3 (warming).
  VM Automatic: Datei stabil: Topic_004.mp3 (1048576 Bytes).
  VM Automatic: Job Topic_004 – Job ready (Dateien stabil).
  VM Automatic: Randomizing 42 eligible clips (Seed 161234567): 12.mp4 → 03.mp4 → …
  VM Automatic: Starting Long-Form
  VM Automatic: Starting Shorts
  VM Automatic: Job Topic_004 – Job completed successfully.
  ```

## 14 · Zustands-Wiederherstellung (Crash/Neustart)

Der gesamte Job-Zustand liegt persistent in `config\vm_automatic\state.json` (atomare Schreibweise,
übersteht Neustarts und Abstürze).

- War ein Job beim Beenden/Absturz `RUNNING`: Beim nächsten Start wird er erkannt, als
  `INTERRUPTED` markiert und (solange Versuche übrig sind) als `RETRY_PENDING` eingeplant –
  er wird **nicht** stillschweigend als erfolgreich behandelt.
- Abgeschlossene Jobs (`SUCCEEDED`) werden nie erneut bearbeitet.
- Eine zweite laufende Instanz wird per Prozess-/Datei-Sperre verhindert
  (`config\vm_automatic\instance.lock`) – zwei Instanzen können dieselbe Queue nie gleichzeitig
  bedienen. Die Sperre wird vom Betriebssystem bei Prozessende automatisch freigegeben.

## 15 · Windows-Autostart (optional)

Standard **AUS** und **nie stillschweigend installiert**. In der VM-Automatic-GUI: Haken
„Mit Windows starten“. Damit wird der übliche Windows-Mechanismus verwendet: der pro-Benutzer-
Run-Schlüssel `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` (keine Admin-Rechte, kein
systemweiter Eintrag). Den Haken entfernt, ist der Eintrag wieder gelöscht. Auf anderen
Betriebssystemen ist die Option einfach nicht verfügbar.

## 16 · VM Automatic anhalten / beenden

- **⏹ Stop Watching:** Wächter und Queue werden angehalten; ein laufender Render läuft zu Ende
  (oder wird mit „Nein“ im Fensterdialog abgebrochen).
- **Fenster schließen:** Automation komplett aus. Bei laufendem Render fragt VM Automatic, ob der
  Render abgebrochen werden soll; der Job bleibt als `RUNNING` im Zustand und wird beim nächsten
  Start über die Wiederherstellung wieder aufgefangen (siehe oben).
- Danach: nichts wird beobachtet oder verarbeitet, bis Sie VM Automatic erneut starten
  (kein Hintergrund-„Magic“).

## 17 · Fehlerbehebung (Troubleshooting)

| Symptom | Ursache / Abhilfe |
| --- | --- |
| „VM Automatic läuft bereits“ | Zweite Instanz? Eine VM-Automatic-Instanz gleichzeitig; alte Prozesse in der Taskleiste beenden. |
| Job kommt nicht aus `WAITING_FOR_STABILITY` | Datei wird noch kopiert/geändert (Stabilitätsdauer läuft bei jeder Änderung neu). Große Dateien: Stabilitätsdauer prüfen, Kopierquelle warten. |
| `FAILED: Keine geeigneten Videoclips im Clip-Pool-Ordner` | Clip-Pool-Ordner leer oder falsche Formate; die gewohnten VideoMerger-Eingabeordner-Regeln gelten. |
| `FAILED: Voiceover-Datei fehlt` | Eingabe im Watch-Ordner gelöscht/umbenannt; Datei erneut ablegen oder Retry. |
| `SUBTITLE GENERATION FAILED …` | VideoMerger-Regeln gelten: Voiceover/Script müssen zusammenpassen (Kompatibilität), ASR-Modell vorhanden; Details im Log. „Continue After Alignment Warning“ bleibt Standard AUS und wird nicht automatisch gesetzt. |
| Ausgabe anders erwartet | Prüfen Sie Ihre **VideoMerger-Master-Konfiguration** – VM Automatic ändert keine Render-Einstellungen. |
| Watch-/Output-Ordner-Verwechslung | VM Automatic verweigert den Start, wenn beide Ordner gleich oder ineinander liegen. |
| Wächter reagiert träge | `watchdog` installiert? (`setup_vm_automatic.ps1`). Ohne watchdog fällt VM Automatic automatisch auf einen 1-s-Polling-Modus zurück (wird im Log angezeigt). |
| FFmpeg-Fehler | `setup_windows.ps1` erneut ausführen (lokales FFmpeg unter `tools\ffmpeg\bin`); VideoMerger-Diagnose öffnet. |

## 18 · Was VM Automatic NICHT tut

- ersetzt VideoMerger nicht (VideoMerger bleibt voll funktionsfähig),
- implementiert keinen zweiten Video-Renderer,
- ändert Ihre VideoMerger-Einstellungen nie,
- löscht/verschiebt Eingaben nie ohne Ihre explizite Archivar-Option,
- startet parallel mehrere Renderjobs nicht,
- aktiviert Debug-Overlay oder Alignment-Override nie stillschweigend,
- verarbeitet eigene Outputs nie als neue Eingaben.

*Konzept:* `EXISTIERENDES VIDEO MERGER + AUTOMATIONSLAYER = VM AUTOMATIC` – der Automationsschlag
bestimmt, **wann** und **mit welchem Job** der Renderer läuft; VideoMerger bestimmt, **wie** gerendert wird.
