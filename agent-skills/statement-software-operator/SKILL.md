---
name: statement-software-operator
description: "Use when operating a generic statement software API from user text or image input: add statement entries, attach sent images, update bank or supplier balances, undo API writes, and return statement exports. The skill is API-only, avoids built-in parser/OCR endpoints, uses placeholder configuration, and replies minimally."
metadata:
  short-description: Operate statement software through its API
---

# Statement Software Operator

Use this skill to operate the statement software through its API. It is for user requests such as:

- "add this receipt to <client>"
- "update my bank balance from this screenshot"
- "send <client> statement"
- "send <client> statement excel"
- "undo"

## Hard Rules

- Use only the statement software API for software changes and exports.
- Never call built-in parser/OCR endpoints such as `/api/v1/parse-image` or `/api/v1/extract-text`.
- Never create PDF, Excel, or CSV statement files from scratch. Download exports from the API.
- Do not include real client, company, account, server, person, domain, IP, token, or password values in skill files or examples.
- Use placeholders only: `<client>`, `<account>`, `<base_url>`, `<api_token>`, `<file>`, `<entry_id>`.
- If the user sent an image and an image file/attachment is available, attach that original image to the entry.
- Do not ask for confirmation before writes when the target and amount are clear.
- Ask only short blocking questions. The common one is: `Which client?`

## Configuration

Read configuration from the environment unless the current agent framework provides a safer secret store:

- `STATEMENT_BASE_URL`: base URL without trailing slash, for example `<base_url>`.
- `STATEMENT_API_TOKEN`: API token with write access.
- `STATEMENT_TIMEZONE`: optional local timezone label; default to the user's current/local timezone.

For configuration details, read `references/configuration.md`.

## Default Behavior

- Statement currency defaults to `CNY`.
- Unclear direction defaults to `OUT`.
- Unclear category defaults to `uncategorized`.
- Date comes from the user text/image; if missing, use the configured local date.
- "Send statement" defaults to PDF.
- "Excel" means `.xlsx`; "CSV" means `.csv`.
- Use API undo when the user asks to reverse the last write.

## Workflows

- For statement entries, image attachment, balance updates, exports, and undo: read `references/api-actions.md`.
- For extracting fields from text/images: read `references/extraction-rules.md`.
- Use `scripts/statement_api.py` for repeatable API calls when available; it reads the same environment variables.

## Minimal Replies

Reply with only what changed or what was sent. Examples use placeholders:

- `Added #<no>: <client>, CNY OUT <amount>, image attached.`
- `Updated <account>: CNY <amount>.`
- `Sent <client> statement PDF.`
- `Undone.`

If blocked:

- `Which client?`
- `Amount?`
- `I could not attach the image because no image file was available. Added #<no>.`
