// End-to-end test of @agentcloud/sdk against a real Agent Cloud server.
//   node test/e2e.mjs            (server at $AGENTCLOUD_URL or http://127.0.0.1:8799)
// Build first: npx -p typescript@5.6.3 tsc
import { createHash, randomBytes } from "node:crypto";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  AgentCloud, AgentCloudError, Conflict, Forbidden, NotFound, Unauthenticated,
} from "../dist/index.js";

const BASE = process.env.AGENTCLOUD_URL ?? "http://127.0.0.1:8799";
const sha = (b) => createHash("sha256").update(b).digest("hex");
const run = Date.now().toString(36) + randomBytes(3).toString("hex");

let passed = 0;
let failed = 0;
function check(cond, label, extra = "") {
  if (cond) {
    passed++;
    console.log(`  ok    ${label}${extra ? `  (${extra})` : ""}`);
  } else {
    failed++;
    console.log(`  FAIL  ${label}${extra ? `  (${extra})` : ""}`);
  }
}
async function expectError(fn, classes, label) {
  try {
    await fn();
    check(false, label, "no error thrown");
  } catch (e) {
    const ok = classes.some((c) => e instanceof c);
    check(ok, label, e instanceof AgentCloudError ? `${e.name} ${e.status} ${e.code}` : String(e));
    return e;
  }
}
const section = (s) => console.log(`\n== ${s}`);

/** fetch wrapper that records which bearer each request used, so we can prove the exchange. */
function recordingFetch(log) {
  return async (url, init = {}) => {
    const auth = init.headers?.authorization ?? "";
    const r = await fetch(url, init);
    log.push({ method: init.method ?? "GET", path: new URL(url).pathname, bearer: auth.slice(7, 11), status: r.status });
    return r;
  };
}

