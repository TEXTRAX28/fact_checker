import { FileBlob, PresentationFile } from "@oai/artifact-tool";
const p = await PresentationFile.importPptx(await FileBlob.load("C:/Users/natan/VSC Code/fact-checker/.codex-presentation-work/gemini-only-20260731/template-starter.pptx"));
const s = p.slides.getItem(0);
const e = s.elements.items.find((item) => item.name === "opening-tagline");
console.log("element", e?.name, Object.keys(e || {}), Object.getOwnPropertyNames(Object.getPrototypeOf(e || {})));
console.log("text", String(e?.text), Object.keys(e?.text || {}), Object.getOwnPropertyNames(Object.getPrototypeOf(e?.text || {})));
console.log("slides", p.slides.items.length, "masters", p.masters.items.length, "layouts", p.layouts.items.length);
