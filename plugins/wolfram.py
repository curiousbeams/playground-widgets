#!/usr/bin/env python3
"""MyST executable plugin embedding interactive Wolfram Language output.

Registers the `wolfram` and `wolfram-notebook` directives plus the
`wolfram-cloud` document transform. Each cell resolves to a public Wolfram
Cloud object — via the committed manifest in
.jupyter-book-wolfram/deployments.json, a `:url:` option, or an opt-in
wolframscript CloudPublish — and is replaced with an anywidget node rendered
client-side by widgets/wolfram-notebook.mjs (no Wolfram Engine for readers).

Page semantics: all `{wolfram}` cells on a page share one kernel session at
deploy time, evaluated in document order, so later cells see definitions from
earlier ones (IncludeDefinitions/SaveDefinitions bake those into each deployed
object). Cell digests are therefore *chained*: cell N's digest covers the code
of cells 1..N, so editing an earlier cell invalidates later deployments.

The expression to deploy is tagged with a full-line comment marker,
`(* #| deploy *)` or `(* #| label: app:my-widget *)`: lines above the marker
are setup, the region below is deployed. Untagged cells are definitions-only.
A label makes the widget a MyST cross-reference target; adding the :caption:
option wraps it in a numbered figure. Marker lines are hidden from :echo:
fences and label renames never invalidate cached deployments.

Deployment is only attempted when JUPYTER_BOOK_WOLFRAM_DEPLOY=1 *and*
wolframscript is on PATH; a cache miss otherwise renders a warning admonition
instead of failing the build, so CI and co-authors need no Wolfram tooling.

Authoring helpers (no MyST involved):
    plugins/wolfram.py --scaffold nb.nb          extract a .nb into an editable
                                                 MyST page (needs wolframscript)
    plugins/wolfram.py --hash < cell.wl          digest of a single/first cell
    plugins/wolfram.py --hash-chain < cells.wl   chained digests; separate
                                                 cells with a line of just %%
    plugins/wolfram.py --record URL < cell.wl    record a deployment by code
    plugins/wolfram.py --record URL --digest D   record by digest (copy it
                                                 from the placeholder box)
    plugins/wolfram.py --record URL --nb f.nb    record a deployed local .nb
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any

TRANSFORM_NAME = "wolfram-cloud"
WOLFRAM_DIRECTIVE = "wolfram"
NOTEBOOK_DIRECTIVE = "wolfram-notebook"
CELL_NODE = "wolframCell"
GENERATED_DIR = ".jupyter-book-wolfram"
MANIFEST_NAME = "deployments.json"
WIDGET_CLASS = "wolfram-cloud-widget"
DEFAULT_ESM = "/widgets/wolfram-notebook.mjs"
ESM_ENV = "JUPYTER_BOOK_WOLFRAM_ESM"
DEPLOY_ENV = "JUPYTER_BOOK_WOLFRAM_DEPLOY"
CLOUD_PATH_ENV = "JUPYTER_BOOK_WOLFRAM_CLOUD_PATH"
CLOUD_BASE_ENV = "JUPYTER_BOOK_WOLFRAM_CLOUD_BASE"
DEFAULT_CLOUD_PATH = "myst-widgets"
DEPLOY_TIMEOUT_SECONDS = 600
SCAFFOLD_TIMEOUT_SECONDS = 300
URL_MARKER = "MYST_WOLFRAM_URL"
ERROR_MARKER = "MYST_WOLFRAM_ERROR"
CELLS_MARKER = "MYST_WOLFRAM_CELLS="
CHAIN_SEPARATOR = "%%"

# --scaffold: notebook cell style -> markdown mapping (the author contract).
HEADING_DEPTH = {"Title": 1, "Chapter": 1, "Section": 2, "Subsection": 3, "Subsubsection": 4}
LIST_MARKERS = {"Item": "- ", "ItemNumbered": "1. ", "Subitem": "  - ", "SubitemNumbered": "  1. "}
CODE_STYLES = {"Input", "Code", "Program"}

# Common WL named characters -> unicode, for prose cells (the front end leaves
# \[Name] literals in exported text). Unknown names are left as-is for review.
WL_NAMED_CHARS = {
    "LongDash": "—", "Dash": "–", "Times": "×", "Divide": "÷", "PlusMinus": "±",
    "Degree": "°", "Rule": "→", "RightArrow": "→", "LeftArrow": "←", "Element": "∈",
    "Infinity": "∞", "PartialD": "∂", "Nabla": "∇", "Sum": "∑", "Product": "∏",
    "Integral": "∫", "LessEqual": "≤", "GreaterEqual": "≥", "NotEqual": "≠",
    "Proportional": "∝", "Angstrom": "Å", "Prime": "′", "VerticalSeparator": "|",
    "Alpha": "α", "Beta": "β", "Gamma": "γ", "Delta": "δ", "Epsilon": "ε",
    "Zeta": "ζ", "Eta": "η", "Theta": "θ", "Kappa": "κ", "Lambda": "λ", "Mu": "μ",
    "Nu": "ν", "Xi": "ξ", "Pi": "π", "Rho": "ρ", "Sigma": "σ", "Tau": "τ",
    "Phi": "φ", "Chi": "χ", "Psi": "ψ", "Omega": "ω",
    "CapitalGamma": "Γ", "CapitalDelta": "Δ", "CapitalTheta": "Θ",
    "CapitalLambda": "Λ", "CapitalPi": "Π", "CapitalSigma": "Σ",
    "CapitalPhi": "Φ", "CapitalPsi": "Ψ", "CapitalOmega": "Ω",
}

# wolframscript driver: read a .nb, emit ordered {style, text} cells as JSON.
# Uses the front end (UsingFrontEnd) to export each cell's exact source text;
# Output cells are dropped. __PATH__/__MARKER__ are substituted from Python.
EXTRACT_DRIVER = r"""
nb = Get[__PATH__];
cells = Cases[nb, Cell[c_, s_String, ___] :> {s, Cell[c, s]}, Infinity];
cells = Select[cells, #[[1]] =!= "Output" &];
export[cell_] := Quiet@Check[First[MathLink`CallFrontEnd[
  FrontEnd`ExportPacket[Append[cell, PageWidth -> Infinity], "InputText"]]], $Failed];
