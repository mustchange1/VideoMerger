from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import QThread, Qt, QUrl, Signal
from PySide6.QtGui import QCloseEvent, QDesktopServices, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QFileDialog,
    QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QRadioButton,
    QListWidget, QListWidgetItem, QScrollArea, QSlider, QSpinBox, QDoubleSpinBox,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..diagnostics import run_diagnostics, run_project_diagnostics
from ..errors import VideoMergerError
from ..logging_utils import configure_file_logger
from ..font_manager import FONT_OPTIONS, register_bundled_fonts_with_qt, resolve_font
from ..image_insertion import clamp_image_duration, clamp_image_zoom, normalize_image_filter, normalize_image_fit_mode, normalize_image_position
from ..models import ExportSettings, ProgressEvent
from ..project_order import natural_order, natural_sort_key, randomize_order
from ..project_assets import probe_audio
from ..quote_artwork import quote_artwork_path
from ..quality import QUALITY_KEYS, QUALITY_PRESETS, quality_label
from ..subtitles import ANIMATION_OPTIONS
from ..paths import ensure_project_directories, locate_ffmpeg, project_root
from ..project_order import ProjectOrderStore
from ..settings_store import SettingsStore
from ..voiceover_order import normalize_voiceover_order_mode, voiceover_order_indices
from ..subtitle_modes import (
    SUBTITLE_OUTPUT_BURNED_ONLY,
    SUBTITLE_OUTPUT_COMBINED,
    SUBTITLE_OUTPUT_LABELS,
    SUBTITLE_OUTPUT_WITHOUT,
    normalize_subtitle_output_mode,
)
from ..subtitle_preview import ImageInsertionPreviewCanvas, QuotePreviewCanvas, SubtitlePreviewCanvas, sample_subtitle_text
from ..timeline import duration_before_merge_value
from ..video_pool import (
    VIDEO_ORDER_ALPHABETICAL,
    VIDEO_ORDER_LEGACY_FOLDER_ALTERNATING,
    VIDEO_ORDER_MANUAL,
    VIDEO_ORDER_NATURAL,
    VIDEO_ORDER_RANDOM,
    compute_pool_status,
    normalize_video_order_mode,
    order_media_for_video_order,
)
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
        self._loading = True

        self.setWindowTitle("VideoMerger – Local Studio")
        self.setMinimumSize(900, 760)
        self.resize(1060, 900)
        self.setAcceptDrops(True)
        register_bundled_fonts_with_qt()
        self._build_ui()
        self.setStyleSheet(APP_STYLE)
        self._load_settings()
        self._append_log("VideoMerger 1.4.0 gestartet – Video-Pool (Required-Only), Smart Stretch, Before/After Merge, Quote/Flyer-Artwork, echte Subtitle-Preview, sauberer Output + YouTube-Metadaten. Alle Videodaten bleiben lokal.")

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
        subtitle = QLabel("Zwei Stufen · Voiceover · Musik · Wort-Sync · Untertitel · Video-Pool · Quote/Flyer · Smart Stretch · Before/After Merge · lokal")
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
        browse_input = QPushButton("Browse Legacy Root …")
        browse_output = QPushButton("Browse …")
        browse_input.clicked.connect(self._browse_input)
        browse_output.clicked.connect(self._browse_output)
        io_layout.addWidget(QLabel("Legacy Input Root"), 0, 0)
        io_layout.addWidget(self.input_edit, 0, 1)
        io_layout.addWidget(browse_input, 0, 2)
        self.source_folders_list = QListWidget()
        self.source_folders_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.source_folders_list.setMaximumHeight(105)
        self.source_folders_list.itemChanged.connect(lambda _item: self._clear_stale_analysis(self.input_edit.text()))
        self.add_folder_button = QPushButton("Add Folder …")
        self.remove_folder_button = QPushButton("Remove Folder")
        self.clear_folders_button = QPushButton("Clear All")
        self.add_folder_button.clicked.connect(self._add_source_folder)
        self.remove_folder_button.clicked.connect(self._remove_source_folder)
        self.clear_folders_button.clicked.connect(self._clear_source_folders)
        folder_buttons = QHBoxLayout()
        folder_buttons.addWidget(self.add_folder_button)
        folder_buttons.addWidget(self.remove_folder_button)
        folder_buttons.addWidget(self.clear_folders_button)
        io_layout.addWidget(QLabel("Configured Video Folders"), 1, 0, Qt.AlignTop)
        io_layout.addWidget(self.source_folders_list, 1, 1, 1, 2)
        io_layout.addLayout(folder_buttons, 2, 1, 1, 2)
        self.video_order_combo = QComboBox()
        self.video_order_combo.addItem("Natural", VIDEO_ORDER_NATURAL)
        self.video_order_combo.addItem("Alphabetical", VIDEO_ORDER_ALPHABETICAL)
        self.video_order_combo.addItem("Random", VIDEO_ORDER_RANDOM)
        self.video_order_combo.addItem("Manual", VIDEO_ORDER_MANUAL)
        self.video_order_combo.setToolTip(
            "The selected project order is applied before Required-Only duration selection. "
            "Manual is the explicit override; Random never becomes Manual automatically."
        )
        self.video_order_combo.currentIndexChanged.connect(self._video_order_mode_changed)
        io_layout.addWidget(QLabel("Video Order"), 3, 0)
        io_layout.addWidget(self.video_order_combo, 3, 1, 1, 2)
        io_layout.addWidget(QLabel("Output Folder"), 4, 0)
        io_layout.addWidget(self.output_edit, 4, 1)
        io_layout.addWidget(browse_output, 4, 2)
        drop_hint = QLabel("Add one or more configured folders; the legacy root scans its immediate files only. MP4, MOV, MKV, AVI, WebM, M4V …")
        drop_hint.setObjectName("dropHint")
        io_layout.addWidget(drop_hint, 5, 0, 1, 3)
        outer.addWidget(io_group)

        audio_group = QGroupBox("2 · Audio & Script")
        audio_layout = QGridLayout(audio_group)
        self.music_edit = QLineEdit()
        music_button = QPushButton("Choose …")
        music_button.clicked.connect(lambda: self._browse_asset(self.music_edit, "audio"))
        self.script_mode_combo = QComboBox()
        self.script_mode_combo.addItem("One Global Script (eine Textdatei für die komplette Voiceover-Timeline)", "single")
        self.script_mode_combo.addItem("Individual Scripts (Basename-Matching pro Voiceover)", "matched")
        self.script_mode_combo.currentIndexChanged.connect(self._sync_script_mode_controls)
        self.voiceover_order_combo = QComboBox()
        self.voiceover_order_combo.addItem("Natural / Alphabetical", "natural")
        self.voiceover_order_combo.addItem("Modification Date – oldest first", "mtime_oldest")
        self.voiceover_order_combo.addItem("Modification Date – newest first", "mtime_newest")
        self.voiceover_order_combo.addItem("Manual (drag / move buttons)", "manual")
        self.voiceover_order_combo.currentIndexChanged.connect(self._voiceover_order_changed)
        self.global_script_edit = QLineEdit()
        self.global_script_edit.setPlaceholderText("One global script for the complete ordered voiceover sequence …")
        self.global_script_edit.textChanged.connect(self._sync_subtitle_request)
        self.global_script_button = QPushButton("Choose Global Script …")
        self.global_script_button.clicked.connect(lambda: self._browse_asset(self.global_script_edit, "script"))
        self.voiceover_pause_combo = QComboBox()
        for label, value in (
            ("0.0 sec", 0.0), ("0.25 sec", 0.25), ("0.5 sec", 0.5),
            ("0.7 sec (Standard)", 0.7), ("1.0 sec", 1.0), ("1.5 sec", 1.5),
            ("2.0 sec", 2.0), ("Custom", -1.0),
        ):
            self.voiceover_pause_combo.addItem(label, value)
        self.voiceover_pause_combo.setCurrentIndex(self.voiceover_pause_combo.findData(0.7))
        self.voiceover_pause_combo.currentIndexChanged.connect(self._voiceover_pause_changed)
        self.voiceover_pause_spin = QDoubleSpinBox()
        self.voiceover_pause_spin.setRange(0.0, 10.0)
        self.voiceover_pause_spin.setSingleStep(0.05)
        self.voiceover_pause_spin.setDecimals(2)
        self.voiceover_pause_spin.setSuffix(" sec")
        self.voiceover_pause_spin.setValue(0.7)
        self.voiceover_pause_spin.valueChanged.connect(self._update_pool_status)
        self.voiceover_table = ReorderTableWidget(0, 3)
        self.voiceover_table.setHorizontalHeaderLabels(["#", "Voiceover", "Script"])
        self.voiceover_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.voiceover_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.voiceover_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.voiceover_table.setMaximumHeight(150)
        self.voiceover_table.row_move_requested.connect(self._move_voiceover_row)
        self.voiceover_add_button = QPushButton("Add Voiceover Files …")
        self.voiceover_remove_button = QPushButton("Remove Selected")
        self.voiceover_delete_all_button = QPushButton("Delete All Voiceovers")
        self.voiceover_clear_scripts_button = QPushButton("Clear All Scripts")
        self.voiceover_script_button = QPushButton("Choose Script for Selected …")
        self.voiceover_up_button = QPushButton("Move Up")
        self.voiceover_down_button = QPushButton("Move Down")
        self.voiceover_top_button = QPushButton("Move to Top")
        self.voiceover_bottom_button = QPushButton("Move to Bottom")
        self.voiceover_reset_button = QPushButton("Reset to Default Order")
        self.voiceover_add_button.clicked.connect(self._add_voiceovers)
        self.voiceover_remove_button.clicked.connect(self._remove_voiceover)
        self.voiceover_delete_all_button.clicked.connect(self._delete_all_voiceovers)
        self.voiceover_clear_scripts_button.clicked.connect(self._clear_all_scripts)
        self.voiceover_script_button.clicked.connect(self._choose_voiceover_script)
        self.voiceover_up_button.clicked.connect(lambda: self._move_voiceover_selected(-1))
        self.voiceover_down_button.clicked.connect(lambda: self._move_voiceover_selected(1))
        self.voiceover_top_button.clicked.connect(lambda: self._move_voiceover_selected_to(-10_000))
        self.voiceover_bottom_button.clicked.connect(lambda: self._move_voiceover_selected_to(10_000))
        self.voiceover_reset_button.clicked.connect(self._reset_voiceover_order)
        audio_layout.addWidget(QLabel("Script Mode"), 0, 0)
        audio_layout.addWidget(self.script_mode_combo, 0, 1, 1, 2)
        audio_layout.addWidget(QLabel("Voiceover Order"), 1, 0)
        audio_layout.addWidget(self.voiceover_order_combo, 1, 1, 1, 2)
        audio_layout.addWidget(self.voiceover_table, 2, 0, 1, 3)
        voice_buttons = QHBoxLayout()
        voice_buttons.addWidget(self.voiceover_add_button)
        voice_buttons.addWidget(self.voiceover_remove_button)
        voice_buttons.addWidget(self.voiceover_delete_all_button)
        voice_buttons.addWidget(self.voiceover_clear_scripts_button)
        voice_buttons.addWidget(self.voiceover_script_button)
        voice_buttons.addWidget(self.voiceover_up_button)
        voice_buttons.addWidget(self.voiceover_down_button)
        voice_buttons.addWidget(self.voiceover_top_button)
        voice_buttons.addWidget(self.voiceover_bottom_button)
        voice_buttons.addWidget(self.voiceover_reset_button)
        audio_layout.addLayout(voice_buttons, 3, 0, 1, 3)
        audio_layout.addWidget(QLabel("Global Script File"), 4, 0)
        audio_layout.addWidget(self.global_script_edit, 4, 1)
        audio_layout.addWidget(self.global_script_button, 4, 2)
        audio_layout.addWidget(QLabel("Pause Between Voiceovers"), 5, 0)
        pause_row = QHBoxLayout()
        pause_row.addWidget(self.voiceover_pause_combo)
        pause_row.addWidget(self.voiceover_pause_spin)
        audio_layout.addLayout(pause_row, 5, 1, 1, 2)
        audio_layout.addWidget(QLabel("Background Music"), 6, 0)
        audio_layout.addWidget(self.music_edit, 6, 1)
        audio_layout.addWidget(music_button, 6, 2)
        # 1.2.4 Default: Original Audio (Mute/Low bleiben unabhängig wählbar).
        self.original_audio_combo = QComboBox()
        self.original_audio_combo.addItem("Original (Standard)", "original")
        self.original_audio_combo.addItem("Low", "low")
        self.original_audio_combo.addItem("Mute", "mute")
        audio_layout.addWidget(QLabel("Original Video Audio"), 7, 0)
        audio_layout.addWidget(self.original_audio_combo, 7, 1)
        self.voice_volume_slider = QSlider(Qt.Horizontal)
        self.voice_volume_slider.setRange(0, 125)
        self.voice_volume_value = QLabel()
        self.voice_volume_slider.valueChanged.connect(
            lambda value: self.voice_volume_value.setText(f"{value} %")
        )
        audio_layout.addWidget(QLabel("Voiceover Volume"), 8, 0)
        audio_layout.addWidget(self.voice_volume_slider, 8, 1)
        audio_layout.addWidget(self.voice_volume_value, 8, 2)
        self.music_preset_combo = QComboBox()
        for label, key, value in (
            ("Very Quiet", "very_quiet", 10), ("Quiet / Background", "quiet", 22),
            ("Balanced", "balanced", 44), ("Medium", "medium", 50), ("Custom", "custom", -1),
        ):
            self.music_preset_combo.addItem(label, (key, value))
        self.music_preset_combo.currentIndexChanged.connect(self._music_preset_changed)
        self.music_volume_slider = QSlider(Qt.Horizontal)
        self.music_volume_slider.setRange(0, 100)
        self.music_volume_value = QLabel()
        self.music_volume_slider.valueChanged.connect(self._music_volume_changed)
        audio_layout.addWidget(QLabel("Music Preset"), 9, 0)
        audio_layout.addWidget(self.music_preset_combo, 9, 1)
        audio_layout.addWidget(QLabel("Music Volume"), 10, 0)
        audio_layout.addWidget(self.music_volume_slider, 10, 1)
        audio_layout.addWidget(self.music_volume_value, 10, 2)
        self.ducking_check = QCheckBox("Voiceover Ducking – Musik weich unter Sprache absenken")
        audio_layout.addWidget(self.ducking_check, 11, 0, 1, 3)
        # 1.3.0 Main Video End Padding: manual, free setting (0.0–5.0 s);
        # the existing ~1 second default is preserved exactly.
        self.end_padding_spin = QDoubleSpinBox()
        self.end_padding_spin.setRange(0.0, 5.0)
        self.end_padding_spin.setSingleStep(0.1)
        self.end_padding_spin.setDecimals(1)
        self.end_padding_spin.setSuffix(" sec")
        self.end_padding_spin.setValue(1.0)
        self.short_video_combo = QComboBox()
        self.short_video_combo.addItem("Hold Last Frame – finalen Frame halten", "hold")
        self.short_video_combo.addItem("Full-Timeline Loop – komplette manuelle Reihenfolge wiederholen", "loop")
        # 1.3.0 Duration Fit Mode: Cut Last Clip (Standard) or Smart Stretch.
        self.duration_fit_combo = QComboBox()
        self.duration_fit_combo.addItem("Cut Last Clip (Standard)", "cut")
        self.duration_fit_combo.addItem("Stretch Last Clip (Smart, minimal slow motion)", "stretch")
        self.duration_fit_combo.currentIndexChanged.connect(self._sync_stretch_controls)
        self.max_stretch_combo = QComboBox()
        for value in (5, 10, 15, 20):
            self.max_stretch_combo.addItem(f"{value} %", float(value))
        self.max_stretch_combo.addItem("Custom", -1.0)
        self.max_stretch_combo.setCurrentIndex(1)  # 10 % = Standard
        self.max_stretch_combo.currentIndexChanged.connect(self._sync_stretch_controls)
        self.max_stretch_spin = QDoubleSpinBox()
        self.max_stretch_spin.setRange(1.0, 50.0)
        self.max_stretch_spin.setSingleStep(1.0)
        self.max_stretch_spin.setDecimals(1)
        self.max_stretch_spin.setSuffix(" %")
        self.max_stretch_spin.setValue(10.0)
        # Independent Before/After Merge duration controls. The old
        # ``video_speed_combo`` name remains a Python compatibility alias, but
        # there is only one visible Before Merge control.
        self.duration_before_merge_combo = QComboBox()
        for step in range(5, 41):  # 0.25 … 2.00 in 0.05 steps
            value = step / 20.0
            label = f"{value:.2f}x" + ("  (Standard)" if abs(value - 0.70) < 1e-9 else "")
            self.duration_before_merge_combo.addItem(label, value)
        self.duration_before_merge_combo.setCurrentIndex(
            self.duration_before_merge_combo.findData(0.70)
        )
        self.video_speed_combo = self.duration_before_merge_combo
        self.duration_after_merge_check = QCheckBox("Enable independent After Merge operation")
        self.duration_after_merge_combo = QComboBox()
        for step in range(5, 41):
            value = step / 20.0
            self.duration_after_merge_combo.addItem(
                f"{value:.2f}x" + ("  (Standard)" if abs(value - 1.0) < 1e-9 else ""), value
            )
        self.duration_after_merge_combo.setCurrentIndex(
            self.duration_after_merge_combo.findData(1.0)
        )
        # 1.2.4/1.3.0: Zieldauer-Einflüsse sofort im Video-Pool-Status zeigen.
        self.end_padding_spin.valueChanged.connect(self._update_pool_status)
        self.short_video_combo.currentIndexChanged.connect(self._update_pool_status)
        self.duration_fit_combo.currentIndexChanged.connect(self._update_pool_status)
        self.max_stretch_combo.currentIndexChanged.connect(self._update_pool_status)
        self.max_stretch_spin.valueChanged.connect(self._update_pool_status)
        self.duration_before_merge_combo.currentIndexChanged.connect(self._update_pool_status)
        self.duration_after_merge_combo.currentIndexChanged.connect(self._update_pool_status)
        self.duration_after_merge_check.toggled.connect(self._update_pool_status)
        audio_layout.addWidget(QLabel("Main Video End Padding (nach Voiceover)"), 12, 0)
        audio_layout.addWidget(self.end_padding_spin, 12, 1)
        audio_layout.addWidget(QLabel("If Video Is Too Short"), 13, 0)
        audio_layout.addWidget(self.short_video_combo, 13, 1)
        audio_layout.addWidget(QLabel("Duration Fit Mode"), 14, 0)
        audio_layout.addWidget(self.duration_fit_combo, 14, 1)
        audio_layout.addWidget(QLabel("Maximum Stretch"), 15, 0)
        stretch_row = QHBoxLayout()
        stretch_row.addWidget(self.max_stretch_combo)
        stretch_row.addWidget(self.max_stretch_spin)
        audio_layout.addLayout(stretch_row, 15, 1)
        audio_layout.addWidget(QLabel("Duration Before Merge"), 16, 0)
        audio_layout.addWidget(self.duration_before_merge_combo, 16, 1)
        audio_layout.addWidget(QLabel("Duration After Merge"), 17, 0)
        after_row = QHBoxLayout()
        after_row.addWidget(self.duration_after_merge_check)
        after_row.addWidget(self.duration_after_merge_combo)
        audio_layout.addLayout(after_row, 17, 1, 1, 2)
        outer.addWidget(audio_group)

        subtitle_group = QGroupBox("3 · Subtitles")
        subtitle_layout = QGridLayout(subtitle_group)
        self.subtitle_check = QCheckBox(
            "Enable Burned-In Subtitles + SRT + VTT (automatic when Voiceover + Script are assigned)"
        )
        subtitle_layout.addWidget(self.subtitle_check, 0, 0, 1, 3)
        self.subtitle_output_combo = QComboBox()
        self.subtitle_output_combo.addItem(SUBTITLE_OUTPUT_LABELS[SUBTITLE_OUTPUT_COMBINED], SUBTITLE_OUTPUT_COMBINED)
        self.subtitle_output_combo.addItem(SUBTITLE_OUTPUT_LABELS[SUBTITLE_OUTPUT_BURNED_ONLY], SUBTITLE_OUTPUT_BURNED_ONLY)
        self.subtitle_output_combo.addItem(SUBTITLE_OUTPUT_LABELS[SUBTITLE_OUTPUT_WITHOUT], SUBTITLE_OUTPUT_WITHOUT)
        self.subtitle_output_combo.setToolTip(
            "The selected mode controls actual subtitle rendering and which output files are created."
        )
        self.subtitle_output_combo.currentIndexChanged.connect(self._subtitle_output_mode_changed)
        subtitle_layout.addWidget(QLabel("Output Mode"), 1, 0)
        subtitle_layout.addWidget(self.subtitle_output_combo, 1, 1, 1, 2)
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
        self.subtitle_position_combo.addItems(["Bottom Center", "Center", "Bottom", "Medium-Low", "Middle", "Top"])
        self._subtitle_position_overridden = False
        self._subtitle_style_overridden = False
        self._subtitle_animation_overridden = False
        self.subtitle_debug_check = QCheckBox("Subtitle Debug Overlay – current word + exact start/end (default OFF)")
        subtitle_layout.addWidget(QLabel("Language"), 2, 0)
        subtitle_layout.addWidget(self.subtitle_language_combo, 2, 1)
        subtitle_layout.addWidget(QLabel("Style"), 3, 0)
        subtitle_layout.addWidget(self.subtitle_style_combo, 3, 1)
        subtitle_layout.addWidget(QLabel("Animation"), 4, 0)
        subtitle_layout.addWidget(self.subtitle_animation_combo, 4, 1)
        subtitle_layout.addWidget(QLabel("Font"), 5, 0)
        subtitle_layout.addWidget(self.subtitle_font_combo, 5, 1)
        subtitle_layout.addWidget(QLabel("Position"), 6, 0)
        subtitle_layout.addWidget(self.subtitle_position_combo, 6, 1)
        subtitle_layout.addWidget(self.subtitle_debug_check, 7, 0, 1, 3)
        # 1.2.4: echte Subtitle-Preview – dieselbe Layout-Logik wie der
        # Burn-In-Renderer (Zeilenumbrüche, Font-Metriken, Safe-Area,
        # Position, Wort-Highlight). Kein fakes GUI-Text.
        self.subtitle_live_preview = SubtitlePreviewCanvas()
        self.subtitle_live_preview.setMinimumHeight(130)
        subtitle_layout.addWidget(self.subtitle_live_preview, 8, 0, 1, 3)
        for control in (
            self.subtitle_font_combo, self.subtitle_style_combo,
            self.subtitle_animation_combo, self.subtitle_position_combo,
            self.subtitle_language_combo,
        ):
            control.currentIndexChanged.connect(self._update_subtitle_live_preview)
        self.subtitle_debug_check.toggled.connect(self._update_subtitle_live_preview)
        self.subtitle_position_combo.currentIndexChanged.connect(self._subtitle_position_changed)
        self.subtitle_style_combo.currentIndexChanged.connect(self._subtitle_style_changed)
        self.subtitle_animation_combo.currentIndexChanged.connect(self._subtitle_animation_changed)
        self.subtitle_preview_button = QPushButton("Open Larger Subtitle Preview")
        self.subtitle_preview_button.clicked.connect(self._preview_subtitle_style)
        subtitle_layout.addWidget(self.subtitle_preview_button, 9, 1)
        self.alignment_warning_check = QCheckBox("Continue After Alignment Warning (manual confirmation)")
        subtitle_layout.addWidget(self.alignment_warning_check, 10, 0, 1, 3)
        outer.addWidget(subtitle_group)

        format_group = QGroupBox("4 · Video Format & Transition")
        format_layout = QGridLayout(format_group)
        self.radio_16 = QRadioButton("16:9 · YouTube / Landscape")
        self.radio_9 = QRadioButton("9:16 · Shorts / Reels / TikTok")
        self.radio_16.toggled.connect(self._update_resolution_choices)
        self.radio_16.toggled.connect(self._update_quote_preview)
        self.radio_16.toggled.connect(self._update_image_preview)
        self.radio_9.toggled.connect(self._update_quote_preview)
        self.radio_9.toggled.connect(self._update_image_preview)
        format_layout.addWidget(self.radio_16, 0, 0)
        format_layout.addWidget(self.radio_9, 0, 1)
        format_layout.addWidget(QLabel("Resolution"), 1, 0)
        self.resolution_combo = QComboBox()
        self.resolution_combo.currentIndexChanged.connect(self._mark_preset_custom)
        self.resolution_combo.currentIndexChanged.connect(self._update_quote_preview)
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

        outro_group = QGroupBox("7 · Optional Stage 2 – Intro / Add Image / Quote-Flyer / Outro")
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
        # Optional, silent Stage-2 artwork between Intro and Main Video.
        # The finished visual is created outside VideoMerger; there is no
        # Uploaded Quote/Flyer artwork is the only Quote workflow in the GUI.
        self.quote_check = QCheckBox("Include Quote / Flyer")
        self.quote_artwork_path_edit = QLineEdit()
        self.quote_artwork_path_edit.setPlaceholderText(
            "PDF, PNG, JPG, JPEG oder WEBP auswählen …"
        )
        self.quote_artwork_choose = QPushButton("Choose File …")
        self.quote_artwork_choose.clicked.connect(
            lambda: self._browse_asset(self.quote_artwork_path_edit, "quote_artwork")
        )
        self.quote_pdf_page_spin = QSpinBox()
        self.quote_pdf_page_spin.setRange(1, 9999)
        self.quote_pdf_page_spin.setValue(1)
        self.quote_pdf_page_spin.setToolTip("One-based page number for a multi-page PDF.")
        self.quote_artwork_fit_combo = QComboBox()
        self.quote_artwork_fit_combo.addItem("Fit", "fit")
        self.quote_artwork_fit_combo.addItem("Fill", "fill")
        self.quote_artwork_fit_combo.addItem("Crop", "crop")
        self.quote_duration_spin = QDoubleSpinBox()
        self.quote_duration_spin.setRange(0.5, 5.0)
        self.quote_duration_spin.setSingleStep(0.1)
        self.quote_duration_spin.setDecimals(1)
        self.quote_duration_spin.setSuffix(" sec")
        self.quote_duration_spin.setValue(4.0)
        self.quote_preview = QuotePreviewCanvas()
        self.quote_preview.setMinimumHeight(180)
        self.quote_check.toggled.connect(self._sync_quote_visibility)
        self.quote_artwork_path_edit.textChanged.connect(self._sync_quote_artwork_controls)
        self.quote_pdf_page_spin.valueChanged.connect(self._update_quote_preview)
        self.quote_artwork_fit_combo.currentIndexChanged.connect(self._update_quote_preview)
        self.quote_duration_spin.valueChanged.connect(self._update_quote_preview)

        # Add Image (Stage 2 only). This is intentionally a separate control
        # namespace from Quote/Flyer, including its own framing, zoom, look,
        # duration, boundary transition and position state. Image Insertion is
        # retained as the legacy API/persistence name.
        self.image_group = QGroupBox("Add Image (silent Stage 2)")
        image_layout = QGridLayout(self.image_group)
        self.image_check = QCheckBox("Include Image")
        self.image_path_edit = QLineEdit()
        self.image_path_edit.setPlaceholderText("PNG, JPG, JPEG oder WEBP auswählen …")
        self.image_choose = QPushButton("Choose Image …")
        self.image_choose.clicked.connect(lambda: self._browse_asset(self.image_path_edit, "image_insertion"))
        self.image_position_combo = QComboBox()
        self.image_position_combo.addItem("Before Main Video", "before_main")
        self.image_position_combo.addItem("After Main Video", "after_main")
        self.image_duration_combo = QComboBox()
        self.image_duration_combo.addItem("2.0 sec", 2.0)
        self.image_duration_combo.addItem("4.0 sec (Standard)", 4.0)
        self.image_duration_combo.addItem("6.0 sec", 6.0)
        self.image_duration_combo.addItem("Custom", -1.0)
        self.image_duration_spin = QDoubleSpinBox()
        self.image_duration_spin.setRange(0.5, 60.0)
        self.image_duration_spin.setSingleStep(0.5)
        self.image_duration_spin.setDecimals(1)
        self.image_duration_spin.setSuffix(" sec")
        self.image_duration_spin.setValue(4.0)
        self.image_transition_combo = QComboBox()
        for key, label, description in TRANSITION_OPTIONS:
            self.image_transition_combo.addItem(label, key)
            self.image_transition_combo.setItemData(
                self.image_transition_combo.count() - 1, description, Qt.ToolTipRole
            )
        self.image_transition_combo.setCurrentIndex(
            self.image_transition_combo.findData("cross_dissolve")
        )
        self.image_transition_spin = QDoubleSpinBox()
        self.image_transition_spin.setRange(0.0, 5.0)
        self.image_transition_spin.setSingleStep(0.25)
        self.image_transition_spin.setDecimals(2)
        self.image_transition_spin.setSuffix(" sec")
        self.image_transition_spin.setValue(1.0)
        self.image_fit_combo = QComboBox()
        self.image_fit_combo.addItem("Fit", "fit")
        self.image_fit_combo.addItem("Fill", "fill")
        self.image_fit_combo.addItem("Crop", "crop")
        self.image_zoom_spin = QSpinBox()
        self.image_zoom_spin.setRange(100, 300)
        self.image_zoom_spin.setSuffix(" %")
        self.image_zoom_spin.setValue(100)
        self.image_filter_combo = QComboBox()
        for label, key in (("Natural", "natural"), ("Cinematic", "cinematic"),
                           ("Moody", "moody"), ("Film", "film"),
                           ("Dark Editorial", "dark_editorial")):
            self.image_filter_combo.addItem(label, key)
        self.image_preview = ImageInsertionPreviewCanvas()
        self.image_preview.setMinimumHeight(180)
        self.image_check.toggled.connect(self._sync_image_visibility)
        self.image_path_edit.textChanged.connect(self._sync_image_controls)
        self.image_duration_combo.currentIndexChanged.connect(self._image_duration_preset_changed)
        for control in (self.image_position_combo, self.image_duration_spin,
                        self.image_transition_combo, self.image_transition_spin,
                        self.image_fit_combo, self.image_zoom_spin, self.image_filter_combo):
            if hasattr(control, "valueChanged"):
                control.valueChanged.connect(self._update_image_preview)
            else:
                control.currentIndexChanged.connect(self._update_image_preview)

        self.final_button = QPushButton("CREATE FINAL VIDEO")
        self.final_button.setObjectName("mergeButton")
        self.final_button.clicked.connect(lambda: self._start("outro"))
        outro_layout.addWidget(QLabel("Intro Video (optional)"), 0, 0)
        outro_layout.addWidget(self.intro_edit, 0, 1)
        outro_layout.addWidget(intro_choose, 0, 2)
        outro_layout.addWidget(QLabel("Main Video"), 2, 0)
        outro_layout.addWidget(self.main_video_edit, 2, 1)
        outro_layout.addWidget(main_choose, 2, 2)
        outro_layout.addWidget(QLabel("Outro Video (optional)"), 3, 0)
        outro_layout.addWidget(self.outro_edit, 3, 1)
        outro_layout.addWidget(outro_choose, 3, 2)
        outro_layout.addWidget(QLabel("Intro Original Audio"), 4, 0)
        outro_layout.addWidget(self.intro_audio_combo, 4, 1)
        outro_layout.addWidget(QLabel("Outro Original Audio"), 5, 0)
        outro_layout.addWidget(self.outro_audio_combo, 5, 1)
        # Add Image is deliberately the first Stage-2 section directly below
        # Add Intro. Quote/Flyer remains a separate section below it.
        image_layout.addWidget(self.image_check, 0, 0, 1, 3)
        image_layout.addWidget(QLabel("Image File"), 1, 0)
        image_layout.addWidget(self.image_path_edit, 1, 1)
        image_layout.addWidget(self.image_choose, 1, 2)
        image_layout.addWidget(QLabel("Placement"), 2, 0)
        image_layout.addWidget(self.image_position_combo, 2, 1)
        image_layout.addWidget(QLabel("Duration"), 3, 0)
        image_duration_row = QHBoxLayout()
        image_duration_row.addWidget(self.image_duration_combo)
        image_duration_row.addWidget(self.image_duration_spin)
        image_layout.addLayout(image_duration_row, 3, 1, 1, 2)
        image_layout.addWidget(QLabel("Transition"), 4, 0)
        image_layout.addWidget(self.image_transition_combo, 4, 1, 1, 2)
        image_layout.addWidget(QLabel("Transition Duration"), 5, 0)
        image_layout.addWidget(self.image_transition_spin, 5, 1)
        image_layout.addWidget(QLabel("Sizing"), 6, 0)
        image_layout.addWidget(self.image_fit_combo, 6, 1)
        image_layout.addWidget(QLabel("Zoom"), 7, 0)
        image_layout.addWidget(self.image_zoom_spin, 7, 1)
        image_layout.addWidget(QLabel("Look / Filter"), 8, 0)
        image_layout.addWidget(self.image_filter_combo, 8, 1)
        image_layout.addWidget(QLabel("Preview"), 9, 0)
        image_layout.addWidget(self.image_preview, 9, 1, 1, 2)
        outro_layout.addWidget(self.image_group, 1, 0, 1, 3)
        outro_layout.addWidget(self.outro_transition_check, 6, 0, 1, 2)
        outro_layout.addWidget(self.quote_check, 7, 0, 1, 3)
        outro_layout.addWidget(QLabel("Quote / Flyer File"), 8, 0)
        outro_layout.addWidget(self.quote_artwork_path_edit, 8, 1)
        outro_layout.addWidget(self.quote_artwork_choose, 8, 2)
        outro_layout.addWidget(QLabel("PDF Page"), 9, 0)
        outro_layout.addWidget(self.quote_pdf_page_spin, 9, 1)
        outro_layout.addWidget(QLabel("Artwork Fit"), 10, 0)
        outro_layout.addWidget(self.quote_artwork_fit_combo, 10, 1, 1, 2)
        outro_layout.addWidget(QLabel("Duration"), 11, 0)
        outro_layout.addWidget(self.quote_duration_spin, 11, 1)
        outro_layout.addWidget(QLabel("Preview"), 12, 0)
        outro_layout.addWidget(self.quote_preview, 12, 1, 1, 2)
        outro_layout.addWidget(self.final_button, 13, 1)
        outer.addWidget(outro_group)

        summary_group = QGroupBox("Projekt-Reihenfolge · Videos – Natural, Alphabetical, Random oder Manual")
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
        self.source_folders_list.clear()
        for value in list(getattr(self.saved, "source_folders", []) or []):
            path = Path(value).expanduser().resolve()
            self.source_folders_list.addItem(QListWidgetItem(str(path)))
        saved_mode = normalize_video_order_mode(
            getattr(self.saved, "video_order_mode", VIDEO_ORDER_NATURAL)
        )
        # Keep the legacy value internally until the user changes the
        # selector; its behavior remains the former folder-aware Natural mode.
        self.video_order_mode = saved_mode
        display_mode = (
            VIDEO_ORDER_NATURAL
            if saved_mode == VIDEO_ORDER_LEGACY_FOLDER_ALTERNATING
            else saved_mode
        )
        self.video_order_combo.blockSignals(True)
        self.video_order_combo.setCurrentIndex(self.video_order_combo.findData(display_mode))
        self.video_order_combo.blockSignals(False)
        saved_keys: set[str] = set()
        if self.store.path.is_file():
            try:
                import json
                raw_settings = json.loads(self.store.path.read_text(encoding="utf-8"))
                if isinstance(raw_settings, dict):
                    saved_keys = set(raw_settings)
            except (OSError, ValueError, TypeError):
                pass
        # A field that is absent from a legacy settings file is not an
        # explicit user override. This lets the new aspect-aware position
        # default apply once, while any saved/manual value remains authoritative.
        self._subtitle_position_overridden = "subtitle_position" in saved_keys
        self._subtitle_style_overridden = "subtitle_style" in saved_keys
        self._subtitle_animation_overridden = "subtitle_animation" in saved_keys
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
        saved_global_script = getattr(self.saved, "global_script_path", "") or ""
        if not saved_global_script and self.saved.script_mode == "single" and script_units:
            # Migration fallback for projects created before the explicit
            # global_script_path field was wired into the GUI.
            saved_global_script = script_units[0]
        self.global_script_edit.setText(saved_global_script)
        order_index = self.voiceover_order_combo.findData(
            normalize_voiceover_order_mode(getattr(self.saved, "voiceover_order_mode", "natural"))
        )
        self.voiceover_order_combo.setCurrentIndex(order_index if order_index >= 0 else 0)
        self._apply_voiceover_order()
        self._render_voiceover_table()
        pause_value = max(0.0, min(10.0, float(getattr(self.saved, "voiceover_pause", 0.7))))
        self.voiceover_pause_spin.setValue(pause_value)
        pause_index = next(
            (index for index in range(self.voiceover_pause_combo.count())
             if abs(float(self.voiceover_pause_combo.itemData(index)) - pause_value) < 1e-9),
            self.voiceover_pause_combo.findData(-1.0),
        )
        self.voiceover_pause_combo.setCurrentIndex(pause_index)
        self._voiceover_pause_changed()
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
        # 1.3.0 Main Video End Padding (freie Eingabe, Standard bleibt 1.0 s).
        self.end_padding_spin.setValue(float(self.saved.final_pause))
        # Duration Fit / Smart Stretch / independent merge durations.
        fit_index = self.duration_fit_combo.findData(
            self.saved.duration_fit_mode if self.saved.duration_fit_mode in {"cut", "stretch"} else "cut"
        )
        self.duration_fit_combo.setCurrentIndex(fit_index if fit_index >= 0 else 0)
        stretch_value = max(1.0, min(50.0, float(self.saved.max_stretch_percent)))
        stretch_index = self.max_stretch_combo.findData(float(stretch_value))
        if stretch_index >= 0:
            self.max_stretch_combo.setCurrentIndex(stretch_index)
        else:
            self.max_stretch_combo.setCurrentIndex(self.max_stretch_combo.count() - 1)  # Custom
        self.max_stretch_spin.setValue(stretch_value)
        before_value = max(0.25, min(2.0, round(float(getattr(self.saved, "duration_before_merge", 0.70) or 0.70) / 0.05) * 0.05))
        before_index = self.duration_before_merge_combo.findData(before_value)
        self.duration_before_merge_combo.setCurrentIndex(
            before_index if before_index >= 0 else self.duration_before_merge_combo.findData(0.70)
        )
        after_value = max(0.25, min(2.0, round(float(getattr(self.saved, "duration_after_merge", 1.0) or 1.0) / 0.05) * 0.05))
        after_index = self.duration_after_merge_combo.findData(after_value)
        self.duration_after_merge_combo.setCurrentIndex(
            after_index if after_index >= 0 else self.duration_after_merge_combo.findData(1.0)
        )
        self.duration_after_merge_check.setChecked(bool(getattr(self.saved, "duration_after_merge_enabled", False)))
        self._sync_stretch_controls()
        self.subtitle_check.setChecked(self.saved.subtitle_enabled)
        subtitle_mode_index = self.subtitle_output_combo.findData(
            normalize_subtitle_output_mode(getattr(self.saved, "subtitle_output_mode", SUBTITLE_OUTPUT_COMBINED))
        )
        self.subtitle_output_combo.setCurrentIndex(
            subtitle_mode_index if subtitle_mode_index >= 0 else 0
        )
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
        # Quote / Flyer artwork. Legacy text Quote fields are intentionally
        # not copied into the new UI; they remain harmlessly loadable in the
        # settings model, but can never trigger text rendering.
        self.quote_check.setChecked(bool(self.saved.quote_enabled))
        self.quote_artwork_path_edit.setText(getattr(self.saved, "quote_artwork_path", ""))
        self.quote_pdf_page_spin.setValue(max(1, int(getattr(self.saved, "quote_pdf_page", 1) or 1)))
        fit_index = self.quote_artwork_fit_combo.findData(
            getattr(self.saved, "quote_artwork_fit_mode", "fit")
        )
        self.quote_artwork_fit_combo.setCurrentIndex(fit_index if fit_index >= 0 else 0)
        self.quote_duration_spin.setValue(max(0.5, min(5.0, float(self.saved.quote_duration))))
        self._sync_quote_visibility()
        self.image_check.setChecked(bool(getattr(self.saved, "image_enabled", False)))
        self.image_path_edit.setText(getattr(self.saved, "image_path", ""))
        image_position_index = self.image_position_combo.findData(
            normalize_image_position(getattr(self.saved, "image_position", "after_intro"))
        )
        self.image_position_combo.setCurrentIndex(image_position_index if image_position_index >= 0 else 0)
        self.image_duration_spin.setValue(clamp_image_duration(getattr(self.saved, "image_duration", 4.0)))
        image_duration_index = self.image_duration_combo.findData(float(self.image_duration_spin.value()))
        self.image_duration_combo.setCurrentIndex(
            image_duration_index if image_duration_index >= 0 else self.image_duration_combo.findData(-1.0)
        )
        image_transition_index = self.image_transition_combo.findData(
            getattr(self.saved, "image_transition_type", "cross_dissolve")
        )
        self.image_transition_combo.setCurrentIndex(
            image_transition_index if image_transition_index >= 0
            else self.image_transition_combo.findData("cross_dissolve")
        )
        self.image_transition_spin.setValue(max(0.0, min(5.0, float(getattr(self.saved, "image_transition_duration", 1.0)))))
        image_fit_index = self.image_fit_combo.findData(normalize_image_fit_mode(getattr(self.saved, "image_fit_mode", "fit")))
        self.image_fit_combo.setCurrentIndex(image_fit_index if image_fit_index >= 0 else 0)
        self.image_zoom_spin.setValue(clamp_image_zoom(getattr(self.saved, "image_zoom", 100)))
        image_filter_index = self.image_filter_combo.findData(normalize_image_filter(getattr(self.saved, "image_filter", "natural")))
        self.image_filter_combo.setCurrentIndex(image_filter_index if image_filter_index >= 0 else 0)
        self._sync_image_visibility()
        self._sync_subtitle_request()
        self._update_subtitle_live_preview()
        self._update_quote_preview()
        self._update_pool_status()

    def _settings(self) -> ExportSettings:
        voiceover_units = list(getattr(self, "voiceover_paths_list", []))
        script_units = list(getattr(self, "voiceover_scripts_list", []))
        script_mode = str(self.script_mode_combo.currentData())
        global_script = self.global_script_edit.text().strip()
        effective_global_script = global_script if script_mode == "single" else ""
        script_paths = (
            [effective_global_script] if effective_global_script
            else (script_units if script_mode == "matched" else [])
        )
        return ExportSettings(
            source_folders=self._configured_source_folders(),
            video_order_mode=normalize_video_order_mode(
                getattr(self, "video_order_mode", self.video_order_combo.currentData())
            ),
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
            script_path=(effective_global_script if script_mode == "single" else (script_units[0] if script_units else "")),
            voiceover_paths=voiceover_units,
            script_paths=script_paths,
            script_mode=script_mode,
            global_script_path=effective_global_script,
            voiceover_order_mode=normalize_voiceover_order_mode(self.voiceover_order_combo.currentData()),
            voiceover_pause=float(self.voiceover_pause_spin.value()),
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
            final_pause=float(self.end_padding_spin.value()),
            short_video_mode=str(self.short_video_combo.currentData()),
            duration_fit_mode=str(self.duration_fit_combo.currentData()),
            max_stretch_percent=self._max_stretch_value(),
            duration_before_merge=float(self.duration_before_merge_combo.currentData()),
            duration_after_merge=float(self.duration_after_merge_combo.currentData()),
            duration_after_merge_enabled=self.duration_after_merge_check.isChecked(),
            video_speed=1.0,
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
            # Stage-2 Quote / Flyer artwork. Legacy text settings are not
            # written from the GUI and cannot produce a generated card.
            quote_enabled=self.quote_check.isChecked(),
            quote_input_mode="artwork",
            quote_artwork_path=self.quote_artwork_path_edit.text().strip(),
            quote_pdf_page=int(self.quote_pdf_page_spin.value()),
            quote_artwork_fit_mode=str(self.quote_artwork_fit_combo.currentData()),
            quote_duration=float(self.quote_duration_spin.value()),
            image_enabled=self.image_check.isChecked(),
            image_path=self.image_path_edit.text().strip(),
            image_position=normalize_image_position(self.image_position_combo.currentData()),
            image_duration=float(self.image_duration_spin.value()),
            image_transition_type=str(self.image_transition_combo.currentData()),
            image_transition_duration=float(self.image_transition_spin.value()),
            image_fit_mode=normalize_image_fit_mode(self.image_fit_combo.currentData()),
            image_zoom=clamp_image_zoom(self.image_zoom_spin.value()),
            image_filter=normalize_image_filter(self.image_filter_combo.currentData()),
            subtitle_output_mode=normalize_subtitle_output_mode(self.subtitle_output_combo.currentData()),
        )

    def _max_stretch_value(self) -> float:
        """Aktiver Max-Stretch-Wert (Preset oder Custom-Spinbox)."""
        data = self.max_stretch_combo.currentData()
        if data is not None and float(data) > 0:
            return float(data)
        return max(1.0, min(50.0, float(self.max_stretch_spin.value())))

    def _sync_stretch_controls(self, *_args) -> None:
        """Max-Stretch nur in „Stretch Last Clip“ aktivieren."""
        stretch_active = str(self.duration_fit_combo.currentData()) == "stretch"
        custom = float(self.max_stretch_combo.currentData() or -1) < 0
        self.max_stretch_combo.setEnabled(stretch_active)
        self.max_stretch_spin.setEnabled(stretch_active and custom)

    def _sync_script_mode_controls(self, *_args) -> None:
        """Enable the script input that belongs to the selected voiceover mode."""
        if not hasattr(self, "script_mode_combo"):
            return
        matched = str(self.script_mode_combo.currentData()) == "matched"
        enabled = not getattr(self, "busy", False)
        self.global_script_edit.setEnabled(enabled and not matched)
        self.global_script_button.setEnabled(enabled and not matched)
        self.voiceover_script_button.setEnabled(enabled and matched)
        self._sync_subtitle_request()

    def _apply_voiceover_order(self) -> None:
        """Apply the selected deterministic order to audio and script rows."""
        units = list(getattr(self, "voiceover_paths_list", []))
        if not units:
            return
        mode = normalize_voiceover_order_mode(self.voiceover_order_combo.currentData())
        indices = voiceover_order_indices(units, mode)
        scripts = list(getattr(self, "voiceover_scripts_list", []))
        scripts.extend([""] * (len(units) - len(scripts)))
        self.voiceover_paths_list = [units[index] for index in indices]
        self.voiceover_scripts_list = [scripts[index] for index in indices]

    def _voiceover_order_changed(self, *_args) -> None:
        """Apply and persist automatic/manual voiceover ordering."""
        if getattr(self, "_loading", False):
            return
        self._apply_voiceover_order()
        self._render_voiceover_table()
        self._save_project()
        self._update_pool_status()

    def _update_transition_description(self) -> None:
        if hasattr(self, "transition_description"):
            self.transition_description.setText(
                transition_description(str(self.transition_combo.currentData()))
            )

    def _video_order_mode_changed(self, *_args) -> None:
        """Apply an automatic order immediately without creating a manual override."""
        selected = normalize_video_order_mode(self.video_order_combo.currentData())
        previous = getattr(self, "video_order_mode", VIDEO_ORDER_NATURAL)
        self.video_order_mode = selected
        if getattr(self, "_loading", False):
            return
        if self.current_media and selected != VIDEO_ORDER_MANUAL:
            self.current_media = order_media_for_video_order(self.current_media, selected)
            self._render_media_table()
            self.summary_label.setText(
                f"Video Order: {self.video_order_combo.currentText()}. "
                "Die nummerierte Liste ist die aktive Exportreihenfolge."
            )
            self._append_log(
                "Video Order angewendet (kein Manual-Override): "
                + " → ".join(item.path.name for item in self.current_media)
            )
        elif self.current_media and selected == VIDEO_ORDER_MANUAL and previous != VIDEO_ORDER_MANUAL:
            # Choosing Manual explicitly promotes the currently visible
            # sequence to the persisted authority. Random mode itself never
            # writes this sequence to the manual order store.
            self._persist_current_order()
            self._update_pool_status()
            return
        self._save_project()
        self._update_pool_status()


    def _subtitle_output_mode_changed(self, *_args) -> None:
        """Persist the explicit output contract without hiding its controls."""
        if not getattr(self, "_loading", False):
            self._append_log(
                "Subtitle output mode: "
                + SUBTITLE_OUTPUT_LABELS.get(
                    normalize_subtitle_output_mode(self.subtitle_output_combo.currentData()),
                    SUBTITLE_OUTPUT_LABELS[SUBTITLE_OUTPUT_COMBINED],
                )
            )
        self._sync_subtitle_request()


    def _image_duration_preset_changed(self, *_args) -> None:
        try:
            value = float(self.image_duration_combo.currentData())
        except (TypeError, ValueError):
            value = -1.0
        custom = value < 0.0
        self.image_duration_spin.setEnabled(custom and not getattr(self, "busy", False))
        if not custom:
            self.image_duration_spin.blockSignals(True)
            self.image_duration_spin.setValue(clamp_image_duration(value))
            self.image_duration_spin.blockSignals(False)
        self._update_image_preview()


    def _sync_image_controls(self, *_args) -> None:
        enabled = self.image_check.isChecked()
        # The chooser is intentionally enabled whenever Include Image is on,
        # even before a path exists. Every other setting is disabled with the
        # feature and cannot accidentally affect a Stage-2 render.
        for widget in (
            self.image_path_edit, self.image_choose, self.image_position_combo,
            self.image_duration_combo, self.image_transition_combo,
            self.image_transition_spin, self.image_fit_combo, self.image_zoom_spin,
            self.image_filter_combo,
            self.image_preview,
        ):
            widget.setEnabled(enabled)
        if enabled:
            self._image_duration_preset_changed()
        self._update_image_preview()


    def _sync_image_visibility(self, *_args) -> None:
        self._sync_image_controls()


    def _update_image_preview(self, *_args) -> None:
        if not hasattr(self, "image_preview"):
            return
        frame = (1920, 1080) if self.radio_16.isChecked() else (1080, 1920)
        self.image_preview.set_image(
            self.image_path_edit.text().strip(),
            normalize_image_fit_mode(self.image_fit_combo.currentData()),
            clamp_image_zoom(self.image_zoom_spin.value()),
            normalize_image_filter(self.image_filter_combo.currentData()),
            frame[0], frame[1],
        )


    def _subtitle_position_changed(self, *_args) -> None:
        if not getattr(self, "_loading", False):
            self._subtitle_position_overridden = True
        self._update_subtitle_live_preview()

    def _subtitle_style_changed(self, *_args) -> None:
        if not getattr(self, "_loading", False):
            self._subtitle_style_overridden = True
        self._update_subtitle_live_preview()

    def _subtitle_animation_changed(self, *_args) -> None:
        if not getattr(self, "_loading", False):
            self._subtitle_animation_overridden = True
        self._update_subtitle_live_preview()

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
            if not self._subtitle_style_overridden:
                default_key = "long_1" if self.radio_16.isChecked() else "short_1"
                style_index = self.subtitle_style_combo.findData(default_key)
                if style_index >= 0:
                    self.subtitle_style_combo.setCurrentIndex(style_index)
            if not self._subtitle_position_overridden:
                self.subtitle_position_combo.setCurrentText(
                    "Center" if self.radio_16.isChecked() else "Bottom Center"
                )
            # Long-form uses a stable phrase; Shorts retain word highlight.
            if not self._subtitle_animation_overridden:
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
            configured = self._configured_source_folders()
            if not configured:
                configured = [str(Path(text.strip()).expanduser().resolve())]
            keys = {os.path.normcase(str(Path(value).expanduser().resolve())) for value in configured}
        except (OSError, ValueError):
            keys = set()
        if not keys or any(
            os.path.normcase(str(item.path.expanduser().resolve().parent)) not in keys
            for item in self.current_media
        ):
            self.current_media = []
            self.files_table.setRowCount(0)
            self.summary_label.setText("Input Folder changed – please Analyze Inputs again.")

    def _browse_asset(self, edit: QLineEdit, role: str) -> None:
        filters = {
            "audio": "Audio (*.wav *.mp3 *.m4a *.aac *.flac *.ogg *.opus);;All files (*)",
            "script": "Text Script (*.txt *.text *.md);;All files (*)",
            "image": "Images (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff);;All files (*)",
            "quote_artwork": "Quote/Flyer (*.pdf *.png *.jpg *.jpeg *.webp);;All files (*)",
            "image_insertion": "Image Insertion (*.png *.jpg *.jpeg *.webp);;All files (*)",
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
        global_script = self.global_script_edit.text().strip() if hasattr(self, "global_script_edit") else ""
        have_scripts = bool(global_script) if mode == "single" else any(bool(script) for script in scripts)
        if units and have_scripts:
            if not self.subtitle_check.isChecked():
                self.subtitle_check.setChecked(True)
                self._append_log(
                    "Untertitel automatisch aktiviert: Voiceover + Script erzeugen SRT, VTT und Burn-In."
                )

    def _voiceover_pause_changed(self, *_args) -> None:
        """Apply the selected pause preset or enable the custom value."""
        data = self.voiceover_pause_combo.currentData()
        try:
            value = float(data)
        except (TypeError, ValueError):
            value = -1.0
        if value >= 0.0:
            self.voiceover_pause_spin.blockSignals(True)
            self.voiceover_pause_spin.setValue(max(0.0, min(10.0, value)))
            self.voiceover_pause_spin.blockSignals(False)
            self.voiceover_pause_spin.setEnabled(False)
        else:
            self.voiceover_pause_spin.setEnabled(not getattr(self, "busy", False))
        self._update_pool_status()

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
    # Quote / Flyer artwork preview
    # ------------------------------------------------------------------ #
    def _quote_dimensions(self) -> tuple[int, int]:
        """Return the selected output canvas for the artwork preview."""
        value = self.resolution_combo.currentText().strip().lower().replace("×", "x")
        if "x" in value:
            try:
                width, height = (int(part) for part in value.split("x", 1))
                if width > 0 and height > 0:
                    return width, height
            except ValueError:
                pass
        return (1920, 1080) if self.radio_16.isChecked() else (1080, 1920)

    def _update_quote_preview(self, *_args) -> None:
        if not hasattr(self, "quote_preview"):
            return
        width, height = self._quote_dimensions()
        self.quote_preview.set_artwork(
            self.quote_artwork_path_edit.text().strip(),
            int(self.quote_pdf_page_spin.value()),
            str(self.quote_artwork_fit_combo.currentData()),
            width,
            height,
        )

    def _sync_quote_artwork_controls(self, *_args) -> None:
        enabled = self.quote_check.isChecked()
        artwork_path = self.quote_artwork_path_edit.text().strip()
        is_pdf = artwork_path.casefold().endswith(".pdf")
        if is_pdf:
            try:
                from ..quote_artwork import pdf_page_count
                page_count = max(1, pdf_page_count(artwork_path))
                self.quote_pdf_page_spin.setMaximum(page_count)
                self.quote_pdf_page_spin.setValue(
                    min(self.quote_pdf_page_spin.value(), page_count)
                )
            except Exception:
                # Export performs the authoritative validation and reports the
                # dependency, corruption, or invalid-page error clearly.
                self.quote_pdf_page_spin.setMaximum(9999)
        else:
            self.quote_pdf_page_spin.setMaximum(9999)
        self.quote_artwork_path_edit.setEnabled(enabled)
        self.quote_artwork_choose.setEnabled(enabled)
        self.quote_pdf_page_spin.setEnabled(enabled and is_pdf)
        self.quote_artwork_fit_combo.setEnabled(enabled)
        self.quote_duration_spin.setEnabled(enabled)
        self._update_quote_preview()

    def _sync_quote_visibility(self, *_args) -> None:
        enabled = self.quote_check.isChecked()
        for widget in (
            self.quote_artwork_path_edit,
            self.quote_artwork_choose,
            self.quote_pdf_page_spin,
            self.quote_artwork_fit_combo,
            self.quote_duration_spin,
            self.quote_preview,
        ):
            widget.setEnabled(enabled)
        self._sync_quote_artwork_controls()

    def _preview_subtitle_style(self) -> None:
        """1.3.0: größere Untertitel-Vorschau mit DER Renderer-Logik.

        Kein fake QLabel-Text mehr: der Dialog malt dieselbe Geometrie wie der
        Burn-In-Renderer (preview_cue + paint_subtitle_layout — identisch zur
        Live-Vorschau) und erlaubt es, den Wort-Fortschritt der Animation
        durchzuschalten (beim Render treiben die akustischen Wortzeitstempel
        genau diese Zustände).
        """
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPen
        from ..subtitle_preview import paint_subtitle_layout, preview_cue, sample_subtitle_text
        from ..subtitle_presets import get_preset

        preset = get_preset(str(self.subtitle_style_combo.currentData()))
        dialog = QDialog(self)
        dialog.setWindowTitle("Subtitle Style Preview")
        dialog.resize(960, 560)
        layout = QVBoxLayout(dialog)
        animation = str(self.subtitle_animation_combo.currentData())
        position = self.subtitle_position_combo.currentText()
        font_key = str(self.subtitle_font_combo.currentData())
        width, height = (1920, 1080) if self.radio_16.isChecked() else (1080, 1920)
        language = self.subtitle_language_combo.currentText()
        text = sample_subtitle_text(language)
        if self.subtitle_debug_check.isChecked():
            text += " [DEBUG Overlay aktiv]"

        class _Canvas(QWidget):
            def __init__(self, parent, state):
                super().__init__(parent)
                self._state = state
                self.setMinimumHeight(320)
                self.setStyleSheet("background:#10141c;")

            def set_stage(self, stage: int) -> None:
                self._state["active"] = stage
                self.update()

            def paintEvent(self, event) -> None:  # noqa: N802
                state = self._state
                layout_obj = state["layout"]
                painter = QPainter(self)
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
                canvas = self.rect()
                if layout_obj is None:
                    painter.end()
                    return
                scale = min(canvas.width() / layout_obj.width, canvas.height() / layout_obj.height)
                w = layout_obj.width * scale
                h = layout_obj.height * scale
                rect = QRectF((canvas.width() - w) / 2, (canvas.height() - h) / 2, w, h)
                gradient = QLinearGradient(rect.topLeft(), rect.bottomLeft())
                gradient.setColorAt(0.0, QColor(38, 46, 60))
                gradient.setColorAt(0.55, QColor(26, 31, 41))
                gradient.setColorAt(1.0, QColor(17, 20, 27))
                painter.fillRect(rect, gradient)
                painter.setPen(QPen(QColor(255, 255, 255, 28)))
                painter.drawRect(rect)
                paint_subtitle_layout(painter, layout_obj, rect, scale, state["animation"], state["active"])
                painter.end()

        layout_data = preview_cue(
            text, font_key, preset.key, position, width, height, animation=animation,
        )
        state = {"layout": layout_data, "animation": animation, "active": -1}
        canvas = _Canvas(dialog, state)
        layout.addWidget(canvas, stretch=1)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Wort-Fortschritt (Animation):"))
        stage_slider = QSlider(Qt.Horizontal)
        total_words = sum(len(line) for line in layout_data.lines)
        stage_slider.setRange(0, max(0, total_words - 1))
        stage_slider.setValue(max(0, min(total_words - 1, round(total_words * 0.6))))
        stage_label = QLabel()
        controls.addWidget(stage_slider, stretch=1)
        controls.addWidget(stage_label)

        def _stage_changed(value: int) -> None:
            canvas.set_stage(value)
            stage_label.setText(f"Wort {value + 1}/{max(1, total_words)}")

        stage_slider.valueChanged.connect(_stage_changed)
        _stage_changed(stage_slider.value())
        layout.addLayout(controls)

        selected_font = resolve_font(font_key)
        note = QLabel(
            f"{selected_font.family} · {self.subtitle_animation_combo.currentText()} · {position} · "
            f"{width}×{height} (Renderer-Geometrie: identische Zeilenumbrüche, Font-Metriken, "
            "Safe-Area und Farben). Beim Rendern steuern ausschließlich echte Voiceover-"
            "Wortzeitstempel die Anzeige; die vollständige Phrase reserviert stabile Geometrie."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        close = QPushButton("Schließen")
        close.clicked.connect(dialog.accept)
        layout.addWidget(close, alignment=Qt.AlignRight)
        dialog.exec()

    def _configured_source_folders(self) -> list[str]:
        """Return the persisted GUI folder list in visible order."""
        if not hasattr(self, "source_folders_list"):
            return []
        values: list[str] = []
        for row in range(self.source_folders_list.count()):
            value = self.source_folders_list.item(row).text().strip()
            if value:
                values.append(str(Path(value).expanduser().resolve()))
        return values

    def _add_source_folder(self) -> None:
        if self.busy:
            return
        selected = QFileDialog.getExistingDirectory(self, "Add Video Source Folder", self.input_edit.text())
        if not selected:
            return
        value = str(Path(selected).expanduser().resolve())
        if value not in self._configured_source_folders():
            self.source_folders_list.addItem(QListWidgetItem(value))
            self._save_project()
            self._clear_stale_analysis(self.input_edit.text())

    def _remove_source_folder(self) -> None:
        if self.busy:
            return
        row = self.source_folders_list.currentRow()
        if row >= 0:
            self.source_folders_list.takeItem(row)
            self._save_project()
            self._clear_stale_analysis(self.input_edit.text())

    def _clear_source_folders(self) -> None:
        if self.busy:
            return
        self.source_folders_list.clear()
        self._save_project()
        self._clear_stale_analysis(self.input_edit.text())

    def _browse_input(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Legacy Input Root", self.input_edit.text())
        if selected:
            self.input_edit.setText(selected)

    def _reset_project_order(self) -> None:
        if self.busy:
            return
        folders = [Path(value) for value in self._configured_source_folders()]
        if not folders:
            folder = Path(self.input_edit.text().strip())
            if not folder.is_dir() and not self.current_media:
                QMessageBox.warning(self, "Input fehlt", "Bitte zuerst einen gültigen Input Folder auswählen.")
                return
            if self.current_media:
                folders = sorted({item.path.expanduser().resolve().parent for item in self.current_media}, key=str)
            else:
                folders = [folder]
        if not self.current_media:
            QMessageBox.information(
                self, "Reihenfolge", "Bitte zuerst Analyze Inputs ausführen, dann kann die Standardreihenfolge wiederhergestellt werden."
            )
            return
        if hasattr(self.order_store, "reset_to_default_many"):
            paths = self.order_store.reset_to_default_many(folders, [item.path for item in self.current_media])
        else:
            paths = self.order_store.reset_to_default(folders[0], [item.path for item in self.current_media])
        self.video_order_mode = VIDEO_ORDER_NATURAL
        self.video_order_combo.blockSignals(True)
        self.video_order_combo.setCurrentIndex(self.video_order_combo.findData(VIDEO_ORDER_NATURAL))
        self.video_order_combo.blockSignals(False)
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
        folders = [Path(value) for value in self._configured_source_folders()]
        if not folders:
            folders = sorted({item.path.expanduser().resolve().parent for item in self.current_media}, key=str)
        paths = randomize_order([item.path for item in self.current_media])
        self.order_store.set_active_order_many(folders, paths)
        for folder in folders:
            folder_paths = [path for path in paths if path.expanduser().resolve().parent == folder.resolve()]
            if folder_paths:
                self.order_store.set_active_order(folder, folder_paths)
        self.video_order_mode = VIDEO_ORDER_MANUAL
        self.video_order_combo.blockSignals(True)
        self.video_order_combo.setCurrentIndex(self.video_order_combo.findData(VIDEO_ORDER_MANUAL))
        self.video_order_combo.blockSignals(False)
        self._save_project()
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

    def _delete_all_voiceovers(self) -> None:
        """Remove voiceovers from the project without touching source files."""
        if self.busy:
            return
        self.voiceover_paths_list = []
        self.voiceover_scripts_list = []
        # Reset all state that belongs to the deleted voiceover set. In
        # particular, a subsequent add starts in the same mode/order/pause
        # state as a fresh project and cannot inherit a stale subtitle request.
        self.global_script_edit.clear()
        self.script_mode_combo.blockSignals(True)
        self.script_mode_combo.setCurrentIndex(self.script_mode_combo.findData("single"))
        self.script_mode_combo.blockSignals(False)
        self.voiceover_order_combo.blockSignals(True)
        self.voiceover_order_combo.setCurrentIndex(self.voiceover_order_combo.findData("natural"))
        self.voiceover_order_combo.blockSignals(False)
        self.voiceover_pause_combo.setCurrentIndex(self.voiceover_pause_combo.findData(0.7))
        self.subtitle_check.setChecked(False)
        self.voiceover_table.clearSelection()
        self.voiceover_table.setCurrentCell(-1, -1)
        self._render_voiceover_table()
        self._append_log("Alle Voiceover-Zuordnungen aus dem Projekt entfernt; Quelldateien bleiben unverändert.")
        self._save_project()
        self._update_pool_status()

    def _clear_all_scripts(self) -> None:
        """Clear per-voiceover and global script assignments only."""
        if self.busy:
            return
        units = list(getattr(self, "voiceover_paths_list", []))
        self.voiceover_scripts_list = [""] * len(units)
        self.global_script_edit.clear()
        self.subtitle_check.setChecked(False)
        self.voiceover_table.clearSelection()
        self.voiceover_table.setCurrentCell(-1, -1)
        self._render_voiceover_table()
        self._append_log("Alle Script-Zuordnungen entfernt; Voiceover-Dateien bleiben im Projekt.")
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
        manual_index = self.voiceover_order_combo.findData("manual")
        self.voiceover_order_combo.blockSignals(True)
        self.voiceover_order_combo.setCurrentIndex(manual_index)
        self.voiceover_order_combo.blockSignals(False)
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
        natural_index = self.voiceover_order_combo.findData("natural")
        self.voiceover_order_combo.blockSignals(True)
        self.voiceover_order_combo.setCurrentIndex(natural_index)
        self.voiceover_order_combo.blockSignals(False)
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
        """Return the full Main target: voiceovers, inter-unit pauses, padding.

        The formula mirrors MainProjectEngine: actual probeable voiceover
        durations plus one configured pause between adjacent units and the
        independent final end padding. ``probe_audio`` is cached, so a status
        update does not repeat expensive analysis.
        """
        units = list(getattr(self, "voiceover_paths_list", []))
        if not units:
            return 0.0
        try:
            _, ffprobe = locate_ffmpeg()
        except Exception:
            return 0.0
        durations: list[float] = []
        for path_text in units:
            path = Path(path_text).expanduser()
            if not path.is_file():
                continue
            try:
                durations.append(max(0.0, float(probe_audio(ffprobe, path).duration)))
            except Exception:
                continue
        if not durations:
            return 0.0
        try:
            pause = max(0.0, min(10.0, float(self.voiceover_pause_spin.value())))
        except Exception:
            pause = 0.7
        try:
            end_padding = max(0.0, float(self.end_padding_spin.value()))
        except Exception:
            end_padding = 0.0
        # Only actual, probeable files are timeline units. The pause is added
        # exactly between those units, never after the final one.
        return sum(durations) + pause * max(0, len(durations) - 1) + end_padding

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
            duration_fit_mode=str(settings.duration_fit_mode),
            max_stretch_percent=float(settings.max_stretch_percent),
            playback_rate=duration_before_merge_value(settings),
            # current_media is already the effective visible sequence. Running
            # the folder alternator again would make pool status disagree with
            # the sequence that Stage 1 receives.
            folder_aware=False,
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
        if not self.current_media:
            return
        configured = [Path(value) for value in self._configured_source_folders()]
        folders = configured or sorted({item.path.expanduser().resolve().parent for item in self.current_media}, key=str)
        try:
            ordered_paths = [item.path for item in self.current_media]
            self.order_store.set_active_order_many(folders, ordered_paths)
            # Keep the established per-folder store readable for legacy
            # callers and single-folder projects as well.
            for folder in folders:
                paths = [path for path in ordered_paths if path.expanduser().resolve().parent == folder.resolve()]
                if paths:
                    self.order_store.set_active_order(folder, paths)
        except AttributeError:
            for folder in folders:
                paths = [item.path for item in self.current_media if item.path.expanduser().resolve().parent == folder.resolve()]
                if paths:
                    self.order_store.set_active_order(folder, paths)
        self.video_order_mode = VIDEO_ORDER_MANUAL
        self.video_order_combo.blockSignals(True)
        self.video_order_combo.setCurrentIndex(self.video_order_combo.findData(VIDEO_ORDER_MANUAL))
        self.video_order_combo.blockSignals(False)
        self._append_log("Manuelle Exportreihenfolge gespeichert: " +
                         " → ".join(item.path.name for item in self.current_media))
        self._save_project()

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
        configured_sources = self._configured_source_folders()
        output_folder = Path(self.output_edit.text().strip())
        if mode != "outro" and not configured_sources and not input_folder.is_dir():
            QMessageBox.warning(self, "Input fehlt", "Bitte einen gültigen Input Root oder mindestens einen Video Folder hinzufügen.")
            return
        if mode != "outro" and configured_sources:
            invalid = [value for value in configured_sources if not Path(value).is_dir()]
            if invalid:
                QMessageBox.warning(self, "Input fehlt", "Nicht lesbare Video Folder:\n" + "\n".join(invalid))
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
            subtitle_mode = normalize_subtitle_output_mode(settings.subtitle_output_mode)
            if settings.subtitle_enabled and subtitle_mode != SUBTITLE_OUTPUT_WITHOUT and not has_units:
                QMessageBox.warning(
                    self, "Subtitle Inputs fehlen",
                    "Burned-In Subtitles benötigen mindestens eine Voiceover-Datei und ein Script."
                )
                return
            if settings.subtitle_enabled and subtitle_mode != SUBTITLE_OUTPUT_WITHOUT and settings.script_mode == "single" and not settings.global_script_path:
                QMessageBox.warning(
                    self, "Script fehlt",
                    "Single Global Script Mode benötigt eine Textdatei für die komplette Timeline."
                )
                return
            if settings.watermark_enabled and not settings.watermark_path:
                QMessageBox.warning(self, "Watermark fehlt", "Bitte ein Watermark-Bild wählen oder Watermark deaktivieren.")
                return
        # Quote / Flyer is artwork-only. Validate it before starting Stage 1
        # so a missing or unsupported Stage-2 asset does not waste a render.
        quote_active = False
        if settings.quote_enabled:
            artwork_value = (settings.quote_artwork_path or "").strip()
            if not artwork_value:
                QMessageBox.warning(
                    self, "Quote / Flyer File fehlt",
                    "Include Quote / Flyer ist aktiviert, aber keine Datei ausgewählt.",
                )
                return
            try:
                quote_artwork_path(artwork_value)
            except VideoMergerError as exc:
                QMessageBox.warning(self, "Quote / Flyer ungültig", str(exc))
                return
            quote_active = True
        image_active = False
        if settings.image_enabled:
            image_value = (settings.image_path or "").strip()
            if not image_value:
                QMessageBox.warning(
                    self, "Image Insertion fehlt",
                    "Include Image ist aktiviert, aber keine Bilddatei ausgewählt.",
                )
                return
            try:
                from ..image_insertion import image_insertion_path
                image_insertion_path(image_value)
            except VideoMergerError as exc:
                QMessageBox.warning(self, "Image Insertion ungültig", str(exc))
                return
            image_active = True
        if mode == "complete" and not Path(settings.intro_path).is_file() and not Path(settings.outro_path).is_file() and not quote_active and not image_active:
            QMessageBox.warning(
                self, "Intro/Outro/Quote fehlen",
                "One-Click benötigt mindestens ein gültiges Intro- oder Outro-Video oder eine aktive Quote-/Flyer-Datei.",
            )
            return
        if mode == "outro":
            if not Path(settings.main_video_path).is_file():
                QMessageBox.warning(
                    self, "Stage 2 Inputs fehlen", "Bitte ein gültiges MainVideo auswählen."
                )
                return
            if not Path(settings.intro_path).is_file() and not Path(settings.outro_path).is_file() and not quote_active and not image_active:
                QMessageBox.warning(
                    self, "Stage 2 Inputs fehlen",
                    "Bitte mindestens ein Intro- oder Outro-Video auswählen oder eine aktive Quote-/Flyer-/Image-Datei auswählen.",
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
            self.voiceover_add_button, self.voiceover_remove_button,
            self.voiceover_delete_all_button, self.voiceover_clear_scripts_button,
            self.voiceover_script_button, self.voiceover_up_button, self.voiceover_down_button,
            self.voiceover_top_button, self.voiceover_bottom_button, self.voiceover_reset_button,
        ):
            button.setEnabled(not busy)
        for widget in (
            self.add_folder_button, self.remove_folder_button, self.clear_folders_button,
            self.source_folders_list, self.video_order_combo,
            self.duration_before_merge_combo, self.duration_after_merge_check,
            self.duration_after_merge_combo,
            self.quote_check, self.quote_artwork_path_edit, self.quote_artwork_choose,
            self.quote_pdf_page_spin, self.quote_artwork_fit_combo,
            self.quote_duration_spin, self.quote_preview,
            self.image_check, self.image_path_edit, self.image_choose,
            self.image_position_combo, self.image_duration_combo,
            self.image_duration_spin, self.image_transition_combo,
            self.image_transition_spin, self.image_fit_combo,
            self.image_zoom_spin, self.image_filter_combo,
            self.image_preview,
        ):
            widget.setEnabled(not busy)
        if not busy:
            self._sync_script_mode_controls()
            self._sync_quote_visibility()
            self._sync_image_visibility()

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
                    if getattr(report, "video_no_subtitles", None):
                        details += f"\nOhne Untertitel: {report.video_no_subtitles}"
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
                if main.video_no_subtitles:
                    details += f"\nMainVideo ohne Untertitel: {main.video_no_subtitles}"
                if main.srt:
                    details += f"\n{main.srt}\n{main.vtt}\n{main.canonical_timeline}"
                if getattr(report, "final_video_no_subtitles", None):
                    details += f"\nFinalVideo ohne Untertitel: {report.final_video_no_subtitles}"
                if getattr(report, "youtube_metadata", None):
                    details += f"\nYouTube-Metadaten: {report.youtube_metadata}"
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
        # Persist the complete project, including Stage-2 Image Insertion and
        # subtitle output mode, even when the user closes without starting a
        # render. This also preserves the existing settings-store contract for
        # every other GUI control.
        self._save_project()
        event.accept()


def launch() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("VideoMerger")
    app.setOrganizationName("Local Video Tools")
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    return app.exec()
