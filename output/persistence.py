import json
import os
import tempfile
from pathlib import Path

from pydantic import BaseModel


def save_json(path: Path, models: list[BaseModel]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps([model.model_dump() for model in models], ensure_ascii=False, indent=2)

    # Write to a temp file in the same directory, then atomically replace, so a crash
    # mid-write can never leave a truncated/corrupt artifact behind.
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write(payload)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
