---
name: ctflow
description: >-
  Invoke CTflow workflows or single workers from the command line to run multi-perspective
  research, structured debate, and independent judgment DAGs. Use this skill when the user
  mentions ctflow, @<workflow_id>, or asks to evaluate a technical proposal or topic through
  a multi-agent DAG workflow.
---

# CTflow CLI Invocation Skill

CTflow orchestrates reusable Workers across a directed acyclic graph (DAG). Intermediate node transitions and token counts go to `stderr`, while only the terminal nodes' final Markdown conclusion goes to `stdout`. Configuration automatically resolves to `E:\CTflow\config\config.json` from any working directory.

## Commands

Run commands from any directory via `uv run --project E:\CTflow ctflow` (in PowerShell, quote `@...` tokens because `@` is PowerShell's splatting operator, or pass the workflow ID directly without `@`):

1. List available workflows or workers before choosing a topology:
   ```powershell
   uv run --project E:\CTflow ctflow list workflows
   uv run --project E:\CTflow ctflow list workers
   ```

2. Execute a saved workflow in one line and capture `stdout`:
   ```powershell
   uv run --project E:\CTflow ctflow "@review" "评估任务或议题描述"
   # Or without @ in PowerShell:
   uv run --project E:\CTflow ctflow review "评估任务或议题描述"
   ```
   Common built-in workflows include `review` (2→2→1 parallel review), `serial-111` (1→1→1 fact-analysis-audit), `debate-312` (3→1→2 opposing arguments), and `decision-3121` (3→1→2→1 full synthesis and judgment).

3. Execute a single configured Worker in one line:
   ```powershell
   uv run --project E:\CTflow ctflow "@@judge" "独立复核问题"
   ```

4. Inspect historical runs or restore configuration snapshots if needed:
   ```powershell
   uv run --project E:\CTflow ctflow list runs
   uv run --project E:\CTflow ctflow show <run_id>
   uv run --project E:\CTflow ctflow rollback
   ```

## Validation

Check the process exit code: `0` indicates all terminal nodes succeeded and `stdout` contains the final report; `1` indicates a node failed or was blocked (inspect `stderr` for the node error); `2` indicates a configuration or argument validation error.
