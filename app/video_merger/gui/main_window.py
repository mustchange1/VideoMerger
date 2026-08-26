from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from PySide6.QtCore import QThread, Qt, QUrl, Signal
from PySide6.QtGui import QCloseEvent, QDesktopServices, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QFileDialog,
    QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QRadioButton,
    QScrollArea, QSlider, QSpinBox, QDoubleSpinBox, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from ..diagnostics import run_diagnostics, run_project_diagnostics
from ..logging_utils import configure_file_logger
from ..font_manager import FONT_OPTIONS, register_bundled_fonts_with_qt, resolve_font
from ..models import ExportSettings, ProgressEvent
from ..project_order import natural_order, natural_sort_key, randomize_order
from ..project_assets import probe_audio
from ..quality import QUALITY_KEYS, QUALITY_PRESETS, quality_label
from ..subtitles import ANIMATION_OPTIONS
from ..paths import ensure_project_directories, locate_ffmpeg, project_root
from ..project_order import ProjectOrderStore
from ..settings_store import SettingsStore
from ..subtitle_preview import QuotePreviewCanvas, SubtitlePreviewCanvas, sample_subtitle_text
from ..video_pool import compute_pool_status
from ..transition_effects import EASE_OPTIONS, TRANSITION_OPTIONS, transition_description
from .style import APP_STYLE
from .workers import ProcessingWorker


def _format_time(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "--:--:--"
    value = int(round(seconds))
    return f"{value // 3600:02d}:{(value % 3600) // 60:02d}:{value % 60:02d}"


class ReorderTableWidget(QTableWidget):
    """Row-oriented table that reports an internal drag's requested position."""

    row_move_requested = Signal(int, int)

    def __init__(self, rows: int, columns: int, parent=None):
        super().__init__(rows, columns, parent)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)

    def dropEvent(self, event: QDropEvent) -> None:
        if event.source() is not self or self.currentRow() < 0:
            event.ignore()
            return
        source = self.currentRow()
        target = self.indexAt(event.position().toPoint()).row()
        if target < 0:
            target = max(0, self.rowCount() - 1)
        event.acceptProposedAction()
        if source != target:
            self.row_move_requested.emit(source, target)


