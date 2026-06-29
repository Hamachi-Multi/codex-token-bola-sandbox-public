# Security Notes

This toolkit reads local Codex transcripts and hook payloads. Those files may
contain prompts, source code, tool output, paths, and project names.

Default behavior for new captures stores bounded local text previews:

- prompt text previews are enabled for the first 800 characters
- instruction excerpts are enabled for the first 600 non-code-block characters
- tool output previews in analytics are disabled

Disable text previews before capturing sensitive prompts:

```bash
CODEX_TOKEN_USAGE_STORE_TEXT=0
```

Do not publish generated `raw/`, `normalized/`, `analytics/`, or
`state/` files.

`CODEX_TOKEN_USAGE_STORE_TEXT=0` affects new hook captures only. It does not
scrub prompt previews, instruction excerpts, tool output previews, paths, or
project names that were already written to `raw/`, `normalized/`, `analytics/`,
or `state/` artifacts.

This toolkit does not provide secret detection, masking, or scrub/export
features. Treat generated artifacts as local private data. If an artifact must
leave the machine, inspect and remove sensitive data outside this toolkit before
sharing it.

To check whether the analytics database still contains stored text previews:

```bash
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('analytics/token-usage.sqlite')
for table, column in [
    ("turns", "prompt_preview"),
    ("tool_call_samples", "output_preview"),
]:
    print(table, column, con.execute(
        f"select count(*) from {table} where length(coalesce({column},'')) > 0"
    ).fetchone()[0])
PY
```
