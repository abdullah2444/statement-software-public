# Extraction Rules

Use the agent's own text and vision ability. Do not call software parser/OCR endpoints.

## Statement Entry Fields

- `client`: from the user message. If missing or ambiguous after API lookup, ask `Which client?`
- `amount`: required. Extract the transaction amount, not account balance totals. If missing, ask `Amount?`
- `currency`: use visible currency if clear; normalize `RMB` and yuan symbols to `CNY`; default `CNY`.
- `direction`: `IN` for received money, client payment received, deposit, refund received. `OUT` for paid, sent, transfer to someone, purchase, fee, supplier payment. Default `OUT`.
- `date`: visible transaction date; normalize to `YYYY-MM-DD`. If missing, use configured local date.
- `description`: concise one-line summary from merchant/payee/purpose. Include useful last-four card digits when visible.
- `category_hint`: map obvious meanings; otherwise `uncategorized`.
- `kind`: use `movement` unless the user explicitly asks for an exchange/transfer workflow.

## Balance Fields

For bank balance updates:

- Extract account name.
- Extract USD and/or CNY balances.
- Preserve existing API value for any currency not shown.
- Do not confuse transaction amounts with current account balance totals.

For supplier balances:

- Extract supplier name.
- Extract amount owed.
- Currency defaults to `CNY`.
- Notes are optional and should be short.

## Confidence Rules

Act automatically when the target client/account/supplier and amount are clear.

Ask only when a write would hit the wrong record or lacks an amount:

- `Which client?`
- `Which account?`
- `Which supplier?`
- `Amount?`

Do not ask the user to confirm extracted details just because the image is imperfect. The user can call undo.

## Image Handling

- If an image file/attachment is available, attach that same original image to the created statement entry.
- If the framework exposes only image pixels and not the original file, create the entry and state that the image could not be attached.
- Do not create a replacement image, screenshot, PDF, or OCR text file.
