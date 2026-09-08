// Shared by both halves of this plugin on purpose.
//
// The browser half validates to answer the person while they type; the server
// half validates again because a backend never trusts its caller. Two copies of
// these rules would drift, and the drift would show up as a button that accepts
// an address the clone then refuses — so there is one copy, and it lives under
// `browser/` because that is the only directory PI WEB serves to the page (the
// server module may reach into it, the browser may not reach out of it).

/** The clone home. Matches the `/workspace` volume in templates/pi-web/template.yml. */
export const WORKSPACE_ROOT = "/workspace";

export const CLONE_OPERATION = "clone";

// Exactly the forms claude-dev accepts (servicebay#2674); everything else is
// refused rather than repaired. `file://` and a bare path are not oversights: a
// "remote" that is a local path turns a clone button into a file-copy tool.
const URL_SCHEMES = ["https://", "http://", "ssh://"];
const SCP_LIKE = /^[A-Za-z0-9._-]+@([A-Za-z0-9._-]+):(.+)$/u;

// The same name rule `pi-web-project` enforces (pi-web/pi_web_project.py), so a
// name this accepts is never one the project's token step then rejects.
const PROJECT_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]*$/u;
const MAX_NAME_LENGTH = 64;

const UNPRINTABLE = /[\s\u0000-\u001F\u007F]/u;

/** Strip `user:secret@` out of anything on its way to a screen or a log. */
export function redactCredentials(text) {
  return String(text ?? "").replace(/([A-Za-z][A-Za-z0-9+.-]*:\/\/)[^/\s@]*@/gu, "$1***@");
}

function refuse(message) {
  return { ok: false, message };
}

export function validateRemoteUrl(raw) {
  const url = String(raw ?? "").trim();
  if (url === "") {
    return refuse("Bitte die Adresse des Repositories eintragen.");
  }
  if (UNPRINTABLE.test(url)) {
    return refuse(
      "Die Adresse enthält ein Leerzeichen oder ein unsichtbares Zeichen. Bitte sie noch einmal aus dem Repository kopieren.",
    );
  }
  // A leading dash would reach git as an option rather than as an address.
  if (url.startsWith("-")) {
    return refuse("Eine Adresse fängt nicht mit einem Bindestrich an. Bitte sie noch einmal kopieren.");
  }
  const scheme = URL_SCHEMES.find((candidate) => url.startsWith(candidate));
  if (scheme !== undefined) {
    const rest = url.slice(scheme.length);
    const slash = rest.indexOf("/");
    if (slash <= 0 || rest.slice(slash + 1).replace(/\/+$/u, "") === "") {
      return refuse(
        "In der Adresse fehlt der Teil hinter dem Servernamen, zum Beispiel „…/benutzer/projekt.git“.",
      );
    }
    return { ok: true, url, path: rest.slice(slash + 1) };
  }
  const scp = SCP_LIKE.exec(url);
  if (scp !== null) {
    if (scp[2].replace(/\/+$/u, "") === "") {
      return refuse(
        "In der Adresse fehlt der Teil hinter dem Doppelpunkt, zum Beispiel „git@github.com:benutzer/projekt.git“.",
      );
    }
    return { ok: true, url, path: scp[2] };
  }
  return refuse(
    "Diese Adresse versteht PI WEB nicht. Sie muss mit „https://“, „http://“ oder „ssh://“ anfangen oder die Form „git@github.com:benutzer/projekt.git“ haben.",
  );
}

export function deriveProjectName(path) {
  const segments = String(path ?? "")
    .split("/")
    .filter((segment) => segment !== "");
  const name = (segments.at(-1) ?? "").replace(/\.git$/u, "");
  if (name === "") {
    return refuse(
      "Aus dieser Adresse lässt sich kein Ordnername ableiten. Bitte die Adresse bis zum Namen des Repositories angeben.",
    );
  }
  if (name.length > MAX_NAME_LENGTH) {
    return refuse(`Der Name „${name}“ ist zu lang — erlaubt sind höchstens ${String(MAX_NAME_LENGTH)} Zeichen.`);
  }
  if (!PROJECT_NAME.test(name)) {
    return refuse(
      `Aus der Adresse ergibt sich der Ordnername „${name}“, und der enthält Zeichen, die hier nicht erlaubt sind. Erlaubt sind Buchstaben, Ziffern, „.“, „_“ und „-“.`,
    );
  }
  return { ok: true, name };
}

/**
 * One answer for one typed address: the address to clone, the folder it lands
 * in, or the single sentence to show the person instead.
 */
export function describeCloneRequest(raw, workspaceRoot = WORKSPACE_ROOT) {
  const remote = validateRemoteUrl(raw);
  if (!remote.ok) return remote;
  const derived = deriveProjectName(remote.path);
  if (!derived.ok) return derived;
  return {
    ok: true,
    url: remote.url,
    name: derived.name,
    path: `${workspaceRoot.replace(/\/+$/u, "")}/${derived.name}`,
  };
}
