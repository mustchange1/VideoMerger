# VideoMerger 1.2.4 – lokale Windows-App

VideoMerger 1.2.4 ist ein additives Release auf Basis des exakt getesteten 1.2.3-Artefakts. Basic Merge, getrennte Stage-1-/Stage-2-Flows, aktive manuelle Reihenfolge, vier Übergänge, Audio, Watermark, Validierung, Hardwareauswahl und nicht überschreibende Exporte bleiben erhalten.

## 1.2.4 – Großer Video-Pool (Required-Only), Quote-Karte, echte Subtitle-Preview, 4 neue Fonts

### Großer Video-Pool – nur benötigte Clips

Der Input-Ordner ist eine Quellbibliothek, keine Render-Warteschlange. Die Erkennung nutzt nur leichtgewichtiges `ffprobe`-Metadaten (Dauer, Auflösung, fps, Codec, Audiopräsenz, Größe – nie ein vollständiges Decodieren aller Dateien) und cached das Ergebnis. Die Auswahl stoppt, sobald die **aktive Reihenfolge** (Natürlich / Manuell / Zufällig) die aus dem Voiceover abgeleitete Zieldauer abdeckt: nur die benötigten Clips werden gerendert. Bei 300 verfügbaren und ~14 benötigten Clips gelangen genau ~14 Clips in die Pipeline – die übrigen tauchen in keinem Decode-, Filter-, Transition- oder Encode-Schritt auf. Der letzte Clip wird passend zutrimmt; ist das Material zu kurz, wiederholt die Full-Timeline-Loop die ausgewählte A-B-C-Sequenz und Hold Last Frame hält nur den letzten Frame. Die Vorverarbeitungsdauer skaliert nicht mit der Größe des ungenutzten Pools, und Änderungen an Subtitle-Stil/Quote-Text/Intro/Outro analysieren den Pool nie erneut. Die GUI zeigt `Videos im Input-Ordner / Benötigt / Ausgewählt / Nicht genutzt / Zieldauer` und aktualisiert sich nach Analyse, Voiceover-Änderungen, Zufall und manueller Sortierung.

### Quote-Karte (optional, stumm)

Eine neue optionale Sektion zwischen Intro und Main: `Intro → (Übergang) → Quote → (Übergang) → Main → (Übergang) → Outro`. Aktivierbar über `[ ] Quote hinzufügen`; Dauer 1,0–3,0 s (Standard 2,0 s). Kinoreife/editorial Design: dunkler neutraler Hintergrund mit dezentem Vignette, das Zitat als einziger Fokuspunkt leicht über dem mathematischen Zentrum, automatisch ausgewogene Zeilenumbrüche (Wort-/Phrasegrenzen, nie ein gebrochenes Wort, keine einzelnen Wort-Zeilen) und auflösungsbewusste Schriftgröße in nativer Auflösung (1080p / 1440p / 4K, 16:9 und 9:16). Verwendet das bestehende Transition-System. **Quote-Karten-Audio ist standardmäßig stumm**: kein Voiceover, keine generierte Musik, keine Untertitel – die Quote gelangt nie in die SRT/VTT/Burn-in-Zeitleiste und verschiebt niemals die Main-Video-Subtitle-Timing. Die Live-GUI-Vorschau wendet dieselbe Layout- und Umbruchlogik an wie der Renderer.

### Echte Subtitle-Preview (Preview ≈ Final Render)

Die GUI-Subtitle-Preview rendert das exakte Demo-Cue über dieselbe Umbruch-, Font-Metrics-, Safe-Area- und Positionslogik wie der eingebrannte Renderer – keine Fake-Texte, kein FFmpeg-Rendering. Font, Stil, Animation, Position, Zeilenumbruch, das max-zwei-Zeilen-Verhalten und Wort-Highlighting aktualisieren sich sofort.

### Vier weitere Fonts

Inter, Manrope, Lora und Roboto (Regular + Bold) ergänzen die bestehende Noto-Sans-Fallback: lesbarer, professioneller Long-Form-Look mit deutscher und englischer Unterstützung und kräftigen Bold-Varianten. Alle Fonts sind legal redistribuierbar (OFL / Apache-2.0, Lizenzen in `tools/fonts/`); proprietäre Fonts bleiben erkanungsseitig mit legalem Fallback. Der Selektor listet alle verfügbaren Fonts.

### 1.2.4-Defaults

Intro/Main/Outro Original Audio alle standardmäßig auf **Original** (Mute/Low/Original bleiben, unabhängig wählbar); Subtitle-Animation-Standard ist **Static Phrase** (Long-Form / YouTube Landscape, alle 5 Animationen wählbar); Output Preset **YouTube Landscape** + Qualität **Maximum** bleiben unverändert.

