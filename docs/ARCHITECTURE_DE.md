# Architektur – VideoMerger 1.5.0

## Architektur-Zusätze 1.5.0

- **Quellordner:** `discovery.discover_videos()` akzeptiert die explizit konfigurierte Ordnerliste und setzt `MediaInfo.source_folder`; der alte Ein-Ordner-Modus scannt weiterhin nur direkt enthaltene Dateien. `ProjectOrderStore` persistiert globale und ordnerspezifische aktive Reihenfolgen.
- **Auswahl:** `video_pool.order_media_for_video_order()` ist die gemeinsame Effective-Order-Pipeline für Natural, Alphabetical, Random und Manual. Natural/Alphabetical verwenden numerische bzw. filename-basierte Queues; Random permutiert zuerst per Fisher-Yates und alterniert danach Quellordner, solange Alternativen vorhanden sind. `folder_aware=False` ist der ausdrückliche bereits-geordnete Timeline-Pfad; die Legacy-Einstellung `folder_alternating` bleibt kompatibel.
- **Dauer:** `timeline.duration_before_merge_value()` skaliert jeden normalen Stage-1-Clip vor dem Timeline-Aufbau; `engine.post_process_duration()` ist die getrennte optionale After-Merge-Operation. Smart Last-Clip Stretch bleibt zwischen diesen Schritten und dem Render.
- **Defaults:** neue Projekte verwenden Before Merge `0,70x`, After Merge deaktiviert / `1,00x` und Long-Form Landscape `Center`; Short-Form Portrait bleibt `Bottom Center`. Gespeicherte Werte haben Vorrang.

## Additiver Aufbau

1.2.3 setzt direkt auf dem getesteten 1.2.2-Stand auf. `VideoMergerEngine`, `FFmpegCommandBuilder`, Target-Resolver, Transition-Katalog, MediaAnalyzer, Validator, Hardwareerkennung, Discovery, `ProjectOrderStore`, Stage 1 und Stage 2 bleiben die Kernschichten. Neue Zufallsreihenfolge, Qualitäts-/Ausgabepresets, Intro-Sektion, mehrere Voiceover/Skript-Einheiten und die Multi-Voiceover-Untertitel-Timeline verwenden diese Schichten, statt sie zu ersetzen.

```text
PySide6 GUI / CLI
  ├─ bestehender Basic Merge
  ├─ Stage 1: MainProjectEngine.create_main
  │   ├─ aktive manuelle Reihenfolge
  │   ├─ Voiceover/Script/Music/Watermark
  │   ├─ LocalWordAligner + getrennte ASR/Mapping-Caches
  │   ├─ fontmetrisches Phrase-/Zwei-Zeilen-Layout
  │   ├─ kanonische Timeline + SRT + VTT + ASS
  │   ├─ atomarer bestehender VideoMergerEngine-Export
  │   └─ First/Middle/Final-Verifikationsframes
  ├─ Stage 2: MainProjectEngine.add_outro
  │   ├─ Main + optionale Intro/Add-Image + Outro
  │   └─ pro Sektion isolierte Audio-Rollen und sichere Übergänge
  └─ One Click: MainProjectEngine.create_complete
      ├─ echte Stage 1 ausführen/validieren
      └─ exakt erzeugtes MainVideo an echte Stage 2 übergeben/validieren
```

## Kanonische Zeitquelle und Layout

`LocalWordAligner` verarbeitet ausschließlich die Voiceover-Datei. Das Skript bestimmt sichtbare Schreibweise und Satzzeichen; akustische Wortgrenzen bestimmen Start/Ende. Transkriptions- und Script-Mapping-Caches sind unabhängig von Stil, Font, Animation, Farbe und Position.

`subtitles.build_cues()` arbeitet nicht mit rohen Zeichenlimits als Wrapper. Es verwendet:

1. Satz-/Klauselzeichen und Phrasengrenzen;
2. maximale Wortzahl als Sicherheitslimit;
3. ausgewählte Glyph-Advances;
4. eine Balancefunktion für zwei Zeilen;
5. Strafen gegen grammatisch schwache Umbrüche und einzelne Restwörter;
6. Reparatur isolierter Long-Form-Gruppen.

Jeder `SubtitleCue` speichert `line_break_after`. Die kanonische Timeline Schema 2 enthält diesen Umbruch und `line_count`. Validierung lehnt mehr als zwei Zeilen, überlappende/ungültige Zeiten und fehlende Wörter ab.

## Font-Layer und Lizenzgrenze

