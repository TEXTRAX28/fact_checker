import fs from "node:fs/promises";
import { FileBlob, PresentationFile } from "@oai/artifact-tool";

const workspace = "C:/Users/natan/VSC Code/fact-checker/.codex-presentation-work/gemini-only-20260731";
const source = `${workspace}/template-starter.pptx`;
const output = "C:/Users/natan/VSC Code/fact-checker/Presentation-gemini-only.pptx";
const renderDir = `${workspace}/final-render`;
const layoutDir = `${workspace}/final-layout`;

const presentation = await PresentationFile.importPptx(await FileBlob.load(source));
const slides = presentation.slides.items;
if (slides.length !== 10) throw new Error(`Expected 10 slides, found ${slides.length}`);

function findNamed(slide, name) {
  const target = slide.elements.items.find((element) => element.name === name);
  if (!target) throw new Error(`Missing inherited element "${name}"`);
  return target;
}

function rewrite(slide, name, value) {
  const target = findNamed(slide, name);
  const before = String(target.text);
  target.text.replace(before, value);
}

function setNotes(slide, body, sources) {
  slide.speakerNotes.textFrame.setText(
    [body, "", "[Sources]", ...sources.map((sourceItem) => `- ${sourceItem}`)].join("\n"),
  );
  slide.speakerNotes.setVisible(true);
}

const internal = "Internal product implementation and test suite, verified 2026-07-31.";
const grounding = "https://ai.google.dev/gemini-api/docs/google-search";
const pricing = "https://ai.google.dev/gemini-api/docs/pricing";

rewrite(slides[0], "opening-tagline", "One Gemini key. Grounded evidence.");
setNotes(
  slides[0],
  "Fact Check now completes extraction, grounded web evidence retrieval, source verification, and verdict generation with one user-supplied Google AI Studio key. The migration removes the second provider credential without removing the evidence-first workflow.",
  [internal, grounding],
);

findNamed(slides[1], "evidence-statement").text.replace(
  "A confident answer is not",
  "Gemini finds the evidence.",
);
findNamed(slides[1], "evidence-statement").text.replace(
  "the same as verified evidence.",
  "Python derives the verdict.",
);
rewrite(slides[1], "evidence-subtitle", "One API key; separate stages; deterministic outcomes.");
setNotes(
  slides[1],
  "Gemini 3.5 Flash-Lite performs claim extraction and source-level verification. Google Search grounding supplies cited web evidence. The application does not trust a model-generated final label: validated source stances are aggregated and Python derives TRUE, FALSE, or UNVERIFIABLE.",
  [internal, grounding],
);

rewrite(slides[2], "input-title", "One key powers every stage.");
findNamed(slides[2], "input-title").text.fontSize = 40;
rewrite(slides[2], "current-page", "EXTRACT CLAIMS");
rewrite(slides[2], "paste-url", "GROUND WITH SEARCH");
rewrite(slides[2], "paste-text", "VERIFY SOURCES");
rewrite(slides[2], "input-subtitle", "Your Google AI Studio key stays in the trusted session.");
setNotes(
  slides[2],
  "Users provide one Gemini API key. That key authorizes extraction, Google Search grounding, and verification. It is stored only in the trusted Chrome session context and is isolated to the job or retry that received it.",
  [internal, grounding],
);

rewrite(slides[3], "pipeline-title", "Four stages keep evidence first.");
rewrite(slides[3], "pipeline", "EXTRACT    →    SEARCH    →    VERIFY    →    DERIVE");
findNamed(slides[3], "pipeline").text.fontSize = 28;
rewrite(slides[3], "pipeline-subtitle", "Gemini extracts and verifies; Python applies verdict rules.");
setNotes(
  slides[3],
  "The pipeline separates four responsibilities: extract checkable claims, retrieve grounded web evidence, verify each source stance, and derive the final verdict deterministically. Each claim moves independently through bounded search and verification work.",
  [internal, grounding],
);

rewrite(slides[4], "input-title", "Grounding is evidence—not a shortcut.");
findNamed(slides[4], "input-title").text.fontSize = 40;
rewrite(slides[4], "current-page", "CITED SEGMENTS");
rewrite(slides[4], "paste-url", "REAL HOSTNAMES");
rewrite(slides[4], "paste-text", "≤3 SOURCES");
rewrite(slides[4], "input-subtitle", "Unbound citations and low-quality domains are excluded.");
setNotes(
  slides[4],
  "Only grounded support segments that map to citation chunks are admitted as evidence. They are labeled as Gemini-synthesized grounded summaries, not publisher quotations. Source security and ranking use the parsed destination hostname, and malformed or unbound citations are skipped.",
  [internal, grounding],
);

rewrite(slides[5], "verdict-title", "Python derives every final outcome.");
setNotes(
  slides[5],
  "TRUE requires supported evidence without contradiction. FALSE requires contradicted evidence without support. Mixed, insufficient, missing, or malformed source analysis becomes UNVERIFIABLE or a protocol failure that can be retried. Top-level model booleans never override validated source analysis.",
  [internal],
);

