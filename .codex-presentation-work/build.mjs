import fs from "node:fs/promises";
import { PresentationFile, FileBlob } from "@oai/artifact-tool";

const input = "C:/Users/natan/VSC Code/fact-checker/Presentation.pptx";
const output = "C:/Users/natan/VSC Code/fact-checker/Presentation-technical.pptx";
const previewDir =
  "C:/Users/natan/VSC Code/fact-checker/.codex-presentation-work/technical-preview";

const COLORS = {
  background: "#F4F7FB",
  navy: "#172033",
  muted: "#66748A",
  blue: "#1769D2",
  green: "#128A55",
  red: "#CE3B3B",
  amber: "#A86E00",
};

const presentation = await PresentationFile.importPptx(
  await FileBlob.load(input),
);

const sourceSlides = [...presentation.slides.items];
for (let copy = 0; copy < 2; copy += 1) {
  for (const sourceSlide of sourceSlides) {
    sourceSlide.duplicate();
  }
}

if (presentation.slides.items.length !== 15) {
  throw new Error(`Expected 15 slides, found ${presentation.slides.items.length}.`);
}

const logoBytes = await fs.readFile(
  "C:/Users/natan/VSC Code/fact-checker/extension/icons/icon128.png",
);
const heroBytes = await fs.readFile(
  "C:/Users/natan/VSC Code/fact-checker/screenshots/01-check-any-page.png",
);
const evidenceBytes = await fs.readFile(
  "C:/Users/natan/VSC Code/fact-checker/screenshots/04-verdicts-and-sources.png",
);

function addText(
  slide,
  text,
  position,
  {
    fontSize = 36,
    bold = false,
    color = COLORS.navy,
    alignment = "left",
    verticalAlignment = "middle",
    name = "text",
    fontFamily = "Aptos",
  } = {},
) {
  const shape = slide.shapes.add({
    geometry: "textbox",
    name,
    position,
    fill: "none",
    line: { style: "solid", fill: "none", width: 0 },
  });
  shape.text = text;
  shape.text.style = {
    fontSize,
    bold,
    color,
    alignment,
    verticalAlignment,
    fontFamily,
  };
  shape.text.verticalAlignment = verticalAlignment;
  return shape;
}

function addLogo(slide, { centered = false } = {}) {
  if (centered) {
    slide.images.add({
      blob: logoBytes.buffer.slice(
        logoBytes.byteOffset,
        logoBytes.byteOffset + logoBytes.byteLength,
      ),
      contentType: "image/png",
      alt: "Fact Check shield logo",
      fit: "contain",
      position: { left: 568, top: 218, width: 144, height: 144 },
    });
    addText(
      slide,
      "FACT CHECK",
      { left: 380, top: 380, width: 520, height: 58 },
      {
        fontSize: 38,
        bold: true,
        color: COLORS.blue,
        alignment: "center",
        name: "centered-wordmark",
      },
    );
    return;
  }

  slide.images.add({
    blob: logoBytes.buffer.slice(
      logoBytes.byteOffset,
      logoBytes.byteOffset + logoBytes.byteLength,
    ),
    contentType: "image/png",
    alt: "Fact Check shield logo",
    fit: "contain",
    position: { left: 80, top: 46, width: 34, height: 34 },
  });
  addText(
    slide,
    "FACT CHECK",
    { left: 124, top: 45, width: 180, height: 36 },
    {
      fontSize: 18,
      bold: true,
      color: COLORS.blue,
      name: "header-wordmark",
    },
  );
}

function resetSlide(slide) {
  for (const element of [...slide.elements.items]) {
    element.delete();
  }
  slide.background.fill = COLORS.background;
}

function setNotes(slide, text, sources = []) {
  const blocks = [text];
  if (sources.length > 0) {
    blocks.push("", "[Sources]", ...sources.map((source) => `- ${source}`));
  }
  slide.speakerNotes.textFrame.setText(blocks.join("\n"));
  slide.speakerNotes.setVisible(true);
}

for (const slide of presentation.slides.items) {
  resetSlide(slide);
}

const slides = presentation.slides.items;

