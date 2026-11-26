// The page build: bundle src/main.ts in memory and inline it into index.html as out/dashboard.html.
//
// Run as `bun run build.ts` from the staged scratch folder, at development time only. Every file it
// reads is listed in the manifest (manifest.txt); it is taken by its fixed name, never from a
// directory listing. Nothing time-, machine- or path-dependent is written.

import { mkdirSync, readFileSync, writeFileSync } from "node:fs";

export const TEMPLATE = "index.html";
export const ENTRY = "src/main.ts";
export const MARKER = "<!--bundle-->";
export const OUT_PAGE = "out/dashboard.html";

// No outdir: the bundle stays in memory. No source map: no scratch path reaches the page.
export const OPTIONS = {
  entrypoints: [ENTRY],
  target: "browser",
  format: "esm",
  minify: true,
  sourcemap: "none",
  splitting: false,
} as const;

function fail(message: string): never {
  process.stderr.write(message.replace(/\s*[\r\n]+\s*/g, " ").trim() + "\n");
  process.exit(1);
}

function firstLog(logs: readonly unknown[]): string {
  const first = logs[0];
  if (first instanceof Error) return first.message;
  if (first && typeof first === "object" && "message" in first) return String(first.message);
  return String(first ?? "unknown error");
}

let result: Awaited<ReturnType<typeof Bun.build>>;
try {
  result = await Bun.build(OPTIONS);
} catch (error) {
  const logs = error instanceof AggregateError ? error.errors : [error];
  fail(`bundle failed: ${firstLog(logs)}`);
}
if (!result.success) fail(`bundle failed: ${firstLog(result.logs)}`);
if (result.outputs.length !== 1) fail(`bundle produced ${result.outputs.length} outputs, expected one`);

const bundle = await result.outputs[0].text();
if (bundle.toLowerCase().includes("</script")) fail("bundle contains </script");

const template = readFileSync(TEMPLATE, "utf-8");
const found = template.split(MARKER).length - 1;
if (found !== 1) fail(`template must hold the marker once, found ${found}`);

const at = template.indexOf(MARKER);
const page =
  template.slice(0, at) + '<script type="module">' + bundle + "</script>" + template.slice(at + MARKER.length);

mkdirSync("out", { recursive: true });
writeFileSync(OUT_PAGE, page, "utf-8");
