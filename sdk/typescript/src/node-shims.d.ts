// Minimal ambient types for the few Node built-ins the SDK loads lazily, so the
// package builds without @types/node. They are only imported when running on Node.
declare module "node:crypto" {
  interface Hash {
    update(data: Uint8Array): Hash;
    digest(encoding: "hex"): string;
  }
  export function createHash(algorithm: string): Hash;
  export function randomUUID(): string;
}

declare module "node:fs/promises" {
  export function readFile(path: string): Promise<Uint8Array>;
  export function writeFile(path: string, data: Uint8Array): Promise<void>;
  export function mkdir(path: string, options?: { recursive?: boolean }): Promise<string | undefined>;
  export function rename(from: string, to: string): Promise<void>;
  export function unlink(path: string): Promise<void>;
}

declare module "node:path" {
  export function dirname(p: string): string;
  export function basename(p: string): string;
}
