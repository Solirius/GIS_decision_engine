"""Who the user is, and who may see which configs and conversations."""
import pytest

import state
from conftest import principal
from store import LocalStore

ALICE, BOB = state.User("alice-oid"), state.User("bob-oid")
ADMIN = state.User("admin-oid", roles=frozenset({"Admin"}))


@pytest.fixture
def store(tmp_path):
    return LocalStore(tmp_path)


def test_no_sign_in_means_local_user():
    assert state.user_from_headers({}) == state.LOCAL


def test_sign_in_required_refuses_anonymous():
    with pytest.raises(PermissionError):
        state.user_from_headers({}, require_sign_in=True)


def test_roles_come_from_the_sign_in_token():
    u = state.user_from_headers(principal("abc-123", roles=["Admin"]))
    assert u.id == "abc-123" and u.is_admin
    assert not state.user_from_headers(principal("abc-123", roles=["Analyst"])).is_admin


def test_malformed_user_id_is_refused():
    with pytest.raises(PermissionError):
        state.user_from_headers({"x-ms-client-principal-id": "../../etc"})


def test_users_see_only_their_own_configs(store):
    a = state.save_config(ALICE, "zones: msoa\n", store=store)
    state.save_config(BOB, "zones: boroughs\n", store=store)
    assert [r["owner"] for r in state.list_configs(ALICE, store)] == ["alice-oid"]
    assert state.read_config(ALICE, "alice-oid", a, store) == "zones: msoa\n"
    with pytest.raises(PermissionError):
        state.read_config(BOB, "alice-oid", a, store)


def test_admins_see_every_config(store):
    a = state.save_config(ALICE, "zones: msoa\n", store=store)
    state.save_config(BOB, "zones: boroughs\n", store=store)
    assert {r["owner"] for r in state.list_configs(ADMIN, store)} == {"alice-oid", "bob-oid"}
    assert state.read_config(ADMIN, "alice-oid", a, store) == "zones: msoa\n"


def test_config_names_cannot_reach_other_folders(store):
    with pytest.raises(PermissionError):
        state.read_config(ALICE, "alice-oid", "../bob-oid/x.yaml", store)


def test_missing_config(store):
    with pytest.raises(FileNotFoundError):
        state.read_config(ALICE, "alice-oid", "nope.yaml", store)


def test_conversations_belong_to_whoever_started_them(store):
    state.claim_conversation(ALICE, "conv_123", store)
    state.check_conversation(ALICE, "conv_123", store)
    with pytest.raises(PermissionError):
        state.check_conversation(BOB, "conv_123", store)
    with pytest.raises(PermissionError):
        state.check_conversation(ADMIN, "conv_123", store)  # not even admins
    with pytest.raises(PermissionError):
        state.check_conversation(ALICE, "conv_unknown", store)


def test_import_folder(store, tmp_path):
    src = tmp_path / "old"
    src.mkdir()
    (src / "agent-1.yaml").write_text("zones: msoa\n")
    assert state.import_folder(src, "local", store) == 1
    assert state.read_config(state.LOCAL, "local", "agent-1.yaml", store) == "zones: msoa\n"


def test_user_name_comes_from_the_headers():
    assert state.user_from_headers(principal("abc")).name == "abc@example.org"
    assert state.user_from_headers({"x-ms-client-principal-id": "abc"}) == state.User("abc", "", frozenset())


def test_custom_role_claim_type_is_honoured():
    import base64
    import json
    blob = base64.b64encode(json.dumps({"role_typ": "http://schemas/role", "claims": [
        {"typ": "http://schemas/role", "val": "Admin"}, {"typ": "name", "val": "Admin"}]}).encode()).decode()
    u = state.user_from_headers({"x-ms-client-principal-id": "abc", "x-ms-client-principal": blob})
    assert u.roles == frozenset({"Admin"})


def b64(obj):
    import base64
    import json
    return base64.b64encode(json.dumps(obj).encode()).decode()


