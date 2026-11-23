"""The programme documents: downloading them from GOV.UK (fetch_docs.py, refresh/docs_sync.py), splitting them into
chunks and searching them (docs.py), and the daily Azure Function. GOV.UK and Foundry are replaced by fakes."""
import hashlib
import io
import json
import sys
import types
from types import SimpleNamespace

import pytest
import yaml

import docs
import docs_sync
import fetch_docs

PAGE = {"title": "Brownfield Fund", "description": 'Money for "brownfield" sites: apply now',
        "public_updated_at": "2025-04-09T10:00:00Z",
        "details": {"body": "<h2>Eligibility</h2><p>Sites must be <abbr title='previously developed'>PDL</abbr>.</p>"}}


def fake_urlopen(pages, opened=None):
    """urlopen replacement serving GOV.UK content API pages by slug."""
    def urlopen(url, timeout=None):
        assert url.startswith("https://www.gov.uk/api/content/") and timeout == 60
        slug = url.removeprefix("https://www.gov.uk/api/content/")
        if opened is not None:
            opened.append(slug)
        return io.BytesIO(json.dumps(pages[slug]).encode())
    return urlopen


def front_matter(markdown):
    _, front, body = markdown.split("---", 2)
    return yaml.safe_load(front), body


# --- docs.py: chunking -------------------------------------------------------------------------------------------

def test_tokens_lowercases_and_drops_stopwords_and_punctuation():
    assert docs.tokens("The BIL Fund, for 2026-2036: what IS eligible?") == ["bil", "fund", "2026", "2036", "eligible"]
    assert docs.tokens("") == []


def write_doc(folder, doc_id, body, title="T"):
    (folder / f"{doc_id}.md").write_text(f"---\nid: {doc_id}\ntitle: \"{title}\"\nurl: https://x/{doc_id}\n"
                                         f"updated: 2025-01-01\n---\n\n# {title}\n\n{body}")


def words(n, word="w"):
    return " ".join(f"{word}{i}" for i in range(n))


def test_load_splits_documents_by_heading(tmp_path, monkeypatch):
    monkeypatch.setattr(docs, "DOCS", tmp_path)
    write_doc(tmp_path, "b-doc", f"{words(30, 'intro')}\n\n## Eligibility\n\n{words(40, 'elig')}\n\n"
                                 f"### Sites\n\n{words(30, 'site')}\n\n## Contact\n\n{words(30, 'contact')}\n")
    write_doc(tmp_path, "a-doc", words(30))
    (tmp_path / "notes.txt").write_text("not a document")
    documents, chunks = docs.load()
    assert [d["id"] for d in documents] == ["a-doc", "b-doc"]
    assert documents[0] == {"id": "a-doc", "title": "T", "url": "https://x/a-doc", "updated": "2025-01-01",
                            "words": 32}  # body includes the "# T" heading
    b = [c for c in chunks if c["document"] == "b-doc"]
    assert [c["section"] for c in b] == ["Introduction", "Eligibility", "Eligibility > Sites", "Contact"]
    assert b[1]["text"] == words(40, "elig") and b[1]["title"] == "T" and b[1]["url"] == "https://x/b-doc"


def test_load_carries_short_sections_into_the_next(tmp_path, monkeypatch):
    monkeypatch.setattr(docs, "DOCS", tmp_path)
    write_doc(tmp_path, "d", f"## Phone\n\n{words(5, 'phone')}\n\n## Email\n\n{words(30, 'email')}\n\n## End\n\nshort tail\n")
    _, chunks = docs.load()
    assert [c["section"] for c in chunks] == ["Email", "End"]
    assert chunks[0]["text"] == words(5, "phone") + " " + words(30, "email")
    assert chunks[1]["text"] == "short tail"  # the last section is kept however short


def test_load_splits_long_sections_into_overlapping_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(docs, "DOCS", tmp_path)
    write_doc(tmp_path, "d", f"## Long\n\n{words(700)}\n\n## Exact\n\n{words(docs.MAX_WORDS, 'x')}\n")
    _, chunks = docs.load()
    long = [c["text"].split() for c in chunks if c["section"] == "Long"]
    step = docs.MAX_WORDS - docs.OVERLAP
    assert [w[0] for w in long] == ["w0", f"w{step}", f"w{2 * step}"]
    assert [len(w) for w in long] == [300, 300, 700 - 2 * step]
    assert long[0][-docs.OVERLAP:] == long[1][:docs.OVERLAP]
    assert len([c for c in chunks if c["section"] == "Exact"]) == 1  # exactly MAX_WORDS: no second window


