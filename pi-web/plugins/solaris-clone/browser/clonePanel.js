// The panel behind "Repo klonen". The plugin's shape lives in
// pi-web-plugin.js; this file is the part a person actually sees.
//
// Panel state is kept here rather than in a custom element: the host hands us
// its `html` tag and a `host.requestRender()`, which is all a form needs, and a
// registered element would be a second lifecycle to keep in step with it.

import { CLONE_OPERATION, describeCloneRequest } from "./cloneRequest.js";

const panelStates = new Map();

function stateFor(context) {
  const key = context.workspace.id;
  const existing = panelStates.get(key);
  if (existing !== undefined) return existing;
  const created = { url: "", busy: false, result: undefined };
  panelStates.set(key, created);
  return created;
}

async function addProject(path, name) {
  const response = await fetch("/api/projects", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ path, name }),
  });
  const body = await response.json().catch(() => undefined);
  if (!response.ok) {
    throw new Error(typeof body?.error === "string" ? body.error : `HTTP ${String(response.status)}`);
  }
  return body;
}

async function startClone(context, state) {
  const described = describeCloneRequest(state.url, context.workspace.path);
  if (!described.ok) {
    state.result = { kind: "error", message: described.message };
    context.host.requestRender();
    return;
  }
  if (context.machine.kind !== "local") {
    state.result = {
      kind: "error",
      message: "Dieser Knopf klont nur auf der Box selbst. Oben links wieder die lokale Maschine auswählen.",
    };
    context.host.requestRender();
    return;
  }
  if (context.backend === undefined) {
    state.result = {
      kind: "error",
      message:
        "Der Server-Teil dieses Plugins läuft gerade nicht, deshalb kann nichts geklont werden. Nach einem Update von PI WEB muss der Sitzungs-Dienst einmal neu starten; danach diese Seite neu laden.",
    };
    context.host.requestRender();
    return;
  }

  state.busy = true;
  state.result = undefined;
  context.host.requestRender();
  try {
    const answer = await context.backend.request(CLONE_OPERATION, { url: described.url });
    if (answer?.ok !== true) {
      state.result = {
        kind: "error",
        message:
          typeof answer?.message === "string"
            ? answer.message
            : "Das Klonen ist fehlgeschlagen, und der Grund kam nicht mit an.",
        detail: typeof answer?.detail === "string" ? answer.detail : undefined,
      };
      return;
    }
    await addProject(answer.path, answer.name);
    state.url = "";
    state.result = {
      kind: "ok",
      name: answer.name,
      path: answer.path,
      token: answer.token === true,
      tokenNote: typeof answer.tokenNote === "string" ? answer.tokenNote : undefined,
    };
  } catch (error) {
    state.result = {
      kind: "error",
      message: "Das hat unerwartet nicht geklappt. Die technische Meldung steht darunter.",
      detail: error instanceof Error ? error.message : String(error),
    };
  } finally {
    state.busy = false;
    context.host.requestRender();
  }
}

function renderResult(html, result) {
  if (result === undefined) return "";
  if (result.kind === "ok") {
    return html`
      <div class="solaris-clone-note solaris-clone-ok">
        <strong>Fertig.</strong>
        <p>„${result.name}“ ist geklont und als Projekt angelegt.</p>
        <p class="solaris-clone-path">${result.path}</p>
        <p>
          ${result.token
            ? "Das Projekt hat sein eigenes Leserecht für die Box bekommen."
            : `Geklont, aber ohne eigenes Leserecht für die Box${
                result.tokenNote === undefined ? "" : `: ${result.tokenNote}`
              }. Das lässt sich später im Terminal mit „pi-web-project add ${result.name}“ nachholen.`}
        </p>
        <button type="button" class="solaris-clone-button" @click=${() => window.location.reload()}>
          Projektliste aktualisieren
        </button>
      </div>
    `;
  }
  return html`
    <div class="solaris-clone-note solaris-clone-error">
      <strong>Das hat nicht geklappt.</strong>
      <p>${result.message}</p>
      ${result.detail === undefined ? "" : html`<pre class="solaris-clone-detail">${result.detail}</pre>`}
    </div>
  `;
}

