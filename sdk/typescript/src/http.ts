import { AgentCloudError, RateLimited, Unauthenticated, errorFromResponse } from "./errors.js";
import { randomId, sleep } from "./util.js";

export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

export interface TokenState {
  /** "user" for acu_/acs_ keys, "agent" for aca_ credentials, "access" for a bare acx_ token. */
  kind: "user" | "agent" | "access" | "none";
  /** Number of aca_ -> acx_ exchanges performed so far. */
  exchanges: number;
  /** Local epoch ms when the cached access token expires (agent credentials only). */
  expiresAt: number | null;
  agentId: string | null;
  hasCachedToken: boolean;
}

/**
 * Owns the credential. User keys are sent as-is. Agent credentials (aca_) are
 * exchanged for short-lived access tokens (acx_), cached, refreshed ahead of
 * expiry, and refreshed once more if the server answers 401.
 */
export class Auth {
  private token: string | null = null;
  private expiresAt = 0;
  private inflight: Promise<string> | null = null;
  private _exchanges = 0;
  private agentId: string | null = null;
  readonly kind: TokenState["kind"];

  constructor(
    private readonly credential: string | undefined,
    private readonly http: Http,
    private readonly refreshMarginMs = 60_000,
    private readonly onExchange?: (info: { agentId: string; expiresIn: number }) => void,
  ) {
    const c = credential ?? "";
    this.kind = !c ? "none" : c.startsWith("aca_") ? "agent" : c.startsWith("acx_") ? "access" : "user";
  }

  state(): TokenState {
    return {
      kind: this.kind, exchanges: this._exchanges, agentId: this.agentId, hasCachedToken: this.token !== null,
      expiresAt: this.kind === "agent" && this.token ? this.expiresAt : null,
    };
  }

  /** Drop the cached access token; the next request exchanges the credential again. */
  invalidate(): void {
    this.token = null;
    this.expiresAt = 0;
  }

  /** Test hook / escape hatch: replace the cached access token (e.g. one from elsewhere). */
  setCachedToken(token: string, expiresInSeconds: number): void {
    this.token = token;
    this.expiresAt = Date.now() + expiresInSeconds * 1000;
  }

  /** The bearer value to send right now (may trigger an exchange). */
  async header(): Promise<string | undefined> {
    if (this.kind === "none") return undefined;
    if (this.kind !== "agent") return `Bearer ${this.credential}`;
    if (this.token && Date.now() < this.expiresAt) return `Bearer ${this.token}`;
    return `Bearer ${await this.exchange()}`;
  }

  /** Called after a 401. Returns true if a retry with a fresh token makes sense. */
  async recover(failedHeader: string | undefined): Promise<boolean> {
    if (this.kind !== "agent") return false;
    // Another request may already have refreshed since this one was sent.
    if (this.token && failedHeader !== `Bearer ${this.token}` && Date.now() < this.expiresAt) return true;
    this.invalidate();
    await this.exchange();
    return true;
  }

  private exchange(): Promise<string> {
    if (this.inflight) return this.inflight;
    this.inflight = (async () => {
      const r = await this.http.raw("POST", "/v1/auth/agent/token", { authorization: `Bearer ${this.credential}` });
      const d = (await r.json()) as { access_token: string; expires_in: number; agent_id: string };
      this._exchanges += 1;
      this.agentId = d.agent_id;
      // Refresh ahead of expiry; for very short TTLs, at half-life.
      const margin = Math.min(this.refreshMarginMs, (d.expires_in * 1000) / 2);
      this.token = d.access_token;
      this.expiresAt = Date.now() + d.expires_in * 1000 - margin;
      this.onExchange?.({ agentId: d.agent_id, expiresIn: d.expires_in });
      return d.access_token;
    })().finally(() => {
      this.inflight = null;
    });
    return this.inflight;
  }
}

