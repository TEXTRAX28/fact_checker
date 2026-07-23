import { readFileSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";

const root = resolve(import.meta.dirname, "..");
const rawUrl = process.argv[2];
if (!rawUrl) {
  fail("Usage: npm run configure:backend -- <https://backend.example>");
}

let backend;
try {
  backend = new URL(rawUrl);
} catch {
  fail("The backend URL is invalid.");
}

if (
  backend.username
  || backend.password
  || backend.search
  || backend.hash
  || !["", "/"].includes(backend.pathname)
) {
  fail("The backend URL must be an origin without credentials, path, query, or fragment.");
}

const local = backend.protocol === "http:"
  && ["localhost", "127.0.0.1"].includes(backend.hostname);
if (!local && backend.protocol !== "https:") {
  fail("A hosted backend URL must use HTTPS.");
}

const origin = backend.origin;
const configPath = join(root, "config.js");
const config = readFileSync(configPath, "utf8").replace(
  /export const API_BASE_URL = "[^"]+";/,
  `export const API_BASE_URL = "${origin}";`,
);
writeFileSync(configPath, config, "utf8");

const manifestPath = join(root, "manifest.json");
const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
manifest.host_permissions = local
  ? ["http://localhost/*", "http://127.0.0.1/*"]
  : [`${origin}/*`];
writeFileSync(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`, "utf8");

console.log(`Configured extension backend: ${origin}`);

function fail(message) {
  console.error(message);
  process.exit(1);
}
