"""Object store conformance: every backend must give the same answers (SPEC §2, §10)."""
import threading

import pytest

from agentcloud_core.store import LocalStore, MemoryStore


def _s3():
    boto3 = pytest.importorskip("boto3")
    moto = pytest.importorskip("moto")
    from agentcloud_core.store import S3Store
    m = moto.mock_aws()
    m.start()
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="acf-conformance")
    return S3Store("acf-conformance", client=s3), m


@pytest.fixture(params=["memory", "local", "s3"])
def store(request, tmp_path):
    if request.param == "memory":
        yield MemoryStore()
    elif request.param == "local":
        yield LocalStore(tmp_path / "objects")
    else:
        s, m = _s3()
        yield s
        m.stop()


def test_create_only(store):
    assert store.put_if_absent("a/b", b"one") is True
    assert store.put_if_absent("a/b", b"two") is False
    data, etag = store.get("a/b")
    assert data == b"one" and etag


def test_compare_and_swap(store):
    assert store.get("ref") is None
    assert store.put_if_absent("ref", b"v1")
    _, e1 = store.get("ref")
    e2 = store.put_if_match("ref", b"v2", e1)
    assert e2 and e2 != e1
    assert store.put_if_match("ref", b"v3", e1) is None          # stale ETag loses
    assert store.get("ref")[0] == b"v2"
    assert store.put_if_match("missing", b"x", e1) is None


def test_list_and_delete(store):
    for k in ("p/1", "p/2", "q/1"):
        store.put_if_absent(k, b"x")
    assert sorted(store.list("p/")) == ["p/1", "p/2"]
    store.delete("p/1")
    assert store.get("p/1") is None and sorted(store.list("p/")) == ["p/2"]


def test_exactly_one_writer_wins_each_cas_round(store):
    store.put_if_absent("counter", b"0")
    wins = []
    lock = threading.Lock()

    def worker(n):
        for _ in range(20):
            while True:
                data, etag = store.get("counter")
                if store.put_if_match("counter", str(int(data) + 1).encode(), etag):
                    with lock:
                        wins.append(n)
                    break
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert int(store.get("counter")[0]) == 120 == len(wins)    # no lost updates