export interface RequestOptions {
  query?: Record<string, string | number | boolean | null | undefined>;
  json?: unknown;
  body?: BodyInit;
  headers?: Record<string, string>;
  /** Skip the Authorization header (signed data-plane URLs). */
  noAuth?: boolean;
  /**
   * Safe to retry on network errors, 429 and 5xx. GETs are; writes are made
   * safe by an Idempotency-Key the SDK attaches automatically.
   */
  retry?: boolean;
  /** Don't attach an Idempotency-Key to a write (e.g. the server rejects it). */
  noIdempotencyKey?: boolean;
}

const WRITE_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);
const RETRY_STATUS = new Set([429, 502, 503, 504]);

export class Http {
  auth!: Auth;
  constructor(
    readonly baseUrl: string,
    private readonly fetchImpl: FetchLike,
    private readonly maxRetries: number,
    private readonly timeoutMs: number,
    private readonly userAgent: string,
  ) {}

  url(path: string, query?: RequestOptions["query"]): string {
    const u = new URL(path.startsWith("http") ? path : this.baseUrl + path);
    for (const [k, v] of Object.entries(query ?? {})) if (v !== undefined && v !== null) u.searchParams.set(k, String(v));
    return u.toString();
  }

  /** One raw HTTP call with error mapping but no auth/retry logic. */
  async raw(method: string, path: string, headers: Record<string, string> = {}, body?: BodyInit, query?: RequestOptions["query"]): Promise<Response> {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), this.timeoutMs);
    let r: Response;
    try {
      r = await this.fetchImpl(this.url(path, query), { method, headers: { "user-agent": this.userAgent, ...headers }, body, signal: ctl.signal });
    } finally {
      clearTimeout(timer);
    }
    if (r.status >= 400) {
      const text = await r.text();
      let parsed: unknown;
      try {
        parsed = JSON.parse(text);
      } catch {
        parsed = undefined;
      }
      throw errorFromResponse(r.status, parsed, text, r.headers);
    }
    return r;
  }

  /** Authenticated request with token refresh on 401 and safe retries. */
  async request(method: string, path: string, opts: RequestOptions = {}): Promise<Response> {
    const headers: Record<string, string> = { ...(opts.headers ?? {}) };
    let body = opts.body;
    if (opts.json !== undefined) {
      headers["content-type"] = "application/json";
      body = JSON.stringify(opts.json);
    }
    const isWrite = WRITE_METHODS.has(method);
    if (isWrite && !opts.noAuth && !opts.noIdempotencyKey && !headers["idempotency-key"]) headers["idempotency-key"] = "sdk_" + randomId();
    const retryable = opts.retry ?? (!isWrite || !!headers["idempotency-key"]);

    let refreshed = false;
    // The server scopes Idempotency-Keys by the exact Authorization header, so the
    // token is pinned for transient retries; it only changes after a 401 (which
    // means the request was never processed). Tokens refresh 60s before expiry,
    // which leaves room for the backoff below.
    let auth = opts.noAuth ? undefined : await this.auth.header();
    for (let attempt = 0; ; attempt++) {
      const h = auth ? { ...headers, authorization: auth } : headers;
      try {
        return await this.raw(method, path, h, body, opts.query);
      } catch (e) {
        // A 401 means the request never reached a handler, so retrying it with a
        // fresh token cannot apply a write twice.
        if (e instanceof Unauthenticated && !refreshed && !opts.noAuth && (await this.auth.recover(auth))) {
          refreshed = true;
          auth = await this.auth.header();
          continue;
        }
        const transient = e instanceof AgentCloudError ? RETRY_STATUS.has(e.status) || e.code === "IDEMPOTENCY_IN_PROGRESS" : isNetworkError(e);
        if (!retryable || !transient || attempt >= this.maxRetries) throw e;
        const ra = e instanceof RateLimited && e.retryAfter ? e.retryAfter * 1000 : 0;
        await sleep(Math.max(ra, Math.min(8000, 250 * 2 ** attempt) * (0.5 + Math.random())));
      }
    }
  }

  async json<T = any>(method: string, path: string, opts: RequestOptions = {}): Promise<T> {
    const r = await this.request(method, path, opts);
    return (await r.json()) as T;
  }
}

function isNetworkError(e: unknown): boolean {
  return e instanceof TypeError || (e instanceof Error && (e.name === "AbortError" || e.name === "TimeoutError"));
}
