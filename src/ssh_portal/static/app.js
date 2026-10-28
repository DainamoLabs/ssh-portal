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
  editable: false,
  localUser: "",
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
    const detail = typeof body.detail === "string"
      ? body.detail
      : Array.isArray(body.detail)
        ? body.detail.map((d) => `${d.loc?.at(-1) ?? "value"}: ${d.msg}`).join("; ")
        : `${res.status} ${res.statusText}`;
    throw new Error(detail);
  }
  return body;
}

function post(body) {
  return { method: "POST", body: JSON.stringify(body) };
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

// Machines that answer but can't be logged in to yet, by what's missing.
const SETUP_LEFT = {
  trust: "ssh-portal hasn't confirmed this machine's identity yet.",
  key: "It doesn't accept your key yet.",
};

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
      state.editable
        ? h("div", { class: "empty" },
            h("h1", {}, "No machines yet"),
            h("p", {}, "Run ", h("code", {}, "sudo bash setup.sh"),
              " on each machine you want to control, then add it here."),
            h("button", { type: "button", class: "btn primary", onclick: openAddDialog }, "Add a machine"))
        : h("div", { class: "empty" },
            h("h1", {}, "No machines yet"),
            h("p", {}, "Add each machine to ", h("code", {}, "hosts.toml"), ", then restart ssh-portal."),
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
    const unfinished = SETUP_LEFT[status.needs];
    if (unfinished) {
      setNotice(tile.notice, `Finish setting up ${name}`, unfinished, { label: "Finish setup", run: () => openRepairDialog(name) });
    } else {
      setNotice(tile.notice, `Can't reach ${name}`, status.error, { label: "Check again", run: () => refreshStatus(name) });
    }
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
    const led = h("span", { class: "led", "aria-hidden": "true", "data-pick-led": host.name });
    const pressed = String(state.picked.has(host.name));
    const button = h("button", { type: "button", class: "pick", "aria-pressed": pressed }, led, host.name);
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

  renderFleet();
}

function renderFleet() {
  const known = state.hosts.filter((h) => state.status[h.name]);
  const online = known.filter((h) => state.status[h.name].online).length;
  $("fleet-summary").textContent = !state.hosts.length
    ? ""
    : known.length < state.hosts.length
      ? "Checking machines…"
      : `${online} of ${state.hosts.length} online`;
  $("fleet-refresh").hidden = !state.hosts.length;
}

/** Re-render everything that lists machines, after one is added or removed. */
function refreshMachines() {
  disconnectTiles();
  buildWall();
  buildRun();
  renderTerminals();
  for (const host of state.hosts) {
    if (state.status[host.name]) renderStatus(host.name);
  }
  renderFleet();
  if (state.view === "screens") connectTiles();
}

// ---------- add and remove machines ----------

const NEARBY_EVERY_MS = 3_000;

const adding = {
  repairing: null,      // name of a listed machine being finished, or null when adding a new one
  target: null,         // {address, port, user}
  display: ":0",
  result: null,         // the latest check of target
  installedKey: false,
  checkRun: 0,          // so a slow, stale check can't overwrite a newer one
  nearbyRun: 0,
  nearbyTimer: null,
  nearbyShown: "",
};

function addStep(step) {
  $("add-find").hidden = step !== "find";
  $("add-confirm").hidden = step !== "confirm";
  $("add-back").hidden = step !== "confirm" || adding.repairing !== null;
  $("add-save").hidden = step !== "confirm";
  $("add-error").textContent = "";
  adding.checkRun++;
  adding.nearbyRun++;
  clearTimeout(adding.nearbyTimer);
  if (step === "find") pollNearby();
}

function resetAdding(fields) {
  $("add-form").reset();
  $("add-checklist").replaceChildren();
  Object.assign(adding, { target: null, display: ":0", result: null, installedKey: false, ...fields });
}

function openAddDialog() {
  resetAdding({ repairing: null, nearbyShown: "" });
  $("add-title").textContent = "Add a machine";
  $("add-name-field").hidden = false;
  $("add-user").value = state.localUser;
  $("add-manual").open = false;
  $("add-nearby").replaceChildren();
  $("add-nearby-status").textContent = "";
  $("add-dialog").showModal();
  addStep("find");
}

/** Trust and install the key for a machine that's listed but can't log in yet. */
function openRepairDialog(name) {
  const host = state.hosts.find((h) => h.name === name);
  if (!host) return;
  resetAdding({
    repairing: name,
    target: { address: host.address, port: host.port, user: host.user },
    display: host.display,
  });
  $("add-title").textContent = `Finish setting up ${name}`;
  $("add-name-field").hidden = true;
  $("add-dialog").showModal();
  addStep("confirm");
  checkMachine();
}

async function pollNearby() {
  const run = adding.nearbyRun;
  let found;
  try {
    found = await api("/api/machines/nearby");
  } catch {
    found = { available: true, machines: [] };
  }
  if (run !== adding.nearbyRun || !$("add-dialog").open) return;
  renderNearby(found);
  // Without avahi on this computer, there's nothing to wait for.
  if (found.available) adding.nearbyTimer = setTimeout(pollNearby, NEARBY_EVERY_MS);
}

function renderNearby({ available, machines }) {
  if (!available) {
    $("add-nearby-status").textContent =
      "To list machines here automatically, install avahi-utils on this computer. Until then, enter the address setup.sh shows.";
    $("add-manual").open = true;
  } else {
    $("add-nearby-status").textContent = machines.length ? "" : "Looking for machines running setup.sh…";
  }
  // Rebuilding an unchanged list every few seconds would steal keyboard focus.
  const shown = JSON.stringify(machines);
  if (shown === adding.nearbyShown) return;
  adding.nearbyShown = shown;
  $("add-nearby").replaceChildren(...machines.map((machine) =>
    h("li", {},
      h("button", { type: "button", class: "nearby-item", onclick: () => pickNearby(machine) },
        h("span", { class: "led", "data-state": "on", "aria-hidden": "true" }),
        h("span", { class: "name" }, machine.name),
        h("span", { class: "where" }, describe(machine)),
        h("span", { class: "nearby-add" }, "Add")))));
}

function pickNearby(machine) {
  adding.target = { address: machine.address, port: machine.port, user: machine.user };
  adding.display = ":0";
  $("add-name").value = machine.name;
  addStep("confirm");
  checkMachine();
}

function continueManual() {
  const port = $("add-port").value.trim();
  const target = {
    address: $("add-address").value.trim(),
    port: port ? Number(port) : null,
    user: $("add-user").value.trim() || null,
  };
  if (!target.address) {
    $("add-error").textContent = "Enter the machine's address first.";
    $("add-address").focus();
    return;
  }
  adding.target = target;
  adding.display = $("add-display").value.trim() || ":0";
  // A hostname makes a good name for the machine; an IP address doesn't.
  const host = target.address.replace(/\.local$/, "");
  $("add-name").value = /^[\d.]+$|:/.test(host) ? "" : host.split(".")[0];
  addStep("confirm");
  checkMachine();
}

function knownHostsPattern({ address, port }) {
  return port && port !== 22 ? `[${address}]:${port}` : address;
}

async function checkMachine({ keepError = false } = {}) {
  const run = ++adding.checkRun;
  const target = adding.target;
  if (!keepError) $("add-error").textContent = "";
  $("add-where").textContent = describe(target);
  if (!adding.result) {
    $("add-checklist").replaceChildren(
      h("li", { class: "check" },
        h("span", { class: "led", "data-state": "wait", "aria-hidden": "true" }),
        h("div", { class: "check-title" }, `Checking ${describe(target)}…`)));
  }
  $("add-save").disabled = true;
  let result;
  try {
    result = await api("/api/machines/check", post(target));
  } catch (err) {
    if (run === adding.checkRun) {
      $("add-checklist").replaceChildren();
      $("add-error").textContent = err.message;
    }
    return;
  }
  if (run !== adding.checkRun) return;
  adding.result = result;
  renderChecklist(result);
}

function renderChecklist(r) {
  const item = (led, title, ...body) =>
    h("li", { class: "check" },
      h("span", { class: "led", "data-state": led, "aria-hidden": "true" }),
      h("div", {}, h("div", { class: "check-title" }, title), ...body));
  const p = (...children) => h("p", {}, ...children);
  const code = (text) => h("code", {}, text);
  const user = adding.target.user || state.localUser;
  const items = [];
  let ready = false;

  if (!r.reachable) {
    items.push(item("off", "Can't connect", p(r.error),
      h("button", { type: "button", class: "btn", onclick: () => checkMachine() }, "Check again")));
  } else if (r.identity === "changed") {
    items.push(item("off", "Identity doesn't match",
      p("This machine's key is different from the one you trusted before. If you reinstalled it, run ",
        code(`ssh-keygen -R '${knownHostsPattern(adding.target)}'`), " in a terminal and check again. ",
        "If you didn't, something may be impersonating it.")));
  } else {
    items.push(r.identity === "unknown"
      ? item("wait", "Confirm it's the right machine",
          p("setup.sh on that machine shows a fingerprint. It must match this one exactly:"),
          h("code", { class: "fingerprint" }, r.fingerprint),
          p("If it doesn't, cancel: something else is answering at this address."))
      : item("on", "Identity confirmed"));

    if (!r.have_key) {
      items.push(item("off", "No SSH key on this computer",
        p("Restart ssh-portal with ", code("./run.sh"), " in a terminal. It offers to create one.")));
    } else if (r.login === "ok") {
      items.push(item("on", "Logs in with your key"));
      if (!r.x11vnc) {
        items.push(item("off", "Screen sharing isn't installed",
          p("On that machine, run ", code("sudo apt install x11vnc"), ". Terminals and commands work without it.")));
      }
      ready = true;
    } else if (r.login === "error") {
      items.push(item("off", "Login failed", p(r.error)));
    } else {
      // With an unconfirmed identity nothing has been tried yet, so the key may already be there.
      items.push(item("wait", "Install your key",
        adding.installedKey
          ? p("Your key was copied, but logging in with it still fails. If it has a passphrase, run ",
              code("ssh-add"), " in a terminal, then try again.")
          : p(`${user}'s password on that machine. It's used once to copy your key there, and never stored.`,
              r.identity === "unknown" ? " Leave it empty if your key is already there." : ""),
        h("div", { class: "inline-form" },
          h("input", { type: "password", id: "add-password", autocomplete: "off", "aria-label": `Password for ${user}` }))));
      ready = true;
    }
  }

  // Don't lose a password typed while a check was still running.
  const typed = $("add-password")?.value ?? "";
  $("add-checklist").replaceChildren(...items);
  if (typed && $("add-password")) $("add-password").value = typed;
  const save = $("add-save");
  const matches = r.identity === "unknown" ? "It matches, " : "";
  save.textContent = adding.repairing
    ? (matches ? `${matches}finish setup` : "Finish setup")
    : (matches ? `${matches}add machine` : "Add machine");
  save.disabled = !ready;
  if (ready) {
    const empty = [$("add-password"), $("add-name-field").hidden ? null : $("add-name")].find((el) => el && !el.value);
    (empty ?? save).focus();
  }
}

/** Trust the machine, install your key if it isn't there yet, and save it. One click from the checklist. */
async function finishAdding(event) {
  event.preventDefault();
  if (!$("add-find").hidden) {
    continueManual(); // Enter in the address fields
    return;
  }
  const r = adding.result;
  const save = $("add-save");
  if (!r || save.disabled) return;

  const target = adding.target;
  const name = adding.repairing ?? $("add-name").value.trim();
  if (!adding.repairing) {
    const problem = !name
      ? "Give the machine a name."
      : state.hosts.some((host) => host.name === name) ? `There's already a machine called ${name}. Pick another name.` : "";
    if (problem) {
      $("add-error").textContent = problem;
      $("add-name").focus();
      return;
    }
  }
  const passwordInput = $("add-password");
  const password = passwordInput?.value ?? "";
  if (passwordInput) passwordInput.value = "";

  $("add-error").textContent = "";
  save.disabled = true;
  save.textContent = adding.repairing ? "Finishing…" : "Adding…";
  try {
    if (r.identity === "unknown") {
      await api("/api/machines/trust", post({ ...target, fingerprint: r.fingerprint }));
    }
    let now = await api("/api/machines/check", post(target));
    if (now.login === "needs_key" && password) {
      await api("/api/machines/install-key", post({ ...target, password }));
      adding.installedKey = true;
      now = await api("/api/machines/check", post(target));
    }
    if (now.login !== "ok") {
      adding.result = now;
      renderChecklist(now);
      if (now.login === "needs_key" && !password) {
        $("add-error").textContent = `Enter ${target.user || state.localUser}'s password, so ssh-portal can copy your key there.`;
      } else if (now.error) {
        $("add-error").textContent = now.error;
      }
      return;
    }

    if (adding.repairing) {
      $("add-dialog").close();
      refreshStatus(adding.repairing);
      return;
    }
    const host = await api("/api/machines", post({ name, ...target, display: adding.display }));
    $("add-dialog").close();
    state.hosts.push(host);
    state.picked.add(host.name);
    refreshMachines();
    refreshStatus(host.name);
  } catch (err) {
    $("add-error").textContent = err.message;
    // Trusting may have worked before the password didn't: show where things stand now.
    await checkMachine({ keepError: true });
  }
}

async function removeMachine(name) {
  const ok = confirm(`Remove ${name} from ssh-portal?\n\nThis only edits your machine list. Nothing changes on ${name} itself.`);
  if (!ok) return;
  try {
    await api(`/api/machines/${enc(name)}`, { method: "DELETE" });
  } catch (err) {
    alert(err.message);
    return;
  }
  closeTerminal(name);
  state.hosts = state.hosts.filter((h) => h.name !== name);
  state.picked.delete(name);
  delete state.status[name];
  show("screens");
  refreshMachines();
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
  $("focus-remove").addEventListener("click", () => state.focus && removeMachine(state.focus.name));
  $("add-open").addEventListener("click", openAddDialog);
  $("add-continue").addEventListener("click", continueManual);
  $("add-back").addEventListener("click", () => {
    Object.assign(adding, { target: null, result: null, installedKey: false });
    $("add-checklist").replaceChildren();
    addStep("find");
  });
  $("add-cancel").addEventListener("click", () => $("add-dialog").close());
  $("add-dialog").addEventListener("close", () => {
    adding.nearbyRun++;
    adding.checkRun++;
    clearTimeout(adding.nearbyTimer);
  });
  $("add-form").addEventListener("submit", finishAdding);
  $("run-form").addEventListener("submit", runCommand);
  $("run-command").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) $("run-form").requestSubmit();
  });

  const config = await api("/api/hosts");
  state.hosts = config.hosts;
  state.configPath = config.config_path;
  state.example = config.example;
  state.editable = config.editable;
  state.localUser = config.local_user;
  for (const host of state.hosts) state.picked.add(host.name);
  $("add-open").hidden = !state.editable;
  $("focus-remove").hidden = !state.editable;

  buildWall();
  buildRun();
  renderTerminals();
  renderFleet();
  show("screens");

  refreshAll();
  setInterval(refreshAll, STATUS_EVERY_MS);
}

start().catch((err) => {
  $("wall").replaceChildren(h("div", { class: "empty" }, h("h1", {}, "ssh-portal didn't load"), h("p", {}, err.message)));
});
