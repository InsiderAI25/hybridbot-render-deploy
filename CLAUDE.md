# CLAUDE.md

## Project Overview

HybridBot Render Deploy is a minimal Flask web application deployed on [Render.com](https://render.com). It serves as a lightweight starter/template for Python web service deployment.

**Owner:** InsiderAI25 (sportsai@insidersportsai.com)

## Tech Stack

- **Language:** Python
- **Framework:** Flask
- **Server:** Gunicorn (production WSGI server)
- **Deployment:** Render.com (via Procfile)

## Repository Structure

```
hybridbot-render-deploy/
├── CLAUDE.md            # AI assistant guide (this file)
├── Procfile             # Render deployment config: `web: gunicorn render_main:app`
├── render_main.py       # Flask app entry point
└── requirements.txt     # Python dependencies (flask, gunicorn)
```

## Key Files

- **`render_main.py`** — Main application. Defines the Flask `app` object and routes. The app variable name `app` is referenced by Gunicorn in the Procfile — do not rename without updating both.
- **`Procfile`** — Tells Render how to start the service: `web: gunicorn render_main:app`
- **`requirements.txt`** — Dependencies are not version-pinned.

## Development

### Running Locally

```bash
pip install -r requirements.txt
python render_main.py
```

Flask dev server starts on `http://localhost:5000`.

### Production

Render runs `gunicorn render_main:app` (defined in Procfile). Render assigns the PORT automatically.

### No Build Step

Python is interpreted — no build/compile step required.

### No Tests or Linting

No test suite, linter, or formatter is configured.

## Deployment

Push to `main` branch triggers Render auto-deploy:
1. Render installs dependencies from `requirements.txt`
2. Runs command from `Procfile`
3. App is live

## Conventions

- **File naming:** snake_case for Python files
- **Flask app variable:** Must be named `app` in `render_main.py` (Gunicorn binding)
- **Minimal approach:** Only include what's necessary; avoid unnecessary complexity
- **No environment variables** are currently used in code

## Important Notes for AI Assistants

- This is a very small codebase (single Python file). Keep changes proportional.
- The `app` variable name in `render_main.py` is coupled to the Procfile — always update both if renaming.
- Dependencies in `requirements.txt` are unpinned. If adding dependencies, follow the existing convention unless asked to pin versions.
- No `.env` or secrets management exists. Do not commit secrets.
- No test infrastructure exists. If adding tests, use `pytest` as the standard Python choice.
