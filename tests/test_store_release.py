"""The local store and data releases: publish, promote, roll back, copy, and fetch with checksums."""
import pytest

import release
from conftest import LEVELS
from store import LocalStore


def test_local_store_round_trip(tmp_path):
    s = LocalStore(tmp_path)
    s.write("a/b.txt", b"hello")
    assert s.read("a/b.txt") == b"hello"
    assert s.exists("a/b.txt") and not s.exists("a/c.txt")
    assert s.list("a/") == ["a/b.txt"]
    s.delete("a/b.txt")
    with pytest.raises(FileNotFoundError):
        s.read("a/b.txt")


def test_local_store_refuses_keys_outside_it(tmp_path):
    with pytest.raises(ValueError):
        LocalStore(tmp_path / "s").write("../escape.txt", b"x")


def test_publish_is_not_live_until_promoted(tmp_path):
    s = LocalStore(tmp_path)
    v = release.publish(s, LEVELS)
    assert release.list_releases(s) == [v]
    assert release.current_version(s) is None
    release.promote(s, v)
    assert release.current_version(s) == v


def test_manifest_lists_levels_in_display_order(tmp_path):
    s = LocalStore(tmp_path)
    m = release.manifest_of(s, release.publish(s, LEVELS))
    assert [lv["key"] for lv in m["levels"]] == ["h3_8", "h3_7", "msoa", "boroughs"]
    assert m["base_level"] == "h3_8"
    assert all(lv["rows"] > 0 and len(lv["sha256"]) == 64 for lv in m["levels"])


def test_releases_are_never_overwritten(tmp_path, small_levels, monkeypatch):
    s = LocalStore(tmp_path)
    frozen = release.build_manifest(LEVELS, "20261112-000000-aaaaaa")
    monkeypatch.setattr(release, "build_manifest", lambda src, version=None: frozen)
    release.publish(s, LEVELS)
    with pytest.raises(FileExistsError):
        release.publish(s, small_levels)
    with pytest.raises(FileExistsError):
        release.copy(s, "20261112-000000-aaaaaa", s)


def test_promote_unknown_release_fails(tmp_path):
    with pytest.raises(FileNotFoundError):
        release.promote(LocalStore(tmp_path), "19990101-000000")


def test_rollback_is_a_promote(tmp_path, small_levels):
    s = LocalStore(tmp_path)
    v1 = release.publish(s, LEVELS, promote_it=True)
    v2 = release.publish(s, small_levels, promote_it=True)
    assert release.current_version(s) == v2 != v1
    release.promote(s, v1)
    assert release.current_version(s) == v1


def test_fetch_caches_and_checks(data_store, tmp_path):
    folder, manifest = release.fetch(data_store, tmp_path / "cache")
    assert (folder / "levels" / "msoa.parquet").exists()
    assert release.fetch(data_store, tmp_path / "cache")[0] == folder  # second time: from the cache


def test_fetch_rejects_a_corrupted_file(data_store, tmp_path):
    v = release.current_version(data_store)
    data_store.write(f"releases/{v}/levels/msoa.parquet", b"not the original")
    with pytest.raises(ValueError, match="Checksum"):
        release.fetch(data_store, tmp_path / "cache")
    assert not (tmp_path / "cache" / v).exists()  # nothing half-cached


def test_fetch_with_nothing_live(tmp_path):
    assert release.fetch(LocalStore(tmp_path / "empty"), tmp_path / "cache") == (None, None)


def test_copy_to_another_store(data_store, tmp_path):
    v = release.current_version(data_store)
    prod = LocalStore(tmp_path / "prod")
    release.copy(data_store, v, prod, promote_it=True)
    assert release.current_version(prod) == v
    assert release.fetch(prod, tmp_path / "prod-cache")[1]["version"] == v


def test_prepare_lays_out_a_release_for_manual_upload(tmp_path):
    out = tmp_path / "build"
    v = release.prepare(LEVELS, out)
    as_store = LocalStore(out)
    assert release.current_version(as_store) == v
    assert release.fetch(as_store, tmp_path / "cache")[0] is not None


# --- Helpers and edge cases ------------------------------------------------------------------------------------

def test_sha256_of_a_file(tmp_path):
    import hashlib
    f = tmp_path / "f.bin"
    data = b"x" * (3 << 20)  # several read blocks
    f.write_bytes(data)
    assert release.sha256(f) == hashlib.sha256(data).hexdigest()


def test_build_manifest_needs_tables(tmp_path):
    with pytest.raises(FileNotFoundError, match="No .parquet tables"):
        release.build_manifest(tmp_path)


def test_build_manifest_orders_unknown_levels_last_and_falls_back_to_the_first_level(tmp_path, small_levels):
    import shutil
    shutil.copyfile(small_levels / "msoa.parquet", small_levels / "zz_custom.parquet")
    shutil.copyfile(small_levels / "msoa.parquet", small_levels / "aa_custom.parquet")
    m = release.build_manifest(small_levels)
    assert [lv["key"] for lv in m["levels"]] == ["msoa", "boroughs", "aa_custom", "zz_custom"]
    assert [lv["label"] for lv in m["levels"]] == ["MSOA", "Boroughs", "aa_custom", "zz_custom"]
    assert m["base_level"] == "msoa"  # no h3_8 in this release


def test_build_manifest_version_is_sortable_and_fingerprinted(small_levels):
    import re
    a, b = release.build_manifest(small_levels), release.build_manifest(small_levels)
    assert re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{6}", a["version"])
    assert a["version"][-6:] == b["version"][-6:]  # same files, same fingerprint
    assert release.build_manifest(LEVELS)["version"][-6:] != a["version"][-6:]
    assert release.build_manifest(small_levels, "v1")["version"] == "v1"


