# sqlglass

<!-- mcp-name: io.github.S-CurveLabs/sqlglass -->

An MCP server that gives GitHub Copilot (VS Code agent mode) — or any MCP client — what it lacks when writing SQL
for **SQL Server / Azure SQL**: the real schema, a safe way to try a query, the optimizer's opinion of it, and a
managed library of the queries you keep.

| Area | Tools |
|---|---|
| Schema (cached, works offline after first read) | `list_connections` `refresh_schema` `list_tables` `describe_table` `search_schema` `find_join_path` |
| Build & check (no database needed) | `build_select` `lint_sql` `analyze_sql` `format_sql` `translate_sql` |
| Execute, **read-only** | `run_query` `explain_query` `sample_table` `profile_table` |
| Writes, as a **dry run** | `preview_write` — an UPDATE / DELETE / INSERT becomes the SELECT that shows what it would do |
| New objects, as **script text** | `build_create_table` `build_procedure` `build_view` |
| Query library (`.sql` files in git) | `list_queries` `get_query` `save_query` `delete_query` `find_usage` `lint_library` `rename_in_library` `extract_parameter` `list_snapshots` `restore_snapshot` |

## Read-only, in layers

1. **Guard** – the statement is tokenised; anything but `SELECT` / `WITH … SELECT` (plus `DECLARE` / `SET @var`) is refused
   before a connection is opened. Because T-SQL needs no semicolons, *any* write/DDL/`EXEC` keyword anywhere outside a
   string, comment or `[quoted name]` rejects the batch — including `SELECT … INTO`, `OPENROWSET`, `sp_*`/`xp_*`, `WAITFOR`.
2. **Transaction** – every query runs with autocommit off and is always rolled back. The ODBC connection is opened
   read-only with `ApplicationIntent=ReadOnly`.
3. **Limits** – rows are capped (`max_rows`, default 200) and queries time out (`timeout_seconds`, default 30).
   `explain_query` uses `SET SHOWPLAN_XML ON`: the server compiles the query and executes nothing.
4. **Yours to add** – point the connection at a login that only has `db_datareader` (+ `GRANT SHOWPLAN` for plans).
   That is the layer the server itself enforces; use it for anything that matters.

Values go in as bound parameters (`params={"@Start": "2026-01-01"}`), not pasted into SQL text.
Passwords are never stored: SQL-auth connections name an environment variable (`password_env`).

## Writes and DDL: previewed and scripted, never executed

The server has no write mode. Instead:

**`preview_write`** turns one write statement into read-only SQL (plus a `COUNT(*)` of affected rows) and can run it:

```sql
UPDATE h SET h.Status = 'CLOSED' FROM dbo.PoHeader h JOIN dbo.Vendor v ON ... WHERE v.Country = 'US'
-- becomes
SELECT [h].[PoId], [h].[Status] AS [Status (current)], 'CLOSED' AS [Status (new)]
FROM dbo.PoHeader AS h JOIN dbo.Vendor AS v ON ... WHERE v.Country = 'US'
```

`UPDATE` → key columns + a current/new pair per SET column (`changed_only=true` hides no-op rows, NULL-safe via `EXCEPT`);
`DELETE` → the rows that would go, plus which child tables reference them; `INSERT` → the rows that would be added under
the target's column names, plus NOT NULL columns left unsupplied. `MERGE` is refused with advice to split it. The generated
text must itself pass the read-only guard before it is returned. Triggers, cascades and constraint failures are not simulated.

**`build_create_table` / `build_procedure` / `build_view`** return DDL *text* (`sql`, `undo_sql`, `notes`) for a person to
review and run in SSMS. Checked against the cached schema: the name must be free, foreign keys must reference a real
primary/unique key with matching types (and get an index), constraints get conventional names, `CREATE TABLE` is wrapped
in `IF OBJECT_ID(...) IS NULL`. `build_procedure(query="open-po-value-by-vendor")` wraps a library query and turns its
header params into typed procedure parameters; every `@variable` must be declared. `save_query(kind="script")` keeps a
script in the library — versioned with the queries, never linted as a query, never runnable through the server.

## Install

```powershell
pip install sqlglass          # or run it without installing: uvx sqlglass
```

Needs Python 3.11+ and a SQL Server ODBC driver. The legacy `SQL Server` driver that ships with Windows works for
Windows/SQL authentication; **Azure SQL / Entra ID sign-in needs "ODBC Driver 18 for SQL Server"**. On Linux/macOS,
install unixODBC plus Microsoft's ODBC driver.