data = UsingFrontEnd[
  Table[With[{txt = export[cells[[i, 2]]]},
    <|"style" -> cells[[i, 1]], "text" -> If[StringQ[txt], txt, ""]|>],
    {i, Length[cells]}]];
Print["__MARKER__" <> ExportString[data, "RawJSON", "Compact" -> True]];
"""

# In-code output tag, mirroring Jupyter/MyST cell magic: a full-line WL comment
# `(* #| deploy *)` or `(* #| label: app:some-label *)`. Lines before the
# marker are setup; the region after it is evaluated and its value deployed.
# Untagged cells are definitions-only (no widget).
MARKER_RE = re.compile(
    r"^\s*\(\*\s*#\|\s*(?:label:\s*(?P<label>\S[^*]*?)|deploy)\s*\*\)\s*$"
)
# Hashing canonicalizes marker lines to this sentinel so renaming a label
# never invalidates a deployment, while moving/adding/removing the marker does.
MARKER_SENTINEL = "(*#|deploy*)"

BOOL = {"type": "boolean"}
STR = {"type": "string"}
NUM = {"type": "number"}

# Directive option -> wolfram-notebook-embedder attribute (model key).
DISPLAY_OPTION_MODEL_KEYS = {
    "width": "width",
    "max-height": "maxHeight",
    "border": "showBorder",
    "interact": "allowInteract",
    "progress": "showRenderProgress",
    "shadow": "useShadowDOM",
    "css": "css",
}
DISPLAY_OPTION_SPECS = {
    "width": NUM,
    "max-height": NUM,
    "border": BOOL,
    "interact": BOOL,
    "progress": BOOL,
    "shadow": BOOL,
    "css": STR,
    "class": STR,
}

PLUGIN_SPEC = {
    "name": "MyST Wolfram",
    "directives": [
        {
            "name": WOLFRAM_DIRECTIVE,
            "doc": (
                "Embed interactive Wolfram Language output as a client-side "
                "Wolfram Cloud widget. All cells on a page share one kernel "
                "session at deploy time (document order). Tag the expression "
                "to deploy with a full-line comment `(* #| deploy *)` or "
                "`(* #| label: my-label *)` (the label makes the widget a "
                "cross-reference target; add :caption: for a numbered figure). "
                "Untagged cells are definitions-only. Use SaveDefinitions->True "
                "in Manipulate when it relies on helper definitions."
            ),
            "options": {
                **DISPLAY_OPTION_SPECS,
                "url": STR,
                "echo": BOOL,
                "caption": STR,
                "defer": BOOL,
            },
            "body": {
                "type": "string",
                "required": False,
                "doc": "Wolfram Language code (optional when the url option is set).",
            },
        },
        {
            "name": NOTEBOOK_DIRECTIVE,
            "doc": (
                "Embed a full Wolfram notebook: either a public Wolfram Cloud "
                "URL or a repo-local .nb file (deployed to the cloud when "
                "deployment is enabled)."
            ),
            "arg": {
                "type": "string",
                "required": True,
                "doc": "Public cloud object URL or repo-local .nb path.",
            },
            "options": {
                **DISPLAY_OPTION_SPECS,
                "hide-input": BOOL,
            },
        },
    ],
    "transforms": [
        {
            "name": TRANSFORM_NAME,
            "doc": "Resolve wolfram cells to deployed cloud objects and anywidget nodes.",
            "stage": "document",
        }
    ],
}


def log(message: str) -> None:
    print(f"[wolfram] {message}", file=sys.stderr)


def declare_result(content: Any) -> None:
    """Keep the executable-plugin protocol to one JSON value on stdout."""
    json.dump(content, sys.stdout)
    raise SystemExit(0)


def normalize_code(code: str) -> str:
    return "\n".join(
        MARKER_SENTINEL if MARKER_RE.match(line) else line.rstrip()
        for line in code.strip().splitlines()
    )


def parse_marker(code: str) -> tuple[str, str | None, str | None]:
    """Split a cell body at its output marker: (setup, tagged, label)."""
    lines = code.splitlines()
    hits = [(i, m) for i, m in ((i, MARKER_RE.match(line)) for i, line in enumerate(lines)) if m]
    if not hits:
        return code, None, None
    if len(hits) > 1:
        raise ValueError("at most one (* #| deploy/label *) marker per {wolfram} cell")
    index, match = hits[0]
    tagged = "\n".join(lines[index + 1 :]).strip()
    if not tagged:
        raise ValueError("the (* #| ... *) marker must be followed by the expression to deploy")
    label = (match.group("label") or "").strip() or None
    return "\n".join(lines[:index]).strip(), tagged, label


def display_code(code: str) -> str:
    """The echoed code fence hides marker lines (like Jupyter hides #| lines)."""
    return "\n".join(line for line in code.splitlines() if not MARKER_RE.match(line)).strip()


def normalize_identifier(label: str) -> str:
    """mystmd normalizeLabel: collapse whitespace, strip quotes, lowercase."""
    collapsed = re.sub(r"[\t\n\r ]+", " ", label)
    return re.sub(r"['‘’\"“”]+", "", collapsed).strip().lower()


def content_hash(code: str) -> str:
    return chain_hashes([code])[0]


def chain_hashes(codes: list[str]) -> list[str]:
    """Chained digests: digest N covers cells 1..N (joined with NUL)."""
    chain = hashlib.sha256()
    digests = []
    for index, code in enumerate(codes):
        if index:
            chain.update(b"\0")
        chain.update(normalize_code(code).encode("utf-8"))
        digests.append(chain.copy().hexdigest()[:16])
    return digests


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def book_source_root() -> Path:
    """MyST spawns executable plugins from the project dir; walk up to be safe."""
    cwd = Path.cwd()
    for parent in (cwd, *cwd.parents):
        if (parent / "myst.yml").exists():
            return parent
    return cwd


def manifest_path() -> Path:
    return book_source_root() / GENERATED_DIR / MANIFEST_NAME


def load_manifest() -> dict[str, Any]:
    path = manifest_path()
    if not path.exists():
        return {"version": 1, "deployments": {}}
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manifest.get("deployments"), dict):
        manifest["deployments"] = {}
    return manifest


