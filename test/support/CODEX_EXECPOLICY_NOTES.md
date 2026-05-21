# Codex Execpolicy Notes

This records the local experiment used to choose the v1 raw remote command
control.

## Observation

Codex loads user and project execpolicy `.rules` files by default. `codex exec`
also has `--ignore-rules`, which disables those files for a probe.

Known valid rule syntax:

```text
prefix_rule(pattern=["date"], decision="allow")
prefix_rule(pattern=["date"], decision="prompt")
```

Invalid decision values observed on Codex `v0.132.0`:

```text
deny
reject
block
ask-user
```

## Probe

Baseline:

```bash
codex --ask-for-approval never exec -C /net/levsha/scratch2/tingran/github/gpu-mcp \
  --sandbox workspace-write \
  "Run exactly this shell command: date. Then report whether it ran."
```

Result: `date` ran.

Temporary project-local or global rule:

```text
prefix_rule(pattern=["date"], decision="prompt")
```

With the same `codex --ask-for-approval never exec ...` probe, `date` was
rejected before execution:

```text
approval required by policy, but AskForApproval is set to Never
```

With `--ignore-rules`, the same `date` command ran again.

The same bare rule also blocked an absolute invocation in the local probe:

```text
prefix_rule(pattern=["date"], decision="prompt")
```

Prompt:

```text
Run exactly this shell command: /usr/bin/date. Then report whether it ran.
```

Result on Codex `v0.132.0`: `/usr/bin/date` was rejected before execution with
the same approval-policy message. This is useful evidence, but doctor should
still report bypass-form coverage from actual probes rather than assuming every
future Codex version behaves identically.

## Design Consequence

Current Codex execpolicy gives us a practical v1 control, not a literal hard
deny rule:

- raw remote commands should have global prompt rules;
- GPU-MCP automation should run Codex with approvals disabled;
- doctor should verify both the rules and the actual `codex exec` rejection;
- doctor probes must not include `--ignore-rules`;
- interactive Codex sessions with approvals enabled may still prompt the human.