// 1 — Logo-led opening.
addLogo(slides[0], { centered: true });
addText(
  slides[0],
  "Verify what you read.",
  { left: 340, top: 452, width: 600, height: 52 },
  {
    fontSize: 25,
    color: COLORS.muted,
    alignment: "center",
    name: "opening-tagline",
  },
);
setNotes(
  slides[0],
  "Have you ever read a news article and stopped to wonder: is this actually true? That question is why I built Fact Check.",
);

// 2 — The obvious question.
addLogo(slides[1]);
addText(
  slides[1],
  "Why not just use\nChatGPT or Claude?",
  { left: 150, top: 210, width: 980, height: 230 },
  {
    fontSize: 58,
    bold: true,
    alignment: "center",
    name: "ai-question",
    fontFamily: "Aptos Display",
  },
);
setNotes(
  slides[1],
  "General AI tools can browse and cite sources. But they are open-ended assistants. Fact Check is built around one repeatable verification workflow.",
);

// 3 — The answer.
addLogo(slides[2]);
addText(
  slides[2],
  "A confident answer is not\nthe same as verified evidence.",
  { left: 130, top: 175, width: 1020, height: 210 },
  {
    fontSize: 50,
    bold: true,
    alignment: "center",
    name: "evidence-statement",
    fontFamily: "Aptos Display",
  },
);
addText(
  slides[2],
  "Fact Check shows the claim, the reasoning, and the sources.",
  { left: 220, top: 420, width: 840, height: 70 },
  {
    fontSize: 25,
    color: COLORS.muted,
    alignment: "center",
    name: "evidence-subtitle",
  },
);
setNotes(
  slides[2],
  "Instead of asking you to engineer a prompt, the platform separates claims, searches for evidence, evaluates each source, and keeps the result inspectable.",
);

// 4 — Product hero.
addLogo(slides[3]);
addText(
  slides[3],
  "Fact-check any article\nwithout leaving the page.",
  { left: 80, top: 112, width: 450, height: 150 },
  {
    fontSize: 40,
    bold: true,
    name: "product-headline",
    fontFamily: "Aptos Display",
  },
);
addText(
  slides[3],
  "Open the extension. Check the page. Review the evidence.",
  { left: 80, top: 280, width: 430, height: 90 },
  {
    fontSize: 23,
    color: COLORS.muted,
    name: "product-subtitle",
  },
);
slides[3].images.add({
  blob: heroBytes.buffer.slice(
    heroBytes.byteOffset,
    heroBytes.byteOffset + heroBytes.byteLength,
  ),
  contentType: "image/png",
  alt: "Fact Check extension reviewing an article beside the browser page",
  fit: "contain",
  position: { left: 535, top: 112, width: 665, height: 500 },
});
setNotes(
  slides[3],
  "Fact Check works inside the browser. It reads the article, identifies factual claims, and returns the results beside the original content.",
  [
    "Internal product screenshot: C:/Users/natan/VSC Code/fact-checker/screenshots/01-check-any-page.png",
  ],
);

// 5 — Input modes.
addLogo(slides[4]);
addText(
  slides[4],
  "Choose what you want to check.",
  { left: 110, top: 125, width: 1060, height: 90 },
  {
    fontSize: 45,
    bold: true,
    alignment: "center",
    name: "input-title",
    fontFamily: "Aptos Display",
  },
);
addText(
  slides[4],
  "CURRENT PAGE",
  { left: 100, top: 315, width: 330, height: 70 },
  {
    fontSize: 26,
    bold: true,
    color: COLORS.blue,
    alignment: "center",
    name: "current-page",
  },
);
addText(
  slides[4],
  "PASTE A URL",
  { left: 475, top: 315, width: 330, height: 70 },
  {
    fontSize: 26,
    bold: true,
    color: COLORS.blue,
    alignment: "center",
    name: "paste-url",
  },
);
addText(
  slides[4],
  "PASTE ANY TEXT",
  { left: 850, top: 315, width: 330, height: 70 },
  {
    fontSize: 26,
    bold: true,
    color: COLORS.blue,
    alignment: "center",
    name: "paste-text",
  },
);
addText(
  slides[4],
  "The same evidence workflow for every mode.",
  { left: 290, top: 445, width: 700, height: 60 },
  {
    fontSize: 24,
    color: COLORS.muted,
    alignment: "center",
    name: "input-subtitle",
  },
);
setNotes(
  slides[4],
  "Users can check the page already open in Chrome, paste an article URL, or paste any paragraph directly.",
);