rewrite(slides[6], "input-title", "BYOK remains isolated and bounded.");
findNamed(slides[6], "input-title").text.fontSize = 40;
rewrite(slides[6], "current-page", "SESSION KEY");
rewrite(slides[6], "paste-url", "JOB TOKEN");
rewrite(slides[6], "paste-text", "DEADLINES");
findNamed(slides[6], "current-page").text.fontSize = 22;
findNamed(slides[6], "paste-url").text.fontSize = 22;
findNamed(slides[6], "paste-text").text.fontSize = 22;
rewrite(slides[6], "input-subtitle", "Keys never enter snapshots, events, exports, or logs.");
setNotes(
  slides[6],
  "The Gemini key remains in trusted session storage and is never serialized into public job state. Random bearer capabilities protect follow-up job operations. Request sizes, provider concurrency, retries, evidence length, and per-call and whole-job deadlines are bounded.",
  [internal],
);

rewrite(slides[7], "input-title", "Estimated list-price equivalent (before free quota)");
findNamed(slides[7], "input-title").text.fontSize = 40;
rewrite(slides[7], "current-page", "INPUT TOKENS\n$0.30 / 1M");
rewrite(slides[7], "paste-url", "OUTPUT + THINKING\n$2.50 / 1M");
rewrite(slides[7], "paste-text", "SEARCH QUERIES\n$0.014 EACH");
findNamed(slides[7], "current-page").text.fontSize = 20;
findNamed(slides[7], "paste-url").text.fontSize = 20;
findNamed(slides[7], "paste-text").text.fontSize = 20;
rewrite(slides[7], "input-subtitle", "ESTIMATED TOTAL • JULY 2026 • ACTUAL MAY BE $0");
setNotes(
  slides[7],
  "The usage view reports input tokens, output tokens including thinking, tool-use prompt tokens, and grounded Google Search query count. For Gemini 3.5 Flash-Lite, the July 2026 list-price basis is $0.30 per million input tokens, $2.50 per million billable output tokens including thinking, and $0.014 per Google Search query after the free allowance. This is an Estimated list-price equivalent (before free quota), not a billing statement. Free quota and the user's billing arrangement can make the actual charge different or $0.",
  [pricing],
);

rewrite(slides[8], "input-title", "Implementation is verified end to end.");
findNamed(slides[8], "input-title").text.fontSize = 40;
rewrite(slides[8], "current-page", "GEMINI ONLY");
rewrite(slides[8], "paste-url", "109 PYTHON TESTS");
rewrite(slides[8], "paste-text", "39 EXTENSION TESTS");
findNamed(slides[8], "paste-url").text.fontSize = 22;
findNamed(slides[8], "paste-text").text.fontSize = 22;
rewrite(slides[8], "input-subtitle", "Gemini 3.5 Flash-Lite • Google Search grounding • Railway");
setNotes(
  slides[8],
  "The current implementation uses the official Google Gen AI SDK, Gemini 3.5 Flash-Lite, and Google Search grounding. The final verification run passed 109 Python tests and 39 extension tests, plus extension validation, compilation, and whitespace checks. Railway remains the hosted backend target.",
  [internal],
);

rewrite(slides[9], "closing-line", "One key. Evidence first. Verify.");
setNotes(
  slides[9],
  "The migration simplifies setup without collapsing the verification architecture: one key authorizes the model and grounded search, while explicit stages, evidence validation, deterministic verdict logic, security controls, and transparent estimates remain in place.",
  [internal, grounding, pricing],
);

await fs.mkdir(renderDir, { recursive: true });
await fs.mkdir(layoutDir, { recursive: true });

for (const [index, slide] of slides.entries()) {
  const number = String(index + 1).padStart(2, "0");
  const png = await presentation.export({ slide, format: "png", scale: 2 });
  await fs.writeFile(`${renderDir}/slide-${number}.png`, new Uint8Array(await png.arrayBuffer()));
  const layout = await slide.export({ format: "layout" });
  await fs.writeFile(`${layoutDir}/slide-${number}.layout.json`, await layout.text());
}

const montage = await presentation.export({ format: "webp", montage: true, scale: 1 });
await fs.writeFile(`${workspace}/final-montage.webp`, new Uint8Array(await montage.arrayBuffer()));

const inspection = await presentation.inspect({
  kind: "slide,textbox,shape,image,notes,layout",
  include: "id,slide,name,title,text,textPreview,bbox,bboxUnit,isPlaceholder,placeholders",
  maxChars: 100000,
});
await fs.writeFile(`${workspace}/final-inspect.ndjson`, inspection.ndjson || "", "utf8");

const pptx = await PresentationFile.exportPptx(presentation);
await pptx.save(output);

console.log(JSON.stringify({
  output,
  slides: slides.length,
  masters: presentation.masters.items.length,
  layouts: presentation.layouts.items.length,
}, null, 2));