`font_manager.py` bietet drei logische Wahlen. Noto Sans Regular/Bold liegt unter SIL OFL in `tools/fonts`. Eveleth Clean bleibt Detection-only und wird nicht verteilt.

Messreihenfolge:

1. echte cmap/hmtx-Advances einer gebündelten oder gefundenen TTF/OTF-Datei via fontTools;
2. `QFontMetricsF` im laufenden GUI;
3. nur für ungewöhnliche Headless-Installationen ein ausgewähltes Font-Profil als Fallback.

Die GUI registriert gebündelte Fonts nur pro Prozess. Libass erhält `fontsdir` im bestehenden `subtitles`-Filter. System-/Benutzerfonts bleiben systemseitig; proprietäre Dateien werden nie in Exporte oder ZIP kopiert.

## Stabile Animation

Jedes ASS-Event enthält die vollständigen Wörter des finalen Cue-Blocks sowie denselben expliziten `\N`-Umbruch:

- **Type Reveal:** zukünftige Wörter haben Alpha FF, bleiben aber im Shaping/Layout;
- **Color Change:** nur die aktuelle Wortfarbe ändert sich;
- **Word Highlight:** aktuelles Wort erhält ruhige Farbe (nur Long-Form);
- **Phrase Focus:** ruhiger, weicher Eintritt auf Cue-Ebene (Standard der Shorts);
- **Static Phrase:** ein vollständiges Event über die Cue-Dauer.

**Outline Highlight ist entfernt**: die Variante zeichnete pro Wort eine kräftige
Outline-Farbe und erzeugte damit gefüllte rechteckige Flächen außerhalb der
Glyphen. `subtitles.normalize_subtitle_animation(value, collection)` ist der
einzige Migrationspunkt (Outline Highlight → Color Change, Word Highlight →
Phrase Focus für Shorts, Unbekanntes → Collection-Standard); `animation_options()`
und `accepted_animation_values()` liefern die auswählbaren bzw. noch akzeptierten
Werte, sodass alte Projektdateien nie abstürzen. Alle verbleibenden Animationen
emittieren ausschließlich glyphenausgerichtete Overrides (`\c`, `\1a`, `\3a`,
`\fad`) – kein `\3c`, `\bord`, `\shad`, `\clip` oder Vektor-`\p` mehr.

Bei den synchronisierten Varianten beginnen Events exakt an kanonischen Wortstarts. Das optionale Debug-Layer erzeugt pro Wort ein eigenes Event mit Wort, Start und Ende. Es ist standardmäßig deaktiviert.

## Stage 1

1. Discovery liefert den aktuellen Pool und seine persistierte Manual-Historie.
2. `order_media_for_video_order()` erzeugt genau einmal die effektive Projektfolge; sie wird vor Required-Only-Auswahl und Duration-Fit verwendet.
3. MediaAnalyzer und Audio-Probes verwenden sichere Pfad/Größe/mtime-Caches.
4. Bei Voiceover ist das Ziel `Voiceover + Quiet Pause`; Hold und Full-Timeline Loop bleiben getrennt.
5. Voiceover + Skript erzwingt den Subtitle-Pfad.
6. Zielauflösung und ausgewählter Font fließen vor dem Rendern in `build_cues()` ein.
7. SRT, VTT, Timeline und ASS werden vor FFmpeg validiert.
8. Ein direkter atomarer `-filter_complex` erzeugt Übergänge, Canvas, ASS-Burn-In, Watermark und Audiomix. `-filter_complex_script` bleibt ausgeschlossen.
9. Das MainVideo wird mit FFprobe validiert; danach werden First/Middle/Final-PNGs aus genau diesem MP4 dekodiert.

Fehler entfernen das vollständige partielle Bundle und beginnen mit `SUBTITLE GENERATION FAILED [Stufe]`.

## One Click und Stage 2

`create_complete()` ist keine konzeptionelle Abkürzung:

1. `create_main()` erzeugt die echte, nicht überschreibende MainVideo-Datei.
2. `report.ok` und Existenz werden geprüft.
3. `dataclasses.replace(settings, main_video_path=str(main_result.video))` setzt exakt diesen Pfad.
4. Das bestehende `add_outro()` wird aufgerufen.
5. Stage 2 analysiert MainVideo + Outro und validiert FinalVideo mit FFprobe.

Stage 2 leert Subtitle-ASS, Voiceover, Script und Musik. Die in MainVideo enthaltene Quiet Pause bleibt daher frei von generierten Rollen. Outro-Audio verwendet nur Mute/Low/Original. Watermark-Scope Main/Outro/Both bleibt erhalten. Die kombinierte Fortschrittsanzeige bildet Stage 1 auf 0–72 % und Stage 2 auf 72–100 % ab.