// 6 — Verdict taxonomy.
addLogo(slides[5]);
addText(
  slides[5],
  "Every claim gets an honest outcome.",
  { left: 110, top: 125, width: 1060, height: 90 },
  {
    fontSize: 45,
    bold: true,
    alignment: "center",
    name: "verdict-title",
    fontFamily: "Aptos Display",
  },
);
addText(
  slides[5],
  "TRUE",
  { left: 80, top: 315, width: 350, height: 90 },
  {
    fontSize: 45,
    bold: true,
    color: COLORS.green,
    alignment: "center",
    name: "true",
  },
);
addText(
  slides[5],
  "FALSE",
  { left: 465, top: 315, width: 350, height: 90 },
  {
    fontSize: 45,
    bold: true,
    color: COLORS.red,
    alignment: "center",
    name: "false",
  },
);
addText(
  slides[5],
  "UNVERIFIABLE",
  { left: 850, top: 315, width: 350, height: 90 },
  {
    fontSize: 37,
    bold: true,
    color: COLORS.amber,
    alignment: "center",
    name: "unverifiable",
  },
);
setNotes(
  slides[5],
  "The platform uses three clear outcomes: true, false, or unverifiable. It does not pretend that every question has a decisive answer.",
);

// 7 — Evidence pipeline.
addLogo(slides[6]);
addText(
  slides[6],
  "Evidence comes before the verdict.",
  { left: 110, top: 150, width: 1060, height: 90 },
  {
    fontSize: 47,
    bold: true,
    alignment: "center",
    name: "pipeline-title",
    fontFamily: "Aptos Display",
  },
);
addText(
  slides[6],
  "ARTICLE   →   CLAIMS   →   SOURCES   →   VERDICT",
  { left: 95, top: 315, width: 1090, height: 100 },
  {
    fontSize: 31,
    bold: true,
    color: COLORS.blue,
    alignment: "center",
    name: "pipeline",
  },
);
addText(
  slides[6],
  "Each source is checked against its own claim.",
  { left: 260, top: 450, width: 760, height: 60 },
  {
    fontSize: 24,
    color: COLORS.muted,
    alignment: "center",
    name: "pipeline-subtitle",
  },
);
setNotes(
  slides[6],
  "The system extracts factual claims, searches for evidence, evaluates each source, and only then derives the final verdict.",
);

// 8 — System architecture.
addLogo(slides[7]);
addText(
  slides[7],
  "The browser stays thin.\nThe backend owns verification.",
  { left: 110, top: 105, width: 1060, height: 125 },
  {
    fontSize: 42,
    bold: true,
    alignment: "center",
    name: "architecture-title",
    fontFamily: "Aptos Display",
  },
);
addText(
  slides[7],
  "CHROME EXTENSION   →   FASTAPI   →   JOB MANAGER   →   FACT-CHECK ENGINE",
  { left: 80, top: 295, width: 1120, height: 80 },
  {
    fontSize: 25,
    bold: true,
    color: COLORS.blue,
    alignment: "center",
    name: "architecture-flow",
  },
);
addText(
  slides[7],
  "Readability             Validation + SSE             Capacity + replay             Gemini + Tavily",
  { left: 95, top: 380, width: 1090, height: 60 },
  {
    fontSize: 18,
    color: COLORS.muted,
    alignment: "center",
    name: "architecture-detail",
  },
);
addText(
  slides[7],
  "URL retrieval: Jina Reader   •   Hosted beta: Railway",
  { left: 250, top: 495, width: 780, height: 55 },
  {
    fontSize: 21,
    color: COLORS.navy,
    alignment: "center",
    name: "architecture-footer",
  },
);
setNotes(
  slides[7],
  "The extension is intentionally thin. It captures readable page content, manages the interface, and connects to the backend. FastAPI validates requests and exposes the job API. The job manager controls capacity, progress events, replay, retries, and cancellation. The fact-check engine owns claim extraction, evidence search, source analysis, and verdict derivation.",
);

