# dev-doctor

A small CLI that checks if your local development environment is ready.
Standard library only, nothing to install.

## What it checks

1. **Tool versions**: Python, Node.js, Docker and Git
2. **Config files**: creates `.env` from `.env.example` if it is missing
3. **Environment variables**: required keys must exist and be filled in
4. **Connectivity**: database (TCP), external API (HTTP) and local port
5. **Report**: colored summary with hints, plus an exit code for CI

## Usage

```bash
python doctor.py
```

| Flag | Description |
| --- | --- |
| `--skip-network` | skip database and API checks |
| `--strict` | treat warnings as failures |
| `--no-color` | disable colors |
| `--timeout N` | network timeout in seconds (default 3) |
| `--root PATH` | repository root (default: current directory) |

Exit code is `0` when everything passes and `1` when something fails.

## Add it to your project

Makefile:

```makefile
doctor:
	python doctor.py
```

package.json:

```json
{
  "scripts": {
    "doctor": "python doctor.py",
    "predev": "python doctor.py --skip-network"
  }
}
```

## Configuration

Edit the `CONFIG` section at the top of `doctor.py`:

- `TOOLS`: tools, minimum versions and whether they are required
- `REQUIRED_ENV_KEYS`: keys that must be set in `.env`
- `API_URL_KEY`: env key holding an optional URL to ping
- `DEFAULT_DB_PORTS`: fallback ports per database scheme

## License

MIT
