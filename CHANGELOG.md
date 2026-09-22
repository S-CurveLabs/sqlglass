# Changelog

## 0.1.0 — 2026-09-21

First public release (developed as `sql-mcp`).

- Schema tools with an offline cache: `list_tables`, `describe_table`, `search_schema`, `find_join_path`.
- Build and check without a database: `build_select`, `lint_sql` (20+ rules), `analyze_sql`, `format_sql`, `translate_sql`.
- Read-only execution behind a token-level guard and an always-rolled-back transaction: `run_query`, `explain_query`, `sample_table`, `profile_table`.
- `preview_write`: an UPDATE / DELETE / INSERT becomes the SELECT that shows what it would change.
- DDL script generators: `build_create_table`, `build_procedure`, `build_view`.
- A git-friendly `.sql` query library with snapshots, diffs, impact analysis and token-aware renames.
