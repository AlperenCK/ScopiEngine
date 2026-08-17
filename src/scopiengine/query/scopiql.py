"""ScopiQL: a hand-written tokenizer and recursive-descent parser for the compact,
log-search-first query language.

No parser-generator dependency — the grammar is small enough that a plain
tokenizer plus one recursive-descent function per precedence level is both
simpler to read and easier to give genuinely helpful parse errors from (every
error names the offending character position and what was expected there,
rather than a bare "invalid syntax").

Grammar (informal)
-------------------
::

    query        := or_expr ( '|' stage )*
    or_expr      := and_expr ( OR and_expr )*
    and_expr     := not_expr ( AND? not_expr )*      # AND is implicit between adjacent terms
    not_expr     := (NOT | '!') not_expr | primary
    primary      := '(' or_expr ')'
                  | field ':' '(' or_expr ')'         # values inside share the field
                  | field ':' '[' value TO value ']'  # inclusive range
                  | ('>=' | '>' | '<=' | '<') value    # after field ':'; one-sided range
                  | field ':' STRING                   # phrase
                  | field ':' WORD                     # term, or prefix if WORD ends in '*'
                  | '_exists_' ':' field
                  | STRING                              # bare phrase, across every text field
                  | WORD                                # bare term, across every text field

    stage        := 'sort' sort_field (',' sort_field)*
                  | 'limit' INTEGER
                  | 'fields' field (',' field)*
                  | 'stats' 'count' '(' ')' 'by' field
    sort_field   := '-'? field                          # leading '-' means descending

``AND``/``OR``/``NOT`` are recognised in uppercase (matching Lucene/ES
``query_string`` convention); ``&&``/``||``/``!`` are equivalent shorthand.
A field name and a comparison/range apply to whatever the field is mapped as:
numeric and date fields are coerced and encoded via
:mod:`scopiengine.mapping.encoding`/:mod:`scopiengine.mapping.fieldtypes`; a
``text`` field is analyzed with its search analyzer. A field the index has no
mapping for is not a parse error — see :mod:`scopiengine.query.compiler` for
why the resulting query simply matches nothing.

``now``, relative to a caller-supplied instant
------------------------------------------------
``now``, ``now-1h``, ``now+30m`` and friends (units ``s``/``m``/``h``/``d``/``w``)
resolve against the ``now`` keyword argument to :func:`parse` (defaulting to
the real current time), so tests that use relative time are deterministic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import NoReturn

from scopiengine.analysis.registry import AnalyzerRegistry
from scopiengine.errors import InvalidQueryError
from scopiengine.mapping.mapping import Mapping
from scopiengine.query import compiler
from scopiengine.query.ast import Bool, MatchAll, Query

__all__ = ["ParsedQuery", "SortSpec", "StatsSpec", "parse"]


# -- pipeline stage results ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class SortSpec:
    """One ``sort`` stage field.

    Attributes:
        field: Field path to sort by, or ``"_score"`` for relevance order.
        descending: Whether a leading ``-`` was given.
    """

    field: str
    descending: bool = False


@dataclass(frozen=True, slots=True)
class StatsSpec:
    """A ``stats count() by <field>`` stage: the only aggregation ScopiQL 1.0 has."""

    by: str


@dataclass(frozen=True, slots=True)
class ParsedQuery:
    """Everything one ScopiQL string parses to: the AST plus any pipeline stages.

    Attributes:
        query: The compiled :mod:`scopiengine.query.ast` query.
        sort: ``sort`` stage fields, in the order given; empty means "relevance".
        limit: ``limit`` stage value, or ``None`` if absent.
        fields: ``fields`` stage projection, or ``None`` for the whole ``_source``.
        stats: ``stats`` stage spec, or ``None`` if absent.
    """

    query: Query
    sort: tuple[SortSpec, ...] = ()
    limit: int | None = None
    fields: tuple[str, ...] | None = None
    stats: StatsSpec | None = None


# -- tokenizer -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Token:
    kind: str
    text: str
    pos: int


#: Ordered so a longer operator (``>=``, ``||``) is tried before its shorter
#: prefix (``>``, ``|``) — alternation in Python's ``re`` picks the first
#: pattern that matches at the position, not the longest one.
_TOKEN_SPEC: tuple[tuple[str, str], ...] = (
    ("WS", r"[ \t\r\n]+"),
    ("STRING", r'"(?:\\.|[^"\\])*"'),
    ("GTE", r">="),
    ("LTE", r"<="),
    ("GT", r">"),
    ("LT", r"<"),
    ("AND2", r"&&"),
    ("OR2", r"\|\|"),
    ("NOT1", r"!"),
    ("LPAREN", r"\("),
    ("RPAREN", r"\)"),
    ("LBRACKET", r"\["),
    ("RBRACKET", r"\]"),
    ("PIPE", r"\|"),
    ("COLON", r":"),
    ("COMMA", r","),
    ("WORD", r'[^\s():\[\]"|,<>&!]+'),
)
_MASTER_RE = re.compile("|".join(f"(?P<{name}>{pattern})" for name, pattern in _TOKEN_SPEC))

#: WORD text recognised as a keyword when it matches exactly (case-sensitive,
#: matching the Lucene/ES ``query_string`` convention of uppercase operators).
_KEYWORDS = frozenset({"AND", "OR", "NOT"})

#: Token kinds that can open a ``not_expr`` — used to detect an implicit AND
#: between two adjacent primaries with no explicit operator between them.
_PRIMARY_START = frozenset({"LPAREN", "STRING", "WORD", "NOT"})


def _is_pure_negation(node: Query) -> bool:
    """Whether ``node`` is exactly the shape ``_not_expr`` produces for a bare ``NOT``/``!``:
    a :class:`~scopiengine.query.ast.Bool` carrying only ``must_not``.
    """
    return (
        isinstance(node, Bool)
        and bool(node.must_not)
        and not node.must
        and not node.should
        and not node.filter
    )


def _combine_and(clauses: list[Query]) -> Query:
    """Build the AND of several clauses, folding a bare ``NOT``'s ``Bool`` into
    the result's own ``must_not`` rather than nesting it inside ``must``.

    Without this, ``status:>=400 AND NOT level:WARN`` would compile to
    ``Bool(must=(Range(...), Bool(must_not=(Term(...),))))`` — and that inner
    ``Bool`` has no ``must``/``should``/``filter`` of its own, so the executor
    (see :mod:`scopiengine.index.searcher`) falls back to gating it on
    ``MatchAll``, which contributes a *constant `1.0` score* that then gets
    summed into the outer AND. The equivalent JSON DSL
    (``{"bool": {"must": [...], "must_not": [...]}}``) has no such extra
    constant term. Flattening here is what keeps ScopiQL and the DSL scoring
    identical for the same query — see the ScopiQL/DSL parity tests in
    ``tests/integration/test_api.py``.
    """
    must: list[Query] = []
    must_not: list[Query] = []
    for clause in clauses:
        if _is_pure_negation(clause):
            assert isinstance(clause, Bool)  # narrowed by _is_pure_negation, for mypy
            must_not.extend(clause.must_not)
        else:
            must.append(clause)
    return Bool(must=tuple(must), must_not=tuple(must_not))


def _unescape(text: str) -> str:
    """Undo backslash-escaping inside a quoted string token (``\\"`` and ``\\\\``)."""
    return re.sub(r"\\(.)", r"\1", text)


def _tokenize(text: str) -> list[_Token]:
    tokens: list[_Token] = []
    pos = 0
    length = len(text)
    while pos < length:
        match = _MASTER_RE.match(text, pos)
        if match is None:
            raise InvalidQueryError(
                f"ScopiQL parse error at position {pos}: unexpected character {text[pos]!r}",
                detail={"position": pos, "expected": "a valid token", "found": text[pos]},
            )
        kind = match.lastgroup
        value = match.group()
        if kind == "WS":
            pos = match.end()
            continue
        if kind == "STRING":
            tokens.append(_Token("STRING", _unescape(value[1:-1]), pos))
        elif kind == "AND2":
            tokens.append(_Token("AND", "AND", pos))
        elif kind == "OR2":
            tokens.append(_Token("OR", "OR", pos))
        elif kind == "NOT1":
            tokens.append(_Token("NOT", "NOT", pos))
        elif kind == "WORD" and value in _KEYWORDS:
            tokens.append(_Token(value, value, pos))
        else:
            assert kind is not None  # every branch of _TOKEN_SPEC is a named group
            tokens.append(_Token(kind, value, pos))
        pos = match.end()
    tokens.append(_Token("EOF", "", length))
    return tokens


# -- relative time -----------------------------------------------------------

_RELATIVE_TIME_RE = re.compile(r"^now(?:([+-])(\d+)([smhdw]))?$")

_UNIT_TO_TIMEDELTA_KWARG = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def _resolve_relative_time(value: str, now: datetime) -> str | None:
    """Resolve ``now``/``now-1h``/``now+30m`` against ``now``, as an ISO-8601 string.

    Returns ``None`` when ``value`` does not match the relative-time grammar,
    so the caller can fall back to treating it as a literal value.
    """
    match = _RELATIVE_TIME_RE.match(value)
    if match is None:
        return None
    sign, amount, unit = match.group(1), match.group(2), match.group(3)
    resolved = now
    if sign is not None:
        delta = timedelta(**{_UNIT_TO_TIMEDELTA_KWARG[unit]: int(amount)})
        resolved = now + delta if sign == "+" else now - delta
    return resolved.isoformat().replace("+00:00", "Z")


# -- parser ------------------------------------------------------------------


#: ``(sort, limit, fields, stats)`` — everything :meth:`_Parser.parse_stages` collects.
_Stages = tuple[tuple[SortSpec, ...], int | None, tuple[str, ...] | None, StatsSpec | None]


class _Parser:
    def __init__(
        self,
        tokens: list[_Token],
        mapping: Mapping,
        analyzers: AnalyzerRegistry,
        now: datetime,
    ) -> None:
        self._tokens = tokens
        self._mapping = mapping
        self._analyzers = analyzers
        self._now = now
        self._i = 0

    def _peek(self) -> _Token:
        return self._tokens[self._i]

    def _advance(self) -> _Token:
        token = self._tokens[self._i]
        if token.kind != "EOF":
            self._i += 1
        return token

    def _error(self, expected: str, token: _Token) -> NoReturn:
        found = "end of input" if token.kind == "EOF" else token.text
        raise InvalidQueryError(
            f"ScopiQL parse error at position {token.pos}: expected {expected}, found {found!r}",
            detail={"position": token.pos, "expected": expected, "found": token.text or None},
        )

    def _expect(self, kind: str, expected: str) -> _Token:
        token = self._peek()
        if token.kind != kind:
            self._error(expected, token)
        return self._advance()

    def _expect_value(self, expected: str) -> _Token:
        """A WORD or STRING token — the two kinds a bound/value can be spelled as."""
        token = self._peek()
        if token.kind not in ("WORD", "STRING"):
            self._error(expected, token)
        return self._advance()

    def _expect_word_text(self, text: str, expected: str) -> _Token:
        token = self._expect("WORD", expected)
        if token.text != text:
            self._error(expected, token)
        return token

    # -- boolean expression grammar, threading a default field context ------

    def parse_query(self) -> Query:
        return self._or_expr(default_field=None)

    def _or_expr(self, default_field: str | None) -> Query:
        clauses = [self._and_expr(default_field)]
        while self._peek().kind == "OR":
            self._advance()
            clauses.append(self._and_expr(default_field))
        return clauses[0] if len(clauses) == 1 else Bool(should=tuple(clauses))

    def _and_expr(self, default_field: str | None) -> Query:
        clauses = [self._not_expr(default_field)]
        while self._peek().kind == "AND" or self._peek().kind in _PRIMARY_START:
            if self._peek().kind == "AND":
                self._advance()
            clauses.append(self._not_expr(default_field))
        return clauses[0] if len(clauses) == 1 else _combine_and(clauses)

    def _not_expr(self, default_field: str | None) -> Query:
        if self._peek().kind == "NOT":
            self._advance()
            return Bool(must_not=(self._not_expr(default_field),))
        return self._primary(default_field)

    def _primary(self, default_field: str | None) -> Query:
        token = self._peek()
        if token.kind == "LPAREN":
            self._advance()
            inner = self._or_expr(default_field)
            self._expect("RPAREN", "')'")
            return inner
        if token.kind == "STRING":
            self._advance()
            return self._bare_value(default_field, token.text, is_phrase=True)
        if token.kind == "WORD":
            if self._tokens[self._i + 1].kind == "COLON":
                return self._field_expr()
            self._advance()
            return self._bare_value(default_field, token.text, is_phrase=False)
        self._error("a term, a phrase, a field, or '('", token)

    def _bare_value(self, default_field: str | None, text: str, *, is_phrase: bool) -> Query:
        if default_field is not None:
            if is_phrase:
                return compiler.phrase_query(self._mapping, self._analyzers, default_field, text)
            if len(text) > 1 and text.endswith("*"):
                return compiler.prefix_query(
                    self._mapping, self._analyzers, default_field, text[:-1]
                )
            return compiler.term_query(self._mapping, self._analyzers, default_field, text)
        if is_phrase:
            return compiler.multi_field_phrase(self._mapping, self._analyzers, text)
        return compiler.multi_field_term(self._mapping, self._analyzers, text)

    def _field_expr(self) -> Query:
        field_token = self._advance()  # the WORD already confirmed by the caller
        field = field_token.text
        self._expect("COLON", "':'")

        if field == "_exists_":
            value_token = self._expect("WORD", "a field name")
            return compiler.exists_query(value_token.text)

        token = self._peek()
        if token.kind == "LPAREN":
            self._advance()
            inner = self._or_expr(default_field=field)
            self._expect("RPAREN", "')'")
            return inner
        if token.kind == "LBRACKET":
            return self._range_brackets(field)
        if token.kind in ("GTE", "GT", "LTE", "LT"):
            return self._range_comparison(field)
        if token.kind == "STRING":
            self._advance()
            return compiler.phrase_query(self._mapping, self._analyzers, field, token.text)
        if token.kind == "WORD":
            self._advance()
            if len(token.text) > 1 and token.text.endswith("*"):
                return compiler.prefix_query(self._mapping, self._analyzers, field, token.text[:-1])
            value = _resolve_relative_time(token.text, self._now) or token.text
            return compiler.term_query(self._mapping, self._analyzers, field, value)
        self._error("a value, '(', '[', or a comparison operator", token)

    def _range_comparison(self, field: str) -> Query:
        op = self._advance().kind
        value_token = self._expect_value("a range value")
        value = _resolve_relative_time(value_token.text, self._now) or value_token.text
        bound_name = {"GTE": "gte", "GT": "gt", "LTE": "lte", "LT": "lt"}[op]
        return compiler.range_query(self._mapping, field, **{bound_name: value})

    def _range_brackets(self, field: str) -> Query:
        self._expect("LBRACKET", "'['")
        lo_token = self._expect_value("a range lower bound")
        self._expect_word_text("TO", "'TO'")
        hi_token = self._expect_value("a range upper bound")
        self._expect("RBRACKET", "']'")
        lo = _resolve_relative_time(lo_token.text, self._now) or lo_token.text
        hi = _resolve_relative_time(hi_token.text, self._now) or hi_token.text
        return compiler.range_query(self._mapping, field, gte=lo, lte=hi)

    # -- pipeline stages ------------------------------------------------------

    def parse_stages(self) -> _Stages:
        sort: tuple[SortSpec, ...] = ()
        limit: int | None = None
        fields: tuple[str, ...] | None = None
        stats: StatsSpec | None = None
        while self._peek().kind == "PIPE":
            self._advance()
            name_token = self._expect(
                "WORD", "a pipeline stage name ('sort', 'limit', 'fields', or 'stats')"
            )
            if name_token.text == "sort":
                sort = self._parse_sort_stage()
            elif name_token.text == "limit":
                limit = self._parse_limit_stage()
            elif name_token.text == "fields":
                fields = self._parse_fields_stage()
            elif name_token.text == "stats":
                stats = self._parse_stats_stage()
            else:
                self._error("'sort', 'limit', 'fields', or 'stats'", name_token)
        if self._peek().kind != "EOF":
            self._error("end of query or '|'", self._peek())
        return sort, limit, fields, stats

    def _parse_sort_stage(self) -> tuple[SortSpec, ...]:
        specs = [_sort_spec(self._expect("WORD", "a sort field").text)]
        while self._peek().kind == "COMMA":
            self._advance()
            specs.append(_sort_spec(self._expect("WORD", "a sort field").text))
        return tuple(specs)

    def _parse_limit_stage(self) -> int:
        token = self._expect("WORD", "an integer limit")
        try:
            value = int(token.text, 10)
        except ValueError:
            self._error("an integer limit", token)
        if value < 0:
            self._error("a non-negative integer limit", token)
        return value

    def _parse_fields_stage(self) -> tuple[str, ...]:
        names = [self._expect("WORD", "a field name").text]
        while self._peek().kind == "COMMA":
            self._advance()
            names.append(self._expect("WORD", "a field name").text)
        return tuple(names)

    def _parse_stats_stage(self) -> StatsSpec:
        self._expect_word_text("count", "'count'")
        self._expect("LPAREN", "'('")
        self._expect("RPAREN", "')'")
        self._expect_word_text("by", "'by'")
        field_token = self._expect("WORD", "a field name")
        return StatsSpec(by=field_token.text)


def _sort_spec(word: str) -> SortSpec:
    if word.startswith("-") and len(word) > 1:
        return SortSpec(field=word[1:], descending=True)
    return SortSpec(field=word)


# -- entry point ---------------------------------------------------------------


def parse(
    text: str,
    mapping: Mapping,
    analyzers: AnalyzerRegistry,
    *,
    now: datetime | None = None,
) -> ParsedQuery:
    """Parse a ScopiQL string into a query AST plus any pipeline stages.

    Args:
        text: The ScopiQL query. An empty or all-whitespace string parses to
            :class:`~scopiengine.query.ast.MatchAll`.
        mapping: The target index's mapping, used to resolve each field's type
            (so numerics/dates are encoded, and text is analyzed) and to
            expand a bare/unqualified term or phrase across every ``text`` field.
        analyzers: Registry used to analyze ``text``-field values.
        now: The instant ``now``/``now-1h``/``now+30m`` resolve against.
            Defaults to the real current time; pass a fixed value for
            deterministic tests.

    Returns:
        The parsed query AST plus any ``sort``/``limit``/``fields``/``stats``
        pipeline stages.

    Raises:
        InvalidQueryError: ``text`` is not valid ScopiQL. The message and the
            ``detail`` dict (``position``, ``expected``, ``found``) both name
            the offending character position and what was expected there.
    """
    if not text or not text.strip():
        return ParsedQuery(query=MatchAll())
    resolved_now = now if now is not None else datetime.now(UTC)
    parser = _Parser(_tokenize(text), mapping, analyzers, resolved_now)
    query = parser.parse_query()
    sort, limit, fields, stats = parser.parse_stages()
    return ParsedQuery(query=query, sort=sort, limit=limit, fields=fields, stats=stats)
