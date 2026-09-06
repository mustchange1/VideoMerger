# VideoMerger 1.5.0 für Windows

## Neu in 1.5.0

### Mehrere Quellordner und ordnerbewusste Auswahl

Mit **Add Folder**, **Remove Folder** und **Clear All** lassen sich beliebig viele Video-Quellordner hinzufügen; die Liste wird in den Projekteinstellungen gespeichert. Jeder Clip behält seine aufgelöste Quellordner-Identität. Die automatische Auswahl nutzt eine reproduzierbare Zufalls-Alternierung: Solange ein anderer Quellordner brauchbare Clips hat, wird kein Ordner direkt wiederholt; erst danach ist der gleiche Ordner als Fallback erlaubt. Eine explizite manuelle Reihenfolge deaktiviert diese Alternierung; Required-Only, Hold Last Frame, Full-Timeline Loop und Smart Last-Clip Stretch bleiben unverändert.

### Unabhängige Merge-Dauer

**Duration Before Merge** ist standardmäßig `0,70x` und gilt für jeden normalen ausgewählten Visual-Clip (`timeline_duration = source_duration / 0,70`) vor dem Timeline-Aufbau. **Duration After Merge** ist standardmäßig deaktiviert / `1,00x` und wird als eigener Post-Merge-Schritt auf den vollständigen Stage-1-Master angewendet. Smart Last-Clip Stretch bleibt zwischen Timeline-Aufbau und Render; Stage-2-Intro, Add Image und Outro werden von Before Merge nicht verändert.

### Untertitel-Defaults

Long-Form im Querformat nutzt standardmäßig **Center**, Short-Form im Hochformat weiterhin **Bottom Center**. Gespeicherte/manuelle Positionswerte bleiben maßgeblich.

## Neu in 1.3.0

- **Windows-Untertitel-Fix (Ursache behoben):** FFmpeg läuft immer mit dem Projekt-Root als Arbeitsverzeichnis; alle Render-Dateien im Filtergraph (ASS und Fonts-Ordner) liegen dort mit ASCII-Namen. Die Filter-Werte sind reine relative POSIX-Pfade — Laufwerks-Doppelpunkt, Backslashes, Leerzeichen und Umlaute können auf keinem Windows-Rechner mehr im Wert auftauchen (auch nicht bei `C:\\Users\\Jürgen Müller\\Downloads\\…`). Pfade außerhalb des Ankers werden UNQUOTED mit Vorwärts-Slashes und der verifizierten Zwei-Stufen-Escapetabelle ausgegeben (Apostrophe wie `O'Brien` sind darstellbar; die alte Quoting-Form brach dort ab). Echte Libass-Burn-Regressionstests über feindliche Pfade laufen in der Suite.
- **Automatisches Chunked Rendering für große Windows-Projekte:** Unter dem konservativen Befehlsziel bleibt der bestehende einzelne FFmpeg-Aufruf aktiv. Größere Projekte werden automatisch an sicheren Übergangsgrenzen in große Segmente geteilt; Cross Dissolve wird nicht dupliziert, der aktive Video-Pool bleibt Required-Only, und die Segmente werden ohne erneutes Re-Encoding zusammengesetzt. Untertitel werden erst einmal auf dem vollständigen Clean Master gebrannt; der bisherige Guard bei ungefähr 30.000 Zeichen bleibt als letzte Sicherheitsgrenze bestehen.
- **Smart Last-Clip Stretch:** Dauer-Fit-Modus `Cut Last Clip` (Standard) oder `Stretch Last Clip` — nur der letzte ausgewählte Clip läuft minimal langsamer (Limit 5/10 (Standard)/15/20/Custom %). Transitions und Kontinuität bleiben erhalten; über dem Limit gilt das normale Kürzen, niemals Hold Last Frame.
- **Phase-2-Mix-Defaults:** `Cross Dissolve` mit 1,0 Sekunden (sicher geklemmt bei kurzen Clips) und Musik 44 % / `Balanced` (ca. +6 dB gegenüber 22 %, weiterhin unter Voiceover). Ducking, Limiter, Looping und manuelle Regler bleiben aktiv.
- **Globale Video-Geschwindigkeit (Legacy-Kompatibilität): 0,50x–2,00x. Duration Before Merge ist jetzt separat 0,70x Standard; Duration After Merge ist deaktiviert / 1,00x. Voiceover bleibt die Timing-Autorität — Untertitel, Voiceover und Musik ändern sich nicht (SRT byte-identisch nachgewiesen).
- **Main Video End Padding:** freie manuelle Einstellung 0,0–5,0 s. Dieser Regler ist heute das **Long-Form Outro (visual after voiceover)** in der Gruppe `4d · Timeline – Visual Intro / Outro / Opening Effect` mit dem Nutzerstandard `2,5 s`; er bleibt der *einzige* visuelle Tail nach dem gesprochenen Audio und kann sich deshalb nie verdoppeln (siehe *Visuelle Intro-/Outro-Abschnitte* unten).
- **Bessere Untertitel-Segmentierung:** Long-Form bevorzugt 1–2 Zeilen natürlicher Phrasen; Ein-/Zwei-Wort-Captions werden zusammengeführt (Wort-Timing unangetastet). Live-Vorschau UND der neue große Vorschau-Dialog malen mit derselben Renderer-Geometrie (inkl. Animations-Wortfortschritt).
- **Sauberer Output-Ordner + wählbare Untertitel-Ausgabe:** Der Untertitel-Ausgabemodus steuert den tatsächlichen Vertrag: `With Subtitles` erzeugt das primäre Video mit eingebrannten Untertiteln sowie SRT/VTT, aber keine zusätzliche Clean-Version; `With and Without Subtitles` erzeugt beide Video-Varianten sowie SRT/VTT; `Without Subtitles` erzeugt nur das saubere primäre Video. Verifikations-PNGs und Timeline-JSON bleiben intern unter `temp/`.
- **Automatische YouTube-Metadaten (lokal, gratis, unbegrenzt):** Nach jedem erfolgreichen One-Click-Finalvideo entsteht `FinalVideo_16x9_YouTube.txt` (TITLE/DESCRIPTION/LANGUAGE) aus dem autoritativen Voiceover-Transkript: starker Einstieg, nützliche Zusammenfassung in den eigenen Worten, Themen als wörtliche Schlüsselphrasen, natürlicher Kanal-CTA; deutsch → deutsch, englisch → englisch. Deterministischer Pure-Python-Generator (immer offline verfügbar, nichts wird erfunden); ein optional vorhandenes lokales Ollama kann unter strenger Validierung polieren. Keine OpenAI-/Claude-/Gemini-/Bezahl-API. Metadaten-Probleme blockieren nie das Rendern.
- **One-Click:** Video-Pool + Voiceover(s) + Script(s) + Musik + Untertitel + Watermark + Intro + optionale Add Image + Main + Outro = FinalVideo mit einem Klick; das gerenderte MainVideo fließt automatisch in Stage 2.
- **Neue Phase-2-Defaults:** Cross Dissolve mit 1,0 s (bei kurzen Clips sicher geklemmt) und Music Volume 44 % / Balanced (ca. +6 dB gegenüber 22 %, weiterhin unter Voiceover). Ducking, Limiter, Looping und manuelle Regler bleiben aktiv. Die übrigen Defaults bleiben: Original Audio, Static White Reveal, YouTube Landscape, Maximum Quality, End-Padding ≈ 1 s (Long-Form; jeder Short endet mit einem festen 0,7 s langen rein visuellen Ende), Cut Last Clip, 10 % Stretch, 1,00x Speed. Explizit gespeicherte Transition-/Audio-Werte bleiben erhalten.

# VideoMerger 1.3.0 für Windows

VideoMerger 1.2.4 ist ein additives Release auf Basis des exakt getesteten 1.2.3-Artefakts. Basic Merge, getrennte Stage-1-/Stage-2-Flows, aktive manuelle Reihenfolge, vier Übergänge, Audio, Watermark, Validierung, Hardwareauswahl und nicht überschreibende Exporte bleiben erhalten.

## 1.2.4 – Großer Video-Pool (Required-Only), echte Subtitle-Preview, 4 neue Fonts

