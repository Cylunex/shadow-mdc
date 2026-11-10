"""MDC skill CLI with private HTTP/SSH configuration and bounded JSON results."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter

from .skill_batch import prepare_batch, run_batch

_MAX_RESPONSE = 8 * 1024 * 1024
_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


class ClientConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    transport: Literal["http", "ssh"] = "http"
    base_url: str = ""
    token_file: Path | None = None
    owner: str = Field(default="", max_length=100)
    timeout_seconds: float = Field(default=60, ge=1, le=300)
    ssh_host: str = ""
    remote_launcher: str = ""
    remote_config: str = ""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def _read_config(path: Path) -> ClientConfig:
    if path.stat().st_mode & 0o077:
        raise ValueError("private config must have permissions 0600")
    return ClientConfig.model_validate_json(path.read_text())


def _http_call(config: ClientConfig, config_path: Path, body: bytes | None) -> JsonValue:
    base = config.base_url.rstrip("/")
    url = urlsplit(base)
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise ValueError("base_url must be an HTTP(S) service URL without credentials, query or fragment")
    if re.fullmatch(r"[A-Za-z0-9_-]{1,100}", config.owner) is None or config.token_file is None:
        raise ValueError("owner and token_file are required for HTTP")
    token_path = config.token_file.expanduser()
    if not token_path.is_absolute():
        token_path = config_path.parent / token_path
    if token_path.stat().st_mode & 0o077:
        raise ValueError("token file must have permissions 0600")
    token = token_path.read_text().strip()
    if not token or "\n" in token or "\r" in token:
        raise ValueError("token file is empty or invalid")
    target = "/api/skill/v1/call" if body is not None else "/api/skill/v1/catalog"
    request = Request(
        base + target,
        data=body,
        headers={
            "Authorization": "Bearer " + token,
            "X-Catalog-Owner": config.owner,
            "Content-Type": "application/json",
        },
    )
    with build_opener(ProxyHandler({}), NoRedirect()).open(
        request, timeout=config.timeout_seconds
    ) as response:
        data = response.read(_MAX_RESPONSE + 1)
    if len(data) > _MAX_RESPONSE:
        raise ValueError("response exceeds the CLI output limit; use smaller pages")
    return _JSON.validate_json(data)


def _ssh_call(config: ClientConfig, command: str, capability: str | None, arguments: JsonValue) -> JsonValue:
    if (
        not re.fullmatch(r"[A-Za-z0-9_@.:-]+", config.ssh_host)
        or config.ssh_host.startswith("-")
        or not config.remote_launcher.startswith("/")
        or not config.remote_config.startswith("/")
    ):
        raise ValueError("SSH requires an existing host alias and absolute launcher/config paths")
    remote = [config.remote_launcher, "--config", config.remote_config, command]
    if capability is not None:
        remote.append(capability)
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", config.ssh_host, shlex.join(remote)],
        input=json.dumps(arguments).encode() if command == "call" else b"",
        capture_output=True,
        timeout=config.timeout_seconds + 15,
        check=False,
    )
    if len(result.stdout) > _MAX_RESPONSE:
        raise ValueError("response exceeds the CLI output limit")
    # stderr may contain SSH diagnostics; keep it out of agent/user output.
    if result.returncode and not result.stdout:
        raise URLError("remote MDC command failed")
    return _JSON.validate_json(result.stdout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MDC operator skill CLI")
    parser.add_argument(
        "--config", default=os.environ.get("MDC_SKILL_CONFIG"), help="private JSON config path"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("catalog", help="list actual server capabilities and schemas")
    describe = commands.add_parser("describe", help="read one actual server input schema")
    describe.add_argument("capability")
    call = commands.add_parser("call", help="execute a capability; JSON file or stdin")
    call.add_argument("capability")
    call.add_argument("input_file", nargs="?", default="-")
    prepare = commands.add_parser(
        "prepare", help="freeze matching work IDs or discover pages into a manifest"
    )
    prepare.add_argument("capability")
    prepare.add_argument("--query", required=True, help="JSON catalog filter or discover.browse input")
    prepare.add_argument(
        "--arguments", required=True, help="JSON operation arguments shared by all selected works"
    )
    prepare.add_argument("--manifest", required=True)
    prepare.add_argument(
        "--source-pages", type=int, default=0, help="discover pages instead of catalog query"
    )
    batch = commands.add_parser("batch", help="execute a JSON manifest with a durable per-item receipt")
    batch.add_argument("manifest")
    batch.add_argument("--receipt", required=True)
    batch.add_argument("--retry-failed", action="store_true")
    batch.add_argument(
        "--retry-unknown",
        action="store_true",
        help="only after reading original target/task state and reconciling the unknown write",
    )
    batch.add_argument("--pace-seconds", type=float, default=0.25)
    args = parser.parse_args(argv)
    output: JsonValue
    try:
        if not args.config:
            raise ValueError("set --config or MDC_SKILL_CONFIG to a private JSON file")
        config_path = Path(args.config).expanduser().resolve()
        config = _read_config(config_path)

        def execute(name: str, values: dict[str, JsonValue]) -> JsonValue:
            if config.transport == "ssh":
                return _ssh_call(config, "call", name, values)
            return _http_call(config, config_path, json.dumps({"name": name, "arguments": values}).encode())

        if args.command == "prepare":
            if not 0 <= args.source_pages <= 100:
                raise ValueError("source-pages must be between 0 and 100")
            query = _JSON.validate_json(Path(args.query).read_text())
            common = _JSON.validate_json(Path(args.arguments).read_text())
            if not isinstance(query, dict) or not isinstance(common, dict):
                raise ValueError("query and arguments must be JSON objects")
            output = prepare_batch(
                query, common, args.capability, Path(args.manifest), execute, source_pages=args.source_pages
            )
            print(json.dumps(output, ensure_ascii=False))
            return 0
        if args.command == "batch":
            if not 0 <= args.pace_seconds <= 60:
                raise ValueError("pace-seconds must be between 0 and 60")
            output = run_batch(
                Path(args.manifest),
                Path(args.receipt),
                execute,
                retry_failed=args.retry_failed,
                retry_unknown=args.retry_unknown,
                pace_seconds=args.pace_seconds,
            )
            print(json.dumps(output, ensure_ascii=False))
            return 0 if output["ok"] else 1
        arguments: JsonValue = {}
        body = None
        if args.command == "call":
            raw = sys.stdin.read() if args.input_file == "-" else Path(args.input_file).read_text()
            arguments = _JSON.validate_json(raw)
            if not isinstance(arguments, dict):
                raise ValueError("capability input must be a JSON object")
            body = json.dumps({"name": args.capability, "arguments": arguments}).encode()
        if config.transport == "ssh":
            output = _ssh_call(
                config, args.command, None if args.command == "catalog" else args.capability, arguments
            )
        else:
            output = _http_call(config, config_path, body)
            if args.command == "describe":
                if not isinstance(output, dict):
                    raise ValueError("invalid capability catalog")
                capabilities = output.get("capabilities")
                if not isinstance(capabilities, list):
                    raise ValueError("invalid capability catalog")
                output = next(
                    (
                        item
                        for item in capabilities
                        if isinstance(item, dict) and item.get("name") == args.capability
                    ),
                    None,
                )
                if output is None:
                    raise ValueError("capability is not offered by the server")
        print(json.dumps(output, ensure_ascii=False))
        return 1 if isinstance(output, dict) and output.get("ok") is False else 0
    except HTTPError as exc:
        output = {"ok": False, "error": {"code": "http_error", "status": exc.code}}
    except (URLError, TimeoutError, subprocess.TimeoutExpired):
        output = {
            "ok": False,
            "error": {
                "code": "connection_error",
                "message": "MDC connection failed; check the original action before retrying a write",
            },
        }
    except (ValueError, OSError, KeyError, TypeError):
        output = {
            "ok": False,
            "error": {
                "code": "invalid_input_or_config",
                "message": "Check private config and input JSON; credentials and raw errors are not printed",
            },
        }
    print(json.dumps(output, ensure_ascii=False))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
