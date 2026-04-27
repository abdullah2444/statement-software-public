# Firefly Statement API

This API is for external tools and future mobile apps. It does not build a phone app by itself.

Base URL:

```text
http://your-server:18451/api/v1
```

## Authentication

There are two ways to call the API.

### Mobile-style login

Use this for real people using a future mobile app.

```bash
curl -c cookies.txt -X POST http://localhost:18451/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin123"}'
```

Then reuse the cookie file:

```bash
curl -b cookies.txt http://localhost:18451/api/v1/auth/me
```

If the response says `password_change_required`, call:

```bash
curl -b cookies.txt -X POST http://localhost:18451/api/v1/auth/change-password \
  -H "Content-Type: application/json" \
  -d '{"new_password":"new-strong-password","confirm_password":"new-strong-password"}'
```

Logout:

```bash
curl -b cookies.txt -X POST http://localhost:18451/api/v1/auth/logout
```

### API token

Use this for scripts, automations, or integrations.

Create tokens in the web UI: `API Tokens`.

Send the token in either header:

```bash
Authorization: Bearer ffs_your_token_here
```

or:

```bash
X-API-Key: ffs_your_token_here
```

## Access Levels

- `full_control`: can read and write almost everything.
- `read_only`: can view, search, and export, but cannot create, edit, or delete.
- `client_portal`: can only read one assigned client. This is groundwork for a future client-facing portal.

Existing old tokens are treated as `full_control` after upgrade so old integrations do not break.

## Common Responses

Success:

```json
{"ok": true}
```

Error:

```json
{"error": "Message explaining what went wrong"}
```

Common status codes:

- `400`: missing or invalid input.
- `401`: login or token is missing/invalid.
- `403`: the access level does not allow the action.
- `404`: the record does not exist.
- `409`: business conflict, such as duplicate names or commission already existing.
- `503`: external service unavailable, such as live FX rate.

## Main Endpoints

### Auth

- `POST /auth/login`
- `GET /auth/me`
- `POST /auth/logout`
- `POST /auth/change-password`

### Dashboard and Options

- `GET /dashboard`
- `GET /options`
- `GET /settings`
- `GET /exchange-rates`
- `GET /fx-rate?from=USD&to=CNY&amount=100`
- `POST /fx-refresh` full control/admin only

#### Dashboard numbers for mobile apps

Use `GET /dashboard` for the mobile home screen. The web dashboard and mobile dashboard must use the same accounting meaning.

Important response groups:

- `bank_totals`: cash currently in bank accounts.
- `supplier_totals`: money owed to suppliers.
- `stats`: all client statement balances.
- `company_status`: the final owner/company balance used by the web UI.
- `exchange_rate_summary.display_rate`: the USD/CNY rate used for approximate RMB display.

Do **not** calculate owner net RMB as only:

```text
bank approx RMB - supplier approx RMB
```

That misses the client statement balances.

The web UI owner/company balance is:

```text
company USD = bank_totals.total_usd - stats.total_usd_balance - supplier_totals.total_usd
company CNY = bank_totals.total_cny - stats.total_cny_balance - supplier_totals.total_cny
owner net RMB = company_status.balance_usd * exchange_rate_summary.display_rate
              + company_status.balance_cny
```

For mobile apps, prefer these API fields:

```text
company_status.balance_usd
company_status.balance_cny
exchange_rate_summary.display_rate
```

Then display:

```text
owner_overview.net_rmb = company_status.balance_usd * display_rate + company_status.balance_cny
owner_overview.cash_rmb = bank_totals.total_usd * display_rate + bank_totals.total_cny
owner_overview.clients_rmb = stats.total_usd_balance * display_rate + stats.total_cny_balance
owner_overview.suppliers_rmb = supplier_totals.total_usd * display_rate + supplier_totals.total_cny
```

Supplier balances are stored as positive owed amounts. The web UI displays them as negative exposure. If showing supplier RMB as an amount owed, display:

```text
supplier exposure RMB = -(supplier_totals.total_usd * display_rate + supplier_totals.total_cny)
```

If `exchange_rate_summary.display_rate` is missing, show USD and CNY separately instead of inventing a rate.

### Clients

- `GET /clients`
- `POST /clients`
- `GET /clients/{client_id}`
- `PATCH /clients/{client_id}`
- `DELETE /clients/{client_id}` admin/full control only
- `POST /clients/group`
- `POST /clients/{client_id}/ungroup`
- `POST /clients/{client_id}/ungroup-all`
- `GET /clients/{client_id}/export.csv`
- `GET /clients/{client_id}/export.xlsx`
- `GET /clients/{client_id}/export.pdf`

