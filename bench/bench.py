"""Latency/throughput benchmark against a real server process.

    python bench/bench.py            # starts its own server on :8790 with a temp data dir
Seeds 20 agents, 1,000 files in one workspace (+10 more workspaces), then measures
p50/p95 for the hot paths and mixed-read throughput with 8 concurrent clients.
"""
import json, os, statistics, subprocess, sys, tempfile, time
from pathlib import Path
import httpx

ROOT = Path(__file__).resolve().parents[1]
PORT = int(os.environ.get("BENCH_PORT", "8790"))
URL = f"http://127.0.0.1:{PORT}"


def start_server(data):
    env = dict(os.environ, AGENTCLOUD_INLINE_WORKER="0", AGENTCLOUD_RATE_LIMIT="10000000", AGENTCLOUD_AUTH_RATE_LIMIT="100000")
    extra = ["--workers", os.environ.get("BENCH_WORKERS", "1")]
    p = subprocess.Popen([sys.executable, "-m", "agentcloud_server", "--port", str(PORT), "--data", data, *extra],
                         cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            if httpx.get(URL + "/healthz", timeout=0.5).status_code == 200:
                return p
        except Exception:
            pass
        time.sleep(0.1)
    raise SystemExit("server did not start")


def timeit(fn, n):
    fn()  # warm
    ts = []
    for _ in range(n):
        t = time.perf_counter(); fn(); ts.append((time.perf_counter() - t) * 1000)
    ts.sort()
    return {"p50": round(statistics.median(ts), 2), "p95": round(ts[int(len(ts) * .95) - 1], 2)}


def _load(k, ws, auth, seconds):
    """One load-generating process: mixed reads (file GET, folder list, MCP read) for `seconds`."""
    A = {"Authorization": auth}
    cl = httpx.Client(base_url=URL, timeout=30)
    stop, i, n = time.time() + seconds, k, 0
    while time.time() < stop:
        i += 7
        if i % 3 == 0:
            cl.get(f"/v1/workspaces/{ws}/files/docs/f{i % 10}/note-{i % 1000}.md", headers=A)
        elif i % 3 == 1:
            cl.get(f"/v1/workspaces/{ws}/files", headers=A, params={"path": f"docs/f{i % 10}/"})
        else:
            cl.post("/mcp", headers=A, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                              "params": {"name": "cloud_read", "arguments": {"path": f"/docs/f{i % 10}/note-{i % 1000}.md"}}})
        n += 1
    return n


def main():
    data = tempfile.mkdtemp(prefix="acbench-")
    srv = start_server(data)
    try:
        c = httpx.Client(base_url=URL, timeout=30)
        tok = c.post("/v1/auth/signup", json={"email": "bench@example.com", "password": "bench-password"}).json()["token"]
        U = {"Authorization": f"Bearer {tok}"}
        agents = [c.post("/v1/agents/provision", headers=U, json={"name": f"agent-{i}", "access": "full"}).json() for i in range(20)]
        A0 = agents[0]
        A = {"Authorization": f"Bearer {A0['token']['token']}"}
        ws = A0["workspace"]["id"]
        for i in range(10):
            c.post("/v1/workspaces", headers=U, json={"name": f"extra-{i}"})
        t = time.perf_counter()
        words = "agents persistent storage market analysis competitor revenue churn pipeline report".split()
        for i in range(1000):
            body = f"# Note {i}\n" + " ".join(words[(i + k) % len(words)] for k in range(40)) + f"\nunique-token-{i}\n"
            c.put(f"/v1/workspaces/{ws}/files/docs/f{i // 100}/note-{i}.md", headers=A, content=body.encode())
        seed_s = time.perf_counter() - t
        n = 0

        def put():
            nonlocal n
            n += 1
            c.put(f"/v1/workspaces/{ws}/files/bench/w-{n}.txt", headers=A, content=b"x" * 1024).raise_for_status()

        mcp = lambda name, args: c.post("/mcp", headers=A, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                                  "params": {"name": name, "arguments": args}}).raise_for_status()
        R = {
            "seed_1000_writes_s": round(seed_s, 2),
            "write_1KB": timeit(put, 200),
            "read_file": timeit(lambda: c.get(f"/v1/workspaces/{ws}/files/docs/f3/note-321.md", headers=A).raise_for_status(), 300),
            "list_folder_100": timeit(lambda: c.get(f"/v1/workspaces/{ws}/files", headers=A, params={"path": "docs/f5/"}).raise_for_status(), 100),
            "list_workspaces_user": timeit(lambda: c.get("/v1/workspaces", headers=U).raise_for_status(), 50),
            "list_agents_user": timeit(lambda: c.get("/v1/agents", headers=U).raise_for_status(), 50),
            "overview": timeit(lambda: c.get("/v1/overview", headers=U).raise_for_status(), 50),
            "search_content": timeit(lambda: c.get("/v1/search", headers=A, params={"q": "unique-token-777"}).raise_for_status(), 20),
            "mcp_read": timeit(lambda: mcp("cloud_read", {"path": "/docs/f1/note-150.md"}), 200),
            "mcp_whoami": timeit(lambda: mcp("cloud_whoami", {}), 100),
            "dashboard_html": timeit(lambda: c.get("/").raise_for_status(), 50),
        }
        # throughput: 8 client *processes* (one Python client process caps out on its own GIL)
        import multiprocessing as mp
        with mp.get_context("spawn").Pool(8) as pool:
            counts = pool.starmap(_load, [(k, ws, A["Authorization"], 4.0) for k in range(8)])
        R["mixed_read_rps_8_clients"] = round(sum(counts) / 4, 1)
        print(json.dumps(R, indent=1))
    finally:
        srv.terminate()


if __name__ == "__main__":
    main()
