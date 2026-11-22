import { AgentCloudError, ChecksumMismatch } from "./errors.js";
import type { Http } from "./http.js";
import type {
  DownloadInfo, FileEntry, FileMeta, Grant, Pending, Proposal, SearchHit, Share, UploadSession, VersionInfo,
  WorkspaceInfo, WriteResult,
} from "./types.js";
import { type BinaryLike, basename, encodePath, guessContentType, readLocalFile, sha256Hex, sleep, toBytes, trimTrailingSlashes, writeLocalFile } from "./util.js";

/** Convert a server write response into Written | Pending. */
export function toWriteResult(d: Record<string, any>): WriteResult {
  if (d.status === "pending_approval") {
    return { pending: true, proposalId: d.proposal_id, path: d.path, baseVersion: d.base_version ?? null, raw: d };
  }
  return {
    pending: false, id: d.id, path: d.path, version: d.version, op: d.op, size: d.size, sha256: d.sha256,
    contentType: d.content_type, raw: d,
  };
}

export interface WriteOptions {
  contentType?: string;
  /** Conditional write: fail with Conflict unless the file is at this version (0 = must not exist). */
  ifVersion?: number;
  /** Recorded in the version history. */
  message?: string;
}

export interface UploadOptions extends WriteOptions {
  /** Destination path in the workspace. Defaults to the local file's name. */
  path?: string;
  /** A local file path (Node), or the content itself as bytes / Blob. */
  file?: string | Exclude<BinaryLike, string>;
  /** Content to upload; a string here is UTF-8 text, never a path. */
  content?: BinaryLike;
}

export interface ListOptions {
  /** Folder to list ("research/"). Alias: prefix. */
  path?: string;
  prefix?: string;
  recursive?: boolean;
  /** Page size (server max 5000). */
  limit?: number;
  cursor?: string | null;
  includeDeleted?: boolean;
  deletedOnly?: boolean;
}

export interface SearchOptions extends ListOptions {
  /** Name substring. */
  query?: string;
  /** Extension filter, e.g. "csv". */
  ext?: string;
  /** Actor, e.g. "agent:agt_…". */
  createdBy?: string;
  /** Epoch seconds. */
  modifiedAfter?: number;
  modifiedBefore?: number;
}

/** An array of entries plus the cursor for the next page (null when done). */
export type FileList = FileEntry[] & { nextCursor: string | null };

export class Files {
  constructor(private readonly http: Http, private readonly ws: Workspace) {}

  private u(rest: string) {
    return `/v1/workspaces/${encodeURIComponent(this.ws.id)}/${rest}`;
  }

  // ------------------------------------------------------------ writing

  /**
   * Upload a file through an upload session: hash locally, reserve, PUT the bytes
   * to the signed URL, then complete with the checksum so the server verifies
   * exactly what arrived. Returns the committed version, or Pending if the
   * workspace requires approval.
   */
  async upload(opts: UploadOptions): Promise<WriteResult> {
    let data: Uint8Array;
    let dest = opts.path;
    if (typeof opts.file === "string") {
      data = await readLocalFile(opts.file);
      dest ??= basename(opts.file);
    } else if (opts.file !== undefined) {
      data = await toBytes(opts.file);
    } else if (opts.content !== undefined) {
      data = await toBytes(opts.content);
    } else {
      throw new TypeError("upload needs `file` (local path or bytes) or `content`");
    }
    if (!dest) throw new TypeError("upload needs a destination `path`");
    return this.writeBytes(dest, data, opts);
  }

  /** Write text or bytes to a path (always via an upload session). */
  async write(path: string, data: BinaryLike, opts: WriteOptions = {}): Promise<WriteResult> {
    const contentType = opts.contentType ?? (typeof data === "string" && !guessContentType(path) ? "text/plain; charset=utf-8" : undefined);
    return this.writeBytes(path, await toBytes(data), { ...opts, contentType });
  }

  writeText(path: string, text: string, opts: WriteOptions = {}): Promise<WriteResult> {
    return this.write(path, text, opts);
  }

