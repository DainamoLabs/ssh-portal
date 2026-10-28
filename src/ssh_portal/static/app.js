import RFB from "./vendor/novnc/core/rfb.js";
import { Terminal } from "./vendor/xterm/xterm.mjs";
import { FitAddon } from "./vendor/xterm/addon-fit.mjs";

const STATUS_EVERY_MS = 20_000;
const WS_BASE = `ws://${location.host}`;
const encoder = new TextEncoder();
const enc = encodeURIComponent;
const $ = (id) => document.getElementById(id);

const state = {
  configPath: null,
  example: "",
  hosts: [],
  status: {},             // name -> {online, error}; missing while the first check runs
  view: "screens",
  tiles: new Map(),       // name -> {screen, notice, led, rfb, connecting}
  focus: null,            // {name, rfb}
  terminals: new Map(),   // name -> {term, fit, ws, el, tab, led, ended}
  activeTerminal: null,
  picked: new Set(),
};

// ---------- small helpers ----------

function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value == null || value === false) continue;
    if (key === "class") el.className = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? "" : value);
  }
  el.append(...children.flat().filter((c) => c != null && c !== false));
  return el;
}

async function api(path, options = {}) {
  const res = await fetch(path, { headers: { "content-type": "application/json" }, ...options });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = typeof body.detail === "string" ? body.detail : `${res.status} ${res.statusText}`;
    throw new Error(detail);
  }
  return body;
}

function describe(host) {
  const who = host.user ? `${host.user}@${host.address}` : host.address;
  return host.port ? `${who}:${host.port}` : who;
}

function setLed(led, value) {
  led.dataset.state = value;
}

function ledFor(name) {
  const status = state.status[name];
  if (!status) return "wait";
  return status.online ? "on" : "off";
}

function setNotice(notice, title, detail, action) {
  notice.replaceChildren(
    ...(title ? [h("div", { class: "notice-title" }, title)] : []),
    ...(detail ? [h("div", { class: "notice-detail" }, detail)] : []),
    ...(action ? [h("button", { type: "button", class: "btn", onclick: action.run }, action.label)] : []),
  );
}

// ---------- views ----------

