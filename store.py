"""Where the app keeps files outside its container: data releases and saved state.

Two stores, each set by a URL:
- ``DATA_STORE_URL``: versioned data releases (see release.py). The app only reads it.
- ``STATE_STORE_URL``: saved configs and conversation owners. The app reads and writes it.

A URL starting with ``https://`` is an Azure Blob container, e.g.
``https://stdetest.blob.core.windows.net/data``, signed in with DefaultAzureCredential: the app's managed
identity when deployed, your ``az login`` locally. Anything else is a local folder, for development and tests.

Keys are paths with forward slashes, e.g. ``releases/20261112-1530/manifest.json``.
"""
import os
import shutil
from pathlib import Path

HERE = Path(__file__).parent


class LocalStore:
    """A folder on disk, with the same interface as ``BlobStore``."""

    def __init__(self, root):
        """Args:
            root: Folder that holds the keys. Created on first write.
        """
        self.root = Path(root)
        self.url = str(self.root)

    def _path(self, key):
        p = (self.root / key).resolve()
        if not p.is_relative_to(self.root.resolve()):  # a key like ../x must not escape the store
            raise ValueError(f"Key outside the store: {key!r}")
        return p

    def read(self, key):
        """Contents of a key as bytes. Raises FileNotFoundError if it doesn't exist."""
        return self._path(key).read_bytes()

    def write(self, key, data, metadata=None):
        """Create or replace a key. Writes to a temporary file first, so readers never see half a file.

        ``metadata`` (a dict of strings) is accepted for interface parity with Blob and ignored.
        """
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(p)

    def exists(self, key):
        """Whether a key exists."""
        return self._path(key).is_file()

    def list(self, prefix=""):
        """Sorted keys starting with ``prefix``."""
        if not self.root.exists():
            return []
        keys = (p.relative_to(self.root).as_posix() for p in self.root.rglob("*")
                if p.is_file() and not p.name.endswith(".tmp"))
        return sorted(k for k in keys if k.startswith(prefix))

    def delete(self, key):
        """Delete a key if it exists."""
        self._path(key).unlink(missing_ok=True)

    def download(self, key, dest):
        """Copy a key to a local file. Raises FileNotFoundError if it doesn't exist."""
        shutil.copyfile(self._path(key), dest)

    def upload(self, key, src):
        """Create or replace a key from a local file."""
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, p)


class BlobStore:
    """An Azure Blob Storage container."""

    def __init__(self, url):
        """Args:
            url: Container URL, ``https://<account>.blob.core.windows.net/<container>``.
        """
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import ContainerClient
        self.url = url
        self.container = ContainerClient.from_container_url(url, credential=DefaultAzureCredential())

    def read(self, key):
        """Contents of a key as bytes. Raises FileNotFoundError if it doesn't exist."""
        from azure.core.exceptions import ResourceNotFoundError
        try:
            return self.container.download_blob(key).readall()
        except ResourceNotFoundError:
            raise FileNotFoundError(key) from None

    def write(self, key, data, metadata=None):
        """Create or replace a key, optionally with blob metadata (a dict of strings)."""
        self.container.upload_blob(key, data, overwrite=True, metadata=metadata)

    def exists(self, key):
        """Whether a key exists."""
        return self.container.get_blob_client(key).exists()

    def list(self, prefix=""):
        """Sorted keys starting with ``prefix``."""
        return sorted(b.name for b in self.container.list_blobs(name_starts_with=prefix))

    def delete(self, key):
        """Delete a key if it exists."""
        from azure.core.exceptions import ResourceNotFoundError
        try:
            self.container.delete_blob(key, delete_snapshots="include")
        except ResourceNotFoundError:
            pass

    def download(self, key, dest):
        """Copy a key to a local file. Raises FileNotFoundError if it doesn't exist."""
        from azure.core.exceptions import ResourceNotFoundError
        try:
            with open(dest, "wb") as f:
                self.container.download_blob(key, max_concurrency=4).readinto(f)
        except ResourceNotFoundError:
            raise FileNotFoundError(key) from None

    def upload(self, key, src):
        """Create or replace a key from a local file."""
        with open(src, "rb") as f:
            self.container.upload_blob(key, f, overwrite=True, max_concurrency=4)


def open_store(url):
    """A store for a URL: Blob for ``https://`` URLs, a local folder otherwise."""
    return BlobStore(url) if str(url).startswith("https://") else LocalStore(url)


def data_store():
    """The data release store from ``DATA_STORE_URL``, or None when it isn't set (the app then serves data/levels)."""
    url = os.environ.get("DATA_STORE_URL")
    return open_store(url) if url else None


def state_store():
    """The state store from ``STATE_STORE_URL``, defaulting to the local folder ./store/state."""
    return open_store(os.environ.get("STATE_STORE_URL") or HERE / "store" / "state")
