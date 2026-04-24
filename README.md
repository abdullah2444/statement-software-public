# Statement Software

Open-source statement management software with Docker/Python setup, full backups, restore, and a friendly `statementsw` terminal command.

GitHub stores code only. Your `.env`, database, uploads, backups, logs, and business data stay private on your machine or server.

## One-Command Install

```bash
curl -fsSL https://raw.githubusercontent.com/abdullah2444/statement-software-public/main/scripts/install.sh | bash
```

After install, use:

```bash
statementsw setup
statementsw start
statementsw status
statementsw doctor
statementsw backup
statementsw restore ./statement-full-backup-YYYYMMDD-HHMMSS.tar.gz
```

Custom install examples:

```bash
curl -fsSL https://raw.githubusercontent.com/abdullah2444/statement-software-public/main/scripts/install.sh | bash -s -- --port 8080
curl -fsSL https://raw.githubusercontent.com/abdullah2444/statement-software-public/main/scripts/install.sh | bash -s -- --method python --dir ~/statement-software
```

## Useful Commands

```bash
statementsw help
statementsw setup
statementsw start
statementsw stop
statementsw restart
statementsw status
statementsw doctor
statementsw backup
statementsw export
statementsw restore ./data/backups/statement-full-backup-YYYYMMDD-HHMMSS.tar.gz
statementsw reset-admin-password --admin-user admin
statementsw update
```

The classic project-local command still works:

```bash
bash setup.sh quickstart
```

## Configuration

`setup.sh` creates `.env` for you. `.env` is private and ignored by Git.

Important settings:

- `PORT`: browser port.
- `HOST`: bind address.
- `DATA_DIR`: private runtime data folder.
- `DATABASE_PATH`: SQLite database path.
- `UPLOAD_DIR`: uploaded image/file path.
- `BACKUP_DIR`: backup path.
- `MAX_UPLOAD_MB`: max Settings restore upload size. Default: `512`.
- `APP_NAME`: visible app name. Default: `Statement Software`.
- `BRAND_NAME`: short UI brand. Default: `Statement`.
- `COMPANY_NAME`: company name shown on PDFs. Default: `Your Company`.
- `DEFAULT_PROFIT_EXPENSE_ACCOUNT_NAME`: default account for company profit mirroring.
- `SEED_DEMO_DATA`: keep `0` for normal fresh installs.
- `SOURCE_CSV_PATH`: optional path to demo CSV when `SEED_DEMO_DATA=1`.
- `DEMO_CLIENT_NAME`: client name used only when importing demo CSV data.
- `SECRET_KEY`: private Flask session secret.
- `SESSION_COOKIE_SECURE`: use `1` only when serving through HTTPS.
- `OPENROUTER_API_KEY`: optional AI image parsing key.
- `FX_PROXY_URL`: optional proxy URL for exchange-rate API calls.

## Backups

Create a full backup:

```bash
statementsw backup
```

The full backup package contains:

- `manifest.json`
- `statement_software.db`
- `uploads/`

Restore a full backup:

```bash
statementsw restore ./data/backups/statement-full-backup-YYYYMMDD-HHMMSS.tar.gz
```

Legacy raw SQLite exports are still accepted, but they are database-only and do not include uploaded images.

## GitHub Safety

Do not commit:

- `.env`
- `data/`
- databases
- uploads
- backups
- logs
- archives
- spreadsheets or real business files
- duplicate extracted app folders

## Updating

```bash
statementsw backup
statementsw update
statementsw doctor
```
