# Security Notes

This toolkit reads local Codex transcripts and hook payloads. Those files may
contain prompts, source code, tool output, paths, and project names.

Default behavior for new captures stores bounded local text previews:

- prompt text previews are enabled for the first 800 characters

Disable text previews before capturing sensitive prompts:

```bash
BOLA_PROMPT_PREVIEW_CHARS=0
```

Do not publish generated `raw/`, `normalized/`, `analytics/`, or
`state/` files.

`BOLA_PROMPT_PREVIEW_CHARS=0` affects new hook captures only. It does not
scrub prompt previews, paths, or project names already written to `raw/`,
`normalized/`, `analytics/`, or `state/` artifacts.

This toolkit does not provide secret detection, masking, or scrub/export
features. Treat generated artifacts as local private data. If an artifact must
leave the machine, inspect and remove sensitive data outside this toolkit before
sharing it.

To check whether the analytics database still contains stored prompt previews,
run `bola paths show`, copy `effective.output_dir`, and use its analytics
database:

```bash
BOLA_DB=/effective/output_dir/analytics/bola.sqlite python3 - <<'PY'
import os
import pathlib
import sqlite3
db = pathlib.Path(os.environ["BOLA_DB"]).expanduser()
with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as con:
    print("turns", "prompt_preview", con.execute(
        "select count(*) from turns where length(coalesce(prompt_preview,'')) > 0"
    ).fetchone()[0])
PY
```
