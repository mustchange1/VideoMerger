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
emitted with ``expansion=none`` (see quote.py), which keeps the text
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
