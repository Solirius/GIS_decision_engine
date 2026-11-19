"""The agent's tool dispatch and turn loop, with Foundry replaced by a fake."""
import asyncio
import threading
from types import SimpleNamespace

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