def save_deployments(entries: dict[str, dict[str, Any]]) -> None:
    """Merge-then-atomic-replace; entries are content-keyed so merges are idempotent."""
    path = manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest()
    manifest["deployments"].update(entries)
    manifest["deployments"] = dict(sorted(manifest["deployments"].items()))
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2)
            handle.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def reject_unknown_options(options: dict[str, Any], allowed: set[str], directive: str) -> None:
    unknown = sorted(set(options) - allowed)
    if unknown:
        raise ValueError(f"Unknown option(s) for {{{directive}}}: {', '.join(unknown)}")


def node_position(data: dict[str, Any]) -> dict[str, Any] | None:
    node = data.get("node")
    return node.get("position") if isinstance(node, dict) else None


def widget_model(url: str, options: dict[str, Any]) -> dict[str, Any]:
    model: dict[str, Any] = {"url": url}
    for option, model_key in DISPLAY_OPTION_MODEL_KEYS.items():
        value = options.get(option)
        if value is not None:
            model[model_key] = value
    return model


def anywidget_node(
    url: str,
    options: dict[str, Any],
    digest: str,
    index: int,
    position: dict[str, Any] | None,
) -> dict[str, Any]:
    css_class = f"{WIDGET_CLASS} {options.get('class', '')}".strip()
    return {
        "type": "anywidget",
        "id": f"wolfram-{digest}-{index}",
        "esm": os.environ.get(ESM_ENV, DEFAULT_ESM),
        "model": widget_model(url, options),
        "class": css_class,
        "position": position,
    }


