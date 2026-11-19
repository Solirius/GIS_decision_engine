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
