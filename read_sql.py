"""Parse the MySQL-family dataset SELECT subset before checking its grants."""

from __future__ import annotations

import re
from typing import NamedTuple

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
_NUMBER = re.compile(r"\d+(?:\.\d+)?(?:[Ee][+-]?\d+)?", re.ASCII)
_PARAMETER = re.compile(r":[A-Za-z_][A-Za-z_0-9]*")
_WHITESPACE = " \t\r\n\f\v"
_OPERATORS = frozenset(
    {
        "=",
        "<",
        ">",
        "<=",
        ">=",
        "<>",
        "!=",
        "<=>",
        "+",
        "-",
        "*",
        "/",
        "%",
        "||",
        "&&",
        "and",
        "or",
        "xor",
        "div",
        "mod",
        "like",
        "between",
    }
)
_COMPOUND_OPERATORS = ("<=>", "<=", ">=", "<>", "!=", "||", "&&", "::")
_UNSUPPORTED_SELECT_MODIFIERS = frozenset(
    {
        "distinctrow",
        "high_priority",
        "sql_small_result",
        "sql_big_result",
        "sql_buffer_result",
        "sql_cache",
        "sql_no_cache",
        "sql_calc_found_rows",
        "straight_join",
    }
)
_SELECT_BOUNDARIES = frozenset(
    {
        "from",
        "where",
        "group",
        "having",
        "order",
        "limit",
        "offset",
        "join",
        "inner",
        "cross",
        "left",
        "right",
        "full",
        "outer",
        "on",
        "using",
        "straight_join",
        "natural",
        "union",
        "intersect",
        "except",
        "partition",
        "use",
        "force",
        "ignore",
        "for",
        "into",
        "as",
        "window",
    }
)
_CONSTANTS = frozenset(
    {"true", "false", "null", "current_date", "current_time", "current_timestamp"}
)
_CAST_TYPES = frozenset(
    {"binary", "char", "date", "datetime", "decimal", "signed", "time", "unsigned"}
)
_EXTRACT_UNITS = frozenset(
    {"day", "hour", "microsecond", "minute", "month", "quarter", "second", "week", "year"}
)
_UNSUPPORTED = "Unsupported dataset SELECT syntax"


class Token(NamedTuple):
    kind: str
    value: str
    start: int
    end: int


class Source(NamedTuple):
    name: tuple[str, ...]
    alias: str | None


class Column(NamedTuple):
    name: tuple[str, ...]
    wildcard: bool = False
    using: bool = False


class Select(NamedTuple):
    sources: list[Source]
    columns: list[Column]
    sql: str


def _quoted_end(sql: str, start: int) -> int:
    quote = sql[start]
    end = start + 1
    while end < len(sql):
        if sql[end] == quote:
            if sql[end + 1 : end + 2] != quote:
                return end + 1
            end += 1
        end += 1
    raise ValueError("Unterminated SQL quoted value")


def tokenize(sql: str) -> list[Token]:
    if "\\" in sql or "$" in sql:
        raise ValueError(
            "Read SQL cannot contain backslashes or dollar-quote syntax "
            "(raw dollar characters); use bound parameters"
        )
    tokens: list[Token] = []
    start = 0
    while start < len(sql):
        char = sql[start]
        pair = sql[start : start + 2]
        if char in _WHITESPACE:
            start += 1
            continue
        if char == "#" or (pair == "--" and (start + 2 == len(sql) or ord(sql[start + 2]) <= 32)):
            end = start + 1
            while end < len(sql) and sql[end] not in "\r\n":
                end += 1
            tokens.append(Token("comment", "", start, end))
        elif pair == "/*":
            end = sql.find("*/", start + 2) + 2
            if end == 1:
                raise ValueError("Unterminated SQL block comment")
            tokens.append(Token("comment", "", start, end))
        elif char in ("'", "`", '"'):
            end = _quoted_end(sql, start)
            kind = "string" if char == "'" else "quoted" if char == "`" else "ambiguous"
            value = sql[start + 1 : end - 1].replace(char * 2, char)
            tokens.append(Token(kind, value, start, end))
        elif match := _IDENTIFIER.match(sql, start):
            end = match.end()
            tokens.append(Token("identifier", match[0], start, end))
        elif match := _NUMBER.match(sql, start):
            end = match.end()
            if end < len(sql) and (sql[end].isalpha() or sql[end] == "_"):
                raise ValueError(_UNSUPPORTED)
            tokens.append(Token("number", match[0], start, end))
        elif match := _PARAMETER.match(sql, start):
            end = match.end()
            tokens.append(Token("parameter", match[0][1:], start, end))
        else:
            operator = next(
                (value for value in _COMPOUND_OPERATORS if sql.startswith(value, start)), char
            )
            end = start + len(operator)
            tokens.append(Token("punctuation", operator, start, end))
        start = end
    return tokens


