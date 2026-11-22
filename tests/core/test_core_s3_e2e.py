"""The whole workflow on the S3 API (moto): no server, just conditional PUTs."""
import os

import pytest

from agentcloud_core import Repo


def test_agents_commit_merge_and_retract_on_s3():
    boto3 = pytest.importorskip("boto3")
    moto = pytest.importorskip("moto")
    from agentcloud_core import S3Store
    with moto.mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="acf-e2e")
        repo = Repo.init(S3Store("acf-e2e", client=s3), "projects/alpha", key=os.urandom(32))
        tx = repo.branch("agents/source").begin(writer="agent:source")
        root = tx.write("data/raw.csv", b"a,b\n1,2\n")
        tx.commit("raw")

        def worker(i):
            tx = repo.branch(f"agents/w{i}").begin(writer=f"agent:w{i}")
            tx.write(f"out/w{i}.md", f"derived {i}".encode(), inputs=[root])
            tx.commit("derive")
        for i in range(8):           # sequential: moto's mocking isn't thread-safe (concurrency: s3-server tests)
            worker(i)
        repo.merge_many([f"agents/w{i}" for i in range(8)] + ["agents/source"], "project")
        assert len(repo.branch("project").files()) == 9
        r = repo.retract(root, reason="bad export", by="human")
        assert len(r["affected"]) == 8
        again = Repo.open(S3Store("acf-e2e", client=s3), "projects/alpha", key=repo.key)
        assert len(again.retracted_files("project")) == 9
        raw = b"".join(s3.get_object(Bucket="acf-e2e", Key=o["Key"])["Body"].read()
                       for o in s3.list_objects_v2(Bucket="acf-e2e")["Contents"])
        assert b"derived 3" not in raw                     # encrypted at rest in the bucket
