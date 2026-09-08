// The server half of "Repo klonen" (#1395 slice B).
//
// PI WEB's server plugin contract offers exactly one thing: a workspace
// provider (`probe`/`list`/`request`/`prepareRemove`). There is no route
// registration and no HTTP surface of our own, and `backend.request` reaches us
// only for a project this provider exclusively OWNS — so the clone button needs
// a project of ours to live in. That project is `/workspace` itself: the folder
// the clones land in. Everything *below* it is deliberately passed on, so each
// clone is claimed by PI WEB's bundled Git provider and keeps its worktree and
// diff features. We add a door; we take nothing away.
//
// Commands run through the host's `execFile`, which is argv-based and never
// goes through a shell — a typed address reaches git as one argument and can
// not become a second command.

import { stat } from "node:fs/promises";

import {
  CLONE_OPERATION,
  WORKSPACE_ROOT,
  describeCloneRequest,
  redactCredentials,
} from "./browser/cloneRequest.js";

// A first clone of a large repository over a home connection is minutes, not
// seconds; the token step is a single local HTTP call.
const CLONE_TIMEOUT_MS = 15 * 60 * 1000;
const PROJECT_TIMEOUT_MS = 30 * 1000;

const plugin = {
  apiVersion: 1,
  name: "Repo klonen",
  activate(context) {
    return { workspaceProvider: createCloneWorkspaceProvider(context) };
  },
};

export default plugin;

export function workspaceRoot(env = process.env) {
  const configured = env["PI_WEB_WORKSPACE"];
  return configured === undefined || configured === "" ? WORKSPACE_ROOT : configured;
}

export function createCloneWorkspaceProvider(context, root = workspaceRoot()) {
  return Object.freeze({
    async probe(project) {
      return project.path === root ? "claim" : "pass";
    },
    async list(project) {
      return [
        {
          key: project.path,
          path: project.path,
          label: "Werkstatt",
          isMain: true,
          publicMetadata: { workspaceRoot: root, clonesProjects: true },
        },
      ];
    },
    async request({ operation, input, signal }) {
      if (operation !== CLONE_OPERATION) {
        throw new Error(`Unbekannter Vorgang: ${operation}`);
      }
      return clone(context, root, input, signal);
    },
  });
}

async function clone(context, root, input, signal) {
  const url = input !== null && typeof input === "object" && !Array.isArray(input) ? input["url"] : input;
  const described = describeCloneRequest(url, root);
  if (!described.ok) return described;

  if (await pathExists(described.path)) {
    return {
      ok: false,
      message: `Unter ${root} liegt schon ein Ordner „${described.name}“. Es wurde nichts überschrieben und nichts gelöscht. Wenn das schon der richtige Klon ist, kann er über „Add a project“ als Projekt hinzugefügt werden; sonst den Ordner umbenennen und es noch einmal versuchen.`,
    };
  }

  const cloned = await context.execFile({
    file: "git",
    args: ["clone", "--", described.url, described.path],
    cwd: root,
    // Without this git waits forever for a username nobody can type into a
    // background process; with it, a missing credential fails fast and the
    // message below can say what to do about it.
    env: { GIT_TERMINAL_PROMPT: "0" },
    unsetEnv: ["GIT_ASKPASS", "SSH_ASKPASS"],
    timeoutMs: CLONE_TIMEOUT_MS,
    signal,
  });
  if (cloned.exitCode !== 0) {
    return { ok: false, ...cloneFailure(cloned) };
  }

  return {
    ok: true,
    name: described.name,
    path: described.path,
    ...(await grantProjectToken(context, root, described.name, signal)),
  };
}

/**
 * Give the fresh checkout its own read-only ServiceBay token (slice C).
 *
 * A failure here is reported, not rolled back: the clone succeeded, and this
 * repo's rule for `pi-web-project` is that nothing deletes a checkout.
 */
async function grantProjectToken(context, root, name, signal) {
  let result;
  try {
    result = await context.execFile({
      file: "pi-web-project",
      args: ["add", name],
      cwd: root,
      timeoutMs: PROJECT_TIMEOUT_MS,
      signal,
    });
  } catch (error) {
    return { token: false, tokenNote: shortDetail(error instanceof Error ? error.message : String(error)) };
  }
  if (result.exitCode === 0) return { token: true };
  return { token: false, tokenNote: shortDetail(`${result.stderr} ${result.stdout}`) };
}

export function cloneFailure(result) {
  const output = `${result.stderr} ${result.stdout}`;
  const detail = shortDetail(output);
  if (result.exitCode === null) {
    return {
      message:
        "Das Klonen wurde abgebrochen, weil es zu lange gedauert hat. Bei einem sehr großen Repository kann es helfen, es im Terminal zu klonen.",
      detail,
    };
  }
  if (/could not resolve host|name or service not known|temporary failure in name resolution/iu.test(output)) {
    return {
      message:
        "Der Server aus der Adresse ist nicht erreichbar. Bitte prüfen, ob der Name richtig geschrieben ist und ob die Box gerade ins Internet kommt.",
      detail,
    };
  }
  if (/terminal prompts disabled|authentication failed|could not read username|could not read password|invalid username or password/iu.test(output)) {
    return {
      message:
        "Der Server hat die Anmeldung abgelehnt. Für ein privates Repository muss im ServiceBay-Assistenten unter „PI_WEB_GIT_TOKEN“ ein gültiges Token stehen, das genau dieses Repository einschließt.",
      detail,
    };
  }
  if (/permission denied \(publickey|host key verification failed/iu.test(output)) {
    return {
      message:
        "Der Server hat den SSH-Zugang abgelehnt. Für dieses Repository stattdessen die „https://“-Adresse verwenden — dafür ist der hinterlegte Git-Token da.",
      detail,
    };
  }
  if (/repository not found|not found|does not exist|access denied/iu.test(output)) {
    return {
      message:
        "Unter dieser Adresse liegt kein Repository, auf das dieser Zugang sehen darf. Bitte die Adresse prüfen — und bei einem privaten Repository, ob der hinterlegte Git-Token es einschließt.",
      detail,
    };
  }
  return { message: "Das Klonen ist fehlgeschlagen. Die Meldung von Git steht darunter.", detail };
}

function shortDetail(output) {
  const text = redactCredentials(output).trim();
  return text.length > 400 ? `${text.slice(0, 400)}…` : text;
}

async function pathExists(path) {
  try {
    await stat(path);
    return true;
  } catch {
    return false;
  }
}
