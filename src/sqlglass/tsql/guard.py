"""Read-only guard: decide from the tokens alone whether a batch is a pure read.

T-SQL does not require semicolons (``SELECT 1 DROP TABLE t`` is two statements), so
checking only how a statement *starts* is not enough. The guard is deliberately
blunt instead: any write/DDL/exec keyword anywhere outside a string, comment or
quoted identifier rejects the batch. A column really called ``update`` must be
written ``[update]`` -- which T-SQL demands anyway.

This is the first of two layers; the engine also runs everything inside a
transaction that is always rolled back.
"""

from __future__ import annotations

from ..errors import GuardError
from .lexer import PUNCT, VARIABLE, WORD, significant, split_batches, split_statements, tokenize

FORBIDDEN = frozenset("""
INSERT UPDATE DELETE MERGE TRUNCATE INTO
CREATE ALTER DROP
EXEC EXECUTE CALL
GRANT REVOKE DENY SETUSER REVERT
BACKUP RESTORE SHUTDOWN DBCC KILL RECONFIGURE CHECKPOINT
OPENROWSET OPENQUERY OPENDATASOURCE BULK
BEGIN COMMIT ROLLBACK SAVE USE
WAITFOR RAISERROR THROW PRINT
ENABLE DISABLE WRITETEXT UPDATETEXT READTEXT
ATTACH DETACH PRAGMA VACUUM REINDEX
""".split())

STARTERS = frozenset({"SELECT", "WITH", "DECLARE", "SET"})


def check_read_only(sql: str) -> None:
    """Raise GuardError unless every batch in ``sql`` is a plain read."""
    batches = split_batches(sql)
    if not batches:
        raise GuardError("There is no SQL statement to run (the text is empty or only comments).")
    for batch in batches:
        _check_batch(batch)


def _check_batch(batch: str) -> None:
    tokens = significant(tokenize(batch))
    for i, t in enumerate(tokens):
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        if t.kind != WORD:
            continue
        word = t.upper
        if word in FORBIDDEN:
            hint = " (SELECT ... INTO creates a table)" if word == "INTO" else ""
            raise GuardError(
                f"Refused: '{t.text}' is not allowed{hint}. This server only runs read-only SELECT statements; "
                f"writes, DDL and procedure calls are never sent to the database. "
                f"To see what an UPDATE / DELETE / INSERT would do, pass it to preview_write instead. "
                f"If this is a column or table name, quote it as [{t.text}].")
        if word.startswith(("SP_", "XP_")):
            raise GuardError(f"Refused: system procedure '{t.text}' cannot be called from a read-only session.")
        if word == "SET" and not (nxt and nxt.kind == VARIABLE):
            raise GuardError("Refused: only 'SET @variable = ...' is allowed; session SET options are not.")
        if word == "NEXT" and nxt and nxt.upper == "VALUE":
            raise GuardError("Refused: NEXT VALUE FOR advances a sequence, which is a write.")
    for stmt in split_statements(tokenize(batch)):
        first = stmt[0]
        ok = (first.kind == WORD and first.upper in STARTERS) or (first.kind == PUNCT and first.text == "(")
        if not ok:
            raise GuardError(
                f"Refused: a statement starts with '{first.text}'. Only SELECT / WITH ... SELECT "
                f"(optionally preceded by DECLARE / SET @variable) can be run.")
    if not any(t.kind == WORD and t.upper == "SELECT" for t in tokens):
        raise GuardError("Refused: the batch contains no SELECT, so there is nothing to read.")
