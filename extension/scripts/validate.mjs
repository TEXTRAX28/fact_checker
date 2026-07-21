import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, resolve } from "node:path";

const root = resolve(import.meta.dirname, "..");
const manifest = JSON.parse(readFileSync(join(root, "manifest.json"), "utf8"));

assert.equal(manifest.manifest_version, 3);
assert.equal(manifest.minimum_chrome_version, "116");
assert.deepEqual(manifest.permissions, ["activeTab", "scripting", "sidePanel", "storage"]);
assert.deepEqual(manifest.host_permissions, ["http://localhost/*", "http://127.0.0.1/*"]);
assert.equal(manifest.side_panel.default_path, "sidepanel.html");
assert.equal(manifest.background.service_worker, "service-worker.js");

const requiredFiles = [
  "sidepanel.html",
  "sidepanel.css",
  "sidepanel.js",
  "vendor/readability/Readability.js",
  "vendor/readability/LICENSE",
  "vendor/lucide/LICENSE",
  "icons/icon16.png",
  "icons/icon48.png",
  "icons/icon128.png",
];
for (const file of requiredFiles) assert.ok(statSync(join(root, file)).isFile(), `${file} is missing`);

const html = readFileSync(join(root, "sidepanel.html"), "utf8");
assert.doesNotMatch(html, /<script[^>]+src=["']https?:/i, "Remote scripts are forbidden");
assert.doesNotMatch(html, /microphone|speech-to-text|live-stream|audio|video/i, "Forbidden feature copy found");

const scripts = walk(root).filter((file) => /\.(?:js|mjs)$/.test(file));
for (const script of scripts) execFileSync(process.execPath, ["--check", script], { stdio: "pipe" });

const runtimeScripts = scripts.filter((file) => !file.includes(`${join(root, "tests")}\\`) && !file.includes(`${join(root, "scripts")}\\`));
const apiLiteralFiles = runtimeScripts.filter((file) => readFileSync(file, "utf8").includes("127.0.0.1:8000"));
assert.deepEqual(apiLiteralFiles, [join(root, "config.js")], "API base URL must exist only in config.js");

for (const size of [16, 48, 128]) {
  const png = readFileSync(join(root, `icons/icon${size}.png`));
  assert.equal(png.toString("hex", 0, 8), "89504e470d0a1a0a", `icon${size}.png is not a PNG`);
  assert.equal(png.readUInt32BE(16), size, `icon${size}.png has the wrong width`);
  assert.equal(png.readUInt32BE(20), size, `icon${size}.png has the wrong height`);
}

console.log(`Validated manifest, ${scripts.length} scripts, local assets, API config, and PNG dimensions.`);

function walk(directory) {
  return readdirSync(directory).flatMap((name) => {
    const path = join(directory, name);
    return statSync(path).isDirectory() ? walk(path) : [path];
  });
}
