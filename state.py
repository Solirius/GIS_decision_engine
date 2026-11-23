"""Saved configs and conversation owners, kept in the state store, and the rules for who may see them.

Who a user is comes from Container Apps sign-in (Easy Auth), which signs the user in and passes their identity to
the app in request headers. It removes any such headers a caller sends, so they can't be forged from outside. Without
sign-in (running locally, or test before sign-in is set up), everyone is the user ``local``.

Layout in the state store:

    configs/<owner object id>/<name>.yaml     one saved config; owner, source and created time also in blob metadata
    conversations/<conversation id>.json      {"owner": <object id>}: who may continue the conversation

Access rules, all in ``can_read``: a user sees their own configs; an ``Admin`` (an app role assigned in Entra) sees
every config in the instance. Conversations can only be continued by the user who started them.

Usage: ``uv run state.py import <folder> [--owner local]`` copies YAML files from a folder (e.g. the old configs/)
into the state store (STATE_STORE_URL, default ./store/state).
"""
import argparse
import base64
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from store import state_store

ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")                 # Entra object ids, Foundry conversation ids
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")    # config names: no slashes, so no other folders
ADMIN_ROLE = "Admin"


@dataclass(frozen=True)
class User:
    """The signed-in user: Entra object id, display name and app roles."""
    id: str
    name: str = ""
    roles: frozenset = field(default_factory=frozenset)

    @property
    def is_admin(self):
        """Whether the user holds the Admin app role."""
        return ADMIN_ROLE in self.roles


LOCAL = User("local", "local")


def user_from_headers(headers, require_sign_in=False):
    """The user a request comes from, read from the headers Easy Auth adds.

    Args:
        headers: Request headers (case-insensitive mapping).
        require_sign_in: Refuse requests without a signed-in user instead of treating them as ``local``.

    Returns:
        A ``User``. Roles come from the ``roles`` claims of the sign-in token; a token that can't be read gives
        no roles.

    Raises:
        PermissionError: If sign-in is required and the request has no user, or the user id is malformed.
    """
    oid = headers.get("x-ms-client-principal-id")
    if not oid:
        if require_sign_in:
            raise PermissionError("Sign-in required")
        return LOCAL
    if not ID.match(oid):
        raise PermissionError("Malformed user id")
    roles = set()
    principal = headers.get("x-ms-client-principal")
    if principal:  # base64 JSON: {"claims": [{"typ": ..., "val": ...}], "role_typ": ...}
        try:
            p = json.loads(base64.b64decode(principal))
            role_types = {"roles", p.get("role_typ")}
            roles = {c["val"] for c in p.get("claims", []) if c.get("typ") in role_types}
        except (ValueError, AttributeError, TypeError, KeyError):  # unreadable, or not the expected shape: no roles
            roles = set()
    return User(oid, headers.get("x-ms-client-principal-name", ""), frozenset(roles))


def can_read(user, owner):
    """Whether ``user`` may list and open configs owned by ``owner``. The one place access rules live."""
    return user.id == owner or user.is_admin


def save_config(user, yaml_text, source="agent", store=None):
    """Save a config under the user's folder.

    Args:
        user: Owner.
        yaml_text: The config as YAML.
        source: What saved it, used in the name: ``agent`` or ``user``.
        store: State store (default: ``state_store()``).

    Returns:
        The config's name, e.g. ``agent-20261112-153012.yaml``.
    """
    store = store or state_store()
    now = datetime.now(timezone.utc)
    name = f"{source}-{now:%Y%m%d-%H%M%S-%f}.yaml"
    store.write(f"configs/{user.id}/{name}", yaml_text.encode(),
                metadata={"owner": user.id, "source": source, "created": now.isoformat(timespec="seconds")})
    return name


def list_configs(user, store=None):
    """Configs the user may see, newest first, as ``{"owner", "name"}`` dicts."""
    store = store or state_store()
    refs = ({"owner": k.split("/")[1], "name": k.split("/")[2]} for k in store.list("configs/") if k.count("/") == 2)
    return sorted((r for r in refs if can_read(user, r["owner"])), key=lambda r: r["name"].split("-", 1)[-1],
                  reverse=True)


def read_config(user, owner, name, store=None):
    """A saved config's YAML.

    Raises:
        PermissionError: If the user may not see the owner's configs, or the owner or name is malformed.
        FileNotFoundError: If there is no such config.
    """
    if not ID.match(owner) or not NAME.match(name):
        raise PermissionError("Malformed config reference")
    if not can_read(user, owner):
        raise PermissionError("Not your config")
    return (store or state_store()).read(f"configs/{owner}/{name}").decode()


def claim_conversation(user, conversation_id, store=None):
    """Record that ``user`` started a conversation, so only they can continue it."""
    if not ID.match(conversation_id):
        raise ValueError("Malformed conversation id")
    (store or state_store()).write(f"conversations/{conversation_id}.json", json.dumps({"owner": user.id}).encode())


def check_conversation(user, conversation_id, store=None):
    """Make sure ``user`` may continue a conversation.

    Raises:
        PermissionError: If the conversation is unknown, malformed or someone else's.
    """
    if not ID.match(conversation_id):
        raise PermissionError("Malformed conversation id")
    try:
        owner = json.loads((store or state_store()).read(f"conversations/{conversation_id}.json"))["owner"]
    except FileNotFoundError:
        raise PermissionError("Unknown conversation: start a new one") from None
    if owner != user.id:
        raise PermissionError("Not your conversation")


def import_folder(folder, owner="local", store=None):
    """Copy every .yaml file in a folder into the owner's configs, keeping file names. Returns the count."""
    store = store or state_store()
    files = sorted(Path(folder).glob("*.yaml"))
    for f in files:
        store.write(f"configs/{owner}/{f.name}", f.read_bytes(), metadata={"owner": owner, "source": "import"})
    return len(files)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Import saved configs into the state store")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("import", help="copy .yaml files from a folder")
    p.add_argument("folder")
    p.add_argument("--owner", default="local", help="Entra object id to file them under (default: local)")
    args = ap.parse_args()
    print(f"Imported {import_folder(args.folder, args.owner)} configs for {args.owner}")