  private async writeBytes(path: string, data: Uint8Array, opts: WriteOptions): Promise<WriteResult> {
    const checksum = await sha256Hex(data);
    const session = await this.http.json<UploadSession>("POST", this.u("uploads"), {
      json: {
        path, size_bytes: data.byteLength, content_type: opts.contentType ?? guessContentType(path) ?? null,
        checksum_sha256: checksum, if_version: opts.ifVersion ?? null, message: opts.message ?? "",
      },
    });
    if (!session.upload_url) throw new AgentCloudError(500, "NO_UPLOAD_URL", "server did not return an upload_url");
    // Signed URL: the signature is the authorisation, so no bearer token. A re-PUT
    // replaces the staged bytes, so retrying it is safe.
    const put = await this.http.json<{ checksum_sha256: string; received_bytes: number }>("PUT", session.upload_url, {
      body: data as BodyInit, noAuth: true, retry: true, headers: { "content-type": "application/octet-stream" },
    });
    if (put.checksum_sha256 !== checksum || put.received_bytes !== data.byteLength) {
      await this.http.request("POST", `/v1/uploads/${session.upload_id}/abort`).catch(() => {});
      throw new ChecksumMismatch(422, "CHECKSUM_MISMATCH", `server received ${put.received_bytes} bytes with sha256 ${put.checksum_sha256}; expected ${data.byteLength} / ${checksum}`);
    }
    // /complete is idempotent server-side (completing a committed upload returns the same result).
    const done = await this.http.json<UploadSession>("POST", `/v1/uploads/${session.upload_id}/complete`, {
      json: { checksum_sha256: checksum }, retry: true,
    });
    if (done.state !== "COMMITTED" || !done.result) {
      throw new AgentCloudError(500, done.error?.code ?? "UPLOAD_NOT_COMMITTED", done.error?.message ?? `upload ended in state ${done.state}`);
    }
    const res = toWriteResult(done.result);
    if (!res.pending && res.sha256 && res.sha256 !== checksum) {
      throw new ChecksumMismatch(500, "CHECKSUM_MISMATCH", `committed sha256 ${res.sha256} != local ${checksum}`);
    }
    return res;
  }

  // ------------------------------------------------------------ reading

  /** Read a file's bytes by path (optionally an older version). Verifies X-SHA256. */
  async read(path: string, opts: { version?: number } = {}): Promise<Uint8Array> {
    const r = await this.http.request("GET", this.u(`files/${encodePath(path)}`), { query: { version: opts.version } });
    const data = new Uint8Array(await r.arrayBuffer());
    await verify(data, r.headers.get("x-sha256"));
    return data;
  }

  async readText(path: string, opts: { version?: number } = {}): Promise<string> {
    return new TextDecoder().decode(await this.read(path, opts));
  }

  /** A short-lived signed download URL for a file (by path or id). */
  async downloadUrl(pathOrId: string, opts: { version?: number } = {}): Promise<DownloadInfo & { absoluteUrl: string }> {
    const id = await this.idOf(pathOrId);
    const d = await this.http.json<DownloadInfo>("GET", `/v1/files/${encodeURIComponent(id)}/download`, { query: { version: opts.version } });
    return { ...d, absoluteUrl: this.http.url(d.url) };
  }

  /**
   * Download via a signed URL. With `to`, writes atomically to that local path
   * (Node) and returns it; without, returns the bytes. Checksum-verified either way.
   */
  async download(pathOrId: string, opts: { to: string; version?: number }): Promise<string>;
  async download(pathOrId: string, opts?: { version?: number }): Promise<Uint8Array>;
  async download(pathOrId: string, opts: { to?: string; version?: number } = {}): Promise<string | Uint8Array> {
    const d = await this.downloadUrl(pathOrId, opts);
    const r = await this.http.request("GET", d.url, { noAuth: true });
    const data = new Uint8Array(await r.arrayBuffer());
    await verify(data, d.checksum_sha256);
    if (opts.to) {
      await writeLocalFile(opts.to, data);
      return opts.to;
    }
    return data;
  }

  // ------------------------------------------------------------ listing & metadata

  /** One page of a folder listing. `.nextCursor` is set when there is more. */
  async list(opts: ListOptions = {}): Promise<FileList> {
    return this.page(opts, {});
  }

  /** Iterate every entry, following cursors. */
  async *iterate(opts: SearchOptions = {}): AsyncGenerator<FileEntry> {
    let cursor = opts.cursor ?? null;
    do {
      const page = await this.page({ ...opts, cursor }, searchParams(opts));
      yield* page;
      cursor = page.nextCursor;
    } while (cursor);
  }

