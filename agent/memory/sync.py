"""Backup/sync backends. Folder sync works today; Drive is a typed seam."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Protocol, runtime_checkable

from agent.core.errors import BackendUnavailable, ConfigurationError


@runtime_checkable
class SyncBackend(Protocol):
    def push(self, db_path: Path) -> Path: ...

    def pull(self, db_path: Path) -> Path: ...


class FolderSync:
    """Copy the SQLite file to/from a local folder (USB, rclone, Drive desktop)."""

    def __init__(self, folder: Path | str) -> None:
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)

    def push(self, db_path: Path) -> Path:
        src = Path(db_path)
        if not src.exists():
            raise ConfigurationError("memory db does not exist", path=str(src))
        dest = self.folder / src.name
        shutil.copy2(src, dest)
        wal = src.with_suffix(src.suffix + "-wal")
        shm = src.with_suffix(src.suffix + "-shm")
        if wal.exists():
            shutil.copy2(wal, self.folder / wal.name)
        if shm.exists():
            shutil.copy2(shm, self.folder / shm.name)
        return dest

    def pull(self, db_path: Path) -> Path:
        dest = Path(db_path)
        src = self.folder / dest.name
        if not src.exists():
            candidates = sorted(self.folder.glob("*.db"))
            if len(candidates) == 1:
                src = candidates[0]
            else:
                raise ConfigurationError("backup db not found", path=str(src))
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        return dest


class DriveSync:
    """Placeholder for Google Drive API credentials on the target machine."""

    def __init__(self, folder_id: str) -> None:
        if not folder_id.strip():
            raise ConfigurationError("Drive folder_id must be non-empty")
        self.folder_id = folder_id

    def push(self, db_path: Path) -> Path:
        raise BackendUnavailable(
            "Google Drive API is not wired; use FolderSync or rclone",
            folder_id=self.folder_id,
            db=str(db_path),
        )

    def pull(self, db_path: Path) -> Path:
        raise BackendUnavailable(
            "Google Drive API is not wired; use FolderSync or rclone",
            folder_id=self.folder_id,
            db=str(db_path),
        )