def code_node(code: str, position: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "type": "code",
        "lang": "mathematica",
        "value": code,
        "position": position,
    }


def text_node(value: str) -> dict[str, Any]:
    return {"type": "text", "value": value}


def placeholder_admonition(
    code: str, digest: str, position: dict[str, Any] | None
) -> dict[str, Any]:
    remedy = (
        f"No cloud object is recorded for this cell (digest {digest}). Either "
        f"build with {DEPLOY_ENV}=1 on a machine with a licensed wolframscript, "
        "add a :url: option pointing at a public Wolfram Cloud object, or record "
        f"an existing deployment with: plugins/wolfram.py --record <url> --digest {digest}"
    )
    children: list[dict[str, Any]] = [
        {
            "type": "admonitionTitle",
            "children": [text_node("Wolfram widget not deployed")],
        },
        {"type": "paragraph", "children": [text_node(remedy)]},
    ]
    if code:
        children.append(code_node(code, None))
    return {
        "type": "admonition",
        "kind": "warning",
        "children": children,
        "position": position,
    }


def wl_string(value: str) -> str:
    """Escape a Python string into a WL string literal (backslashes, quotes)."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def deploy_enabled() -> bool:
    # PATH presence alone is deliberately insufficient: an unlicensed
    # wolframscript can hang on activation, so deployment is strictly opt-in.
    return os.environ.get(DEPLOY_ENV) == "1" and shutil.which("wolframscript") is not None


class Cell:
    """One collected wolframCell, resolved in page (document) order."""

    def __init__(self, node: dict[str, Any]) -> None:
        self.options = dict(node.get("options") or {})
        self.code = str(node.get("value") or "")
        self.nb_raw = node.get("nbPath")
        self.position = node.get("position")
        self.digest: str | None = None
        self.url: str | None = str(self.options.get("url") or "").strip() or None
        self.error: str | None = None
        try:
            self.setup, self.tagged, self.marker_label = parse_marker(self.code)
        except ValueError as error:
            self.setup, self.tagged, self.marker_label = self.code, None, None
            self.error = str(error)

    @property
    def is_notebook(self) -> bool:
        return self.nb_raw is not None

    @property
    def deployable(self) -> bool:
        """Only tagged code cells (and .nb embeds) produce a cloud object."""
        return self.is_notebook or self.tagged is not None

    @property
    def wants_output(self) -> bool:
        return self.deployable or self.url is not None

    @property
    def nb_path(self) -> Path | None:
        return book_source_root() / str(self.nb_raw) if self.nb_raw else None


def cloud_config() -> tuple[str, str | None]:
    prefix = os.environ.get(CLOUD_PATH_ENV, DEFAULT_CLOUD_PATH).strip("/")
    return prefix, os.environ.get(CLOUD_BASE_ENV)


def publish_call(expr: str, cloud_path: str, digest: str, cloud_base: str | None) -> str:
    options = "IncludeDefinitions -> True"
    if cloud_base:
        options += f", CloudBase -> {wl_string(cloud_base)}"
    marker = wl_string(f"{URL_MARKER}[{digest}]=")
    return f"Print[{marker} <> First[CloudPublish[{expr}, {wl_string(cloud_path)}, {options}]]]"


def build_driver(tmp: Path, cells: list[Cell], publish: set[str]) -> Path:
    """One wolframscript session per page: evaluate every code cell in
    document order (so definitions carry across), publishing only the
    unresolved ones; standalone .nb deployments are appended at the end."""
    cloud_prefix, cloud_base = cloud_config()
    lines: list[str] = []
    for cell in cells:
        if cell.is_notebook or cell.error:
            continue
        if not cell.code:
            continue
        if cell.digest in publish and cell.tagged is not None:
            # Setup evaluates in the page session; the tagged region's value
            # is captured and published, with a guard against Null (a stray
            # trailing semicolon would otherwise deploy an empty notebook).
            if cell.setup:
                setup_file = tmp / f"setup-{cell.digest}.wl"
                setup_file.write_text(cell.setup + "\n", encoding="utf-8")
                lines.append(f"Get[{wl_string(str(setup_file))}];")
            tagged_file = tmp / f"tagged-{cell.digest}.wl"
            tagged_file.write_text(cell.tagged + "\n", encoding="utf-8")
            lines.append(f"MystWolfram`Result = Get[{wl_string(str(tagged_file))}];")
            null_message = wl_string(
                f"{ERROR_MARKER}[{cell.digest}]="
                "tagged region evaluated to Null (trailing semicolon?)"
            )
            lines.append(
                f"If[MystWolfram`Result === Null, Print[{null_message}], "
                + publish_call(
                    "MystWolfram`Result",
                    f"{cloud_prefix}/wolfram-{cell.digest}",
                    cell.digest,
                    cloud_base,
                )
                + "]"
            )
        else:
            # Definitions-only, cached, or :url: cell: evaluate the whole body
            # (marker lines are comments) so its definitions reach later cells.
            cell_file = tmp / f"cell-{cell.digest}.wl"
            cell_file.write_text(cell.code + "\n", encoding="utf-8")
            lines.append(f"Get[{wl_string(str(cell_file))}];")
    for cell in cells:
        if not cell.is_notebook or cell.error or cell.digest not in publish:
            continue
        expr = f'Import[{wl_string(str(cell.nb_path.resolve()))}, "NB"]'
        if cell.options.get("hide-input"):
            expr = (
                f"({expr}) /. Cell[c_, \"Input\", o___] :> "
                f"Cell[c, \"Input\", CellOpen -> False, o]"
            )
        lines.append(
            publish_call(
                expr, f"{cloud_prefix}/wolfram-nb-{cell.digest}", cell.digest, cloud_base
            )
        )
    driver = tmp / "deploy-page.wl"
    driver.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return driver


def deploy_page(cells: list[Cell], publish: set[str]) -> dict[str, dict[str, Any]]:
    """Run the page session and return manifest entries for published cells."""
    cloud_prefix, cloud_base = cloud_config()
    with tempfile.TemporaryDirectory(prefix="myst-wolfram-") as tmp:
        driver = build_driver(Path(tmp), cells, publish)
        try:
            result = subprocess.run(
                ["wolframscript", "-file", str(driver)],
                capture_output=True,
                text=True,
                timeout=DEPLOY_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            log(f"wolframscript failed: {error}")
            return {}
    if result.returncode != 0:
        log(f"wolframscript exited {result.returncode}: {result.stderr.strip()}")
        return {}

    urls: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if line.startswith(f"{URL_MARKER}["):
            digest, _, url = line[len(URL_MARKER) + 1 :].partition("]=")
            urls[digest] = url.strip()
        elif line.startswith(f"{ERROR_MARKER}["):
            digest, _, message = line[len(ERROR_MARKER) + 1 :].partition("]=")
            log(f"deployment of {digest} failed: {message.strip()}")

    entries: dict[str, dict[str, Any]] = {}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for cell in cells:
        digest = cell.digest
        if digest not in publish:
            continue
        url = urls.get(digest)
        if not url:
            log(f"no {URL_MARKER} line for {digest}")
            continue
        kind = "wolfram-nb" if cell.is_notebook else "wolfram"
        entry: dict[str, Any] = {
            "url": url,
            "cloudPath": f"{cloud_prefix}/{kind}-{digest}",
            "deployed": now,
        }
        if cloud_base:
            entry["cloudBase"] = cloud_base
        if cell.is_notebook:
            entry["source"] = str(cell.nb_raw)
        else:
            entry["code"] = normalize_code(cell.code)
        entries[digest] = entry
    return entries


def directive_nodes(name: str, data: dict[str, Any]) -> list[dict[str, Any]]:
    options = dict(data.get("options") or {})
    position = node_position(data)

    if name == WOLFRAM_DIRECTIVE:
        reject_unknown_options(
            options,
            set(DISPLAY_OPTION_SPECS) | {"url", "echo", "caption", "defer"},
            WOLFRAM_DIRECTIVE,
        )
        code = str(data.get("body") or "").strip()
        if not code and not str(options.get("url") or "").strip():
            raise ValueError("{wolfram} requires a code body or the url option")
        parse_marker(code)  # surface marker errors at directive-parse time
        return [
            {
                "type": CELL_NODE,
                "value": code,
                "options": options,
                "position": position,
            }
        ]

    if name == NOTEBOOK_DIRECTIVE:
        reject_unknown_options(
            options, set(DISPLAY_OPTION_SPECS) | {"hide-input"}, NOTEBOOK_DIRECTIVE
        )
        arg = str(data.get("arg") or "").strip()
        if not arg:
            raise ValueError(
                "{wolfram-notebook} requires a cloud URL or local .nb path argument"
            )
        if arg.startswith(("http://", "https://")):
            start_line = (position or {}).get("start", {}).get("line", 0)
            digest = hashlib.sha256(f"{arg}\0{start_line}".encode("utf-8")).hexdigest()[:16]
            return [anywidget_node(arg, options, digest, 0, position)]
        # Local .nb: resolved (manifest/deploy/placeholder) in the transform.
        return [
            {
                "type": CELL_NODE,
                "nbPath": arg,
                "options": options,
                "position": position,
            }
        ]

    raise ValueError(f"Unknown directive: {name}")


def collect_cell_nodes(tree: dict[str, Any]) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []

    def visit(node: dict[str, Any]) -> None:
        if node.get("type") == CELL_NODE:
            nodes.append(node)
        for child in node.get("children") or []:
            if isinstance(child, dict):
                visit(child)

    visit(tree)
    return nodes


def replace_nodes(
    node: dict[str, Any], replacements: dict[int, list[dict[str, Any]]]
) -> None:
    children = node.get("children")
    if not isinstance(children, list):
        return
    new_children: list[Any] = []
    for child in children:
        if isinstance(child, dict) and id(child) in replacements:
            new_children.extend(replacements[id(child)])
            continue
        if isinstance(child, dict):
            replace_nodes(child, replacements)
        new_children.append(child)
    node["children"] = new_children


def resolve_page(cells: list[Cell]) -> None:
    """Fill in each cell's digest and url (manifest, then opt-in deployment)."""
    code_cells = [cell for cell in cells if not cell.is_notebook]
    for cell, digest in zip(code_cells, chain_hashes([cell.code for cell in code_cells])):
        cell.digest = digest
    for cell in cells:
        if cell.is_notebook:
            nb_path = cell.nb_path
            if not nb_path.exists():
                cell.error = f"notebook file not found: {cell.nb_raw}"
            else:
                cell.digest = file_hash(nb_path)

    manifest = load_manifest()
    for cell in cells:
        if cell.url is None and cell.error is None:
            entry = manifest["deployments"].get(cell.digest)
            if isinstance(entry, dict):
                cell.url = entry.get("url")

    publish = {
        cell.digest
        for cell in cells
        if cell.url is None
        and cell.error is None
        and cell.deployable
        and not cell.options.get("defer")
    }
    if publish and deploy_enabled():
        entries = deploy_page(cells, publish)
        if entries:
            save_deployments(entries)
            for cell in cells:
                if cell.digest in entries:
                    cell.url = entries[cell.digest]["url"]