## Audio- und Video-Invarianten

- Voiceover spielt einmal und wird nie geloopt.
- Musik wird nur bis zum gesprochenen Programm geloopt/getrimmt und geduckt.
- Clipaudio folgt Trim/Transition der visuellen Timeline.
- Stage 2 verwendet Main-Endaudio ↔ Outro-Originalaudio oder echten Concat ohne Transition.
- H.264 High, yuv420p, progressive, SAR 1:1, SDR BT.709, CRF 18, slow, faststart und AAC-LC Stereo 48 kHz bleiben Defaults.
- Auto/CPU/NVENC/QSV/AMF bleiben echte Probeentscheidungen mit CPU-Fallback.
- Exporte überschreiben keine vorhandene Datei.

## Validierung und Tests

FFprobe prüft MP4/Streams, Codec, Pixelformat, Auflösung, FPS, SAR, Seitenverhältnis, Dauer, Audio und Faststart. Subtitle-Tests führen echte Libass-Burns für fünf Animationen, fünf Long-Form-Stile, drei Fontwahlen/Fallbacks, vier Positionen sowie 1920×1080, 3840×2160 und 1080×1920 aus. One Click wird als echter Stage-1→Stage-2-Lauf mit Audio-/Subtitle-Isolation getestet; 1.2.3 testet zusätzlich Intro→Main→Outro, Fisher-Yates-Randomisierung, Multi-Voiceover-Ausrichtung mit kumulativen Offsets und Qualitäts-/Auflösungs-/FPS-Erhalt (720p/1080p/1440p/4K). Evidenz liegt unter `test_evidence/1.2.3/`.


## Architektur-Zusätze 1.3.0

### Windows-sichere Filterpfade

`filter_escape.filter_file_value()` ist die einzige Stelle für Dateipfade im Filtergraph. FFmpeg läuft mit `cwd = project_root()` (`engine._execute(..., working_directory)`); alle Render-Dateien (staged ASS unter `temp/` und `tools/fonts`) bekommen relative ASCII-Werte. Außerhalb des Ankers: UNQUOTED + Zwei-Stufen-Escape (apostrophe-sicher). `command_builder._filter_path` und `engine.burn_subtitles` nutzen ausschließlich diesen Einstiegspunkt.

### Dauer-Fit, Speed, End-Padding

`timeline.fit_media_to_duration(..., duration_fit_mode, max_stretch_percent, playback_rate)` bleibt die eine Auswahl-Mathematik. `cut` = exaktes 1.2.4-Verhalten; `stretch` zieht das Präfix bevorzugt einen Clip kürzer und dehnt nur den letzten (relativ zur geschwindigkeitsskalierten Timeline-Dauer, begrenzt). `video_pool` spiegelt dieselbe Entscheidung in O(n) (eine Präfix-Berechnung pro Status). `MediaInfo.playback_rate` wird im Graph via `setpts=PTS/rate` + `atempo` umgesetzt (nur Clip-Audio; Voiceover/Musik/Untertitel unberührt). `final_pause` bleibt das kanonische End-Padding-Feld (Standard 1,0 s für direkte
API-Aufrufe); die GUI schreibt es zusammen mit `long_form_outro_seconds`
(Nutzerstandard 2,5 s) aus einem einzigen Regler, sodass das frühere Main Video
End Padding und das neue Long-Form-Outro derselbe Abschnitt sind.

### Flexible Subtitle-Ausgaben + sauberer Output

`subtitle_output_mode` ist ein echter Pipeline-Vertrag: `with_subtitles` rendert intern einen sauberen Master, brennt genau einmal ASS und schreibt SRT/VTT, behält aber keine zusätzliche Clean-Datei; `with_and_without_subtitles` behält zusätzlich die Clean-Variante; `without_subtitles` überspringt Alignment und Burn-in vollständig. Der alte Wert `burned_and_sidecars` wird für gespeicherte Projekte als duale Ausgabe migriert. Chunked Rendering segmentiert zuerst den clean master und brennt höchstens einmal nach der Assembly. Stage 2 erhält anschließend genau die gewählte Main-Variante.

### Add Image / Legacy Image Insertion

