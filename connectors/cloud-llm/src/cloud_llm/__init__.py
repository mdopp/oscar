"""OSCAR cloud-LLM connector.

Audited per-request escalation to Anthropic Claude or Google Gemini from
the otherwise-local OSCAR stack. Distinct from the deployment-wide cloud
mode (HERMES_API_KEY in oscar-brain) — this connector exists so a local
HERMES can occasionally consult a stronger cloud model when the Gemma-1B
router thinks the query needs it, with the audit trail mandated by the
architecture's privacy stance.

Spec: docs/connector-skeleton.md + oscar-architecture.md section 7.
"""

__version__ = "0.1.0"