Client detail supports filters:

```text
GET /clients/1?q=taxi&currency=CNY&category=travel_expense&date_from=2026-04-01&date_to=2026-04-30&page=1&per_page=25
```

The response includes:

- `summary`: balances and totals.
- `entries`: full table-style rows.
- `mobile_cards`: compact rows for phone screens.
- `pagination`: page, per-page, total rows.

### Statement Entries

- `POST /clients/{client_id}/entries`
- `PATCH /entries/{entry_id}`
- `DELETE /entries/{entry_id}`
- `POST /entries/{entry_id}/image`
- `DELETE /entries/{entry_id}/image`
- `POST /entries/{entry_id}/commission`
- `POST /entries/link-transfer`
- `POST /clients/{client_id}/exchange`
- `POST /undo`

Create entry example:

```bash
curl -X POST http://localhost:18451/api/v1/clients/1/entries \
  -H "Authorization: Bearer ffs_your_token_here" \
  -H "Content-Type: application/json" \
  -d '{
    "entry_date": "2026-04-27",
    "description": "Client payment",
    "currency": "USD",
    "direction": "IN",
    "amount": 1200,
    "kind": "movement",
    "category_hint": "client_receipt"
  }'
```

Upload an image:

```bash
curl -X POST http://localhost:18451/api/v1/entries/123/image \
  -H "Authorization: Bearer ffs_your_token_here" \
  -F "image=@receipt.jpg"
```

### Bank and Supplier Balances

- `GET /bank-balances`
- `GET /bank-balances/{balance_id}`
- `POST /bank-balances`
- `PATCH /bank-balances/{balance_id}`
- `DELETE /bank-balances/{balance_id}`
- `GET /supplier-balances`
- `GET /supplier-balances/{supplier_id}`
- `POST /supplier-balances`
- `PATCH /supplier-balances/{supplier_id}`
- `DELETE /supplier-balances/{supplier_id}`

### Expenses

- `GET /expenses`
- `POST /expenses`
- `GET /expenses/{account_id}`
- `PATCH /expenses/{account_id}`
- `DELETE /expenses/{account_id}`
- `GET /expenses/{account_id}/balances`
- `POST /expenses/{account_id}/entries`
- `PATCH /expenses/entries/{entry_id}`
- `DELETE /expenses/entries/{entry_id}`
- `POST /expenses/{account_id}/undo`
- `GET /expenses/{account_id}/templates`
- `POST /expenses/{account_id}/templates`
- `PATCH /expenses/templates/{template_id}`
- `POST /expenses/templates/{template_id}/toggle`
- `DELETE /expenses/templates/{template_id}`
- `POST /expenses/{account_id}/import`
- `GET /expenses/{account_id}/export.csv`
- `GET /expenses/{account_id}/export.xlsx`
- `GET /expenses/{account_id}/export.pdf`

### Quick Submit

- `GET /quick-submits`
- `POST /quick-submits`
- `POST /quick-submits/{submit_id}/process`
- `DELETE /quick-submits/{submit_id}`

Quick submit upload:

```bash
curl -X POST http://localhost:18451/api/v1/quick-submits \
  -H "Authorization: Bearer ffs_your_token_here" \
  -F "client_id=1" \
  -F "description=Taxi receipt" \
  -F "amount=72" \
  -F "image=@receipt.jpg"
```

### AI Helpers

- `POST /parse-image`
- `POST /extract-text`

These require OpenRouter to be configured in Settings.

### Admin

Admin/full-control only:

- `GET /users`
- `POST /users`
- `PATCH /users/{user_id}`
- `POST /users/{user_id}/reset-password`
- `DELETE /users/{user_id}`
- `GET /tokens`
- `POST /tokens`
- `PATCH /tokens/{token_id}`
- `DELETE /tokens/{token_id}`
- `GET /audit-log`
  - Lists the recent activity log for API and web changes.
  - Each row includes `source`, `actor_type`, `actor_name`, `actor_role`, `api_token_id`, `api_token_name`, action, resource, details, and time.
  - `source: "api"` means an API token or API login made the change. `source: "web"` means a signed-in web user made it from the normal UI.

## Mobile App Notes

For a future mobile app:

- Use username/password login for staff.
- Store and send cookies securely after login.
- If login returns `must_change_password: true`, show a password change screen before normal app screens.
- Use `GET /dashboard` for the home screen.
- Use `GET /clients/{id}` with `mobile_cards` for phone statement screens.
- Use file uploads with `multipart/form-data` for receipt images.