`image_insertion.py` validiert ausschließlich PNG/JPG/JPEG/WEBP und normalisiert die kanonischen Grenzen Before Main/After Main (die alten After Intro/Before Outro-Aliasse bleiben kompatibel), Dauer, Transition, Fit, Zoom und die fünf deterministischen Looks. `MainProjectEngine.add_outro()` fügt genau eine `MediaInfo(is_image_insertion=True)` unmittelbar vor oder nach Main Video ein; Intro/Outro werden dabei nicht umgeordnet. `command_builder` loopt nur den Videoeingang, erzeugt die Projektauflösung mit aspect-safe Fit/Fill/Crop plus Zoom/Filter und stellt für die Bildposition ausschließlich `anullsrc` bereit. Die Transition-Familie und Dauer der Bildgrenzen werden unabhängig von den übrigen Clip-Grenzen sicher geklemmt; Audio, Untertitel und Voiceover bleiben unangetastet. SettingsStore, GUI und CLI verwenden dieselben Werte. Die Add-Image-Datei (einschließlich SHA-256) und alle Bildparameter stehen im unabhängigen Stage-2-Kompositions-Fingerprint; reine Add-Image-Änderungen invalidieren deshalb nicht den wiederverwendbaren Stage-1-Main-Render.

### Lokale YouTube-Metadaten

`youtube_metadata.py`: deterministischer Extraktor (Titel = stärkster Eröffnungssatz; Zusammenfassung = saliente Originalsätze; Themen = wörtliche Schlüsselphrasen; CTA) + optionale lokale Ollama-Politur unter strikter Validierung (nur `127.0.0.1:11434`, nie eine Cloud-API). Fehler blockieren das Rendern nie; ohne autoritatives Transkript wird nichts erfunden.

## YouTube Long-Form / Shorts / kombiniert

`youtube_outputs.py` plant drei echte Modi. Long-Form ruft die vorhandene Multi-Voiceover-Stage-1-Timeline mit erzwungenem 16:9 auf. Shorts erzeugen für jede geordnete Voiceover-Einheit einen eigenen 9:16-Auftrag mit `voiceover_pause=0`, eigener Zieldauer und eigener `render_variant_key`; dadurch sind auch doppelt referenzierte Audiodateien cache-separat. Matched Scripts bleiben basename-gekoppelt, ein globales Script wird als ein globaler Input erhalten und ist unabhängig von der Voiceover-Anzahl. `create_youtube_exports()` ordnet die fertigen Artefakte in `LongForm/` und `Shorts/` ein und wird auch vom One-Click-Worker verwendet.

Die Subtitle-Modi sind `with_subtitles`, `without_subtitles` und `with_and_without_subtitles`. Der neue Standard erzeugt Burn-in sowie SRT/VTT, aber keine zusätzliche Clean-Datei; der dritte Modus behält die Clean-Datei user-facing. Der alte gespeicherte Wert `burned_and_sidecars` wird als Kompatibilitätswert weiterhin als duale Ausgabe gelesen. Das Shorts-Profil wird erst unmittelbar im Short-Job auf `short_*`, mobile Schrift-/Safe-Position und synchronisierte Animation umgeschaltet; Long-Form-Profile bleiben unverändert.

### Getrennte Musik für Long-Form und Shorts

`ExportSettings.music_path` ist die Long-Form-/Basic-Musik, `ExportSettings.short_music_path` die eigene Shorts-Musik. `youtube_outputs.short_settings()` ersetzt `music_path` eines Short-Auftrags strikt durch die Shorts-Auswahl: ohne eigene Auswahl bleibt der Short ohne Hintergrundmusik, die Long-Form-Spur wird nie in einen vertikalen Render gemischt. `long_form_settings()` lässt `music_path` unverändert. Die Lautstärke ist seit Phase 23 ebenfalls getrennt: `long_form_music_volume` und `shorts_music_volume` (Sentinel `None`, Standard je `MUSIC_VOLUME_PERCENT = 44`) werden von `youtube_outputs.output_music_volume()` pro Auftrag aufgelöst und in das kanonische `music_volume` des jeweiligen Jobs kopiert – ein Wert ändert nie den anderen. Fehlt beiden Feldern ein Wert, dient das gespeicherte gemeinsame `music_volume` als Migrations-Fallback (`SettingsStore.load()` kopiert es in beide neuen Felder, überschreibt aber niemals explizit gespeicherte Werte). Preset, Ducking, Looping (`-stream_loop -1`) und Trimming bleiben die gemeinsame, unveränderte Musik-Pipeline der jeweils aktiven Spur. Die Stage-1-Cache-Identität entsteht weiterhin über das geprobte Musik-Asset-Payload, weshalb ein Musikwechsel korrekt invalidiert und beide Spuren getrennt cachefähig sind. `app/cli.py` ergänzt `--short-music`, die GUI eine zweite Auswahlzeile, `diagnostics.py` einen eigenen Prüfeintrag.