  /** All entries (follows every cursor). */
  async listAll(opts: SearchOptions = {}): Promise<FileEntry[]> {
    const out: FileEntry[] = [];
    for await (const e of this.iterate(opts)) out.push(e);
    return out;
  }

  /** Filter by name, extension, creator and modification time (one page). */
  async search(opts: SearchOptions | string): Promise<FileList> {
    const o = typeof opts === "string" ? { query: opts, recursive: true } : { recursive: true, ...opts };
    return this.page(o, searchParams(o));
  }

  private async page(opts: ListOptions, extra: Record<string, string | number | undefined>): Promise<FileList> {
    const d = await this.http.json<{ entries: FileEntry[]; next_cursor: string | null }>("GET", this.u("files"), {
      query: {
        path: (opts.path ?? opts.prefix ?? "").replace(/^\/+/, ""), recursive: opts.recursive || undefined,
        limit: opts.limit, cursor: opts.cursor ?? undefined, include_deleted: opts.includeDeleted || undefined,
        deleted_only: opts.deletedOnly || undefined, ...extra,
      },
    });
    return Object.assign(d.entries, { nextCursor: d.next_cursor ?? null });
  }

  async stat(path: string): Promise<FileMeta> {
    return this.http.json("GET", this.u(`meta/${encodePath(path)}`));
  }

  async exists(path: string): Promise<boolean> {
    try {
      return !(await this.stat(path)).deleted;
    } catch (e) {
      if (e instanceof AgentCloudError && e.status === 404) return false;
      throw e;
    }
  }

  // ------------------------------------------------------------ organising

  async mkdir(path: string): Promise<{ id: string; path: string; kind: "folder" }> {
    return this.http.json("POST", this.u("folders"), { json: { path } });
  }

  /** Move a file or folder ("a/" moves a folder). */
  async move(from: string, to: string, opts: { message?: string } = {}): Promise<{ moved: Record<string, unknown>[] }> {
    return this.http.json("POST", this.u("move"), { json: { from, to, message: opts.message ?? "" } });
  }

  /** Rename in place: `rename("reports/q1.csv", "q1-final.csv")`. */
  async rename(path: string, newName: string, opts: { message?: string } = {}) {
    if (newName.includes("/")) throw new TypeError("newName must be a name, not a path; use move()");
    const isFolder = path.endsWith("/");
    const trimmed = trimTrailingSlashes(path);
    const parent = trimmed.includes("/") ? trimmed.slice(0, trimmed.lastIndexOf("/") + 1) : "";
    return this.move(path, parent + newName + (isFolder ? "/" : ""), opts);
  }

  async copy(from: string, to: string, opts: { message?: string } = {}): Promise<WriteResult> {
    return toWriteResult(await this.http.json("POST", this.u("copy"), { json: { from, to, message: opts.message ?? "" } }));
  }

  /** Soft delete: history is kept and `restore` brings it back. */
  async delete(path: string, opts: { ifVersion?: number; message?: string } = {}): Promise<WriteResult> {
    const headers: Record<string, string> = {};
    if (opts.ifVersion !== undefined) headers["if-match"] = String(opts.ifVersion);
    if (opts.message) headers["x-message"] = opts.message;
    return toWriteResult(await this.http.json("DELETE", this.u(`files/${encodePath(path)}`), { headers }));
  }

  // ------------------------------------------------------------ history

  /** Newest first. */
  async versions(path: string): Promise<VersionInfo[]> {
    return (await this.http.json<{ versions: VersionInfo[] }>("GET", this.u(`versions/${encodePath(path)}`))).versions;
  }

  /** Make an old version current again (as a new version). */
  async restore(path: string, version: number, opts: { message?: string } = {}): Promise<WriteResult> {
    return toWriteResult(await this.http.json("POST", this.u(`restore/${encodePath(path)}`), { json: { version, message: opts.message ?? "" } }));
  }

  async diff(path: string, from: number, to?: number): Promise<Record<string, unknown>> {
    return this.http.json("GET", this.u(`diff/${encodePath(path)}`), { query: { from, to } });
  }

  // ------------------------------------------------------------ sharing

  /** Share one file with another agent. Returns a share you can revoke. */
  async share(pathOrId: string, opts: { agentId: string; permission?: "read" | "write"; ttlSeconds?: number; expiresAt?: number }): Promise<Share> {
    const id = await this.idOf(pathOrId);
    return this.http.json("POST", `/v1/files/${encodeURIComponent(id)}/shares`, {
      json: { recipient_agent_id: opts.agentId, permission: opts.permission ?? "read", ttl_seconds: opts.ttlSeconds, expires_at: opts.expiresAt },
    });
  }