class DiagnosticsDialog(QDialog):
    def __init__(self, parent=None, project_items=None):
        super().__init__(parent)
        self.setWindowTitle("System Diagnostics")
        self.resize(760, 430)
        layout = QVBoxLayout(self)
        title = QLabel("System Diagnostics")
        title.setObjectName("title")
        layout.addWidget(title)
        table = QTableWidget(0, 3)
        table.setHorizontalHeaderLabels(["Komponente", "Status", "Details"])
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        items = run_diagnostics(test_encoders=False)
        items.extend(project_items or [])
        table.setRowCount(len(items))
        for row, item in enumerate(items):
            table.setItem(row, 0, QTableWidgetItem(item.name))
            status = QTableWidgetItem("OK" if item.ok else "FEHLER")
            status.setForeground(Qt.GlobalColor.green if item.ok else Qt.GlobalColor.red)
            table.setItem(row, 1, status)
            table.setItem(row, 2, QTableWidgetItem(item.detail))
        layout.addWidget(table)
        summary = QLabel("All systems ready." if all(item.ok for item in items) else "Mindestens eine Prüfung ist fehlgeschlagen.")
        layout.addWidget(summary)
        close = QPushButton("Schließen")
        close.clicked.connect(self.accept)
        layout.addWidget(close, alignment=Qt.AlignRight)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        ensure_project_directories()
        self.root = project_root()
        self.store = SettingsStore()
        self.saved = self.store.load()
        self.order_store = ProjectOrderStore()
        self.current_media: list = []
        self.logger, self.log_path = configure_file_logger()
        self.thread: QThread | None = None
        self.worker: ProcessingWorker | None = None
        self.last_output: Path | None = None
        self.active_mode = ""
        self.busy = False

        self.setWindowTitle("VideoMerger – Local Studio")
        self.setMinimumSize(900, 760)
        self.resize(1060, 900)
        self.setAcceptDrops(True)
        register_bundled_fonts_with_qt()
        self._build_ui()
        self.setStyleSheet(APP_STYLE)
        self._load_settings()
        self._append_log("VideoMerger 1.2.4 gestartet – Video-Pool (Required-Only), Quote-Karte, echte Subtitle-Preview. Alle Videodaten bleiben lokal.")

    def _build_ui(self) -> None:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        central = QWidget()
        scroll.setWidget(central)
        self.setCentralWidget(scroll)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(22, 18, 22, 22)
        outer.setSpacing(12)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("VideoMerger")
        title.setObjectName("title")
        subtitle = QLabel("Zwei Stufen · Voiceover · Musik · Wort-Sync · Untertitel · Video-Pool · Quote-Karte · Outro · lokal")
        subtitle.setObjectName("subtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()
        diagnostics = QPushButton("System Diagnostics")
        diagnostics.clicked.connect(self._show_diagnostics)
        header.addWidget(diagnostics)
        outer.addLayout(header)

        io_group = QGroupBox("1 · Ordner")
        io_layout = QGridLayout(io_group)
        self.input_edit = QLineEdit()
        self.input_edit.textChanged.connect(self._clear_stale_analysis)
        self.output_edit = QLineEdit()
        browse_input = QPushButton("Browse …")
        browse_output = QPushButton("Browse …")
        browse_input.clicked.connect(self._browse_input)
        browse_output.clicked.connect(self._browse_output)
        io_layout.addWidget(QLabel("Input Folder"), 0, 0)
        io_layout.addWidget(self.input_edit, 0, 1)
        io_layout.addWidget(browse_input, 0, 2)
        io_layout.addWidget(QLabel("Output Folder"), 1, 0)
        io_layout.addWidget(self.output_edit, 1, 1)
        io_layout.addWidget(browse_output, 1, 2)
        drop_hint = QLabel("Videoordner hierher ziehen – MP4, MOV, MKV, AVI, WebM, M4V und weitere")
        drop_hint.setObjectName("dropHint")
        io_layout.addWidget(drop_hint, 2, 0, 1, 3)
        outer.addWidget(io_group)

        audio_group = QGroupBox("2 · Audio & Script")
        audio_layout = QGridLayout(audio_group)
        self.music_edit = QLineEdit()
        music_button = QPushButton("Choose …")
        music_button.clicked.connect(lambda: self._browse_asset(self.music_edit, "audio"))
        self.script_mode_combo = QComboBox()
        self.script_mode_combo.addItem("Single Global Script (eine Textdatei für die ganze Timeline)", "single")
        self.script_mode_combo.addItem("Multiple Matched Scripts (eine Textdatei pro Voiceover)", "matched")
        self.script_mode_combo.currentIndexChanged.connect(self._sync_subtitle_request)
        self.voiceover_table = ReorderTableWidget(0, 3)
        self.voiceover_table.setHorizontalHeaderLabels(["#", "Voiceover", "Script"])
        self.voiceover_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.voiceover_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.voiceover_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.voiceover_table.setMaximumHeight(150)
        self.voiceover_table.row_move_requested.connect(self._move_voiceover_row)
        self.voiceover_add_button = QPushButton("Add Voiceover Files …")
        self.voiceover_remove_button = QPushButton("Remove Selected")
        self.voiceover_script_button = QPushButton("Choose Script for Selected …")
        self.voiceover_up_button = QPushButton("Move Up")
        self.voiceover_down_button = QPushButton("Move Down")
        self.voiceover_top_button = QPushButton("Move to Top")
        self.voiceover_bottom_button = QPushButton("Move to Bottom")
        self.voiceover_reset_button = QPushButton("Reset to Default Order")
        self.voiceover_add_button.clicked.connect(self._add_voiceovers)
        self.voiceover_remove_button.clicked.connect(self._remove_voiceover)
        self.voiceover_script_button.clicked.connect(self._choose_voiceover_script)
        self.voiceover_up_button.clicked.connect(lambda: self._move_voiceover_selected(-1))
        self.voiceover_down_button.clicked.connect(lambda: self._move_voiceover_selected(1))
        self.voiceover_top_button.clicked.connect(lambda: self._move_voiceover_selected_to(-10_000))
        self.voiceover_bottom_button.clicked.connect(lambda: self._move_voiceover_selected_to(10_000))
        self.voiceover_reset_button.clicked.connect(self._reset_voiceover_order)
        audio_layout.addWidget(QLabel("Script Mode"), 0, 0)
        audio_layout.addWidget(self.script_mode_combo, 0, 1, 1, 2)
        audio_layout.addWidget(self.voiceover_table, 1, 0, 1, 3)
        voice_buttons = QHBoxLayout()
        voice_buttons.addWidget(self.voiceover_add_button)
        voice_buttons.addWidget(self.voiceover_remove_button)
        voice_buttons.addWidget(self.voiceover_script_button)
        voice_buttons.addWidget(self.voiceover_up_button)
        voice_buttons.addWidget(self.voiceover_down_button)
        voice_buttons.addWidget(self.voiceover_top_button)
        voice_buttons.addWidget(self.voiceover_bottom_button)
        voice_buttons.addWidget(self.voiceover_reset_button)
        audio_layout.addLayout(voice_buttons, 2, 0, 1, 3)
        audio_layout.addWidget(QLabel("Background Music"), 3, 0)
        audio_layout.addWidget(self.music_edit, 3, 1)
        audio_layout.addWidget(music_button, 3, 2)
        # 1.2.4 Default: Original Audio (Mute/Low bleiben unabhängig wählbar).
        self.original_audio_combo = QComboBox()
        self.original_audio_combo.addItem("Original (Standard)", "original")
        self.original_audio_combo.addItem("Low", "low")
        self.original_audio_combo.addItem("Mute", "mute")
        audio_layout.addWidget(QLabel("Original Video Audio"), 4, 0)
        audio_layout.addWidget(self.original_audio_combo, 4, 1)
        self.voice_volume_slider = QSlider(Qt.Horizontal)
        self.voice_volume_slider.setRange(0, 125)
        self.voice_volume_value = QLabel()
        self.voice_volume_slider.valueChanged.connect(
            lambda value: self.voice_volume_value.setText(f"{value} %")
        )
        audio_layout.addWidget(QLabel("Voiceover Volume"), 5, 0)
        audio_layout.addWidget(self.voice_volume_slider, 5, 1)
        audio_layout.addWidget(self.voice_volume_value, 5, 2)
        self.music_preset_combo = QComboBox()
        for label, key, value in (
            ("Very Quiet", "very_quiet", 10), ("Quiet / Background", "quiet", 22),
            ("Balanced", "balanced", 35), ("Medium", "medium", 50), ("Custom", "custom", -1),
        ):
            self.music_preset_combo.addItem(label, (key, value))
        self.music_preset_combo.currentIndexChanged.connect(self._music_preset_changed)
        self.music_volume_slider = QSlider(Qt.Horizontal)
        self.music_volume_slider.setRange(0, 100)
        self.music_volume_value = QLabel()
        self.music_volume_slider.valueChanged.connect(self._music_volume_changed)
        audio_layout.addWidget(QLabel("Music Preset"), 6, 0)
        audio_layout.addWidget(self.music_preset_combo, 6, 1)
        audio_layout.addWidget(QLabel("Music Volume"), 7, 0)
        audio_layout.addWidget(self.music_volume_slider, 7, 1)
        audio_layout.addWidget(self.music_volume_value, 7, 2)
        self.ducking_check = QCheckBox("Voiceover Ducking – Musik weich unter Sprache absenken")
        audio_layout.addWidget(self.ducking_check, 8, 0, 1, 3)
        self.pause_combo = QComboBox()
        for value in (0.5, 1.0, 1.5, 2.0):
            self.pause_combo.addItem(f"{value:.1f} sec", value)
        self.short_video_combo = QComboBox()
        self.short_video_combo.addItem("Hold Last Frame – finalen Frame halten", "hold")
        self.short_video_combo.addItem("Full-Timeline Loop – komplette manuelle Reihenfolge wiederholen", "loop")
        # 1.2.4: Zieldauer-Einflüsse sofort im Video-Pool-Status zeigen.
        self.pause_combo.currentIndexChanged.connect(self._update_pool_status)
        self.short_video_combo.currentIndexChanged.connect(self._update_pool_status)
        audio_layout.addWidget(QLabel("Quiet Pause before Outro"), 9, 0)
        audio_layout.addWidget(self.pause_combo, 9, 1)
        audio_layout.addWidget(QLabel("If Video Is Too Short"), 10, 0)
        audio_layout.addWidget(self.short_video_combo, 10, 1)
        outer.addWidget(audio_group)

        subtitle_group = QGroupBox("3 · Subtitles")
        subtitle_layout = QGridLayout(subtitle_group)
        self.subtitle_check = QCheckBox(
            "Enable Burned-In Subtitles + SRT + VTT (automatic when Voiceover + Script are assigned)"
        )
        subtitle_layout.addWidget(self.subtitle_check, 0, 0, 1, 3)
        self.subtitle_language_combo = QComboBox()
        self.subtitle_language_combo.addItems(["German", "English", "Auto"])
        self.subtitle_style_combo = QComboBox()
        from ..subtitle_presets import SUBTITLE_PRESETS
        for preset in SUBTITLE_PRESETS:
            self.subtitle_style_combo.addItem(preset.label, preset.key)
            self.subtitle_style_combo.setItemData(
                self.subtitle_style_combo.count() - 1, preset.description, Qt.ToolTipRole
            )
        self.subtitle_animation_combo = QComboBox()
        for key, label in ANIMATION_OPTIONS:
            self.subtitle_animation_combo.addItem(label, key)
        self.subtitle_font_combo = QComboBox()
        for key, label in FONT_OPTIONS:
            self.subtitle_font_combo.addItem(label, key)
        self.subtitle_position_combo = QComboBox()
        self.subtitle_position_combo.addItems(["Bottom", "Medium-Low", "Middle", "Top"])
        self.subtitle_debug_check = QCheckBox("Subtitle Debug Overlay – current word + exact start/end (default OFF)")
        subtitle_layout.addWidget(QLabel("Language"), 1, 0)
        subtitle_layout.addWidget(self.subtitle_language_combo, 1, 1)
        subtitle_layout.addWidget(QLabel("Style"), 2, 0)
        subtitle_layout.addWidget(self.subtitle_style_combo, 2, 1)
        subtitle_layout.addWidget(QLabel("Animation"), 3, 0)
        subtitle_layout.addWidget(self.subtitle_animation_combo, 3, 1)
        subtitle_layout.addWidget(QLabel("Font"), 4, 0)
        subtitle_layout.addWidget(self.subtitle_font_combo, 4, 1)
        subtitle_layout.addWidget(QLabel("Position"), 5, 0)
        subtitle_layout.addWidget(self.subtitle_position_combo, 5, 1)
        subtitle_layout.addWidget(self.subtitle_debug_check, 6, 0, 1, 3)
        # 1.2.4: echte Subtitle-Preview – dieselbe Layout-Logik wie der
        # Burn-In-Renderer (Zeilenumbrüche, Font-Metriken, Safe-Area,
        # Position, Wort-Highlight). Kein fakes GUI-Text.
        self.subtitle_live_preview = SubtitlePreviewCanvas()
        self.subtitle_live_preview.setMinimumHeight(130)
        subtitle_layout.addWidget(self.subtitle_live_preview, 7, 0, 1, 3)
        for control in (
            self.subtitle_font_combo, self.subtitle_style_combo,
            self.subtitle_animation_combo, self.subtitle_position_combo,
            self.subtitle_language_combo,
        ):
            control.currentIndexChanged.connect(self._update_subtitle_live_preview)
        self.subtitle_debug_check.toggled.connect(self._update_subtitle_live_preview)
        self.subtitle_preview_button = QPushButton("Open Larger Subtitle Preview")
        self.subtitle_preview_button.clicked.connect(self._preview_subtitle_style)
        subtitle_layout.addWidget(self.subtitle_preview_button, 8, 1)
        self.alignment_warning_check = QCheckBox("Continue After Alignment Warning (manual confirmation)")
        subtitle_layout.addWidget(self.alignment_warning_check, 9, 0, 1, 3)
        outer.addWidget(subtitle_group)

        format_group = QGroupBox("4 · Video Format & Transition")
        format_layout = QGridLayout(format_group)
        self.radio_16 = QRadioButton("16:9 · YouTube / Landscape")
        self.radio_9 = QRadioButton("9:16 · Shorts / Reels / TikTok")
        self.radio_16.toggled.connect(self._update_resolution_choices)
        format_layout.addWidget(self.radio_16, 0, 0)
        format_layout.addWidget(self.radio_9, 0, 1)
        format_layout.addWidget(QLabel("Resolution"), 1, 0)
        self.resolution_combo = QComboBox()
        self.resolution_combo.currentIndexChanged.connect(self._mark_preset_custom)
        format_layout.addWidget(self.resolution_combo, 1, 1)
        format_layout.addWidget(QLabel("Fit Mode"), 2, 0)
        self.fit_combo = QComboBox()
        self.fit_combo.addItem("Contain + Blurred Background (sicher)", "contain_blur")
        self.fit_combo.addItem("Crop / Fill (kann Bildränder abschneiden)", "crop_fill")
        format_layout.addWidget(self.fit_combo, 2, 1)
        outer.addWidget(format_group)

        effect_group = QGroupBox("4b · Transition & Background")
        effect_layout = QGridLayout(effect_group)
        effect_layout.addWidget(QLabel("Transition"), 0, 0)
        self.transition_combo = QComboBox()
        for key, label, description in TRANSITION_OPTIONS:
            self.transition_combo.addItem(label, key)
            self.transition_combo.setItemData(self.transition_combo.count() - 1, description, Qt.ToolTipRole)
        self.transition_combo.currentIndexChanged.connect(self._update_transition_description)
        effect_layout.addWidget(self.transition_combo, 0, 1)
        self.transition_description = QLabel()
        self.transition_description.setWordWrap(True)
        self.transition_description.setObjectName("subtitle")
        effect_layout.addWidget(self.transition_description, 1, 1, 1, 2)
        effect_layout.addWidget(QLabel("Duration"), 2, 0)
        self.transition_spin = QDoubleSpinBox()
        self.transition_spin.setRange(0.05, 5.0)
        self.transition_spin.setSingleStep(0.25)
        self.transition_spin.setDecimals(2)
        self.transition_spin.setSuffix(" sec")
        self.transition_spin.valueChanged.connect(self._update_pool_status)
        effect_layout.addWidget(self.transition_spin, 2, 1)

        self.blur_slider = QSlider(Qt.Horizontal)
        self.blur_slider.setRange(0, 50)
        self.blur_value = QLabel()
        self.blur_slider.valueChanged.connect(lambda value: self.blur_value.setText(str(value)))
        effect_layout.addWidget(QLabel("Background Blur"), 3, 0)
        effect_layout.addWidget(self.blur_slider, 3, 1)
        effect_layout.addWidget(self.blur_value, 3, 2)

        self.dark_slider = QSlider(Qt.Horizontal)
        self.dark_slider.setRange(0, 30)
        self.dark_value = QLabel()
        self.dark_slider.valueChanged.connect(lambda value: self.dark_value.setText(f"{value} %"))
        effect_layout.addWidget(QLabel("Background Darkness"), 4, 0)
        effect_layout.addWidget(self.dark_slider, 4, 1)
        effect_layout.addWidget(self.dark_value, 4, 2)

        self.zoom_slider = QSlider(Qt.Horizontal)
        self.zoom_slider.setRange(100, 120)
        self.zoom_value = QLabel()
        self.zoom_slider.valueChanged.connect(lambda value: self.zoom_value.setText(f"{value} %"))
        effect_layout.addWidget(QLabel("Background Zoom"), 5, 0)
        effect_layout.addWidget(self.zoom_slider, 5, 1)
        effect_layout.addWidget(self.zoom_value, 5, 2)
        self.normalize_check = QCheckBox("Audio sanft auf −16 LUFS normalisieren")
        effect_layout.addWidget(self.normalize_check, 6, 0, 1, 3)
        outer.addWidget(effect_group)

        preset_group = QGroupBox("4c · Output Preset & Quality (1.2.3)")
        preset_layout = QGridLayout(preset_group)
        self.output_preset_combo = QComboBox()
        self.output_preset_combo.addItem("YouTube Landscape (Standard)", "youtube_landscape")
        self.output_preset_combo.addItem("YouTube Shorts / Reels (9:16)", "youtube_vertical")
        self.output_preset_combo.addItem("Custom", "custom")
        self.output_preset_combo.currentIndexChanged.connect(self._output_preset_changed)
        self.quality_combo = QComboBox()
        for key in QUALITY_KEYS:
            entry = QUALITY_PRESETS[key]
            self.quality_combo.addItem(f"{entry['label']} – CRF {entry['crf']}, preset {entry['preset']}", key)
        self.quality_combo.addItem("Custom (Advanced Settings)", "custom")
        self.quality_combo.currentIndexChanged.connect(self._quality_changed)
        self.quality_description = QLabel()
        self.quality_description.setWordWrap(True)
        self.quality_description.setObjectName("subtitle")
        preset_layout.addWidget(QLabel("Output Preset"), 0, 0)
        preset_layout.addWidget(self.output_preset_combo, 0, 1)
        preset_layout.addWidget(QLabel("Quality"), 1, 0)
        preset_layout.addWidget(self.quality_combo, 1, 1)
        preset_layout.addWidget(self.quality_description, 2, 0, 1, 2)
        outer.addWidget(preset_group)

        self.advanced_toggle = QPushButton("▸ Advanced Settings")
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.toggled.connect(self._toggle_advanced)
        outer.addWidget(self.advanced_toggle)
        self.advanced_box = QGroupBox("Advanced Settings")
        advanced = QFormLayout(self.advanced_box)
        self.fps_combo = QComboBox()
        self.fps_combo.addItems(["Auto", "24", "25", "30", "50", "60"])
        self.fps_combo.currentIndexChanged.connect(self._mark_preset_custom)
        self.ease_combo = QComboBox()
        for key, label in EASE_OPTIONS:
            self.ease_combo.addItem(label, key)
        self.encoding_combo = QComboBox()
        self.encoding_combo.addItems(["Auto", "CPU", "NVIDIA NVENC", "Intel Quick Sync", "AMD AMF"])
        self.crf_spin = QSpinBox()
        self.crf_spin.setRange(14, 28)
        self.preset_combo = QComboBox()
        self.preset_combo.addItems(["fast", "medium", "slow"])
        self.output_name_edit = QLineEdit()
        self.output_name_edit.setPlaceholderText("leer = merged_16x9_Datum_Uhrzeit.mp4")
        self.duck_attack_spin = QSpinBox()
        self.duck_attack_spin.setRange(1, 2000)
        self.duck_attack_spin.setSuffix(" ms")
        self.duck_release_spin = QSpinBox()
        self.duck_release_spin.setRange(10, 9000)
        self.duck_release_spin.setSuffix(" ms")
        self.subtitle_model_combo = QComboBox()
        self.subtitle_model_combo.addItems(["tiny", "base", "small", "medium"])
        advanced.addRow("Target FPS", self.fps_combo)
        advanced.addRow("Transition Curve", self.ease_combo)
        advanced.addRow("Encoding", self.encoding_combo)
        advanced.addRow("CRF / Quality", self.crf_spin)
        advanced.addRow("CPU Preset", self.preset_combo)
        advanced.addRow("Ducking Attack", self.duck_attack_spin)
        advanced.addRow("Ducking Release", self.duck_release_spin)
        advanced.addRow("Local Alignment Model", self.subtitle_model_combo)
        advanced.addRow("Basic Output filename", self.output_name_edit)
        self.advanced_box.hide()
        outer.addWidget(self.advanced_box)

        watermark_group = QGroupBox("5 · Watermark")
        watermark_layout = QGridLayout(watermark_group)
        self.watermark_check = QCheckBox("Enable Watermark")
        self.watermark_edit = QLineEdit()
        watermark_button = QPushButton("Choose Image …")
        watermark_button.clicked.connect(
            lambda: self._browse_asset(self.watermark_edit, "image")
        )
        self.watermark_position_combo = QComboBox()
        for label, key in (("Top Left", "top_left"), ("Top Right", "top_right"),
                           ("Bottom Left", "bottom_left"), ("Bottom Right", "bottom_right")):
            self.watermark_position_combo.addItem(label, key)
        self.watermark_scope_combo = QComboBox()
        self.watermark_scope_combo.addItem("Both (Main + Outro)", "both")
        self.watermark_scope_combo.addItem("Main Video", "main")
        self.watermark_scope_combo.addItem("Outro", "outro")
        self.watermark_opacity_slider = QSlider(Qt.Horizontal)
        self.watermark_opacity_slider.setRange(0, 100)
        self.watermark_opacity_value = QLabel()
        self.watermark_opacity_slider.valueChanged.connect(
            lambda value: self.watermark_opacity_value.setText(f"{value} %")
        )
        self.watermark_size_spin = QSpinBox()
        self.watermark_size_spin.setRange(2, 35)
        self.watermark_size_spin.setSuffix(" % frame width")
        self.watermark_margin_spin = QSpinBox()
        self.watermark_margin_spin.setRange(0, 15)
        self.watermark_margin_spin.setSuffix(" %")
        watermark_layout.addWidget(self.watermark_check, 0, 0, 1, 3)
        watermark_layout.addWidget(QLabel("Image"), 1, 0)
        watermark_layout.addWidget(self.watermark_edit, 1, 1)
        watermark_layout.addWidget(watermark_button, 1, 2)
        watermark_layout.addWidget(QLabel("Position"), 2, 0)
        watermark_layout.addWidget(self.watermark_position_combo, 2, 1)
        watermark_layout.addWidget(QLabel("Apply To"), 3, 0)
        watermark_layout.addWidget(self.watermark_scope_combo, 3, 1)
        watermark_layout.addWidget(QLabel("Opacity"), 4, 0)
        watermark_layout.addWidget(self.watermark_opacity_slider, 4, 1)
        watermark_layout.addWidget(self.watermark_opacity_value, 4, 2)
        watermark_layout.addWidget(QLabel("Size"), 5, 0)
        watermark_layout.addWidget(self.watermark_size_spin, 5, 1)
        watermark_layout.addWidget(QLabel("Margin"), 6, 0)
        watermark_layout.addWidget(self.watermark_margin_spin, 6, 1)
        outer.addWidget(watermark_group)

        action_layout = QHBoxLayout()
        self.analyze_button = QPushButton("Analyze Inputs")
        self.preview_button = QPushButton("Preview Transition")
        self.merge_button = QPushButton("MERGE VIDEOS (BASIC)")
        self.main_button = QPushButton("CREATE MAIN VIDEO")
        self.complete_button = QPushButton("CREATE FINAL VIDEO – ONE CLICK")
        self.merge_button.setObjectName("mergeButton")
        self.main_button.setObjectName("mergeButton")
        self.complete_button.setObjectName("mergeButton")
        self.cancel_button = QPushButton("Abbrechen")
        self.cancel_button.setObjectName("cancelButton")
        self.cancel_button.setEnabled(False)
        self.analyze_button.clicked.connect(lambda: self._start("analyze"))
        self.preview_button.clicked.connect(lambda: self._start("preview"))
        self.merge_button.clicked.connect(lambda: self._start("merge"))
        self.main_button.clicked.connect(lambda: self._start("main"))
        self.complete_button.clicked.connect(lambda: self._start("complete"))
        self.cancel_button.clicked.connect(self._cancel)
        action_layout.addWidget(self.analyze_button)
        action_layout.addWidget(self.preview_button)
        action_layout.addWidget(self.merge_button)
        action_layout.addWidget(self.main_button)
        action_layout.addWidget(self.complete_button, stretch=1)
        action_layout.addWidget(self.cancel_button)
        outer.addLayout(action_layout)

        outro_group = QGroupBox("7 · Optional Stage 2 – Intro / Quote Card / Outro")
        outro_layout = QGridLayout(outro_group)
        self.intro_edit = QLineEdit()
        self.main_video_edit = QLineEdit()
        self.outro_edit = QLineEdit()
        intro_choose = QPushButton("Choose Intro …")
        main_choose = QPushButton("Choose MainVideo …")
        outro_choose = QPushButton("Choose Outro …")
        intro_choose.clicked.connect(lambda: self._browse_asset(self.intro_edit, "video"))
        main_choose.clicked.connect(lambda: self._browse_asset(self.main_video_edit, "video"))
        outro_choose.clicked.connect(lambda: self._browse_asset(self.outro_edit, "video"))
        self.intro_audio_combo = QComboBox()
        self.intro_audio_combo.addItem("Original (Standard)", "original")
        self.intro_audio_combo.addItem("Low", "low")
        self.intro_audio_combo.addItem("Mute", "mute")
        self.outro_audio_combo = QComboBox()
        self.outro_audio_combo.addItem("Original (Standard)", "original")
        self.outro_audio_combo.addItem("Low", "low")
        self.outro_audio_combo.addItem("Mute", "mute")
        self.outro_transition_check = QCheckBox("Use selected visual transition between sections")
        # 1.2.4 Quote Card: optionale, STILLE Zitatkarte zwischen Intro und
        # MainVideo (Intro → [Quote] → MainVideo → Outro). Sie erhält keine
        # Voiceover, keine Musik und keine Untertitel und nutzt das bestehende
        # Transition-System. Design: dunkler, editorialer Hintergrund mit
        # Vignette, Zitat als einziger Fokus leicht über der Mitte.
        self.quote_check = QCheckBox("Add Quote Card – stille Zitatkarte vor dem Main Video (1.2.4)")
        self.quote_text_edit = QPlainTextEdit()
        self.quote_text_edit.setPlaceholderText(
            "Zitat in Deutsch oder Englisch – Umlaute, Anführungszeichen und Punktzeichen sind erlaubt …"
        )
        self.quote_text_edit.setFixedHeight(72)
        self.quote_attribution_edit = QLineEdit()
        self.quote_attribution_edit.setPlaceholderText("Attribution (optional), z. B. – Marie Curie")
        self.quote_duration_combo = QComboBox()
        for value in (1.0, 1.5, 2.0, 2.5, 3.0):
            self.quote_duration_combo.addItem(f"{value:.1f} sec", value)
        self.quote_duration_combo.setCurrentIndex(2)  # 2.0 s = Standard
        self.quote_font_combo = QComboBox()
        for key, label in FONT_OPTIONS:
            self.quote_font_combo.addItem(label, key)
        self.quote_preview = QuotePreviewCanvas()
        self.quote_preview.setMinimumHeight(180)
        self.quote_check.toggled.connect(self._sync_quote_visibility)
        self.quote_text_edit.textChanged.connect(self._update_quote_preview)
        self.quote_attribution_edit.textChanged.connect(self._update_quote_preview)
        self.quote_duration_combo.currentIndexChanged.connect(self._update_quote_preview)
        self.quote_font_combo.currentIndexChanged.connect(self._update_quote_preview)
        self.final_button = QPushButton("CREATE FINAL VIDEO")
        self.final_button.setObjectName("mergeButton")
        self.final_button.clicked.connect(lambda: self._start("outro"))
        outro_layout.addWidget(QLabel("Intro Video (optional)"), 0, 0)
        outro_layout.addWidget(self.intro_edit, 0, 1)
        outro_layout.addWidget(intro_choose, 0, 2)
        outro_layout.addWidget(QLabel("Main Video"), 1, 0)
        outro_layout.addWidget(self.main_video_edit, 1, 1)
        outro_layout.addWidget(main_choose, 1, 2)
        outro_layout.addWidget(QLabel("Outro Video (optional)"), 2, 0)
        outro_layout.addWidget(self.outro_edit, 2, 1)
        outro_layout.addWidget(outro_choose, 2, 2)
        outro_layout.addWidget(QLabel("Intro Original Audio"), 3, 0)
        outro_layout.addWidget(self.intro_audio_combo, 3, 1)
        outro_layout.addWidget(QLabel("Outro Original Audio"), 4, 0)
        outro_layout.addWidget(self.outro_audio_combo, 4, 1)
        outro_layout.addWidget(self.outro_transition_check, 5, 0, 1, 2)
        outro_layout.addWidget(self.quote_check, 6, 0, 1, 3)
        outro_layout.addWidget(QLabel("Quote Text"), 7, 0)
        outro_layout.addWidget(self.quote_text_edit, 7, 1, 1, 2)
        outro_layout.addWidget(QLabel("Attribution"), 8, 0)
        outro_layout.addWidget(self.quote_attribution_edit, 8, 1, 1, 2)
        row = 9
        outro_layout.addWidget(QLabel("Quote Duration"), row, 0)
        outro_layout.addWidget(self.quote_duration_combo, row, 1)
        outro_layout.addWidget(QLabel("Quote Font"), row + 1, 0)
        outro_layout.addWidget(self.quote_font_combo, row + 1, 1)
        outro_layout.addWidget(QLabel("Quote Preview"), row + 2, 0)
        outro_layout.addWidget(self.quote_preview, row + 2, 1, 1, 2)
        outro_layout.addWidget(self.final_button, row + 3, 1)
        outer.addWidget(outro_group)

        summary_group = QGroupBox("Projekt-Reihenfolge · Videos – natürlich, manuell oder randomisiert (persistent)")
        summary_layout = QVBoxLayout(summary_group)
        summary_header = QHBoxLayout()
        self.summary_label = QLabel(
            "Noch nicht analysiert. Zeilen nach der Analyse ziehen; die sichtbare Nummerierung ist die Exportreihenfolge."
        )
        self.summary_label.setWordWrap(True)
        self.move_up_button = QPushButton("Nach oben")
        self.move_down_button = QPushButton("Nach unten")
        self.randomize_button = QPushButton("Randomize Order")
        self.reset_order_button = QPushButton("Reset to Default Order")
        self.move_up_button.clicked.connect(lambda: self._move_selected(-1))
        self.move_down_button.clicked.connect(lambda: self._move_selected(1))
        self.randomize_button.setToolTip(
            "Echte Fisher-Yates-Zufallspermutation der aktuellen aktiven Liste. Die neue Reihenfolge "
            "wird sofort aktiv und dauerhaft gespeichert."
        )
        self.randomize_button.clicked.connect(self._randomize_order)
        self.reset_order_button.setToolTip(
            "Natürliche numerische/alphabetische Standardreihenfolge wiederherstellen (1, 2, 3, 10)."
        )
        self.reset_order_button.clicked.connect(self._reset_project_order)
        summary_header.addWidget(self.summary_label, stretch=1)
        summary_header.addWidget(self.move_up_button)
        summary_header.addWidget(self.move_down_button)
        summary_header.addWidget(self.randomize_button)
        summary_header.addWidget(self.reset_order_button)
        summary_layout.addLayout(summary_header)
        # 1.2.4 Video-Pool Status: zeigt nach Analyse, VO-Änderung, Randomize
        # und manuellem Neusortieren, wie viele Clips tatsächlich benötigt und
        # selektiert sind (Required-Only-Verarbeitung; nicht genutzte Clips
        # werden nie decodiert oder gerendert).
        self.pool_status_label = QLabel("Video-Pool: noch keine Analyse.")
        self.pool_status_label.setObjectName("subtitle")
        self.pool_status_label.setWordWrap(True)
        summary_layout.addWidget(self.pool_status_label)
        self.files_table = ReorderTableWidget(0, 6)
        self.files_table.setHorizontalHeaderLabels(["#", "Datei", "Dauer", "Video", "FPS", "Audio"])
        self.files_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.files_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        for column in range(2, 6):
            self.files_table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.files_table.setMaximumHeight(240)
        self.files_table.row_move_requested.connect(self._move_row)
        summary_layout.addWidget(self.files_table)
        outer.addWidget(summary_group)

        progress_group = QGroupBox("Fortschritt")
        progress_layout = QGridLayout(progress_group)
        self.stage_label = QLabel("Bereit")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)
        self.current_label = QLabel("Current file: –")
        self.elapsed_label = QLabel("Elapsed: 00:00:00")
        self.remaining_label = QLabel("Estimated remaining: --:--:--")
        progress_layout.addWidget(self.stage_label, 0, 0, 1, 3)
        progress_layout.addWidget(self.progress_bar, 1, 0, 1, 3)
        progress_layout.addWidget(self.current_label, 2, 0)
        progress_layout.addWidget(self.elapsed_label, 2, 1)
        progress_layout.addWidget(self.remaining_label, 2, 2)
        outer.addWidget(progress_group)

        result_layout = QHBoxLayout()
        self.open_video_button = QPushButton("Open Video")
        self.open_folder_button = QPushButton("Open Output Folder")
        self.open_log_button = QPushButton("Open Log")
        self.open_video_button.clicked.connect(self._open_video)
        self.open_folder_button.clicked.connect(self._open_folder)
        self.open_log_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.log_path))))
        self.open_video_button.setEnabled(False)
        self.open_folder_button.setEnabled(False)
        result_layout.addWidget(self.open_video_button)
        result_layout.addWidget(self.open_folder_button)
        result_layout.addWidget(self.open_log_button)
        result_layout.addStretch()
        self.log_toggle = QPushButton("▸ Log anzeigen")
        self.log_toggle.setCheckable(True)
        self.log_toggle.toggled.connect(self._toggle_log)
        result_layout.addWidget(self.log_toggle)
        outer.addLayout(result_layout)
        self.log_edit = QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setMaximumBlockCount(3000)
        self.log_edit.setMinimumHeight(210)
        self.log_edit.hide()
        outer.addWidget(self.log_edit)

    def _load_settings(self) -> None:
        self._loading = True
        try:
            self._load_settings_inner()
        finally:
            self._loading = False

    def _load_settings_inner(self) -> None:
        self.input_edit.setText(str(self.root / "input"))
        self.output_edit.setText(str(self.root / "output"))
        self.radio_16.setChecked(self.saved.aspect != "9:16")
        self.radio_9.setChecked(self.saved.aspect == "9:16")
        self._update_resolution_choices()
        index = self.resolution_combo.findText(self.saved.resolution)
        self.resolution_combo.setCurrentIndex(max(0, index))
        index = self.fit_combo.findData(self.saved.fit_mode)
        self.fit_combo.setCurrentIndex(max(0, index))
        index = self.transition_combo.findData(self.saved.transition_type)
        self.transition_combo.setCurrentIndex(max(0, index))
        self._update_transition_description()
        index = self.ease_combo.findData(self.saved.transition_ease)
        self.ease_combo.setCurrentIndex(max(0, index))
        self.transition_spin.setValue(self.saved.transition_duration)
        self.blur_slider.setValue(self.saved.background_blur)
        self.dark_slider.setValue(self.saved.background_darkness)
        self.zoom_slider.setValue(self.saved.background_zoom)
        self.normalize_check.setChecked(self.saved.normalize_audio)
        for combo, value in ((self.fps_combo, self.saved.fps_choice), (self.encoding_combo, self.saved.encoding), (self.preset_combo, self.saved.preset)):
            index = combo.findText(value)
            combo.setCurrentIndex(max(0, index))
        self.crf_spin.setValue(self.saved.crf)
        self.output_name_edit.setText(self.saved.output_name)
        self.music_edit.setText(self.saved.music_path)
        self.main_video_edit.setText(self.saved.main_video_path)
        self.intro_edit.setText(self.saved.intro_path)
        self.outro_edit.setText(self.saved.outro_path)
        self.watermark_edit.setText(self.saved.watermark_path)
        # 1.2.3 quality/output presets. Presets are authoritative defaults;
        # "custom" keeps the explicit low-level fields.
        quality_index = self.quality_combo.findData(self.saved.quality_preset)
        self.quality_combo.setCurrentIndex(quality_index if quality_index >= 0 else 0)
        preset_index = self.output_preset_combo.findData(self.saved.output_preset)
        self.output_preset_combo.setCurrentIndex(preset_index if preset_index >= 0 else 0)
        self._update_quality_description()
        # Voiceover units: the ordered list is authoritative; migrate the
        # legacy single-file fields into the list transparently.
        voiceover_units = list(self.saved.voiceover_paths)
        if not voiceover_units and self.saved.voiceover_path.strip():
            voiceover_units = [self.saved.voiceover_path]
        script_units = list(self.saved.script_paths)
        if not script_units and self.saved.script_path.strip():
            script_units = [self.saved.script_path]
        self.voiceover_paths_list: list[str] = voiceover_units
        self.voiceover_scripts_list: list[str] = script_units
        self._render_voiceover_table()
        mode_index = self.script_mode_combo.findData(self.saved.script_mode)
        self.script_mode_combo.setCurrentIndex(mode_index if mode_index >= 0 else 0)
        for combo, value in (
            (self.original_audio_combo, self.saved.original_audio_mode),
            (self.intro_audio_combo, self.saved.intro_audio_mode),
            (self.outro_audio_combo, self.saved.outro_audio_mode),
            (self.short_video_combo, self.saved.short_video_mode),
            (self.subtitle_style_combo, self.saved.subtitle_style),
            (self.subtitle_animation_combo, self.saved.subtitle_animation),
            (self.subtitle_font_combo, self.saved.subtitle_font),
            (self.watermark_position_combo, self.saved.watermark_position),
            (self.watermark_scope_combo, self.saved.watermark_scope),
        ):
            index = combo.findData(value)
            combo.setCurrentIndex(index if index >= 0 else 0)
        self.voice_volume_slider.setValue(self.saved.voiceover_volume)
        preset_index = next(
            (i for i in range(self.music_preset_combo.count())
             if (self.music_preset_combo.itemData(i) or (None,))[0] == self.saved.music_preset),
            next(i for i in range(self.music_preset_combo.count())
                 if (self.music_preset_combo.itemData(i) or (None,))[0] == "custom"),
        )
        self.music_preset_combo.blockSignals(True)
        self.music_preset_combo.setCurrentIndex(preset_index)
        self.music_preset_combo.blockSignals(False)
        self.music_volume_slider.setValue(self.saved.music_volume)
        self.ducking_check.setChecked(self.saved.ducking_enabled)
        pause_index = self.pause_combo.findData(self.saved.final_pause)
        self.pause_combo.setCurrentIndex(pause_index if pause_index >= 0 else 1)
        self.subtitle_check.setChecked(self.saved.subtitle_enabled)
        self.alignment_warning_check.setChecked(self.saved.allow_alignment_warnings)
        self.subtitle_language_combo.setCurrentText(self.saved.subtitle_language)
        self.subtitle_position_combo.setCurrentText(self.saved.subtitle_position)
        self.subtitle_debug_check.setChecked(self.saved.subtitle_debug_overlay)
        self.watermark_check.setChecked(self.saved.watermark_enabled)
        self.watermark_opacity_slider.setValue(self.saved.watermark_opacity)
        self.watermark_size_spin.setValue(self.saved.watermark_size)
        self.watermark_margin_spin.setValue(self.saved.watermark_margin)
        self.outro_transition_check.setChecked(self.saved.outro_transition_enabled)
        self.duck_attack_spin.setValue(self.saved.ducking_attack_ms)
        self.duck_release_spin.setValue(self.saved.ducking_release_ms)
        self.subtitle_model_combo.setCurrentText(self.saved.subtitle_model)
        # 1.2.4 Quote Card.
        self.quote_check.setChecked(self.saved.quote_enabled)
        self.quote_text_edit.setPlainText(self.saved.quote_text)
        self.quote_attribution_edit.setText(self.saved.quote_attribution)
        duration_index = self.quote_duration_combo.findData(float(self.saved.quote_duration))
        self.quote_duration_combo.setCurrentIndex(duration_index if duration_index >= 0 else 2)
        font_index = self.quote_font_combo.findData(self.saved.quote_font)
        self.quote_font_combo.setCurrentIndex(font_index if font_index >= 0 else 0)
        self._sync_quote_visibility()
        self._sync_subtitle_request()
        self._update_subtitle_live_preview()
        self._update_quote_preview()
        self._update_pool_status()

    def _settings(self) -> ExportSettings:
        voiceover_units = list(getattr(self, "voiceover_paths_list", []))
        script_units = list(getattr(self, "voiceover_scripts_list", []))
        return ExportSettings(
            aspect="16:9" if self.radio_16.isChecked() else "9:16",
            resolution=self.resolution_combo.currentText(),
            fit_mode=str(self.fit_combo.currentData()),
            transition_type=str(self.transition_combo.currentData()),
            transition_ease=str(self.ease_combo.currentData()),
            transition_duration=self.transition_spin.value(),
            background_blur=self.blur_slider.value(),
            background_darkness=self.dark_slider.value(),
            background_zoom=self.zoom_slider.value(),
            normalize_audio=self.normalize_check.isChecked(),
            fps_choice=self.fps_combo.currentText(),
            encoding=self.encoding_combo.currentText(),
            crf=self.crf_spin.value(),
            preset=self.preset_combo.currentText(),
            quality_preset=str(self.quality_combo.currentData()),
            output_preset=str(self.output_preset_combo.currentData()),
            output_name=self.output_name_edit.text().strip(),
            voiceover_path=voiceover_units[0] if voiceover_units else "",
            script_path=script_units[0] if script_units else "",
            voiceover_paths=voiceover_units,
            script_paths=script_units,
            script_mode=str(self.script_mode_combo.currentData()),
            music_path=self.music_edit.text().strip(),
            main_video_path=self.main_video_edit.text().strip(),
            intro_path=self.intro_edit.text().strip(),
            outro_path=self.outro_edit.text().strip(),
            original_audio_mode=str(self.original_audio_combo.currentData()),
            intro_audio_mode=str(self.intro_audio_combo.currentData()),
            outro_audio_mode=str(self.outro_audio_combo.currentData()),
            voiceover_volume=self.voice_volume_slider.value(),
            music_volume=self.music_volume_slider.value(),
            music_preset=str((self.music_preset_combo.currentData() or ("custom", 0))[0]),
            ducking_enabled=self.ducking_check.isChecked(),
            ducking_attack_ms=self.duck_attack_spin.value(),
            ducking_release_ms=self.duck_release_spin.value(),
            final_pause=float(self.pause_combo.currentData()),
            short_video_mode=str(self.short_video_combo.currentData()),
            subtitle_enabled=self.subtitle_check.isChecked(),
            subtitle_language=self.subtitle_language_combo.currentText(),
            subtitle_style=str(self.subtitle_style_combo.currentData()),
            subtitle_animation=str(self.subtitle_animation_combo.currentData()),
            subtitle_font=str(self.subtitle_font_combo.currentData()),
            subtitle_position=self.subtitle_position_combo.currentText(),
            subtitle_debug_overlay=self.subtitle_debug_check.isChecked(),
            subtitle_model=self.subtitle_model_combo.currentText(),
            allow_alignment_warnings=self.alignment_warning_check.isChecked(),
            watermark_enabled=self.watermark_check.isChecked(),
            watermark_path=self.watermark_edit.text().strip(),
            watermark_position=str(self.watermark_position_combo.currentData()),
            watermark_opacity=self.watermark_opacity_slider.value(),
            watermark_size=self.watermark_size_spin.value(),
            watermark_margin=self.watermark_margin_spin.value(),
            watermark_scope=str(self.watermark_scope_combo.currentData()),
            outro_transition_enabled=self.outro_transition_check.isChecked(),
            # 1.2.4 Quote Card (optional, still, zwischen Intro und MainVideo).
            quote_enabled=self.quote_check.isChecked(),
            quote_text=self.quote_text_edit.toPlainText().strip(),
            quote_attribution=self.quote_attribution_edit.text().strip(),
            quote_duration=float(self.quote_duration_combo.currentData()),
            quote_font=str(self.quote_font_combo.currentData()),
        )

    def _update_transition_description(self) -> None:
        if hasattr(self, "transition_description"):
            self.transition_description.setText(
                transition_description(str(self.transition_combo.currentData()))
            )

    def _update_resolution_choices(self) -> None:
        if not hasattr(self, "resolution_combo"):
            return
        previous = self.resolution_combo.currentText()
        self.resolution_combo.clear()
        choices = ["Auto", "1280x720", "1920x1080", "2560x1440", "3840x2160"] if self.radio_16.isChecked() else ["Auto", "720x1280", "1080x1920", "2160x3840"]
        self.resolution_combo.addItems(choices)
        index = self.resolution_combo.findText(previous)
        self.resolution_combo.setCurrentIndex(index if index >= 0 else 0)
        if hasattr(self, "subtitle_style_combo"):
            default_key = "long_1" if self.radio_16.isChecked() else "short_1"
            style_index = self.subtitle_style_combo.findData(default_key)
            if style_index >= 0:
                self.subtitle_style_combo.setCurrentIndex(style_index)
            self.subtitle_position_combo.setCurrentText(
                "Bottom" if self.radio_16.isChecked() else "Medium-Low"
            )
            # 1.2.4 Default: Static Phrase (Long-Form / YouTube Landscape).
            # 9:16 Shorts behalten word_highlight. Alle 5 Animationen bleiben
            # jederzeit manuell wählbar.
            animation_key = "static_phrase" if self.radio_16.isChecked() else "word_highlight"
            animation_index = self.subtitle_animation_combo.findData(animation_key)
            if animation_index >= 0:
                self.subtitle_animation_combo.setCurrentIndex(animation_index)
            self._update_subtitle_live_preview()
        self._mark_preset_custom()
        self._update_quote_preview()

    def _toggle_advanced(self, checked: bool) -> None:
        self.advanced_box.setVisible(checked)
        self.advanced_toggle.setText(("▾" if checked else "▸") + " Advanced Settings")

    def _toggle_log(self, checked: bool) -> None:
        self.log_edit.setVisible(checked)
        self.log_toggle.setText(("▾" if checked else "▸") + " Log anzeigen")

    def _clear_stale_analysis(self, text: str) -> None:
        if not self.current_media:
            return
        try:
            folder = Path(text.strip()).expanduser().resolve()
        except OSError:
            folder = None
        if folder is None or any(item.path.expanduser().resolve().parent != folder for item in self.current_media):
            self.current_media = []
            self.files_table.setRowCount(0)
            self.summary_label.setText("Input Folder geändert – bitte Analyze Inputs ausführen.")

    def _browse_asset(self, edit: QLineEdit, role: str) -> None:
        filters = {
            "audio": "Audio (*.wav *.mp3 *.m4a *.aac *.flac *.ogg *.opus);;All files (*)",
            "script": "Text Script (*.txt *.text *.md);;All files (*)",
            "image": "Images (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff);;All files (*)",
            "video": "Videos (*.mp4 *.mov *.mkv *.m4v *.avi *.webm);;All files (*)",
        }
        selected, _ = QFileDialog.getOpenFileName(
            self, "Choose " + role.title(), edit.text() or str(self.root), filters.get(role, "All files (*)")
        )
        if selected:
            edit.setText(selected)

    def _sync_subtitle_request(self) -> None:
        # Voiceover + Script is the canonical subtitle workflow. Auto-enable
        # the option visibly; MainProjectEngine enforces the same rule even for
        # CLI/persisted projects so this cannot become a GUI-only guarantee.
        if not hasattr(self, "subtitle_check"):
            return
        units = list(getattr(self, "voiceover_paths_list", []))
        scripts = list(getattr(self, "voiceover_scripts_list", []))
        mode = str(self.script_mode_combo.currentData()) if hasattr(self, "script_mode_combo") else "single"
        have_scripts = (bool(scripts) and bool(scripts[0])) if mode == "single" else bool(units) and len(scripts) >= len(units) and all(scripts)
        if units and have_scripts:
            if not self.subtitle_check.isChecked():
                self.subtitle_check.setChecked(True)
                self._append_log(
                    "Untertitel automatisch aktiviert: Voiceover + Script erzeugen SRT, VTT und Burn-In."
                )

    def _music_preset_changed(self) -> None:
        data = self.music_preset_combo.currentData()
        if data and data[1] >= 0:
            self.music_volume_slider.blockSignals(True)
            self.music_volume_slider.setValue(int(data[1]))
            self.music_volume_slider.blockSignals(False)
            self.music_volume_value.setText(f"{int(data[1])} %")

    def _music_volume_changed(self, value: int) -> None:
        self.music_volume_value.setText(f"{value} %")
        data = self.music_preset_combo.currentData()
        if data and data[1] >= 0 and int(data[1]) != value:
            custom = next(
                (i for i in range(self.music_preset_combo.count())
                 if (self.music_preset_combo.itemData(i) or (None,))[0] == "custom"), 0
            )
            self.music_preset_combo.blockSignals(True)
            self.music_preset_combo.setCurrentIndex(custom)
            self.music_preset_combo.blockSignals(False)

    def _update_subtitle_live_preview(self, *_args) -> None:
        """Render the live caption preview with the REAL renderer logic.

        1.2.4: Preview ≈ Final Render. The canvas reuses the exact line-
        breaking, font metrics, safe-area and position calculations of the
        burned-in renderer (subtitles.preview_cue), so what the user sees is
        what the export produces. No FFmpeg render, instant on every change.
        """
        if not hasattr(self, "subtitle_live_preview") or not hasattr(self.subtitle_live_preview, "set_state"):
            return
        language = self.subtitle_language_combo.currentText()
        sample = sample_subtitle_text(language)
        if self.subtitle_debug_check.isChecked():
            sample += " [DEBUG Overlay aktiv]"
        frame = (1920, 1080) if self.radio_16.isChecked() else (1080, 1920)
        self.subtitle_live_preview.set_state(
            font_key=str(self.subtitle_font_combo.currentData()),
            preset_key=str(self.subtitle_style_combo.currentData()),
            position=self.subtitle_position_combo.currentText(),
            animation=str(self.subtitle_animation_combo.currentData()),
            text=sample,
            width=frame[0],
            height=frame[1],
        )

    # ------------------------------------------------------------------ #
    # Quote Card (1.2.4) – stille Zitatkarte zwischen Intro und MainVideo
    # ------------------------------------------------------------------ #
    def _quote_dimensions(self) -> tuple[int, int]:
        return (1920, 1080) if self.radio_16.isChecked() else (1080, 1920)

    def _update_quote_preview(self, *_args) -> None:
        """Live-Preview der Quote-Karte mit exakt der Renderer-Layoutlogik.

        preview_cue() und layout_quote() teilen sich Zeilenumbrüche,
        Font-Metriken, Safe-Area und Vertikal-Position – daher entspricht
        die GUI-Vorschau dem Final Render (1920×1080 bzw. 1080×1920 Referenz).
        """
        if not hasattr(self, "quote_preview") or not hasattr(self.quote_preview, "set_state"):
            return
        width, height = self._quote_dimensions()
        if not self.quote_check.isChecked():
            self.quote_preview.set_state("", "", str(self.quote_font_combo.currentData()), width, height)
            return
        self.quote_preview.set_state(
            text=self.quote_text_edit.toPlainText().strip(),
            attribution=self.quote_attribution_edit.text().strip(),
            font_key=str(self.quote_font_combo.currentData()),
            width=width,
            height=height,
        )

    def _sync_quote_visibility(self, *_args) -> None:
        enabled = self.quote_check.isChecked()
        for widget in (
            self.quote_text_edit, self.quote_attribution_edit,
            self.quote_duration_combo, self.quote_font_combo,
        ):
            widget.setEnabled(enabled)
        self._update_quote_preview()

    def _preview_subtitle_style(self) -> None:
        from ..subtitle_presets import get_preset
        preset = get_preset(str(self.subtitle_style_combo.currentData()))
        dialog = QDialog(self)
        dialog.setWindowTitle("Subtitle Style Preview")
        dialog.resize(720, 405)
        layout = QVBoxLayout(dialog)
        canvas = QWidget()
        canvas.setStyleSheet("background:#172033; border:1px solid #3b4a68;")
        canvas_layout = QVBoxLayout(canvas)
        sample = QLabel()
        sample.setAlignment(Qt.AlignCenter)
        sample.setWordWrap(True)
        sample.setTextFormat(Qt.RichText)
        accent = "#ffd43b"
        sample.setText(
            "Das ist ein <span style='color:%s'>präzise synchronisiertes</span> Beispiel" % accent
        )
        size = 32 if preset.collection == "long" else 40
        box = "background:rgba(0,0,0,150); padding:10px;" if preset.box else ""
        selected_font = resolve_font(str(self.subtitle_font_combo.currentData()))
        sample.setStyleSheet(
            f"color:#f7f7f7; font-size:{size}px; font-family:'{selected_font.family}'; "
            f"font-weight:{700 if preset.bold or selected_font.weight == 'bold' else 500}; {box}"
        )
        position = self.subtitle_position_combo.currentText()
        if position in {"Bottom", "Medium-Low"}:
            canvas_layout.addStretch(3 if position == "Bottom" else 2)
            canvas_layout.addWidget(sample)
            canvas_layout.addStretch(1 if position == "Medium-Low" else 0)
        elif position == "Top":
            canvas_layout.addWidget(sample)
            canvas_layout.addStretch(3)
        else:
            canvas_layout.addStretch()
            canvas_layout.addWidget(sample)
            canvas_layout.addStretch()
        layout.addWidget(canvas)
        note = QLabel(
            f"{selected_font.family} · {self.subtitle_animation_combo.currentText()} · {position}. "
            "Beim Rendern steuern ausschließlich echte Voiceover-Wortzeitstempel die Anzeige; "
            "die vollständige Phrase reserviert stabile Geometrie."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        close = QPushButton("Schließen")
        close.clicked.connect(dialog.accept)
        layout.addWidget(close, alignment=Qt.AlignRight)
        dialog.exec()

    def _browse_input(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Input Folder", self.input_edit.text())
        if selected:
            self.input_edit.setText(selected)

    def _reset_project_order(self) -> None:
        if self.busy:
            return
        folder = Path(self.input_edit.text().strip())
        if not folder.is_dir():
            QMessageBox.warning(self, "Input fehlt", "Bitte zuerst einen gültigen Input Folder auswählen.")
            return
        if not self.current_media:
            QMessageBox.information(
                self, "Reihenfolge", "Bitte zuerst Analyze Inputs ausführen, dann kann die Standardreihenfolge wiederhergestellt werden."
            )
            return
        paths = self.order_store.reset_to_default(folder, [item.path for item in self.current_media])
        by_path = {item.path.expanduser().resolve(): item for item in self.current_media}
        self.current_media = [by_path[path.expanduser().resolve()] for path in paths]
        self._render_media_table()
        self.summary_label.setText(
            "Natürliche Standardreihenfolge wiederhergestellt. Die nummerierte Liste ist die aktive Exportreihenfolge."
        )
        self._append_log("Aktive Reihenfolge auf die natürliche Standardreihenfolge zurückgesetzt: " +
                         " → ".join(path.name for path in paths))
        self._update_pool_status()

    def _randomize_order(self) -> None:
        if self.busy:
            return
        if not self.current_media:
            QMessageBox.information(
                self, "Reihenfolge", "Bitte zuerst Analyze Inputs ausführen, dann kann die Reihenfolge randomisiert werden."
            )
            return
        if len(self.current_media) < 2:
            QMessageBox.information(self, "Reihenfolge", "Mindestens zwei Clips sind für Randomize Order nötig.")
            return
        folder = Path(self.input_edit.text().strip())
        if not folder.is_dir():
            QMessageBox.warning(self, "Input fehlt", "Bitte zuerst einen gültigen Input Folder auswählen.")
            return
        paths = self.order_store.set_randomized_order(folder, [item.path for item in self.current_media])
        by_path = {item.path.expanduser().resolve(): item for item in self.current_media}
        self.current_media = [by_path[path.expanduser().resolve()] for path in paths]
        self._render_media_table()
        self.summary_label.setText(
            "Randomisiert (Fisher-Yates). Die nummerierte Liste ist die aktive Exportreihenfolge."
        )
        self._append_log("Randomize Order: echte Zufallspermutation der aktiven Liste – " +
                         " → ".join(path.name for path in paths))
        self._update_pool_status()

    # ------------------------------------------------------------------ #
    # Voiceover order list (1.2.3)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _auto_match_script(audio_path: Path) -> Path | None:
        candidate = audio_path.with_suffix(".txt")
        if candidate.is_file():
            return candidate
        return None

    def _render_voiceover_table(self, selected_row: int | None = None) -> None:
        units = list(getattr(self, "voiceover_paths_list", []))
        scripts = list(getattr(self, "voiceover_scripts_list", []))
        self.voiceover_table.setRowCount(len(units))
        for row, path_text in enumerate(units):
            name = Path(path_text).name
            script_text = scripts[row] if row < len(scripts) else ""
            values = [str(row + 1), name, script_text or "— no script —"]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setTextAlignment(Qt.AlignCenter)
                self.voiceover_table.setItem(row, column, item)
        if selected_row is not None and units:
            self.voiceover_table.selectRow(max(0, min(selected_row, len(units) - 1)))
        self._sync_subtitle_request()

    def _add_voiceovers(self) -> None:
        if self.busy:
            return
        selected, _ = QFileDialog.getOpenFileNames(
            self, "Choose Voiceover Audio Files", str(self.root),
            "Audio (*.wav *.mp3 *.m4a *.aac *.flac *.ogg *.opus);;All files (*)",
        )
        if not selected:
            return
        units = list(getattr(self, "voiceover_paths_list", []))
        scripts = list(getattr(self, "voiceover_scripts_list", []))
        # The voiceover list has a natural numeric/alphabetical default order:
        # newly added units are inserted in natural order (1, 2, 3, 10), while
        # already-present units keep their current manual positions.
        additions: list[tuple[str, str]] = []
        for path_text in selected:
            path = Path(path_text).expanduser().resolve()
            match = self._auto_match_script(path)
            additions.append((str(path), str(match) if match else ""))
        additions.sort(key=lambda pair: natural_sort_key(Path(pair[0]).name))
        for unit_path, script_path in additions:
            units.append(unit_path)
            scripts.append(script_path)
        self.voiceover_paths_list = units
        self.voiceover_scripts_list = scripts
        self._render_voiceover_table()
        self._append_log(f"Voiceover-Einheiten: {len(units)} (natürliche Standardreihenfolge beim nächsten Reset)")
        self._save_project()
        self._update_pool_status()

    def _remove_voiceover(self) -> None:
        if self.busy:
            return
        row = self.voiceover_table.currentRow()
        units = list(getattr(self, "voiceover_paths_list", []))
        scripts = list(getattr(self, "voiceover_scripts_list", []))
        if row < 0 or row >= len(units):
            return
        units.pop(row)
        scripts.pop(row)
        self.voiceover_paths_list = units
        self.voiceover_scripts_list = scripts
        self._render_voiceover_table()
        self._save_project()
        self._update_pool_status()

    def _choose_voiceover_script(self) -> None:
        if self.busy:
            return
        row = self.voiceover_table.currentRow()
        units = list(getattr(self, "voiceover_paths_list", []))
        scripts = list(getattr(self, "voiceover_scripts_list", []))
        if row < 0 or row >= len(units):
            QMessageBox.information(self, "Script", "Bitte zuerst eine Voiceover-Zeile auswählen.")
            return
        selected, _ = QFileDialog.getOpenFileName(
            self, "Choose Script for " + Path(units[row]).name, str(self.root),
            "Text Script (*.txt *.text *.md);;All files (*)",
        )
        if selected:
            while len(scripts) <= row:
                scripts.append("")
            scripts[row] = selected
            self.voiceover_scripts_list = scripts
            self._render_voiceover_table(row)
            self._save_project()

    def _move_voiceover_selected(self, delta: int) -> None:
        row = self.voiceover_table.currentRow()
        if row >= 0:
            self._move_voiceover_row(row, row + delta)

    def _move_voiceover_selected_to(self, position: int) -> None:
        row = self.voiceover_table.currentRow()
        if row < 0:
            return
        target = 0 if position < 0 else len(self.voiceover_paths_list) - 1
        self._move_voiceover_row(row, target)

    def _move_voiceover_row(self, source: int, target: int) -> None:
        if self.busy:
            return
        units = list(getattr(self, "voiceover_paths_list", []))
        scripts = list(getattr(self, "voiceover_scripts_list", []))
        if not (0 <= source < len(units)):
            return
        target = max(0, min(target, len(units) - 1))
        if source == target:
            return
        unit = units.pop(source)
        script = scripts.pop(source) if source < len(scripts) else ""
        units.insert(target, unit)
        scripts.insert(target, script)
        self.voiceover_paths_list = units
        self.voiceover_scripts_list = scripts
        self._render_voiceover_table(target)
        self._append_log("Voiceover-Reihenfolge geändert: " + " → ".join(Path(path).name for path in units))
        self._save_project()
        self._update_pool_status()

    def _reset_voiceover_order(self) -> None:
        if self.busy:
            return
        units = list(getattr(self, "voiceover_paths_list", []))
        scripts = list(getattr(self, "voiceover_scripts_list", []))
        if not units:
            return
        ordered_names = natural_order([Path(path).name for path in units])
        by_name = {Path(path).name: (path, script) for path, script in zip(units, scripts)}
        units = [by_name[name][0] for name in ordered_names]
        scripts = [by_name[name][1] for name in ordered_names]
        self.voiceover_paths_list = units
        self.voiceover_scripts_list = scripts
        self._render_voiceover_table()
        self._append_log("Voiceover-Reihenfolge auf natürliche Standardreihenfolge zurückgesetzt.")
        self._save_project()

    def _save_project(self) -> None:
        """Persist the complete current project state (order + settings)."""
        try:
            self.store.save(self._settings())
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Output preset + quality (1.2.3)
    # ------------------------------------------------------------------ #
    def _update_quality_description(self) -> None:
        if not hasattr(self, "quality_description"):
            return
        key = str(self.quality_combo.currentData())
        if key in QUALITY_PRESETS:
            self.quality_description.setText(str(QUALITY_PRESETS[key]["description"]))
        else:
            self.quality_description.setText("Explicit CRF/preset from Advanced Settings are used.")

    def _quality_changed(self) -> None:
        self._update_quality_description()
        if getattr(self, "_loading", False):
            return
        key = str(self.quality_combo.currentData())
        if key in QUALITY_PRESETS:
            entry = QUALITY_PRESETS[key]
            self.crf_spin.blockSignals(True)
            self.preset_combo.blockSignals(True)
            self.crf_spin.setValue(int(entry["crf"]))
            self.preset_combo.setCurrentText(str(entry["preset"]))
            self.crf_spin.blockSignals(False)
            self.preset_combo.blockSignals(False)
        if not getattr(self, "_applying_preset", False):
            self.output_preset_combo.setCurrentIndex(self.output_preset_combo.findData("custom"))
        self._append_log(f"Qualität: {quality_label(key)}")

    def _output_preset_changed(self) -> None:
        if getattr(self, "_loading", False):
            return
        key = str(self.output_preset_combo.currentData())
        if key == "custom":
            return
        self._applying_preset = True
        try:
            if key == "youtube_vertical":
                self.radio_9.setChecked(True)
                self.radio_16.setChecked(False)
            else:
                self.radio_16.setChecked(True)
                self.radio_9.setChecked(False)
            self._update_resolution_choices()
            self.resolution_combo.setCurrentIndex(0)  # Auto / highest appropriate source
            self.fps_combo.setCurrentIndex(0)  # Auto / source
            self.quality_combo.setCurrentIndex(self.quality_combo.findData("maximum"))
            self._quality_changed()
            self._append_log("Output Preset: YouTube Landscape – 16:9, Auto-Auflösung, Maximum Quality, Source FPS")
        finally:
            self._applying_preset = False

    def _mark_preset_custom(self, *_args) -> None:
        if getattr(self, "_loading", False) or getattr(self, "_applying_preset", False):
            return
        self.output_preset_combo.setCurrentIndex(self.output_preset_combo.findData("custom"))

    # ------------------------------------------------------------------ #
    # Video Pool (1.2.4) – Required-Only-Verarbeitung, kein Pre-Render
    # ------------------------------------------------------------------ #
    def _vo_target_duration(self) -> float:
        """Zieldauer für den Video-Pool: Summe der Voiceover-Dauern + Pause.

        Gleiche Formel wie MainProjectEngine (voice_total + max(0, pause)).
        probe_audio() ist selbst-cachend (ffprobe, Pfad, Größe, mtime),
        daher kostet ein Status-Update keine neue Analyse.
        """
        units = list(getattr(self, "voiceover_paths_list", []))
        if not units:
            return 0.0
        try:
            _, ffprobe = locate_ffmpeg()
        except Exception:
            return 0.0
        total = 0.0
        for path_text in units:
            path = Path(path_text).expanduser()
            if not path.is_file():
                continue
            try:
                total += probe_audio(ffprobe, path).duration
            except Exception:
                continue
        try:
            return total + max(0.0, float(self.pause_combo.currentData()))
        except Exception:
            return total

    def _update_pool_status(self, *_args) -> None:
        """Video-Pool-Status: Videos / Required / Selected / Not Used / Ziel.

        Rechnet ausschließlich mit den bereits analysierten Metadaten
        (leichtgewichtiges ffprobe-Discovery) – ändert niemals die Auswahl
        und erzwingt keine Neu-Analyse.
        """
        if not hasattr(self, "pool_status_label"):
            return
        if not self.current_media:
            self.pool_status_label.setText(
                "Video-Pool: keine Dateien analysiert – 'Analyze Inputs' ausführen "
                "(nur leichtgewichtiges ffprobe-Metadatendiscovery)."
            )
            return
        settings = self._settings()
        fps_values = [item.fps for item in self.current_media if item.fps and item.fps > 0]
        fps = round(sum(fps_values) / len(fps_values)) if fps_values else 30
        status = compute_pool_status(
            self.current_media,
            self._vo_target_duration(),
            float(settings.transition_duration),
            max(1.0, float(fps)),
            str(settings.short_video_mode),
        )
        self.pool_status_label.setText(status.summary_line)

    def _move_selected(self, delta: int) -> None:
        row = self.files_table.currentRow()
        if row >= 0:
            self._move_row(row, row + delta)

    def _move_row(self, source: int, target: int) -> None:
        if self.busy or not (0 <= source < len(self.current_media)):
            return
        target = max(0, min(target, len(self.current_media) - 1))
        if source == target:
            return
        item = self.current_media.pop(source)
        self.current_media.insert(target, item)
        self._persist_current_order()
        self._render_media_table(target)
        self._update_pool_status()

    def _persist_current_order(self) -> None:
        folder = Path(self.input_edit.text().strip())
        if folder.is_dir() and self.current_media:
            self.order_store.set_active_order(folder, [item.path for item in self.current_media])
            self._append_log("Manuelle Exportreihenfolge gespeichert: " +
                             " → ".join(item.path.name for item in self.current_media))

    def _render_media_table(self, selected_row: int | None = None) -> None:
        self.files_table.setRowCount(len(self.current_media))
        for row, item in enumerate(self.current_media):
            audio = (
                f"{item.audio.codec or 'ja'} {item.audio.sample_rate or ''} Hz"
                if item.audio.present else "kein Audio → Stille"
            )
            values = [
                str(row + 1), item.path.name, f"{item.duration:.2f} s",
                f"{item.effective_width}x{item.effective_height} ({item.video_codec})",
                f"{item.fps:.3f}", audio,
            ]
            for column, value in enumerate(values):
                table_item = QTableWidgetItem(value)
                if column == 0:
                    table_item.setTextAlignment(Qt.AlignCenter)
                self.files_table.setItem(row, column, table_item)
        if selected_row is not None and self.current_media:
            self.files_table.selectRow(max(0, min(selected_row, len(self.current_media) - 1)))

    def _browse_output(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Output Folder", self.output_edit.text())
        if selected:
            self.output_edit.setText(selected)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        urls = event.mimeData().urls()
        if urls:
            path = Path(urls[0].toLocalFile())
            self.input_edit.setText(str(path if path.is_dir() else path.parent))
            event.acceptProposedAction()

    def _start(self, mode: str) -> None:
        if self.busy:
            return
        input_folder = Path(self.input_edit.text().strip())
        output_folder = Path(self.output_edit.text().strip())
        if mode != "outro" and not input_folder.is_dir():
            QMessageBox.warning(self, "Input fehlt", "Bitte einen gültigen Input Folder auswählen.")
            return
        try:
            output_folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.critical(self, "Output nicht beschreibbar", str(exc))
            return
        settings = self._settings()
        settings.workflow_stage = "main" if mode in {"main", "complete"} else ("outro" if mode == "outro" else "basic")
        if mode in {"main", "complete"}:
            has_units = bool(settings.voiceover_paths)
            if settings.subtitle_enabled and not has_units:
                QMessageBox.warning(
                    self, "Subtitle Inputs fehlen",
                    "Burned-In Subtitles benötigen mindestens eine Voiceover-Datei und ein Script."
                )
                return
            if settings.subtitle_enabled:
                if settings.script_mode == "matched" and len(settings.script_paths) < len(settings.voiceover_paths):
                    QMessageBox.warning(
                        self, "Script fehlt",
                        "Missing script for voiceover:\n" + settings.voiceover_paths[len(settings.script_paths)]
                    )
                    return
                if settings.script_mode == "single" and not settings.script_paths:
                    QMessageBox.warning(
                        self, "Script fehlt",
                        "Single Global Script Mode benötigt eine Textdatei für die komplette Timeline."
                    )
                    return
            if settings.watermark_enabled and not settings.watermark_path:
                QMessageBox.warning(self, "Watermark fehlt", "Bitte ein Watermark-Bild wählen oder Watermark deaktivieren.")
                return
        # 1.2.4: Eine Quote-Karte mit Text ist ein gültiger Stage-2-Grund,
        # auch ohne Intro UND ohne Outro (gleiche Regel wie MainProjectEngine).
        quote_active = bool(settings.quote_enabled and (settings.quote_text or "").strip())
        if mode in {"complete", "outro"} and settings.quote_enabled and not quote_active:
            QMessageBox.warning(
                self, "Quote-Text fehlt",
                "Die Quote-Karte ist aktiv, aber der Quote-Text ist leer. Text eingeben oder Karte deaktivieren.",
            )
            return
        if mode == "complete" and not Path(settings.intro_path).is_file() and not Path(settings.outro_path).is_file() and not quote_active:
            QMessageBox.warning(
                self, "Intro/Outro/Quote fehlen",
                "One-Click benötigt mindestens ein gültiges Intro- oder Outro-Video oder eine aktive Quote-Karte mit Text.",
            )
            return
        if mode == "outro":
            if not Path(settings.main_video_path).is_file():
                QMessageBox.warning(
                    self, "Stage 2 Inputs fehlen", "Bitte ein gültiges MainVideo auswählen."
                )
                return
            if not Path(settings.intro_path).is_file() and not Path(settings.outro_path).is_file() and not quote_active:
                QMessageBox.warning(
                    self, "Stage 2 Inputs fehlen",
                    "Bitte mindestens ein Intro- oder Outro-Video auswählen oder eine aktive Quote-Karte mit Text hinterlegen.",
                )
                return
        self.store.save(settings)
        self.active_mode = mode
        self.busy = True
        self._set_busy(True)
        self.progress_bar.setValue(0)
        self.stage_label.setText("Dateien werden analysiert …")
        self.last_output = None
        self.open_video_button.setEnabled(False)
        self.open_folder_button.setEnabled(False)
        self._append_log("=" * 72)
        self._append_log(f"Auftrag gestartet: {mode}")
        self.thread = QThread(self)
        self.worker = ProcessingWorker(mode, input_folder, output_folder, settings)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.log.connect(self._append_log)
        self.worker.progress.connect(self._on_progress)
        self.worker.analysis_ready.connect(self._on_analysis)
        self.worker.finished.connect(self._on_finished)
        self.worker.failed.connect(self._on_failed)
        self.worker.cancelled.connect(self._on_cancelled)
        self.worker.finished.connect(self.thread.quit)
        self.worker.failed.connect(self.thread.quit)
        self.worker.cancelled.connect(self.thread.quit)
        self.thread.finished.connect(self._thread_finished)
        self.thread.start()

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.analyze_button.setEnabled(not busy)
        self.preview_button.setEnabled(not busy)
        self.merge_button.setEnabled(not busy)
        self.main_button.setEnabled(not busy)
        self.complete_button.setEnabled(not busy)
        self.final_button.setEnabled(not busy)
        self.cancel_button.setEnabled(busy)
        self.files_table.setEnabled(not busy)
        self.files_table.setDragEnabled(not busy)
        self.move_up_button.setEnabled(not busy)
        self.move_down_button.setEnabled(not busy)
        self.randomize_button.setEnabled(not busy)
        self.reset_order_button.setEnabled(not busy)
        self.voiceover_table.setEnabled(not busy)
        self.voiceover_table.setDragEnabled(not busy)
        for button in (
            self.voiceover_add_button, self.voiceover_remove_button, self.voiceover_script_button,
            self.voiceover_up_button, self.voiceover_down_button, self.voiceover_top_button,
            self.voiceover_bottom_button, self.voiceover_reset_button,
        ):
            button.setEnabled(not busy)
        for widget in (
            self.quote_check, self.quote_text_edit, self.quote_attribution_edit,
            self.quote_duration_combo, self.quote_font_combo, self.quote_preview,
        ):
            widget.setEnabled(not busy)

    def _cancel(self) -> None:
        if self.worker:
            self.stage_label.setText("Abbruch wird ausgeführt …")
            self.worker.cancel()

    def _on_analysis(self, media, resolved) -> None:
        # The worker emits media in the exact persisted active order captured at
        # job start. Keep that sequence; never sort it in the GUI.
        self.current_media = list(media)
        self._render_media_table()
        self.summary_label.setText(
            f"{len(media)} Clips · nummerierte aktive Exportreihenfolge · "
            f"Zieldauer ca. {_format_time(resolved.expected_duration)} · "
            f"{resolved.resolution_text} · {resolved.fps:g} fps · {resolved.encoder_label}"
        )
        self._update_pool_status()

    def _on_progress(self, event: ProgressEvent) -> None:
        self.progress_bar.setValue(int(event.percent * 10))
        self.stage_label.setText(event.stage)
        self.current_label.setText("Current file: " + (event.current_file or "–"))
        self.elapsed_label.setText("Elapsed: " + _format_time(event.elapsed))
        self.remaining_label.setText("Estimated remaining: " + _format_time(event.remaining))

    def _on_finished(self, output_text: str, report) -> None:
        self.busy = False
        self._set_busy(False)
        if output_text:
            self.last_output = Path(output_text)
            self.progress_bar.setValue(1000)
            self.stage_label.setText("Export completed successfully.")
            self.open_video_button.setEnabled(True)
            self.open_folder_button.setEnabled(True)
            details = output_text
            if self.active_mode == "main":
                self.main_video_edit.setText(output_text)
                saved = self._settings()
                saved.main_video_path = output_text
                self.store.save(saved)
                if getattr(report, "srt", None):
                    details += f"\n{report.srt}\n{report.vtt}"
                    if getattr(report, "canonical_timeline", None):
                        details += f"\n{report.canonical_timeline}"
                    frames = getattr(report, "verification_frames", [])
                    if frames:
                        details += "\nVisual verification: " + ", ".join(path.name for path in frames)
            elif self.active_mode == "complete":
                main = report.main
                self.main_video_edit.setText(str(main.video))
                saved = self._settings()
                saved.main_video_path = str(main.video)
                self.store.save(saved)
                details += f"\nActual MainVideo handoff: {main.video}"
                if main.srt:
                    details += f"\n{main.srt}\n{main.vtt}\n{main.canonical_timeline}"
            QMessageBox.information(self, "VideoMerger", f"Export completed successfully.\n\n{details}")
        else:
            self.stage_label.setText("Analyse abgeschlossen – bereit zum Export.")

    def _on_failed(self, message: str) -> None:
        self.busy = False
        self._set_busy(False)
        subtitle_failure = message.startswith("SUBTITLE GENERATION FAILED")
        self.stage_label.setText(
            "SUBTITLE GENERATION FAILED – Details im Log"
            if subtitle_failure else "Fehler – Details im Log"
        )
        self._append_log(("SUBTITLE GENERATION FAILED: " if subtitle_failure else "FEHLER: ") + message)
        if not self.log_edit.isVisible():
            self.log_toggle.setChecked(True)
        QMessageBox.critical(
            self,
            "SUBTITLE GENERATION FAILED" if subtitle_failure else "VideoMerger – Fehler",
            message,
        )

    def _on_cancelled(self) -> None:
        self.busy = False
        self._set_busy(False)
        self.stage_label.setText("Export abgebrochen.")
        self._append_log("Export wurde abgebrochen; unvollständige Ausgabe wurde entfernt.")

    def _thread_finished(self) -> None:
        if self.worker:
            self.worker.deleteLater()
        if self.thread:
            self.thread.deleteLater()
        self.worker = None
        self.thread = None

    def _append_log(self, message: str) -> None:
        self.log_edit.appendPlainText(message)
        self.logger.info(message)

    def _show_diagnostics(self) -> None:
        DiagnosticsDialog(
            self, run_project_diagnostics(self._settings(), self.current_media)
        ).exec()

    def _open_video(self) -> None:
        if self.last_output and self.last_output.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.last_output)))

    def _open_folder(self) -> None:
        folder = self.last_output.parent if self.last_output else Path(self.output_edit.text())
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.busy:
            answer = QMessageBox.question(self, "Export läuft", "Export abbrechen und Anwendung schließen?")
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            self._cancel()
            if self.thread:
                self.thread.quit()
                self.thread.wait(6000)
        event.accept()


def launch() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("VideoMerger")
    app.setOrganizationName("Local Video Tools")
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    return app.exec()
