const NS = "http://www.w3.org/2000/svg";

const ICONS = Object.freeze({
  "shield-check": [
    ["path", { d: "M20 13c0 5-3.5 7.5-7.7 9a1 1 0 0 1-.6 0C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.2-2.7a1.2 1.2 0 0 1 1.6 0C14.5 3.8 17 5 19 5a1 1 0 0 1 1 1z" }],
    ["path", { d: "m9 12 2 2 4-4" }],
  ],
  "file-text": [
    ["path", { d: "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" }],
    ["path", { d: "M14 2v6h6M8 13h8M8 17h6" }],
  ],
  link: [
    ["path", { d: "M10 13a5 5 0 0 0 7.5.5l2-2a5 5 0 0 0-7-7l-1.1 1" }],
    ["path", { d: "M14 11a5 5 0 0 0-7.5-.5l-2 2a5 5 0 0 0 7 7l1.1-1" }],
  ],
  "align-left": [
    ["path", { d: "M4 6h16M4 12h12M4 18h16" }],
  ],
  check: [["path", { d: "m5 12 4 4L19 6" }]],
  x: [["path", { d: "M18 6 6 18M6 6l12 12" }]],
  "triangle-alert": [
    ["path", { d: "m21.7 18-8-14a2 2 0 0 0-3.4 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.7-3" }],
    ["path", { d: "M12 9v4M12 17h.01" }],
  ],
  "circle-help": [
    ["circle", { cx: "12", cy: "12", r: "10" }],
    ["path", { d: "M9.1 9a3 3 0 1 1 5.8 1c0 2-3 2-3 4M12 18h.01" }],
  ],
  "external-link": [
    ["path", { d: "M15 3h6v6M10 14 21 3M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" }],
  ],
  "rotate-cw": [
    ["path", { d: "M21 12a9 9 0 1 1-2.6-6.4L21 8" }],
    ["path", { d: "M21 3v5h-5" }],
  ],
  square: [["rect", { x: "4", y: "4", width: "16", height: "16", rx: "2" }]],
  download: [
    ["path", { d: "M12 15V3" }],
    ["path", { d: "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" }],
    ["path", { d: "m7 10 5 5 5-5" }],
  ],
  search: [
    ["circle", { cx: "11", cy: "11", r: "8" }],
    ["path", { d: "m21 21-4.3-4.3" }],
  ],
});

export function createIcon(name, size = 18) {
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", String(size));
  svg.setAttribute("height", String(size));
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "2");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  svg.classList.add("icon");

  for (const [tag, attributes] of ICONS[name] || ICONS["circle-help"]) {
    const child = document.createElementNS(NS, tag);
    for (const [key, value] of Object.entries(attributes)) child.setAttribute(key, value);
    svg.append(child);
  }
  return svg;
}

export function hydrateIcons(root = document) {
  for (const target of root.querySelectorAll("[data-icon]")) {
    target.replaceChildren(createIcon(target.dataset.icon, Number(target.dataset.iconSize) || 18));
  }
}