### Festes 0,7-Sekunden-Ende jedes Shorts

`youtube_outputs.SHORT_ENDING_SECONDS = 0.7` ist die einzige Quelle dieses Werts
und dient nur noch als garantierte Untergrenze für Settings-Objekte ohne das Feld
`short_outro_seconds`; `short_settings()` setzt `final_pause` jedes Short-Auftrags
auf das explizite Short-Outro (Nutzerstandard 0,7 s), das diesen Wert **ersetzt**
statt ihn zu einem zweiten sichtbaren Ende aufzustocken, während das Long-Form das
frei wählbare Long-Form-Outro behält. Die vorhandene Timeline-Logik setzt das Ziel deterministisch um: `create_main` berechnet `voice_total + final_pause` als Video-Zieldauer, `fit_media_to_duration` liefert dafür zusätzliches Bildmaterial (Clip-Auswahl, Übergänge, Hold/Loop, Chunked Rendering bleiben unverändert), und `subtitle_program_end = voice_total` begrenzt die Untertitel-Zeitleiste auf das gesprochene Audio. Das bestehende Guard-Raise verhindert zusätzlich, dass ein Cue in das Ende reicht; Voiceover-Audio endet ohnehin mit der Datei. Der Shorts-Pool ohne Ersatz reserviert pro Short `voice_total + final_pause` und stellt damit vorab genug Material für das Ende bereit.

### Eine Skript-Textdatei pro Short

`youtube_outputs.write_short_script_text()` schreibt `<finaler Videoname>.txt` (inklusive eines durch `_available_bundle` hochgezählten Namens) mit exakt dem Skripttext des Shorts: Quelle ist `main_project.global_script_path(job_settings)`, also die abgeleitete globale Skript-Sektion, das basename-gematchte Einzelskript oder – bei einem einzelnen Voiceover – das vollständige globale Skript. `MainProjectEngine._publish_short_script_text()` ruft sie im Auftrags-Loop nach jedem gerenderten Short auf; ein Audio-only-Short erhält keine Datei, ein Schreibfehler wird protokolliert und macht den fertigen Short nicht zum Fehlversuch. Es läuft keine zusätzliche ASR: die Sektionen stammen aus dem einen bereits vorhandenen globalen Mapping. Damit auch `without_subtitles` korrekte Texte liefert, leitet `_short_script_sections` die Sektionen in diesem Modus einmalig ab (dasselbe gecachte `align_global`, fehlertolerant), während der Render selbst weiterhin keine Ausrichtung, kein Burn-in, kein SRT und kein VTT erzeugt.

### Entferntes Quote-/Flyer-Artwork

`quote_artwork.py`, die GUI-Sektion inklusive PDF-Seite/Artwork Fit/Vorschau, `MediaInfo.is_quote_artwork`/`quote_fit_mode`, `ExportSettings.quote_*`, die CLI-Schalter `--quote*`, der Stage-2-Einbau in `add_outro`, die Quote-Zweige im `command_builder` und `QuotePreviewCanvas` sind entfernt; PyMuPDF ist keine Abhängigkeit mehr. Add Image (`is_image_insertion`, `image_*`), Intro/Outro und alle übrigen Timeline-Funktionen bleiben unverändert, und `_stage2_image_target()` (vormals `_quote_artwork_target`) bestimmt weiterhin die Auto-Auflösung für Standbilder. Alte Projektdateien bleiben ladbar, weil `SettingsStore.load()` unbekannte Schlüssel verwirft; `render_cache` hebt wegen der geänderten Payload-Form beide Fingerprint-Schemata auf `2`, damit keine Einträge aus der Zeit vor der Entfernung wiederverwendet werden.

## Visuelle Intro-/Outro-Abschnitte, Opening Effect, Legacy-Priorität

### Kanonische Timeline `[Intro][Voiceover][Outro]`

`youtube_outputs.MainTimeline` (eingefroren: `intro`, `spoken`, `outro`) ist die
eine Quelle der Wahrheit für einen Voiceover-getriebenen Auftrag: `voiceover_start`,
`spoken_end`, `subtitle_start`/`subtitle_end`, `target`, `audio_program` und
`log_lines()` leiten sich aus diesen drei Zahlen ab, und Video-Timeline,
Audio-Graph, Untertitel-Offset, Shorts-Pool-Reservierung und Log lesen dieselben
Werte (`main_timeline(settings, voice_total)`). `visual_section_seconds()`
validiert jeden Abschnitt (Zahl, endlich, ≥ 0, auf Millisekunden gerundet; 0
deaktiviert ihn, negativ wird abgelehnt, kein künstliches Limit – die
GUI-Obergrenze `MAX_VISUAL_SECTION_SECONDS = 60` ist reine Widget-Range).

