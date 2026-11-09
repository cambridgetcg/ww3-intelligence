# ledger/

`status.json` is written by the scheduled run of `ww3-export.yml`, once a week,
and by nothing else. It records what that export found in the collector's
ledger:

| field | meaning |
|---|---|
| `observations` | rows in the `observation` table |
| `max_id` | the highest observation id |
| `by_source` | rows per registered source ID |
| `first_fetched_at`, `last_fetched_at` | the earliest and latest capture times, UTC |
| `db_sha256` | the SHA-256 of the exported database file |
| `export_run_id` | the workflow run that made the export |

These are counts of what the collector stored, not of what happened in the
world. A source's count says how many distinct records and revisions were
captured from it, not how much it published, and nothing here weighs a record's
truth or importance. The file holds no publisher text; the database it
describes is kept encrypted in the archive (`ops/ARCHIVE.md`).

The commit also exists so that the repository shows activity: GitHub disables
a public repository's scheduled workflows after 60 days without any.
