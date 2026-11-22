"""Object store conformance: every backend must give the same answers (SPEC §2, §10).

Backends: memory, local directory, S3 through moto (in-process emulator), and a real
S3-compatible server when AGENTCLOUD_TEST_S3_ENDPOINT is set (CI runs LocalStack). Moto's request
mocking isn't thread-safe, so concurrent tests run against that server instead of moto.
"""
import os
import threading
import uuid

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


def _minio():
    endpoint = os.environ.get("AGENTCLOUD_TEST_S3_ENDPOINT")
    if not endpoint:
        pytest.skip("set AGENTCLOUD_TEST_S3_ENDPOINT to test a real S3-compatible server")
    boto3 = pytest.importorskip("boto3")
    from botocore.config import Config

    from agentcloud_core.store import S3Store
    s3 = boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1",
                      aws_access_key_id=os.environ.get("AGENTCLOUD_TEST_S3_KEY", "test"),
                      aws_secret_access_key=os.environ.get("AGENTCLOUD_TEST_S3_SECRET", "test"),
                      config=Config(max_pool_connections=32))
    bucket = f"acf-{uuid.uuid4().hex[:12]}"
    s3.create_bucket(Bucket=bucket)
    return S3Store(bucket, client=s3)


@pytest.fixture(params=["memory", "local", "s3", "s3-server"])
def store(request, tmp_path):
    if request.param == "memory":
        yield MemoryStore()
    elif request.param == "local":
        yield LocalStore(tmp_path / "objects")
    elif request.param == "s3-server":
        yield _minio()
    else:
        s, m = _s3()
        s.emulated = True
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
    if getattr(store, "emulated", False):
        pytest.skip("moto's mocking isn't thread-safe; the s3-server backend (LocalStack in CI) covers this")
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
