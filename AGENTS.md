# Agent Guidance

Project: evaluate a Microsoft Foundry toolbox at tool level (tool tests: direct MCP calls) and agent level (agent evals: an
agent connected to the toolbox). Read `README.md` and `docs\plan.md` first.

- Python 3.11+ in `.venv`: `.\.venv\Scripts\pip install -r requirements.txt` (pinned; also installs this repo in editable mode).
  Add dependencies only when needed, to both `requirements.txt` (pinned) and `pyproject.toml` (ranges). Never add
  `azure-ai-projects`: it needs `openai>=3`, which conflicts with assert-ai.
- Tests: `.\.venv\Scripts\python -m unittest discover -s tests`. They run offline and must not depend on Azure or `.env`.
- Eval data is JSONL validated against `schemas\` plus the rules in `evals\cases.py` (`scripts\validate_eval_jsonl.py`).
  Use synthetic data only.
- Never commit secrets, tenant-specific values or run output. Local values go in the git-ignored `.env`,
  `config\*.local.json` and `deploy\resources.local.md`.
- Windows paths with backslashes.
