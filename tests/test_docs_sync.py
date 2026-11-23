"""Keeping the programme documents in the agent's Foundry vector store up to date (refresh/docs_sync.py), and the
daily Azure Function that runs it. GOV.UK and Foundry are replaced by fakes."""
import hashlib
import io
import json
import sys
import types
from types import SimpleNamespace

import pytest
import yaml

import docs_sync

PAGE = {"title": "Brownfield Fund", "description": 'Money for "brownfield" sites: apply now',
        "public_updated_at": "2025-04-09T10:00:00Z",
        "details": {"body": "<h2>Eligibility</h2><p>Sites must be <abbr title='previously developed'>PDL</abbr>.</p>"}}


def fake_urlopen(pages):
    """urlopen replacement serving GOV.UK content API pages by slug."""
    def urlopen(url, timeout=None):
        assert url.startswith("https://www.gov.uk/api/content/") and timeout == 60
        return io.BytesIO(json.dumps(pages[url.removeprefix("https://www.gov.uk/api/content/")]).encode())
    return urlopen


def front_matter(markdown):
    _, front, body = markdown.split("---", 2)
    return yaml.safe_load(front), body


# --- refresh/docs_sync.py ----------------------------------------------------------------------------------------

def test_docs_sync_fetch(monkeypatch):
    monkeypatch.setattr(docs_sync, "urlopen", fake_urlopen({"guidance/bil": PAGE}))
    meta, body = front_matter(docs_sync.fetch("bil", "guidance/bil"))
    assert meta["id"] == "bil" and meta["title"] == "Brownfield Fund" and meta["updated"] == "2025-04-09"
    assert meta["summary"] == PAGE["description"] and meta["url"] == "https://www.gov.uk/guidance/bil"
    assert "# Brownfield Fund\n\n## Eligibility" in body and "<abbr" not in body


class FakeFoundry:
    """The Foundry project's OpenAI client: vector stores and files, logging every change."""

    def __init__(self, stores=(), files=(), status="completed"):
        self.log, self.status, self.next_id = [], status, 0
        self.stores = [SimpleNamespace(id=f"vs_{n}", name=n) for n in stores]
        self.files = {f"file_{i}": name for i, name in enumerate(files)}  # id -> filename, all in the store
        self.vector_stores = SimpleNamespace(
            list=lambda: list(self.stores), create=self._create_store,
            files=SimpleNamespace(list=self._list, delete=self._detach, upload_and_poll=self._upload))
        self.files_api = SimpleNamespace(retrieve=lambda fid: SimpleNamespace(filename=self.files[fid]),
                                         delete=lambda fid: self.log.append(("delete", fid)))

    def _create_store(self, name):
        self.stores.append(SimpleNamespace(id=f"vs_{name}", name=name))
        self.log.append(("create_store", name))
        return self.stores[-1]

    def _list(self, store_id):
        return [SimpleNamespace(id=fid) for fid in self.files]

    def _detach(self, fid, vector_store_id):
        self.log.append(("detach", fid, vector_store_id))

    def _upload(self, vector_store_id, file):
        name, data = file
        self.log.append(("upload", name, vector_store_id))
        return SimpleNamespace(id=f"new_{name}", status=self.status, last_error="bad file")


@pytest.fixture
def foundry(monkeypatch):
    """Wire docs_sync to a FakeFoundry and fixed document texts; returns a function that sets it up."""
    texts = {"bil": "bil text", "sahp": "sahp text"}
    monkeypatch.setattr(docs_sync, "SOURCES", {k: f"guidance/{k}" for k in texts})
    monkeypatch.setattr(docs_sync, "fetch", lambda doc_id, slug: texts[doc_id])
    digest = {k: hashlib.sha256(t.encode()).hexdigest()[:16] for k, t in texts.items()}

    class Context:
        def __init__(self, value):
            self.value = value

        def __enter__(self):
            return self.value

        def __exit__(self, *exc):
            return False

    def setup(**kwargs):
        fake = FakeFoundry(**kwargs)
        openai = SimpleNamespace(vector_stores=fake.vector_stores, files=fake.files_api)
        project = SimpleNamespace(get_openai_client=lambda: openai)
        monkeypatch.setattr(docs_sync, "DefaultAzureCredential", lambda: Context("credential"))
        monkeypatch.setattr(docs_sync, "AIProjectClient",
                            lambda endpoint, credential: Context(project) if credential == "credential" else None)
        return fake
    return setup, digest


