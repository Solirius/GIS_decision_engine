"""The agent's tool dispatch and turn loop, with Foundry replaced by a fake."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

import agent


def make_tools(log):
    """Tools that record what they were called with, and in which thread."""
    return agent.Tools(
        describe=lambda level: log.append(("describe", level)) or {"zones": level},
        evaluate=lambda cfg: log.append(("evaluate", threading.current_thread().name)) or {"ok": True},
        save_config=lambda text: log.append(("save", text)) or "agent-1.yaml",
        claim_conversation=lambda cid: log.append(("claim", cid)))


def test_run_tool_dispatch():
    log, applied = [], []
    tools = make_tools(log)
    assert '"zones": null' in agent.run_tool("describe_data", {}, tools, None)
    assert agent.run_tool("preview_config", {"config_yaml": "zones: msoa"}, tools, None).startswith("{")
    assert agent.run_tool("preview_config", {"config_yaml": "{bad"}, tools, None).startswith("Invalid config")
    out = agent.run_tool("apply_config", {"config_yaml": "zones: msoa"}, tools, lambda c, n: applied.append((c, n)))
    assert out == "Config loaded into the map (saved as agent-1.yaml)."
    assert applied == [({"zones": "msoa"}, "agent-1.yaml")]
    assert agent.run_tool("nope", {}, tools, None) == "Unknown tool 'nope'"


class FakeOpenAI:
    """Answers the first request with an apply_config call, the second with a message."""

    def __init__(self):
        self.requests = []
        self.conversations = SimpleNamespace(create=self._create)
        self.responses = SimpleNamespace(create=self._respond)

    async def _create(self):
        return SimpleNamespace(id="conv_new")

    async def _respond(self, **kwargs):
        self.requests.append(kwargs)
        usage = SimpleNamespace(total_tokens=10)
        if len(self.requests) == 1:
            call = SimpleNamespace(type="function_call", name="apply_config", call_id="c1",
                                   arguments='{"config_yaml": "zones: msoa"}')
            return SimpleNamespace(output=[call], usage=usage)
        text = SimpleNamespace(type="output_text", text="Done.")
        return SimpleNamespace(output=[SimpleNamespace(type="message", content=[text])], usage=usage)


def run_chat(monkeypatch, session_id=None):
    fake, log = FakeOpenAI(), []

    async def client():
        return fake
    monkeypatch.setattr(agent, "client", client)

    async def collect():
        return [e async for e in agent.chat("hi", "", session_id, make_tools(log))]
    return asyncio.run(collect()), log, fake


def test_chat_claims_new_conversations_and_runs_tools_off_the_event_loop(monkeypatch):
    events, log, fake = run_chat(monkeypatch)
    assert ("claim", "conv_new") in log
    assert [e["type"] for e in events] == ["tool", "config", "text", "done"]
    assert events[-1] == {"type": "done", "session_id": "conv_new", "tokens": 20, "error": False}
    evaluate_thread = next(t for name, t in log if name == "evaluate")
    assert evaluate_thread != threading.main_thread().name
    assert fake.requests[1]["input"][0]["output"] == "Config loaded into the map (saved as agent-1.yaml)."


def test_chat_continues_without_claiming_again(monkeypatch):
    events, log, _ = run_chat(monkeypatch, session_id="conv_old")
    assert not any(name == "claim" for name, _ in log)
    assert events[-1]["session_id"] == "conv_old"


# --- Helpers and tool dispatch edge cases ------------------------------------------------------------------------


def test_config_arg_schema():
    s = agent.config_arg("the config")
    assert s == {"type": "object", "properties": {"config_yaml": {"type": "string", "description": "the config"}},
                 "required": ["config_yaml"]}


def test_text():
    assert agent.text("as is") == "as is"
    assert agent.text({"a": [1]}) == '{\n "a": [\n  1\n ]\n}'
    assert agent.text(None) == "null"


def test_tools_are_declared_for_every_function_run_tool_handles():
    assert [t.name for t in agent.TOOLS] == ["describe_data", "preview_config", "apply_config"]


def test_describe_errors_go_back_to_the_model():
    def describe(level):
        raise ValueError(f"Unknown zones {level!r}")
    tools = agent.Tools(describe=describe, evaluate=None, save_config=None, claim_conversation=None)
    assert agent.run_tool("describe_data", {"zones": "nope"}, tools, None) == "Unknown zones 'nope'"


def test_empty_config_is_evaluated_as_defaults():
    seen = []
    tools = agent.Tools(describe=None, evaluate=lambda cfg: seen.append(cfg) or "result", save_config=None,
                        claim_conversation=None)
    assert agent.run_tool("preview_config", {"config_yaml": ""}, tools, None) == "result"
    assert seen == [{}]


def test_invalid_config_is_neither_saved_nor_applied():
    def evaluate(cfg):
        raise ValueError("Unknown keys: ['nope']")
    saved = []
    tools = agent.Tools(describe=None, evaluate=evaluate, save_config=saved.append, claim_conversation=None)
    out = agent.run_tool("apply_config", {"config_yaml": "features: {nope: {}}"}, tools, lambda *a: saved.append(a))
    assert out == "Invalid config: Unknown keys: ['nope']" and saved == []
    assert agent.run_tool("apply_config", {}, tools, None).startswith("Invalid config")  # missing argument


# --- Registering the agent in Foundry ----------------------------------------------------------------------------

class AsyncList:
    def __init__(self, items):
        self.items = items

    def __aiter__(self):
        async def gen():
            for i in self.items:
                yield i
        return gen()


class FakeProject:
    """Foundry project client: a vector store list and an agent registry."""
    instances = []

    def __init__(self, endpoint, credential, stores=("he-programme-documents",), latest_hash=None):
        self.endpoint, self.created = endpoint, []
        openai = SimpleNamespace(vector_stores=SimpleNamespace(
            list=lambda: AsyncList([SimpleNamespace(id=f"vs_{i}", name=n) for i, n in enumerate(stores)])))
        self.get_openai_client = lambda: openai
        self.openai = openai

        async def get(name):
            if latest_hash is None:
                raise LookupError("no such agent")
            return SimpleNamespace(versions=SimpleNamespace(latest=SimpleNamespace(
                metadata={"definition_hash": latest_hash})))

        async def create_version(name, definition, metadata, description):
            self.created.append((name, definition, metadata))
        self.agents = SimpleNamespace(get=get, create_version=create_version)
        FakeProject.instances.append(self)


@pytest.fixture
def foundry(monkeypatch):
    """Replace Foundry with FakeProject; returns a function to set what the project holds."""
    monkeypatch.setattr(agent, "_openai", None)
    monkeypatch.setattr(agent, "DefaultAzureCredential", lambda: "credential")
    FakeProject.instances = []

    def setup(**kwargs):
        monkeypatch.setattr(agent, "AIProjectClient", lambda endpoint, credential: FakeProject(endpoint, credential,
                                                                                               **kwargs))
    return setup


def test_client_registers_a_new_agent_once(foundry):
    foundry(stores=("other", "he-programme-documents"))
    openai = asyncio.run(agent.client())
    project = FakeProject.instances[0]
    assert openai is project.openai and project.endpoint == agent.ENDPOINT
    [(name, definition, metadata)] = project.created
    assert name == agent.AGENT_NAME and definition.model == agent.MODEL
    assert definition.instructions == agent.SYSTEM_PROMPT and len(metadata["definition_hash"]) == 16
    file_search = definition.as_dict()["tools"][-1]
    assert file_search["vector_store_ids"] == ["vs_1"]
    assert asyncio.run(agent.client()) is openai and len(FakeProject.instances) == 1  # cached


def test_client_keeps_an_up_to_date_agent(foundry):
    foundry()
    asyncio.run(agent.client())
    digest = FakeProject.instances[0].created[0][2]["definition_hash"]
    agent._openai = None
    foundry(latest_hash=digest)
    asyncio.run(agent.client())
    assert FakeProject.instances[1].created == []
    agent._openai = None
    foundry(latest_hash="stale")
    asyncio.run(agent.client())
    assert len(FakeProject.instances[2].created) == 1


def test_client_needs_the_documents_vector_store(foundry):
    foundry(stores=("something-else",))
    with pytest.raises(RuntimeError, match="No vector store"):
        asyncio.run(agent.client())
    assert agent._openai is None


# --- The turn loop -----------------------------------------------------------------------------------------------

class ScriptedOpenAI(FakeOpenAI):
    """Answers each request with the next list of output items from a script."""

    def __init__(self, script, usage=True):
        super().__init__()
        self.script, self.usage = list(script), usage

    async def _respond(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(output=self.script.pop(0) if self.script else [],
                               usage=SimpleNamespace(total_tokens=7) if self.usage else None)


def message(*texts):
    return SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", text=t) for t in texts]
                           + [SimpleNamespace(type="refusal", text="ignored")])


def call(name, args='{}', call_id="c"):
    return SimpleNamespace(type="function_call", name=name, call_id=call_id, arguments=args)


def run(monkeypatch, fake, session_id="conv_1", tools=None):
    async def client():
        return fake
    monkeypatch.setattr(agent, "client", client)

    async def collect():
        return [e async for e in agent.chat("hi", "zones: msoa", session_id, tools or make_tools([]))]
    return asyncio.run(collect())


def test_chat_sends_the_message_with_the_current_config(monkeypatch):
    fake = ScriptedOpenAI([[message("Hi")]])
    run(monkeypatch, fake)
    [req] = fake.requests
    assert req["conversation"] == "conv_1"
    assert req["input"] == [{"type": "message", "role": "user",
                             "content": "hi\n\n<current_config>\nzones: msoa\n</current_config>"}]
    assert req["extra_body"] == {"agent_reference": {"name": agent.AGENT_NAME, "type": "agent_reference"}}


def test_chat_strips_citations_and_skips_empty_replies(monkeypatch):
    fake = ScriptedOpenAI([[SimpleNamespace(type="file_search_call"), message("See fileciteturn0file1 the ",
                                                                              "guidance."), message("  ")]])
    events = run(monkeypatch, fake)
    assert events == [{"type": "tool", "name": "file_search"}, {"type": "text", "text": "See  the guidance."},
                      {"type": "done", "session_id": "conv_1", "tokens": 7, "error": False}]


def test_chat_sends_every_function_output_back_together(monkeypatch):
    fake = ScriptedOpenAI([[call("describe_data", None, "a"), call("preview_config", '{"config_yaml": "{"}', "b")],
                           [message("ok")]], usage=False)
    events = run(monkeypatch, fake)
    assert [e["type"] for e in events] == ["tool", "tool", "text", "done"] and events[-1]["tokens"] == 0
    outputs = fake.requests[1]["input"]
    assert [o["call_id"] for o in outputs] == ["a", "b"]
    assert outputs[0]["output"] == '{\n "zones": null\n}' and outputs[1]["output"].startswith("Invalid config")


def test_chat_gives_up_after_max_turns(monkeypatch):
    monkeypatch.setattr(agent, "MAX_TURNS", 3)
    fake = ScriptedOpenAI([[call("describe_data")]] * 5)
    events = run(monkeypatch, fake)
    assert len(fake.requests) == 3
    assert events[-1] == {"type": "done", "session_id": "conv_1", "tokens": 21, "error": True}


def test_chat_reports_failures_as_an_error_event(monkeypatch):
    async def broken():
        raise RuntimeError("Foundry is down")
    monkeypatch.setattr(agent, "client", broken)

    async def collect():
        return [e async for e in agent.chat("hi", "", None, make_tools([]))]
    assert asyncio.run(collect()) == [{"type": "error", "text": "Foundry is down"}]


def test_chat_reports_a_failed_claim_and_does_not_continue(monkeypatch):
    fake = ScriptedOpenAI([[message("never sent")]])

    def claim(cid):
        raise ValueError("Malformed conversation id")
    tools = agent.Tools(describe=None, evaluate=None, save_config=None, claim_conversation=claim)
    assert run(monkeypatch, fake, session_id=None, tools=tools) == [{"type": "error", "text": "Malformed conversation id"}]
    assert fake.requests == []
