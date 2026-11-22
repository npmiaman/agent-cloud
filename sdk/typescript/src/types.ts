// Shapes returned by the Agent Cloud API. Fields are as the server sends them
// (snake_case); SDK-level results that the SDK constructs are camelCase.

export interface WorkspaceInfo {
  id: string;
  name: string;
  description?: string;
  project_id?: string;
  storage_limit_bytes?: number;
  storage_used_bytes?: number;
  quota_bytes?: number;
  used_bytes?: number;
  retention_days?: number | null;
  created_at?: number;
  status?: string;
  [k: string]: unknown;
}

export interface FileEntry {
  id?: string;
  path: string;
  kind: "file" | "folder";
  version?: number;
  size?: number;
  content_type?: string | null;
  sha256?: string | null;
  updated_at?: number;
  deleted?: boolean;
  [k: string]: unknown;
}

export interface FileMeta extends FileEntry {
  id: string;
  workspace_id: string;
  name: string;
  deleted: boolean;
  deleted_at: number | null;
}

export interface VersionInfo {
  id: string;
  file_id: string;
  n: number;
  size: number;
  blob_sha256: string | null;
  content_type: string | null;
  op: string;
  actor: string;
  message?: string;
  created_at: number;
  [k: string]: unknown;
}

/** A write that was committed. */
export interface Written {
  pending: false;
  id: string;
  path: string;
  version: number;
  /** "create" | "update" | "unchanged" | "restore" | "delete" | ... */
  op: string;
  size?: number;
  sha256?: string;
  contentType?: string;
  raw: Record<string, unknown>;
}

/** A write that is waiting for a human to approve it. Not an error. */
export interface Pending {
  pending: true;
  proposalId: string;
  path: string;
  baseVersion: number | null;
  raw: Record<string, unknown>;
}

export type WriteResult = Written | Pending;

export interface Proposal {
  id: string;
  workspace_id: string;
  path: string;
  status: "pending" | "approved" | "rejected" | string;
  [k: string]: unknown;
}

export interface SearchHit {
  workspace: string;
  workspace_id: string;
  path: string;
  version: number;
  size: number;
  updated_at: number;
  score: number;
  matched_in: string;
  snippet: string | null;
}

export interface Share {
  share_id: string;
  file_id: string;
  recipient_agent_id: string;
  permission: "read" | "write";
  expires_at: number | null;
  status: string;
}

export interface Grant {
  id: string;
  workspace_id: string;
  agent_id: string;
  path: string;
  read: boolean;
  write: boolean;
  delete: boolean;
  [k: string]: unknown;
}

export interface Credential {
  id: string;
  agent_id: string;
  /** The secret (aca_…). Only returned once, at creation/rotation. */
  token: string;
  prefix: string;
  scopes: string[];
  expires_at: number | null;
  replaces?: string;
  old_expires_at?: number;
  [k: string]: unknown;
}

export interface Agent {
  id: string;
  name: string;
  description: string;
  status: string;
  disabled: boolean;
  tokens: unknown[];
  grants: Grant[];
  [k: string]: unknown;
}

export interface Provisioned {
  agent: Agent;
  workspace: WorkspaceInfo;
  credential: Credential;
  /** Same as credential (server alias). */
  token: Credential;
  connect: Record<string, unknown>;
}

export interface Environment {
  identity: { type: string; id: string; name: string };
  home_workspace: string | null;
  workspaces: { id: string; name: string; role: string; storage_used_bytes: number; storage_limit_bytes: number; capabilities: unknown }[];
  [k: string]: unknown;
}

export interface UploadSession {
  upload_id: string;
  workspace_id: string;
  path: string;
  state: "CREATED" | "UPLOADED" | "COMMITTED" | "FAILED" | "EXPIRED" | "ABORTED";
  size_bytes: number;
  content_type: string | null;
  expires_at: number;
  received_bytes: number | null;
  checksum_sha256: string | null;
  upload_url?: string;
  upload_method?: string;
  result?: Record<string, unknown>;
  error?: { code: string; message: string };
}

export interface DownloadInfo {
  file_id: string;
  version: number;
  size: number;
  content_type: string | null;
  checksum_sha256: string;
  expires_at: number;
  url: string;
}
