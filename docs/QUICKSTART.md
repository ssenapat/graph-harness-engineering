# Quickstart
1. `bin/ghe-init <target>` then `pip install pyyaml` in the target.
2. In Claude Code in the target: `/ghe-setup` (sets `validation.commands` and, for Jira, your site URL, project key and board in `ghe/config.yaml`; shipped values are placeholders).
3. Authenticate the Atlassian MCP (`/mcp`) if tracker is `jira`.
4. `/ghe-run "<GHE-123 | path/to/spec.md | free text>"`.
5. If the run pauses (WAITING_HUMAN), answer in the CLI or on the Jira ticket, then `/ghe-run --resume <run-id>`.
6. Cloud runs: provide the AWS region and credentials in the request or environment. Approve the plan at the `cloud-apply` gate.

Run state is in `ghe/runs/<run-id>/` (`state.json`, `graph.resolved.yaml`, `artifacts/`, `nodes/`). `RUN-REPORT.md` is written at the end.
