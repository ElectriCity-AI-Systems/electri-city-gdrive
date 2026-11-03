"""Closed cloud capability facade for synthetic QA trees."""
from electridrive.qa_policy import QaSafetyError, current_policy
from electridrive.google_api.client import FileListing, GoogleDriveClient


def _deny(*args, **kwargs):
    raise QaSafetyError("QA safety stop: operation is outside registered synthetic cloud trees")


class QaDriveClient:
    def __init__(self, client, policy):
        policy.verify()
        if isinstance(client, GoogleDriveClient) and client._qa_profile != str(policy.profile):
            _deny()
        self.__client = client
        self.__policy = policy
        self.__parents = {}

    def verify(self):
        self.__policy.verify()
        active = current_policy()
        if active is None or active.profile != self.__policy.profile:
            _deny()

    @property
    def service(self):
        return _deny()

    def _check(self, rid, seen=None):
        self.verify()
        seen = set() if seen is None else seen
        if rid in seen:
            _deny()
        seen.add(rid)
        roots = self.__policy.remote_ids
        if rid not in roots and rid not in self.__parents:
            _deny()
        remote = self.__client.get_metadata(rid)
        if remote.id != rid or remote.trashed:
            _deny()
        if rid in roots:
            spec = self.__policy.root_spec(rid)
            if (not remote.is_folder or remote.name != spec['name'] or
                list(remote.parents) != spec['parents'] or
                remote.app_properties.get('electridrive_qa_nonce') != self.__policy.nonce):
                _deny()
        else:
            parent = self.__parents[rid]
            if remote.parents != (parent,):
                _deny()
            if not self._check(parent, seen).is_folder:
                _deny()
        return remote

    def _remember(self, rid, parent):
        self.__parents[rid] = parent
        return self._check(rid)

    def ensure_folder_path(self, remote_path):
        self.verify()
        if not isinstance(remote_path, str) or not remote_path.startswith('qa:'):
            _deny()
        rid = remote_path[3:]
        if rid not in self.__policy.remote_ids:
            _deny()
        self._check(rid)
        return rid

    def list_folder(self, parent_id='root', page_token=None):
        self.verify()
        if parent_id == 'root':
            if page_token is not None:
                _deny()
            return FileListing([self._check(rid) for rid in self.__policy.remote_ids])
        if not self._check(parent_id).is_folder:
            _deny()
        result = self.__client.list_folder(parent_id, page_token)
        files = []
        for remote in result.files:
            if remote.parents != (parent_id,) or remote.trashed:
                _deny()
            files.append(self._remember(remote.id, parent_id))
        return FileListing(files, result.next_page_token)

    def get_metadata(self, file_id):
        return self._check(file_id)

    def _find(self, name, parent_id, folder):
        matches = []
        page = None
        seen = set()
        while True:
            result = self.list_folder(parent_id, page)
            matches.extend(f.id for f in result.files if f.name == name and
                           (f.is_folder if folder else not f.is_google_doc and not f.is_folder))
            page = result.next_page_token
            if not page:
                break
            if page in seen:
                _deny()
            seen.add(page)
        if len(matches) > 1:
            raise ValueError('Ambiguous QA Drive path')
        return matches[0] if matches else None

    def find_folder(self, name, parent_id=None):
        return self._find(name, parent_id, True)

    def find_file(self, name, parent_id):
        return self._find(name, parent_id, False)

    @staticmethod
    def _name(name):
        if not isinstance(name, str) or not name.strip() or name in {".", ".."} or any(c in name for c in ("/", "\\", "\x00")):
            _deny()

    def create_folder(self, name, parent_id=None):
        self._name(name)
        if not self._check(parent_id).is_folder:
            _deny()
        rid = self.__client.create_folder(name, parent_id)
        self._remember(rid, parent_id)
        return rid

    def create_qa_root(self, name):
        self.verify()
        self._name(name)
        remote = self.__client._create_qa_folder(name, self.__policy.nonce)
        # Validate the actual server metadata, never enroll an existing name match.
        actual = self.__client.get_metadata(remote.id)
        return self.__policy.register_new_root(actual, name)

    def upload_file(self, local_file, parent_id, remote_name, progress_cb=None):
        self._name(remote_name)
        self.__policy.local(local_file)
        if not self._check(parent_id).is_folder:
            _deny()
        rid = self.__client.upload_file(local_file, parent_id, remote_name, progress_cb)
        self._remember(rid, parent_id)
        return rid

    def update_file(self, file_id, local_file, remote_name=None, progress_cb=None):
        self.__policy.local(local_file)
        if remote_name is not None:
            self._name(remote_name)
        target = self._check(file_id)
        if target.is_folder or target.is_google_doc:
            _deny()
        result = self.__client.update_file(file_id, local_file, remote_name, progress_cb)
        if result != file_id:
            _deny()
        self._check(file_id)
        return result

    def download_file(self, file_id, dest_path, progress_cb=None):
        self.__policy.local(dest_path, exists=False)
        self._check(file_id)
        return self.__client.download_file(file_id, dest_path, progress_cb)

    def export_file(self, file_id, dest_path, mime_type=None, progress_cb=None):
        self.__policy.local(dest_path, exists=False)
        self._check(file_id)
        return self.__client.export_file(file_id, dest_path, mime_type, progress_cb)

    def get_start_page_token(self):
        self.verify()
        return None

    def list_changes(self, page_token):
        self.verify()
        return None

    def get_about(self):
        self.verify()
        return self.__client.get_about()

    search = list_files = list_starred = list_trash = trash = untrash = _deny
