"""One-time migration from multi-strategy records to the retained history."""

import json
import shutil
from pathlib import Path

from .prediction import analytic_signal


LEGACY_OUTPUT = "btc_guard_v4"
REMOVED_FIELDS = ("model_version", "model_outputs", "model_probabilities")


def _read_jsonl(path):
    rows = []
    try:
        with path.open() as source:
            for line in source:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return rows


def _clean_decision(row):
    cleaned = dict(row)
    had_legacy_output = LEGACY_OUTPUT in (cleaned.get("model_outputs") or {})
    for field in REMOVED_FIELDS:
        cleaned.pop(field, None)
    if had_legacy_output:
        result = analytic_signal(
            cleaned.get("components") or {},
            cleaned.get("quotes") or {},
            float(cleaned.get("fee_rate") or 0),
        )
        cleaned.update(result)
    return cleaned


def prepare_history(data):
    """Archive retired history and create the retained-strategy history once."""
    data = Path(data)
    target = data / "round-history.jsonl"
    sources = sorted(data.glob("stage4-round-history*.jsonl"))
    if not sources:
        target.touch(exist_ok=True)
        return {"migrated": False, "retained_rows": 0}

    source_rows = []
    for path in sources:
        source_rows.extend(_read_jsonl(path))
    decisions = [row for row in source_rows if row.get("record_type") == "decision"]
    legacy_starts = [
        int(row.get("start") or 0) for row in decisions
        if LEGACY_OUTPUT in (row.get("model_outputs") or {})
    ]
    if not legacy_starts:
        raise RuntimeError("Retained-strategy history could not be identified")
    last_legacy_start = max(legacy_starts)
    retained_slugs = {
        row.get("slug") for row in decisions
        if (LEGACY_OUTPUT in (row.get("model_outputs") or {})
            or (int(row.get("start") or 0) > last_legacy_start
                and "model_outputs" not in row))
    }
    retained_slugs.discard(None)

    retained = []
    seen = set()
    for row in sorted(source_rows, key=lambda value: value.get("recorded_ms") or 0):
        if row.get("slug") not in retained_slugs:
            continue
        key = (row.get("record_type"), row.get("slug"), row.get("delay_seconds"))
        if key in seen:
            continue
        seen.add(key)
        retained.append(_clean_decision(row) if row.get("record_type") == "decision"
                        else dict(row))

    temporary = target.with_name(f".{target.name}.tmp")
    with temporary.open("w") as output:
        for row in retained:
            output.write(json.dumps(row, separators=(",", ":")) + "\n")
        output.flush()
    temporary.replace(target)

    archive = data / "archive" / "legacy-history"
    archive.mkdir(parents=True, exist_ok=True)
    for path in sources:
        destination = archive / path.name
        if destination.exists():
            destination.unlink()
        path.replace(destination)
    old_status = data / "stage4-live-status.json"
    if old_status.exists():
        old_status.replace(archive / old_status.name)
    old_active = data / "stage4-active"
    if old_active.exists():
        destination = archive / old_active.name
        if destination.exists():
            shutil.rmtree(destination)
        old_active.replace(destination)

    manifest = {
        "source_files": [path.name for path in sources],
        "retained_strategy_first_start": min(legacy_starts),
        "retained_strategy_last_legacy_start": last_legacy_start,
        "retained_rounds": len(retained_slugs),
        "retained_rows": len(retained),
    }
    (archive / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return {"migrated": True, **manifest}