def labeled_output(widget: dict[str, Any], cell: Cell) -> dict[str, Any]:
    """Attach the marker label as a cross-reference target; a :caption: wraps
    the widget in a figure container (numbered by mystmd's enumeration)."""
    label = cell.marker_label
    caption = str(cell.options.get("caption") or "").strip()
    if caption:
        container: dict[str, Any] = {
            "type": "container",
            "kind": "figure",
            "children": [
                widget,
                {
                    "type": "caption",
                    "children": [{"type": "paragraph", "children": [text_node(caption)]}],
                },
            ],
            "position": cell.position,
        }
        if label:
            container["label"] = label
            container["identifier"] = normalize_identifier(label)
        return container
    if label:
        widget["label"] = label
        widget["identifier"] = normalize_identifier(label)
    return widget


def cell_output_nodes(cell: Cell, index: int) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    if cell.options.get("echo") and cell.code:
        nodes.append(code_node(display_code(cell.code), cell.position))
    if cell.error:
        nodes.append(
            placeholder_admonition(
                f"(* {cell.error} *)", cell.digest or "invalid-cell", cell.position
            )
        )
    elif not cell.wants_output:
        pass  # untagged cell: definitions only, fence (if echoed) but no widget
    elif cell.url:
        widget = anywidget_node(cell.url, cell.options, cell.digest, index, cell.position)
        nodes.append(labeled_output(widget, cell))
    else:
        nodes.append(
            placeholder_admonition(
                display_code(cell.code) or str(cell.nb_raw or ""), cell.digest, cell.position
            )
        )
    return nodes


