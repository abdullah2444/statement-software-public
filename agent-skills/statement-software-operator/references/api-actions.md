# API Actions

Use these workflows exactly. Replace placeholders with runtime values only; do not hard-code real names or secrets.

## Base Request Rules

- Base URL: `STATEMENT_BASE_URL`
- Auth header: `Authorization: Bearer <api_token>`
- JSON requests: `Content-Type: application/json`
- File uploads: `multipart/form-data`
- Treat non-2xx responses as failures and report the shortest useful reason.
- Do not call `/api/v1/parse-image` or `/api/v1/extract-text`.

## Resolve Client

1. `GET /api/v1/clients`
2. Match the user-provided client text against returned clients/groups.
3. Prefer exact case-insensitive match, then unique contains match.
4. If no unique match, ask: `Which client?`
5. After the user answers, resolve again.

## Add Statement Entry

Required payload:

```json
{
  "entry_date": "<YYYY-MM-DD>",
  "description": "<short description>",
  "currency": "CNY",
  "direction": "OUT",
  "amount": 0,
  "kind": "movement",
  "category_hint": "uncategorized"
}
```

Steps:

1. Resolve the client.
2. Extract entry fields from the message and image using agent vision/text reasoning.
3. Apply defaults from `SKILL.md`.
4. If an original image file is available, prefer one multipart call:
   - `POST /api/v1/clients/{client_id}/entries`
   - form fields are the payload keys
   - file field name is `image`
5. If multipart create is not practical, create JSON first, then attach:
   - `POST /api/v1/clients/{client_id}/entries`
   - `POST /api/v1/entries/{entry_id}/image` with file field `image`
6. Reply: `Added #<no>: <client>, CNY OUT <amount>, image attached.`

If the API response has no visible sequence number, use the returned entry id.

## Update Bank Balance

Use for user requests about bank/cash/account balance screenshots or text.

1. Extract account name, USD balance, and/or CNY balance.
2. `GET /api/v1/bank-balances`
3. Match existing account by exact case-insensitive name, then unique contains match.
4. If matched, preserve any currency not supplied by the user:
   - `PATCH /api/v1/bank-balances/{balance_id}`
   - body: `account_name`, `usd_balance`, `cny_balance`
5. If no match, create:
   - `POST /api/v1/bank-balances`
   - body: `account_name`, `usd_balance`, `cny_balance`
   - missing currency value defaults to `0`
6. Reply: `Updated <account>: CNY <amount>.`

If account name is missing, ask: `Which account?`

## Update Supplier Balance

Use for supplier/owed/payable balance screenshots or text.

1. Extract supplier name, currency, amount owed, and optional notes.
2. Currency defaults to `CNY`.
3. `GET /api/v1/supplier-balances`
4. Match existing supplier by exact case-insensitive name, then unique contains match.
5. If matched:
   - `PATCH /api/v1/supplier-balances/{supplier_id}`
   - body: `supplier_name`, `currency`, `amount_owed`, `notes`
6. If not matched:
   - `POST /api/v1/supplier-balances`
   - body: `supplier_name`, `currency`, `amount_owed`, `notes`
7. Reply: `Updated <account>: CNY <amount>.`

If supplier name is missing, ask: `Which supplier?`

## Send Statement Export

Use only API export endpoints. Do not generate files manually.

1. Resolve the client.
2. Choose format:
   - no format mentioned: PDF
   - "excel" or "xlsx": XLSX
   - "csv": CSV
3. Download:
   - PDF: `GET /api/v1/clients/{client_id}/export.pdf`
   - XLSX: `GET /api/v1/clients/{client_id}/export.xlsx`
   - CSV: `GET /api/v1/clients/{client_id}/export.csv`
4. Return the downloaded file as-is through the current agent channel.
5. Reply: `Sent <client> statement PDF.`

## Undo

For "undo", "reverse that", or "wrong":

1. `POST /api/v1/undo`
2. Reply: `Undone.`

If undo fails because there is nothing to undo, reply with the API error in one short sentence.