Copy [`sqlglass.example.toml`](https://github.com/S-CurveLabs/sqlglass/blob/main/sqlglass.example.toml) to `sqlglass.toml` in your workspace (or in
`%LOCALAPPDATA%\sqlglass\`) and define your connections. Passwords never go in the file.

**VS Code / Copilot.** Add the server to the **user-level** `%APPDATA%\Code\User\mcp.json` so it works from every
window (a workspace `.vscode/mcp.json` only loads once you trust that workspace's MCP servers):

```json
"sqlglass": {
  "type": "stdio",
  "command": "uvx",
  "args": ["sqlglass"],
  "env": { "SQLGLASS_WORKSPACE": "C:\\path\\to\\your\\project", "REPORTING_SQL_PASSWORD": "${input:reporting-sql-pwd}" }
}
```

with a matching `"inputs": [{ "id": "reporting-sql-pwd", "type": "promptString", "password": true, "description": "..." }]`.
VS Code asks for the password once and keeps it in its secret storage — never put the value in the file, and do not
rely on a user environment variable: a VS Code process started before the variable existed will never see it.
Verified 2026-09-21 with Copilot agent mode (GPT-5.6): list_tables → describe_table ×3 → build_select → lint_sql →
explain_query → run_query, 19 steps, results identical to a direct run.

Config lookup order: `$SQLGLASS_CONFIG` → `$SQLGLASS_WORKSPACE\sqlglass.toml` → `.\sqlglass.toml` → `%LOCALAPPDATA%\sqlglass\sqlglass.toml`.
Schema cache and snapshots live under `%LOCALAPPDATA%\sqlglass\` (override with `SQLGLASS_HOME`).

## The query library

A folder of plain `.sql` files; the id is the path without `.sql`. Each opens with a header:

```sql
-- name: Open POs by vendor
-- description: Open purchase-order value per vendor since a start date.
-- connection: erp
-- tags: purchasing, monthly
-- param: @StartDate date = '2026-01-01' | first order date to include

SELECT v.Name, SUM(l.Amount) AS OpenValue
FROM dbo.PoHeader AS h
JOIN dbo.Vendor AS v ON v.VendorId = h.VendorId
...
WHERE h.OrderDate >= @StartDate
```

The body does **not** declare its parameters — the server does that when running it (in SSMS, add the `DECLARE`s
yourself). Git is the history; on top of that every write through the server takes a snapshot first
(`restore_snapshot` undoes it), returns a diff, and supports `dry_run`.

- `find_usage("dbo.Vendor", "Name")` – which saved queries break if this changes.
- `rename_in_library` – follow a table/column rename through every query, token-aware (strings/comments untouched).
- `lint_library` after `refresh_schema` – finds queries that reference tables/columns that no longer exist.

## Lint rules

Correctness: `join-without-on` `comma-join` `not-in-subquery` `left-join-filtered-in-where` `top-without-order-by`
`between-date-end` `undeclared-parameter` `unused-parameter` `parameter-declared-twice` `unused-cte`
· Performance: `non-sargable-predicate` `leading-wildcard-like` `select-star` `nolock` `distinct-over-join` `union-distinct`
· Style: `order-by-ordinal` `missing-schema-prefix` `unqualified-column`
· With a cached schema: `unknown-table` `unknown-column` (with did-you-mean).

## Layout

`src/sqlglass/` — `tsql/` (lexer, read-only guard, sqlglot analysis) → pure `lint` / `refactor` / `builder` / `preview` / `ddl` / `plan`
(SHOWPLAN XML summariser) / `schema` (model + cache + FK join paths) → `engines/` (`mssql` over pyodbc, `sqlite` for
tests and local files) → `library` + `snapshots` → `server.py` (the tools).

## Development

```powershell
git clone https://github.com/S-CurveLabs/sqlglass; cd sqlglass
python -m venv .venv
.venv\Scripts\pip install -e .[dev]
.venv\Scripts\pytest
```

Everything except the live SQL Server path runs against a SQLite fixture. To exercise `engines/mssql.py`, define a
connection and run `set SQLGLASS_TEST_CONNECTION=<name>` then `pytest -m mssql`.

## Not built yet

A write mode (by design), reading existing procedure / view definitions from the database, MERGE previews, actual (post-execution) plans and `STATISTICS IO`,
Postgres/MySQL engines, SQL embedded in Power Query (`Value.NativeQuery`) — the bridge to [letin](https://github.com/S-CurveLabs/letin).
