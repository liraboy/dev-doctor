#!/usr/bin/env python3
"""
doctor.py - development environment checker.

Runs, in order:
  1. Tool versions     (python, node, docker, git)
  2. Config files      (.env, auto-created from .env.example)
  3. Env variables     (required keys must be filled in)
  4. Connectivity      (database, external API, local port)
  5. Final report      (colored, with an exit code for CI/scripts)

Standard library only. Usage:  python doctor.py --help
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

# ============================== CONFIG ==============================
# Edit this section to match your repository.

# (display name, command, minimum version, required?)
# Python is checked through the running interpreter, so its command is None.
TOOLS: List[Tuple[str, Optional[str], Tuple[int, ...], bool]] = [
    ("Python", None, (3, 9), True),
    ("Node.js", "node", (18, 0), True),
    ("Docker", "docker", (20, 10), False),  # optional -> warning only
    ("Git", "git", (2, 30), True),
]

# Variables that must exist in .env AND have a value.
REQUIRED_ENV_KEYS = ["DATABASE_URL", "API_KEY", "PORT"]

# Optional URL checked over HTTP, if present in .env.
API_URL_KEY = "API_URL"

# Default TCP ports used when DATABASE_URL has no explicit port.
DEFAULT_DB_PORTS = {
    "postgres": 5432, "postgresql": 5432, "mysql": 3306,
    "mariadb": 3306, "mongodb": 27017, "redis": 6379,
}

# Values that look like leftovers from .env.example.
PLACEHOLDER = re.compile(r"^(changeme|change_me|your[_-].*|<.*>|x{3,}|todo)$", re.I)
# ====================================================================


class Status(Enum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass
class Result:
    status: Status
    message: str
    hint: Optional[str] = None


# ----------------------------- output -----------------------------

class Style:
    """ANSI colors and symbols, with a plain fallback."""

    def __init__(self, color: bool) -> None:
        self.color = color
        self.codes = {
            Status.OK: "\033[32m", Status.WARN: "\033[33m", Status.FAIL: "\033[31m",
            "bold": "\033[1m", "dim": "\033[2m", "reset": "\033[0m",
        }
        self.symbols = {Status.OK: "✔", Status.WARN: "⚠", Status.FAIL: "✖"}

    def paint(self, text: str, key) -> str:
        return f"{self.codes[key]}{text}{self.codes['reset']}" if self.color else text

    def symbol(self, status: Status) -> str:
        return self.paint(self.symbols[status], status)


def setup_terminal() -> None:
    """Make Unicode symbols and ANSI colors work on Windows terminals."""
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    if os.name == "nt":
        os.system("")  # enables ANSI escape processing in cmd/PowerShell


# ----------------------------- helpers -----------------------------

def parse_version(text: str) -> Optional[Tuple[int, ...]]:
    """Extract the first 'X.Y[.Z]' from a string, e.g. 'git version 2.43.0'."""
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text)
    if not match:
        return None
    return tuple(int(p) for p in match.groups() if p is not None)


def fmt(version: Tuple[int, ...]) -> str:
    return ".".join(str(p) for p in version)


def parse_env_file(path: Path) -> Dict[str, str]:
    """Minimal .env parser: KEY=VALUE, comments, 'export', and quotes."""
    values: Dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = re.sub(r"\s+#.*$", "", value)  # strip inline comment
        values[key.strip()] = value
    return values


# ----------------------------- 1. tools -----------------------------

def check_tools() -> List[Result]:
    results: List[Result] = []
    for name, command, minimum, required in TOOLS:
        missing_status = Status.FAIL if required else Status.WARN
        need = f">= {fmt(minimum)}"

        if command is None:  # the running Python interpreter
            found: Optional[Tuple[int, ...]] = tuple(sys.version_info[:3])
        else:
            path = shutil.which(command)
            if not path:
                results.append(Result(missing_status, f"{name} not found (need {need})",
                                      f"Install {name} and make sure '{command}' is in your PATH"))
                continue
            try:
                proc = subprocess.run([path, "--version"], capture_output=True,
                                      text=True, timeout=10)
                found = parse_version(proc.stdout + proc.stderr)
            except (subprocess.SubprocessError, OSError) as exc:
                results.append(Result(missing_status, f"{name}: could not run '{command} --version' ({exc})"))
                continue

        if found is None:
            results.append(Result(Status.WARN, f"{name}: could not read the version"))
        elif found >= minimum:
            results.append(Result(Status.OK, f"{name} {fmt(found)} (need {need})"))
        else:
            results.append(Result(missing_status, f"{name} {fmt(found)} is too old (need {need})",
                                  f"Upgrade {name} to {fmt(minimum)} or newer"))
    return results


# ----------------------------- 2. config -----------------------------

def check_env_file(env_path: Path, example_path: Path) -> List[Result]:
    if env_path.is_file():
        return [Result(Status.OK, f"{env_path.name} found")]
    if not example_path.is_file():
        return [Result(Status.FAIL, f"{env_path.name} and {example_path.name} are both missing",
                       f"Create {example_path.name} with the required keys")]
    try:
        shutil.copyfile(example_path, env_path)
    except OSError as exc:
        return [Result(Status.FAIL, f"Could not create {env_path.name}: {exc}")]
    return [Result(Status.WARN, f"{env_path.name} was missing, created from {example_path.name}",
                   f"Open {env_path.name} and fill in your real values")]


# ----------------------------- 3. variables -----------------------------

def check_env_vars(env_path: Path) -> Tuple[List[Result], Dict[str, str]]:
    if not env_path.is_file():
        return [Result(Status.FAIL, f"Cannot validate variables: {env_path.name} is missing")], {}
    try:
        values = parse_env_file(env_path)
    except (OSError, UnicodeDecodeError) as exc:
        return [Result(Status.FAIL, f"Could not read {env_path.name}: {exc}")], {}

    results: List[Result] = []
    for key in REQUIRED_ENV_KEYS:
        value = values.get(key)
        if value is None:
            results.append(Result(Status.FAIL, f"{key} is not defined", f"Add {key}=... to {env_path.name}"))
        elif value == "":
            results.append(Result(Status.FAIL, f"{key} is empty", f"Set a value for {key}"))
        elif PLACEHOLDER.match(value):
            results.append(Result(Status.WARN, f"{key} still looks like a placeholder", "Replace it with a real value"))
        else:
            results.append(Result(Status.OK, f"{key} is set"))
    return results, values


# ----------------------------- 4. connectivity -----------------------------

def check_tcp(host: str, port: int, timeout: float) -> Optional[str]:
    """Return None if the TCP connection works, otherwise the error text."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return None
    except OSError as exc:
        return str(exc)