export function renderClonePanel(html, context) {
  const state = stateFor(context);
  const typed = state.url.trim() !== "";
  const described = typed ? describeCloneRequest(state.url, context.workspace.path) : undefined;
  const ready = described?.ok === true && !state.busy;

  return html`
    <style>
      .solaris-clone { display: flex; flex-direction: column; gap: 0.75rem; padding: 1rem; max-width: 40rem; }
      .solaris-clone h2 { margin: 0; font-size: 1.1rem; }
      .solaris-clone p { margin: 0; line-height: 1.45; }
      .solaris-clone label { font-weight: 600; }
      .solaris-clone input {
        width: 100%; box-sizing: border-box; min-height: 2.75rem; padding: 0 0.6rem;
        font: inherit; color: inherit; background: transparent;
        border: 1px solid currentColor; border-radius: 6px; opacity: 0.95;
      }
      .solaris-clone-button {
        min-height: 2.75rem; padding: 0 1rem; font: inherit; font-weight: 600;
        color: inherit; background: transparent; border: 1px solid currentColor;
        border-radius: 6px; cursor: pointer;
      }
      .solaris-clone-button[disabled] { opacity: 0.45; cursor: default; }
      .solaris-clone-hint { opacity: 0.75; font-size: 0.9rem; }
      .solaris-clone-note { border-left: 3px solid currentColor; padding: 0.5rem 0 0.5rem 0.75rem; display: flex; flex-direction: column; gap: 0.5rem; }
      .solaris-clone-ok { border-left-color: #2e7d32; }
      .solaris-clone-error { border-left-color: #c62828; }
      .solaris-clone-path, .solaris-clone-detail { font-family: ui-monospace, monospace; font-size: 0.85rem; opacity: 0.8; }
      .solaris-clone-detail { white-space: pre-wrap; overflow-x: auto; margin: 0; }
      @media (min-width: 30rem) { .solaris-clone-actions { align-self: flex-start; } }
    </style>
    <section class="solaris-clone">
      <h2>Repo klonen</h2>
      <p>
        Adresse eines Repositories eintragen — es wird nach ${context.workspace.path} geholt und
        steht danach als eigenes Projekt in der Liste.
      </p>

      <label for="solaris-clone-url">Adresse des Repositories</label>
      <input
        id="solaris-clone-url"
        type="text"
        inputmode="url"
        autocapitalize="off"
        autocomplete="off"
        spellcheck="false"
        placeholder="https://github.com/benutzer/projekt.git"
        .value=${state.url}
        ?disabled=${state.busy}
        @input=${(event) => {
          state.url = event.target.value;
          state.result = undefined;
          context.host.requestRender();
        }}
        @keydown=${(event) => {
          if (event.key === "Enter" && ready) startClone(context, state);
        }}
      />

      <p class="solaris-clone-hint">
        ${!typed
          ? html`Erlaubt sind „https://…“, „http://…“, „ssh://…“ und „git@server:benutzer/projekt.git“.`
          : described.ok
            ? html`Wird geholt nach <span class="solaris-clone-path">${described.path}</span>`
            : described.message}
      </p>

      <div class="solaris-clone-actions">
        <button
          type="button"
          class="solaris-clone-button"
          ?disabled=${!ready}
          @click=${() => startClone(context, state)}
        >
          ${state.busy ? "Wird geklont …" : "Klonen"}
        </button>
      </div>

      ${state.busy
        ? html`<p class="solaris-clone-hint">
            Das kann bei einem großen Repository ein paar Minuten dauern. Die Seite darf offen bleiben.
          </p>`
        : renderResult(html, state.result)}
    </section>
  `;
}
