import fs from "node:fs/promises";
import { spawnSync } from "node:child_process";

const workspace = "C:/Users/natan/VSC Code/fact-checker/.codex-presentation-work/gemini-only-20260731";
const pptx = "C:/Users/natan/VSC Code/fact-checker/Presentation-gemini-only.pptx";
const failures = [];

for (let slideNumber = 1; slideNumber <= 10; slideNumber += 1) {
  const layout = JSON.parse(await fs.readFile(
    `${workspace}/final-layout/slide-${String(slideNumber).padStart(2, "0")}.layout.json`,
    "utf8",
  ));
  const { width, height } = layout.slide.frame;
  for (const element of layout.elements || []) {
    const [left, top, elementWidth, elementHeight] = element.bbox || [];
    if (![left, top, elementWidth, elementHeight].every(Number.isFinite)) continue;
    if (left < 0 || top < 0 || left + elementWidth > width || top + elementHeight > height) {
      failures.push(`slide ${slideNumber}: ${element.name || element.kind} exceeds canvas`);
    }
  }
}

const names = spawnSync("tar", ["-tf", pptx], { encoding: "utf8" });
if (names.status !== 0) failures.push("could not enumerate PPTX package");
const xmlNames = String(names.stdout || "")
  .split(/\r?\n/)
  .filter((name) => /^ppt\/(?:slides|notesSlides)\/.*\.xml$/.test(name));
let sourcesBlocks = 0;
for (const name of xmlNames) {
  const extracted = spawnSync("tar", ["-xOf", pptx, name], {
    encoding: "utf8",
    maxBuffer: 20 * 1024 * 1024,
  });
  const xml = String(extracted.stdout || "");
  if (/tavily|x-tavily-key|two api keys|two keys/i.test(xml)) {
    failures.push(`${name}: stale two-provider copy`);
  }
  if (name.startsWith("ppt/slides/") && /Click to (?:add|edit)|Slide Number|Footer/i.test(xml)) {
    failures.push(`${name}: unresolved placeholder prompt`);
  }
  if (name.startsWith("ppt/slides/")) {
    for (const shape of xml.match(/<p:sp\b[\s\S]*?<\/p:sp>/g) || []) {
      if (!/<p:ph\b/.test(shape)) continue;
      const text = [...shape.matchAll(/<a:t>([\s\S]*?)<\/a:t>/g)]
        .map((match) => match[1].replace(/<[^>]+>/g, "").trim())
        .join("");
      if (!text) failures.push(`${name}: empty local structural placeholder`);
    }
  }
  if (name.startsWith("ppt/notesSlides/") && xml.includes("[Sources]")) {
    sourcesBlocks += 1;
  }
}
if (sourcesBlocks !== 10) failures.push(`expected 10 [Sources] note blocks, found ${sourcesBlocks}`);

const report = {
  status: failures.length ? "fail" : "pass",
  checkedSlides: 10,
  sourcesBlocks,
  failures,
};
await fs.writeFile(`${workspace}/qa-gates.json`, `${JSON.stringify(report, null, 2)}\n`);
console.log(JSON.stringify(report, null, 2));
if (failures.length) process.exit(1);
