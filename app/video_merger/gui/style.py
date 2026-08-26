APP_STYLE = r"""
QMainWindow, QWidget { background: #111722; color: #e8edf5; font-family: 'Segoe UI'; font-size: 10pt; }
QScrollArea { border: none; }
QGroupBox { border: 1px solid #2a3547; border-radius: 9px; margin-top: 14px; padding: 14px 10px 10px; font-weight: 600; background: #161e2b; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; color: #9fc2ff; }
QLineEdit, QComboBox, QDoubleSpinBox, QSpinBox, QPlainTextEdit, QTableWidget {
  background: #0d131c; border: 1px solid #344157; border-radius: 6px; padding: 6px; color: #edf3fc;
}
QLineEdit:focus, QComboBox:focus, QDoubleSpinBox:focus, QSpinBox:focus { border: 1px solid #4f8cff; }
QPushButton { background: #25334a; border: 1px solid #3a4b67; border-radius: 7px; padding: 8px 14px; color: #f1f5fb; }
QPushButton:hover { background: #304360; }
QPushButton:disabled { color: #6e7888; background: #1a2230; border-color: #273244; }
QPushButton#mergeButton { background: #2874f0; border: none; font-weight: 700; font-size: 12pt; padding: 13px; }
QPushButton#mergeButton:hover { background: #3c83f5; }
QPushButton#cancelButton { background: #7a2c38; }
QProgressBar { background: #0b1018; border: 1px solid #344157; border-radius: 7px; height: 20px; text-align: center; }
QProgressBar::chunk { background: #2f80ed; border-radius: 6px; }
QHeaderView::section { background: #202b3c; color: #cdd8e8; padding: 6px; border: none; }
QTableWidget { gridline-color: #273347; }
QSlider::groove:horizontal { height: 5px; background: #2b374a; border-radius: 2px; }
QSlider::handle:horizontal { background: #5c95f5; width: 16px; margin: -6px 0; border-radius: 8px; }
QCheckBox, QRadioButton { spacing: 7px; }
QLabel#title { font-size: 20pt; font-weight: 700; color: white; }
QLabel#subtitle { color: #98a7ba; }
QLabel#dropHint { color: #86a6d6; border: 1px dashed #425779; border-radius: 7px; padding: 7px; }
"""
