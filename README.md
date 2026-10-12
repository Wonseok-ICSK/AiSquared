# AiSquared — Personal Project

**This is a personal project by Wonseok-ICSK, shared for learning, experimentation, and code review.**

AiSquared contains two local travel-planning applications: a Node.js-based flight-monitoring app called **Wayfinder**, and a separate Python/FastAPI-based **Travel Planner**. Both applications retain their original source layout. Much of the interface and the original documentation is in Korean.

## Applications

| Application | Location | Main technologies | Purpose |
| --- | --- | --- | --- |
| Wayfinder | Repository root | React, Vite, Express, Node.js SQLite | Flight-price monitoring, trip planning, and travel-service integrations |
| Travel Planner | `travel-planner/` | FastAPI, SQLAlchemy, APScheduler, React, TypeScript | Travel planning, scheduled flight checks, and optional AI and notification integrations |

These are independent applications with separate dependencies, configuration, and local databases.

## Repository guide

```text
src/                          Wayfinder React interface
server/                       Wayfinder API, providers, and scheduling
shared/                       Shared Wayfinder modules
scripts/                      Wayfinder development and configuration scripts
tests/                        Wayfinder tests and fixtures
public/                       Wayfinder static assets
README.original.md            Original Wayfinder documentation, preserved unchanged
travel-planner/
  backend/app/                FastAPI application and integrations
  backend/tests/              Backend tests
  backend/requirements.txt    Existing Python dependency specification
  frontend/                   React/TypeScript interface and frontend tests
  scripts/                    Windows development and launch scripts
  docs/                       Specification and research notes
  DESIGN.md                   Existing design document
```

## Run Wayfinder

Prerequisites: **Node.js 24 or newer** and npm. The application uses Node.js's built-in SQLite support. Microsoft Edge is used by the configured browser-based flight provider.

From the repository root:

```powershell
npm ci
Copy-Item .env.example .env
npm run dev
```

The default development interface is at `http://localhost:5173`; the API defaults to port `3001`. The example configuration enables demo seed data. Configure integrations before relying on live results; available providers and setup details are described in [the original README](README.original.md).

To build and serve the interface from the backend:

```powershell
npm run build
npm start
```

## Run Travel Planner

The supplied launch scripts target **Windows PowerShell** and a **Python 3.13** virtual environment. Node.js and npm are also required for the frontend.

Starting from the repository root:

```powershell
cd travel-planner
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
Copy-Item .env.example .env

cd frontend
npm ci
npm run build
cd ..

.\scripts\run.ps1 -NoBrowser
```

Open `http://127.0.0.1:8765`. Review `travel-planner/.env.example` for provider, scheduling, and notification settings. Optional integrations require their own credentials and setup. See the [specification](travel-planner/docs/SPEC.md), [design notes](travel-planner/DESIGN.md), and [research notes](travel-planner/docs/research/) for details.

Dependency manifests and lockfiles are preserved as supplied; their installation and compatibility have not been revalidated as part of this upload.

## Tests and review

Existing test suites are included for reviewers:

| Area | Command and working directory |
| --- | --- |
| Wayfinder unit tests | `npm test` from the repository root |
| Wayfinder UI tests | `npm run test:ui` from the repository root |
| Travel Planner backend | `..\.venv\Scripts\python.exe -m pytest` from `travel-planner/backend` |
| Travel Planner frontend | `npm test` from `travel-planner/frontend` |

Some browser or live-provider tests need additional setup or credentials. Consult the test configuration before enabling live tests. During import, Python syntax and JSON parsing were checked; the applications and full test suites were not run.

## Snapshot contents

This initial upload preserves the selected original files byte for byte. The original root README is retained as `README.original.md`; this English overview and a Git attributes file were added for sharing. No application code, existing comments, dependencies, or behavior were changed.

Source code, dependency manifests, tests, fixtures, design documents, static images, and the backend airport reference catalog are included. Installed packages, virtual environments, build output, runtime databases and logs, caches, generated screenshots, local environment files, and browser/session or credential storage are excluded. Runtime state is created locally when the applications run.

Keep personal configuration and credentials outside version control. This repository is intended for review and personal experimentation; the source documentation describes its local operating assumptions and limitations.
