import { spawnSync } from "node:child_process";
const r = spawnSync("unzip", ["-Z1", "C:\\Users\\natan\\VSC Code\\fact-checker\\Presentation-finished.pptx"], { encoding: "utf8" });
console.log({ status: r.status, error: String(r.error || ""), out: r.stdout?.slice(0, 100), err: r.stderr });