@pytest.mark.parametrize("blob", ["not base64!", "bm90IGpzb24=", "//79",  # not base64, base64 of "not json", bad UTF-8
                                  b64(["roles", "Admin"]), b64("Admin"), b64({"claims": "Admin"}),  # wrong shapes
                                  b64({"claims": [{"typ": "roles"}]}), b64({"claims": [["roles", "Admin"]]})])
def test_unreadable_sign_in_token_means_no_roles(blob):
    u = state.user_from_headers({"x-ms-client-principal-id": "abc", "x-ms-client-principal": blob})
    assert u.id == "abc" and u.roles == frozenset()


def test_can_read():
    assert state.can_read(ALICE, "alice-oid")
    assert not state.can_read(ALICE, "bob-oid")
    assert state.can_read(ADMIN, "bob-oid")


def test_save_config_names_and_labels_it(store):
    import re
    name = state.save_config(ALICE, "zones: msoa\n", source="user", store=store)
    assert re.fullmatch(r"user-\d{8}-\d{6}-\d{6}\.yaml", name)
    assert store.read(f"configs/alice-oid/{name}") == b"zones: msoa\n"


def test_save_config_uses_the_state_store_by_default(state_dir):
    name = state.save_config(ALICE, "zones: msoa\n")
    assert (state_dir / "configs" / "alice-oid" / name).read_text() == "zones: msoa\n"
    assert state.list_configs(ALICE) == [{"owner": "alice-oid", "name": name}]
    assert state.read_config(ALICE, "alice-oid", name) == "zones: msoa\n"


def test_list_configs_newest_first_whatever_saved_it(store):
    names = [state.save_config(ALICE, "x", source=s, store=store) for s in ("user", "agent", "user")]
    store.write("configs/alice-oid/nested/ignored.yaml", b"x")  # not a config: too deep
    store.write("configs/stray.yaml", b"x")  # not a config: no owner folder
    assert [r["name"] for r in state.list_configs(ALICE, store)] == names[::-1]


@pytest.mark.parametrize("owner, name", [("../x", "a.yaml"), ("alice-oid", ".hidden"), ("alice-oid", "a/b.yaml"),
                                         ("", "a.yaml"), ("alice-oid", "")])
def test_malformed_config_references_are_refused(store, owner, name):
    with pytest.raises(PermissionError, match="Malformed"):
        state.read_config(ADMIN, owner, name, store)


def test_malformed_conversation_ids(store):
    with pytest.raises(ValueError):
        state.claim_conversation(ALICE, "../conv", store)
    with pytest.raises(PermissionError, match="Malformed"):
        state.check_conversation(ALICE, "../conv", store)
    assert store.list() == []


def test_claiming_again_hands_the_conversation_over(store):
    state.claim_conversation(ALICE, "conv_1", store)
    state.claim_conversation(BOB, "conv_1", store)
    state.check_conversation(BOB, "conv_1", store)
    with pytest.raises(PermissionError):
        state.check_conversation(ALICE, "conv_1", store)


def test_import_folder_only_takes_yaml_files(store, tmp_path):
    src = tmp_path / "old"
    src.mkdir()
    (src / "b.yaml").write_text("zones: msoa\n")
    (src / "a.yaml").write_text("zones: boroughs\n")
    (src / "notes.txt").write_text("not a config")
    (src / "c.yml").write_text("other extension")
    assert state.import_folder(src, "alice-oid", store) == 2
    assert [r["name"] for r in state.list_configs(ALICE, store)] == ["b.yaml", "a.yaml"]
    assert state.import_folder(tmp_path / "empty-or-missing", "alice-oid", store) == 0


def test_import_command_line(tmp_path, state_dir, capsys, monkeypatch):
    import runpy
    import sys
    src = tmp_path / "old"
    src.mkdir()
    (src / "a.yaml").write_text("zones: msoa\n")
    monkeypatch.setattr(sys, "argv", ["state.py", "import", str(src), "--owner", "bob-oid"])
    runpy.run_path(state.__file__, run_name="__main__")
    assert capsys.readouterr().out == "Imported 1 configs for bob-oid\n"
    assert (state_dir / "configs" / "bob-oid" / "a.yaml").exists()
