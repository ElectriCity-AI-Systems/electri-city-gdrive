from pathlib import Path

from electridrive.google_api.client import GoogleDriveClient


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


def test_ensure_folder_path_anchors_first_component_at_drive_root():
    class Files:
        def __init__(self):
            self.queries = []

        def list(self, **kwargs):
            self.queries.append(kwargs["q"])
            # This models same-named folders both under root and elsewhere.  The
            # query's parent clause determines which result Drive may return.
            if "'root' in parents" in kwargs["q"]:
                return _Response({"files": [{"id": "root-team", "name": "Team"}]})
            return _Response({"files": [{"id": "nested-team", "name": "Team"}]})

    class Service:
        def __init__(self):
            self.files_api = Files()

        def files(self):
            return self.files_api

    service = Service()
    client = GoogleDriveClient(service=service)

    assert client.ensure_folder_path("Team") == "root-team"
    assert service.files_api.queries
    assert "'root' in parents" in service.files_api.queries[0]


def test_update_file_uses_drive_update_and_preserves_id(tmp_path: Path):
    class UploadRequest:
        def next_chunk(self):
            return None, {"id": "canonical-id"}

    class Files:
        def __init__(self):
            self.update_kwargs = None

        def update(self, **kwargs):
            self.update_kwargs = kwargs
            return UploadRequest()

    class Service:
        def __init__(self):
            self.files_api = Files()

        def files(self):
            return self.files_api

    source = tmp_path / "report.txt"
    source.write_text("new bytes", encoding="utf-8")
    service = Service()
    client = GoogleDriveClient(service=service)

    result = client.update_file("canonical-id", source, "report.txt")

    assert result == "canonical-id"
    assert service.files_api.update_kwargs["fileId"] == "canonical-id"
    assert service.files_api.update_kwargs["body"] == {"name": "report.txt"}
    assert "parents" not in service.files_api.update_kwargs["body"]
    assert service.files_api.update_kwargs["media_body"] is not None

    # Sync updates are media-only so collision-disambiguated local names never
    # rename the canonical Drive object.
    assert client.update_file("canonical-id", source) == "canonical-id"
    assert service.files_api.update_kwargs["body"] == {}


def test_find_file_reuses_only_binary_media_in_the_exact_parent():
    class Files:
        def __init__(self):
            self.kwargs = None

        def list(self, **kwargs):
            self.kwargs = kwargs
            return _Response(
                {
                    "files": [
                        {
                            "id": "folder-id",
                            "mimeType": "application/vnd.google-apps.folder",
                        },
                        {
                            "id": "native-id",
                            "mimeType": "application/vnd.google-apps.document",
                        },
                        {"id": "binary-id", "mimeType": "text/plain"},
                    ]
                }
            )

    class Service:
        def __init__(self):
            self.files_api = Files()

        def files(self):
            return self.files_api

    service = Service()
    client = GoogleDriveClient(service=service)

    assert client.find_file("report.txt", "parent-id") == "binary-id"
    query = service.files_api.kwargs["q"]
    assert "name = 'report.txt'" in query
    assert "'parent-id' in parents" in query
    assert "trashed = false" in query
