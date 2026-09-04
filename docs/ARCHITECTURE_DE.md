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
- **Word Highlight:** aktuelles Wort erhält ruhige Farbe/Outline;
- **Outline Highlight:** aktuelles Wort erhält eine stärkere Outline;
- **Static Phrase:** ein vollständiges Event über die Cue-Dauer.

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

`timeline.fit_media_to_duration(..., duration_fit_mode, max_stretch_percent, playback_rate)` bleibt die eine Auswahl-Mathematik. `cut` = exaktes 1.2.4-Verhalten; `stretch` zieht das Präfix bevorzugt einen Clip kürzer und dehnt nur den letzten (relativ zur geschwindigkeitsskalierten Timeline-Dauer, begrenzt). `video_pool` spiegelt dieselbe Entscheidung in O(n) (eine Präfix-Berechnung pro Status). `MediaInfo.playback_rate` wird im Graph via `setpts=PTS/rate` + `atempo` umgesetzt (nur Clip-Audio; Voiceover/Musik/Untertitel unberührt). `final_pause` bleibt die autoritative End-Padding-Größe (GUI: freier Spin, Standard 1,0 s).

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

`ExportSettings.music_path` ist die Long-Form-/Basic-Musik, `ExportSettings.short_music_path` die eigene Shorts-Musik. `youtube_outputs.short_settings()` ersetzt `music_path` eines Short-Auftrags strikt durch die Shorts-Auswahl: ohne eigene Auswahl bleibt der Short ohne Hintergrundmusik, die Long-Form-Spur wird nie in einen vertikalen Render gemischt. `long_form_settings()` lässt `music_path` unverändert. Lautstärke, Preset, Ducking, Looping (`-stream_loop -1`) und Trimming bleiben die gemeinsame, unveränderte Musik-Pipeline der jeweils aktiven Spur. Die Stage-1-Cache-Identität entsteht weiterhin über das geprobte Musik-Asset-Payload, weshalb ein Musikwechsel korrekt invalidiert und beide Spuren getrennt cachefähig sind. `app/cli.py` ergänzt `--short-music`, die GUI eine zweite Auswahlzeile, `diagnostics.py` einen eigenen Prüfeintrag.

### Festes 0,7-Sekunden-Ende jedes Shorts

`youtube_outputs.SHORT_ENDING_SECONDS = 0.7` ist die einzige Quelle dieses Werts; `short_settings()` setzt damit `final_pause` jedes Short-Auftrags, während das Long-Form das frei wählbare `Main Video End Padding` behält. Die vorhandene Timeline-Logik setzt das Ziel deterministisch um: `create_main` berechnet `voice_total + final_pause` als Video-Zieldauer, `fit_media_to_duration` liefert dafür zusätzliches Bildmaterial (Clip-Auswahl, Übergänge, Hold/Loop, Chunked Rendering bleiben unverändert), und `subtitle_program_end = voice_total` begrenzt die Untertitel-Zeitleiste auf das gesprochene Audio. Das bestehende Guard-Raise verhindert zusätzlich, dass ein Cue in das Ende reicht; Voiceover-Audio endet ohnehin mit der Datei. Der Shorts-Pool ohne Ersatz reserviert pro Short `voice_total + final_pause` und stellt damit vorab genug Material für das Ende bereit.

### Eine Skript-Textdatei pro Short

`youtube_outputs.write_short_script_text()` schreibt `<finaler Videoname>.txt` (inklusive eines durch `_available_bundle` hochgezählten Namens) mit exakt dem Skripttext des Shorts: Quelle ist `main_project.global_script_path(job_settings)`, also die abgeleitete globale Skript-Sektion, das basename-gematchte Einzelskript oder – bei einem einzelnen Voiceover – das vollständige globale Skript. `MainProjectEngine._publish_short_script_text()` ruft sie im Auftrags-Loop nach jedem gerenderten Short auf; ein Audio-only-Short erhält keine Datei, ein Schreibfehler wird protokolliert und macht den fertigen Short nicht zum Fehlversuch. Es läuft keine zusätzliche ASR: die Sektionen stammen aus dem einen bereits vorhandenen globalen Mapping. Damit auch `without_subtitles` korrekte Texte liefert, leitet `_short_script_sections` die Sektionen in diesem Modus einmalig ab (dasselbe gecachte `align_global`, fehlertolerant), während der Render selbst weiterhin keine Ausrichtung, kein Burn-in, kein SRT und kein VTT erzeugt.

### Entferntes Quote-/Flyer-Artwork

`quote_artwork.py`, die GUI-Sektion inklusive PDF-Seite/Artwork Fit/Vorschau, `MediaInfo.is_quote_artwork`/`quote_fit_mode`, `ExportSettings.quote_*`, die CLI-Schalter `--quote*`, der Stage-2-Einbau in `add_outro`, die Quote-Zweige im `command_builder` und `QuotePreviewCanvas` sind entfernt; PyMuPDF ist keine Abhängigkeit mehr. Add Image (`is_image_insertion`, `image_*`), Intro/Outro und alle übrigen Timeline-Funktionen bleiben unverändert, und `_stage2_image_target()` (vormals `_quote_artwork_target`) bestimmt weiterhin die Auto-Auflösung für Standbilder. Alte Projektdateien bleiben ladbar, weil `SettingsStore.load()` unbekannte Schlüssel verwirft; `render_cache` hebt wegen der geänderten Payload-Form beide Fingerprint-Schemata auf `2`, damit keine Einträge aus der Zeit vor der Entfernung wiederverwendet werden.