  async shares(pathOrId: string): Promise<Grant[]> {
    return this.http.json("GET", `/v1/files/${encodeURIComponent(await this.idOf(pathOrId))}/shares`);
  }

  async revokeShare(shareId: string): Promise<void> {
    await this.http.request("DELETE", `/v1/shares/${encodeURIComponent(shareId)}`);
  }

  /** File ids look like "fil_" + 20 hex chars; anything else is treated as a path. */
  private async idOf(pathOrId: string): Promise<string> {
    return /^fil_[0-9a-f]{20}$/.test(pathOrId) ? pathOrId : (await this.stat(pathOrId)).id;
  }
}

function searchParams(o: SearchOptions): Record<string, string | number | undefined> {
  return { q: o.query, ext: o.ext, created_by: o.createdBy, modified_after: o.modifiedAfter, modified_before: o.modifiedBefore };
}

async function verify(data: Uint8Array, expected: string | null | undefined) {
  if (!expected) return;
  const got = await sha256Hex(data);
  if (got !== expected.toLowerCase()) throw new ChecksumMismatch(500, "CHECKSUM_MISMATCH", `downloaded sha256 ${got} != server ${expected}`);
}

export class Workspace {
  readonly files: Files;
  constructor(private readonly http: Http, readonly id: string, public info: WorkspaceInfo | null = null) {
    this.files = new Files(http, this);
  }

  get name(): string | undefined {
    return this.info?.name;
  }

  private u(rest: string) {
    return `/v1/workspaces/${encodeURIComponent(this.id)}/${rest}`;
  }

  async refresh(): Promise<WorkspaceInfo> {
    this.info = await this.http.json("GET", `/v1/workspaces/${encodeURIComponent(this.id)}`);
    return this.info!;
  }

  /** Name + content search inside this workspace. */
  async search(q: string, opts: { path?: string; limit?: number } = {}): Promise<SearchHit[]> {
    return (await this.http.json<{ results: SearchHit[] }>("GET", "/v1/search", { query: { q, workspace: this.id, path: opts.path, limit: opts.limit } })).results;
  }

  /** Give an agent access to the whole workspace (""), a folder ("reports/") or a file. */
  async grant(agentId: string, opts: { path?: string; read?: boolean; write?: boolean; delete?: boolean; share?: boolean; approval?: "none" | "overwrite" | "all"; ttlSeconds?: number } = {}): Promise<Grant> {
    return this.http.json("POST", this.u("grants"), {
      json: {
        agent_id: agentId, path: opts.path ?? "", read: opts.read ?? true, write: opts.write ?? false, delete: opts.delete ?? false,
        share: opts.share ?? false, approval: opts.approval, ttl_seconds: opts.ttlSeconds,
      },
    });
  }

  async grants(): Promise<Grant[]> {
    return this.http.json("GET", this.u("grants"));
  }

  async revokeGrant(grantId: string): Promise<void> {
    await this.http.request("DELETE", this.u(`grants/${encodeURIComponent(grantId)}`));
  }

  /** Approval proposals in this workspace (agents see only their own). */
  async proposals(status: "pending" | "approved" | "rejected" | "all" = "pending"): Promise<Proposal[]> {
    return this.http.json("GET", this.u("proposals"), { query: { status } });
  }

  /** Poll until a pending write is approved or rejected. */
  async waitFor(pending: Pending | string, opts: { timeoutMs?: number; pollMs?: number } = {}): Promise<"approved" | "rejected"> {
    const id = typeof pending === "string" ? pending : pending.proposalId;
    const deadline = Date.now() + (opts.timeoutMs ?? 3_600_000);
    while (Date.now() < deadline) {
      const p = await this.http.json<Proposal>("GET", `/v1/approvals/${encodeURIComponent(id)}`);
      if (p.status !== "pending") return p.status as "approved" | "rejected";
      await sleep(opts.pollMs ?? 5000);
    }
    throw new AgentCloudError(408, "TIMEOUT", `proposal ${id} still pending`);
  }

  async activity(opts: { limit?: number; path?: string } = {}): Promise<Record<string, unknown>[]> {
    return this.http.json("GET", this.u("activity"), { query: { limit: opts.limit, path: opts.path } });
  }
}
