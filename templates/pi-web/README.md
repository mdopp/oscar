# pi-web — the coding-agent web UI on the box

[PI WEB](https://pi-web.dev/) (`@jmfederico/pi-web`) is a browser surface for the
[Pi Coding Agent](https://github.com/earendil-works/pi/tree/main/packages/coding-agent):
agent sessions that keep running in real repositories on this box after the
browser disconnects. This template deploys it at `pi.<publicDomain>` behind
Authelia and wires it to the box's own model server.

## What it is made of

| | |
|---|---|
| Image | `ghcr.io/mdopp/solaris-pi-web:latest`, built from `pi-web/Dockerfile` in this repo |
| Containers | `sessiond` (owns the sessions, terminals and the model runtime) + `web` (HTTP/WebSocket) |
| Network | isolated netns, `hostPort` 8504 |
| Route | `pi.<publicDomain>`, internal exposure, Authelia forward-auth `one_factor` |
| Model | llama-server on this box, via the Pi agent's `models.json` |
| Volumes | `{{DATA_DIR}}/pi-web/data` → `/data`, `{{DATA_DIR}}/pi-web/workspace` → `/workspace`, ServiceBays Agenten-Paket → `/opt/servicebay` (nur lesend) |

### Why the image is ours

Upstream publishes an **npm package only**. Its `docker/` directory is a
Dockerfile you are expected to build yourself, and no OCI image is pushed to
GHCR or Docker Hub. So this repo builds one, pinned to a specific
`@jmfederico/pi-web` version, in the same `build-images.yml` matrix as the
engine and the gatekeeper. Bumping PI WEB is a `PI_WEB_VERSION` change in
`pi-web/Dockerfile`, in its own commit — never a silent `latest` drift under a
running session.

### Why it is not on host networking

ADR 0007 Decision 2's carve-out list is **closed**; a new service does not join
it by arguing its case, and Decision 3 says explicitly that needing to reach a
loopback-bound sibling is not a reason either. So the pod runs in its own
network namespace, publishes 8504 as a `hostPort`, and addresses llama-server
as `http://host.containers.internal:11435/v1`. That path answers because the
sibling half already landed in #1344: llama-server binds `0.0.0.0` and
`LLAMA_PORT` carries `blockLanAccess: true`, so the LAN is refused at the host
firewall while loopback — where the pasta-proxied pod path arrives — is not.

`PI_WEB_PORT` carries the same `blockLanAccess: true` flag, for the same
reason one step further out: PI WEB has no login of its own (upstream states
plainly that it assumes trusted users and is not a sandbox), so a published
port reachable from the WLAN would be a way around Authelia. nginx reaches it
over loopback; a laptop on the WLAN does not.

The service never talks to `llama.<publicDomain>`. That route exists for a
human with a browser and is Authelia-gated; a service on the same box that used
it would meet a login page.

## How the model is configured

PI WEB has no LLM settings of its own — the model runtime belongs to the Pi
Coding Agent, and a self-hosted OpenAI-compatible endpoint is declared in the
agent directory's `models.json`. The post-deploy writes that file into
`{{DATA_DIR}}/pi-web/data/pi-agent/models.json`:

```json
{
  "providers": {
    "solaris-llama": {
      "baseUrl": "http://host.containers.internal:11435/v1",
      "api": "openai-completions",
      "apiKey": "llama",
      "compat": { "supportsDeveloperRole": false, "supportsReasoningEffort": false },
      "models": [{ "id": "qwen3.8-27b" }, { "id": "gemma-4-e4b" }]
    }
  }
}
```

Three details that are not obvious:

- **`api: "openai-completions"`, not Pi's built-in `llama.cpp` provider.** That
  built-in speaks to llama.cpp's *router* mode, which discovers models in a
  directory and loads them on demand. This box runs llama-server in
  single-model mode (`-m <weights>`), where the router endpoints do not exist
  but `/v1` does.
- **`apiKey` is a placeholder.** llama-server ships no authentication and there
  is no key to hold; Pi hides models whose provider has no auth configured at
  all, so a dummy value is what makes them appear. Upstream's own Ollama
  example does the same.
- **Both aliases are listed.** llama-server serves one model at a time: the
  coding alias answers while the lease is held from the model tile, the
  household alias otherwise. A list with only one of them would name a model
  that is absent for half the day.

## The model it answers on, and how Qwen is requested

The household model stays Gemma 4 E4B (#1318/#1325). Qwen 3.8 27B is what the
**coding lease** loads, and that lease is an endpoint of the Solaris Engine.
PI WEB does not ask for it: **Qwen über die Modell-Kachel anfordern; pi läuft
sonst auf dem Haushaltsmodell.** The tile is the model widget in Solaris
(#1374/#1381) — reachable from the phone's home screen, without a development
tool running — and it holds the window under its own holder.

So PI WEB simply talks to `http://host.containers.internal:11435/v1` and uses
whatever llama-server currently serves: the coding alias while somebody holds
the lease, the household alias otherwise. Both are listed in `models.json` for
exactly that reason; picking the one that is not loaded is a request llama-server
answers with the model it has.

### The retired lease unit (#1392)

Until v0.63 the post-deploy installed a host-side systemd unit
`pi-web-model-lease.service`, `BindsTo=pi-web.service`, that took the coding
lease whenever PI WEB started. That coupling is what forced PI WEB to stay
switched off (#1373) — a reboot, or ServiceBay's own start on every deploy,
would otherwise load Qwen, move voice onto the CPU and leave the household
assistant slow for up to four hours nobody asked for.

The upgrade retires it rather than deleting it quietly: the post-deploy stops
the unit, `disable`s it (which is what drops the `pi-web.service.wants` link —
a unit file removed while still enabled comes back with the next PI WEB start),
removes the unit file and the `{{DATA_DIR}}/pi-web/pi-web-lease.py` script copy,
and gives back a window still filed under holder `pi-web` — `GET` first, `DELETE`
only if it is ours, so the model tile's own window is never touched.

## Why it now runs around the clock

PI WEB runs like any other service on the box: `pi.<publicDomain>` answers
without anybody starting anything first. ServiceBay's kube-write path emits
`[Install] WantedBy=default.target` into every `.kube` unit it renders, which is
what Quadlet turns into the `default.target.wants` link, and the post-deploy no
longer strips it back out. A box upgraded from #1373 carries a `.kube` this
template *stripped*, and ServiceBay only rewrites that file when the rendered
spec changed — so the post-deploy adds the section back when it is missing,
reloads the generator, and starts the service. (`systemctl enable` is not the
tool for it: a Quadlet-generated unit cannot be enabled; the generator makes
the link from `[Install]` itself.)

The run-state log of #1373/#1377/#1378 is gone with the reason for it: nothing
has to remember whether the operator had PI WEB running, because the answer is
now always "yes".

## Git-Zugang für private Repositories

Eine PI-WEB-Sitzung ist eine Shell in einem Ordner unter `/workspace`. „Add a
project" zeigt auf einen Ordner, **der schon existiert** — geklont wird über den
Knopf „Repo klonen" (siehe unten) oder im Terminal, und beides braucht
Zugangsdaten im Container, sonst bleibt ein `git clone` eines privaten
Repositories an der Anmeldung hängen (#1395).

**Was der Betreiber einträgt** — im ServiceBay-Assistenten, einmal:

| Variable | Wert |
|---|---|
| `PI_WEB_GIT_TOKEN` | das Token (Typ `secret`, kein Vorgabewert, wird nicht erzeugt) |
| `PI_WEB_GIT_USER` | `x-access-token` (GitHub-Konvention; GitLab: `oauth2`) |
| `PI_WEB_GIT_HOST` | `github.com` |

**Ein GitHub-Token dafür anlegen:** GitHub → Settings → Developer settings →
Personal access tokens → **Fine-grained tokens** → *Generate new token*.
*Repository access* auf **Only select repositories** stellen und genau die
Repositories auswählen, in denen hier gearbeitet wird; unter *Repository
permissions* reicht **Contents: Read and write** — mehr braucht `git clone`,
`fetch` und `push` nicht. Eine Laufzeit setzen (90 Tage sind ein guter
Kompromiss) und den Wert direkt in den Assistenten kopieren; GitHub zeigt ihn
nur einmal.

**Wie das Token in den Container kommt.** Nicht über `sessiond` oder `web`,
sondern nur über einen eigenen Init-Container `pi-web-git-credentials`, der als
`USER node` läuft und daraus einmal pro Start schreibt:

- `/data/pi-web/git-credentials`, Modus **0600**, Eigentümer `node` — das
  Format des eingebauten Helfers `git credential-store`, eine Zeile
  `https://<user>:<token>@<host>`.
- `/data/home/.gitconfig` (`$HOME` im Image) mit
  `credential.helper = store --file=/data/pi-web/git-credentials` und
  `safe.directory` für `/workspace` und `*` — ohne das verweigert Git jeden
  Checkout, dessen Dateien einer anderen UID gehören.

Der Init-Container muss **nach** `pi-web-data-perms` laufen: dessen
`chmod -R a+rwX /data` würde eine 0600-Datei bei jedem Start wieder öffnen.

**Was das schützt — und was nicht.** Das Token steht in keinem Argument, in
keiner Remote-URL und in keiner Shell-History; es taucht in `ps` nicht auf und
wird nirgends ausgegeben. `sessiond` und `web` tragen die Variable *nicht*, eine
Sitzung findet sie also nicht in ihrer eigenen Umgebung. Sie kann die Datei
`/data/pi-web/git-credentials` aber lesen — sie läuft als `node`, und genau das
ist der Zweck der Datei; wer eine Sitzung hat, hat das Token. Und weil
ServiceBay keinen Secret-Mount kennt (jedes Secret dieses Repositories erreicht
seinen Pod als gerendertes `value:`, siehe `templates/solaris/template.yml`),
ist der Wert für `podman inspect` dieses einen Init-Containers sichtbar. Der
Zuschnitt ist deshalb das Token selbst: fine-grained, nur die Repositories, nur
`Contents`.

Ein geleertes `PI_WEB_GIT_TOKEN` entfernt die Datei beim nächsten Start wieder —
Widerrufen heißt also: Token auf GitHub löschen, Feld im Assistenten leeren,
neu ausrollen.

> Nach einem Upgrade sind die drei Variablen neu: ServiceBay übernimmt für neue
> Variablen keine Installations-Überschreibungen, das Feld muss im Assistenten
> einmal ausgefüllt werden.

## Ein ServiceBay-Token je Projekt

Damit der Agent in einer Sitzung diese Box *lesen* kann — Dienstliste, Logs,
gerenderte Service-Definitionen — bekommt jedes Projekt sein eigenes,
schreibgeschütztes ServiceBay-Token (#1395).

**Warum das hier anders aussieht als bei claude-dev.** Bei claude-dev trägt der
MCP-Eintrag des Projekts dessen `sb_`-Token, und dieser Eintrag *ist* der
Besitznachweis (servicebay#2680). **Pi kennt kein MCP** — upstream sagt das
ausdrücklich („It intentionally does not include built-in MCP … build CLI tools
with READMEs") — es gibt hier also keine MCP-Konfigurationsdatei, in die ein
Token gehören könnte. An ihre Stelle tritt ein kleines Kommando im Container:

```
pi-web-project add <Projekt>        # Token anlegen
pi-web-project get services         # damit lesen (ruft `servicebay` auf)
pi-web-project list                 # welche Projekte eins haben
pi-web-project remove <Projekt>     # Token widerrufen
```

`get` spricht seit #1398 keine ServiceBay-Route mehr selbst, sondern ruft das
mitgelieferte Agenten-CLI auf (siehe den nächsten Abschnitt); geblieben ist hier
nur die Frage, **mit wessen Token** gelesen wird.

**Die drei Regeln**, weil sie das Verhalten erklären, das sonst überrascht:

- **Der Eintrag ist der Besitznachweis.** Ein Projekt gehört uns genau dann,
  wenn `/data/servicebay/projects/<Name>.json` existiert und ein `sb_`-Token
  nennt. Dieser eine Datensatz ist gleichzeitig Kennzeichen und Zugangsdatum,
  Token und Eintrag können also nicht auseinanderlaufen.
- **Nichts wird übernommen, nur weil es da ist.** Es gibt keinen Abgleich beim
  Start, der für neu aufgetauchte Ordner Token anlegt, und keine Markierungsdatei
  zum Mitmachen. `add` tippt ein Mensch, einmal, pro Projekt — was von Hand
  geklont wurde, bleibt unangetastet, und `remove` weist es ab statt zu raten.
- **`remove` löscht keine Dateien.** Es widerruft das Token und entfernt den
  Eintrag; das Arbeitsverzeichnis bleibt liegen. Danach scheitert derselbe
  Lesezugriff mit **401**.

**Woher das Eltern-Token kommt.** Aus der Variablen `PI_WEB_SB_TOKEN` — **leer
lassen**: `mintApiToken` heißt, dass ServiceBay bei der Installation selbst
eines anlegt, nur mit Leserecht und ohne Ablauf, und bei einer erneuten
Installation dasselbe wiederverwendet. Kein Handgriff für den Betreiber. Ein
selbst eingetragener Wert gewinnt.

> Ausdrücklich **nicht** `SB_READ_TOKEN`: das widerruft und erneuert ServiceBay
> bei jedem Ausrollen, und Kind-Token werden mit ihrem Elternteil ungültig
> (servicebay#2049) — jeder Deploy hätte damit sämtliche Projekt-Token
> abgeräumt, ohne dass irgendwo etwas danach aussieht.

Der Wert erreicht nur den Init-Container `pi-web-sb-token`, der ihn nach
`/data/servicebay/parent-token` (Modus 0600, Eigentümer `node`) schreibt und
nebenbei die Modi der Projekt-Einträge wiederherstellt — `pi-web-data-perms`
öffnet mit `chmod -R a+rwX /data` bei jedem Start sonst auch die. `sessiond` und
`web` tragen die Variable nicht; in der Umgebung einer Sitzung steht nur
`SERVICEBAY_API_URL`, und das ist kein Geheimnis. Wie beim Git-Token gilt: wer
eine Sitzung hat, kann die Dateien unter `/data` lesen — der Zuschnitt ist
deshalb das Recht selbst, `read` und sonst nichts.

> Nach einem Upgrade sind `PI_WEB_SB_TOKEN` und `SERVICEBAY_API_URL` neue
> Variablen; ServiceBay übernimmt für neue Variablen keine
> Installations-Überschreibungen, der Assistent muss also einmal durchlaufen
> werden (das Token-Feld dabei leer lassen).

## Wissen und Fähigkeiten im Container

Ein Coding-Agent, der diese Box nicht kennt, erfindet ihre Regeln neu. ServiceBay
pflegt deshalb ein **Agenten-Paket** und liefert es auf die Box aus — den
Assist-Katalog (Architekturentscheidungen, Rezepte, Leitfäden, Fußangeln), ein
abhängigkeitsfreies Agenten-CLI und eine `AGENTS.md`
(servicebay#2906–#2909). Dieses Template hängt es nur noch ein und übersetzt es
in die Formen, die Pi wirklich liest (#1398 Scheibe A).

**Der Mount.** Drei Unterverzeichnisse des ausgelieferten Checkouts —
`agent-cli`, `agent-docs`, `assists` — landen **nur lesend** unter
`/opt/servicebay` in `sessiond`, `web` und `autoloop`. Bewusst die
Unterverzeichnisse und nicht die Wurzel: dort liegen auch ServiceBays eigene
Repository-Dateien, und dessen `CLAUDE.md` in einem Container, der an einem
*anderen* Projekt arbeitet, wäre eine zweite Anweisungsquelle, der das Modell
folgt. Der Pfad steht **fest** im Pod-Spec — `/mnt/data/servicebay/agent-kit/
checkout/{agent-cli,agent-docs,assists}` — und ist bewusst keine eigene
Installationsvariable: ServiceBay setzt den Standardwert einer *neu
hinzugekommenen* Variable beim Upgrade eines bestehenden Dienstes nicht ein
(servicebay#2913), der Mount-Pfad wäre dann leer gerendert und der Pod liefe in
eine Neustart-Schleife, während der Installationsauftrag `done` meldet (#1403).
Auch nicht `{{DATA_DIR}}` (ein erster Versuch in #1404): die Variable ist **pro
Dienst** skaliert — box-verifiziert als `/mnt/data/stacks/pi-web` für diesen
Pod, nicht das flache `/mnt/data`, das der Name nahelegt —, während dieser
Checkout ServiceBays eigenes Auslieferungsziel ist, dieselbe Kopie für jeden
Abnehmer gleich welchen Dienstes. Fährt die Box ein ServiceBay, das noch nichts
ausliefert, startet PI WEB trotzdem — der Mount ist dann leer, der
Init-Container schreibt genau eine Zeile ins Log und endet mit 0, und
`servicebay` sagt es beim Aufruf in einem Satz.

**Der Befehl `servicebay`.** Das CLI selbst ist ServiceBays; auf `$PATH` liegt
nur ein Aufruf davon, der zwei Dinge weiß, die das CLI nicht wissen kann: wo es
liegt, und **mit wessen Token** es läuft. Arbeitet die Sitzung in einem Projekt,
das `pi-web-project add` bekommen hat, ist es dessen eigenes Token; sonst das des
Pods. Übergeben wird immer nur der *Pfad* der Token-Datei
(`SERVICEBAY_MCP_TOKEN_FILE`) — das CLI hat aus gutem Grund keinen
`--token`-Schalter, denn `/proc/<pid>/cmdline` ist für alle lesbar.

```
servicebay services            # Dienstliste
servicebay logs solaris        # Unit- und Podman-Logs
servicebay assists --query backup
servicebay assist adr-0007-container-network-isolation-and-carveouts
```

**Assists als Pi-Skills.** Pi lädt Skills nach dem Agent-Skills-Standard: ein
Verzeichnis je Skill mit einer `SKILL.md`, deren Kopf `name` und `description`
nennt. Ein Assist hat stattdessen `title`/`whenToUse`/`kind`/`tags` — derselbe
Inhalt in anderer Form, also wird er **erzeugt** und nicht verlinkt. Der
Init-Container `pi-web-agent-kit` schreibt bei **jedem Start**
`/data/pi-agent/skills/servicebay/<id>/SKILL.md` aus dem Mount: `description` ist
Titel und `whenToUse` zusammen, denn genau die Zeile entscheidet, ob Pi den Skill
öffnet. Unveränderte Dateien werden nicht angefasst, zurückgezogene Assists
verschwinden — und ein **leerer** Mount lässt die vorhandenen Skills stehen,
statt aus ServiceBays Auslieferungsfehler hier einen zweiten, stillen zu machen.
So wirkt eine geänderte Architekturentscheidung ohne neue Version: ServiceBay
frischt den Checkout stündlich auf, der nächste Start übernimmt ihn.

**Die `AGENTS.md`, zweimal.** Global schreibt derselbe Init-Container
`/data/pi-agent/AGENTS.md` — ein kurzer Vorspann dieser Box (wo `/workspace`
liegt, dass `servicebay` auf dem `$PATH` steht, dass die Gates dem Projekt
gehören) und darunter unverändert die ausgelieferte Datei. Kein Symlink, weil Pi
keine Einbindung kennt: Vorspann und Handbuch müssen **eine** Datei sein. Je
Projekt legt `pi-web-project add` — und damit auch der Knopf „Repo klonen" —
einen fünfzeiligen Zeiger auf die globale Datei ab, **nur** wenn das Projekt
keine eigene `AGENTS.md`/`CLAUDE.md` mitbringt. Eine vorhandene wird nie
überschrieben: Pi nimmt den ersten Treffer im Verzeichnis, unsere Datei stünde
sonst vor den Konventionen des Projekts.

## Der Knopf „Repo klonen"

Ein Repository kommt auf die Box, ohne dass jemand ein Terminal öffnet: in PI WEB
im Projekt **Werkstatt** der Reiter **Repo klonen**, Adresse eintragen, klicken.
Der Klon landet unter `/workspace/<name>` und ist danach ein **eigenes Projekt**
in der Liste, in dem sich sofort eine Sitzung starten lässt (#1395).

**Beim ersten Mal**, solange es noch gar kein Projekt gibt: die Befehlspalette
öffnen und *„Werkstatt für geklonte Repositories anlegen"* ausführen. Das legt
`/workspace` selbst als Projekt an — mehr ist einmalig nicht zu tun, danach ist
der Reiter immer da.

**Was der Knopf annimmt und was nicht.** Erlaubt sind genau die Formen, die auch
claude-dev annimmt: `https://…`, `http://…`, `ssh://…` und
`git@server:benutzer/projekt.git`. Alles andere — `file://`, ein Pfad auf der
Box, ein Wort mit einem Bindestrich am Anfang — wird abgelehnt, mit einem Satz,
der sagt, was stattdessen dort hingehört. Der Ordnername wird aus dem letzten
Teil der Adresse abgeleitet (`.git` fällt weg) und muss dieselbe Namensregel
erfüllen wie bei `pi-web-project`, damit der Token-Schritt danach nicht an einem
Namen scheitert, den der Klon-Schritt noch durchgelassen hat.

**Es wird nie etwas überschrieben.** Liegt unter `/workspace` schon ein Ordner
dieses Namens, bricht der Knopf ab und sagt das — Git wird dafür gar nicht erst
gestartet. Nichts wird umbenannt und nichts gelöscht.

**Was ein Klick auslöst**, in dieser Reihenfolge: `git clone` mit den
hinterlegten Zugangsdaten (Scheibe A), dann `pi-web-project add <name>` für das
eigene Leserecht des Projekts (Scheibe C), dann `POST /api/projects` beim
Wirt, damit der Klon in der Projektliste steht. Schlägt der Token-Schritt fehl,
gilt der Klon trotzdem als erfolgreich: der Ordner ist da, und die Meldung sagt,
wie sich das Leserecht nachholen lässt. Ein Fehlschlag beim Klonen selbst wird
in Klartext übersetzt — abgelehnte Anmeldung, unbekannter Server, kein
Repository unter der Adresse — mit Gits eigener Meldung darunter, aus der
Zugangsdaten herausgestrichen sind.

### Warum das ein Plugin ist und keine zweite Oberfläche

PI WEB bringt seine Projektverwaltung mit; daneben eine eigene
Konfigurationsseite zu stellen wie bei claude-dev war die ausdrückliche
Entscheidung *dagegen* (mdopp, 2026-09-08). Upstream sieht für genau diesen Fall
Plugins vor, und der eingebaute Git-Teil benutzt dieselben öffentlichen
Verträge — wir hängen uns also nicht an, sondern benutzen die vorgesehene Tür.

Der Vertrag bestimmt dabei den Zuschnitt: Ein Server-Plugin darf **nur** einen
Workspace-Provider beitragen, und sein Rückkanal `backend.request` ist nur in
einem Projekt erreichbar, das dieser Provider **exklusiv besitzt**. Deshalb ist
`/workspace` selbst das Projekt „Werkstatt", in dem der Knopf sitzt — und
deshalb beansprucht unser Provider ausdrücklich **nichts** darunter: die Klone
gehören dem eingebauten Git-Plugin, mit Worktrees und Diff-Ansicht. Der Knopf
fügt eine Tür hinzu und nimmt nichts weg. Kommandos laufen über das
`execFile` des Wirts, das argumentbasiert ist und **keine Shell** benutzt — eine
eingetippte Adresse kann also kein zweites Kommando werden.

### Wie das Plugin installiert wird

Es liegt im **Image** (`pi-web/plugins/solaris-clone`, kopiert nach
`/opt/solaris/pi-web-plugins`), nicht im Asset-Baum des Templates: Es ist gegen
die Plugin-API der in `pi-web/Dockerfile` angehefteten PI-WEB-Version
geschrieben, die beiden gehören zusammen und werden zusammen angehoben.

Der Init-Container `pi-web-plugins` kopiert es bei jedem Start nach
`/data/pi-web/plugins/` — das ist die lokale Plugin-Quelle, die PI WEB von sich
aus durchsucht. Ein dort gefundenes Plugin ist **standardmäßig aktiv**;
*Settings → PI WEB plugins* braucht man nur zum **Ab**schalten, nicht zum
Freischalten. Es ist also nichts zu klicken, damit der Reiter nach dem Ausrollen
da ist.

> **Der Server-Teil wird beim Start von `sessiond` aktiviert** — eine Änderung
> daran braucht also einen Neustart des Sitzungs-Dienstes, und PI WEB weist eine
> Anfrage an eine veraltete Fassung mit *„reload after the session daemon
> restarts"* ab. Beim Ausrollen über ServiceBay passiert das von selbst, weil der
> Pod ohnehin neu startet. Nur wer die Dateien unter `/data` von Hand ändert,
> muss `sessiond` selbst neu starten; für die Browser-Hälfte allein genügt ein
> Neuladen der Seite.

## Pi-Autoloop

Neben der Weboberfläche läuft im selben Pod ein dritter Prozess, `autoloop`. Er
holt sich Tickets von GitHub und lässt Pi sie kopflos abarbeiten.
Arbeitsteilung: **Claude schneidet zu, Pi baut ab.**

**Ein Ticket freigeben.** Auf dem Ticket das Label **`pi:ready`** setzen — mehr
nicht. Kein Label, kein Zugriff: der Loop sieht ausschließlich offene Tickets
mit diesem Label, und ausschließlich in den Repositories, die in
`PI_AUTOLOOP_REPOS` stehen. Was dort nicht steht, wird nicht einmal geklont.

**Was dann passiert.** Höchstens ein Ticket je Runde (Standard: alle fünf
Minuten nachsehen):

1. Der Loop legt die Sperre `refs/autoloop/claim/<Nummer>` im Repository an.
   Wer sie zuerst anlegt, arbeitet; jeder zweite Loop bekommt von GitHub
   **HTTP 422 „Reference already exists"** und lässt das Ticket in Ruhe. Es ist
   dieselbe Sperre, die die Claude-Seite benutzt — deshalb greifen die beiden
   nie nach demselben Ticket.
2. Das Repository wird nach `/workspace/autoloop/<besitzer>/<repo>/<nummer>`
   geklont.
3. `pi --mode json` bekommt das Ticket als Auftrag, mit dem Modell, das
   llama-server **gerade** geladen hat. Der Loop fordert **keine** GPU an; Qwen
   kommt weiterhin über die Modell-Kachel in Solaris.
4. Danach laufen die Prüfungen des Zielrepositories — was es selbst mitbringt
   (`ruff`, `pytest`, `npm run lint`, `npm test`). Ein Werkzeug, das dieser
   Container nicht hat, steht im Protokoll als *übersprungen* und gilt nie als
   bestanden.
5. Commit auf den Zweig `pi/<nummer>-<kurztitel>`, Push, Pull Request mit
   `Refs #<nummer>`.

**Zusammengeführt wird nie etwas.** Der Loop öffnet den PR und hört auf.
Zusammenführen entscheidet ein Mensch oder die Claude-Seite. Ist das Gate rot
oder das Zeitlimit (Standard eine Stunde) erreicht, wird der PR als **Entwurf**
geöffnet — abgebrochen, aber sichtbar, und **ohne Nachbesserungsschleife**.

**Wo das Protokoll steht.** Als Kommentar am Pull Request und im Container unter
`/data/pi-web/autoloop/<besitzer>-<repo>-<nummer>.log` (auf der Box unter
`<DATA_DIR>/pi-web/data/pi-web/autoloop/`). Es nennt Modell, Dauer, Zweig, PR,
das Ergebnis jedes Gates und eine Zusammenfassung dessen, was Pi getan hat.

**Noch einmal arbeiten lassen.** Am Ende gibt der Loop die Sperre wieder frei.
Dass ein Ticket trotzdem nicht sofort erneut angefasst wird, liegt am
gepushten Zweig: solange `pi/<nummer>-…` im Repository steht, überspringt der
Loop das Ticket. Wer eine zweite Runde will, löscht diesen Zweig.

**Anschalten.** Standardmäßig ist der Loop **aus** (`PI_AUTOLOOP_ENABLED` =
`false`) und schreibt das einmal je Runde in sein Log. Vor dem Anschalten muss
`PI_WEB_GIT_TOKEN` drei Berechtigungen haben: `Issues: Read`,
`Pull requests: Read and write`, `Contents: Read and write`. Der Token steht
nirgends in einer Umgebungsvariable — der Loop liest ihn aus derselben
0600-Datei, aus der auch `git` ihn nimmt, und startet Pi ohne ihn.

## Not in the `solarisbay` stack

The stack is the household assistant — the model server plus the Solaris
service. PI WEB is a developer tool that happens to live on the same box, like
`paperless`, so it installs on its own.

## Verifying on the box

- `https://pi.<publicDomain>/` unauthenticated → **302** to Authelia; after
  login the UI loads over WebSocket.
- The model picker lists the alias llama-server currently reports
  (`curl http://127.0.0.1:11435/v1/models`).
- `systemctl --user status pi-web-model-lease` reports **not-found** and
  `grep -c Install ~/.config/containers/systemd/pi-web.kube` is 1 — the
  service is up now and comes back after a reboot.
- `curl -s http://127.0.0.1:8787/api/model-lease` names no holder `pi-web`
  until somebody takes the lease from the model tile.
- From another LAN device, `curl -m 3 http://<box>:8504/` and
  `http://<box>:11435/v1/models` must both fail — the `blockLanAccess` rules.
- In einer Sitzung im Projektordner: `pi-web-project add <Projekt>` meldet eine
  Token-Kennung, `pi-web-project get services` beantwortet die Dienstliste, und
  nach `pi-web-project remove <Projekt>` scheitert derselbe Aufruf mit **401** —
  das Arbeitsverzeichnis liegt danach noch da. `ls -l
  /data/servicebay/projects/` zeigt `-rw------- node`.
- Agenten-Paket (setzt ServiceBay ≥ 5.34.0 auf der Box voraus): `ls
  /opt/servicebay/{agent-cli,agent-docs,assists}` ist gefüllt, und ein Schreiben
  dorthin scheitert mit „Read-only file system".
- `servicebay services --json` liefert im Projektordner die Dienstliste und
  `servicebay assist adr-0007-container-network-isolation-and-carveouts` den
  Text der Entscheidung. `ps auxww | grep servicebay` zeigt kein Token.
- `ls /data/pi-agent/skills/servicebay | wc -l` nennt so viele Skills wie
  `ls /opt/servicebay/assists/*.md | wc -l`, und `head -4
  /data/pi-agent/AGENTS.md` zeigt den Vorspann dieser Box.
- Der Startkopf einer neuen Sitzung nennt die geladene `AGENTS.md` und die
  Skills; `/skill:adr-0007-container-network-isolation-and-carveouts` öffnet den
  Text in der Sitzung.
- Ein frisch geklontes Projekt hat eine `AGENTS.md` mit dem Zeiger; ein Klon
  eines Repositories, das eine eigene mitbringt, hat unverändert dessen Fassung.
- `ls /data/pi-web/plugins/solaris-clone` zeigt das Plugin, und unter
  *Settings → PI WEB plugins* steht „solaris-clone" als aktiv — ohne dass jemand
  es eingeschaltet hat.
- Befehlspalette → *„Werkstatt für geklonte Repositories anlegen"*: `/workspace`
  taucht als Projekt auf und hat den Reiter **Repo klonen**.
- Dort eine öffentliche Repo-Adresse eintragen und klicken: der Klon liegt unter
  `/workspace/<name>`, steht nach dem Aktualisieren als eigenes Projekt in der
  Liste, und `pi-web-project list` nennt für ihn eine Token-Kennung.
- Derselbe Klick ein zweites Mal meldet, dass der Ordner schon existiert — und
  `ls -la /workspace/<name>` zeigt denselben Stand wie vorher.
- Eine abgelehnte Adresse (`file:///etc/passwd`) meldet einen Satz auf Deutsch,
  und im Log von `sessiond` steht kein `git`-Aufruf dazu.
- In einer Sitzung: `git clone https://github.com/<privates Repo>.git` läuft ohne
  Rückfrage durch, `git -C /workspace/<name> fetch` ebenso. `ls -l
  /data/pi-web/git-credentials` zeigt `-rw------- node`, und
  `grep -r <Token-Präfix> ~/.bash_history` sowie `ps auxww` finden nichts.
- Autoloop aus (Standard): `podman logs pi-web-autoloop` meldet je Runde
  „switched off", und unter `/workspace/autoloop` liegt nichts.
- Autoloop an: ein Wegwerf-Ticket mit `pi:ready` markieren. Das Log nennt Klon,
  Modell und Gates, unter `/data/pi-web/autoloop/` liegt ein Protokoll, und im
  Repository steht ein PR mit `Refs #<nummer>` — nicht zusammengeführt.
- Sperre: während der Lauf läuft, liefert
  `gh api repos/<repo>/git/matching-refs/autoloop/claim` den Ref des Tickets,
  und ein zweiter Anlauf auf dasselbe Ticket
  (`gh api --method POST repos/<repo>/git/refs -f ref=refs/autoloop/claim/<n>
  -f sha=$(git rev-parse origin/main)`) scheitert mit **422 „Reference already
  exists"**. Nach dem Lauf ist der Ref wieder weg.
- `podman exec pi-web-autoloop env | grep -i token` findet nichts.
