#!/usr/bin/env python3
"""Small API helper for the statement software operator skill.

Reads STATEMENT_BASE_URL and STATEMENT_API_TOKEN from the environment.
Supports JSON requests, multipart uploads, and binary downloads.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is required")
    return value


def parse_kv(items: list[str]) -> dict[str, str]:
    data: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"Expected key=value: {item}")
        key, value = item.split("=", 1)
        data[key] = value
    return data


def build_multipart(fields: dict[str, str], files: dict[str, Path]) -> tuple[bytes, str]:
    boundary = "----statement-" + uuid.uuid4().hex
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
            str(value).encode(),
            b"\r\n",
        ])
    for name, path in files.items():
        filename = path.name
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode(),
            f"Content-Type: {mime}\r\n\r\n".encode(),
            path.read_bytes(),
            b"\r\n",
        ])
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def call_api(
    method: str,
    path: str,
    json_payload: str | None,
    form: list[str],
    file_args: list[str],
    output: str | None,
) -> int:
    base_url = require_env("STATEMENT_BASE_URL").rstrip("/")
    token = require_env("STATEMENT_API_TOKEN")
    url = base_url + path
    headers = {"Authorization": f"Bearer {token}"}
    body: bytes | None = None

    if json_payload:
        body = json_payload.encode()
        headers["Content-Type"] = "application/json"
    elif form or file_args:
        fields = parse_kv(form)
        files = {key: Path(value) for key, value in parse_kv(file_args).items()}
        body, content_type = build_multipart(fields, files)
        headers["Content-Type"] = content_type

    request = Request(url, data=body, headers=headers, method=method.upper())
    try:
        with urlopen(request, timeout=60) as response:
            content = response.read()
            content_type = response.headers.get("Content-Type", "")
    except HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        print(text or str(exc), file=sys.stderr)
        return exc.code

    if output:
        Path(output).write_bytes(content)
        print(output)
        return 0

    if "application/json" in content_type:
        parsed = json.loads(content.decode("utf-8"))
        print(json.dumps(parsed, ensure_ascii=False, indent=2))
    else:
        sys.stdout.buffer.write(content)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Call the statement software API")
    parser.add_argument("method")
    parser.add_argument("path", help="API path, for example /api/v1/clients")
    parser.add_argument("--json", dest="json_payload")
    parser.add_argument("--form", action="append", default=[], help="form key=value")
    parser.add_argument("--file", action="append", default=[], help="file field=/path/to/file")
    parser.add_argument("--output", help="write binary response to path")
    args = parser.parse_args()
    return call_api(args.method, args.path, args.json_payload, args.form, args.file, args.output)


if __name__ == "__main__":
    raise SystemExit(main())