### Großer Video-Pool – nur benötigte Clips

Der Input-Ordner ist eine Quellbibliothek, keine Render-Warteschlange. Die Erkennung nutzt nur leichtgewichtiges `ffprobe`-Metadaten (Dauer, Auflösung, fps, Codec, Audiopräsenz, Größe – nie ein vollständiges Decodieren aller Dateien) und cached das Ergebnis. Die Auswahl stoppt, sobald die **aktive Reihenfolge** (Natürlich / Manuell / Zufällig) die aus dem Voiceover abgeleitete Zieldauer abdeckt: nur die benötigten Clips werden gerendert. Bei 300 verfügbaren und ~14 benötigten Clips gelangen genau ~14 Clips in die Pipeline – die übrigen tauchen in keinem Decode-, Filter-, Transition- oder Encode-Schritt auf. Der letzte Clip wird passend zutrimmt; ist das Material zu kurz, wiederholt die Full-Timeline-Loop die ausgewählte A-B-C-Sequenz und Hold Last Frame hält nur den letzten Frame. Die Vorverarbeitungsdauer skaliert nicht mit der Größe des ungenutzten Pools, und Änderungen an Subtitle-Stil/Add-Image/Intro/Outro analysieren den Pool nie erneut. Die GUI zeigt `Videos im Input-Ordner / Benötigt / Ausgewählt / Nicht genutzt / Zieldauer` und aktualisiert sich nach Analyse, Voiceover-Änderungen, Zufall und manueller Sortierung.

### Add Image (optional, stumm, Stage 2)

Add Image erscheint direkt unter Add Intro. Es kann genau ein PNG, JPG, JPEG oder WEBP ausgewählt und unmittelbar **Before Main Video** oder **After Main Video** platziert werden. Die Dauer ist über Presets und Custom editierbar (Standard 4,0 s), der gemeinsame Transition-Selektor verwendet standardmäßig Cross Dissolve mit 1,0 s an den Bildgrenzen. Fit, Fill und Crop bewahren das Seitenverhältnis; optionaler Zoom startet bei 100 %, und Natural, Cinematic, Moody, Film sowie Dark Editorial sind feste, reproduzierbare Looks. Die echte Vorschau spiegelt Datei, Format, Sizing, Zoom und Look.

Die effektive Stage-2-Zeitleiste ist immer `Intro → optional Add Image Before Main → Main Video → optional Add Image After Main → Outro`. Das Bild erhält kein Voiceover, keine Musik, kein Original-Audio und keine künstlichen Untertitelzeiten; der Stage-2-Graph erzeugt ausschließlich passende Stille. Alle Einstellungen sowie Identität und Inhalt der Bilddatei werden gespeichert und in den unabhängigen Stage-2-Kompositions-Fingerprint aufgenommen; Stage 1 bleibt bei reinen Add-Image-Änderungen wiederverwendbar. One-Click, normale Stage-2-Ausgabe, Preview/Basic-Handoff, Intro/Outro, Chunking, 16:9/9:16, 1080p/4K und Subtitle-Ausgabemodi werden unterstützt. Legacy-Image-Insertion-Namen und Positionsaliasse bleiben kompatibel.

### Subtitle-Ausgabemodi

Der Standard **With Subtitles** erzeugt intern einen sauberen Master, brennt genau einmal die ausgerichtete ASS-Spur und schreibt SRT/VTT; die Clean-Datei bleibt intern. **With and Without Subtitles** behält zusätzlich die saubere Variante. **With Subtitles (legacy burned-only)** führt den Burn ohne SRT/VTT aus. **Without Subtitles** führt weder Alignment noch Burn-in/SRT/VTT aus; Voiceover-, Musik- und Videodauer bleiben unverändert.

### Echte Subtitle-Preview (Preview ≈ Final Render)

Die GUI-Subtitle-Preview rendert das exakte Demo-Cue über dieselbe Umbruch-, Font-Metrics-, Safe-Area- und Positionslogik wie der eingebrannte Renderer – keine Fake-Texte, kein FFmpeg-Rendering. Font, Stil, Animation, Position, Zeilenumbruch, das max-zwei-Zeilen-Verhalten und Wort-Highlighting aktualisieren sich sofort.

### Vier weitere Fonts

Inter, Manrope, Lora und Roboto (Regular + Bold) ergänzen die bestehende Noto-Sans-Fallback: lesbarer, professioneller Long-Form-Look mit deutscher und englischer Unterstützung und kräftigen Bold-Varianten. Alle Fonts sind legal redistribuierbar (OFL / Apache-2.0, Lizenzen in `tools/fonts/`); proprietäre Fonts bleiben erkanungsseitig mit legalem Fallback. Der Selektor listet alle verfügbaren Fonts.

### 1.2.4-Defaults

Intro/Main/Outro Original Audio alle standardmäßig auf **Original** (Mute/Low/Original bleiben, unabhängig wählbar); Subtitle-Animation-Standard ist **Static White Reveal** (Long-Form / YouTube Landscape, alle 5 Animationen wählbar); Output Preset **YouTube Landscape** + Qualität **Maximum** bleiben unverändert.

## Flexible Video-Reihenfolge

Der projektweite **Video Order**-Selektor bietet **Natural**, **Alphabetical**, **Random** und **Manual**. Natural berücksichtigt numerische Segmente (`1, 2, 3, 10`), Alphabetical sortiert Dateinamen unabhängig von Groß-/Kleinschreibung; bei mehreren Quellordnern bleibt die Ordner-Alternierung aktiv, solange Alternativen vorhanden sind. Random erzeugt vor der Required-Only-Dauerauswahl eine echte Fisher-Yates-Permutation und wendet danach dieselbe Ordnerregel an. Random wird nicht automatisch zu Manual und schreibt keine Zufallsfolge in die manuelle Persistenz. Für deterministische Tests akzeptiert der gemeinsame Order-Helper einen Seed/RNG; normale Exporte verwenden eine neue Zufallsquelle.

Der bestehende Button **Randomize Order** bleibt als explizite Einmal-Aktion erhalten und speichert die erzeugte Liste aus Kompatibilitätsgründen als Manual. **Reset to Default Order** stellt die natürliche numerische/alphabetische Reihenfolge wieder her und schaltet auf Natural zurück. Manuelles Ziehen bleibt die höchste Autorität.

## 1.2.3 – Maximum Quality, Intro und mehrere Voiceovers

