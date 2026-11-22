/** Every non-2xx API response becomes an AgentCloudError (or a subclass). */
export class AgentCloudError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string | undefined;
  /** The full `error` object from the response, including any extra fields. */
  readonly details: Record<string, unknown>;

  constructor(status: number, code: string, message: string, requestId?: string, details: Record<string, unknown> = {}) {
    super(`${status} ${code}: ${message}${requestId ? ` (request ${requestId})` : ""}`);
    this.name = new.target.name;
    this.status = status;
    this.code = code;
    this.requestId = requestId;
    this.details = details;
  }
}

export class BadRequest extends AgentCloudError {}
export class Unauthenticated extends AgentCloudError {}
export class Forbidden extends AgentCloudError {}
export class NotFound extends AgentCloudError {}
export class Conflict extends AgentCloudError {}
export class RateLimited extends AgentCloudError {
  /** Seconds the server asked us to wait (Retry-After), if given. */
  retryAfter: number | undefined;
}
export class QuotaExceeded extends AgentCloudError {}

/** Thrown when bytes on the wire don't match the SHA-256 computed locally. */
export class ChecksumMismatch extends AgentCloudError {}

export function errorFromResponse(status: number, body: unknown, text: string, headers: Headers): AgentCloudError {
  const err = (body && typeof body === "object" && (body as { error?: unknown }).error) as Record<string, unknown> | undefined;
  const code = (err && typeof err.code === "string" ? err.code : "HTTP_ERROR") as string;
  const message = (err && typeof err.message === "string" ? err.message : text.slice(0, 500) || `HTTP ${status}`) as string;
  const requestId = (err && typeof err.request_id === "string" ? err.request_id : headers.get("x-request-id")) ?? undefined;
  const details = err ?? {};
  let cls: typeof AgentCloudError = AgentCloudError;
  if (code === "QUOTA_EXCEEDED" || status === 507) cls = QuotaExceeded;
  else if (status === 400 || status === 422) cls = BadRequest;
  else if (status === 401) cls = Unauthenticated;
  else if (status === 403) cls = Forbidden;
  else if (status === 404) cls = NotFound;
  else if (status === 409) cls = Conflict;
  else if (status === 429) cls = RateLimited;
  const e = new cls(status, code, message, requestId, details);
  if (e instanceof RateLimited) {
    const ra = Number(headers.get("retry-after"));
    e.retryAfter = Number.isFinite(ra) && ra > 0 ? ra : undefined;
  }
  return e;
}