## 1.2.3 – Zufallsreihenfolge, Maximum Quality, Intro und mehrere Voiceovers

- **Randomize Order** mischt die aktive Clip-Liste per echtem unverzerrtem Fisher-Yates – nur die aktuelle Liste, keine wieder hinzugefügten Dateien. Die Mischung wird sofort aktive Exportreihenfolge und bleibt gespeichert. **Reset to Default Order** stellt die natürliche numerische/alphabetische Reihenfolge wieder her (1, 2, 3, 10 – nie 1, 10, 2, 3), nie die letzte Zufallsreihenfolge. Manuelles Ziehen bleibt die höchste Steuerung.
- **Maximum Quality** ist Standard: echtes `libx264` mit CRF 16, Preset `slow`, High Profile und yuv420p. Standard-Ausgabepreset **YouTube Landscape**: 16:9, Auto-Auflösung (höchste passende Quelle), Maximum Quality, Quell-/Auto-FPS, AAC-LC 48 kHz, Fast Start. 4K bleibt 4K, Quell-FPS bleibt erhalten, außer explizit geändert.
- **Optionaler Intro**: Endkomposition **Intro → Main → Outro**. Intro und Outro erhalten weder Main-Voiceover noch Main-Musik noch Untertitel; jede Sektion behält ihr eigenes Original-Audio mit unabhängigem Mute/Low/**Original** (Standard).
- **Mehrere Voiceover/Skript-Dateien**: eigene **Voiceover-Reihenfolge**-Liste (Hinzufügen/Entfernen/Hoch/Runter/Anfang/Ende/Reset), natürliche Standardreihenfolge, unabhängig von der Video-Reihenfolge, gespeichert. Skripte werden per normalisiertem Basisnamen automatisch zugeordnet und können manuell überschrieben werden. **Single Global Script** (Standard) oder **Multiple Matched Scripts** (fehlendes Skript = klare `SUBTITLE GENERATION FAILED [script matching]`-Fehlermeldung, nie stilles Bild ohne Untertitel). Jedes Paar wird separat ausgerichtet und mit kumulativen Offsets zu **einer kanonischen Untertitel-Timeline** für SRT/VTT/Burn-In verbunden.
- **CREATE FINAL VIDEO – ONE CLICK** rendert weiterhin die echte `MainVideo.mp4` und übergibt exakt diese Datei automatisch an die Intro→Main→Outro-Komposition. Ausgaben: `MainVideo.mp4`, `MainVideo.srt`, `MainVideo.vtt`, `FinalVideo.mp4`.
- Nur Video-Reihenfolge, Untertitel-Stil/-Animation, Intro oder Outro ändern? ASR/Ausrichtung laufen nicht erneut; alle Caches bleiben intakt.

## 1.2.2 – professionelle Untertitel und One Click (weiterhin enthalten)

- 16:9-Standard: **Clean Editorial + Type Reveal + Bottom**.
- Ruhige Satz-/Phrasenblöcke verwenden Satzzeichen, Phrasengrenzen, echte ausgewählte Font-Advances und visuelle Balance; Long Form ist strikt auf höchstens zwei Zeilen begrenzt.
- Die vollständige finale Phrase und der kanonische Zeilenumbruch bleiben in jedem Animationsevent reserviert. Reveal/Highlight verursacht dadurch kein Resize, Recenter, Reflow oder Jitter.
- Animationen: **Type Reveal, Color Change, Word Highlight, Outline Highlight, Static Phrase**.
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
- Music Volume: Very Quiet, Quiet/Background (Standard), Balanced, Medium, Custom
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

Short Form (9:16-Standard: Kinetic Chunk, Position Medium-Low):

1. SHORT FORM 1 – Kinetic Chunk
2. SHORT FORM 2 – Bold Highlight
3. SHORT FORM 3 – Clean Pop
4. SHORT FORM 4 – Karaoke Lite
5. SHORT FORM 5 – Impact

Long-Form-Presets verwenden ruhige, satzzeichen-/phrasenorientierte Blöcke, standardmäßig keine isolierten Einzelwortblöcke und strikt maximal zwei explizite Zeilen. Short-Form-Presets behalten stabile kompakte Gruppen ohne zufällige Positionierung, Schütteln oder Flashen. Die Animation wird unabhängig vom Preset gewählt. Bei Type Reveal bleiben zukünftige Glyphen transparent, aber vollständig im Layout; bei Color/Word/Outline Highlight bleibt die gesamte Phrase sichtbar. `Static Phrase` zeigt den vollständigen Block.

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
