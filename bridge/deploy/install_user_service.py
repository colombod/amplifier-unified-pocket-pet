"""Install the new user's gateway unit. Never pair, enable, start or stop a service.

Use the gateway venv's Python. Default is a read-only check; pass --install
to write the unit. Existing units and private settings are never replaced.
"""
import argparse
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

BRIDGE = Path(__file__).resolve().parents[1]
UNIT = "pocket-gateway.service"


class Refused(Exception):
    pass


def unit_path(path):
    """Absolute literal paths only. Don't pass shell/systemd expressions through."""
    value = str(path)
    if (not path.is_absolute() or ".." in path.parts or value.endswith(" ")
            or not all(ch.isprintable() and ch not in "\"'\\$" for ch in value)):
        raise Refused("Use absolute paths without quotes, backslashes, '$' or control characters.")
    return value.replace("%", "%%")


def render(python, settings, env_file, event_log):
    template = (BRIDGE / "deploy" / UNIT).read_text(encoding="utf-8")
    # Use the same template for both supported forms, not a second service design.
    values = {"PYTHON": '"' + unit_path(python) + '"', "BRIDGE_DIR": unit_path(BRIDGE),
              "ENV_FILE": unit_path(settings), "CONFIG": unit_path(settings),
              "EVENT_LOG": unit_path(event_log)}
    lines = []
    for line in template.splitlines():
        if not env_file and line.startswith("#ExecStart="):
            line = line[1:]
        elif line.startswith("#") or (not env_file and line.startswith("ExecStart=")):
            continue
        lines.append(re.sub(r"@([A-Z_]+)@", lambda m: values[m[1]], line))
    if sum(line.startswith("ExecStart=") for line in lines) != 1:
        raise Refused("Invalid gateway unit template.")
    return "\n".join(lines) + "\n"


def directory(path, *, private, install):
    """Create only missing directories, never chmod existing ones."""
    unit_path(path)
    if path.resolve() != path:
        raise Refused("A setup directory is not a direct path.")
    anchor = path
    while not anchor.exists():
        anchor = anchor.parent
    info = anchor.stat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o022 or (anchor == path and private and info.st_mode & 0o077)):
        raise Refused("Use an owner-controlled directory (0700 for the log directory).")
    if install:
        current = anchor
        for component in path.relative_to(anchor).parts:
            current = current / component
            current.mkdir(mode=0o700)  # refuse a directory that appeared after preflight


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--env-file", type=Path)
    source.add_argument("--config", type=Path, help="alternative private JSON settings")
    parser.add_argument("--install", action="store_true", help="write a new unit without activating it")
    args = parser.parse_args()
    try:
        if os.geteuid() == 0 or sys.platform != "linux":
            raise Refused("Run as the intended non-root Linux user.")
        settings = args.env_file or args.config
        unit_path(settings)
        sys.path.insert(0, str(BRIDGE))
        from pet_bridge import gateway_server
        try:
            if args.env_file:
                gateway_server.load_env_config(settings)
            else:
                gateway_server.load_config(settings)
        except gateway_server.StartupError:
            raise Refused("The gateway refused the settings file or its permissions.") from None
        home = Path.home()
        unit_dir = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / "systemd/user"
        log_dir = Path(os.environ.get("XDG_STATE_HOME") or home / ".local/state") / "pocket-gateway"
        event_log = log_dir / "events.jsonl"
        target = unit_dir / UNIT
        # Keep the venv interpreter path, not its base interpreter's resolved symlink.
        content = render(Path(sys.executable), settings, bool(args.env_file), event_log)
        if os.path.lexists(target):
            raise Refused("A gateway unit already exists. Nothing was replaced.")
        if os.path.lexists(event_log):
            raise Refused("The event log already exists. Use the documented existing-install procedure.")
        for path, private in ((unit_dir, False), (log_dir, True)):
            directory(path, private=private, install=False)
        if not args.install:
            print("Settings parsed and unit paths checked. No files written or service started.")
            return 0
        directory(unit_dir, private=False, install=True)
        directory(log_dir, private=True, install=True)
        # Publish complete bytes, atomically, without overwriting any earlier file.
        with tempfile.NamedTemporaryFile(dir=unit_dir, mode="w", encoding="utf-8") as tmp:
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
            os.link(tmp.name, target)
        print(f"Installed {target}. Service NOT enabled or started.")
        print("After pairing and binding, run: systemctl --user daemon-reload && "
              "systemctl --user enable --now pocket-gateway.service")
        return 0
    except ImportError:
        print("Use the gateway Python environment with deploy/requirements.txt installed.", file=sys.stderr)
    except Refused as exc:
        print(str(exc), file=sys.stderr)
    except (OSError, ValueError, RuntimeError):
        print("Could not prepare the user unit. Check paths and ownership. No service was started.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())