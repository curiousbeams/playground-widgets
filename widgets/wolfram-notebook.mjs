// Anywidget embedding a public Wolfram Cloud notebook via the official
// wolfram-notebook-embedder (https://github.com/WolframResearch/wolfram-notebook-embedder).
//
// Model keys:
//   url                 (required) public Wolfram Cloud object URL
//   width               number | null (null = adapt to container)
//   maxHeight           number
//   showBorder          boolean
//   allowInteract       boolean (default true)
//   showRenderProgress  boolean (default true)
//   useShadowDOM        boolean (default true — the MyST book theme mounts
//                       every anywidget inside a shadow root, so notebook CSS
//                       injected into document.head would never reach us)
//   css                 string, injected as a <style> alongside the embed

const EMBEDDER_ESM =
  "https://cdn.jsdelivr.net/npm/wolfram-notebook-embedder@0.3.0/+esm";

let embedderPromise;
const loadEmbedder = () => (embedderPromise ??= import(EMBEDDER_ESM));

const ATTR_KEYS = [
  "width",
  "maxHeight",
  "showBorder",
  "allowInteract",
  "showRenderProgress",
  "useShadowDOM",
];

function attributesFromModel(model) {
  const attrs = {};
  for (const key of ATTR_KEYS) {
    const value = model.get(key);
    if (value !== undefined && value !== null) attrs[key] = value;
  }
  attrs.useShadowDOM ??= true;
  return attrs;
}

function showError(el, url, message) {
  const box = document.createElement("div");
  box.style.cssText =
    "border: 1px solid #c62828; border-radius: 4px; padding: 0.75em 1em;" +
    "font-family: monospace; font-size: 0.85em; color: #c62828;" +
    "overflow-wrap: anywhere;";
  box.textContent = `wolfram-notebook: ${message}${url ? ` — ${url}` : ""}`;
  el.appendChild(box);
}

// The Wolfram Cloud runtime behind the embedder keeps page-global state that
// breaks re-embedding once notebooks are detached: after an SPA navigation,
// fresh embed() calls stay stuck in the cloud's initial-render-phase. So we
// never detach. On unmount, the live embed's DOM is parked in a hidden
// off-screen lot; rendering the same notebook URL again re-adopts the parked
// embed (which also makes back-navigation instant). Parked embeds are keyed
// by URL and pooled, so several widgets may share one URL.
let lotElement;
function lot() {
  if (!lotElement) {
    lotElement = document.createElement("div");
    lotElement.setAttribute("data-wolfram-notebook-lot", "");
    lotElement.style.cssText =
      "position: fixed; top: 0; left: 0; width: 800px; height: 0;" +
      "overflow: hidden; visibility: hidden; pointer-events: none;";
    document.body.appendChild(lotElement);
  }
  return lotElement;
}

const parked = new Map(); // url -> [{container, notebook}]

function park(url, container, notebook) {
  lot().appendChild(container);
  let entries = parked.get(url);
  if (!entries) parked.set(url, (entries = []));
  entries.push({ container, notebook });
}

function adopt(url) {
  return parked.get(url)?.pop();
}

export default {
  async render({ model, el }) {
    const url = model.get("url");
    const css = model.get("css");
    if (css) {
      const style = document.createElement("style");
      style.textContent = css;
      el.appendChild(style);
    }

    if (!url) {
      showError(el, null, 'missing required "url" in the widget model');
      return;
    }

    let container;
    let notebook;
    const previous = adopt(url);
    if (previous) {
      ({ container, notebook } = previous);
      el.appendChild(container);
      try {
        notebook.setAttributes(attributesFromModel(model));
      } catch {
        // a stale notebook that rejects attribute updates still renders
      }
    } else {
      container = document.createElement("div");
      container.style.width = "100%";
      el.appendChild(container);
      try {
        const embedder = await loadEmbedder();
        const embed = embedder.embed ?? embedder.default?.embed;
        notebook = await embed(url, container, attributesFromModel(model));
      } catch (error) {
        showError(el, url, error?.message ?? String(error));
        return;
      }
    }

    // The theme's model supports on() but off() throws; the model instance
    // dies with the widget, so we never unsubscribe.
    const update = () => notebook.setAttributes(attributesFromModel(model));
    for (const key of ATTR_KEYS) model.on(`change:${key}`, update);

    return () => {
      park(url, container, notebook);
    };
  },
};
