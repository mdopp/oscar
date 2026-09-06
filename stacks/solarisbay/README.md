# Solaris stack

End-to-end install for the household deployment of Solaris on a
ServiceBay full-stack host.

Bundles: `llama` + `solaris`. The merged `solaris` service (#271) is one
ServiceBay service / one tile holding the Solaris Engine (agent core + chat
UI), household glue + skills, voice bridge, and operator soul as separate
containers in one Pod; `llama` stays its own service (GPU/LLM engine) and runs
both model servers — the household chat model with its vision projector, and a
small second instance for embeddings. `ollama` was the third member until
solarisbay#1332 retired it.
Does NOT bundle `home-assistant` or `voice` — those are smart-home
infra that lives in ServiceBay's default registry. Enable both
registries side-by-side if you want the full household setup.

## Services

ServiceBay's stack installer reads `stack.yml`'s `templates:` list (this
checklist mirrors it). Both are selected by default:

- [x] llama
- [x] solaris

For step-by-step installation instructions including registry setup,
operator UX, and post-install checks, see the [top-level
README](../../README.md).
