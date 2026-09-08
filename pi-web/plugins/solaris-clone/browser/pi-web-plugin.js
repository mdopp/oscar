// "Repo klonen" — the browser half (#1395 slice B).
//
// The panel is the button; the action exists only for the cold start. PI WEB
// shows workspace panels inside a project, so on a box where no project exists
// yet there is nowhere for the panel to appear. The action creates that one
// project — `/workspace` itself, the folder clones land in — and from then on
// the panel is a tab like any other.

import { renderClonePanel } from "./clonePanel.js";
import { WORKSPACE_ROOT } from "./cloneRequest.js";

const plugin = {
  apiVersion: 2,
  name: "Repo klonen",
  activate: ({ html, svg }) => ({
    contributions: {
      actions: [
        {
          id: "clone.create-workspace-project",
          title: "Werkstatt für geklonte Repositories anlegen",
          description: `Legt ${WORKSPACE_ROOT} als Projekt an. Darin sitzt der Knopf „Repo klonen“.`,
          group: "Repo klonen",
          run: async (context) => {
            // The store keys projects by path and hands back the existing one,
            // so running this twice adds nothing and breaks nothing.
            await fetch("/api/projects", {
              method: "POST",
              headers: { "content-type": "application/json" },
              body: JSON.stringify({ path: WORKSPACE_ROOT, name: "Werkstatt", create: true }),
            });
            await context.refreshAppData();
          },
        },
      ],
      workspacePanels: [
        {
          id: "workspace.clone",
          title: "Repo klonen",
          icon: svg`
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M12 5v14"></path>
              <path d="M5 12h14"></path>
            </svg>
          `,
          order: 20,
          // Only in the workspace our own provider owns. Everywhere else the
          // backend that does the cloning is not reachable, and a tab that can
          // only apologise is worse than no tab.
          visible: (context) => context.workspace.provider?.metadata?.["clonesProjects"] === true,
          render: (context) => renderClonePanel(html, context),
        },
      ],
    },
  }),
};

export default plugin;
