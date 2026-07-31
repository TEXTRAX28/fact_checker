import { PresentationFile, FileBlob } from "@oai/artifact-tool";

const presentation = await PresentationFile.importPptx(
  await FileBlob.load("C:/Users/natan/VSC Code/fact-checker/Presentation.pptx"),
);
const slide = presentation.slides.getItem(0);
const image = slide.images.items[0];

console.log("slide", Object.keys(slide), Object.getOwnPropertyNames(Object.getPrototypeOf(slide)));
console.log("elements", Object.keys(slide.elements ?? {}), Object.getOwnPropertyNames(Object.getPrototypeOf(slide.elements ?? {})));
console.log("images", Object.keys(slide.images ?? {}), Object.getOwnPropertyNames(Object.getPrototypeOf(slide.images ?? {})));
console.log("image", Object.keys(image), Object.getOwnPropertyNames(Object.getPrototypeOf(image)));
