"""A small T-SQL lexer.

sqlglot gives us the tree; this gives us exact source positions, which is what the
read-only guard, GO batch splitting, parameter detection and token-aware edits need.
It never fails: unknown characters become single-character ``punct`` tokens and an
unterminated string/comment simply runs to the end of the text.
"""

from __future__ import annotations

from dataclasses import dataclass

WS, COMMENT, STRING, BRACKET, QIDENT, VARIABLE, NUMBER, WORD, PUNCT = (
    "ws", "comment", "string", "bracket", "qident", "variable", "number", "word", "punct")


@dataclass(frozen=True)
class Token:
    kind: str
    text: str
    start: int

    @property
    def end(self) -> int:
        return self.start + len(self.text)

    @property
    def upper(self) -> str:
        return self.text.upper()

    @property
    def significant(self) -> bool:
        return self.kind not in (WS, COMMENT)

    @property
    def ident(self) -> str | None:
        """The identifier this token names, unquoted; None for non-identifiers."""
        if self.kind == WORD:
            return self.text
        if self.kind == BRACKET:
            return self.text[1:-1].replace("]]", "]")
        if self.kind == QIDENT:
            return self.text[1:-1].replace('""', '"')
        return None


def _is_word_start(c: str) -> bool:
    return c.isalpha() or c in "_#"


def _is_word_char(c: str) -> bool:
    return c.isalnum() or c in "_#$@"


def tokenize(sql: str) -> list[Token]:
    out: list[Token] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        j = i + 1
        if c.isspace():
            while j < n and sql[j].isspace():
                j += 1
            kind = WS
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j < 0 else j
            kind = COMMENT
        elif sql.startswith("/*", i):  # T-SQL block comments nest
            depth, j = 1, i + 2
            while j < n and depth:
                if sql.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif sql.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            kind = COMMENT
        elif c == "'" or (c in "Nn" and j < n and sql[j] == "'"):
            j = _quoted(sql, i + (0 if c == "'" else 1), "'")
            kind = STRING
        elif c == "[":
            j = _quoted(sql, i, "]")
            kind = BRACKET
        elif c == '"':
            j = _quoted(sql, i, '"')
            kind = QIDENT
        elif c == "@":
            while j < n and _is_word_char(sql[j]):
                j += 1
            kind = VARIABLE if j > i + 1 else PUNCT
        elif c.isdigit() or (c == "." and j < n and sql[j].isdigit()):
            while j < n and (sql[j].isalnum() or sql[j] == "."):
                j += 1
            kind = NUMBER
        elif _is_word_start(c):
            while j < n and _is_word_char(sql[j]):
                j += 1
            kind = WORD
        else:
            kind = PUNCT
        out.append(Token(kind, sql[i:j], i))
        i = j
    return out


def _quoted(sql: str, open_at: int, close: str) -> int:
    """Index just past the closing delimiter; a doubled delimiter is an escape."""
    j, n = open_at + 1, len(sql)
    while j < n:
        if sql[j] == close:
            if j + 1 < n and sql[j + 1] == close:
                j += 2
                continue
            return j + 1
        j += 1
    return n


def significant(tokens: list[Token]) -> list[Token]:
    return [t for t in tokens if t.significant]


def split_batches(sql: str) -> list[str]:
    """Split on GO separators (a client-side convention, never sent to the server)."""
    tokens = tokenize(sql)
    cuts: list[tuple[int, int]] = []
    line_has_code = False
    for idx, t in enumerate(tokens):
        if t.kind == WS:
            if "\n" in t.text:
                line_has_code = False
            continue
        if t.kind == WORD and t.upper == "GO" and not line_has_code and _rest_of_line_empty(tokens, idx + 1):
            end = t.end
            for nxt in tokens[idx + 1:]:
                if nxt.kind == WS and "\n" in nxt.text:
                    break
                end = nxt.end
            cuts.append((t.start, end))
        if t.kind != COMMENT:
            line_has_code = True
    batches, pos = [], 0
    for start, end in cuts:
        batches.append(sql[pos:start])
        pos = end
    batches.append(sql[pos:])
    return [b.strip() for b in batches if significant(tokenize(b))]


def _rest_of_line_empty(tokens: list[Token], idx: int) -> bool:
    for t in tokens[idx:]:
        if t.kind == WS:
            if "\n" in t.text:
                return True
        elif t.kind == NUMBER or (t.kind == COMMENT and t.text.startswith("--")):
            continue  # "GO 5" and trailing line comments are still a separator
        else:
            return False
    return True


def split_statements(tokens: list[Token]) -> list[list[Token]]:
    """Split significant tokens on top-level semicolons."""
    out: list[list[Token]] = [[]]
    depth = 0
    for t in significant(tokens):
        if t.kind == PUNCT and t.text == "(":
            depth += 1
        elif t.kind == PUNCT and t.text == ")":
            depth = max(0, depth - 1)
        if t.kind == PUNCT and t.text == ";" and depth == 0:
            out.append([])
        else:
            out[-1].append(t)
    return [s for s in out if s]


def variables(sql: str) -> list[str]:
    """@variables referenced, in first-seen order, original casing; @@globals excluded."""
    seen: dict[str, str] = {}
    for t in tokenize(sql):
        if t.kind == VARIABLE and not t.text.startswith("@@"):
            seen.setdefault(t.text.lower(), t.text)
    return list(seen.values())


def declared_variables(sql: str) -> list[str]:
    out: dict[str, str] = {}
    for stmt in split_statements(tokenize(sql)):
        if stmt[0].kind != WORD or stmt[0].upper != "DECLARE":
            continue
        depth = 0
        for prev, t in zip(stmt, stmt[1:]):
            if t.kind == PUNCT and t.text in "()":
                depth += 1 if t.text == "(" else -1
            if t.kind == VARIABLE and depth == 0 and (prev.upper == "DECLARE" or prev.text == ","):
                out.setdefault(t.text.lower(), t.text)
    return list(out.values())


def quote_ident(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def quote_name(*parts: str | None) -> str:
    return ".".join(quote_ident(p) for p in parts if p)
