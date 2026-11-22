import { Auth, type FetchLike, Http, type TokenState } from "./http.js";
import type { Agent, Credential, Environment, Grant, Proposal, Provisioned, SearchHit, WorkspaceInfo } from "./types.js";
import { Workspace } from "./workspace.js";

export * from "./errors.js";
export * from "./types.js";
export { Files, Workspace, toWriteResult } from "./workspace.js";
export type { FileList, ListOptions, SearchOptions, UploadOptions, WriteOptions } from "./workspace.js";
export type { BinaryLike } from "./util.js";
export type { FetchLike, TokenState } from "./http.js";

export const VERSION = "0.1.0";

export interface AgentCloudOptions {
  /**
   * A user API key (acu_…), a dashboard session (acs_…), or an agent credential
   * (aca_…). Agent credentials are exchanged for short-lived access tokens automatically.
   * Defaults to $AGENT_CLOUD_KEY, then $AGENTCLOUD_TOKEN.
   */
  apiKey?: string;
  /** Defaults to $AGENTCLOUD_URL, then http://127.0.0.1:8787. */
  baseUrl?: string;
  /** Retries for transient failures (network, 429, 502–504). Writes are retried only with an Idempotency-Key. Default 2. */
  maxRetries?: number;
  /** Per-request timeout. Default 60s. */
  timeoutMs?: number;
  /** Refresh an exchanged access token this long before it expires. Default 60s. */
  refreshMarginMs?: number;
  /** Custom fetch (tests, proxies). Defaults to globalThis.fetch. */
  fetch?: FetchLike;
  /** Called after every aca_ → acx_ exchange. */
  onTokenExchange?: (info: { agentId: string; expiresIn: number }) => void;
}