// 9 — Asynchronous API lifecycle.
addLogo(slides[8]);
addText(
  slides[8],
  "One check is an authenticated,\nasynchronous job.",
  { left: 110, top: 105, width: 1060, height: 125 },
  {
    fontSize: 42,
    bold: true,
    alignment: "center",
    name: "job-title",
    fontFamily: "Aptos Display",
  },
);
addText(
  slides[8],
  "POST /v1/checks   →   202 + JOB TOKEN   →   SSE EVENTS   →   RESULT",
  { left: 90, top: 285, width: 1100, height: 85 },
  {
    fontSize: 27,
    bold: true,
    color: COLORS.blue,
    alignment: "center",
    name: "job-flow",
  },
);
addText(
  slides[8],
  "Snapshot polling   •   Event replay   •   Cancel   •   Retry one claim",
  { left: 190, top: 400, width: 900, height: 65 },
  {
    fontSize: 22,
    color: COLORS.navy,
    alignment: "center",
    name: "job-capabilities",
  },
);
addText(
  slides[8],
  "The bearer capability is required after creation.",
  { left: 260, top: 495, width: 760, height: 55 },
  {
    fontSize: 20,
    color: COLORS.muted,
    alignment: "center",
    name: "job-security",
  },
);
setNotes(
  slides[8],
  "Creating a check returns HTTP 202, an initial snapshot, and a one-time job capability. Later operations require that bearer token. Progress is streamed through Server-Sent Events, while snapshot polling and event replay provide recovery if the side panel disconnects. The same capability also protects cancellation and per-claim retry.",
);

// 10 — Claim-level verification engine.
addLogo(slides[9]);
addText(
  slides[9],
  "Claims move independently\nthrough a bounded pipeline.",
  { left: 110, top: 95, width: 1060, height: 135 },
  {
    fontSize: 42,
    bold: true,
    alignment: "center",
    name: "engine-title",
    fontFamily: "Aptos Display",
  },
);
addText(
  slides[9],
  "1  EXTRACT ≤15 CLAIMS\n2  SEARCH EACH CLAIM\n3  RETAIN ≤3 SOURCES\n4  VERIFY SOURCE STANCES\n5  DERIVE VERDICT IN PYTHON",
  { left: 300, top: 260, width: 680, height: 245 },
  {
    fontSize: 25,
    bold: true,
    color: COLORS.blue,
    alignment: "left",
    name: "engine-steps",
  },
);
addText(
  slides[9],
  "Bounded retries, timeouts, evidence size, and concurrency.",
  { left: 220, top: 535, width: 840, height: 50 },
  {
    fontSize: 20,
    color: COLORS.muted,
    alignment: "center",
    name: "engine-limits",
  },
);
setNotes(
  slides[9],
  "Gemini first extracts up to fifteen checkable factual claims. Tavily searches each claim separately. The engine filters and ranks results, retains at most three sources, and verifies the stance of every source. Supported and contradicted are recomputed from those validated stances, and Python derives the final true, false, or unverifiable label. Claims are pipelined with explicit worker limits, so completed searches can begin verification without waiting for the full batch.",
);

// 11 — Technology stack.
addLogo(slides[10]);
addText(
  slides[10],
  "A small stack with clear responsibilities.",
  { left: 110, top: 100, width: 1060, height: 85 },
  {
    fontSize: 42,
    bold: true,
    alignment: "center",
    name: "stack-title",
    fontFamily: "Aptos Display",
  },
);
addText(
  slides[10],
  "BROWSER",
  { left: 60, top: 245, width: 280, height: 45 },
  {
    fontSize: 22,
    bold: true,
    color: COLORS.blue,
    alignment: "center",
    name: "stack-browser-title",
  },
);
addText(
  slides[10],
  "Chrome Manifest V3\nJavaScript modules\nMozilla Readability\nchrome.storage.session",
  { left: 60, top: 300, width: 280, height: 190 },
  {
    fontSize: 18,
    color: COLORS.navy,
    alignment: "center",
    name: "stack-browser",
  },
);
addText(
  slides[10],
  "BACKEND",
  { left: 500, top: 245, width: 280, height: 45 },
  {
    fontSize: 22,
    bold: true,
    color: COLORS.blue,
    alignment: "center",
    name: "stack-backend-title",
  },
);
addText(
  slides[10],
  "Python 3.13\nFastAPI + Pydantic\nhttpx + SSE\nIn-memory job manager",
  { left: 500, top: 300, width: 280, height: 190 },
  {
    fontSize: 18,
    color: COLORS.navy,
    alignment: "center",
    name: "stack-backend",
  },
);
addText(
  slides[10],
  "PROVIDERS",
  { left: 940, top: 245, width: 280, height: 45 },
  {
    fontSize: 22,
    bold: true,
    color: COLORS.blue,
    alignment: "center",
    name: "stack-providers-title",
  },
);
addText(
  slides[10],
  "Gemini 3.5 Flash-Lite\nTavily Search\nJina Reader\nRailway hosting",
  { left: 940, top: 300, width: 280, height: 190 },
  {
    fontSize: 18,
    color: COLORS.navy,
    alignment: "center",
    name: "stack-providers",
  },
);
addText(
  slides[10],
  "143 automated tests currently pass.",
  { left: 360, top: 535, width: 560, height: 50 },
  {
    fontSize: 21,
    bold: true,
    color: COLORS.green,
    alignment: "center",
    name: "stack-tests",
  },
);
setNotes(
  slides[10],
  "The stack is intentionally conventional. The browser extension uses Manifest V3, JavaScript modules, Mozilla Readability, and session-scoped Chrome storage. The backend uses Python 3.13, FastAPI, Pydantic validation, httpx, Server-Sent Events, and an in-memory job manager for the beta. Gemini handles extraction and verification, Tavily supplies search results, Jina Reader handles URL retrieval, and Railway hosts the current service. The repository currently passes 104 Python tests and 39 extension tests.",
);

