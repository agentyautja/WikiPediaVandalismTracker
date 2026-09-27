"""Word-level diff between two versions of an article's wikitext."""

import difflib
import re
from dataclasses import dataclass, field

_TOKEN = re.compile(r"\w+|\s+|[^\w\s]")
MAX_WORD_PAIRS = 3_000_000   # bigger than this: diff line by line first so huge edits stay fast
CONTEXT_CHARS = 80
MAX_SEGMENT_CHARS = 600
MAX_HUNKS = 5


@dataclass
class Diff:
    hunks: list = field(default_factory=list)   # per hunk: [[op, text], ...] with op "=", "-" or "+"
    added: str = ""                             # all text that was added
    removed: str = ""                           # all text that was removed
    added_sample: str = ""                      # a distinctive piece of added text, to spot it later
    removed_sample: str = ""
    new_context: str = ""                       # the new version of each changed spot, with some context


def diff_texts(old, new):
    a, b = _TOKEN.findall(old), _TOKEN.findall(new)
    # Most edits touch one small spot of a long article: skip the identical start and end.
    limit = min(len(a), len(b))
    start = 0
    while start < limit and a[start] == b[start]:
        start += 1
    end = 0
    while end < limit - start and a[-1 - end] == b[-1 - end]:
        end += 1
    segments = _segments(a[start:len(a) - end], b[start:len(b) - end])
    added = [text for op, text in segments if op == "+"]
    removed = [text for op, text in segments if op == "-"]
    hunks = _hunks("".join(a[:start]), segments, "".join(a[len(a) - end:]))
    return Diff(
        hunks=hunks,
        added="\n".join(added),
        removed="\n".join(removed),
        added_sample=_sample(added),
        removed_sample=_sample(removed),
        new_context="\n".join("".join(text for op, text in hunk if op != "-") for hunk in hunks),
    )


def _segments(a, b):
    if len(a) * len(b) <= MAX_WORD_PAIRS:
        return _word_segments(a, b)
    # A huge change: compare lines first, then words inside the changed lines.
    a_lines = "".join(a).splitlines(keepends=True)
    b_lines = "".join(b).splitlines(keepends=True)
    segments = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a_lines, b_lines, autojunk=False).get_opcodes():
        if tag == "equal":
            segments.append(["=", "".join(a_lines[i1:i2])])
            continue
        old_part, new_part = "".join(a_lines[i1:i2]), "".join(b_lines[j1:j2])
        ta, tb = _TOKEN.findall(old_part), _TOKEN.findall(new_part)
        if len(ta) * len(tb) <= MAX_WORD_PAIRS:
            segments.extend(_word_segments(ta, tb))
        else:
            segments += [["-", old_part]] if old_part else []
            segments += [["+", new_part]] if new_part else []
    return segments


def _word_segments(a, b):
    raw = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            raw.append(["=", "".join(a[i1:i2])])
        if tag in ("delete", "replace"):
            raw.append(["-", "".join(a[i1:i2])])
        if tag in ("insert", "replace"):
            raw.append(["+", "".join(b[j1:j2])])
    # Glue changes separated only by a space or a single character into one readable change.
    merged, minus, plus = [], [], []

    def flush():
        if minus:
            merged.append(["-", "".join(minus)])
        if plus:
            merged.append(["+", "".join(plus)])
        minus.clear()
        plus.clear()

    for i, (op, text) in enumerate(raw):
        if op == "-":
            minus.append(text)
        elif op == "+":
            plus.append(text)
        elif (minus or plus) and i + 1 < len(raw) and len(text) <= 3 and len(text.strip()) <= 1:
            if minus:
                minus.append(text)
            if plus:
                plus.append(text)
        else:
            flush()
            merged.append(["=", text])
    flush()
    return merged


def _hunks(before, segments, after):
    full = [["=", before]] + segments + [["=", after]]
    hunks, current = [], None
    for i, (op, text) in enumerate(full):
        if op != "=":
            if current is None:
                current = []
                context = _tail(full[i - 1][1]) if full[i - 1][0] == "=" else ""
                if context:
                    current.append(["=", context])
            current.append([op, _clip(text)])
        elif current is not None:
            next_is_change = i + 1 < len(full) and full[i + 1][0] != "="
            if next_is_change and len(text) <= 2 * CONTEXT_CHARS and "\n\n" not in text:
                current.append(["=", text])   # short gap between two changes: keep them together
            else:
                context = _head(text)
                if context:
                    current.append(["=", context])
                hunks.append(current)
                current = None
    if current:
        hunks.append(current)
    return hunks[:MAX_HUNKS]


def _tail(text):
    line = text.rsplit("\n", 1)[-1]
    if len(line) <= CONTEXT_CHARS:
        return line
    cut = line[-CONTEXT_CHARS:]
    space = cut.find(" ")
    return "…" + (cut[space + 1:] if 0 <= space < 20 else cut)


def _head(text):
    line = text.split("\n", 1)[0]
    if len(line) <= CONTEXT_CHARS:
        return line
    cut = line[:CONTEXT_CHARS]
    space = cut.rfind(" ")
    return (cut[:space] if space > CONTEXT_CHARS - 20 else cut) + "…"


def _clip(text):
    if len(text) <= MAX_SEGMENT_CHARS:
        return text
    return text[:MAX_SEGMENT_CHARS] + f"… [+{len(text) - MAX_SEGMENT_CHARS:,} more characters]"


def _sample(parts):
    lines = [line.strip() for part in parts for line in part.splitlines()]
    lines = [line for line in lines if len(line) >= 6]
    return max(lines, key=len)[:150] if lines else ""
