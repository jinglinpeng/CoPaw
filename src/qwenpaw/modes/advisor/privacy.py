# -*- coding: utf-8 -*-
"""Keep tool data on the worker's side of Advisor Mode.

The advisor is a remote model; the worker runs next to the data. With
privacy on, everything sent to the advisor passes one gate.

* **Provenance.** :class:`AdvisorPrivacy` reads the worker's context in
  order. A value is data only when it first appeared in a tool result.
  User messages, system prompts, the worker's own code and prose and the
  advisor's replies are public, so an alias or a print template the worker
  wrote before running it stays readable even after it shows up in a
  result.
* **Masking.** Data in an outgoing text is replaced by placeholders, the
  same value always by the same one: ``<DATE_n>`` dates, ``<V_n>`` whole
  values, ``<T_n>`` fragments of longer result text, ``<ID_n>`` identifiers
  and their fragments, ``<C_n>`` short all-caps codes and ``<N_n>``
  numbers. Numbers above ``small_int_max`` that are not in the task or the
  advisor's replies are masked whatever their origin, because a derived
  number (a sum, a ratio) never appears verbatim in a result.
* **Restoring.** Placeholders in the advisor's reply are turned back into
  the real values before the worker reads it.
* **Withheld results.** A successful tool result is never forwarded, only
  described by :meth:`AdvisorPrivacy.describe_result`. A failed result's
  error text is masked and forwarded.

The gate protects against accidental disclosure by an honest worker. It
does not stop a paraphrase, and it is not built for a worker that is
steered into smuggling data out.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from typing import Any, Callable, Iterable

PLACEHOLDER_RE = re.compile(r"<(DATE|ID|V|T|C|N)_(\d+)>")
# The tail of a streamed reply that may still grow into a placeholder.
_PARTIAL_PLACEHOLDER_RE = re.compile(r"<(?:[A-Z]{1,4}(?:_\d*)?)?$")

# Masking hides finished placeholders and result handles behind sentinels
# from the Unicode private use area, so later rules cannot touch them.
_SENTINEL_OPEN = ""
_SENTINEL_CLOSE = ""
_SENTINEL_BASE = 0xE100
_SENTINEL_RE = re.compile(f"{_SENTINEL_OPEN}(..){_SENTINEL_CLOSE}", re.S)
_PRIVATE_USE = "-"

_WORD_RE = re.compile(r"[A-Za-z0-9]+")
_TERM_RE = re.compile(r"[#@]?[A-Za-z0-9][A-Za-z0-9_.@/:>+\-]*")
_TOKEN_RE = re.compile(
    rf"(?<![\w<{_PRIVATE_USE}])#?[A-Za-z0-9]"
    rf"(?:[A-Za-z0-9_\-]*[A-Za-z0-9])?(?![\w>{_PRIVATE_USE}])",
    re.ASCII,
)
_DATE_RE = re.compile(
    r"(?<![\w<])\d{4}[-/]\d{1,2}(?:[-/]\d{1,2})?"
    r"(?:T[\d:.]+(?:Z|[+-]\d{2}:?\d{2})?)?(?![\w>])",
    re.ASCII,
)
_NUMBER_RE = re.compile(
    rf"(?<![\w.<{_PRIVATE_USE}])(?:\d{{1,3}}(?:,\d{{3}})+|\d+)(?:\.\d+)?",
    re.ASCII,
)
_CAPS_RE = re.compile(r"(?<![\w<])[A-Z]{2,4}(?![\w>])", re.ASCII)
_QUOTED_RE = re.compile(r"'([^'\n]{1,200})'|\"([^\"\n]{1,200})\"")
_JSON_STRING_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')
_NUMERICISH_RE = re.compile(r"[\d\s.,:/+\-%$T]*")
_STRUCTURED_START_RE = re.compile(r"(?m)^[ \t]*[\[{]")
# Word-character classes above are ASCII-only, so a value or number
# written right next to Chinese text is still found.
_JSON_PREFIX_RE = re.compile(r"\[\s*[\[{\"]|\{\s*\"")
_FIELD_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NAME_RUN_RE = re.compile(r"\b[A-Z][\w'’-]*(?:[ \t]+[A-Z][\w'’-]*)+")

# Values too generic to treat as data.
_LITERAL_STOP = frozenset(
    {"none", "null", "true", "false", "nan", "n/a", "yes", "no", "ok"},
)
# All-caps words that are vocabulary, not codes.
_CAPS_STOP = frozenset(
    {
        "AND",
        "OR",
        "NOT",
        "ID",
        "IDS",
        "SQL",
        "THE",
        "FOR",
        "API",
        "JSON",
        "NULL",
        "TRUE",
        "FALSE",
        "ASC",
        "DESC",
        "LIMIT",
        "WHERE",
        "FROM",
        "JOIN",
        "LEFT",
        "IN",
        "IS",
        "AS",
        "ON",
        "BY",
        "OK",
        "CSV",
        "URL",
        "UTC",
        "NONE",
        "NAN",
        "ALL",
        "ANY",
        "MAX",
        "MIN",
        "AVG",
        "SUM",
        "COUNT",
        "CASE",
        "WHEN",
        "THEN",
        "ELSE",
        "END",
        "NA",
    },
)

_MAX_VALUE_CHARS = 80
_MAX_NAME_RUN_WORDS = 6
_MAX_CODE_CHARS = 64
_MIN_CODE_CHARS = 5
_MAX_COLUMNS_SHOWN = 12

_MISSING = object()


def _norm(text: str) -> str:
    return text.strip().lstrip("#").strip().lower()


def _codeish(token: str) -> bool:
    """An identifier rather than a word: letters with digits, or a
    lowercase letter followed by an uppercase run (``NIliIAG``). Camel-case
    words such as ``GitHub`` do not count."""
    if re.search(r"\d", token) and re.search(r"[A-Za-z]", token):
        return True
    return re.search(r"[a-z][A-Z]{2,}", token) is not None


def _value_edge(text: str, index: int, *, left: bool) -> bool:
    """Whether ``text[index]`` can border a masked value: outside the
    text, or not an ASCII word character, a placeholder bracket or a hidden
    span. A ``#`` may precede a value (IDs often carry one). Non-ASCII
    letters count as borders, so values inside Chinese text are found."""
    if left and 0 <= index < len(text) and text[index] == "#":
        index -= 1
    if index < 0 or index >= len(text):
        return True
    char = text[index]
    return not (
        (char.isascii() and char.isalnum())
        or char in ("_<" if left else "_>")
        or "" <= char <= ""
    )


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def _windows(words: list[str], n: int) -> Iterable[str]:
    for i in range(len(words) - n + 1):
        yield " ".join(words[i : i + n])


def _bucket(count: int, unit: str) -> str:
    if count == 0:
        return f"no {unit}s"
    if count == 1:
        return f"1 {unit}"
    for upper, label in (
        (10, "2-10"),
        (100, "11-100"),
        (1000, "101-1,000"),
        (10000, "1,001-10,000"),
    ):
        if count <= upper:
            return f"{label} {unit}s"
    return f"more than 10,000 {unit}s"


def _strings(data: Any) -> Iterable[tuple[str, bool]]:
    """Every string in a JSON value as ``(text, is_key)``. Keys are
    included because pivoted results put data in the keys."""
    if isinstance(data, str):
        yield data, False
    elif isinstance(data, dict):
        for key, value in data.items():
            yield str(key), True
            yield from _strings(value)
    elif isinstance(data, list):
        for item in data:
            yield from _strings(item)


def _field_name(key: str) -> bool:
    """A key shaped like a field identifier (``application_number``,
    ``rowCount``, ``state``) names a column, not a value. Capitalised words
    and all-caps keys (``Sports``, ``PA``) are left as data, and so are
    record ids used as keys (``ka0Wt000000Eo6cIAC``), which mix digits with
    capitals."""
    if not _FIELD_NAME_RE.fullmatch(key):
        return False
    if any(c.isdigit() for c in key) and any(c.isupper() for c in key):
        return False
    return "_" in key or key[:1].islower()


def parse_structured(
    text: str,
) -> tuple[Any, list[tuple[str, bool]] | None]:
    """Find a JSON value in a tool result.

    Returns ``(value, None)`` when one parses, ``(_MISSING, literals)``
    when JSON starts but is cut off (a preview), with the string literals
    that did arrive as ``(text, is_key)``, and ``(_MISSING, None)`` for
    plain text.
    """
    decoder = json.JSONDecoder()
    starts = [m.end() - 1 for m in _STRUCTURED_START_RE.finditer(text)]
    for pos in starts:
        try:
            value, _end = decoder.raw_decode(text, pos)
        except ValueError:
            continue
        return value, None
    # Cut-off JSON (a preview) still starts like JSON: ``[{``, ``["``,
    # ``[[`` or ``{"``. Other bracketed lines are plain text.
    cut = [p for p in starts if _JSON_PREFIX_RE.match(text, p)]
    if cut:
        literals = []
        for match in _JSON_STRING_RE.finditer(text, cut[0]):
            try:
                literal = json.loads(f'"{match.group(1)}"')
            except ValueError:
                literal = match.group(1)
            is_key = text[match.end() : match.end() + 3].lstrip()[:1] == ":"
            literals.append((literal, is_key))
        return _MISSING, literals
    return _MISSING, None


class AdvisorPrivacy:
    """Provenance index, masking gate and placeholder map of one
    conversation. Kept across the requests of that conversation."""

    def __init__(self, config: Any) -> None:
        self.config = config
        pattern = getattr(config, "reference_pattern", "") or ""
        self._ref_re = re.compile(pattern) if pattern else None
        self._ngram = max(3, int(config.shingle_words))
        # Provenance: what the advisor may read.
        self._observed: set[str] = set()
        self._public_terms: set[str] = set()
        self._public_ngrams: set[str] = set()
        self._public_numbers: set[str] = set()
        self._public_dates: set[str] = set()
        # Word n-grams and terms any tool result contained: the worker
        # repeating them later does not make them public.
        self._result_ngrams: set[str] = set()
        self._result_terms: set[str] = set()
        # Data: what only the worker may read.
        self._values_by_word: dict[str, set[str]] = {}
        self._code_parts: set[str] = set()
        self._short: set[str] = set()
        self._fragments: Counter[str] = Counter()
        # Distinct record texts counted into ``_fragments``: a row fetched
        # again by another query is still one record.
        self._records: set[str] = set()
        # Placeholders.
        self._placeholder_by_key: dict[tuple[str, str], str] = {}
        self._surface: dict[str, str] = {}
        self._kind_counts: Counter[str] = Counter()
        # Context blocks already read (see the middleware's scan).
        self.scanned_blocks: set[str] = set()

    # ── observation ─────────────────────────────────────────────────────

    def _first_time(self, kind: str, text: str) -> bool:
        digest = hashlib.sha1(
            f"{kind}\0{text}".encode("utf-8", "replace"),
        ).hexdigest()
        if digest in self._observed:
            return False
        self._observed.add(digest)
        return True

    def observe_public(
        self,
        text: str,
        *,
        numbers: bool = False,
        declassify: bool = False,
    ) -> None:
        """Text the advisor may read anyway.

        ``numbers`` also whitelists its numbers and dates (the task, the
        advisor's replies). ``declassify`` releases data values the text
        repeats: what the user writes is theirs to share.
        """
        if not text:
            return
        mode = f"public:{int(numbers)}{int(declassify)}"
        if not self._first_time(mode, text):
            return
        self._add_public(text)
        if numbers:
            self._public_dates.update(
                m.group(0) for m in _DATE_RE.finditer(text)
            )
            self._public_numbers.update(
                m.group(0).replace(",", "") for m in _NUMBER_RE.finditer(text)
            )
        if declassify:
            self._declassify(text)

    def observe_worker(self, text: str) -> None:
        """The worker's own code and prose: public where it came first.

        Words the worker copied from an earlier result (a name it found in
        a description, say) stay data even when it writes them first as a
        whole value.
        """
        if text and self._first_time("worker", text):
            self._add_public(text, worker=True)

    def observe_result(self, text: str, *, failed: bool = False) -> None:
        """Index the data a tool result showed the worker.

        A failed result's error text is fixed wording around quoted values
        and identifiers, so only those are indexed.
        """
        if not text or not self._first_time(f"result:{int(failed)}", text):
            return
        if failed:
            for match in _QUOTED_RE.finditer(text):
                quoted = match.group(1) or match.group(2) or ""
                self._note_result_text(quoted)
                self._add_value(_norm(quoted))
            for match in _TOKEN_RE.finditer(text):
                self._add_code(match.group(0))
            return
        data, partial = parse_structured(text)
        if data is not _MISSING:
            records: Iterable[tuple[str, bool]] = _strings(data)
        elif partial is not None:
            records = partial
        else:
            for record in text.splitlines():
                self._add_record(record, free_text=True)
            return
        for record, is_key in records:
            self._add_structured(record, is_key)

    def _add_structured(
        self, record: str, is_key: bool, depth: int = 0
    ) -> None:
        """One string of a JSON result. Keys shaped like field names are
        schema; strings that hold JSON themselves (license lists, nested
        info columns) are unpacked."""
        if is_key and _field_name(record):
            self._add_public(record)
            return
        self._add_record(record, free_text=False)
        stripped = record.strip()
        if depth < 3 and stripped[:1] in ("[", "{"):
            try:
                inner = json.loads(stripped)
            except ValueError:
                return
            for text, key in _strings(inner):
                self._add_structured(text, key, depth + 1)

    def _note_result_text(self, text: str) -> None:
        low = text.lower()
        words = _WORD_RE.findall(low)
        for n in range(1, self._ngram + 1):
            self._result_ngrams.update(_windows(words, n))
        for term in _TERM_RE.findall(low):
            self._result_terms.add(term.lstrip("#@"))

    def _add_public(self, text: str, *, worker: bool = False) -> None:
        low = text.lower()
        words = _WORD_RE.findall(low)
        for n in range(1, self._ngram + 1):
            grams = set(_windows(words, n))
            if worker:
                # The worker repeating a result, or a piece of a result id
                # (an id's tail it matched on), does not make it its own.
                grams = {
                    gram
                    for gram in grams - self._result_ngrams
                    if not any(w in self._code_parts for w in gram.split())
                }
            self._public_ngrams.update(grams)
        terms = {t for t in _TERM_RE.findall(low)}
        terms |= {t.lstrip("#@") for t in terms}
        terms |= {
            _norm(m.group(1) or m.group(2) or "")
            for m in _QUOTED_RE.finditer(low)
        }
        if worker:
            terms -= self._result_terms
            terms -= self._code_parts
        self._public_terms.update(terms)

    def _is_public(self, value: str) -> bool:
        if value in self._public_terms:
            return True
        words = _WORD_RE.findall(value)
        if not words:
            return True
        n = self._ngram
        if len(words) <= n:
            return " ".join(words) in self._public_ngrams
        return all(w in self._public_ngrams for w in _windows(words, n))

    def _add_record(self, record: str, *, free_text: bool) -> None:
        raw = record.strip()
        if not raw:
            return
        self._note_result_text(raw)
        value = _norm(raw)
        if not _NUMERICISH_RE.fullmatch(value):
            self._add_value(value)
        if 2 <= len(raw) <= 4 and raw.isalpha() and raw.isupper():
            self._add_short(raw)
        # Inside longer text there are no cell boundaries: runs of
        # capitalised words (names, titles) stand in for values, with their
        # sub-runs, so "QuantumPCB Modeler" is found in "The QuantumPCB
        # Modeler".
        for match in _NAME_RUN_RE.finditer(raw):
            run = match.group(0).split()[:_MAX_NAME_RUN_WORDS]
            for i in range(len(run)):
                for j in range(i + 2, len(run) + 1):
                    self._add_value(_norm(" ".join(run[i:j])))
        if free_text:
            for match in _QUOTED_RE.finditer(raw):
                self._add_value(_norm(match.group(1) or match.group(2) or ""))
            for match in _CAPS_RE.finditer(raw):
                self._add_short(match.group(0))
        for match in _TOKEN_RE.finditer(raw):
            self._add_code(match.group(0))
        words = _words(value)
        n = int(self.config.shingle_words)
        if len(words) >= n:
            digest = hashlib.sha1(
                value.encode("utf-8", "replace"),
            ).hexdigest()
            if digest in self._records:
                return
            self._records.add(digest)
            for fragment in set(_windows(words, n)):
                if fragment not in self._public_ngrams:
                    self._fragments[fragment] += 1

    def _add_value(self, value: str) -> None:
        if (
            len(value) < int(self.config.min_value_chars)
            or len(value) > _MAX_VALUE_CHARS
            or value in _LITERAL_STOP
            or _NUMERICISH_RE.fullmatch(value)
            or self._is_public(value)
        ):
            return
        key = max(_WORD_RE.findall(value), key=len)
        self._values_by_word.setdefault(key, set()).add(value)

    def _add_code(self, token: str) -> None:
        bare = token.lstrip("#")
        low = bare.lower()
        if (
            not _MIN_CODE_CHARS <= len(low) <= _MAX_CODE_CHARS
            or not _codeish(bare)
            or self._is_public(low)
        ):
            return
        self._code_parts.add(low)
        for k in range(_MIN_CODE_CHARS, len(low)):
            self._code_parts.add(low[:k])
            self._code_parts.add(low[-k:])

    def _add_short(self, token: str) -> None:
        low = token.lower()
        if token not in _CAPS_STOP and low not in self._public_ngrams:
            self._short.add(low)

    def _declassify(self, text: str) -> None:
        low = text.lower()
        for word in set(_WORD_RE.findall(low)):
            values = self._values_by_word.get(word)
            if values:
                values.difference_update({v for v in values if v in low})
        for match in _TOKEN_RE.finditer(text):
            self._code_parts.discard(match.group(0).lstrip("#").lower())
        for match in _CAPS_RE.finditer(text):
            self._short.discard(match.group(0).lower())
        threshold = int(self.config.format_min_records)
        for fragment in _windows(_words(text), int(self.config.shingle_words)):
            if fragment in self._fragments:
                self._fragments[fragment] = threshold

    # ── masking ─────────────────────────────────────────────────────────

    def mask(self, text: str, *, numbers: bool = True) -> str:
        """Replace the data in ``text`` with placeholders.

        ``numbers=False`` leaves numbers and dates alone, for texts whose
        numbers are not the worker's (system prompts).
        """
        if not text:
            return text
        vault: list[str] = []

        def hide(match: re.Match) -> str:
            index = len(vault)
            vault.append(match.group(0))
            return (
                _SENTINEL_OPEN
                + chr(_SENTINEL_BASE + index // 1024)
                + chr(_SENTINEL_BASE + index % 1024)
                + _SENTINEL_CLOSE
            )

        def protect(value: str) -> str:
            return PLACEHOLDER_RE.sub(hide, value)

        out = protect(text)
        if self._ref_re is not None:
            out = self._ref_re.sub(hide, out)
        if numbers:
            out = protect(self._mask_dates(out))
        out = protect(self._mask_values(out))
        out = protect(self._mask_fragments(out))
        out = protect(self._mask_codes(out))
        out = protect(self._mask_short(out))
        if numbers:
            out = protect(self._mask_numbers(out))

        def reveal(match: re.Match) -> str:
            hi, lo = match.group(1)
            index = (
                (ord(hi) - _SENTINEL_BASE) * 1024 + ord(lo) - _SENTINEL_BASE
            )
            return vault[index]

        return _SENTINEL_RE.sub(reveal, out)

    def _placeholder(self, kind: str, key: str, surface: str) -> str:
        found = self._placeholder_by_key.get((kind, key))
        if found is None:
            self._kind_counts[kind] += 1
            found = f"<{kind}_{self._kind_counts[kind]}>"
            self._placeholder_by_key[(kind, key)] = found
            self._surface[found] = surface
        return found

    def _mask_dates(self, text: str) -> str:
        def sub(match: re.Match) -> str:
            token = match.group(0)
            if token in self._public_dates:
                return token
            return self._placeholder("DATE", token, token)

        return _DATE_RE.sub(sub, text)

    def _mask_values(self, text: str) -> str:
        low = text.lower()
        candidates: set[str] = set()
        for word in set(_WORD_RE.findall(low)):
            for value in self._values_by_word.get(word, ()):
                if value in low:
                    candidates.add(value)
        if not candidates:
            return text
        if len(low) != len(text):
            # Lower-casing changed the length (rare Unicode): match per
            # value with a regex instead of by position.
            return self._mask_values_slow(text, candidates)
        # Longest values first; a shorter one inside a longer match is
        # already covered.
        spans: list[tuple[int, int, str]] = []
        for value in sorted(candidates, key=len, reverse=True):
            start = 0
            while True:
                i = low.find(value, start)
                if i < 0:
                    break
                j = i + len(value)
                start = j
                if not (
                    _value_edge(text, i - 1, left=True)
                    and _value_edge(text, j, left=False)
                ) or any(a < j and i < b for a, b, _ in spans):
                    continue
                spans.append((i, j, value))
        out: list[str] = []
        last = 0
        for i, j, value in sorted(spans):
            begin = i - 1 if i > 0 and text[i - 1] == "#" else i
            out.append(text[last:begin])
            out.append(self._placeholder("V", value, text[i:j]))
            last = j
        out.append(text[last:])
        return "".join(out)

    def _mask_values_slow(self, text: str, candidates: set[str]) -> str:
        for value in sorted(candidates, key=len, reverse=True):
            pattern = re.compile(
                rf"(?<![A-Za-z0-9_<{_PRIVATE_USE}])#?{re.escape(value)}"
                rf"(?![A-Za-z0-9_>{_PRIVATE_USE}])",
                re.IGNORECASE,
            )
            text = pattern.sub(
                lambda m, v=value: self._placeholder(
                    "V",
                    v,
                    m.group(0).lstrip("#"),
                ),
                text,
            )
        return text

    def _mask_fragments(self, text: str) -> str:
        if not self._fragments:
            return text
        n = int(self.config.shingle_words)
        threshold = int(self.config.format_min_records)
        tokens = list(_WORD_RE.finditer(text))
        cover = [False] * len(tokens)
        for i in range(len(tokens) - n + 1):
            window = tokens[i : i + n]
            gaps = (
                text[a.end() : b.start()] for a, b in zip(window, window[1:])
            )
            if any(_SENTINEL_OPEN in gap for gap in gaps):
                continue
            fragment = " ".join(t.group(0).lower() for t in window)
            count = self._fragments.get(fragment)
            if count is not None and count < threshold:
                for j in range(i, i + n):
                    cover[j] = True
        out: list[str] = []
        last = i = 0
        while i < len(tokens):
            if not cover[i]:
                i += 1
                continue
            j = i
            while (
                j + 1 < len(tokens)
                and cover[j + 1]
                and _SENTINEL_OPEN
                not in text[tokens[j].end() : tokens[j + 1].start()]
            ):
                j += 1
            start, end = tokens[i].start(), tokens[j].end()
            span = text[start:end]
            out.append(text[last:start])
            out.append(self._placeholder("T", " ".join(_words(span)), span))
            last = end
            i = j + 1
        out.append(text[last:])
        return "".join(out)

    def _mask_codes(self, text: str) -> str:
        def sub(match: re.Match) -> str:
            token = match.group(0)
            bare = token.lstrip("#")
            low = bare.lower()
            if (
                len(low) < _MIN_CODE_CHARS
                or not _codeish(bare)
                or low not in self._code_parts
                or self._is_public(low)
            ):
                return token
            return self._placeholder("ID", low, bare)

        return _TOKEN_RE.sub(sub, text)

    def _mask_short(self, text: str) -> str:
        def sub(match: re.Match) -> str:
            token = match.group(0)
            low = token.lower()
            if (
                token in _CAPS_STOP
                or low not in self._short
                or low in self._public_ngrams
            ):
                return token
            return self._placeholder("C", low, token)

        return _CAPS_RE.sub(sub, text)

    def _mask_numbers(self, text: str) -> str:
        limit = int(self.config.small_int_max)

        def sub(match: re.Match) -> str:
            token = match.group(0)
            plain = token.replace(",", "")
            if plain in self._public_numbers:
                return token
            if "." not in plain and int(plain) <= limit:
                return token
            return self._placeholder("N", plain, token)

        return _NUMBER_RE.sub(sub, text)

    # ── restoring ───────────────────────────────────────────────────────

    def restore(self, text: str) -> str:
        """Turn placeholders back into the values they stand for."""
        if not text:
            return text
        return PLACEHOLDER_RE.sub(
            lambda m: self._surface.get(m.group(0), m.group(0)),
            text,
        )

    def restore_prefix(self, text: str) -> str:
        """:meth:`restore` for a reply still streaming in.

        A trailing piece that may grow into a placeholder is held back, so
        each result is a prefix of the next one.
        """
        partial = _PARTIAL_PLACEHOLDER_RE.search(text or "")
        cut = partial.start() if partial else len(text or "")
        return self.restore((text or "")[:cut])

    def restoring(
        self,
        on_text: Callable[[str], None] | None,
    ) -> Callable[[str], None] | None:
        """Wrap a cumulative-text callback so it receives restored text."""
        if on_text is None:
            return None

        def wrapped(text: str) -> None:
            on_text(self.restore_prefix(text))

        return wrapped

    # ── describing ──────────────────────────────────────────────────────

    def describe_result(self, text: str) -> str:
        """What the advisor learns about a successful tool result: its
        shape, never its content."""
        data, partial = parse_structured(text or "")
        if data is not _MISSING:
            if isinstance(data, list):
                desc = _bucket(len(data), "row")
                columns: list[str] = []
                for row in data:
                    if isinstance(row, dict):
                        columns = [str(k) for k in row.keys()]
                        break
                if columns:
                    shown = [
                        self.mask(c) for c in columns[:_MAX_COLUMNS_SHOWN]
                    ]
                    more = len(columns) - len(shown)
                    desc += "; columns: " + ", ".join(shown)
                    if more > 0:
                        desc += f" (+{more} more)"
            elif isinstance(data, dict):
                keys = [self.mask(str(k)) for k in data.keys()]
                desc = "an object"
                if keys:
                    desc += " with keys: " + ", ".join(
                        keys[:_MAX_COLUMNS_SHOWN]
                    )
                    if len(keys) > _MAX_COLUMNS_SHOWN:
                        desc += f" (+{len(keys) - _MAX_COLUMNS_SHOWN} more)"
            else:
                desc = "a single value"
        elif partial is not None:
            desc = (
                "a large structured result, only a preview reached the worker"
            )
        else:
            desc = f"text output, {_bucket(len(text or ''), 'character')}"
        return f"[result kept on the worker's side: {desc}]"

    # ── audit ───────────────────────────────────────────────────────────

    @staticmethod
    def placeholder_counts(text: str) -> dict[str, int]:
        """Placeholders per kind in ``text``."""
        counts: Counter[str] = Counter(
            m.group(1) for m in PLACEHOLDER_RE.finditer(text or "")
        )
        return dict(counts)


__all__ = ["AdvisorPrivacy", "PLACEHOLDER_RE", "parse_structured"]
