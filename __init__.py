"""Cost Chart — unified Hermes plugin package.

The value of this package lives in the two halves Hermes wires automatically:

- ``dashboard/plugin_api.py`` — FastAPI ``router`` mounted at
  ``/api/plugins/costchart/`` (requires ``costchart`` in ``plugins.enabled``).
- ``desktop/plugin.js`` — the single-page daily chart in the Desktop app.

No agent-side tools or hooks are registered; ``register`` is a no-op so the
plugin manager loads the package without complaint.
"""


def register(ctx):
    return None
