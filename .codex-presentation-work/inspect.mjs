import fs from "node:fs/promises";
import { PresentationFile, FileBlob } from "@oai/artifact-tool";

const input = "C:/Users/natan/VSC Code/fact-checker/Presentation.pptx";
const presentation = await PresentationFile.importPptx(await FileBlob.load(input));

const snapshot = await presentation.inspect({
  kind: "deck,slide,textbox,shape,image,layout,notes",
  include: "id,slide,name,title,text,textPreview,bbox,bboxUnit,isPlaceholder,placeholders",
  maxChars: 50000,
});

await fs.writeFile(
  "C:/Users/natan/VSC Code/fact-checker/.codex-presentation-work/inspection.ndjson",
  snapshot.ndjson,
);

const summary = {
  slideCount: presentation.slides.items.length,
  layoutCount: presentation.layouts.items.length,
  masterCount: presentation.masters.items.length,
  slides: presentation.slides.items.map((slide, index) => ({
    index,
    id: slide.id,
    layoutId: slide.layoutId,
    localElements: slide.elements?.items?.length ?? null,
    placeholders: slide.placeholders?.summary?.() ?? null,
  })),
  layouts: presentation.layouts.items.map((layout) => ({
    id: layout.id,
    name: layout.name,
    parentLayoutId: layout.parentLayoutId,
    placeholders: layout.placeholders?.summary?.() ?? null,
  })),
  masters: presentation.masters.items.map((master) => ({
    id: master.id,
    name: master.name,
    elements: master.elements?.items?.length ?? null,
    placeholders: master.placeholders?.summary?.() ?? null,
  })),
};

await fs.writeFile(
  "C:/Users/natan/VSC Code/fact-checker/.codex-presentation-work/structure.json",
  JSON.stringify(summary, null, 2),
);

console.log(JSON.stringify(summary, null, 2));