def test_list_releases_ignores_incomplete_ones(tmp_path):
    s = LocalStore(tmp_path)
    v = release.publish(s, LEVELS)
    s.write("releases/20991231-000000-ffffff/levels/msoa.parquet", b"upload interrupted")
    assert release.list_releases(s) == [v]
    with pytest.raises(FileNotFoundError):
        release.manifest_of(s, "20991231-000000-ffffff")


def test_list_releases_newest_first(tmp_path, small_levels):
    s = LocalStore(tmp_path)
    for v in ("20260101-000000-aaaaaa", "20260301-000000-bbbbbb", "20260201-000000-cccccc"):
        s.write(f"releases/{v}/manifest.json", b"{}")
    assert release.list_releases(s) == ["20260301-000000-bbbbbb", "20260201-000000-cccccc", "20260101-000000-aaaaaa"]


def test_copy_of_an_unknown_release_fails(tmp_path):
    with pytest.raises(FileNotFoundError):
        release.copy(LocalStore(tmp_path / "a"), "nope", LocalStore(tmp_path / "b"))


def test_copy_without_promote_is_not_live(data_store, tmp_path):
    v = release.current_version(data_store)
    prod = LocalStore(tmp_path / "prod")
    release.copy(data_store, v, prod)
    assert release.list_releases(prod) == [v] and release.current_version(prod) is None


def test_fetch_a_named_release_that_is_not_live(tmp_path, small_levels):
    s = LocalStore(tmp_path / "s")
    v1 = release.publish(s, LEVELS)
    release.publish(s, small_levels, promote_it=True)
    folder, manifest = release.fetch(s, tmp_path / "cache", v1)
    assert manifest["version"] == v1 and folder.name == v1
    assert sorted(p.name for p in (folder / "levels").iterdir()) == sorted(f"{lv['key']}.parquet" for lv in manifest["levels"])


def test_fetch_when_another_worker_cached_it_first(data_store, tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    v = release.current_version(data_store)
    real_download = data_store.download

    def download_while_another_worker_finishes(key, dest):
        real_download(key, dest)
        if not (cache / v).exists():  # the other worker's copy lands while this one is still downloading
            (cache / v).mkdir(parents=True)
            (cache / v / "keep.txt").write_text("theirs")
    monkeypatch.setattr(data_store, "download", download_while_another_worker_finishes)
    folder, manifest = release.fetch(data_store, cache)
    assert folder == cache / v and manifest["version"] == v
    assert (folder / "keep.txt").read_text() == "theirs"  # theirs kept, ours thrown away
    assert [p.name for p in cache.iterdir()] == [v]


def test_local_is_an_unversioned_release_of_data_levels():
    folder, manifest = release.local()
    assert folder == LEVELS.parent and manifest["version"] == "local"
    assert all((folder / lv["file"]).exists() for lv in manifest["levels"])


def test_prepare_empties_the_output_folder(tmp_path, small_levels):
    out = tmp_path / "build"
    old = release.prepare(LEVELS, out)
    new = release.prepare(small_levels, out)
    assert old != new
    assert [p.name for p in (out / "releases").iterdir()] == [new]


# --- Command line ------------------------------------------------------------------------------------------------

@pytest.fixture
def cli(monkeypatch, capsys):
    """Run release.main with arguments; returns what it printed."""
    monkeypatch.delenv("DATA_STORE_URL", raising=False)

    def run(*argv):
        release.main(list(argv))
        return capsys.readouterr().out
    return run


def test_cli_publish_promote_list_and_copy(cli, tmp_path, monkeypatch):
    test, prod = tmp_path / "test", tmp_path / "prod"
    out = cli("--store", str(test), "publish")
    v1 = out.split()[1].rstrip(";")
    assert out.startswith(f"Published {v1}; make it live with: promote {v1}")
    assert release.current_version(LocalStore(test)) is None
    frozen = release.build_manifest(LEVELS, "29990101-000000-ffffff")
    monkeypatch.setattr(release, "build_manifest", lambda src, version=None: frozen)
    assert cli("--store", str(test), "publish", "--promote") == "Published 29990101-000000-ffffff and made it live\n"
    assert cli("--store", str(test), "list") == f"29990101-000000-ffffff  (live)\n{v1}\n"
    assert cli("--store", str(test), "promote", v1) == f"{v1} is live\n"
    assert cli("--store", str(test), "copy", v1, "--to", str(prod)) == f"Copied {v1} to {prod}\n"
    assert release.current_version(LocalStore(prod)) is None
    assert cli("--store", str(test), "copy", "29990101-000000-ffffff", "--to", str(prod), "--promote") \
        == f"Copied 29990101-000000-ffffff to {prod} and made it live\n"
    assert release.current_version(LocalStore(prod)) == "29990101-000000-ffffff"


def test_cli_store_comes_from_the_environment(cli, tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_STORE_URL", str(tmp_path))
    release.publish(LocalStore(tmp_path), LEVELS, promote_it=True)
    assert cli("list").endswith("(live)\n")


def test_cli_without_a_store_exits(cli):
    with pytest.raises(SystemExit, match="No store"):
        cli("list")


def test_cli_prepare_needs_no_store(cli, tmp_path, monkeypatch):
    monkeypatch.setattr(release, "BUILD", tmp_path / "build")
    calls = []
    monkeypatch.setattr(release, "prepare", lambda: calls.append(1) or "v9")
    assert cli("prepare").startswith(f"Release v9 written to {tmp_path / 'build'}.")
    assert calls == [1]