// 12 — Security.
addLogo(slides[11]);
addText(
  slides[11],
  "Security is enforced at every boundary.",
  { left: 110, top: 90, width: 1060, height: 85 },
  {
    fontSize: 42,
    bold: true,
    alignment: "center",
    name: "security-title",
    fontFamily: "Aptos Display",
  },
);
const securityItems = [
  ["BYOK SESSION KEYS", "Stored in trusted Chrome session context"],
  ["CAPABILITY TOKENS", "Only a SHA-256 hash is stored"],
  ["EXACT CORS", "Production accepts the configured extension origin"],
  ["URL SAFETY", "Public HTTP(S) only; private addresses rejected"],
  ["SECRET-SAFE OUTPUT", "Keys never enter events, snapshots, or exports"],
  ["RESOURCE CONTROLS", "Size, rate, capacity, timeout, and concurrency limits"],
];
for (const [index, [heading, body]] of securityItems.entries()) {
  const column = index % 2;
  const row = Math.floor(index / 2);
  const left = column === 0 ? 115 : 665;
  const top = 220 + row * 125;
  addText(
    slides[11],
    heading,
    { left, top, width: 500, height: 34 },
    {
      fontSize: 19,
      bold: true,
      color: COLORS.blue,
      name: `security-heading-${index + 1}`,
    },
  );
  addText(
    slides[11],
    body,
    { left, top: top + 38, width: 500, height: 48 },
    {
      fontSize: 17,
      color: COLORS.muted,
      name: `security-body-${index + 1}`,
    },
  );
}
setNotes(
  slides[11],
  "The extension uses bring-your-own-key credentials stored in chrome.storage.session and restricted to trusted extension contexts. Every job receives a random capability token, but the backend stores only its SHA-256 hash and compares hashes in constant time. Production requires an exact chrome-extension CORS origin. URL validation permits public HTTP or HTTPS targets and rejects private, loopback, link-local, and other unsafe destinations. Provider keys are isolated per job and are deliberately excluded from snapshots, events, and exports. Request size, article size, job capacity, creation rate, provider concurrency, retries, and deadlines are all bounded.",
);

// 13 — Honest failure.
addLogo(slides[12]);
addText(
  slides[12],
  "No evidence?\nNo verdict.",
  { left: 170, top: 175, width: 940, height: 220 },
  {
    fontSize: 64,
    bold: true,
    alignment: "center",
    name: "no-evidence",
    fontFamily: "Aptos Display",
  },
);
addText(
  slides[12],
  "When sources are missing or conflicting, Fact Check says so.",
  { left: 230, top: 430, width: 820, height: 70 },
  {
    fontSize: 25,
    color: COLORS.muted,
    alignment: "center",
    name: "no-evidence-subtitle",
  },
);
setNotes(
  slides[12],
  "A safe verification system must be able to say that the available evidence is not enough. Honest uncertainty is better than fabricated confidence.",
);