- **Randomize Order** mischt die aktive Clip-Liste per echtem unverzerrtem Fisher-Yates – nur die aktuelle Liste, keine wieder hinzugefügten Dateien. Die Mischung wird sofort aktive Exportreihenfolge und bleibt gespeichert. **Reset to Default Order** stellt die natürliche numerische/alphabetische Reihenfolge wieder her (1, 2, 3, 10 – nie 1, 10, 2, 3), nie die letzte Zufallsreihenfolge. Manuelles Ziehen bleibt die höchste Steuerung.
- **Maximum Quality** ist Standard: echtes `libx264` mit CRF 16, Preset `slow`, High Profile und yuv420p. Standard-Ausgabepreset **YouTube Landscape**: 16:9, Auto-Auflösung (höchste passende Quelle), Maximum Quality, Quell-/Auto-FPS, AAC-LC 48 kHz, Fast Start. 4K bleibt 4K, Quell-FPS bleibt erhalten, außer explizit geändert.
- **Optionaler Intro**: Endkomposition **Intro → Main → Outro**. Intro und Outro erhalten weder Main-Voiceover noch Main-Musik noch Untertitel; jede Sektion behält ihr eigenes Original-Audio mit unabhängigem Mute/Low/**Original** (Standard).
- **Mehrere Voiceover/Skript-Dateien**: Die effektive **Voiceover-Reihenfolge** kann **Natural / Alphabetical**, Änderungsdatum **älteste zuerst**, Änderungsdatum **neueste zuerst** oder **Manual** (Hinzufügen/Entfernen/Hoch/Runter/Anfang/Ende) sein. Sie wird mit dem Projekt gespeichert; Manual bewahrt die explizite Liste exakt. Skripte werden per normalisiertem Basisnamen automatisch zugeordnet und können manuell überschrieben werden. **One Global Script** (Standard) speichert genau eine maßgebliche Datei für die komplette geordnete Voiceover-Timeline, nie eine Kopie pro Zeile. **Individual Scripts** richtet jedes passende Paar aus; fehlendes Skript = klare `SUBTITLE GENERATION FAILED [script matching]`-Fehlermeldung, nie stilles Bild ohne Untertitel.
- **Pause Between Voiceovers**: Presets `0.0`, `0.25`, `0.5`, `0.7` (Standard), `1.0`, `1.5`, `2.0` Sekunden plus Custom. Die Pause ist echte Stille zwischen den Dateien und fließt in Voiceover-Zieldauer, Main Video und kumulative Untertitel-Offsets ein. Untertitel bleiben während der Stille unsichtbar. **Main Video End Padding** bleibt davon getrennt und standardmäßig 1.0 Sekunde. Globales Alignment verwendet eine gemeinsame Mapping-Operation über die vollständige logische Timeline; Individual Scripts werden zu einer kanonischen SRT/VTT/Burn-In-Timeline verbunden.
- **CREATE FINAL VIDEO – ONE CLICK** rendert weiterhin die echte `MainVideo.mp4` und übergibt exakt diese Datei automatisch an die Intro→Main→Outro-Komposition. Ausgaben: `MainVideo.mp4`, `MainVideo.srt`, `MainVideo.vtt`, `FinalVideo.mp4`.
- Nur Video-Reihenfolge, Untertitel-Stil/-Animation, Intro oder Outro ändern? ASR/Ausrichtung laufen nicht erneut; alle Caches bleiben intakt.

## 1.2.2 – professionelle Untertitel und One Click (weiterhin enthalten)

- 16:9-Standard: **Clean Editorial + Static White Reveal + Center**.
- Ruhige Satz-/Phrasenblöcke verwenden Satzzeichen, Phrasengrenzen, echte ausgewählte Font-Advances und visuelle Balance; Long Form ist strikt auf höchstens zwei Zeilen begrenzt.
- Die vollständige finale Phrase und der kanonische Zeilenumbruch bleiben in jedem Animationsevent reserviert. Reveal/Highlight verursacht dadurch kein Resize, Recenter, Reflow oder Jitter.
- Animationen (Long-Form): **Type Reveal, Color Change, Word Highlight, Phrase Focus, Static White Reveal**. Shorts bieten dieselbe Liste ohne **Word Highlight** und verwenden standardmäßig **Phrase Focus** – ein ruhiger, konservativer Eintritt auf Phrasenebene, der im 9:16-Mobile-Frame gut lesbar bleibt. **Outline Highlight ist entfernt**: die Variante zeichnete pro Wort eine kräftige Outline-Farbe und erzeugte gefüllte rechteckige Flächen außerhalb der Glyphen. Gespeicherte Projekte migrieren automatisch und deterministisch (Outline Highlight → Color Change, Word Highlight in Shorts → Phrase Focus, Unbekanntes → Standard der Collection); keine wählbare Animation emittiert noch Outline-, Shadow-, Clip- oder Vektor-Zeichen-Overrides – jeder Effekt bleibt glyphenausgerichtet.
- Positionen: **Bottom, Medium-Low, Middle, Top** mit sicheren, auflösungsabhängigen Bereichen.
- Fontauswahl: **Eveleth Clean, Modern Sans Bold, Clean Sans**. Eveleth wird nicht mitgeliefert, sondern nur als lizenzierte Benutzerinstallation erkannt. Ohne Eveleth wird Noto Sans unter SIL OFL verwendet.
- Sofortige eingebettete Vorschau und größere Vorschau für Stil, Font, Animation und Position.
- Optionales **Subtitle Debug Overlay**, standardmäßig AUS, zeigt aktuelles Wort und exakte Start-/Endzeit.
- **CREATE FINAL VIDEO – ONE CLICK** rendert die echte Stage-1-MainVideo-Datei und übergibt exakt diesen Pfad automatisch an den bestehenden Stage-2-Renderer.
- Der Headless-CLI unterstützt `--stage complete`, `--subtitle-animation`, `--subtitle-font` und `--subtitle-debug-overlay`.
- Die 1.2.1-Caches/Performancekorrekturen bleiben erhalten; rein visuelle Subtitle-Änderungen führen kein ASR erneut aus.

## QUICK START

1. `VideoMerger_Final_1.2.4.zip` nach **Downloads** herunterladen.
2. Windows PowerShell öffnen.
3. Vollständig ausführen:

```powershell
$Zip = Join-Path $HOME "Downloads\VideoMerger_Final_1.2.4.zip"
$ProjectRoot = Join-Path $HOME "Downloads\VideoMerger_Final_1.2.4"
if (Test-Path -LiteralPath $ProjectRoot) { Remove-Item -LiteralPath $ProjectRoot -Recurse -Force }
New-Item -ItemType Directory -Path $ProjectRoot -Force | Out-Null
Expand-Archive -LiteralPath $Zip -DestinationPath $ProjectRoot -Force
$Required = @("PROJECT_ROOT.txt", "setup_windows.ps1", "run_windows.ps1", "diagnostics_windows.ps1", "app\main.py", "requirements.txt")
foreach ($Name in $Required) { if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot $Name))) { throw "ZIP-Strukturfehler: $Name fehlt" } }
powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ProjectRoot "setup_windows.ps1")
powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ProjectRoot "diagnostics_windows.ps1")
powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ProjectRoot "run_windows.ps1")
```

`-ExecutionPolicy Bypass` gilt nur für den gestarteten Prozess. Das Setup benötigt keine globale PATH-Änderung und keine manuelle venv-Aktivierung. Alle PowerShell-Dateien sind UTF-8 mit BOM und CRLF.

## Installation und Offline-Betrieb

`setup_windows.ps1`:

1. findet Python 3.10–3.13 oder installiert Python 3.12 per winget im Benutzerkonto;
2. erstellt `<ProjectRoot>\.venv`;
3. installiert PySide6, faster-whisper und fontTools;
4. lädt das lokale faster-whisper-Modell `small` nach `tools\alignment_models`;
5. lädt FFmpeg/FFprobe, sucht die EXE-Dateien rekursiv und normalisiert sie nach `tools\ffmpeg\bin`;
6. führt beide Programme, den direkten `-filter_complex`-Preflight, Selbsttest und Diagnose aus.

Internet wird nur für diese erste Installation/Downloads benötigt. Voiceover-Ausrichtung, Rendering und Untertitel laufen danach lokal. Es wird keine OpenAI-/Google-/Cloud-API verwendet.

# Zwei getrennte Stufen

## Stage 1 – CREATE MAIN VIDEO

Explizite Rollen im GUI:

- **Video Folder**
- **Voiceover Audio** (optional)
- **Script Text** (bei Script-Untertiteln erforderlich)
- **Background Music** (optional)
- **Watermark** (optional)

Ausgabe:

```text
MainVideo_16x9.mp4   oder MainVideo_9x16.mp4
MainVideo_16x9.srt   (bei Voiceover + Script automatisch)
MainVideo_16x9.vtt
MainVideo_16x9.subtitle_timeline.json
MainVideo_16x9.subtitle_first.png / subtitle_middle.png / subtitle_final.png
```

Stage 1 enthält Video, den gewählten Übergang, Haupt-Audiomix, Burned-In-Untertitel und Watermark. Es enthält keinen Outro.

### Voiceover-getriebene Dauer

Mit Voiceover gilt:

```text
Voiceover-Dauer + konfigurierte Pause = Main-Video-Zieldauer
```

Pause: 0,5 / 1,0 / 1,5 / 2,0 Sekunden, Standard 1,0. Die Pause enthält standardmäßig kein Voiceover und keine Musik. Die aktive Clip-Reihenfolge bleibt unangetastet. Zu langes Material wird nur am letzten benötigten Clip gekürzt; überzählige Clips werden nicht gerendert. Ist das Material zu kurz, hält **Hold Last Frame** den finalen gerenderten Frame. **Full-Timeline Loop** startet dagegen die komplette aktive Reihenfolge wieder am ersten Clip und verwendet auch an der Loop-Grenze den gewählten Übergang. Der letzte wiederholte Abschnitt und die Ausgabe enden exakt am Ziel. Das Voiceover wird niemals geloopt oder abgeschnitten.

Ohne Voiceover bleibt der bestehende Video-Merge-Workflow verfügbar. Musik kann trotzdem benutzt werden. Ohne aktivierte Untertitel ist kein Skript erforderlich.

### Audio-Mix

```text
Original Video Audio + Voiceover + Background Music = Main Audio Mix
```

- Original Video Audio: **Mute** (Standard), Low, Original
- Voiceover: einmalig, primäre Sprache, eigener Lautstärkeregler
- Musik: wird am Input geloopt und exakt am Ende des gesprochenen Programms getrimmt
- Music Volume: Very Quiet, Quiet/Background, Balanced (Standard, 44 %), Medium, Custom
- Voiceover Ducking: weicher FFmpeg-Sidechain-Kompressor, Standard AN
- Attack/Release liegen unter Advanced Settings
- `amix` + Limiter verhindern digitales Clipping; optionale Loudness-Normalisierung bleibt erhalten
- internes und finales Audio: AAC-LC, 48 kHz, Stereo

Unterstützt sind mindestens WAV, MP3, M4A, AAC und FLAC; diese Formate werden in realen Tests dekodiert.

## Stage 2 – CREATE FINAL VIDEO

Eingaben:

```text
MainVideo.mp4 + Outro Video
```

Ausgabe:

```text
FinalVideo_16x9.mp4 oder FinalVideo_9x16.mp4
```

Stage 2 ist optional und vollkommen getrennt. Sie verwendet denselben ausgewählten visuellen Übergang und dieselbe Dauer. Der Übergang kann ausdrücklich deaktiviert werden. Stage 2 öffnet keine Voiceover-/Musik-/ASS-Rolle. Der Outro erhält:

- kein App-Voiceover;
- keine App-Hintergrundmusik;
- keine Untertitel;
- ausschließlich sein eigenes Originalaudio bzw. erzeugte Stille bei fehlendem Audio.

Outro Original Audio: Mute / Low / **Original (Standard)**. Ein Audio-Crossfade betrifft nur Main-Endaudio und Outro-Originalaudio. SRT/VTT bleiben unverändert und enden mit dem gesprochenen Main-Programm.

## One Click – CREATE FINAL VIDEO – ONE CLICK

Der neue Flow ersetzt die getrennten Stufen nicht. Er führt intern zuerst `MainProjectEngine.create_main()` aus, prüft dessen echte MP4-Ausgabe und setzt genau diesen erzeugten Pfad als `main_video_path` für den bestehenden `add_outro()`-Renderer. Erst nach FFprobe-Validierung beider Stufen wird Erfolg gemeldet. Die kombinierte Fortschrittsanzeige benennt Stufe 1/2 und 2/2.

Die konfigurierte Quiet Pause liegt bereits am Ende von MainVideo und bleibt frei von Voiceover, Musik und Untertiteln. Stage 2 leert diese Rollen ausdrücklich. Der Outro behält ausschließlich seinen gewählten Originalaudio-Modus. Watermark Main/Outro/Both bleibt aktiv.

# Video-Reihenfolge

First-In bleibt die initiale Reihenfolge. Nach **Analyze Inputs** können Zeilen gezogen oder mit Nach oben/Nach unten verschoben werden. Die sofort nummerierte sichtbare Liste ist die exakte Vorschau-/Exportreihenfolge. `first_in` und `active` werden getrennt gespeichert. Rescan behält überlebende manuelle Positionen, entfernt fehlende Dateien und hängt neue hinten an. **Reset to First-In Order** nutzt die gespeicherte Historie, nie alphabetische Sortierung. Während ein Worker die erfasste Reihenfolge benutzt, sind Änderungen gesperrt.

# Wortgenaue Untertitel

Der Benutzertext ist immer die autoritative Schreibweise. Er wird weder umgeschrieben noch zusammengefasst. Groß-/Kleinschreibung, Satzzeichen, Umlaute und englische Kontraktionen bleiben erhalten.

Ablauf:

```text
Voiceover (nicht Videoclips)
→ lokales faster-whisper (reale akustische Wortzeitstempel)
→ gecachte kanonische Transkription
→ Sequenz-/Forced-Mapping auf das exakte Skript
→ monotone Wortzeiten in `.subtitle_timeline.json`
→ SRT + VTT + ASS-Burn-In im finalen MP4
→ First/Middle/Final-Verifikationsframes aus genau diesem MP4
```

ASR liefert nur akustische Zeitgrenzen; angezeigt wird das Skript. Nicht eindeutig erkannte Skriptwörter werden ausschließlich zwischen benachbarten akustischen Ankern eingeordnet und als Warnung gemeldet. Bei niedriger Kompatibilität stoppt Stage 1 vor dem Rendern. Erst die ausdrückliche GUI-Option **Continue After Alignment Warning** erlaubt einen erneuten Lauf. Es gibt keine Behauptung „100 % exakt“; Log/Diagnostics nennen Methode, Kompatibilität, Confidence und Warnungen.

Sprachen sind exakt German, English und Auto; Standard German.

SRT/VTT werden auf gültige monotone Zeiten, Überlappungen, leere Cues und vollständige Wortabdeckung geprüft. Burn-In liegt im selben Haupt-Filtergraphen; es gibt keinen zusätzlichen verlustbehafteten Subtitle-Encode. Die Dateien sind saubere YouTube-Zeit/Text-Tracks ohne ASS-Styling.

## Genau zehn Presets

Long Form (16:9-Standard: Clean Editorial, Position Bottom):

1. LONG FORM 1 – Clean Editorial
2. LONG FORM 2 – Documentary Box
3. LONG FORM 3 – Minimal Cinematic
4. LONG FORM 4 – Subtle Highlight
5. LONG FORM 5 – Podcast / Interview

Short Form (9:16-Standard: Kinetic Chunk, Inter, Position Bottom Center):

1. SHORT FORM 1 – Kinetic Chunk
2. SHORT FORM 2 – Bold Highlight
3. SHORT FORM 3 – Clean Pop
4. SHORT FORM 4 – Karaoke Lite
5. SHORT FORM 5 – Impact

Long-Form-Presets verwenden ruhige, satzzeichen-/phrasenorientierte Blöcke, standardmäßig keine isolierten Einzelwortblöcke und strikt maximal zwei explizite Zeilen. Short-Form-Presets behalten stabile kompakte Gruppen ohne zufällige Positionierung, Schütteln oder Flashen. Die Animation wird unabhängig vom Preset gewählt. Bei Type Reveal bleiben zukünftige Glyphen transparent, aber vollständig im Layout; bei Color Change, Word Highlight und Phrase Focus bleibt die gesamte Phrase sichtbar. `Static White Reveal` zeigt den vollständigen Block.

Fontgröße, reale verfügbare cmap/hmtx-Advances, Outline, Margin und Watermark skalieren relativ zur Zielauflösung. Reale Libass-Tests decken 1920×1080, 3840×2160 und 1080×1920 ab. Positionen: Bottom, Medium-Low, Middle, Top. Medium-Low liegt bei 9:16 über der UI-sensitiven Unterkante. Das GUI enthält eine eingebettete und eine größere Vorschau. Noto Sans Regular/Bold wird mit SIL-OFL-Datei legal gebündelt. Eveleth Clean wird niemals verteilt; nur eine vorhandene lizenzierte Installation wird erkannt.

# Watermark

PNG (inkl. Transparenz), JPG, WebP, BMP und TIFF werden über FFmpeg unterstützt. Steuerung:

- Disabled (Standard) / Enable
- Top Left / Top Right / Bottom Left / Bottom Right
- Opacity
- relative Size
- relative Margin
- Main / Outro / Both (Standard Both)

Stage 1 komponiert Main/Both im Hauptgraphen. Stage 2 komponiert Outro/Both nur ab dem Outro-Übergang, damit das bereits markierte Main nicht nochmals vollständig überlagert wird. Acht reale Eckpositions-Renderings decken 16:9 und 9:16 ab.

# Bestehende Videoqualität

Unverändert:

- Smooth Blur Crossfade (Standard)
- Cross Dissolve
- Film Dissolve
- Additive Dissolve
- Linear / Ease In / Ease Out / Ease In + Ease Out
- sichere Transition-Clamps für kurze Clips
- identische Canvas-Vorbereitung vor `xfade`
- 9:16 Self-Video-Blur mit vollständigem Vordergrund
- Auto-Auflösung bis 3840×2160 bzw. 2160×3840
- Auto-FPS bewahrt einheitliche Quell-FPS; keine unnötige 60→30-/50→25-Konvertierung
- H.264 High, yuv420p, progressive, SAR 1:1, SDR BT.709, CRF 18, Preset slow, `+faststart`
- Auto/CPU/NVENC/QSV/AMF nur mit echter Probe und CPU-Fallback
- keine unnötigen verlustbehafteten Zwischen-Encodes

Ein realer 4K-Test stellt sicher, dass 3840×2160 bei Auto nicht still auf 1080p reduziert wird. HDR/PQ/HLG/BT.2020 wird weiterhin ausdrücklich blockiert.

# Diagnose und Validierung

System Diagnostics meldet Python, PySide6, faster-whisper, lokale FFmpeg-/FFprobe-Versionen, direkten Filtertest und Schreibzugriffe. Project Diagnostics ergänzt:

- Voiceover-Dauer, Sample-Rate, Kanäle
- Skriptwortzahl und ausgewählte Sprache
- Musikdauer und Sample-Rate
- Alignment-Methode/Status
- aktive Clipzahl/Materialdauer
- Main-/Outro-Zuordnung

Der Renderlog enthält exakte aktive Reihenfolge, komplette sicher zitierte FFmpeg-Argumentliste, Zielauflösung/FPS, Audiomix, Alignment-Confidence und Warnungen. Zusätzlich werden `video_analysis_seconds`, Voiceover-/Musikverarbeitung, ASR, Forced Mapping, Subtitle-Erzeugung, FFmpeg-Rendering, Finalisierung und Gesamtzeit gemessen. Cache-Hit/-Miss wird explizit genannt.

Nach jedem Export prüft FFprobe:

- Datei/MP4-Integrität und Fast Start
- H.264, yuv420p
- Auflösung, FPS, SAR und Seitenverhältnis
- Container-, Video- und Audiostreamdauer
- AAC, 48 kHz, Stereo
- Bitrate
- erwartete Gesamtdauer

Fehlerhafte Ausgaben werden nicht als Erfolg ausgegeben.

# Deterministische Dateinamen

Stage 1: `MainVideo_16x9.mp4/.srt/.vtt/.subtitle_timeline.json` plus drei Verifikations-PNGs oder entsprechend `MainVideo_9x16.*`.  
Stage 2: `FinalVideo_16x9.mp4` oder `FinalVideo_9x16.mp4`.

Vorhandene Dateien werden nicht überschrieben; `_2`, `_3` usw. werden atomar als gemeinsames Bundle gewählt.

# Tests

Alle Tests unter Windows:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\test_windows.ps1"
# Optionaler, deutlich längerer 2-Minuten-Benchmark:
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\test_windows.ps1" -Benchmark
```

Die ausgelieferte Suite enthält Unit-, GUI-Offscreen-, lokale Alignment- und echte FFmpeg-Tests für:

- alle 1.1-Funktionen und vier Übergänge;
- Voiceover-only, Music-only, Voice+Music, Loop, Trim, fehlende optionale Rollen;
- Ducking, Limiter, Sample-Rates und Mute/Low/Original;
- deutsche und englische reale lokale Wortzeitstempel;
- Satzzeichen, Umlaute, Kontraktionen, kurze/lange Skripte;
- SRT, VTT, Burn-In und alle zehn Presets;
- Watermark an allen vier Ecken in 16:9/9:16;
- Stage 2 mit Audio/ohne Audio und Mute/Low/Original;
- komplette Zwei-Stufen-Pipeline;
- Auto-Subtitle-Pflichtpfad und sichtbarer Fehlerpfad ohne captionless Restvideo;
- reale First/Middle/Final-Wortzeit-/Sprachenergie-/Burn-In-Pixelprüfung;
- ASR-/Alignment-/Media-Cache und Stilwechsel ohne erneutes ASR;
- Full-Timeline Loop in manueller Reihenfolge, Loop-Grenzübergang und exakte Zieldauer;
- separaten Hold-Last-Frame-Modus mit exakter Zieldauer;
- gemessenen 2-Minuten-CPU-Benchmark mit Voiceover, kaltem `small`-ASR, Untertiteln und Musik (**1,926 Minuten Analyse bis Finalisierung in der Linux-Entwicklungsumgebung; kein nativer Windows-Hardwarewert**);
- alle fünf Animationen als echte Libass-Burns mit First/Middle/Final-Wortframes;
- alle fünf verbesserten Long-Form-Stile, drei Fontwahlen/Fallbacks und vier sichere Positionen als echte Burns;
- ein-/zweizeiliges Layout ohne 3+ Zeilen und ohne isolierte Standardblöcke für Deutsch/Englisch, schnell/langsam und Umlaute;
- 1920×1080, 3840×2160 und 1080×1920 mit echtem Libass;
- echten One-Click-Aufruf, exakten MainVideo-Handoff, Final-FFprobe sowie Quiet-Gap-/Outro-Audio-/Subtitle-/Musik-Isolation;
- reales 4K-Auto-Rendering;
- saubere Extraktion und Manifestprüfung.

Linux-Offscreen-, Linux-FFmpeg- und PowerShell-7-Prüfungen sind keine nativen Windows-Nachweise. Native Windows-Status stehen ehrlich in `BUILD_REPORT.md`.

# Deinstallation

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\uninstall_windows.ps1"
```

Originale werden niemals gelöscht oder verändert. `-RemoveOutputs` entfernt nur auf ausdrücklichen Wunsch den Projekt-Ausgabeordner.

# YouTube-Ausgabemodi

Der Ausgabemodus ist ein echter Pipeline-Schalter:

- **YouTube Long-Form** erzeugt eine vollständige Landschafts-Timeline in 16:9. Alle Voiceovers und globale bzw. gematchte Skripte bleiben auf einer gemeinsamen Timeline.
- **YouTube Shorts** erzeugt einen unabhängigen vertikalen 9:16-Short je vorhandenem Voiceover. Ein gemeinsamer Video-Pool vergibt pro Short den nächsten benötigten Präfix ohne Ersatz; Wiederverwendung beginnt erst nach vollständigem Verbrauch des Pools. Dauer und Materialauswahl werden für jedes Voiceover separat berechnet; die Skriptanzahl bestimmt nicht die Ausgabeanzahl. 10 Voiceovers mit einem globalen Script ergeben deshalb 10 Shorts.
- **YouTube Long-Form + YouTube Shorts** erzeugt beide Ausgabemengen.

Die Ausgaben werden getrennt gespeichert: `Output/LongForm/YouTube_LongForm.mp4` und `Output/Shorts/001.mp4`, `002.mp4` usw. Intro, Outro, Add Image, Musik, Original-Audio, Übergänge, Reihenfolgen und One-Click laufen für jeden Auftrag über die vorhandene Render-Pipeline. Long-Form verwendet standardmäßig das 16:9-Profil Static White Reveal; Shorts verwenden standardmäßig Inter, Bottom Center und die eigene wort-synchronisierte 9:16-Animation. Jeder Short besitzt eine eigene Cache-Identität.

**Getrennte Hintergrundmusik, getrennte Lautstärke.** Long-Form und Shorts verwenden zwei unabhängige Musikauswahlen: **Background Music (Long-Form)** und **Background Music (Shorts)**. Beide Spuren sind strikt getrennt — die Long-Form-Musik wird nie in einen Short gemischt, und ein Short ohne eigene Auswahl bleibt ohne Hintergrundmusik (es wird nie künstliches Audio erzeugt). Jede Ausgabe besitzt außerdem ihre **eigene Musik-Lautstärke**: `Long-Form Music Volume` und `Shorts Music Volume`, beide standardmäßig `44 %` und unabhängig voneinander von `0 %` bis `150 %` einstellbar; eine Änderung der einen verändert nie die andere. Eine gewählte Spur **startet bei `0,000 s`**, läuft kontinuierlich durch das visuelle Intro, das Voiceover und das visuelle Outro und wird bis zum **endgültigen Video-Ende** geloopt bzw. geschnitten — nie nur bis zum Ende der Sprache. Es entsteht also weder eine stille Opening- noch eine stille Endphase. Preset, Ducking und Looping gelten unverändert für die jeweils aktive Spur; im kombinierten Modus (und im One-Click) nutzt jede Ausgabe ausschließlich ihre eigene Spur mit ihrer eigenen Lautstärke.

**Rein visuelles Short-Intro und -Outro.** Jeder Short ist `[0,7 s visuelles Intro][eigenes Voiceover][0,7 s visuelles Outro]`. Das gesprochene Audio bleibt die maßgebliche Dauer und wird nie verlängert; Intro und Outro enthalten keine Sprache, kein Voiceover-Audio und keine Untertitel — die Untertitel-Zeitleiste wird im Timeline-Modell um das Intro verschoben und endet mit dem Voiceover, sodass in keinem der beiden Abschnitte ein Cue dieses oder eines anderen Shorts erscheinen kann. Das Material stammt aus der regulären Timeline-Logik (Clip-Auswahl, Übergänge, Hold/Loop, Chunked Rendering und der Shorts-Pool ohne Ersatz, der Intro, gesprochenen Teil und Outro vorab reserviert). Das frühere feste `0,7 s`-Ende wird durch das einstellbare Short-Outro **ersetzt**, nie zusätzlich dahintergestapelt — der neue Standard des Short-Outros ist exakt diese `0,7 s`, sichtbar bleibt also genau ein Ende; `0,7 s` bleibt außerdem die garantierte Untergrenze für Settings-Objekte ohne explizites Short-Outro. Das Long-Form behält sein frei einstellbares **Long-Form Outro** (das frühere Main Video End Padding).

**Eine Skript-Textdatei pro Short.** Neben jedem gerenderten Short schreibt VideoMerger automatisch `<gleicher Name>.txt` (`001.mp4` → `001.txt`) mit genau dem Skripttext, den dieser Short verwendet: sein eigener Abschnitt eines globalen Skripts oder sein gematchtes/einzelnes Skript. Es wird nichts erneut transkribiert — die Datei nutzt die bereits abgeleiteten Inhalte, stimmt also immer mit den gesprochenen/angezeigten Wörtern dieses Shorts überein und enthält nie Text eines anderen Shorts. Ein ausdrücklicher Audio-only-Short (ein Voiceover, das keinen Teil des globalen Skripts spricht) hat keinen Text und daher keine Begleitdatei.

Bei einem globalen Skript mit mehreren Voiceovers verwendet das Long-Form weiterhin das vollständige Skript über die gesamte Timeline, während jeder Short nur den Abschnitt erhält, den sein eigenes Voiceover tatsächlich spricht — akustisch aus einem einzigen gemeinsamen globalen Mapping abgeleitet, nie durch einen Abgleich des vollständigen Skripts gegen jeden Short.

Standardmäßig gilt **With Subtitles**: eingebrannte Untertitel plus SRT/VTT, aber keine zusätzliche Clean-Version. **Without Subtitles** überspringt Untertitel vollständig. **With and Without Subtitles** erzeugt beide Varianten. Long-Form und Shorts besitzen getrennte Profile; Shorts sind standardmäßig groß, lesbar, vertikal sicher, auf höchstens zwei Zeilen begrenzt und wort-synchron animiert.

CLI: `--export-mode long_form|shorts|long_form_and_shorts`, `--music` (Long-Form), `--short-music` (Shorts), `--subtitle-output-mode with_subtitles|without_subtitles|with_and_without_subtitles` sowie `--short-subtitle-style`, `--short-subtitle-animation`, `--short-subtitle-font`, `--short-subtitle-position`.

# Visuelle Intro-/Outro-Abschnitte, Opening Effect und Legacy-Input-Root-Priorität

Jedes voiceover-getriebene Main Video besitzt jetzt eine explizite, eindeutige
Struktur: `[visuelles Intro][Voiceover + normales Video][visuelles Outro]`. Beide
visuellen Abschnitte spielen bewegtes Material aus der regulären Timeline (nie
Schwarzbild oder unbeabsichtigte Standbilder) und enthalten weder Voiceover-Audio
noch Untertitel. Hintergrundmusik startet bei `0,000 s` und läuft durch **beide**
visuellen Abschnitte bis zum letzten Frame (siehe *Durchgängige Ausgabemusik*),
solange eine Spur gewählt ist.

- **Long-Form Intro (visual before voiceover)** — Standard `1,5 s`, `0` deaktiviert es, jeder positive Wert ist erlaubt (der GUI-Spin endet bei `60 s`, das Modell selbst hat kein künstliches Limit). Das Voiceover startet exakt nach dem Intro, die Untertitel starten nie davor.
- **Long-Form Outro (visual after voiceover)** — Standard `1,5 s`. Dieser Abschnitt **ist** das frühere Main Video End Padding: ein Regler, ein kanonischer Timeline-Tail, beide können sich nie verdoppeln. Eine alte Projektdatei behält ihr gespeichertes Padding.
- **Short Intro / Short Outro** — Standard je `0,7 s` pro Short mit denselben Regeln; das Short-Outro ersetzt das frühere `0,7 s`-Ende, statt ein zweites dahinterzustapeln.
- **Untertitel nur während der Sprache** — die gesamte Untertitel-Zeitleiste wird im Timeline-Modell um das Intro verschoben (nicht durch einen nachgelagerten Delay), beginnt exakt mit dem Voiceover und endet exakt mit dem gesprochenen Audio. Wort-Timing, globale Skript-Sektionen, SRT/VTT, Burn-in und die strikte Cue-Validierung bleiben unverändert.
- **Opening Effect (Main Video)** — optionaler, dezenter Einstieg in Gruppe `4d`: `None` (Standard), `Gentle Zoom In`, `Gentle Zoom Out`. Spitze 5 %, zentriert, nur über dem Öffnungsabschnitt (dem visuellen Intro, sonst `3 s`, nie länger als das Programm), angewendet auf die fertige Timeline **vor** dem Untertitel-Burn-in, damit Captions scharf bleiben; über Chunk-Grenzen kontinuierlich und danach Identität. Der Effekt ergänzt oder entfernt keinen Frame — Dauer, Voiceover-Sync und Untertitel bleiben unberührt. Shorts nutzen ihn nie. Einen Animations-Editor gibt es bewusst nicht.
- **Legacy Input Root Priorität (nur Random)** — solange **Random** aktiv ist, stammen die ersten drei Clips der effektiven Reihenfolge immer aus der Legacy Input Root (dem konfigurierten Input-Ordner): möglichst verschieden, untereinander gemischt und **vor** dem Aufbau der restlichen Sequenz reserviert. Ab Clip 4 bleibt der volle Zufallspool unverändert (Ordner-Alternierung, Duplikat-/Erschöpfungsregeln, Seed-Determinismus). Weniger als drei geeignete Clips reservieren, was vorhanden ist, und füllen danach normal auf; eine leere oder fehlende Wurzel ändert nichts — nicht einmal die Zufallsfolge, bestehende Projekte bleiben also bit-identisch. Natural, Alphabetical und Manual sind nicht betroffen. Eine einzige Logzeile nennt die reservierten Clips.

GUI: die Gruppe **`4d · Timeline – Visual Intro / Outro / Opening Effect`** enthält die vier Dauer-Spins und die Opening-Effect-Auswahl; die Untertitel-Animations-Combos werden pro Collection (Long-Form / Shorts) mit eigenen Standards aufgebaut, und der Wechsel auf 9:16 wählt automatisch den Shorts-Standard. Alle neuen Werte werden im Projekt gespeichert, überstehen alte Projektdateien (fehlende Felder → dokumentierte Standards, veraltete Animationen migrieren, unbekannte Felder werden ignoriert) und sind Teil der Render-Cache-Identität (Fingerprint-Schema `4`), sodass jede Änderung — einschließlich der unten beschriebenen Musik-Lautstärken und Übergangswerte pro Ausgabe — neu rendert statt einen veralteten Cache-Eintrag zu nutzen.

CLI: `--long-intro`, `--long-outro` (Alias von `--pause`/`--end-padding`), `--short-intro`, `--short-outro` und `--opening-effect none|zoom_in|zoom_out`.

# Durchgängige Ausgabemusik, eigene Lautstärken und Übergänge pro Ausgabe

Die kanonische Timeline jeder voiceover-getriebenen Ausgabe ist vollständig
explizit, und das Audio folgt ihr — nicht dem gesprochenen Programm:

| | Long-Form Standard | Shorts Standard |
| --- | --- | --- |
| Visuelles Intro (kein Voiceover, keine Untertitel) | `1,5 s` | `0,7 s` |
| Voiceover + Untertitel | das gesprochene Audio | das gesprochene Audio |
| Visuelles Outro (kein Voiceover, keine Untertitel) | `1,5 s` | `0,7 s` |
| Musikfenster | `0,000 s` → Video-Ende | `0,000 s` → Video-Ende |
| Musik-Lautstärke | `44 %` (`long_form_music_volume`) | `44 %` (`shorts_music_volume`) |
| Übergang | Cross Dissolve / `2,0 s` | Cross Dissolve / `2,0 s` |

Ein Standard-Long-Form mit 3,0 s Voiceover ist damit exakt `6,000 s`
(`1,5 + 3,0 + 1,5`), ein Standard-Short exakt `4,400 s` (`0,7 + 3,0 + 0,7`).

- **Musik deckt das komplette Video ab.** Die Musikdauer leitet sich aus der
  finalen Videolänge ab (Intro + Sprache + Outro), nie aus der gesprochenen
  Timeline allein. Looping und Trimming bleiben die bestehende Architektur: die
  Spur läuft mit `-stream_loop -1` und erhält ihre Lautstärke **vor** dem Schnitt.
  Der geloopte *Input* wird nur für das gesprochene Programm gelesen; das
  visuelle Outro deckt der Filtergraph ab, indem er das Ende dieser geschnittenen
  Musik wiederholt (`aloop`, Fenster auf 15 s begrenzt, damit ein langes
  Voiceover nie vollständig gepuffert wird). Das Wiederholungsfenster beginnt
  exakt am Programmende, sodass das Outro die Spur nahtlos fortsetzt, und wird
  exakt am Video-Ende geschnitten. Diese Form ist zugleich die einzig sichere:
  einen `-stream_loop -1`-Input bis unmittelbar an das Ausgabe-Ende lesen zu
  lassen, lässt FFmpeg 6.0 bei 0 % CPU blockieren (mit derselben 0,6-s-Spur
  gemessen: `atrim=duration=<Ziel>` hängt, `<Ziel − 0,1 s>` läuft in 0,4 s
  durch). Das Voiceover bleibt unberührt: Es startet nach dem eingestellten
  Intro, endet mit dem gesprochenen Audio, und das Outro enthält keine Sprache.
  Ohne gewählte Spur bleibt die Ausgabe stumm.
- **Untertitel folgen dem Voiceover, nicht der Musik.** Captions starten mit dem
  Voiceover (also nach der Intro-Länge), enden mit dem gesprochenen Inhalt und
  erscheinen nie in einem visuellen Abschnitt. Wort-Level-Alignment, Aufteilung
  globaler Skripte, Skripte pro Voiceover, SRT/VTT, Burn-in und die strikte
  Überlappungsvalidierung bleiben unverändert.
- **Unabhängige Übergänge.** `long_form_transition_type` /
  `long_form_transition_duration` und `shorts_transition_type` /
  `shorts_transition_duration` werden pro Ausgabe aufgelöst; alle bestehenden
  Übergangstypen bleiben verfügbar, und der kombinierte Modus sowie One-Click
  wenden die Long-Form-Werte auf das Long-Form und die Shorts-Werte auf jeden
  Short an.
- **Abwärtskompatibilität.** Alte Projekte behalten ihre expliziten Werte. Ein
  gespeicherter gemeinsamer `music_volume` wird **nur** dann in beide neuen
  Lautstärken kopiert, wenn diese fehlen; gespeicherte gemeinsame
  Übergangswerte dienen als Migrations-Fallback für beide Ausgaben; unbekannte
  Felder werden ignoriert.
- **Cache-Identität.** Alle vier Abschnittsdauern, beide Musik-Lautstärken, alle
  vier Übergangswerte, der Opening Effect, die Untertitel-Animations-/Profilwerte
  und die effektive Medienreihenfolge sind Teil des Stage-1-Fingerprints
  (`FINGERPRINT_SCHEMA = 5`). Einträge älterer Schemata werden nie
  wiederverwendet (Fail-closed), sodass eine Änderung von Musik-Lautstärke oder
  Übergang immer neu rendert.
- **Logging, einmal pro Auftrag:** `Timeline: Intro 1.500 s (visual only) ·
  Voiceover start 1.500 s · Spoken 3.000 s · Spoken end 4.500 s · Outro 1.500 s
  (visual only) · Video start 0.000 s · Video end 6.000 s`, `Music: start
  0.000 s · end 6.000 s (video end) · continuous through the visual intro, the
  voiceover and the visual outro`, `Subtitles: start 1.500 s · end 4.500 s · no
  caption in the visual intro or outro` sowie `Output settings (Long-Form): Music
  volume 44 % · Voiceover volume 100 % · Ducking off · Transition Cross Dissolve
  / 2.000 s`.

GUI: Die Long-Form- und die Shorts-Gruppe enthalten jeweils eigene Zeilen für
**Timeline** (visuell vor/nach dem Voiceover, beim Long-Form zusätzlich der
Opening Effect), **Audio** (Spur + Lautstärkeregler) und **Transitions** (Typ +
Dauer) sowie einen Hinweistext, dass Musik sofort startet und bis zum Ende
läuft, während Voiceover und Untertitel erst nach dem Intro beginnen. Kein Regler
existiert doppelt.

CLI: `--long-intro`, `--long-outro`, `--short-intro`, `--short-outro`,
`--long-music-volume`, `--short-music-volume`, `--long-transition`,
`--short-transition`, `--long-transition-effect`, `--short-transition-effect` und
`--opening-effect none|zoom_in|zoom_out`. Die gemeinsamen Flags `--music-volume`,
`--transition` und `--transition-effect` bleiben erhalten und wirken als
Kompatibilitäts-Fallback für beide Ausgaben, wenn das ausgabenspezifische Flag
fehlt.

# Visuelle Verifikation ist optionaler Nachweis, nie ein Render-Fehler

Die Verifikationsbilder (first/middle/final) werden aus der fertigen,
FFprobe-validierten MP4 dekodiert und sind ausschließlich interner
Qualitätsnachweis. Ihre Zeitstempel sind jetzt begrenzt: jede Anfrage wird strikt
innerhalb der realen Videodauer geklemmt, mit einem Sicherheitsabstand von zwei
Frame-Perioden (mindestens `40 ms`) aus der tatsächlichen Framerate. Schlägt eine
Extraktion fehl, wird bei `duration − 2·margin`, `duration − 3·margin` und
`duration − 4·margin` erneut versucht — nie am oder hinter dem Dateiende und nie
endlos. Eine PNG gilt nur dann als gültig, wenn sie existiert, nicht leer ist und
strukturell dekodierbar bleibt (Signatur, `IHDR` mit von Null verschiedener
Größe, `IEND` vorhanden); leere oder abgeschnittene Dateien werden entfernt und
neu versucht.

Jedes Bild protokolliert genau eine Zeile, zum Beispiel
`Visual verification final: requested=80.792000 · fallback=80.750333 · PNG=PASS
(1920x1080)` oder `Visual verification final: requested=5.990000 · 4 bounded
attempts · PNG=FAIL (no decodable frame)`.

Das Ergebnis wird getrennt vom Rendering klassifiziert:
`MainVideoResult.verification_status` ist `PASS`, `DEGRADED` (einzelne Bilder),
`FAIL` (kein Bild) oder `SKIPPED` (keine zuverlässig zugeordneten Wörter bzw.
keine Untertitel); ein aus dem Cache wiederverwendetes Video meldet `CACHED`.
Ein `FAIL` protokolliert `Visual verification: PNG=FAIL · 0/3 frames decoded ·
rendered output retained · overall render status=SUCCESS`, und die gültige MP4
bleibt ebenso erhalten wie der Clean Master, SRT und VTT. Zuvor wurde derselbe
Fall als `SUBTITLE GENERATION FAILED [first/middle/final visual verification]`
gemeldet und die aufgeräumte Ausgabe gelöscht — ein langer, gültiger Render wurde
verworfen, weil eine optionale PNG am Dateiende nicht extrahiert werden konnte.

Echte Fehler schlagen weiterhin hart fehl und räumen auf: Fehler bei der
Untertitel-Erzeugung, eine ungültige Untertitel-Zeitachse, fehlende oder leere
SRT/VTT/kanonische Timeline, ein fehlgeschlagener Burn-in-Durchlauf und jeder
FFprobe-Validierungsfehler behalten die strikte Klassifizierung
`SUBTITLE GENERATION FAILED […]`.

## Weiche Timeline-Areas – welcher Quellenordner wo läuft

Qualität bleibt eine **Ordner**-Entscheidung. Dieses Feature ändert ausschließlich,
*welcher konfigurierte Ordner an welcher ungefähren Stelle der Timeline verwendet
wird*. Es analysiert nichts: kein Scoring, keine Bewegungs- oder Qualitätsmessung,
keine semantische Klassifikation, kein Ranking einzelner Clips, keine KI/CV.
Projektreihenfolge (Natural / Alphabetical / Random / Manual, inklusive
Legacy-Input-Root-Priorität und Ordner-Alternierung) und jede Randomisierung
innerhalb einer Quelle bleiben exakt unverändert – der Scheduler gruppiert nur
**ganze** Clips in einem O(n)-Durchlauf um.

Pro konfiguriertem Video-Ordner ist eine von drei Rollen möglich (Gruppe
`1 · Ordner`: **Role**-Combo plus **Set Role** für die markierte Zeile; ein neu
hinzugefügter Ordner übernimmt die gerade gewählte Rolle):

| Rolle | Vorgesehen für |
| --- | --- |
| `1. Start & End` | Anfang **und** Ende des Long-Form-Videos |
| `2. Start to Middle` | den früheren/Hauptteil bis zum Midpoint-Ziel |
| `3. Middle to End` | den späteren/Hauptteil bis zur End-Reserve |

Mehrere Ordner dürfen sich eine Rolle teilen; sie behalten ihre konfigurierte
Reihenfolge und werden nie gegeneinander gewichtet.

- **Jede Grenze ist ein weiches Ziel.** Der aktuelle Clip läuft immer zuerst zu
  Ende, die nächste Rolle beginnt an dieser natürlichen Clip-Grenze – ein
  `23,7 s`-Clip erfüllt ein `20 s`-Startziel. Kein Clip wird je geschnitten,
  gekürzt oder geteilt, um eine Zone zu treffen; die Toleranz ist der Clip
  selbst, kein versteckter zweiter Parameter.
- **Konfigurierbar und persistent** (mit abwärtskompatiblen Defaults):
  Start-Zonen-Ziel `20 s`, End-Zonen-Ziel `20 s` (beide als ≈10–30 s gedacht),
  Midpoint-Ziel `50 %` – der Midpoint ist eine Einstellung, nie hart kodiert.
- **Sehr kurze Ausgaben** verkleinern beide Reserven anteilig, statt sich zu
  überlappen oder zu fehlschlagen; der Midpoint bleibt stets dazwischen,
  komplette Clips und Übergänge bleiben erhalten.
- **Knappes `1. Start & End`-Material wird auf beide Enden verteilt**: Reicht
  die Rolle nicht für beide Reserven, werden ihre Clips entsprechend den
  konfigurierten Zielen aufgeteilt, statt dass die führende Zone alles verbraucht
  und das Ende seine Rolle verliert.
- **Eine Zone wird nie mit Clips einer anderen Rolle aufgefüllt.** Ist eine Rolle
  leer, endet ihre Zone an der natürlichen Clip-Grenze und die folgende Rolle
  beginnt dort; was die Zonen nicht verbraucht haben, behält seine
  Eingangsreihenfolge am Ende der Sequenz. Ordner ohne Rolle bleiben damit die
  allgemeine Reserve, die sie immer waren, und nichts geht verloren.
- **Zonenziele sind Positionen auf der gerenderten Timeline**, daher werden Clips
  mit dem kanonischen `Duration Before Merge`-Multiplikator (`0,70x`-Default)
  gemessen. Clip-Dauern selbst werden nie verändert.
- **Legacy Input Root** bleibt die optionale Legacy-Quelle und wird nie in das
  Drei-Rollen-System gezwungen.
- **YouTube Shorts** nutzen nur `1. Start & End` + `2. Start to Middle`;
  `3. Middle to End` ist ausgeschlossen. Die Checkbox *Allow '3. Middle to End'
  material in Shorts* schaltet ihn frei und wird separat gespeichert, die
  Shorts-Politik ist also unabhängig vom Long-Form. Ein Short behält seine
  historische Reihenfolge und seinen Without-Replacement-Pool-Cursor – nur sein
  Quellen-**Pool** wird eingeschränkt, die Shorts-Pipeline selbst ist unverändert.
  Würde der Ausschluss den Pool leeren, wird stattdessen der volle Pool genutzt.
- **Keine Rolle konfiguriert = byte-identisches Verhalten.** Ohne Rolle (oder ohne
  voiceover-getriebenes Ziel) ist der Scheduler ein No-op und die historische
  Sequenz wird unverändert gerendert.

Rendering, FFmpeg-Befehlsaufbau, Encoding/CRF/Bitrate/Codec, Übergänge, Blur,
Untertitel (ASR/Transkription, Alignment, Timing, Styling, Animation, Burn-in),
Voiceover- und Musik-Timing/-Lautstärke/-Looping/-Ducking/-Stream-Mapping,
Originalaudio sowie Intro-/Outro-Rendering bleiben unberührt: Das Feature lebt
ausschließlich auf der Clip-/Quellenauswahl-Ebene.

GUI: Gruppe `1 · Ordner` – Zeilen zeigen `Pfad · Rolle`, **Role**-Combo und
**Set Role** weisen zu, die Zeile `Timeline Areas` enthält Start Zone / Midpoint /
End Zone plus die Shorts-Checkbox. Die Video-Pool-Statuszeile rechnet mit
derselben Reihenfolge wie der Render, Status und Stage 1 widersprechen sich also
nie. Die Diagnostics nennen die aufgelösten Rollen, die weichen Ziele, die
Shorts-Politik und wie viele analysierte Clips in welche Area fallen.

CLI: `--folder-area ORDNER=AREA` (wiederholbar; `AREA` akzeptiert `1|2|3`,
`Start & End`, `2) Start to Middle`, `area_3`, die kanonischen Schlüssel, …),
`--area-start`, `--area-end`, `--area-midpoint` und `--shorts-allow-area3`.

Alle fünf Einstellungen werden in der Projektdatei gespeichert, überstehen ältere
Projektdateien (fehlende Felder fallen auf die dokumentierten Defaults zurück,
unbrauchbare oder widersprüchliche Werte werden zu „keine Rolle" statt zu einem
Absturz) und sind Teil der Render-Cache-Identität (`FINGERPRINT_SCHEMA = 5`),
eine andere Quellenreihenfolge rendert also immer neu. Eine Log-Zeile pro Auftrag
zeigt, was wirklich passiert ist, z. B. `Timeline areas (soft targets): 0.0-2.0 s
= 1. Start & End · … · 5 clip(s) re-grouped, none cut`, für den Shorts-Pool
`Timeline areas (Shorts): 3. Middle to End excluded → 4 of 6 clip(s) eligible
(1. Start & End + 2. Start to Middle).`

Eine qualitätsneutrale Render-Optimierung war weder nötig noch hinreichend
sicher und wurde daher nicht ergänzt: Der Scheduler ist ein einziger
Permutationsdurchlauf über bereits analysierte Metadaten und ändert keinen
Render-Graphen, keinen Filter und keinen Encoding-Parameter.
