from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from electridrive.google_api.oauth import authenticate_interactive

LOGGER = logging.getLogger(__name__)
DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"
DRIVE_SHORTCUT_MIME = "application/vnd.google-apps.shortcut"

ProgressCB = Callable[[int, int], None]  # (bytes_done, total_bytes)

# Fields requested for every file listing. Kept in one place so all calls agree.
_FILE_FIELDS = (
    "id, name, mimeType, size, modifiedTime, parents, trashed, starred, "
    "md5Checksum, webViewLink, iconLink"
)

# Google Workspace docs are not real binary files; they must be exported.
GOOGLE_EXPORT_FORMATS: dict[str, tuple[str, str]] = {
    "application/vnd.google-apps.document": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".docx",
    ),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".pptx",
    ),
    "application/vnd.google-apps.drawing": ("image/png", ".png"),
}
DEFAULT_EXPORT: tuple[str, str] = ("application/pdf", ".pdf")


def export_format_for(mime_type: str | None) -> tuple[str, str]:
    """Return (export_mime, file_extension) for a Google Workspace mime type."""
    if mime_type is None:
        return DEFAULT_EXPORT
    return GOOGLE_EXPORT_FORMATS.get(mime_type, DEFAULT_EXPORT)


@dataclass(frozen=True)
class RemoteFile:
    id: str
    name: str
    mime_type: str | None = None
    size: int | None = None
    modified_time: str | None = None
    parents: tuple[str, ...] = ()
    trashed: bool = False
    starred: bool = False
    md5_checksum: str | None = None
    web_view_link: str | None = None
    icon_link: str | None = None

    @property
    def is_folder(self) -> bool:
        return self.mime_type == DRIVE_FOLDER_MIME

    @property
    def is_google_doc(self) -> bool:
        return bool(
            self.mime_type
            and self.mime_type.startswith("application/vnd.google-apps.")
            and not self.is_folder
        )

    @classmethod
    def from_api(cls, item: dict) -> "RemoteFile":
        size = item.get("size")
        return cls(
            id=item["id"],
            name=item.get("name", "(unnamed)"),
            mime_type=item.get("mimeType"),
            size=int(size) if size is not None else None,
            modified_time=item.get("modifiedTime"),
            parents=tuple(item.get("parents", []) or []),
            trashed=bool(item.get("trashed", False)),
            starred=bool(item.get("starred", False)),
            md5_checksum=item.get("md5Checksum"),
            web_view_link=item.get("webViewLink"),
            icon_link=item.get("iconLink"),
        )


@dataclass(frozen=True)
class RemoteChange:
    file_id: str
    removed: bool
    file: RemoteFile | None = None


@dataclass(frozen=True)
class StorageQuota:
    limit: int | None
    usage: int
    usage_in_drive: int
    user_email: str | None = None
    user_name: str | None = None


@dataclass
class FileListing:
    files: list[RemoteFile] = field(default_factory=list)
    next_page_token: str | None = None


class DriveClientProtocol(Protocol):
    def ensure_folder_path(self, remote_path: str) -> str: ...
    def upload_file(
        self, local_file: Path, parent_id: str, remote_name: str, progress_cb: ProgressCB | None = ...
    ) -> str: ...
    def list_files(self, limit: int = 20) -> list[RemoteFile]: ...
    def list_folder(self, parent_id: str = "root", page_token: str | None = ...) -> FileListing: ...
    def download_file(self, file_id: str, dest_path: Path, progress_cb: ProgressCB | None = ...) -> Path: ...
    def export_file(
        self, file_id: str, dest_path: Path, mime_type: str | None = ..., progress_cb: ProgressCB | None = ...
    ) -> Path: ...
    def get_metadata(self, file_id: str) -> RemoteFile: ...


