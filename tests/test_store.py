"""The two store backends and the functions that pick one from the environment. Blob Storage is replaced by an
in-memory fake container, so nothing touches Azure."""

import azure.identity
import azure.storage.blob
import pytest
from azure.core.exceptions import ResourceNotFoundError

import store
from store import BlobStore, LocalStore


# --- LocalStore ---------------------------------------------------------------------------------------------------

def test_local_store_list_of_a_missing_folder_is_empty(tmp_path):
    assert LocalStore(tmp_path / "never-written").list() == []


def test_local_store_list_filters_by_prefix_and_skips_partial_writes(tmp_path):
    s = LocalStore(tmp_path)
    s.write("b/2.txt", b"x")
    s.write("a/1.txt", b"x")
    s.write("a/0.txt", b"x")
    (tmp_path / "a" / "half.txt.tmp").write_bytes(b"in progress")
    assert s.list() == ["a/0.txt", "a/1.txt", "b/2.txt"]
    assert s.list("a/") == ["a/0.txt", "a/1.txt"]
    assert s.list("c/") == []


def test_local_store_write_replaces_and_leaves_no_temporary_file(tmp_path):
    s = LocalStore(tmp_path)
    s.write("k.txt", b"one", metadata={"owner": "x"})  # metadata is accepted and ignored
    s.write("k.txt", b"two")
    assert s.read("k.txt") == b"two"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["k.txt"]


def test_local_store_delete_of_a_missing_key_is_a_no_op(tmp_path):
    LocalStore(tmp_path).delete("nope.txt")


def test_local_store_upload_and_download(tmp_path):
    s = LocalStore(tmp_path / "s")
    src = tmp_path / "src.bin"
    src.write_bytes(b"payload")
    s.upload("deep/er/key.bin", src)
    assert s.read("deep/er/key.bin") == b"payload"
    dest = tmp_path / "dest.bin"
    s.download("deep/er/key.bin", dest)
    assert dest.read_bytes() == b"payload"
    with pytest.raises(FileNotFoundError):
        s.download("missing.bin", tmp_path / "x")


@pytest.mark.parametrize("method, args", [("read", ()), ("exists", ()), ("delete", ()), ("download", ("dest",)),
                                          ("upload", ("src",))])
def test_local_store_every_method_refuses_keys_outside_it(tmp_path, method, args):
    s = LocalStore(tmp_path / "s")
    with pytest.raises(ValueError, match="outside the store"):
        getattr(s, method)("../escape.txt", *(tmp_path / a for a in args))


# --- BlobStore against a fake container --------------------------------------------------------------------------

class FakeDownload:
    def __init__(self, data):
        self.data = data

    def readall(self):
        return self.data

    def readinto(self, f):
        f.write(self.data)


class FakeContainer:
    """Just enough of azure.storage.blob.ContainerClient, keeping blobs in a dict."""

    def __init__(self):
        self.blobs, self.metadata, self.calls = {}, {}, []

    def _missing(self, key):
        if key not in self.blobs:
            raise ResourceNotFoundError(key)

    def download_blob(self, key, max_concurrency=1):
        self.calls.append(("download_blob", key, max_concurrency))
        self._missing(key)
        return FakeDownload(self.blobs[key])

    def upload_blob(self, key, data, overwrite=False, metadata=None, max_concurrency=1):
        assert overwrite
        self.blobs[key] = data.read() if hasattr(data, "read") else bytes(data)
        self.metadata[key] = metadata

    def get_blob_client(self, key):
        return type("Blob", (), {"exists": lambda _: key in self.blobs})()

    def list_blobs(self, name_starts_with=""):
        return [type("Props", (), {"name": k})() for k in reversed(list(self.blobs)) if k.startswith(name_starts_with)]

    def delete_blob(self, key, delete_snapshots=None):
        assert delete_snapshots == "include"
        self._missing(key)
        del self.blobs[key]


@pytest.fixture
def blob(monkeypatch):
    """A BlobStore whose container is a FakeContainer; records how the container client was made."""
    fake, made = FakeContainer(), []
    credential = object()
    monkeypatch.setattr(azure.identity, "DefaultAzureCredential", lambda: credential)

    def from_container_url(url, credential):
        made.append((url, credential))
        return fake
    monkeypatch.setattr(azure.storage.blob.ContainerClient, "from_container_url", staticmethod(from_container_url))
    s = BlobStore("https://acct.blob.core.windows.net/data")
    assert made == [("https://acct.blob.core.windows.net/data", credential)]
    return s, fake


def test_blob_store_round_trip(blob):
    s, fake = blob
    assert s.url == "https://acct.blob.core.windows.net/data"
    s.write("a/b.txt", b"hello", metadata={"owner": "alice"})
    assert fake.metadata["a/b.txt"] == {"owner": "alice"}
    assert s.read("a/b.txt") == b"hello"
    assert s.exists("a/b.txt") and not s.exists("a/c.txt")
    s.write("a/a.txt", b"x")
    s.write("z.txt", b"x")
    assert s.list("a/") == ["a/a.txt", "a/b.txt"]  # sorted, whatever order the service returns
    assert s.list() == ["a/a.txt", "a/b.txt", "z.txt"]
    s.delete("a/b.txt")
    assert not s.exists("a/b.txt")


def test_blob_store_missing_keys(blob, tmp_path):
    s, _ = blob
    with pytest.raises(FileNotFoundError):
        s.read("nope")
    with pytest.raises(FileNotFoundError):
        s.download("nope", tmp_path / "out")
    s.delete("nope")  # no error


def test_blob_store_upload_and_download_stream_files(blob, tmp_path):
    s, fake = blob
    src = tmp_path / "src.bin"
    src.write_bytes(b"payload" * 1000)
    s.upload("big.bin", src)
    assert fake.blobs["big.bin"] == b"payload" * 1000
    dest = tmp_path / "dest.bin"
    s.download("big.bin", dest)
    assert dest.read_bytes() == b"payload" * 1000
    assert ("download_blob", "big.bin", 4) in fake.calls  # parallel download


# --- Choosing a store --------------------------------------------------------------------------------------------

def test_open_store_picks_blob_for_https_and_a_folder_otherwise(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "BlobStore", lambda url: ("blob", url))
    assert store.open_store("https://acct.blob.core.windows.net/c") == ("blob", "https://acct.blob.core.windows.net/c")
    local = store.open_store(tmp_path)
    assert isinstance(local, LocalStore) and local.root == tmp_path
    assert isinstance(store.open_store(str(tmp_path)), LocalStore)


def test_data_store_is_none_without_a_url(tmp_path, monkeypatch):
    monkeypatch.delenv("DATA_STORE_URL", raising=False)
    assert store.data_store() is None
    monkeypatch.setenv("DATA_STORE_URL", "")
    assert store.data_store() is None
    monkeypatch.setenv("DATA_STORE_URL", str(tmp_path))
    assert store.data_store().root == tmp_path


def test_state_store_defaults_to_a_local_folder(tmp_path, monkeypatch):
    monkeypatch.delenv("STATE_STORE_URL", raising=False)
    assert store.state_store().root == store.HERE / "store" / "state"
    monkeypatch.setenv("STATE_STORE_URL", str(tmp_path))
    assert store.state_store().root == tmp_path
