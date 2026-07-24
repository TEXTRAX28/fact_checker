import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, resolve } from "node:path";

const root = resolve(import.meta.dirname, "..");
const manifest = JSON.parse(readFileSync(join(root, "manifest.json"), "utf8"));
const config = readFileSync(join(root, "config.js"), "utf8");
const apiMatch = config.match(/API_BASE_URL\s*=\s*"([^"]+)"/);
assert.ok(apiMatch, "config.js must define API_BASE_URL as a string literal");
const apiUrl = new URL(apiMatch[1]);
assert.equal(apiUrl.pathname, "/", "API_BASE_URL must not contain a path");
const localBackend = apiUrl.protocol === "http:"
  && ["localhost", "127.0.0.1"].includes(apiUrl.hostname);
assert.ok(localBackend || apiUrl.protocol === "https:", "Hosted API_BASE_URL must use HTTPS");
const expectedHostPermissions = localBackend
  ? ["http://localhost/*", "http://127.0.0.1/*"]
  : [`${apiUrl.origin}/*`];

assert.equal(manifest.manifest_version, 3);
assert.equal(manifest.minimum_chrome_version, "116");
assert.deepEqual(manifest.permissions, ["activeTab", "scripting", "sidePanel", "storage"]);
assert.deepEqual(manifest.host_permissions, expectedHostPermissions);
assert.deepEqual(manifest.optional_host_permissions, ["http://*/*", "https://*/*"]);
assert.equal(manifest.side_panel.default_path, "sidepanel.html");
assert.equal(manifest.background.service_worker, "service-worker.js");

const requiredFiles = [
  "sidepanel.html",
  "sidepanel.css",
  "sidepanel.js",
  "page-find.js",
  "page-capture.js",
  "vendor/readability/Readability.js",
  "vendor/readability/LICENSE",
  "vendor/lucide/LICENSE",
  "icons/icon16.png",
  "icons/icon48.png",
  "icons/icon128.png",
];
for (const file of requiredFiles) assert.ok(statSync(join(root, file)).isFile(), `${file} is missing`);

const html = readFileSync(join(root, "sidepanel.html"), "utf8");
const sidepanel = readFileSync(join(root, "sidepanel.js"), "utf8");
assert.doesNotMatch(html, /<script[^>]+src=["']https?:/i, "Remote scripts are forbidden");
assert.doesNotMatch(html, /microphone|speech-to-text|live-stream|audio|video/i, "Forbidden feature copy found");
assert.equal((html.match(/role="tab"/g) || []).length, 3, "All three modes must be tabs");
assert.equal(
  (html.match(/aria-controls="input-panel"/g) || []).length,
  3,
  "Every mode tab must identify its panel",
);
assert.match(
  html,
  /id="input-panel"[^>]+role="tabpanel"/,
  "The source input must expose tabpanel semantics",
);
assert.match(
  html,
  /AI can make mistakes\. Verify important claims against the cited sources\./,
  "The results view must include an accuracy notice",
);
assert.match(
  html,
  /id="privacy-link"[^>]+target="_blank"[^>]+rel="noreferrer"/,
  "The side panel must link to the public privacy policy",
);
assert.match(
  sidepanel,
  /privacyLink\.href\s*=\s*apiUrl\("\/privacy\?v=20260724-1"\)/,
  "The privacy link must use the configured backend origin",
);
assert.match(
  sidepanel,
  /chrome\.storage\.session\.setAccessLevel\(\{\s*accessLevel:\s*"TRUSTED_CONTEXTS"\s*\}\)/,
  "Session storage must be restricted to trusted extension contexts",
);
assert.doesNotMatch(
  sidepanel,
  /chrome\.storage\.(?:local|sync)\.(?:get|set)\([^)]*STORAGE\.credentials/s,
  "Provider credentials must not use persistent Chrome storage",
);

const scripts = walk(root).filter((file) => /\.(?:js|mjs)$/.test(file));
for (const script of scripts) execFileSync(process.execPath, ["--check", script], { stdio: "pipe" });

const runtimeScripts = scripts.filter((file) => !file.includes(`${join(root, "tests")}\\`) && !file.includes(`${join(root, "scripts")}\\`));
const apiLiteralFiles = runtimeScripts.filter((file) => (
  readFileSync(file, "utf8").includes(apiUrl.origin)
));
assert.deepEqual(apiLiteralFiles, [join(root, "config.js")], "API base URL must exist only in config.js");

for (const size of [16, 48, 128]) {
  const png = readFileSync(join(root, `icons/icon${size}.png`));
  assert.equal(png.toString("hex", 0, 8), "89504e470d0a1a0a", `icon${size}.png is not a PNG`);
  assert.equal(png.readUInt32BE(16), size, `icon${size}.png has the wrong width`);
  assert.equal(png.readUInt32BE(20), size, `icon${size}.png has the wrong height`);
}

console.log(`Validated manifest, ${scripts.length} scripts, assets, ${apiUrl.origin}, and PNG dimensions.`);

function walk(directory) {
  return readdirSync(directory).flatMap((name) => {
    const path = join(directory, name);
    return statSync(path).isDirectory() ? walk(path) : [path];
  });
}