function env(name: string): string | undefined {
  return (globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env?.[name] || undefined;
}

export class AgentCloud {
  readonly baseUrl: string;
  /** @internal */
  readonly http: Http;
  private readonly _auth: Auth;

  readonly workspaces: Workspaces;
  readonly agents: Agents;
  readonly approvals: Approvals;
  readonly shares: Shares;
  readonly apiKeys: ApiKeys;

  constructor(opts: AgentCloudOptions = {}) {
    this.baseUrl = (opts.baseUrl ?? env("AGENTCLOUD_URL") ?? "http://127.0.0.1:8787").replace(/\/+$/, "");
    const f = opts.fetch ?? globalThis.fetch?.bind(globalThis);
    if (!f) throw new Error("global fetch is unavailable (Node 18+ required) — pass `fetch`");
    this.http = new Http(this.baseUrl, f, opts.maxRetries ?? 2, opts.timeoutMs ?? 60_000, `agentcloud-sdk-ts/${VERSION}`);
    this._auth = new Auth(opts.apiKey ?? env("AGENT_CLOUD_KEY") ?? env("AGENTCLOUD_TOKEN"), this.http, opts.refreshMarginMs, opts.onTokenExchange);
    this.http.auth = this._auth;
    this.workspaces = new Workspaces(this.http);
    this.agents = new Agents(this.http);
    this.approvals = new Approvals(this.http);
    this.shares = new Shares(this.http);
    this.apiKeys = new ApiKeys(this.http);
  }

  /** Token cache controls (agent credentials). */
  get auth() {
    const a = this._auth;
    return {
      state: (): TokenState => a.state(),
      /** Forget the cached access token; the next call exchanges the credential again. */
      invalidate: () => a.invalidate(),
      /** Replace the cached access token (mostly for tests). */
      setCachedToken: (token: string, expiresInSeconds: number) => a.setCachedToken(token, expiresInSeconds),
    };
  }

  /** Create an account and return a client authenticated with its session token. */
  static async signup(opts: { email: string; password: string; organisation?: string; baseUrl?: string; fetch?: FetchLike }) {
    const anon = new AgentCloud({ baseUrl: opts.baseUrl, fetch: opts.fetch, apiKey: "" });
    const d = await anon.http.json("POST", "/v1/auth/signup", { json: { email: opts.email, password: opts.password, organisation: opts.organisation }, noIdempotencyKey: true, retry: false });
    return { client: new AgentCloud({ baseUrl: anon.baseUrl, fetch: opts.fetch, apiKey: d.token }), ...d } as
      { client: AgentCloud; token: string; user_id: string; organisation_id: string; project_id: string; email: string };
  }

  static async login(opts: { email: string; password: string; baseUrl?: string; fetch?: FetchLike }) {
    const anon = new AgentCloud({ baseUrl: opts.baseUrl, fetch: opts.fetch, apiKey: "" });
    const d = await anon.http.json("POST", "/v1/auth/login", { json: { email: opts.email, password: opts.password }, noIdempotencyKey: true });
    return { client: new AgentCloud({ baseUrl: anon.baseUrl, fetch: opts.fetch, apiKey: d.token }), ...d } as
      { client: AgentCloud; token: string; user_id: string; email: string };
  }

  /** Who am I: a user (with organisations) or an agent (with grants and environment). */
  me(): Promise<Record<string, any>> {
    return this.http.json("GET", "/v1/me");
  }

  /** Agent environment discovery: identity, home workspace, storage, capabilities. */
  environment(): Promise<Environment> {
    return this.http.json("GET", "/v1/environment");
  }

  /** Name + text-content search across every workspace the caller can read. */
  async search(q: string, opts: { workspace?: string; path?: string; limit?: number } = {}): Promise<SearchHit[]> {
    return (await this.http.json<{ results: SearchHit[] }>("GET", "/v1/search", { query: { q, ...opts } })).results;
  }

  /** The caller's home workspace (agents) or the only reachable one. */
  async home(): Promise<Workspace> {
    const e = await this.environment();
    const id = e.home_workspace ?? (e.workspaces.length === 1 ? e.workspaces[0]!.id : null);
    if (!id) throw new Error("no home workspace; use client.workspaces.get(id)");
    return this.workspaces.get(id);
  }
}

class Workspaces {
  constructor(private readonly http: Http) {}

  /** Fetches the workspace (so a bad id or missing access fails here). */
  async get(id: string): Promise<Workspace> {
    const w = new Workspace(this.http, id);
    await w.refresh();
    return w;
  }

  /** A handle without a round trip. */
  ref(id: string): Workspace {
    return new Workspace(this.http, id);
  }

  async list(opts: { projectId?: string } = {}): Promise<Workspace[]> {
    const rows = await this.http.json<WorkspaceInfo[]>("GET", "/v1/workspaces", { query: { project_id: opts.projectId } });
    return rows.map((r) => new Workspace(this.http, r.id, r));
  }

  async create(opts: { name: string; description?: string; projectId?: string; storageLimitBytes?: number; retentionDays?: number }): Promise<Workspace> {
    const d = await this.http.json<WorkspaceInfo>("POST", "/v1/workspaces", {
      json: { name: opts.name, description: opts.description ?? "", project_id: opts.projectId, storage_limit_bytes: opts.storageLimitBytes, retention_days: opts.retentionDays },
    });
    return new Workspace(this.http, d.id, d);
  }

  async update(id: string, patch: { name?: string; description?: string; storageLimitBytes?: number; retentionDays?: number; projectId?: string }): Promise<Workspace> {
    const d = await this.http.json<WorkspaceInfo>("PATCH", `/v1/workspaces/${encodeURIComponent(id)}`, {
      json: { name: patch.name, description: patch.description, storage_limit_bytes: patch.storageLimitBytes, retention_days: patch.retentionDays, project_id: patch.projectId },
    });
    return new Workspace(this.http, d.id, d);
  }

  async delete(id: string): Promise<void> {
    await this.http.request("DELETE", `/v1/workspaces/${encodeURIComponent(id)}`);
  }
}

class Credentials {
  constructor(private readonly http: Http) {}
  private u(agentId: string, rest = "") {
    return `/v1/agents/${encodeURIComponent(agentId)}/credentials${rest}`;
  }
  create(agentId: string, opts: { name?: string; ttlSeconds?: number; scopes?: string[] } = {}): Promise<Credential> {
    return this.http.json("POST", this.u(agentId), { json: { name: opts.name ?? "default", ttl_seconds: opts.ttlSeconds, scopes: opts.scopes } });
  }
  list(agentId: string): Promise<Record<string, unknown>[]> {
    return this.http.json("GET", this.u(agentId));
  }
  /** New secret with the same scopes; the old one keeps working for `graceSeconds` (default 3600). */
  rotate(agentId: string, credentialId: string, opts: { graceSeconds?: number } = {}): Promise<Credential> {
    return this.http.json("POST", this.u(agentId, `/${encodeURIComponent(credentialId)}/rotate`), { json: { grace_seconds: opts.graceSeconds ?? 3600 } });
  }
  async revoke(agentId: string, credentialId: string): Promise<void> {
    await this.http.request("DELETE", this.u(agentId, `/${encodeURIComponent(credentialId)}`));
  }
}

class Agents {
  readonly credentials: Credentials;
  constructor(private readonly http: Http) {
    this.credentials = new Credentials(http);
  }
  create(opts: { name: string; description?: string; projectId?: string; metadata?: Record<string, unknown> }): Promise<Agent> {
    return this.http.json("POST", "/v1/agents", { json: { name: opts.name, description: opts.description ?? "", project_id: opts.projectId, metadata: opts.metadata ?? {} } });
  }
  /** Agent + its own workspace + storage + access + a credential, in one call. The credential is shown once. */
  provision(opts: { name: string; description?: string; projectId?: string; storageMb?: number; access?: "read_only" | "read_write" | "full"; approval?: "none" | "overwrite" | "all" }): Promise<Provisioned> {
    return this.http.json("POST", "/v1/agents/provision", {
      json: { name: opts.name, description: opts.description ?? "", project_id: opts.projectId, storage_mb: opts.storageMb, access: opts.access ?? "read_write", approval: opts.approval ?? "none" },
    });
  }
  list(opts: { projectId?: string } = {}): Promise<Agent[]> {
    return this.http.json("GET", "/v1/agents", { query: { project_id: opts.projectId } });
  }
  get(id: string): Promise<Agent> {
    return this.http.json("GET", `/v1/agents/${encodeURIComponent(id)}`);
  }
  update(id: string, patch: { name?: string; description?: string; disabled?: boolean; metadata?: Record<string, unknown>; projectId?: string }): Promise<Agent> {
    return this.http.json("PATCH", `/v1/agents/${encodeURIComponent(id)}`, {
      json: { name: patch.name, description: patch.description, disabled: patch.disabled, metadata: patch.metadata, project_id: patch.projectId },
    });
  }
  /** Disables the agent and revokes its credentials and grants. Its workspaces remain. */
  async delete(id: string): Promise<void> {
    await this.http.request("DELETE", `/v1/agents/${encodeURIComponent(id)}`);
  }
}

class Approvals {
  constructor(private readonly http: Http) {}
  /** Pending proposals across the organisation (users only). */
  list(): Promise<Proposal[]> {
    return this.http.json("GET", "/v1/approvals");
  }
  get(proposalId: string): Promise<Proposal> {
    return this.http.json("GET", `/v1/approvals/${encodeURIComponent(proposalId)}`);
  }
  approve(proposalId: string, opts: { note?: string; force?: boolean } = {}): Promise<Record<string, unknown>> {
    return this.http.json("POST", `/v1/approvals/${encodeURIComponent(proposalId)}/approve`, { json: { note: opts.note ?? "", force: opts.force ?? false } });
  }
  reject(proposalId: string, opts: { note?: string } = {}): Promise<Record<string, unknown>> {
    return this.http.json("POST", `/v1/approvals/${encodeURIComponent(proposalId)}/reject`, { json: { note: opts.note ?? "" } });
  }
}

class Shares {
  constructor(private readonly http: Http) {}
  async revoke(shareId: string): Promise<void> {
    await this.http.request("DELETE", `/v1/shares/${encodeURIComponent(shareId)}`);
  }
}

class ApiKeys {
  constructor(private readonly http: Http) {}
  create(opts: { name?: string; ttlSeconds?: number } = {}): Promise<{ id: string; token: string; prefix: string; expires_at: number | null }> {
    return this.http.json("POST", "/v1/api-keys", { json: { name: opts.name ?? "default", ttl_seconds: opts.ttlSeconds } });
  }
  list(): Promise<Record<string, unknown>[]> {
    return this.http.json("GET", "/v1/api-keys");
  }
  async revoke(id: string): Promise<void> {
    await this.http.request("DELETE", `/v1/api-keys/${encodeURIComponent(id)}`);
  }
}

export type { Grant };
export default AgentCloud;