def test_sync_creates_the_store_and_adds_every_document(foundry):
    setup, digest = foundry
    fake = setup()
    out = docs_sync.sync()
    assert out == {"vector_store": f"vs_{docs_sync.VECTOR_STORE}", "documents": {"bil": "added", "sahp": "added"}}
    assert fake.log == [("create_store", docs_sync.VECTOR_STORE),
                        ("upload", f"bil--{digest['bil']}.md", out["vector_store"]),
                        ("upload", f"sahp--{digest['sahp']}.md", out["vector_store"])]


def test_sync_updates_changed_documents_and_removes_dropped_ones(foundry):
    setup, digest = foundry
    fake = setup(stores=("unrelated", docs_sync.VECTOR_STORE),
                 files=(f"bil--{digest['bil']}.md", f"bil--{digest['bil']}.md",  # current, and a duplicate
                        "sahp--0000000000000000.md", "old--1111111111111111.md"))
    out = docs_sync.sync()
    assert out["documents"] == {"bil": "unchanged", "sahp": "updated", "old": "removed"}
    store = f"vs_{docs_sync.VECTOR_STORE}"
    assert fake.log == [("detach", "file_1", store), ("delete", "file_1"),  # the duplicate of the current bil
                        ("upload", f"sahp--{digest['sahp']}.md", store),  # new sahp before the old one goes
                        ("detach", "file_2", store), ("delete", "file_2"),
                        ("detach", "file_3", store), ("delete", "file_3")]


def test_sync_stops_if_indexing_fails(foundry):
    setup, _ = foundry
    fake = setup(stores=(docs_sync.VECTOR_STORE,), files=("bil--0000000000000000.md",), status="failed")
    with pytest.raises(RuntimeError, match="Indexing bil failed: bad file"):
        docs_sync.sync()
    assert not any(op[0] in ("detach", "delete") for op in fake.log)  # the old version is still searchable


def test_vector_store_and_remove():
    fake = FakeFoundry(stores=("a", docs_sync.VECTOR_STORE, docs_sync.VECTOR_STORE))
    openai = SimpleNamespace(vector_stores=fake.vector_stores, files=fake.files_api)
    assert docs_sync.vector_store(openai) is fake.stores[1]  # the first match
    docs_sync.remove(openai, SimpleNamespace(id="vs_x"), SimpleNamespace(id="f1"))
    assert fake.log == [("detach", "f1", "vs_x"), ("delete", "f1")]


# --- refresh/function_app.py -------------------------------------------------------------------------------------

def test_function_app_runs_sync_daily_and_logs_it(monkeypatch, caplog):
    schedules = []

    class FunctionApp:
        def timer_trigger(self, schedule, arg_name):
            schedules.append((schedule, arg_name))
            return lambda f: f
    stub = types.ModuleType("azure.functions")
    stub.FunctionApp, stub.TimerRequest = FunctionApp, object
    monkeypatch.setitem(sys.modules, "azure.functions", stub)
    monkeypatch.delitem(sys.modules, "function_app", raising=False)
    import function_app
    monkeypatch.delitem(sys.modules, "function_app")
    assert schedules == [("0 0 6 * * *", "timer")]
    monkeypatch.setattr(docs_sync, "sync", lambda: {"documents": {"bil": "unchanged"}})
    with caplog.at_level("INFO"):
        function_app.refresh_documents(None)
    assert "Document refresh: {'documents': {'bil': 'unchanged'}}" in caplog.text
