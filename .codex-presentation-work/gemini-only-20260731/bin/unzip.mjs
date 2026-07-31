import { spawnSync } from "node:child_process";

const args = process.argv.slice(2);
let tarArgs;
if (args[0] === "-Z1" && args.length === 2) {
  tarArgs = ["-tf", args[1]];
} else if (args[0] === "-p" && args.length === 3) {
  tarArgs = ["-xOf", args[1], args[2]];
} else {
  process.stderr.write(`Unsupported unzip arguments: ${args.join(" ")}\n`);
  process.exit(2);
}

const result = spawnSync("tar", tarArgs, { encoding: null, maxBuffer: 100 * 1024 * 1024 });
if (result.stdout) process.stdout.write(result.stdout);
if (result.stderr) process.stderr.write(result.stderr);
process.exit(result.status ?? 1);
