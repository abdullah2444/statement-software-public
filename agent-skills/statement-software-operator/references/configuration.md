# Configuration

Keep real values outside this skill.

## Environment

Required:

```text
STATEMENT_BASE_URL=<base_url>
STATEMENT_API_TOKEN=<api_token>
```

Optional:

```text
STATEMENT_TIMEZONE=<timezone>
```

`STATEMENT_BASE_URL` should not include a trailing slash. API calls append `/api/v1/...`.

## Secrets

- Do not write tokens, passwords, domains, IP addresses, or client names into the skill.
- If a token is missing, ask the user to provide it through the current secure channel or environment.
- If the API returns `401`, say: `API token is missing or invalid.`
- If the API returns `403`, say: `API token does not allow this action.`

## Deployment Notes

This skill is portable. Any deployment-specific values such as server address, container name, ports, repository remotes, or credentials must come from the current workspace, environment, or user instruction at runtime.