def check_database(values: Dict[str, str], timeout: float) -> Optional[Result]:
    url = values.get("DATABASE_URL", "")
    if not url:
        return None
    parsed = urlparse(url)
    scheme = parsed.scheme.split("+")[0]
    host = parsed.hostname
    try:
        port = parsed.port or DEFAULT_DB_PORTS.get(scheme)
    except ValueError:
        port = None
    if not host or not port:
        return Result(Status.WARN, "DATABASE_URL could not be parsed (host/port missing)")
    error = check_tcp(host, port, timeout)
    if error is None:
        return Result(Status.OK, f"Database reachable at {host}:{port}")
    return Result(Status.FAIL, f"Database unreachable at {host}:{port} ({error})",
                  "Is the database running? Try: docker compose up -d")


def check_api(values: Dict[str, str], timeout: float) -> Optional[Result]:
    url = values.get(API_URL_KEY, "")
    if not url:
        return None
    if urlparse(url).scheme not in ("http", "https"):
        return Result(Status.WARN, f"{API_URL_KEY} is not an http(s) URL")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=timeout) as resp:
            return Result(Status.OK, f"API reachable at {url} (HTTP {resp.status})")
    except urllib.error.HTTPError as exc:
        # The server answered, so the network path works even if it is 401/404.
        return Result(Status.OK, f"API reachable at {url} (HTTP {exc.code})")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return Result(Status.FAIL, f"API unreachable at {url} ({getattr(exc, 'reason', exc)})",
                      "Check the URL, your connection or VPN")