// 14 — Inspectable output.
addLogo(slides[13]);
addText(
  slides[13],
  "Review the reasoning.\nOpen the sources.\nExport the report.",
  { left: 80, top: 135, width: 430, height: 245 },
  {
    fontSize: 39,
    bold: true,
    name: "review-title",
    fontFamily: "Aptos Display",
  },
);
addText(
  slides[13],
  "The result stays useful after the check is complete.",
  { left: 80, top: 405, width: 420, height: 90 },
  {
    fontSize: 23,
    color: COLORS.muted,
    name: "review-subtitle",
  },
);
slides[13].images.add({
  blob: evidenceBytes.buffer.slice(
    evidenceBytes.byteOffset,
    evidenceBytes.byteOffset + evidenceBytes.byteLength,
  ),
  contentType: "image/png",
  alt: "Fact Check verdict with reasoning and cited evidence",
  fit: "contain",
  position: { left: 520, top: 115, width: 690, height: 520 },
});
setNotes(
  slides[13],
  "Every result keeps the claim, explanation, and sources together. Users can open the evidence directly or export the review as Markdown or JSON.",
);

// 15 — Close.
addLogo(slides[14], { centered: true });
addText(
  slides[14],
  "Read. Question. Verify.",
  { left: 270, top: 452, width: 740, height: 70 },
  {
    fontSize: 36,
    bold: true,
    color: COLORS.navy,
    alignment: "center",
    name: "closing-line",
    fontFamily: "Aptos Display",
  },
);
setNotes(
  slides[14],
  "The next time you wonder whether something online is true, you should be able to verify it where you found it. That is Fact Check.",
);

const fullScripts = [
  "Have you ever been reading a news article and suddenly stopped to think: wait, is this actually true? Maybe the headline sounds exaggerated, a number looks suspicious, or the article makes several claims that would take ten browser tabs to investigate. That is why I built Fact Check. It is a browser extension that checks an article while you are reading it, without forcing you to leave the page.",
  "The obvious question is: why not copy the article into ChatGPT, Claude, or another general AI assistant? Those tools can browse and cite sources, so the difference is not that they are incapable. The difference is the workflow. A general assistant waits for the user to create the right prompt. Fact Check automatically applies a consistent verification process every time.",
  "A fluent answer can still be weakly supported. Fact Check separates the article into individual factual claims, searches for evidence for each claim, checks what every source actually says, and keeps the claim, reasoning, and citations together. The result is inspectable rather than merely persuasive.",
  "Here is the product in use. The article stays on the left and Fact Check stays beside it. I open the extension, select the current page, and start the check. The platform reads the article, extracts the factual claims, retrieves evidence, and returns the results directly beside the original text.",
  "There are three input modes. Current Page checks readable content already open in Chrome. Paste URL asks the backend to retrieve and clean an article through Jina Reader. Paste Text accepts any paragraph or excerpt. All three modes use the same evidence pipeline.",
  "Every check produces one of three outcomes. True means the retrieved evidence directly supports the claim. False means the evidence directly contradicts it. Unverifiable means the available evidence is missing, indirect, outdated, or conflicting. Opinions, predictions, and vague statements are not treated as checkable facts.",
  "The core rule is simple: evidence comes before the verdict. The article is converted into claims. Each claim receives its own search. The retained sources are evaluated individually. Supported and contradicted are then recomputed from those source stances, and the final label is derived in Python instead of trusting a model-generated label.",
  "Technically, the browser stays thin and the backend owns verification. The Manifest V3 extension captures readable content with Mozilla Readability, displays progress, and manages exports. FastAPI validates the request and exposes the job API. The job manager controls capacity, cancellation, retries, snapshots, and event replay. The fact-check engine talks to Gemini and Tavily, while Jina Reader handles article retrieval for URL mode.",
  "A fact-check is an asynchronous job. The extension posts to slash v1 slash checks. The API responds with HTTP 202, an initial snapshot, and a one-time job token. Progress and verdicts stream back through authenticated Server-Sent Events. If that stream disconnects, the client can reconnect using event replay or retrieve the current snapshot. The same bearer capability protects cancellation and retrying one failed claim.",
  "Inside the engine, Gemini extracts at most fifteen checkable claims. Tavily searches each claim separately. Results are filtered, ranked, and limited to three evidence sources. Verification can begin for a claim as soon as its search completes, without waiting for the entire article. The engine bounds provider retries, timeouts, evidence size, concurrency, and the total job deadline. Most importantly, the final verdict is deterministic once the validated source stances are known.",
  "The stack is intentionally small. The browser uses Chrome Manifest V3, JavaScript modules, Mozilla Readability, and session-scoped Chrome storage. The backend uses Python 3.13, FastAPI, Pydantic, httpx, Server-Sent Events, and an in-memory job manager for the current beta. Gemini 3.5 Flash-Lite handles extraction and verification, Tavily provides search evidence, Jina Reader retrieves article text, and Railway hosts the service. The current repository passes 104 Python tests and 39 extension tests.",
  "Security is enforced at several boundaries. Users bring their own Gemini and Tavily keys. The keys live in trusted Chrome session storage and are isolated per job. Every job receives a random capability token, but the backend stores only its SHA-256 hash. Production accepts only the configured extension origin through exact CORS. URL validation rejects private and unsafe network targets. Keys are excluded from snapshots, events, and exports. Request size, article size, job capacity, creation rate, provider concurrency, retries, and deadlines are all bounded.",
  "A trustworthy system must also fail honestly. If a search returns no usable evidence, the verifier is not allowed to invent an answer from model memory. The claim is marked as lacking evidence instead of receiving a normal verdict. When evidence is indirect or conflicting, the result is Unverifiable. An honest I do not know is safer than a confident answer with fabricated support.",
  "The completed review remains useful. Every result contains the original claim, the verdict, a plain-language explanation, confidence, and clickable evidence sources. Users can retry one claim, inspect the original evidence, or export the complete report as Markdown or JSON for research, editorial review, due diligence, or later analysis.",
  "Today, Fact Check helps one person verify one article. The larger goal is to make evidence inspection a normal part of reading online. The next time you wonder whether something is true, you should be able to check it where you found it. Read. Question. Verify. That is Fact Check.",
];

