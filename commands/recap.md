---
description: Regenerate and open the local Claude Work Recap Dashboard
allowed-tools: [Bash]
---

Run the shared deterministic recap entry point:

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/recap.sh"
```

Relay whether the script opened the dashboard and always include the dashboard
path it prints. Do not separately rerun the generator or opener; the script
already owns that workflow.