class GoogleDriveClient:
    """Thin, safety-aware wrapper over the Drive v3 API.

    Heavy google-api imports stay inside methods so the package imports without the
    optional dependencies installed (keeps the core test-able / headless).
    """

    def __init__(self, service=None, credentials=None):
        # httplib2 (and the built service) are NOT thread-safe. The UI uses a thread
        # pool and the transfer manager has its own worker threads, so each thread gets
        # its own service built from shared credentials (see the `service` property).
        self._explicit_service = service
        self._local = threading.local()
        if service is not None:
            self._creds = None
        elif credentials is not None:
            self._creds = credentials
        else:
            self._creds = authenticate_interactive()

    @property
    def service(self):
        if self._explicit_service is not None:
            return self._explicit_service
        svc = getattr(self._local, "service", None)
        if svc is None:
            try:
                from googleapiclient.discovery import build
            except Exception as exc:  # pragma: no cover
                raise RuntimeError(
                    "Google API dependencies are not installed. Run: pip install -r requirements.txt"
                ) from exc
            svc = build("drive", "v3", credentials=self._creds,
                        cache_discovery=False, static_discovery=True)
            self._local.service = svc
        return svc

    @staticmethod
    def _escape_query_text(value: str) -> str:
        return value.replace("\\", "\\\\").replace("'", "\\'")

    # ----------------------------------------------------------------- folders
    def find_folder(self, name: str, parent_id: str | None = None) -> str | None:
        escaped = self._escape_query_text(name)
        query = [
            f"name = '{escaped}'",
            f"mimeType = '{DRIVE_FOLDER_MIME}'",
            "trashed = false",
        ]
        if parent_id:
            query.append(f"'{parent_id}' in parents")
        response = self.service.files().list(
            q=" and ".join(query),
            spaces="drive",
            fields="files(id, name)",
            pageSize=10,
        ).execute()
        files = response.get("files", [])
        return files[0]["id"] if files else None

    def create_folder(self, name: str, parent_id: str | None = None) -> str:
        metadata = {"name": name, "mimeType": DRIVE_FOLDER_MIME}
        if parent_id:
            metadata["parents"] = [parent_id]
        created = self.service.files().create(body=metadata, fields="id").execute()
        return created["id"]

    def ensure_folder_path(self, remote_path: str) -> str:
        parts = [p.strip() for p in remote_path.replace("\\", "/").split("/") if p.strip()]
        if not parts:
            return "root"
        parent_id: str | None = None
        current_id = "root"
        for part in parts:
            found = self.find_folder(part, parent_id)
            if found is None:
                found = self.create_folder(part, parent_id)
                LOGGER.info("Created Drive folder: %s", part)
            current_id = found
            parent_id = found
        return current_id

    # ----------------------------------------------------------------- listing
    def list_folder(self, parent_id: str = "root", page_token: str | None = None) -> FileListing:
        response = self.service.files().list(
            q=f"'{parent_id}' in parents and trashed = false",
            spaces="drive",
            fields=f"nextPageToken, files({_FILE_FIELDS})",
            pageSize=200,
            orderBy="folder,name",
            pageToken=page_token,
        ).execute()
        return FileListing(
            files=[RemoteFile.from_api(it) for it in response.get("files", [])],
            next_page_token=response.get("nextPageToken"),
        )

    def list_files(self, limit: int = 20) -> list[RemoteFile]:
        response = self.service.files().list(
            pageSize=limit,
            fields=f"files({_FILE_FIELDS})",
            q="trashed = false",
            orderBy="modifiedTime desc",
        ).execute()
        return [RemoteFile.from_api(it) for it in response.get("files", [])]

    def search(self, text: str, limit: int = 100) -> list[RemoteFile]:
        escaped = self._escape_query_text(text)
        response = self.service.files().list(
            q=f"name contains '{escaped}' and trashed = false",
            spaces="drive",
            fields=f"files({_FILE_FIELDS})",
            pageSize=limit,
            orderBy="modifiedTime desc",
        ).execute()
        return [RemoteFile.from_api(it) for it in response.get("files", [])]

    def list_starred(self, limit: int = 200) -> list[RemoteFile]:
        response = self.service.files().list(
            q="starred = true and trashed = false",
            fields=f"files({_FILE_FIELDS})",
            pageSize=limit,
            orderBy="modifiedTime desc",
        ).execute()
        return [RemoteFile.from_api(it) for it in response.get("files", [])]

    def list_trash(self, limit: int = 200) -> list[RemoteFile]:
        response = self.service.files().list(
            q="trashed = true",
            fields=f"files({_FILE_FIELDS})",
            pageSize=limit,
        ).execute()
        return [RemoteFile.from_api(it) for it in response.get("files", [])]

    def get_metadata(self, file_id: str) -> RemoteFile:
        item = self.service.files().get(fileId=file_id, fields=_FILE_FIELDS).execute()
        return RemoteFile.from_api(item)

    # --------------------------------------------------------------- transfers
    def upload_file(
        self,
        local_file: Path,
        parent_id: str,
        remote_name: str,
        progress_cb: ProgressCB | None = None,
    ) -> str:
        try:
            from googleapiclient.http import MediaFileUpload
        except Exception as exc:  # pragma: no cover
            raise RuntimeError(
                "Google API upload dependency missing. Run: pip install -r requirements.txt"
            ) from exc

        metadata = {"name": remote_name, "parents": [parent_id] if parent_id != "root" else []}
        total = local_file.stat().st_size
        media = MediaFileUpload(str(local_file), resumable=True)
        request = self.service.files().create(body=metadata, media_body=media, fields="id")
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status and progress_cb:
                progress_cb(int(status.resumable_progress), total)
        if progress_cb:
            progress_cb(total, total)
        return response["id"]

    def download_file(self, file_id: str, dest_path: Path, progress_cb: ProgressCB | None = None) -> Path:
        try:
            from googleapiclient.http import MediaIoBaseDownload
        except Exception as exc:  # pragma: no cover
            raise RuntimeError(
                "Google API download dependency missing. Run: pip install -r requirements.txt"
            ) from exc

        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        request = self.service.files().get_media(fileId=file_id)
        with dest_path.open("wb") as handle:
            downloader = MediaIoBaseDownload(handle, request)
            done = False
            while not done:
                status, done = downloader.next_chunk()
                if status and progress_cb:
                    progress_cb(int(status.resumable_progress), int(status.total_size or 0))
        return dest_path

    def export_file(
        self,
        file_id: str,
        dest_path: Path,
        mime_type: str | None = None,
        progress_cb: ProgressCB | None = None,
    ) -> Path:
        """Export a Google Workspace document (Docs/Sheets/Slides) to a real file."""
        try:
            from googleapiclient.http import MediaIoBaseDownload
        except Exception as exc:  # pragma: no cover
            raise RuntimeError(
                "Google API export dependency missing. Run: pip install -r requirements.txt"
            ) from exc

        if mime_type is None:
            meta = self.get_metadata(file_id)
            mime_type, _ext = export_format_for(meta.mime_type)
        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        request = self.service.files().export_media(fileId=file_id, mimeType=mime_type)
        with dest_path.open("wb") as handle:
            downloader = MediaIoBaseDownload(handle, request)
            done = False
            while not done:
                status, done = downloader.next_chunk()
                if status and progress_cb:
                    progress_cb(int(status.resumable_progress), int(status.total_size or 0))
        return dest_path

    # ------------------------------------------------------------------ delete
    def trash(self, file_id: str) -> None:
        """Move a file to Drive trash (reversible). Never permanently deletes."""
        self.service.files().update(fileId=file_id, body={"trashed": True}).execute()

    def untrash(self, file_id: str) -> None:
        self.service.files().update(fileId=file_id, body={"trashed": False}).execute()

    # ------------------------------------------------------------- account/quota
    def get_about(self) -> StorageQuota:
        about = self.service.about().get(fields="storageQuota, user").execute()
        quota = about.get("storageQuota", {})
        user = about.get("user", {})
        limit = quota.get("limit")
        return StorageQuota(
            limit=int(limit) if limit is not None else None,
            usage=int(quota.get("usage", 0)),
            usage_in_drive=int(quota.get("usageInDrive", 0)),
            user_email=user.get("emailAddress"),
            user_name=user.get("displayName"),
        )

    # --------------------------------------------------------------- changes API
    def get_start_page_token(self) -> str:
        resp = self.service.changes().getStartPageToken().execute()
        return resp["startPageToken"]

    def list_changes(self, page_token: str) -> tuple[list[RemoteChange], str | None, str | None]:
        """Return (changes, next_page_token, new_start_page_token).

        Use new_start_page_token (set on the final page) to resume next time.
        """
        resp = self.service.changes().list(
            pageToken=page_token,
            spaces="drive",
            includeRemoved=True,
            fields=f"newStartPageToken, nextPageToken, changes(fileId, removed, file({_FILE_FIELDS}))",
            pageSize=200,
        ).execute()
        changes = [
            RemoteChange(
                file_id=c.get("fileId", ""),
                removed=bool(c.get("removed", False)),
                file=RemoteFile.from_api(c["file"]) if c.get("file") else None,
            )
            for c in resp.get("changes", [])
        ]
        return changes, resp.get("nextPageToken"), resp.get("newStartPageToken")
