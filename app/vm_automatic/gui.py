"""VM Automatic – dedicated lightweight GUI (PySide6, like VideoMerger).

The GUI is a thin front end around the Qt-independent
:class:`~app.vm_automatic.controller.VMAutomaticController`. All automation
logic lives in the controller; the window only displays state and forwards
user actions.
"""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QObject, QUrl, Qt, Signal
from PySide6.QtGui import QCloseEvent, QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
    QSpinBox, QDoubleSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

from ..video_merger.paths import project_root
from . import __version__
from .config import (
    OUTPUTS_AUTO, OUTPUTS_BOTH, OUTPUTS_LONG, OUTPUTS_SHORTS,
    WORKFLOW_AUTO, WORKFLOW_COMPLETE, WORKFLOW_MAIN, VMAutomaticConfig,
)
from .controller import (
    STATUS_STOPPED, STATUS_WATCHING, VMAutomaticController,
)
from .state import JobState
from . import windows_startup

_STATE_COLORS = {
    JobState.NEW: "#888888",
    JobState.WAITING_FOR_STABILITY: "#d7a300",
    JobState.READY: "#3aa0ff",
    JobState.QUEUED: "#3aa0ff",
    JobState.RUNNING: "#7b61ff",
    JobState.SUCCEEDED: "#2ea44f",
    JobState.FAILED: "#d1242f",
    JobState.RETRY_PENDING: "#d7a300",
    JobState.INTERRUPTED: "#d7a300",
}


class _Bridge(QObject):
    """Thread-safe controller → GUI event bridge."""

    event = Signal(object)


class VMAutomaticWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.root = project_root()
        self.vm_config = VMAutomaticConfig.load()
        self.controller = VMAutomaticController(self.vm_config)
        self._bridge = _Bridge()
        self._bridge.event.connect(self._on_event)
        self.controller.add_listener(lambda line: self._bridge.event.emit({"type": "log", "message": line}))

        self.setWindowTitle("VM Automatic – Video Merger Automatic")
        self.setMinimumSize(980, 720)
        self.resize(1080, 840)
        self._build_ui()
        self._refresh_all()
        self.statusBar().showMessage("VM Automatic " + __version__ + " – VideoMerger bleibt vollständig unverändert.")

    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(18, 14, 18, 14)
        outer.setSpacing(10)

        header = QHBoxLayout()
        title = QLabel("VM Automatic")
        title.setObjectName("title")
        title.setStyleSheet("font-size: 20px; font-weight: 700;")
        self.status_label = QLabel("Zustand: STOPPED")
        self.status_label.setStyleSheet("font-weight: 600; color: #d7a300;")
        header.addWidget(title)
        header.addStretch()
        header.addWidget(self.status_label)
        outer.addLayout(header)

        # --- folders / parameters ------------------------------------- #
        io_group = QGroupBox("Ordner & Parameter")
        io = QGridLayout(io_group)
        self.watch_edit = QLineEdit()
        self.output_edit = QLineEdit()
        self.pool_edit = QLineEdit()
        browse_watch = QPushButton("Browse …")
        browse_output = QPushButton("Browse …")
        browse_pool = QPushButton("Browse …")
        browse_watch.clicked.connect(lambda: self._browse(self.watch_edit))
        browse_output.clicked.connect(lambda: self._browse(self.output_edit))
        browse_pool.clicked.connect(lambda: self._browse(self.pool_edit))
        self.reconcile_spin = QSpinBox()
        self.reconcile_spin.setRange(10, 86400)
        self.reconcile_spin.setSuffix(" s")
        self.stability_spin = QDoubleSpinBox()
        self.stability_spin.setRange(1.0, 300.0)
        self.stability_spin.setSingleStep(1.0)
        self.stability_spin.setSuffix(" s")
        self.retries_spin = QSpinBox()
        self.retries_spin.setRange(1, 20)
        self.workflow_combo = QComboBox()
        self.workflow_combo.addItem("Auto (One-Click wenn Intro/Outro/Quote, sonst Main Video)", WORKFLOW_AUTO)
        self.workflow_combo.addItem("Nur Main Video (Stage 1)", WORKFLOW_MAIN)
        self.workflow_combo.addItem("One-Click Complete (Final Video)", WORKFLOW_COMPLETE)
        self.outputs_combo = QComboBox()
        self.outputs_combo.addItem("Auto (Folge dem VideoMerger-Aspect)", OUTPUTS_AUTO)
        self.outputs_combo.addItem("Long-Form (16:9)", OUTPUTS_LONG)
        self.outputs_combo.addItem("Shorts (9:16)", OUTPUTS_SHORTS)
        self.outputs_combo.addItem("Long-Form + Shorts", OUTPUTS_BOTH)
        self.archive_check = QCheckBox("Beendete Eingaben archivieren (Standard: AUS – Eingaben bleiben unverändert)")
        self.autostart_check = QCheckBox("Mit Windows starten (Standard: AUS)")
        io.addWidget(QLabel("Watch Folder (Inbox):"), 0, 0)
        io.addWidget(self.watch_edit, 0, 1)
        io.addWidget(browse_watch, 0, 2)
        io.addWidget(QLabel("Output Folder:"), 1, 0)
        io.addWidget(self.output_edit, 1, 1)
        io.addWidget(browse_output, 1, 2)
        io.addWidget(QLabel("Clip-Pool-Ordner (VideoMerger-Input):"), 2, 0)
        io.addWidget(self.pool_edit, 2, 1)
        io.addWidget(browse_pool, 2, 2)
        io.addWidget(QLabel("Scan-Intervall (Reconcile):"), 3, 0)
        io.addWidget(self.reconcile_spin, 3, 1)
        io.addWidget(QLabel("Stabilitäts-Dauer:"), 3, 2)
        io.addWidget(self.stability_spin, 3, 3)
        io.addWidget(QLabel("Workflow:"), 4, 0)
        io.addWidget(self.workflow_combo, 4, 1)
        io.addWidget(QLabel("Outputs:"), 4, 2)
        io.addWidget(self.outputs_combo, 4, 3)
        io.addWidget(self.archive_check, 5, 0, 1, 2)
        io.addWidget(self.autostart_check, 5, 2, 1, 2)
        outer.addWidget(io_group)

        # --- controls ---------------------------------------------------- #
        controls = QHBoxLayout()
        self.start_button = QPushButton("▶ Start Watching")
        self.stop_button = QPushButton("⏹ Stop Watching")
        self.scan_button = QPushButton("Scan Now")
        self.pause_button = QPushButton("Pause Queue")
        self.resume_button = QPushButton("Resume Queue")
        self.retry_button = QPushButton("Retry")
        self.reseed_button = QPushButton("Retry + Randomize Again")
        self.open_watch_button = QPushButton("Open Watch Folder")
        self.open_output_button = QPushButton("Open Output Folder")
        self.open_log_button = QPushButton("Open Logs")
        self.start_button.clicked.connect(self._start_watching)
        self.stop_button.clicked.connect(self._stop_watching)
        self.scan_button.clicked.connect(self.controller.scan_now)
        self.pause_button.clicked.connect(lambda: self._set_paused(True))
        self.resume_button.clicked.connect(lambda: self._set_paused(False))
        self.retry_button.clicked.connect(lambda: self._retry_selected(False))
        self.reseed_button.clicked.connect(lambda: self._retry_selected(True))
        self.open_watch_button.clicked.connect(lambda: self._open_folder(self.controller.vm_config.resolved_watch_folder()))
        self.open_output_button.clicked.connect(lambda: self._open_folder(self.controller.vm_config.resolved_output_folder()))
        self.open_log_button.clicked.connect(self._open_logs)
        controls.addWidget(self.start_button)
        controls.addWidget(self.stop_button)
        controls.addWidget(self.scan_button)
        controls.addWidget(self.pause_button)
        controls.addWidget(self.resume_button)
        controls.addWidget(self.retry_button)
        controls.addWidget(self.reseed_button)
        controls.addWidget(self.open_watch_button)
        controls.addWidget(self.open_output_button)
        controls.addWidget(self.open_log_button)
        outer.addLayout(controls)

        # --- master profile + queue -------------------------------------- #
        middle = QHBoxLayout()
        profile_group = QGroupBox("Aktives VideoMerger-Profil (Master-Config – read-only)")
        profile = QVBoxLayout(profile_group)
        self.profile_label = QLabel()
        self.profile_label.setWordWrap(True)
        self.profile_label.setStyleSheet("font-size: 12px;")
        profile.addWidget(self.profile_label)
        middle.addWidget(profile_group, 2)

        queue_group = QGroupBox("Queue & Jobs")
        qv = QVBoxLayout(queue_group)
        self.queue_label = QLabel("Queue: –")
        self.current_label = QLabel("Aktueller Job: –")
        self.current_label.setStyleSheet("font-weight: 600;")
        self.last_ok_label = QLabel("Letzter erfolgreicher Job: –")
        self.failed_label = QLabel("Fehlgeschlagene Jobs: 0")
        self.completed_label = QLabel("Abgeschlossene Jobs: 0")
        qv.addWidget(self.queue_label)
        qv.addWidget(self.current_label)
        qv.addWidget(self.last_ok_label)
        qv.addWidget(self.failed_label)
        qv.addWidget(self.completed_label)
        qv.addStretch()
        middle.addWidget(queue_group, 1)
        outer.addLayout(middle)

        # --- job table ----------------------------------------------------- #
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            ["Job", "Zustand", "Audio", "Script", "Versuch", "Seed", "Erkannt", "Output / Fehler"]
        )
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(7, QHeaderView.Stretch)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        outer.addWidget(self.table, stretch=3)

        # --- logs ------------------------------------------------------------ #
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        self.log_view.setStyleSheet("font-family: monospace; font-size: 12px;")
        outer.addWidget(self.log_view, stretch=2)

    # ------------------------------------------------------------------ #
    # Refresh
    # ------------------------------------------------------------------ #
    def _refresh_all(self) -> None:
        snap = self.controller.snapshot()
        # folders (only when not editing – keep it simple: always sync unless focused)
        for edit, value in (
            (self.watch_edit, snap["watch_folder"]),
            (self.output_edit, snap["output_folder"]),
            (self.pool_edit, snap["clip_pool_folder"]),
        ):
            if not edit.hasFocus():
                edit.setText(value)
        if not self.reconcile_spin.hasFocus():
            self.reconcile_spin.setValue(int(snap["reconciliation_seconds"]))
        if not self.stability_spin.hasFocus():
            self.stability_spin.setValue(float(snap["stability_seconds"]))
        if not self.retries_spin.hasFocus():
            self.retries_spin.setValue(int(snap["max_attempts"]))
        self.workflow_combo.setCurrentIndex(self.workflow_combo.findData(self.controller.vm_config.workflow))
        self.outputs_combo.setCurrentIndex(self.outputs_combo.findData(self.controller.vm_config.outputs))
        self.archive_check.setChecked(bool(self.controller.vm_config.archive_inputs))
        self.autostart_check.setChecked(windows_startup.status(self.root) == "on")

        status = snap["status"]
        if status == STATUS_WATCHING:
            self.status_label.setText(f"Zustand: WATCHING · Wächter: {snap['watcher_backend']} · Scan: {self.controller.last_scan_summary or '–'}")
            self.status_label.setStyleSheet("font-weight: 600; color: #2ea44f;")
        elif status == STATUS_STOPPED:
            self.status_label.setText("Zustand: STOPPED – Automation ist AUS (nichts wird verarbeitet)")
            self.status_label.setStyleSheet("font-weight: 600; color: #888888;")
        else:
            self.status_label.setText(f"Zustand: {status}")
            self.status_label.setStyleSheet("font-weight: 600; color: #d7a300;")

        self.start_button.setEnabled(status == STATUS_STOPPED)
        self.stop_button.setEnabled(status != STATUS_STOPPED)
        self.pause_button.setEnabled(snap["paused"] is False and status == STATUS_WATCHING)
        self.resume_button.setEnabled(snap["paused"] is True)

        jobs = snap["jobs"]
        queued = [j for j in jobs if j.state in {JobState.READY, JobState.QUEUED, JobState.RETRY_PENDING}]
        succeeded = [j for j in jobs if j.state == JobState.SUCCEEDED]
        failed = [j for j in jobs if j.state == JobState.FAILED]
        self.queue_label.setText(
            f"Queue: {len(queued)} wartend (Queue " + ("PAUSIERT" if snap["paused"] else "aktiv") + ")"
        )
        self.current_label.setText("Aktueller Job: " + (snap["current_job"] or "–"))
        self.last_ok_label.setText("Letzter erfolgreicher Job: " + (snap["last_successful_job"] or "–"))
        self.failed_label.setText(f"Fehlgeschlagene Jobs: {len(failed)}")
        self.completed_label.setText(f"Abgeschlossene Jobs: {len(succeeded)}")
        self._render_profile()

        # table
        self.table.setRowCount(len(jobs))
        for row, job in enumerate(sorted(jobs, key=lambda j: j.detected_at or "")):
            color = _STATE_COLORS.get(job.state, "#000000")
            output_text = ", ".join(Path(p).name for p in job.outputs[:4])
            if job.last_error and job.state == JobState.FAILED:
                output_text = (output_text + " · " if output_text else "") + job.last_error[:160]
            values = [
                job.display_id,
                job.state,
                job.audio or "–",
                job.script or "–",
                f"{job.attempts}/{job.max_attempts}",
                str(job.seed) if job.seed is not None else "–",
                (job.detected_at or "")[:19].replace("T", " "),
                output_text,
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if col == 1:
                    item.setForeground(Qt.GlobalColor.transparent)
                    item.setBackground(Qt.GlobalColor.transparent)
                    item.setText(job.state)
                    from PySide6.QtGui import QColor

                    item.setForeground(QColor(color))
                self.table.setItem(row, col, item)

    def _render_profile(self) -> None:
        master = self.controller.settings_store.load()
        path = self.controller.settings_store.path
        lines = [
            f"Config-Datei: {path}",
            f"Aspect: {master.aspect} · Output-Preset: {master.output_preset} · Qualität: {master.quality_preset}",
            f"Übergang: {master.transition_type} · {master.transition_duration:g} s",
            f"Musik: {Path(master.music_path).name if master.music_path else '–'} (Volume {master.music_volume} %, Preset {master.music_preset}, Ducking {'AN' if master.ducking_enabled else 'AUS'})",
            f"Untertitel: {master.subtitle_style} · {master.subtitle_language} · Animation {master.subtitle_animation} · Position {master.subtitle_position}",
            f"Debug-Overlay: {'AN' if master.subtitle_debug_overlay else 'AUS (Standard)'} · Alignment-Warnung weiter: {'AN' if master.allow_alignment_warnings else 'AUS (Standard)'}",
            f"Intro: {Path(master.intro_path).name if master.intro_path else '–'} · Outro: {Path(master.outro_path).name if master.outro_path else '–'} · Quote: {'AN' if master.quote_enabled else 'AUS'}",
            "VM Automatic ändert diese Einstellungen NIEMALS – nur Job-Input + job-lokale Clip-Reihenfolge.",
        ]
        self.profile_label.setText("\n".join(lines))

    # ------------------------------------------------------------------ #
    # Events
    # ------------------------------------------------------------------ #
    def _on_event(self, payload: dict) -> None:
        ptype = payload.get("type")
        if ptype == "log":
            self.log_view.appendPlainText(str(payload.get("message", "")))
            return
        self._refresh_all()

    # ------------------------------------------------------------------ #
    # Actions
    # ------------------------------------------------------------------ #
    def _browse(self, edit: QLineEdit) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Ordner wählen", edit.text() or str(self.root))
        if selected:
            edit.setText(selected)

    def _start_watching(self) -> None:
        changes: dict = {}
        if self.watch_edit.hasFocus():
            return
        # persist edited parameters
        for key, value in (
            ("watch_folder", self.watch_edit.text().strip()),
            ("output_folder", self.output_edit.text().strip()),
            ("clip_pool_folder", self.pool_edit.text().strip()),
            ("reconciliation_seconds", float(self.reconcile_spin.value())),
            ("stability_seconds", float(self.stability_spin.value())),
            ("max_attempts", int(self.retries_spin.value())),
            ("workflow", str(self.workflow_combo.currentData())),
            ("outputs", str(self.outputs_combo.currentData())),
            ("archive_inputs", bool(self.archive_check.isChecked())),
        ):
            if value:
                changes[key] = value
        try:
            self.controller.update_config(changes)
        except Exception as exc:
            QMessageBox.critical(self, "VM Automatic", str(exc))
            return
        if windows_startup.status(self.root) != ("on" if self.autostart_check.isChecked() else "off"):
            try:
                windows_startup.set_state(self.autostart_check.isChecked(), self.root)
            except Exception as exc:  # non-Windows or registry problem
                self.log_view.appendPlainText(f"Windows-Autostart: {exc}")
        try:
            self.controller.start()
        except Exception as exc:
            QMessageBox.critical(self, "VM Automatic", "Start fehlgeschlagen:\n" + str(exc))
        self._refresh_all()

    def _stop_watching(self) -> None:
        self.controller.stop(cancel_running=True)
        self._refresh_all()

    def _set_paused(self, paused: bool) -> None:
        if paused:
            self.controller.pause_queue()
        else:
            self.controller.resume_queue()
        self._refresh_all()

    def _selected_job_id(self) -> str | None:
        row = self.table.currentRow()
        if row < 0 or row >= self.table.rowCount():
            return None
        snap = self.controller.snapshot()
        jobs = sorted(snap["jobs"], key=lambda j: j.detected_at or "")
        if row >= len(jobs):
            return None
        return jobs[row].id

    def _retry_selected(self, reseed: bool) -> None:
        job_id = self._selected_job_id()
        if not job_id:
            QMessageBox.information(self, "VM Automatic", "Bitte zuerst einen Job in der Tabelle auswählen.")
            return
        job = self.controller.store.get(job_id)
        if job and job.state == JobState.SUCCEEDED:
            QMessageBox.information(
                self, "VM Automatic",
                "Dieser Job ist SUCCEEDED und wird aus Duplikatschutzgründen niemals erneut verarbeitet.",
            )
            return
        try:
            self.controller.retry(job_id, reseed=reseed)
        except Exception as exc:
            QMessageBox.critical(self, "VM Automatic", str(exc))
        self._refresh_all()

    def _open_folder(self, folder: Path) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _open_logs(self) -> None:
        log_path = self.controller.log.log_path
        if log_path and log_path.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_path)))

    # ------------------------------------------------------------------ #
    def closeEvent(self, event: QCloseEvent) -> None:
        # Stopping the window stops the automation (no background magic).
        if self.controller.status != STATUS_STOPPED:
            answer = QMessageBox.question(
                self, "VM Automatic",
                "Automation stoppen und VM Automatic beenden?\n(Ein laufender Render wird abgebrochen und beim nächsten Start wiederhergestellt.)",
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
        self.controller.stop(cancel_running=True)
        event.accept()


def launch() -> int:
    from .applock import InstanceLockError, SingleInstanceLock

    app = QApplication(sys.argv)
    app.setApplicationName("VM Automatic")
    app.setOrganizationName("Local Video Tools")
    app.setStyle("Fusion")
    lock = SingleInstanceLock()
    try:
        lock.acquire()
    except InstanceLockError as exc:
        QMessageBox.critical(None, "VM Automatic", str(exc))
        return 1
    try:
        window = VMAutomaticWindow()
        window.show()
        return app.exec()
    finally:
        lock.release()
