# Architektur – VideoMerger 1.3.0

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
  │   └─ bestehender Export mit Main + Outro
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

1. Discovery liefert die exakte aktive Reihenfolge.
2. MediaAnalyzer und Audio-Probes verwenden sichere Pfad/Größe/mtime-Caches.
3. Bei Voiceover ist das Ziel `Voiceover + Quiet Pause`; Hold und Full-Timeline Loop bleiben getrennt.
4. Voiceover + Skript erzwingt den Subtitle-Pfad.
5. Zielauflösung und ausgewählter Font fließen vor dem Rendern in `build_cues()` ein.
6. SRT, VTT, Timeline und ASS werden vor FFmpeg validiert.
7. Ein direkter atomarer `-filter_complex` erzeugt Übergänge, Canvas, ASS-Burn-In, Watermark und Audiomix. `-filter_complex_script` bleibt ausgeschlossen.
8. Das MainVideo wird mit FFprobe validiert; danach werden First/Middle/Final-PNGs aus genau diesem MP4 dekodiert.

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

`filter_escape.filter_file_value()` ist die einzige Stelle für Dateipfade im Filtergraph. FFmpeg läuft mit `cwd = project_root()` (`engine._execute(..., working_directory)`); alle Render-Dateien (staged ASS unter `temp/`, `tools/fonts`, Quote-Font) bekommen relative ASCII-Werte. Außerhalb des Ankers: UNQUOTED + Zwei-Stufen-Escape (apostrophe-sicher). `command_builder._filter_path`, `quote._filter_path_for` und `engine.burn_subtitles` nutzen ausschließlich diesen Einstiegspunkt.

### Dauer-Fit, Speed, End-Padding

`timeline.fit_media_to_duration(..., duration_fit_mode, max_stretch_percent, playback_rate)` bleibt die eine Auswahl-Mathematik. `cut` = exaktes 1.2.4-Verhalten; `stretch` zieht das Präfix bevorzugt einen Clip kürzer und dehnt nur den letzten (relativ zur geschwindigkeitsskalierten Timeline-Dauer, begrenzt). `video_pool` spiegelt dieselbe Entscheidung in O(n) (eine Präfix-Berechnung pro Status). `MediaInfo.playback_rate` wird im Graph via `setpts=PTS/rate` + `atempo` umgesetzt (nur Clip-Audio; Voiceover/Musik/Untertitel unberührt). `final_pause` bleibt die autoritative End-Padding-Größe (GUI: freier Spin, Standard 1,0 s).

### Doppelte Untertitel-Ausgaben + sauberer Output

`create_main` rendert zuerst das saubere Master (`_no_subtitles.mp4`), dann brennt `engine.burn_subtitles()` die ASS in die primäre Datei (libass-Pass, Audio Stream-Copy, identische Encoder-Argumente). SRT/VTT liegen im Output; Timeline-JSON und Verifikations-PNGs bleiben unter `temp/`. `create_complete` reserviert beide FinalVideo-Namen vorab, rendert die primäre (untertitelte) Komposition aus dem untertitelten Main und die Clean-Variante aus dem sauberen Main.

### Quote-Karten-Stile

`quote.QUOTE_STYLES` (fünf `QuoteStyleSpec`) definieren Hintergrund/Text/Attribution/Hairline/Korn/Default-Font; `layout_quote(...)` nimmt alle manuellen Regler keyword-only an (Geometrie-erhaltende Defaults). `quote_video_chain()` setzt stilabhängig Vignette/Korn/Hairline/drawtext/zoompan (d=1 → Dauer exakt) und bleibt stumm (anullsrc). `add_outro` validiert die freie Dauer 0,5–5,0 s und kann die Übergänge um die Karte separat begrenzen (`quote_transition_duration`, gleiche 45-%-Clamp-Regel).

### Lokale YouTube-Metadaten

`youtube_metadata.py`: deterministischer Extraktor (Titel = stärkster Eröffnungssatz; Zusammenfassung = saliente Originalsätze; Themen = wörtliche Schlüsselphrasen; CTA) + optionale lokale Ollama-Politur unter strikter Validierung (nur `127.0.0.1:11434`, nie eine Cloud-API). Fehler blockieren das Rendern nie; ohne autoritatives Transkript wird nichts erfunden.