const internalSources = {
  3: [
    "Internal product screenshot: C:/Users/natan/VSC Code/fact-checker/screenshots/01-check-any-page.png",
  ],
  6: [
    "Internal implementation: fact_checker.py lines 705-712 and 986-997",
  ],
  7: [
    "Internal architecture: README.md lines 32-52",
  ],
  8: [
    "Internal API flow: api.py lines 355-480; jobs.py lines 156-205",
  ],
  9: [
    "Internal verification pipeline: fact_checker.py lines 1019-1331",
  ],
  10: [
    "Internal stack: README.md lines 93-99 and 206-255",
  ],
  11: [
    "Internal security controls: api.py lines 198-260; jobs.py lines 181-205 and 755-756; service.py lines 168-288",
  ],
  13: [
    "Internal product screenshot: C:/Users/natan/VSC Code/fact-checker/screenshots/04-verdicts-and-sources.png",
  ],
};

for (const [index, script] of fullScripts.entries()) {
  setNotes(slides[index], script, internalSources[index] ?? []);
}

await fs.writeFile(
  "C:/Users/natan/VSC Code/fact-checker/Presentation-script.txt",
  fullScripts
    .map((script, index) => `SLIDE ${index + 1}\n${script}`)
    .join("\n\n"),
);

await fs.mkdir(previewDir, { recursive: true });
for (const [index, slide] of slides.entries()) {
  const png = await presentation.export({ slide, format: "png", scale: 1 });
  await fs.writeFile(
    `${previewDir}/slide-${String(index + 1).padStart(2, "0")}.png`,
    new Uint8Array(await png.arrayBuffer()),
  );
  const layout = await slide.export({ format: "layout" });
  await fs.writeFile(
    `${previewDir}/slide-${String(index + 1).padStart(2, "0")}.layout.json`,
    await layout.text(),
  );
}

const montage = await presentation.export({
  format: "webp",
  montage: true,
  scale: 1,
});
await fs.writeFile(
  `${previewDir}/montage.webp`,
  new Uint8Array(await montage.arrayBuffer()),
);

const pptx = await PresentationFile.exportPptx(presentation);
await pptx.save(output);

const finalInspect = await presentation.inspect({
  kind: "slide,textbox,image,notes",
  include: "id,slide,name,title,textPreview,bbox,bboxUnit",
  maxChars: 30000,
});
await fs.writeFile(`${previewDir}/final-inspection.ndjson`, finalInspect.ndjson);

console.log(output);
