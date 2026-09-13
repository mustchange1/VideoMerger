"""VM Automatic – Video Merger Automatic.

A companion application that automates the existing VideoMerger workflow:
it watches an inbox folder for complete audio/script jobs, waits for file
stability, keeps a persistent sequential queue, randomizes the eligible
VideoMerger clip order per job and launches the existing VideoMerger render
pipeline (unchanged). It never replaces, re-implements or modifies
VideoMerger's rendering, settings or user configuration.
"""

__version__ = "1.0.0"