def transform_document(tree: dict[str, Any]) -> dict[str, Any]:
    nodes = collect_cell_nodes(tree)
    if not nodes:
        return tree
    cells = [Cell(node) for node in nodes]
    resolve_page(cells)
    replacements = {
        id(node): cell_output_nodes(cell, index)
        for index, (node, cell) in enumerate(zip(nodes, cells))
    }
    replace_nodes(tree, replacements)
    return tree


def record_deployment(url: str, code: str | None, nb: str | None, digest: str | None) -> None:
    entry: dict[str, Any] = {"url": url}
    if digest is not None:
        if code:
            entry["code"] = normalize_code(code)
    elif nb is not None:
        nb_path = Path(nb)
        if not nb_path.exists():
            raise SystemExit(f"notebook file not found: {nb}")
        digest = file_hash(nb_path)
        entry["source"] = nb
    else:
        digest = content_hash(code or "")
        entry["code"] = normalize_code(code or "")
    entry["deployed"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_deployments({digest: entry})
    print(digest)


def extract_cells(nb_path: Path) -> list[dict[str, str]]:
    """Extract ordered {style, text} cells from a .nb via wolframscript."""
    if shutil.which("wolframscript") is None:
        raise SystemExit(
            "--scaffold needs wolframscript on PATH; run it on a licensed Wolfram machine."
        )
    if not nb_path.exists():
        raise SystemExit(f"notebook file not found: {nb_path}")
    driver_text = EXTRACT_DRIVER.replace(
        "__PATH__", wl_string(str(nb_path.resolve()))
    ).replace("__MARKER__", CELLS_MARKER)
    with tempfile.TemporaryDirectory(prefix="myst-wolfram-") as tmp:
        driver = Path(tmp) / "extract.wl"
        driver.write_text(driver_text, encoding="utf-8")
        try:
            result = subprocess.run(
                ["wolframscript", "-file", str(driver)],
                capture_output=True,
                text=True,
                timeout=SCAFFOLD_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SystemExit(f"wolframscript extraction failed: {error}")
    if result.returncode != 0:
        raise SystemExit(f"wolframscript exited {result.returncode}: {result.stderr.strip()}")
    for line in result.stdout.splitlines():
        if line.startswith(CELLS_MARKER):
            return json.loads(line[len(CELLS_MARKER):])
    raise SystemExit("wolframscript produced no cell data (no MYST_WOLFRAM_CELLS line)")


def is_typeset(text: str) -> bool:
    r"""Typeset display cells (\!\(TraditionalForm...\)) are not runnable code."""
    return text.lstrip().startswith("\\!\\(")


def clean_prose(text: str) -> str:
    r"""Replace common \[Name] literals with unicode in prose cells."""
    return re.sub(
        r"\\\[([A-Za-z]+)\]",
        lambda m: WL_NAMED_CHARS.get(m.group(1), m.group(0)),
        text,
    )


def wolfram_directive(code: str) -> str:
    # Blank line after the options terminates directive-option parsing, so the
    # WL body is taken verbatim (any (* #| ... *) marker in it is preserved).
    return ":::{wolfram}\n:echo: true\n\n" + code.strip() + "\n:::"


def cells_to_markdown(cells: list[dict[str, str]], title: str | None) -> str:
    """Render extracted notebook cells as an editable MyST page (best-effort).

    Headings, prose (Text), and lists (Item) convert directly; code cells
    become {wolfram} directives (add a (* #| deploy/label *) marker to the ones
    you want deployed); typeset equations and unknown styles are emitted with a
    TODO comment for hand review, never silently dropped.
    """
    blocks: list[str] = []
    title_consumed = title is not None

    for cell in cells:
        style = cell.get("style", "")
        text = (cell.get("text") or "").strip()
        if not text:
            continue

        if style in HEADING_DEPTH:
            if not title_consumed and HEADING_DEPTH[style] == 1:
                title = text
                title_consumed = True
                continue
            blocks.append("#" * HEADING_DEPTH[style] + " " + text)
        elif style in LIST_MARKERS:
            item = LIST_MARKERS[style] + clean_prose(text).replace("\n", " ")
            # Keep a run of list items in one block so MyST renders one list.
            if blocks and blocks[-1].startswith(tuple(LIST_MARKERS.values())):
                blocks[-1] += "\n" + item
            else:
                blocks.append(item)
        elif style == "Text":
            blocks.append(clean_prose(text))
        elif style in CODE_STYLES:
            if is_typeset(text):
                blocks.append(
                    "<!-- TODO: typeset equation from the notebook — convert to LaTeX -->\n"
                    "```wl\n" + text + "\n```"
                )
            else:
                blocks.append(wolfram_directive(text))
        else:
            blocks.append(f"<!-- TODO: unsupported cell style '{style}' -->\n" + text)

    frontmatter = f"---\ntitle: {title or 'Untitled'}\n---"
    return frontmatter + "\n\n" + "\n\n".join(blocks) + "\n"


def scaffold(nb: str, title: str | None, out: str | None, force: bool) -> None:
    cells = extract_cells(Path(nb))
    markdown = cells_to_markdown(cells, title)
    if out is None:
        sys.stdout.write(markdown)
        return
    target = Path(out)
    if target.exists() and not force:
        raise SystemExit(f"{out} already exists (use --force to overwrite)")
    target.write_text(markdown, encoding="utf-8")
    log(f"wrote {out} ({len(cells)} cells)")


def split_chain_input(raw: str) -> list[str]:
    cells: list[list[str]] = [[]]
    for line in raw.splitlines():
        if line.strip() == CHAIN_SEPARATOR:
            cells.append([])
        else:
            cells[-1].append(line)
    return ["\n".join(cell) for cell in cells]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nb", help="Local .nb path for --hash/--record")
    parser.add_argument("--digest", help="Record under an explicit digest (see placeholder box)")
    parser.add_argument("-o", "--out", help="Write --scaffold output to a file instead of stdout")
    parser.add_argument("--title", help="Page title for --scaffold (default: notebook's first heading)")
    parser.add_argument("--force", action="store_true", help="Overwrite an existing --scaffold output file")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--role")
    group.add_argument("--directive")
    group.add_argument("--transform")
    group.add_argument("--scaffold", metavar="NB", help="Extract a .nb into an editable MyST page")
    group.add_argument("--hash", action="store_true", help="Print digest of stdin code (or --nb file)")
    group.add_argument("--hash-chain", action="store_true", help="Chained digests; cells separated by a %% line")
    group.add_argument("--record", metavar="URL", help="Record a deployment (stdin code, --nb, or --digest)")
    args = parser.parse_args()

    if args.scaffold:
        scaffold(args.scaffold, args.title, args.out, args.force)
        return
    if args.hash:
        print(file_hash(Path(args.nb)) if args.nb else content_hash(sys.stdin.read()))
        return
    if args.hash_chain:
        for digest in chain_hashes(split_chain_input(sys.stdin.read())):
            print(digest)
        return
    if args.record:
        code = None if (args.nb or sys.stdin.isatty()) else sys.stdin.read()
        record_deployment(args.record, code, args.nb, args.digest)
        return
    if args.directive:
        declare_result(directive_nodes(args.directive, json.load(sys.stdin)))
    if args.transform:
        if args.transform != TRANSFORM_NAME:
            raise ValueError(f"Unknown transform: {args.transform}")
        declare_result(transform_document(json.load(sys.stdin)))
    if args.role:
        raise NotImplementedError(args.role)
    declare_result(PLUGIN_SPEC)


if __name__ == "__main__":
    main()