def strip_comments(sql: str) -> str:
    pieces: list[str] = []
    previous = 0
    for token in tokenize(sql):
        if token.kind == "comment":
            pieces.extend((sql[previous : token.start], " "))
            previous = token.end
    pieces.append(sql[previous:])
    return "".join(pieces)


class _SelectParser:
    def __init__(self, sql: str, functions: frozenset[str]):
        self.sql = sql
        self.tokens = [token for token in tokenize(sql) if token.kind != "comment"]
        self.tokens.append(Token("end", "", len(sql), len(sql)))
        self.position = 0
        self.functions = functions
        self.sources: list[Source] = []
        self.columns: list[Column] = []
        self.output_aliases: dict[str, tuple[str, int] | None] = {}
        self.order_positions: list[tuple[int, int, int]] = []
        self.has_projection_wildcard = False
        self.depth = 0

    @property
    def token(self) -> Token:
        return self.tokens[self.position]

    @property
    def syntax(self) -> str:
        if self.token.kind not in {"identifier", "punctuation"}:
            return ""
        return self.token.value.lower()

    def _take(self, value: str) -> bool:
        if self.syntax != value:
            return False
        self.position += 1
        return True

    def _expect(self, value: str) -> None:
        if not self._take(value):
            raise ValueError(_UNSUPPORTED)

    def _identifier(self) -> str:
        token = self.token
        if token.kind not in {"identifier", "quoted"} or not token.value:
            raise ValueError(_UNSUPPORTED)
        if token.kind == "quoted" and not all(
            char.isalnum() or char in "_ .-" for char in token.value
        ):
            raise ValueError("Unsupported quoted SQL identifier")
        self.position += 1
        return token.value

    def _alias(self, *, implicit: bool = False) -> str | None:
        if self._take("as"):
            if self.syntax in _SELECT_BOUNDARIES:
                raise ValueError(_UNSUPPORTED)
            return self._identifier()
        if (
            implicit
            and self.token.kind in {"identifier", "quoted"}
            and self.syntax not in _SELECT_BOUNDARIES
        ):
            return self._identifier()
        return None

    def _source(self) -> None:
        if self.token.kind not in {"identifier", "quoted"} or self.syntax in _SELECT_BOUNDARIES:
            raise ValueError("Could not confidently identify referenced resources")
        name = (self._identifier(),)
        if self._take("."):
            name += (self._identifier(),)
        self.sources.append(Source(name, self._alias(implicit=True)))

    def _join(self) -> bool:
        if self._take(","):
            self._source()
            return True
        if self._take("left") or self._take("right"):
            self._take("outer")
            self._expect("join")
        elif self._take("inner") or self._take("cross"):
            self._expect("join")
        elif not self._take("join"):
            return False
        self._source()
        if self._take("on"):
            self._expression()
        elif self._take("using"):
            self._expect("(")
            self.columns.append(Column((self._identifier(),), using=True))
            while self._take(","):
                self.columns.append(Column((self._identifier(),), using=True))
            self._expect(")")
        return True

    def _call(self, function: str) -> None:
        if function not in self.functions:
            raise ValueError("Only read-only SELECT statements are allowed")
        if function == "cast":
            self._expression()
            self._expect("as")
            cast_type = self.syntax
            if cast_type not in _CAST_TYPES:
                raise ValueError(_UNSUPPORTED)
            self.position += 1
            if cast_type in {"signed", "unsigned"}:
                self._take("integer")
            self._expect(")")
            return
        if function == "extract":
            if self.syntax not in _EXTRACT_UNITS:
                raise ValueError(_UNSUPPORTED)
            self.position += 1
            self._expect("from")
            self._expression()
            self._expect(")")
            return
        if function == "count" and self._take("*"):
            self._expect(")")
            return
        if self._take(")"):
            return
        self._take("distinct")
        self._expression_list()
        self._expect(")")

    def _atom(self, *, projection: bool) -> bool:
        """Consume an atom and return whether it was a projection wildcard."""
        while self.syntax in {"not", "+", "-", "~", "!"}:
            self.position += 1
            projection = False
        if self._take("("):
            self._expression_list()
            self._expect(")")
            return False
        if self._take("case"):
            if self.syntax != "when":
                self._expression()
            self._expect("when")
            while True:
                self._expression()
                self._expect("then")
                self._expression()
                if not self._take("when"):
                    break
            if self._take("else"):
                self._expression()
            self._expect("end")
            return False
        if self.token.kind in {"string", "parameter", "number"} or self.syntax in _CONSTANTS:
            self.position += 1
            return False
        if projection and self._take("*"):
            self.columns.append(Column((), wildcard=True))
            return True
        if self.syntax in _SELECT_BOUNDARIES or self.syntax == "select":
            raise ValueError(_UNSUPPORTED)
        first = self.token
        name = (self._identifier(),)
        if self.syntax == "(":
            if first.kind != "identifier" or first.end != self.token.start:
                raise ValueError(_UNSUPPORTED)
            self.position += 1
            self._call(name[0].lower())
            return False
        while self._take("."):
            if projection and self._take("*"):
                self.columns.append(Column(name, wildcard=True))
                return True
            name += (self._identifier(),)
            if len(name) > 3:
                raise ValueError(_UNSUPPORTED)
        self.columns.append(Column(name))
        return False

    def _expression(self, *, projection: bool = False) -> None:
        self.depth += 1
        if self.depth > 64:
            raise ValueError("Dataset expression is too deeply nested")
        wildcard = self._atom(projection=projection)
        while not wildcard:
            if self._take("is"):
                self._take("not")
                if self.syntax not in {"null", "true", "false", "unknown"}:
                    raise ValueError(_UNSUPPORTED)
                self.position += 1
            elif self._take("in"):
                self._expect("(")
                self._expression_list()
                self._expect(")")
            elif self.syntax in _OPERATORS:
                self.position += 1
                self._atom(projection=False)
            elif self._take("not"):
                if self._take("in"):
                    self._expect("(")
                    self._expression_list()
                    self._expect(")")
                elif self._take("like") or self._take("between"):
                    self._atom(projection=False)
                else:
                    raise ValueError(_UNSUPPORTED)
            else:
                break
        self.depth -= 1

    def _expression_list(self) -> None:
        self._expression()
        while self._take(","):
            self._expression()

    def _projection(self) -> None:
        ordinal = 1
        while True:
            self._expression(projection=True)
            alias = self._alias()
            if alias is not None:
                key = alias.casefold()
                self.output_aliases[key] = None if key in self.output_aliases else (alias, ordinal)
            if not self._take(","):
                break
            ordinal += 1
        self.has_projection_wildcard = any(column.wildcard for column in self.columns)

    def _order_item(self) -> None:
        token = self.token
        position = self.output_aliases.get(token.value.casefold())
        if (
            not self.has_projection_wildcard
            and token.kind in {"identifier", "quoted"}
            and self.syntax not in _CONSTANTS
            and position is not None
            and position[0] == token.value
        ):
            following = self.tokens[self.position + 1]
            standalone = (
                following.kind == "end"
                or (
                    following.kind == "identifier"
                    and following.value.lower() in {"asc", "desc", "limit"}
                )
                or (following.kind == "punctuation" and following.value in {",", ";"})
            )
            if standalone:
                # The projected expression was already checked. Use its ordinal
                # so a same-named physical column cannot change SQL resolution.
                self.order_positions.append((token.start, token.end, position[1]))
                self.position += 1
                return
        self._expression()

    def _order_by(self) -> None:
        while True:
            self._order_item()
            self._take("asc") or self._take("desc")
            if not self._take(","):
                return

    def _rewritten_sql(self) -> str:
        pieces: list[str] = []
        end = 0
        for start, stop, ordinal in self.order_positions:
            pieces.extend((self.sql[end:start], str(ordinal)))
            end = stop
        return "".join((*pieces, self.sql[end:]))

    def parse(self) -> Select:
        self._expect("select")
        self._take("distinct") or self._take("all")
        if self.syntax in _UNSUPPORTED_SELECT_MODIFIERS:
            raise ValueError(_UNSUPPORTED)
        self._projection()
        self._expect("from")
        self._source()
        while self._join():
            pass
        if self._take("where"):
            self._expression()
        if self._take("group"):
            self._expect("by")
            self._expression_list()
        if self._take("having"):
            self._expression()
        if self._take("order"):
            self._expect("by")
            self._order_by()
        if self._take("limit"):
            self._integer()
            if self._take("offset"):
                self._integer()
        self._take(";")
        if self.token.kind != "end":
            raise ValueError(_UNSUPPORTED)
        return Select(self.sources, self.columns, self._rewritten_sql())

    def _integer(self) -> None:
        if (
            self.token.kind != "number"
            or not self.token.value.isascii()
            or not self.token.value.isdecimal()
        ):
            raise ValueError(_UNSUPPORTED)
        self.position += 1


def parse_select(sql: str, functions: frozenset[str]) -> Select:
    return _SelectParser(sql, functions).parse()
