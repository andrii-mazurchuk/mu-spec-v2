// The one text-only rendering path (S-509): every view draws through el and mount, every string
// child becomes a text node, and a slice mark is set by one property. This module also owns the
// colour path (the node palette tokens, each read with today's fallback, and today's slice colours),
// the side mark set once at page open, and the sink lists and match rules the page source sweeps
// use. It imports nothing from the page build script, which runs its build when loaded.

export const TAGS = ["div", "span", "p", "ul", "ol", "li", "table", "thead", "tbody", "tr", "th", "td", "button", "select",
  "option", "input", "label", "h1", "h2", "h3", "pre", "code", "small", "strong", "em", "details", "summary", "section", "header"] as const

export type Tag = typeof TAGS[number];
export type SliceIndex = 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9;
export type Props = {
  cls?: string;
  data?: Record<string, string>;
  title?: string;
  type?: "button" | "checkbox" | "text";
  value?: string;
  disabled?: boolean;
  selected?: boolean;
  checked?: boolean;
  /** The element shows a slice; null: 'no slice'. */
  slice?: SliceIndex | null;
  onClick?: () => void;
  onChange?: (value: string) => void;
};
export type Child = Node | string | null;

/** The node's palette tokens today's page reads, in the order it first uses them. */
export const PALETTE = ["--ground", "--ink", "--panel", "--brand", "--brand-ink", "--panel-alt", "--ink-soft",
  "--down", "--rule", "--dim", "--ok", "--warn", "--panel-3"] as const
export type Token = typeof PALETTE[number];

export const FALLBACK_PREFIX = "--fallback-";

/** FALLBACK_PREFIX + token without its leading "--", e.g. "--fallback-ink". */
export function fallbackName(token: Token): string {
  return FALLBACK_PREFIX + token.slice(2);
}

/** The token, falling back to its side's fallback where the node does not supply it. */
export function colour(token: Token): string {
  return "var(" + token + ", var(" + fallbackName(token) + "))";
}

/** Today's ten fixed slice colours, under today's names, in today's order. */
export const SLICE_VARS = ["--slice-1", "--slice-2", "--slice-3", "--slice-4", "--slice-5",
  "--slice-6", "--slice-7", "--slice-8", "--slice-9", "--slice-10"] as const
/** Today's 'no slice' colour, under today's name. */
export const NO_SLICE_VAR = "--slice-none";

/** Index 0 is today's --slice-1. */
export function sliceColour(index: SliceIndex | null): string {
  return "var(" + (index === null ? NO_SLICE_VAR : SLICE_VARS[index]) + ")";
}

/** The data attribute el sets on every element that shows a slice. */
export const SLICE_DATA = "slice";
/** The CSS properties today's page paints a slice marking with (its bg-, fill- and stroke- slice classes). */
export const SLICE_PROPERTIES: readonly string[] = ["background-color", "fill", "stroke"];

export const SIDES = ["light", "dark"] as const;
/** The class today's page adds to the document's root element for its dark side. */
export const DARK_MARK = "dark";
/** The selector each side's palette declarations sit under, as today's page writes them. */
export const SIDE_CONDITION = { light: ":root", dark: ".dark" } as const;

/** Today's condition for the dark side: the top-level document, in a browser preferring dark. */
export function darkSide(win: Window): boolean {
  return win.self === win.top && win.matchMedia("(prefers-color-scheme: dark)").matches;
}

/** Called once, by the page's start before its first draw; a later preference change changes nothing. */
export function markSide(win: Window): void {
  if (darkSide(win)) win.document.documentElement.classList.add(DARK_MARK);
}

export const PALETTE_BEGIN = "/*palette*/";
export const PALETTE_END = "/*end palette*/";
/** 13 PALETTE fallbacks x 2 SIDES + 11 slice colours x 2 SIDES. */
export const PALETTE_DECLARATIONS = 48;

export const MARKUP_SINKS = ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "DOMParser",
  "createContextualFragment", "srcdoc", "setAttribute", "window.open", "javascript:", "<a", "<form", "target="]

export const ADDRESS_SINKS = ["location", "history.", "localStorage", "sessionStorage"]

/** The one file allowed to name an ADDRESS_SINKS member. */
export const ADDRESS_MODULE = "dashboard/src/address.ts";

export const COLOUR_LITERAL = /#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color)\(/

export const COLOUR_VAR = /var\(\s*(--[A-Za-z0-9-]+)\s*(?:,\s*var\(\s*(--[A-Za-z0-9-]+)\s*\)\s*)?\)/g

/** Declarations in this file whose own text the sweeps skip. */
export const SWEPT_DECLARATIONS = ["MARKUP_SINKS", "ADDRESS_SINKS", "COLOUR_LITERAL", "COLOUR_VAR"];
/** An identifier character, for the bounded naming rule. */
export const IDENT_CHARS = /[A-Za-z0-9_$]/;

function toNodes(children: Child[]): Node[] {
  const nodes: Node[] = [];
  for (const child of children) {
    if (child === null) continue;
    nodes.push(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return nodes;
}

export function el(tag: Tag, props: Props, ...children: Child[]): HTMLElement {
  const e = document.createElement(tag);
  const fields = e as unknown as Record<string, unknown>;
  if (props.cls !== undefined) e.className = props.cls;
  if (props.data !== undefined) Object.assign(e.dataset, props.data);
  if (props.title !== undefined) fields.title = props.title;
  if (props.type !== undefined) fields.type = props.type;
  if (props.value !== undefined) fields.value = props.value;
  if (props.disabled !== undefined) fields.disabled = props.disabled;
  if (props.selected !== undefined) fields.selected = props.selected;
  if (props.checked !== undefined) fields.checked = props.checked;
  if (props.slice !== undefined) {
    const slice = props.slice;
    e.dataset[SLICE_DATA] = slice === null ? "none" : String(slice);
    for (const p of SLICE_PROPERTIES) e.style.setProperty(p, sliceColour(slice));
  }
  const { onClick, onChange } = props;
  if (onClick !== undefined) e.addEventListener("click", () => onClick());
  if (onChange !== undefined) e.addEventListener("change", () => onChange(String(fields.value ?? "")));
  e.append(...toNodes(children));
  return e;
}

/** Replaces the container's children, by the same conversion as el. */
export function mount(container: Element, ...children: Child[]): void {
  container.replaceChildren(...toNodes(children));
}