def check_local_port(values: Dict[str, str]) -> Optional[Result]:
    raw = values.get("PORT", "")
    if not raw.isdigit() or not 0 < int(raw) < 65536:
        return None
    port = int(raw)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return Result(Status.WARN, f"Local port {port} is already in use",
                          "Stop the other process or change PORT in .env")
    return Result(Status.OK, f"Local port {port} is free")


def check_connectivity(values: Dict[str, str], timeout: float, skip_network: bool) -> List[Result]:
    results: List[Result] = []
    if skip_network:
        results.append(Result(Status.WARN, "Network checks skipped (--skip-network)"))
    else:
        for check in (check_database, check_api):
            result = check(values, timeout)
            if result:
                results.append(result)
    port_result = check_local_port(values)
    if port_result:
        results.append(port_result)
    if not results:
        results.append(Result(Status.OK, "Nothing to test"))
    return results


# ----------------------------- 5. report -----------------------------

def render(report: List[Tuple[str, List[Result]]], style: Style) -> Dict[Status, int]:
    totals = {Status.OK: 0, Status.WARN: 0, Status.FAIL: 0}
    for index, (title, results) in enumerate(report, start=1):
        print(f"\n{style.paint(f'{index}. {title}', 'bold')}")
        for res in results:
            totals[res.status] += 1
            print(f"  {style.symbol(res.status)} {res.message}")
            if res.hint and res.status is not Status.OK:
                print(style.paint(f"      → {res.hint}", "dim"))

    summary = (f"{style.paint(str(totals[Status.OK]) + ' passed', Status.OK)}, "
              f"{style.paint(str(totals[Status.WARN]) + ' warnings', Status.WARN)}, "
              f"{style.paint(str(totals[Status.FAIL]) + ' failed', Status.FAIL)}")
    print(f"\n{style.paint('Summary:', 'bold')} {summary}")
    if totals[Status.FAIL]:
        print(style.paint("\n✖ Environment is NOT ready. Fix the errors above.", Status.FAIL))
    elif totals[Status.WARN]:
        print(style.paint("\n⚠ Environment is usable, but check the warnings.", Status.WARN))
    else:
        print(style.paint("\n✔ Environment is ready. Happy coding!", Status.OK))
    return totals


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check the local development environment.")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="repository root (default: current dir)")
    parser.add_argument("--env-file", default=".env", help="env file name (default: .env)")
    parser.add_argument("--example-file", default=".env.example", help="template name (default: .env.example)")
    parser.add_argument("--timeout", type=float, default=3.0, help="network timeout in seconds (default: 3)")
    parser.add_argument("--skip-network", action="store_true", help="skip database and API checks")
    parser.add_argument("--strict", action="store_true", help="treat warnings as failures (exit code 1)")
    parser.add_argument("--no-color", action="store_true", help="disable colors")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    setup_terminal()
    use_color = sys.stdout.isatty() and not args.no_color and "NO_COLOR" not in os.environ
    style = Style(use_color)

    env_path = args.root / args.env_file
    example_path = args.root / args.example_file

    print(style.paint("Checking your development environment...", "bold"))
    report: List[Tuple[str, List[Result]]] = []
    report.append(("Tool versions", check_tools()))
    report.append(("Config files", check_env_file(env_path, example_path)))
    var_results, values = check_env_vars(env_path)
    report.append(("Environment variables", var_results))
    report.append(("Connectivity and services", check_connectivity(values, args.timeout, args.skip_network)))

    totals = render(report, style)
    failed = totals[Status.FAIL] > 0 or (args.strict and totals[Status.WARN] > 0)
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)