`ExportSettings` trennt weiterhin kanonische und nutzerseitige Felder: kanonisch
bleiben `visual_intro_seconds = 0.0` und `final_pause = 1.0`, sodass ein direkter
`create_main(ExportSettings())`-Render exakt das frühere Verhalten zeigt;
nutzerseitig kommen `long_form_intro_seconds`/`long_form_outro_seconds` (je 1,5 s),
`short_intro_seconds`/`short_outro_seconds` (je 0,7 s), `opening_effect` (`"none"`)
und `legacy_input_root` (`""`) hinzu. `long_form_settings()` und `short_settings()`
kopieren die nutzerseitigen Werte in die kanonischen Felder – das Long-Form-Outro
**ist** damit das frühere Main Video End Padding: ein einziger Tail, der sich nie
verdoppeln kann. `SettingsStore.load()` migriert ein gespeichertes `final_pause`
nach `long_form_outro_seconds`, wenn das neue Feld fehlt; `app/cli.py` schreibt aus
`--pause`/`--end-padding` beide Namen; die GUI besitzt einen Regler in der Gruppe
„4d · Timeline – Visual Intro / Outro / Opening Effect“.

Audio: `command_builder` stellt dem Voiceover-Concat bei `intro > 0` ein
`anullsrc`-Segment `[vintro]` voran
(`[vintro][vu1]concat=n=2:v=0:a=1[vvoice_all]`); bei `intro == 0` bleibt der
historische Graph byte-identisch. Musik und Clip-Originalton laufen über
`audio_program = intro + spoken`, dürfen also während des Intros spielen und enden
mit der Sprache. Das Render-Ziel ist `intro + voice_total + outro`, wodurch
`fit_media_to_duration` und der Shorts-Pool ohne Ersatz echtes Bildmaterial für
alle drei Abschnitte reservieren – kein Schwarzbild, kein Standbild, kein
doppeltes Audio.

Untertitel: `main_project._offset_alignment(alignment, intro)` verschiebt die
kanonische Wort-Zeitleiste **vor** `_scale_alignment`, und
`subtitle_program_end = (intro + voice_total) / speed` begrenzt sie auf den
gesprochenen Teil. Die Verschiebung lebt damit im Timeline-Modell, nicht in einem
nachgelagerten Delay; Wort-Timing, globale Skript-Sektionen, SRT/VTT, Burn-in und
die strikte Cue-Validierung bleiben unverändert, und kein Cue reicht in Intro oder
Outro. `render_cache.FINGERPRINT_SCHEMA = 3` nimmt alle neuen Felder
(`long_form_intro_seconds`, `long_form_outro_seconds`, `short_intro_seconds`,
`short_outro_seconds`, `visual_intro_seconds`, `final_pause`, `opening_effect`) in
die Stage-1-Identität auf; Stage 2 bleibt bei Schema 2.

### Opening Effect des Main Videos

`opening_effects.py` ist ein bewusst kleines Register ohne Animations-Editor:
`none` (Standard), `zoom_in`, `zoom_out`. `normalize_opening_effect()` akzeptiert
Alias-/Groß-Kleinschreibvarianten und bildet Unbekanntes auf `none` ab.
`opening_effect_window(intro, program)` nutzt das visuelle Intro als
Öffnungsabschnitt (ohne Intro die festen `OPENING_EFFECT_SECONDS = 3.0`), begrenzt
ihn durch die Programmlänge und liefert unter
`MIN_OPENING_EFFECT_SECONDS = 0.5` gar keinen Effekt. `opening_effect_filter()`
erzeugt `scale=…:eval=frame:flags=lanczos` (5 % Spitze, gerade Größen via
`trunc(…/2)*2`) gefolgt von einem zentrierten `crop` fester Größe und `setsar=1`
**nach** dem Crop: zwischen dem pro Frame variierenden `scale` und dem festen
`crop` darf kein Filter stehen, weil FFmpeg 6.0 sonst bei einer Zoom-Out-Rampe
reproduzierbar abstürzt. `time_offset` hält die Rampe über Chunk-Grenzen
kontinuierlich. Eingebaut wird der Effekt in Stage 1 (`workflow_stage == "main"`)
als `[vprogram]…[vopening]` **vor** dem ASS-Burn-in, sodass Untertitel nie
skaliert werden; Shorts erhalten grundsätzlich `none`. Außerhalb des Fensters ist
die Kette ein verlustfreier Same-Size-Durchlauf, es wird kein Frame ergänzt oder
entfernt – Ziel-Dauer, Voiceover-Sync und Untertitel bleiben unberührt.

