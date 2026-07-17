import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path


class StreamWriter:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    def write(self, record: dict) -> str:
        if "id" not in record:
            record["id"] = str(uuid.uuid4())
        if "ts" not in record:
            record["ts"] = datetime.now(timezone.utc).isoformat()
        if "processed" not in record:
            record["processed"] = False
        descriptor = os.open(
            self.path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        os.chmod(self.path, 0o600)
        with os.fdopen(descriptor, "a") as f:
            f.write(json.dumps(record) + "\n")
        return record["id"]


class StreamReader:
    def __init__(self, path):
        self.path = Path(path)

    def read_all(self):
        if not self.path.exists():
            return
        with open(self.path) as f:
            for line in f:
                line = line.strip()
                if line:
                    yield json.loads(line)

    def read_unprocessed(self):
        for record in self.read_all():
            if not record.get("processed", False):
                yield record

    def mark_processed(self, record_id: str):
        if not self.path.exists():
            return
        lines = self.path.read_text().splitlines()
        new_lines = []
        for line in lines:
            if line.strip():
                rec = json.loads(line)
                if rec.get("id") == record_id:
                    rec["processed"] = True
                new_lines.append(json.dumps(rec))
        self.path.write_text("\n".join(new_lines) + "\n")
