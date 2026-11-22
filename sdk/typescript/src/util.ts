/** Anything the SDK accepts as file content. A plain string is UTF-8 text. */
export type BinaryLike = Uint8Array | ArrayBuffer | ArrayBufferView | Blob | string;

const isNode = typeof (globalThis as { process?: { versions?: { node?: string } } }).process?.versions?.node === "string";

let nodeCrypto: typeof import("node:crypto") | null | undefined;
async function loadNodeCrypto() {
  if (nodeCrypto === undefined) {
    try {
      nodeCrypto = isNode ? await import("node:crypto") : null;
    } catch {
      nodeCrypto = null;
    }
  }
  return nodeCrypto;
}

const hex = (buf: ArrayBuffer) => Array.from(new Uint8Array(buf), (b) => b.toString(16).padStart(2, "0")).join("");

/** Lowercase hex SHA-256. Uses node:crypto when available, else WebCrypto. */
export async function sha256Hex(data: Uint8Array): Promise<string> {
  const nc = await loadNodeCrypto();
  if (nc) return nc.createHash("sha256").update(data).digest("hex");
  if (!globalThis.crypto?.subtle) throw new Error("no SHA-256 implementation available (need node:crypto or crypto.subtle)");
  return hex(await globalThis.crypto.subtle.digest("SHA-256", data));
}

export function randomId(): string {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  return Array.from({ length: 4 }, () => Math.random().toString(16).slice(2, 10)).join("-");
}

export async function toBytes(data: BinaryLike): Promise<Uint8Array> {
  if (typeof data === "string") return new TextEncoder().encode(data);
  if (data instanceof Uint8Array) return data;
  if (data instanceof ArrayBuffer) return new Uint8Array(data);
  if (ArrayBuffer.isView(data)) return new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
  if (typeof Blob !== "undefined" && data instanceof Blob) return new Uint8Array(await data.arrayBuffer());
  throw new TypeError("expected Uint8Array, ArrayBuffer, Blob or string");
}

export async function readLocalFile(path: string): Promise<Uint8Array> {
  if (!isNode) throw new TypeError("reading a local file path needs Node.js; pass bytes, a Blob or a string instead");
  const fs = await import("node:fs/promises");
  return new Uint8Array(await fs.readFile(path));
}

/** Write atomically: temp file in the same directory, then rename. */
export async function writeLocalFile(path: string, data: Uint8Array): Promise<void> {
  if (!isNode) throw new TypeError("writing to a local path needs Node.js");
  const fs = await import("node:fs/promises");
  const p = await import("node:path");
  await fs.mkdir(p.dirname(path), { recursive: true });
  const tmp = `${path}.agentcloud-${randomId()}.part`;
  try {
    await fs.writeFile(tmp, data);
    await fs.rename(tmp, path);
  } catch (e) {
    await fs.unlink(tmp).catch(() => {});
    throw e;
  }
}

export function basename(path: string): string {
  const parts = path.split(/[\\/]/).filter(Boolean);
  return parts[parts.length - 1] ?? path;
}

/**
 * Encode a workspace path for a URL. fetch() resolves '.' and '..' segments before
 * sending, which would silently retarget the request, so refuse them.
 */
export function encodePath(path: string): string {
  const clean = path.replace(/^\/+/, "");
  const segs = clean.split("/");
  if (segs.some((s) => s === "." || s === "..")) throw new TypeError(`'.' and '..' are not allowed in workspace paths: ${path}`);
  return segs.map(encodeURIComponent).join("/");
}

const TYPES: Record<string, string> = {
  csv: "text/csv", tsv: "text/tab-separated-values", txt: "text/plain", md: "text/markdown", json: "application/json",
  jsonl: "application/jsonl", html: "text/html", xml: "application/xml", yaml: "application/yaml", yml: "application/yaml",
  pdf: "application/pdf", png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", gif: "image/gif", webp: "image/webp",
  svg: "image/svg+xml", zip: "application/zip", gz: "application/gzip", py: "text/x-python", js: "text/javascript",
  ts: "text/plain", parquet: "application/vnd.apache.parquet",
};

export function guessContentType(path: string): string | undefined {
  const name = basename(path);
  const dot = name.lastIndexOf(".");
  if (dot < 0) return undefined;
  return TYPES[name.slice(dot + 1).toLowerCase()];
}

export const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));
