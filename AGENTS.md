# Agent Guidance

Project: evaluate an Azure AI Foundry toolbox at tool level and agent level. Read `README.md` and `docs\plan.md` first.

- Python 3.11+, standard library preferred; add dependencies to `requirements.txt` only when needed.
- Eval data is JSONL validated against `schemas\`. Use synthetic data only.
- Never commit secrets, endpoints with keys, or run output. Local config goes in `config\*.local.json`.
- Windows paths with backslashes.