def test_load_ignores_a_trailing_empty_section(tmp_path, monkeypatch):
    monkeypatch.setattr(docs, "DOCS", tmp_path)
    write_doc(tmp_path, "d", f"{words(30)}\n\n## Nothing here\n")
    _, chunks = docs.load()
    assert [c["section"] for c in chunks] == ["Introduction"]


def test_the_shipped_documents_load():
    assert {d["id"] for d in docs.DOCUMENTS} == set(fetch_docs.SOURCES)
    assert docs.CHUNKS and all(c["text"] for c in docs.CHUNKS)
    assert len(docs.TOKENS) == len(docs.CHUNKS) and docs.AVG_LEN > 0


# --- docs.py: search ---------------------------------------------------------------------------------------------

@pytest.fixture
def corpus(monkeypatch):
    """Swap the index for a tiny one; returns a function that builds it from (document, section, text) triples."""
    def build(*items):
        chunks = [{"document": d, "title": d, "url": "u", "section": s, "text": t} for d, s, t in items]
        toks = [docs.tokens(c["section"] + " " + c["text"]) for c in chunks]
        monkeypatch.setattr(docs, "CHUNKS", chunks)
        monkeypatch.setattr(docs, "TOKENS", toks)
        monkeypatch.setattr(docs, "AVG_LEN", sum(map(len, toks)) / len(toks))
        monkeypatch.setattr(docs, "DF", docs.Counter(t for ts in toks for t in set(ts)))
    return build


def test_search_ranks_by_bm25(corpus):
    corpus(("a", "Viability", "viability viability assessment"), ("a", "Other", "viability is mentioned once here"),
           ("b", "Unrelated", "flood risk zones"))
    hits = docs.search("viability")
    assert [h["section"] for h in hits] == ["Viability", "Other"]
    assert hits[0]["score"] > hits[1]["score"] > 0 and hits[0]["score"] == round(hits[0]["score"], 2)
    assert set(hits[0]) == {"score", "document", "title", "url", "section", "text"}


def test_search_matches_section_headings(corpus):
    corpus(("a", "Eligibility", "who may apply"), ("a", "Other", "who may apply"))
    assert [h["section"] for h in docs.search("eligibility")] == ["Eligibility"]


def test_search_rare_terms_weigh_more(corpus):
    corpus(("a", "s1", "homes homes grant"), ("a", "s2", "homes homes land"), ("a", "s3", "homes homes other"))
    assert docs.search("homes grant")[0]["section"] == "s1"


def test_search_filters_by_document_and_limits_results(corpus):
    corpus(*[("a" if i % 2 else "b", f"s{i}", "housing delivery") for i in range(10)])
    assert len(docs.search("housing")) == 5
    assert len(docs.search("housing", top_k=2)) == 2
    assert {h["document"] for h in docs.search("housing", document="a", top_k=10)} == {"a"}
    assert docs.search("housing", document="missing") == []


def test_search_without_matches(corpus):
    corpus(("a", "s", "housing delivery"))
    assert docs.search("submarine") == [] and docs.search("the and of") == [] and docs.search("") == []


def test_search_the_shipped_documents():
    hits = docs.search("brownfield land infrastructure", document="bil-fund", top_k=3)
    assert 0 < len(hits) <= 3 and {h["document"] for h in hits} == {"bil-fund"}
    assert [h["score"] for h in hits] == sorted((h["score"] for h in hits), reverse=True)


# --- fetch_docs.py -----------------------------------------------------------------------------------------------

def test_fetch_docs_writes_markdown_that_docs_can_load(tmp_path, monkeypatch, capsys):
    opened = []
    monkeypatch.setattr(fetch_docs, "DOCS", tmp_path / "docs")
    monkeypatch.setattr(fetch_docs, "SOURCES", {"bil": "guidance/bil"})
    monkeypatch.setattr(fetch_docs, "urlopen", fake_urlopen({"guidance/bil": PAGE}, opened))
    fetch_docs.main()
    assert opened == ["guidance/bil"]
    text = (tmp_path / "docs" / "bil.md").read_text()
    meta, body = front_matter(text)
    assert meta == {"id": "bil", "title": "Brownfield Fund", "url": "https://www.gov.uk/guidance/bil",
                    "updated": __import__("datetime").date(2025, 4, 9), "summary": PAGE["description"],
                    "licence": "Open Government Licence v3.0"}
    assert body.startswith("\n\n# Brownfield Fund\n\n## Eligibility\n\nSites must be PDL.") and body.endswith(".\n")
    assert capsys.readouterr().out == "bil: 6 words\n"  # "##", "Eligibility", "Sites", "must", "be", "PDL."
    monkeypatch.setattr(docs, "DOCS", tmp_path / "docs")
    _, chunks = docs.load()
    assert [c["section"] for c in chunks] == ["Eligibility"]


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
