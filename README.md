# Moloni console

Internal **retail operations** UI for [Moloni](https://www.moloni.pt/) (Portuguese invoicing / ERP): supplier invoices, bulk product updates (EAN, PVP, categories), suppliers, and a lazy-loaded category tree. The backend is a thin **FastAPI** proxy so Moloni credentials and tokens never reach the browser.

![Python](https://img.shields.io/badge/python-3.12+-blue.svg)
![Node](https://img.shields.io/badge/node-20+-green.svg)

## Features

- **Supplier invoices** — list, detail (with live product data), update document lines and header, bulk **push products** to Moloni (`products/update`), printable report, CSV for label workflows.
- **Products** — browse by category (Moloni `products/getAll`), PVP ↔ PV using product VAT.
- **Suppliers & categories** — list/edit; categories use **lazy tree** (`productCategories/getAll` per `parent_id`) so the UI stays fast.
- **Auth** — shared staff password + httpOnly session; optional secure cookie behind HTTPS.
- **Docker** — API + nginx static bundle; bind to `127.0.0.1` so your existing **host nginx** can add a new `server` without touching other sites.

## Quick start (local)

1. **Backend**

   ```bash
   cd backend
   cp .env.example .env   # fill Moloni + CONSOLE_PASSWORD + SESSION_SECRET
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
   ```

2. **Frontend**

   ```bash
   cd frontend
   npm install
   npm run dev
   ```

   Vite proxies `/api` → `http://127.0.0.1:8000`.

### Environment variables

| Variable | Purpose |
|----------|---------|
| `MOLONI_DEVELOPER_ID` / `MOLONI_CLIENT_ID` | Moloni panel **DEVELOPER_ID** (sent as `client_id` in `/grant`) |
| `MOLONI_CLIENT_KEY` / `MOLONI_CLIENT_SECRET` | Panel **CLIENT_KEY** (sent as `client_secret`) |
| `MOLONI_USERNAME`, `MOLONI_PASSWORD` | Moloni user (use a **plain** password in `.env`; URL-encoded passwords double-encode in HTTP clients) |
| `MOLONI_COMPANY_ID` | Company id for API calls |
| `CONSOLE_PASSWORD` | Staff login for this app |
| `SESSION_SECRET` | Cookie signing secret |
| `CORS_ORIGINS` | Comma-separated browser origins (must include your public URL in production) |
| `SESSION_COOKIE_SECURE` | Set `1` when served only over HTTPS |

See [backend/.env.example](backend/.env.example) for the full list.

## Docker + host nginx

From the **repository root** (where `docker-compose.yml` lives):

```bash
cp backend/.env.example .env   # edit: production CORS + SESSION_COOKIE_SECURE if HTTPS
docker compose build
docker compose up -d
```

Default: web UI on **`127.0.0.1:9080`**. Change with `MOLONI_CONSOLE_BIND=9144 docker compose up -d`.

Add a **new** nginx `server` (e.g. subdomain) that `proxy_pass`es to that port — example: [deploy/nginx-host-snippet.conf.example](deploy/nginx-host-snippet.conf.example).

## Moloni API

Uses Moloni **v1** JSON endpoints (e.g. [`productCategories/getAll`](https://www.moloni.pt/dev/products/product-categories/getall/), [supplier invoices](https://www.moloni.pt/dev/documents/supplier-invoices/)). Official index: [Moloni API endpoints](https://www.moloni.pt/dev/endpoints/).

## Security

- **Never commit** `.env` or real Moloni keys. This repo ships only `.env.example`.
- If keys were ever exposed, **rotate** them in the Moloni developer panel.
- `too_many_login_attempts` from Moloni means temporary lockout — fix credentials and wait before retrying.

## License

Private / internal use unless you add a license file.
