r"""FFmpeg filtergraph value escaping.

Verified empirically against the shipped FFmpeg builds (johnvansickle
7.0.2-static and BtbN 8.x with drawtext) by probing the exact value the
parser delivers (the scale filter's evaluation error echoes it).

A ``-filter_complex`` string is parsed in two passes before a value reaches
a filter option:

* Pass 1 (``filter_parse`` in libavfilter/graphparser.c): the option string
  of one filter is tokenized with term ``[];,``.  Backslash escapes are
  consumed there (``\\X`` -> ``X``) and single-quoted spans are copied
  VERBATIM (their quotes are consumed; no escaping happens inside).
* Pass 2 (``ff_filter_opt_parse`` -> ``av_opt_get_key_value`` in
  libavfilter/avfilter.c): each ``key=value`` pair is re-tokenized with
  term ``:``; backslash escapes and quotes are applied a second time.

Consequences:

* An UNQUOTED value is escaped twice -> use :func:`escape_unquoted_value`.
* A QUOTED value (``'...'``) is only escaped in pass 2 -> single-level
  escaping (see ``command_builder._filter_path``), and it can never contain
  an apostrophe at all (pass 1's quote mode ends at the first raw ``'``).

Therefore drawtext ``text`` values are emitted UNQUOTED with the
two-level table (apostrophes such as German/English contractions require
this); drawtext ``fontfile`` values use the same table without the
``%%`` step (no drawtext text expansion happens on paths); paths in
``subtitles=filename=...`` stay quoted with single-level escaping.

Escape table (unquoted, two passes), escaping backslashes FIRST:

    backslash    ->  \\\\
    apostrophe   ->  \\\\'
    colon        ->  \\:
    comma        ->  \,     (pass 1 only; pass 2 splits on ':' only)
    semicolon    ->  \;     (pass 1 only)
    '[' / ']'    ->  \[ / \] (pass 1 only)

Everything else (spaces, '=', '&', umlauts, '%%', ...) passes through as-is.
Percent signs are NOT doubled: all drawtext instances in this project are
emitted with ``expansion=none`` (see the Stage-2 artwork filter graph), which keeps the text
byte-for-byte literal and also avoids the modern engine's hard "Stray %"
failure mode (n8.x: a stray '%' makes drawtext log an error and render
nothing at all, while the process still exits 0).
"""

from __future__ import annotations


def escape_unquoted_value(value: str) -> str:
    """Escape ``value`` for an UNQUOTED filter option value (two passes)."""
    out = value.replace("\\", "\\\\\\\\")
    out = out.replace("'", "\\\\\\'")
    out = out.replace(":", "\\\\:")
    for ch in ",;[]":
        out = out.replace(ch, "\\" + ch)
    return out


def escape_drawtext_text(value: str) -> str:
    """Escape ``value`` for an unquoted drawtext ``text=`` value.

    Identical to :func:`escape_unquoted_value`: percent signs are NOT
    doubled because every drawtext in this project is emitted with
    ``expansion=none`` (byte-for-byte literal text).  Kept as a distinct
    name so call sites express intent and a future re-introduction of
    ``expansion=normal`` must deliberately change one place.
    """
    return escape_unquoted_value(value)


def escape_quoted_value(value: str) -> str:
    """Escape ``value`` for use INSIDE a single-quoted span (one pass).

    Mirrors ``command_builder._filter_path`` behaviour: single-level
    escaping, since pass 1 copies quoted spans verbatim.  Callers must
    guarantee the value contains no apostrophe (a quoted span cannot
    contain one; raise ValueError instead of emitting a broken graph).
    """
    if "'" in value:
        raise ValueError("quoted filter value must not contain an apostrophe")
    out = value.replace("\\", "\\\\")
    out = out.replace(":", "\\:")
    for ch in ",;[]":
        out = out.replace(ch, "\\" + ch)
    return out


