# Runtime log archive

The runtime `data/` directory is normally ignored because it grows continuously.
This repository snapshot includes the available logs for analysis.

Two raw JSONL files exceeded GitHub's 100 MB per-file limit, so they are stored
as gzip archives:

- `archive/legacy-history/stage4-active/20261008T183437.174683Z-1791488100.jsonl.gz`
- `active-rounds/20261117T154305.235173Z-1794930300.jsonl.gz`

Restore either one with:

```bash
gzip -dk path/to/file.jsonl.gz
```

The local `.env` file is intentionally excluded because it contains credentials.
