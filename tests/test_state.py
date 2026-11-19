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