# --------------------------------------------------------------------------- #
# 1.3.0: Windows-proof file-path strategy for the subtitles/fontsdir/drawtext
# fontfile filter options (the real drive-letter/backslash/space/umlaut
# problem).  The root cause of the classic Windows failures is that an
# absolute path has to survive BOTH filtergraph parser passes AND the C
# runtime/libass ``fopen`` with the system code page: a ``C:\\Users\\Käthe\\…``
# prefix can break in any of those layers depending on the FFmpeg build.
#
# The fix removes the failure surface instead of escaping harder:
#
# 1. Render-time files referenced by the filtergraph (staged ASS subtitle
#    file and bundled fonts directory) are always created
#    by this application itself under the project root with app-controlled
#    ASCII names (``temp/<stem>_burn.ass``, ``tools/fonts``).  When the file
#    lies under the anchor directory (the FFmpeg working directory), the
#    filter receives a RELATIVE POSIX path: no drive-letter colon, no
#    backslash, no space, no non-ASCII byte can remain — immune to both
#    parser passes, to the Windows code page and to libass ``fopen``.  This
#    holds for EVERY location a user unpacks the project to, including
#    ``C:\\Users\\Jürgen Müller\\Downloads\\VideoMerger_Final_1.3.0``.
# 2. Paths that cannot be made relative fall back to an UNQUOTED absolute
#    value with forward slashes and the verified two-level escape table.
#    Unlike the 1.2.4 quoted form, an unquoted value can represent an
#    apostrophe (e.g. ``C:/Users/O'Brien/…``), so path-dependent renders no
#    longer raise ValueError and never emit a broken quoted span.
# --------------------------------------------------------------------------- #

from pathlib import Path as _Path
import re as _re

_WINDOWS_ABSOLUTE = _re.compile(r"^[A-Za-z]:[\\/]")
_WINDOWS_UNC = _re.compile(r"^\\\\[^\\/]+")


def normalize_filter_path_text(value: str | _Path) -> str:
    """Normalize any path text to forward-slash absolute POSIX form.

    Windows drive paths (``C:\\...``) and UNC paths (``\\\\server\\...``) are
    normalized as pure strings: ``Path.resolve()`` on a non-Windows host
    would otherwise treat ``C:`` as a relative segment and mangle the path.
    """
    text = str(value).strip()
    if _WINDOWS_ABSOLUTE.match(text) or _WINDOWS_UNC.match(text):
        return text.replace("\\", "/")
    return str(_Path(value).expanduser().resolve()).replace("\\", "/")


def relative_filter_path(value: str | _Path, anchor: str | _Path) -> str | None:
    """Return an ASCII-safe relative POSIX filter value under ``anchor``.

    Returns None when the path does not lie under ``anchor`` or when the
    relative remainder still contains a non-ASCII character, a space, a
    colon, an apostrophe or another character that could stress either
    parser pass (the caller then uses the absolute fallback).  App-staged
    render-time files always produce a pure ``[a-z0-9_./-]+`` value.
    """
    target = _Path(value).expanduser().resolve()
    anchor_path = _Path(anchor).expanduser().resolve()
    try:
        relative = target.relative_to(anchor_path)
    except ValueError:
        return None
    if not str(relative) or str(relative) == ".":
        return None
    text = relative.as_posix()
    if any(not (ch.isascii() and (ch.isalnum() or ch in "./-_")) for ch in text):
        return None
    # A leading "./" is unnecessary; a bare "../" escape can never happen
    # after relative_to(), which is the point of the anchor.
    return text


def escape_absolute_filter_path(value: str | _Path) -> str:
    """Escape an absolute path as an UNQUOTED filter value (two passes).

    Forward slashes first, then the verified two-level table.  Handles
    drive-letter colons (``C:``), backslash remnants, spaces, umlauts and
    apostrophes without raising.
    """
    return escape_unquoted_value(normalize_filter_path_text(value))


def filter_file_value(value: str | _Path, anchor: str | _Path | None) -> str:
    """The one entry point for file paths inside the filtergraph.

    ``anchor`` is the working directory the FFmpeg process will run in
    (the project root).  Prefer the safe relative form; otherwise the
    escaped absolute form.  The returned value must be emitted UNQUOTED.
    """
    if anchor is not None:
        relative = relative_filter_path(value, anchor)
        if relative is not None:
            return relative
    return escape_absolute_filter_path(value)
