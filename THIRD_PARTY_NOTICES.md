# Third-party notices

VideoMerger installs and uses the following third-party projects:

- **FFmpeg** – downloaded by the Windows setup from the Gyan.dev builds page. FFmpeg licensing depends on build configuration. See <https://ffmpeg.org/legal.html> and the notices delivered by that build.
- **PySide6 / Qt for Python** – installed from PyPI. Licensed under LGPLv3/GPL/commercial terms. See <https://www.qt.io/qt-licensing>.
- **Python** – Python Software Foundation License. See <https://docs.python.org/3/license.html>.
- **faster-whisper** – installed from PyPI; MIT License. See <https://github.com/SYSTRAN/faster-whisper>.
- **fontTools** – installed from PyPI; MIT License. Used locally to read selected-font advance metrics. See <https://github.com/fonttools/fonttools>.
- **Noto Sans Regular/Bold** – bundled in `tools/fonts` under the SIL Open Font License 1.1; the complete license is `tools/fonts/OFL.txt`. Source: <https://github.com/notofonts/noto-fonts>.
- **Eveleth Clean** – commercial/proprietary and **not bundled**. VideoMerger only detects a user-installed licensed copy and otherwise uses the Noto Sans fallback.
- **CTranslate2** – installed as a faster-whisper dependency; MIT License. See <https://github.com/OpenNMT/CTranslate2>.
- **Whisper model weights** – downloaded during setup through the faster-whisper/Hugging Face mechanism and stored locally under `tools/alignment_models`; model license/source metadata is retained in that cache.

FFmpeg binaries are not embedded in this source ZIP. The setup downloads them directly from the named distributor and records the source URL in `tools/ffmpeg/SOURCE.txt`.