function show(view) {
  const leaving = state.view;
  state.view = view;
  for (const section of document.querySelectorAll(".view")) {
    section.hidden = section.id !== `view-${view}`;
  }
  for (const button of document.querySelectorAll("[data-view]")) {
    const current = button.dataset.view === view || (view === "focus" && button.dataset.view === "screens");
    if (current) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  if (leaving === "focus" && view !== "focus") closeFocus();
  if (view === "screens") connectTiles();
  else disconnectTiles();
  if (view === "terminals") renderTerminals();
}

// ---------- screens ----------

async function attachScreen(name, container, { viewOnly }) {
  await api(`/api/hosts/${enc(name)}/screen`, { method: "POST" });
  const rfb = new RFB(container, `${WS_BASE}/ws/vnc/${enc(name)}`, { shared: true });
  rfb.viewOnly = viewOnly;
  rfb.focusOnClick = !viewOnly;
  rfb.scaleViewport = true;
  rfb.background = "#0d0f12";
  // Thumbnails trade sharpness for bandwidth; the screen you're driving doesn't.
  rfb.qualityLevel = viewOnly ? 3 : 7;
  rfb.compressionLevel = viewOnly ? 8 : 2;
  return rfb;
}

function buildWall() {
  const wall = $("wall");
  wall.replaceChildren();
  state.tiles.clear();

  if (!state.hosts.length) {
    wall.append(
      h("div", { class: "empty" },
        h("h1", {}, "No machines yet"),
        h("p", {}, "Add each machine to ", h("code", {}, state.configPath || "hosts.toml"),
          ", then restart ssh-portal. The README explains how to prepare a machine first."),
        h("pre", {}, state.example)),
    );
    return;
  }

  for (const host of state.hosts) {
    const screen = h("div", { class: "screen" });
    const notice = h("div", { class: "notice" });
    const led = h("span", { class: "led", "aria-hidden": "true" });
    wall.append(
      h("article", { class: "monitor", "aria-label": host.name },
        h("div", { class: "well" },
          screen,
          notice,
          h("button", {
            type: "button",
            class: "well-hit",
            "aria-label": `Take control of ${host.name}`,
            onclick: () => openFocus(host.name),
          })),
        h("div", { class: "chin" },
          led,
          h("span", { class: "name" }, host.name),
          h("span", { class: "where" }, describe(host)),
          h("span", { class: "actions" },
            h("button", { type: "button", class: "btn quiet", onclick: () => openTerminal(host.name) }, "Terminal")))),
    );
    state.tiles.set(host.name, { screen, notice, led, rfb: null, connecting: false });
  }
}

function connectTiles() {
  for (const host of state.hosts) connectTile(host.name);
}

async function connectTile(name) {
  const tile = state.tiles.get(name);
  if (!tile || tile.rfb || tile.connecting || state.view !== "screens") return;
  const status = state.status[name];
  if (!status) {
    setNotice(tile.notice, `Checking ${name}…`);
    return;
  }
  if (!status.online) {
    setNotice(tile.notice, `Can't reach ${name}`, status.error, { label: "Check again", run: () => refreshStatus(name) });
    return;
  }

  tile.connecting = true;
  setNotice(tile.notice, "Connecting…");
  let rfb;
  try {
    rfb = await attachScreen(name, tile.screen, { viewOnly: true });
  } catch (err) {
    setNotice(tile.notice, "Screen unavailable", err.message, { label: "Try again", run: () => connectTile(name) });
    return;
  } finally {
    tile.connecting = false;
  }
  if (state.view !== "screens") {
    rfb.disconnect();
    return;
  }
  tile.rfb = rfb;
  rfb.addEventListener("connect", () => setNotice(tile.notice));
  rfb.addEventListener("disconnect", (e) => {
    if (tile.rfb !== rfb) return;
    tile.rfb = null;
    if (state.view === "screens") {
      setNotice(tile.notice, "Screen disconnected", e.detail.clean ? "" : "The connection dropped.",
        { label: "Reconnect", run: () => connectTile(name) });
    }
  });
}

function disconnectTiles() {
  for (const tile of state.tiles.values()) {
    const rfb = tile.rfb;
    tile.rfb = null;
    rfb?.disconnect();
  }
}

async function openFocus(name) {
  const host = state.hosts.find((h) => h.name === name);
  show("focus");
  $("focus-name").textContent = name;
  $("focus-where").textContent = describe(host);
  setLed($("focus-led"), ledFor(name));
  $("focus-scale").setAttribute("aria-pressed", "true");

  const container = $("focus-screen");
  const notice = h("div", { class: "notice" });
  container.replaceChildren(notice);
  setNotice(notice, `Connecting to ${name}…`);
  const focus = { name, rfb: null };
  state.focus = focus;

  let rfb;
  try {
    rfb = await attachScreen(name, container, { viewOnly: false });
  } catch (err) {
    if (state.focus === focus) {
      setNotice(notice, "Screen unavailable", err.message, { label: "Try again", run: () => openFocus(name) });
    }
    return;
  }
  if (state.focus !== focus) {
    rfb.disconnect();
    return;
  }
  focus.rfb = rfb;
  rfb.addEventListener("connect", () => {
    setNotice(notice);
    rfb.focus();
  });
  rfb.addEventListener("disconnect", (e) => {
    if (state.focus !== focus) return;
    focus.rfb = null;
    setNotice(notice, "Disconnected", e.detail.clean ? "" : "The connection dropped.",
      { label: "Reconnect", run: () => openFocus(name) });
  });
}

function closeFocus() {
  const rfb = state.focus?.rfb;
  state.focus = null;
  rfb?.disconnect();
  $("focus-screen").replaceChildren();
}

function toggleScale() {
  const rfb = state.focus?.rfb;
  if (!rfb) return;
  rfb.scaleViewport = !rfb.scaleViewport;
  $("focus-scale").setAttribute("aria-pressed", String(rfb.scaleViewport));
}

// ---------- terminals ----------

function openTerminal(name) {
  show("terminals");
  if (!state.terminals.has(name)) createTerminal(name);
  activateTerminal(name);
}

function createTerminal(name) {
  const el = h("div", { class: "term", role: "tabpanel", "aria-label": `Terminal on ${name}` });
  $("term-stage").append(el);

  const term = new Terminal({
    fontFamily: '"DejaVu Sans Mono", "Ubuntu Mono", monospace',
    fontSize: 14,
    cursorBlink: true,
    scrollback: 5000,
    theme: {
      background: "#0d0f12",
      foreground: "#d7dce3",
      cursor: "#e8edf3",
      selectionBackground: "#3e4652",
    },
  });
  const fit = new FitAddon();
  term.loadAddon(fit);

  const led = h("span", { class: "led", "aria-hidden": "true" });
  const tab = h("div", { class: "tab", role: "tab" },
    h("button", { type: "button", class: "tab-select", onclick: () => activateTerminal(name) }, led, name),
    h("button", { type: "button", class: "tab-close", "aria-label": `Close terminal on ${name}`, onclick: () => closeTerminal(name) }, "×"));

  const t = { name, el, term, fit, tab, led, ws: null, ended: false, opened: false };
  state.terminals.set(name, t);

  term.onData((data) => send(t, encoder.encode(data)));
  term.onBinary((data) => send(t, Uint8Array.from(data, (c) => c.charCodeAt(0))));
  term.onResize(({ cols, rows }) => {
    if (t.ws?.readyState === WebSocket.OPEN) t.ws.send(JSON.stringify({ type: "resize", cols, rows }));
  });
  new ResizeObserver(() => {
    if (t.opened && el.clientWidth > 0) fit.fit();
  }).observe(el);
  return t;
}

function connectTerminal(t) {
  t.ended = false;
  setLed(t.led, "wait");
  const ws = new WebSocket(`${WS_BASE}/ws/term/${enc(t.name)}?cols=${t.term.cols}&rows=${t.term.rows}`);
  ws.binaryType = "arraybuffer";
  ws.onopen = () => {
    setLed(t.led, "on");
    ws.send(JSON.stringify({ type: "resize", cols: t.term.cols, rows: t.term.rows }));
  };
  ws.onmessage = (e) => {
    if (typeof e.data === "string") {
      const msg = JSON.parse(e.data);
      if (msg.type === "error") t.term.write(`\r\n\x1b[31m${msg.message}\x1b[0m\r\n`);
      return;
    }
    t.term.write(new Uint8Array(e.data));
  };
  ws.onclose = () => {
    if (t.ws !== ws) return;
    t.ended = true;
    setLed(t.led, "off");
    t.term.write("\r\n\x1b[2mSession ended. Press Enter to reconnect.\x1b[0m\r\n");
  };
  t.ws = ws;
}

function send(t, bytes) {
  if (t.ended) {
    if (bytes.length === 1 && bytes[0] === 13) connectTerminal(t);
    return;
  }
  if (t.ws?.readyState === WebSocket.OPEN) t.ws.send(bytes);
}

function activateTerminal(name) {
  state.activeTerminal = name;
  renderTerminals();
  const t = state.terminals.get(name);
  if (!t.opened) {
    // xterm measures its font on open, so the panel must be visible first.
    t.term.open(t.el);
    t.opened = true;
    t.fit.fit();
    connectTerminal(t);
  } else {
    t.fit.fit();
  }
  t.term.focus();
}

function closeTerminal(name) {
  const t = state.terminals.get(name);
  if (!t) return;
  state.terminals.delete(name);
  const ws = t.ws;
  t.ws = null;
  ws?.close();
  t.term.dispose();
  t.el.remove();
  if (state.activeTerminal === name) {
    const next = [...state.terminals.keys()].at(-1) ?? null;
    state.activeTerminal = null;
    if (next) activateTerminal(next);
  }
  renderTerminals();
}

function renderTerminals() {
  const tabs = $("term-tabs");
  const stage = $("term-stage");
  stage.querySelector(".term-empty")?.remove();

  tabs.replaceChildren(...[...state.terminals.values()].map((t) => t.tab));
  for (const t of state.terminals.values()) {
    const active = t.name === state.activeTerminal;
    t.tab.setAttribute("aria-selected", String(active));
    t.el.hidden = !active;
  }
  tabs.hidden = state.terminals.size === 0;

  if (state.terminals.size === 0) {
    stage.append(
      h("div", { class: "term-empty" },
        h("p", {}, state.hosts.length ? "Open a shell on:" : "Add a machine to open a terminal on it."),
        h("div", { class: "pick-row" },
          state.hosts.map((host) =>
            h("button", { type: "button", class: "btn", onclick: () => openTerminal(host.name) }, host.name)))),
    );
  }
}

// ---------- run a command ----------

function buildRun() {
  const picks = $("run-picks");
  picks.querySelector(".row")?.remove();
  const row = h("div", { class: "row" });
  for (const host of state.hosts) {
    state.picked.add(host.name);
    const led = h("span", { class: "led", "aria-hidden": "true", "data-pick-led": host.name });
    const button = h("button", { type: "button", class: "pick", "aria-pressed": "true" }, led, host.name);
    button.addEventListener("click", () => {
      const on = !state.picked.has(host.name);
      if (on) state.picked.add(host.name);
      else state.picked.delete(host.name);
      button.setAttribute("aria-pressed", String(on));
      updateRunButton();
    });
    row.append(button);
  }
  picks.append(row);
  updateRunButton();
}

function updateRunButton() {
  const n = state.picked.size;
  const button = $("run-submit");
  button.textContent = n === 1 ? "Run on 1 machine" : `Run on ${n} machines`;
  button.disabled = n === 0;
}

async function runCommand(event) {
  event.preventDefault();
  const command = $("run-command").value.trim();
  const hosts = state.hosts.map((h) => h.name).filter((n) => state.picked.has(n));
  if (!command || !hosts.length) return;
  const timeout = Math.min(Math.max(Number($("run-timeout").value) || 60, 1), 3600);

  const blocks = new Map(hosts.map((name) => {
    const led = h("span", { class: "led", "aria-hidden": "true" });
    const stateText = h("span", { class: "result-state" }, "Running…");
    const body = h("div");
    const el = h("section", { class: "result", "aria-label": name },
      h("div", { class: "result-head" }, led, h("span", { class: "name" }, name), stateText),
      body);
    return [name, { el, led, stateText, body }];
  }));
  $("run-results").replaceChildren(...[...blocks.values()].map((b) => b.el));

  const submit = $("run-submit");
  submit.disabled = true;
  // One request per machine, so fast machines report while slow ones still run.
  await Promise.all(hosts.map(async (name) => {
    let result;
    try {
      result = (await api("/api/run", { method: "POST", body: JSON.stringify({ hosts: [name], command, timeout }) }))[name];
    } catch (err) {
      result = { error: err.message };
    }
    showResult(blocks.get(name), result);
  }));
  submit.disabled = false;
  updateRunButton();
}

function showResult(block, result) {
  const ok = !result.error && result.exit_status === 0;
  setLed(block.led, ok ? "on" : "off");
  block.stateText.textContent = result.error
    ? result.error
    : result.exit_signal
      ? `Killed by ${result.exit_signal}`
      : `Exit ${result.exit_status}`;
  const parts = [];
  if (result.stdout) parts.push(h("pre", {}, result.stdout));
  if (result.stderr) parts.push(h("pre", { class: "stderr" }, result.stderr));
  if (!parts.length && !result.error) parts.push(h("p", { class: "no-output" }, "No output"));
  block.body.replaceChildren(...parts);
}

// ---------- machine status ----------

async function refreshStatus(name) {
  try {
    state.status[name] = await api(`/api/hosts/${enc(name)}/status`);
  } catch (err) {
    state.status[name] = { online: false, error: err.message };
  }
  renderStatus(name);
}

function refreshAll() {
  return Promise.all(state.hosts.map((host) => refreshStatus(host.name)));
}

function renderStatus(name) {
  const led = ledFor(name);
  const tile = state.tiles.get(name);
  if (tile) {
    setLed(tile.led, led);
    if (state.view === "screens") connectTile(name);
  }
  document.querySelectorAll(`[data-pick-led="${CSS.escape(name)}"]`).forEach((el) => setLed(el, led));
  if (state.focus?.name === name) setLed($("focus-led"), led);

  const known = state.hosts.filter((h) => state.status[h.name]);
  const online = known.filter((h) => state.status[h.name].online).length;
  $("fleet-summary").textContent = known.length < state.hosts.length
    ? "Checking machines…"
    : `${online} of ${state.hosts.length} online`;
}

// ---------- start ----------

async function start() {
  for (const button of document.querySelectorAll("[data-view]")) {
    button.addEventListener("click", () => show(button.dataset.view));
  }
  $("focus-back").addEventListener("click", () => show("screens"));
  $("focus-terminal").addEventListener("click", () => state.focus && openTerminal(state.focus.name));
  $("focus-scale").addEventListener("click", toggleScale);
  $("focus-full").addEventListener("click", () => $("focus-screen").requestFullscreen?.());
  $("fleet-refresh").addEventListener("click", refreshAll);
  $("run-form").addEventListener("submit", runCommand);
  $("run-command").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) $("run-form").requestSubmit();
  });

  const config = await api("/api/hosts");
  state.hosts = config.hosts;
  state.configPath = config.config_path;
  state.example = config.example;

  buildWall();
  buildRun();
  renderTerminals();
  show("screens");

  if (state.hosts.length) {
    $("fleet-summary").textContent = "Checking machines…";
    refreshAll();
    setInterval(refreshAll, STATUS_EVERY_MS);
  } else {
    $("fleet-summary").textContent = "No machines configured";
    $("fleet-refresh").hidden = true;
  }
}

start().catch((err) => {
  $("wall").replaceChildren(h("div", { class: "empty" }, h("h1", {}, "ssh-portal didn't load"), h("p", {}, err.message)));
});
