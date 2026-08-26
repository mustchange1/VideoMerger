class VideoMergerError(Exception):
    """Base exception for errors that can be shown to the user."""


class DependencyError(VideoMergerError):
    pass


class MediaAnalysisError(VideoMergerError):
    pass


class ExportError(VideoMergerError):
    pass


class ExportCancelled(ExportError):
    pass


class ValidationError(ExportError):
    pass