### Legacy Input Root Priorität (nur Random)

`video_pool.reserve_legacy_priority(media, legacy_root, rng, count=LEGACY_PRIORITY_CLIPS=3)`
zieht per `rng.sample` bis zu drei verschiedene Clips des Legacy Input Root,
mischt sie untereinander und entfernt sie **vor** der weiteren Sequenz aus dem
Pool; `order_media_for_video_order(..., legacy_root=)` ruft sie ausschließlich im
Random-Zweig auf und hängt `randomize_order` + `folder_aware_order` für Clip 4+
unverändert an. Gibt es keine eligible Wurzel (leer, fehlend, weniger als drei
Clips, Nicht-Random-Modus), wird nichts reserviert **und keine Zufälligkeit
verbraucht**, womit der historische unverfälschte Shuffle bit-identisch bleibt.
Manual, Alphabetical und Natural bleiben unberührt. `legacy_input_root` wird von
der GUI (`_settings()`) und der CLI (aufgelöstes `--input`) gesetzt und über
`main_project`, `engine`, `timeline` und `gui/workers` durchgereicht;
`_log_legacy_priority()` protokolliert genau eine Zeile mit den reservierten
Clipnamen – nur im Random-Modus, nur wenn wirklich reserviert wurde und nur dort,
wo die Reihenfolge erzeugt wurde.

## Phase 23: Musikfenster, eigene Übergänge, robuste Verifikation

### Musik von 0,000 s bis zum Video-Ende

`MainTimeline` kennt neben `voiceover_start`, `spoken_end`, `subtitle_start` und
`subtitle_end` auch `video_start` (immer `0.0`), `video_end` (= `target`),
`music_start` (immer `0.0`) und `music_end` (= `video_end`); `log_lines()` gibt
diese Werte einmal pro Auftrag aus und unterscheidet dabei
`music_configured=True/False` („Music: not configured …“ statt eines Fensters).
Der Audio-Graph folgt genau diesem Fenster: Musik wird nie per `adelay`
verschoben, erhält ihre Lautstärke **vor** dem Schnitt und endet exakt mit dem
letzten Frame. Das Voiceover bleibt unverändert an das gesprochene Programm
gebunden (`anullsrc`-Intro + `atrim=duration=<Programm>` + `apad`), sodass im
visuellen Outro zwar Musik, aber keine Sprache liegt.

`command_builder.music_outro_loop(program, target)` erzeugt die Erweiterung für
das visuelle Outro. Der geloopte Input wird weiterhin nur für das gesprochene
Programm gelesen (`atrim=duration=<Programm>`) – genau die historisch sichere
Menge: FFmpeg 6.0 blockiert bei 0 % CPU, sobald ein `-stream_loop -1`-Input eine
Verzweigung bis unmittelbar an das Ausgabe-Ende versorgen muss (gemessen mit
derselben 0,6-s-Spur: `atrim=duration=<Ziel>` hängt, `<Ziel − 0,1 s>` läuft in
0,4 s durch; unabhängig von `aresample async`, `asetpts`, `apad`-Variante und
endlicher `-t`-Begrenzung des Inputs). Deshalb wiederholt `aloop` das
**Ende** des bereits geschnittenen Programms: `window = min(programm,
max(outro, 1 s), MUSIC_LOOP_WINDOW_SECONDS = 15 s)`, `start =
(programm − window) · 48000`, `loop = ceil(outro / window)`. Die Wiederholung
beginnt exakt am Programmende (nahtlos), der Puffer ist auf 15 s begrenzt (ein
10-Minuten-Voiceover wird nie vollständig gepuffert), und das folgende
`atrim=duration=<Ziel>` schneidet exakt am Video-Ende. Ohne Outro
(`target == program`) ist die Kette byte-identisch zur früheren Form.

### Übergänge und Lautstärken pro Ausgabe