const tmp = await mkdtemp(join(tmpdir(), "agentcloud-ts-e2e-"));
try {
  // ------------------------------------------------------------------ 1. user
  section("1. sign up a user (POST /v1/auth/signup)");
  const email = `ts-sdk-${run}@example.com`;
  const { client: user, user_id } = await AgentCloud.signup({ email, password: "correct-horse-battery", baseUrl: BASE });
  check(user_id?.startsWith("usr_"), "signup returned a user", `${email} -> ${user_id}`);
  const me = await user.me();
  check(me.kind === "user", "user session works", `GET /v1/me kind=${me.kind}`);

  // ------------------------------------------------------------------ 2. agent 1, credential only
  section("2. provision agent A; from here agent A uses ONLY its aca_ credential through the SDK");
  const provA = await user.agents.provision({ name: `researcher-${run}`, access: "full", storageMb: 64 });
  const credA = provA.credential.token;
  check(credA.startsWith("aca_"), "provision returned an agent credential", `${provA.agent.id}, workspace ${provA.workspace.id}`);

  const reqLogA = [];
  const exchangeEvents = [];
  const agentA = new AgentCloud({
    apiKey: credA, baseUrl: BASE, fetch: recordingFetch(reqLogA),
    onTokenExchange: (i) => exchangeEvents.push(i),
  });
  check(agentA.auth.state().exchanges === 0, "no exchange before first call");

  const env = await agentA.environment();
  check(env.identity.type === "agent" && env.identity.id === provA.agent.id, "environment discovery", `identity=${env.identity.name}, home=${env.home_workspace}`);
  const ws = await agentA.workspaces.get(provA.workspace.id);
  check(ws.id === provA.workspace.id, "client.workspaces.get()", `name=${ws.name}`);

  const csvV1 = "company,sector,employees\nAnthropic,AI,1000\nZephyrQuill,robotics,42\n";
  const csvPath = join(tmp, "companies.csv");
  await writeFile(csvPath, csvV1);

  const folder = await ws.files.mkdir("research/");
  check(folder.kind === "folder", "mkdir research/", folder.id);

  const up1 = await ws.files.upload({ path: "research/companies.csv", file: csvPath });
  check(!up1.pending && up1.version === 1 && up1.op === "create", "upload CSV from local path (upload session)", `v${up1.version} ${up1.op} sha=${up1.sha256?.slice(0, 12)}`);
  check(up1.sha256 === sha(Buffer.from(csvV1)), "server-committed sha256 == local sha256");

  const listing = await ws.files.list({ path: "research/" });
  check(Array.isArray(listing) && listing.some((e) => e.path === "research/companies.csv"), "list({ path: 'research/' })", listing.map((e) => e.path).join(", ") + `; nextCursor=${listing.nextCursor}`);

  const back = await ws.files.read("research/companies.csv");
  check(Buffer.from(back).equals(Buffer.from(csvV1)), "read back, bytes identical", `${back.byteLength} bytes`);

  const stat = await ws.files.stat("research/companies.csv");
  check(stat.id.startsWith("fil_") && stat.content_type === "text/csv", "stat", `${stat.id} ${stat.content_type} v${stat.version}`);

  const csvV2 = csvV1 + "Hooli,search,9000\n";
  const up2 = await ws.files.write("research/companies.csv", csvV2, { message: "add Hooli" });
  check(!up2.pending && up2.version === 2 && up2.op === "update", "overwrite -> v2", `${up2.op}`);
  check((await ws.files.readText("research/companies.csv")) === csvV2, "readText returns v2");

  const unchanged = await ws.files.write("research/companies.csv", csvV2);
  check(!unchanged.pending && unchanged.op === "unchanged" && unchanged.version === 2, "identical rewrite is a no-op", unchanged.op);

  const vers = await ws.files.versions("research/companies.csv");
  check(vers.length === 2 && vers[0].n === 2 && vers[1].n === 1, "versions()", vers.map((v) => `v${v.n}:${v.op}`).join(" "));

  const diff = await ws.files.diff("research/companies.csv", 1, 2);
  check(diff && typeof diff === "object", "diff(1, 2)", JSON.stringify(diff).slice(0, 110));

  const restored = await ws.files.restore("research/companies.csv", 1);
  check(!restored.pending && restored.version === 3 && restored.op === "restore", "restore v1 -> new v3", restored.op);
  check((await ws.files.readText("research/companies.csv")) === csvV1, "content after restore == v1");
  check((await ws.files.readText("research/companies.csv", { version: 2 })) === csvV2, "read({version: 2}) still returns v2");

  await expectError(() => ws.files.write("research/companies.csv", "x", { ifVersion: 1 }), [Conflict], "ifVersion mismatch -> Conflict");

  section("2b. ~3 MB binary round trip");
  const bin = randomBytes(3 * 1024 * 1024 + 123);
  const binSha = sha(bin);
  const upBin = await ws.files.upload({ path: "data/blob.bin", content: new Uint8Array(bin) });
  check(!upBin.pending && upBin.sha256 === binSha && upBin.size === bin.length, "upload 3 MB bytes", `${upBin.size} bytes sha=${binSha.slice(0, 12)}`);
  const dlPath = join(tmp, "out", "blob.bin");
  await ws.files.download("data/blob.bin", { to: dlPath });
  const dl = await readFile(dlPath);
  check(sha(dl) === binSha && dl.length === bin.length, "download via signed URL to local path, checksum matches", `${dl.length} bytes`);
  const viaPath = await ws.files.read("data/blob.bin");
  check(sha(viaPath) === binSha, "read by path, checksum matches");

  section("2c. listing, pagination, search, organising");
  for (const n of ["a", "b", "c", "d", "e"]) await ws.files.write(`notes/${n}.md`, `# note ${n}\n`);
  const p1 = await ws.files.list({ path: "notes/", limit: 2 });
  check(p1.length === 2 && p1.nextCursor, "page 1 (limit 2) has nextCursor", `${p1.map((e) => e.path)} -> ${p1.nextCursor}`);
  const all = await ws.files.listAll({ path: "notes/", limit: 2 });
  check(all.length === 5 && new Set(all.map((e) => e.path)).size === 5, "listAll follows cursors", all.map((e) => e.path.split("/").pop()).join(","));

  const byName = await ws.files.search({ query: "companies" });
  check(byName.some((e) => e.path === "research/companies.csv"), "files.search({query}) by name");
  const byExt = await ws.files.search({ ext: "md" });
  check(byExt.length === 5, "files.search({ext: 'md'})", `${byExt.length} hits`);

  const hits = await agentA.search("ZephyrQuill");
  check(hits.some((h) => h.path === "/research/companies.csv" && h.matched_in.includes("content")), "content search GET /v1/search?q=", hits.map((h) => `${h.path} [${h.matched_in}] "${h.snippet?.trim()}"`).join("; "));
  const wsHits = await ws.search("note c");
  check(wsHits.some((h) => h.path === "/notes/c.md"), "workspace.search scoped", wsHits.map((h) => h.path).join(","));

  const cp = await ws.files.copy("notes/a.md", "notes/a-copy.md");
  check(!cp.pending && cp.path === "notes/a-copy.md", "copy", JSON.stringify(cp.raw).slice(0, 90));
  await ws.files.move("notes/a-copy.md", "archive/a-copy.md");
  check(await ws.files.exists("archive/a-copy.md"), "move");
  await ws.files.rename("archive/a-copy.md", "a-renamed.md");
  check((await ws.files.exists("archive/a-renamed.md")) && !(await ws.files.exists("archive/a-copy.md")), "rename");
  const del = await ws.files.delete("archive/a-renamed.md");
  check(!del.pending && del.op === "delete" && !(await ws.files.exists("archive/a-renamed.md")), "delete (soft)");
  await expectError(() => ws.files.read("archive/a-renamed.md"), [NotFound], "read deleted file -> NotFound");

  // ------------------------------------------------------------------ 3. second agent
  section("3. agent B is denied until shared, then can read");
  const provB = await user.agents.provision({ name: `reader-${run}`, access: "read_only" });
  const agentB = new AgentCloud({ apiKey: provB.credential.token, baseUrl: BASE });
  await expectError(() => agentB.workspaces.get(ws.id), [NotFound, Forbidden], "B: get A's workspace before share");
  const wsFromB = agentB.workspaces.ref(ws.id);
  await expectError(() => wsFromB.files.read("research/companies.csv"), [NotFound, Forbidden], "B: read A's CSV before share");

  const share = await ws.files.share("research/companies.csv", { agentId: provB.agent.id, permission: "read" });
  check(share.share_id && share.status === "active", "A shares the CSV with B (POST /v1/files/{id}/shares)", share.share_id);
  const bRead = await wsFromB.files.read("research/companies.csv");
  check(Buffer.from(bRead).equals(Buffer.from(csvV1)), "B: read after share, bytes identical");
  const bDl = await wsFromB.files.download("research/companies.csv");
  check(sha(bDl) === sha(Buffer.from(csvV1)), "B: download via signed URL after share");
  await expectError(() => wsFromB.files.read("data/blob.bin"), [NotFound, Forbidden], "B: still denied on an unshared file");
  await expectError(() => wsFromB.files.write("research/companies.csv", "hijack"), [Forbidden, NotFound], "B: write denied (read-only share)");
  await agentA.shares.revoke(share.share_id);
  await expectError(() => wsFromB.files.read("research/companies.csv"), [NotFound, Forbidden], "B: denied again after revoke");

  // ------------------------------------------------------------------ 4. token exchange + recovery
  section("4. token exchange and recovery");
  const st = agentA.auth.state();
  check(st.kind === "agent" && st.exchanges === 1 && st.agentId === provA.agent.id, "aca_ credential was exchanged exactly once so far", JSON.stringify({ ...st, expiresAt: st.expiresAt && new Date(st.expiresAt).toISOString() }));
  const tokenCalls = reqLogA.filter((r) => r.path === "/v1/auth/agent/token");
  const acaElsewhere = reqLogA.filter((r) => r.bearer === "aca_" && r.path !== "/v1/auth/agent/token");
  const acx = reqLogA.filter((r) => r.bearer === "acx_");
  check(tokenCalls.length === 1 && tokenCalls[0].bearer === "aca_", "aca_ only ever sent to /v1/auth/agent/token", `${tokenCalls.length} exchange call(s)`);
  check(acaElsewhere.length === 0 && acx.length > 20, "every API call used the acx_ access token", `${acx.length} acx_ calls, ${acaElsewhere.length} aca_ calls elsewhere`);

  agentA.auth.invalidate();
  await ws.files.stat("research/companies.csv");
  check(agentA.auth.state().exchanges === 2, "after invalidate(): next call re-exchanges", `exchanges=${agentA.auth.state().exchanges}`);

  agentA.auth.setCachedToken("acx_not-a-real-token", 600);
  const before = reqLogA.length;
  const txt = await ws.files.readText("research/companies.csv");
  const seq = reqLogA.slice(before).map((r) => `${r.method} ${r.path} ${r.bearer} ${r.status}`);
  check(txt === csvV1 && agentA.auth.state().exchanges === 3, "bad cached token: 401 -> refresh -> retry succeeds", seq.join(" | "));

  agentA.auth.setCachedToken("acx_not-a-real-token", 600);
  const vBefore = (await ws.files.versions("notes/b.md")).length; // this call recovers (exchange #4)
  agentA.auth.setCachedToken("acx_not-a-real-token", 600);
  const w = await ws.files.write("notes/b.md", "# note b, edited after a 401\n");
  const vAfter = (await ws.files.versions("notes/b.md")).length;
  check(!w.pending && w.version === 2 && vAfter === vBefore + 1, "write after a 401 is applied exactly once", `versions ${vBefore} -> ${vAfter}, exchanges=${agentA.auth.state().exchanges}`);
  check(exchangeEvents.length === agentA.auth.state().exchanges, "onTokenExchange fired for every exchange", `${exchangeEvents.length}`);

  section("4b. credential rotation / revocation");
  const credId = provA.credential.id;
  const rotated = await user.agents.credentials.rotate(provA.agent.id, credId, { graceSeconds: 0 });
  check(rotated.token.startsWith("aca_") && rotated.replaces === credId, "rotate with graceSeconds=0", rotated.id);
  const err = await expectError(() => ws.files.stat("research/companies.csv"), [Unauthenticated], "old credential: SDK refresh fails cleanly with Unauthenticated");
  const agentA2 = new AgentCloud({ apiKey: rotated.token, baseUrl: BASE });
  const again = await agentA2.workspaces.ref(ws.id).files.readText("research/companies.csv");
  check(again === csvV1, "new credential works");
  void err;

  // ------------------------------------------------------------------ 5. approvals
  section("5. approval-gated agent: writes come back as Pending, not errors");
  const provC = await user.agents.provision({ name: `gated-${run}`, approval: "all" });
  const agentC = new AgentCloud({ apiKey: provC.credential.token, baseUrl: BASE });
  const wsC = agentC.workspaces.ref(provC.workspace.id);
  const pend = await wsC.files.write("plan.md", "# plan\n");
  check(pend.pending === true && pend.proposalId?.startsWith("prop_"), "write returns { pending: true, proposalId }", pend.proposalId);
  const pendingList = await user.approvals.list();
  check(pendingList.some((p) => p.id === pend.proposalId), "user sees it in approvals.list()");
  const decision = await user.approvals.approve(pend.proposalId, { note: "lgtm" });
  check(decision.status === "approved", "user approves", JSON.stringify(decision).slice(0, 90));
  check((await wsC.waitFor(pend, { pollMs: 100 })) === "approved" && (await wsC.files.readText("plan.md")) === "# plan\n", "agent sees approved content");
  const pend2 = await wsC.files.write("plan.md", "# plan v2\n");
  await user.approvals.reject(pend2.proposalId, { note: "no" });
  check((await wsC.waitFor(pend2, { pollMs: 100 })) === "rejected" && (await wsC.files.readText("plan.md")) === "# plan\n", "rejected write leaves file unchanged");

  // ------------------------------------------------------------------ 6. user-side CRUD
  section("6. workspaces + agents + credentials (user key)");
  const key = await user.apiKeys.create({ name: "e2e" });
  const u2 = new AgentCloud({ apiKey: key.token, baseUrl: BASE });
  const nws = await u2.workspaces.create({ name: `scratch-${run}`, storageLimitBytes: 10 * 1024 * 1024 });
  check(nws.id.startsWith("ws"), "workspaces.create (acu_ API key)", nws.id);
  const upd = await u2.workspaces.update(nws.id, { description: "updated" });
  check(upd.info.description === "updated", "workspaces.update");
  check((await u2.workspaces.list()).some((w) => w.id === nws.id), "workspaces.list");
  await u2.workspaces.delete(nws.id);
  await expectError(() => u2.workspaces.get(nws.id), [NotFound], "workspaces.delete then get -> NotFound");
  const ag = await u2.agents.create({ name: `bare-${run}` });
  const cred = await u2.agents.credentials.create(ag.id, { name: "ci", ttlSeconds: 3600 });
  check(cred.token.startsWith("aca_"), "agents.create + credentials.create");
  await u2.agents.credentials.revoke(ag.id, cred.id);
  const dead = new AgentCloud({ apiKey: cred.token, baseUrl: BASE });
  await expectError(() => dead.environment(), [Unauthenticated], "revoked credential -> Unauthenticated");
  await expectError(() => agentB.workspaces.create({ name: "nope" }), [Forbidden], "agent can't create workspaces -> Forbidden");
} catch (e) {
  failed++;
  console.log(`\nUNEXPECTED ERROR: ${e?.stack ?? e}`);
} finally {
  await rm(tmp, { recursive: true, force: true });
}

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
