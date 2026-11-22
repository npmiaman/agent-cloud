import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "sdk" / "python")]

from agentcloud import AgentCloud  # noqa: E402
from agentcloud_server import create_app  # noqa: E402
from agentcloud_server.config import Settings  # noqa: E402


class Server:
    """An app over a data dir. `restart()` throws the app away and builds a new one on
    the same data, which is what an agent environment shutting down looks like to us."""

    def __init__(self, data_dir: Path, **overrides):
        self.data_dir, self.overrides = data_dir, overrides
        self.start()

    def start(self):
        s = Settings()
        s.data_dir = self.data_dir
        s.auth_rate_limit_per_minute = 100000
        s.rate_limit_per_minute = 100000
        for k, v in self.overrides.items():
            setattr(s, k, v)
        self.app = create_app(s)

    def restart(self):
        self.start()

    def client(self, token: str | None = None) -> AgentCloud:
        return AgentCloud.from_client(TestClient(self.app, base_url="http://test"), token)

    def signup(self, email="dev@example.com", password="correct-horse-battery") -> AgentCloud:
        r = self.client().http.post("/v1/auth/signup", json={"email": email, "password": password})
        assert r.status_code == 201, r.text
        return self.client(r.json()["token"])


@pytest.fixture
def server(tmp_path):
    return Server(tmp_path / "data")