`output_transition_type()` und `output_transition_duration()` lösen
`long_form_transition_type`/`long_form_transition_duration` bzw.
`shorts_transition_type`/`shorts_transition_duration` pro Auftrag auf; leere
Sentinel-Werte fallen auf die gemeinsamen `transition_type`/`transition_duration`
zurück (Migrationspfad alter Projekte), und `TRANSITION_DURATION_LEGACY_DEFAULT`
bleibt der Wert für Basic-Merge/kanonische Direkt-Renders. Beide Ausgaben
starten mit `Cross Dissolve / 2,0 s`; `long_form_settings()` und
`short_settings()` schreiben die aufgelösten Werte in die kanonischen Felder,
sodass Combined-Modus und One-Click automatisch die jeweils eigenen Werte
verwenden. Alle Resolver klemmen und validieren (0–150 %, ≥ 0 s, endlich) und
werfen `VideoMergerError` mit lesbarem Label, den `diagnostics.py` im Eintrag
„Output Music & Transitions“ fail-closed anzeigt, statt ihn zu verschlucken.

`render_cache.FINGERPRINT_SCHEMA` steht auf `4`: Die Stage-1-Identität enthält
die vier Abschnittsdauern, beide Musik-Lautstärken (nur bei konfigurierter Spur)
und die vier Übergangswerte (unbedingt) neben Opening Effect,
Animations-/Profilwerten und der effektiven Medienreihenfolge. `load()` bleibt
fail-closed – abweichendes Schema oder Digest liefert `None`, kein Eintrag einer
älteren Version wird still wiederverwendet. Stage 2 behält Schema `2`.

### Visuelle Verifikation: begrenzt, wiederholt, getrennt klassifiziert

`subtitle_verification.py` dekodiert die Nachweisbilder aus der fertigen,
FFprobe-validierten MP4. `frame_safe_margin(duration, fps)` leitet den Abstand
vom Dateiende aus der realen Framerate ab (zwei Frame-Perioden, mindestens
`MINIMUM_FRAME_MARGIN_SECONDS = 0.04`), schrumpft ihn für sehr kurze Dateien auf
ein Viertel ihrer Dauer und greift ohne verwertbare Dauer auf
`DEFAULT_VERIFICATION_FPS = 25` zurück. `bounded_verification_times()` liefert
den angefragten Zeitstempel (aus dem Wort-Timing) plus bis zu drei strikt
frühere Kandidaten (`MAXIMUM_VERIFICATION_ATTEMPTS = 4`); kein Kandidat liegt am
oder hinter dem Dateiende, keiner ist negativ, Dubletten entfallen.

`png_frame_status()` akzeptiert eine PNG nur, wenn sie existiert, nicht leer ist,
Signatur und `IHDR` trägt, von Null verschiedene Dimensionen hat und `IEND`
enthält – eine 0-Byte- oder abgeschnittene Datei wird entfernt und erneut
versucht. `_extract_frame()` meldet `OSError`, `TimeoutExpired`, einen
Returncode ≠ 0 und jede ungültige Datei als `(False, Grund)` statt zu werfen.
`verify_subtitle_frames()` liefert ein `VisualVerification`-Objekt
(`FrameVerification` pro Label mit `requested`, `used`, `attempts`, `detail`,
`status` ∈ PASS/DEGRADED/FAIL/SKIPPED) und protokolliert genau eine Zeile pro
Bild; `create_visual_verification_frames()` bleibt als kompatibler Wrapper
erhalten, der nur die erfolgreich dekodierten Pfade zurückgibt.

`main_project.create_main()` trennt die Kategorien: Der kritische Block prüft
weiterhin kanonische Timeline sowie SRT/VTT und wirft bei einem echten Problem
`SUBTITLE GENERATION FAILED [subtitle output artifacts]`; die optionale
Verifikation läuft danach in einem eigenen `try`, dessen Fehler lediglich
`WARNUNG: Visuelle Verifikation nicht möglich, Ausgabe bleibt gültig: …`
protokolliert. `MainVideoResult.verification_status` transportiert die
Klassifizierung (`CACHED` bei einem Cache-Hit), während `verification_frames`
weiterhin die Liste der dekodierten Bilder ist. Ein fehlgeschlagenes
Verifikationsbild löscht damit nie wieder eine gültige Ausgabe – der frühere
Realfehler (`SUBTITLE GENERATION FAILED [first/middle/final visual
verification]: … keine gültige PNG-Ausgabe` bei 80,792 s Dauer, obwohl MP4,
FFprobe, Audio, Video und Burn-in gültig waren) ist strukturell ausgeschlossen,
weil der letzte Zeitstempel aus der Wort-Zeitleiste stets am oder hinter dem
Dateiende liegen konnte. Echte Fehler – Untertitel-Erzeugung, ungültige
Zeitachse, fehlende/leere Artefakte, Burn-in, FFprobe-Validierung – behalten die
strikte Klassifizierung und räumen weiterhin auf.
