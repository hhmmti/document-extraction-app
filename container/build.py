"""Builds the container platform's app folder for the run viewer (app/main.py) from this repository.

    python container/build.py --target <platform repo> --contact "<whom engineers tell>" --owner "<the app's owner>"

Writes <target>/apps/enterprise/esp-design-extraction/, replacing that folder and nothing else:

    app/, extraction/, .streamlit/config.toml     the files git tracks or would track there: no cache, nothing ignored
    Dockerfile, requirements.txt, .dockerignore   this folder's, as they are
    app.config.json                               this folder's, its placeholders filled
    SOURCE.txt                                    where the files came from: remote, commit, time, shipped paths

The contact and the owner come from the command line; the data role's ARN, its external id and its account from the
target's cdk.json, under context.master.  Nothing is written with a placeholder left or a value missing.  A shipped
path with uncommitted changes is refused, so a folder that goes into a pull request is a commit's; --local-test builds
from the files as they are, and SOURCE.txt says so.  The same source gives the same files, SOURCE.txt's time apart.
No network, no AWS.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FOLDER = "apps/enterprise/esp-design-extraction"
SHIPPED = ("app", "extraction", ".streamlit/config.toml")     # the allow-list: what the app runs on
CONFIG = "app.config.json"
OWN = ("Dockerfile", "requirements.txt", ".dockerignore", CONFIG)    # container/'s files, beside the shipped paths
NOTE = "SOURCE.txt"
LOCAL_TEST = "uncommitted: local test only"
CACHES = ("__pycache__", ".pytest_cache", ".DS_Store")


class Refused(Exception):
    """Why nothing was written."""


def git(source, *args, check=True) -> str:
    try:
        done = subprocess.run(["git", "-C", str(source), *args], capture_output=True, text=True,
                              env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})          # read only: no index refresh
    except OSError as err:
        raise Refused(f"git could not run: {err}") from err
    if check and done.returncode:
        raise Refused(f"git {' '.join(args)} failed in {source}: {done.stderr.strip()}")
    return done.stdout


def role(target) -> dict:
    """The data role's values, from the platform's cdk.json under context.master."""
    try:
        master = json.loads((Path(target) / "cdk.json").read_text(encoding="utf-8"))["context"]["master"]
        account, data_role = master["accountId"], master["crossAccountDataRole"]
        return {"ACCOUNT_ID": account, "ROLE_ARN": f"arn:aws:iam::{account}:role/{data_role['roleName']}",
                "EXTERNAL_ID": data_role["externalId"]}
    except (OSError, ValueError, KeyError, TypeError) as err:
        raise Refused(f"{Path(target) / 'cdk.json'} does not give the data role under context.master: {err!r}") from err


def fill(template: str, values: dict) -> str:
    """The template with each {{NAME}} replaced by its value, as JSON text; refused with a placeholder left."""
    empty = sorted(name for name, value in values.items() if not str(value).strip())
    if empty:
        raise Refused(f"no value for {', '.join(empty)}")
    for name, value in values.items():
        template = template.replace("{{%s}}" % name, json.dumps(str(value), ensure_ascii=False)[1:-1])
    left = sorted(set(re.findall(r"\{\{.*?\}\}", template)))
    if left:
        raise Refused(f"{CONFIG} would keep a placeholder: {', '.join(left)}")
    json.loads(template)
    return template


def shipped_files(source) -> list[str]:
    """Every file under the shipped paths that git tracks or would track, caches left out."""
    names = git(source, "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", *SHIPPED).split("\0")
    return sorted(name for name in names if name and (Path(source) / name).is_file()
                  and not name.endswith(".pyc") and not set(Path(name).parts) & set(CACHES))


def build(target, contact, owner, local_test=False, source=REPO) -> Path:
    """Writes the app folder under `target` and returns it; raises Refused, with nothing written, otherwise."""
    source, target = Path(source), Path(target)
    if not (target / "apps").is_dir():
        raise Refused(f"{target} has no apps/ folder: it is not the platform repository")
    paths = [*SHIPPED, *(f"container/{name}" for name in OWN)]
    changed = git(source, "status", "--porcelain", "--untracked-files=all", "--", *paths).rstrip()
    if changed and not local_test:
        raise Refused(f"uncommitted changes in what would be shipped:\n{changed}\nCommit them, or pass --local-test "
                      "for a folder that is only tested on this machine.")
    config = fill((source / "container" / CONFIG).read_text(encoding="utf-8"),
                  {"CONTACT": contact, "OWNER": owner, **role(target)})
    files = shipped_files(source)
    remote = git(source, "config", "--get", "remote.origin.url", check=False).strip()
    note = "\n".join([
        f"source: {re.sub(r'(?<=://)[^/@]*@', '', remote) or 'local'}",       # never a credential kept in a URL
        f"commit: {git(source, 'rev-parse', 'HEAD').strip()}",
        *([LOCAL_TEST] if local_test else []),
        f"built: {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}",
        "shipped paths:", *(f"  {path}" for path in paths)]) + "\n"

    dest = target / FOLDER                 # everything is checked: replace the folder
    if dest.exists():
        shutil.rmtree(dest)
    for name in files:
        (dest / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, dest / name)
    for name in OWN:
        shutil.copyfile(source / "container" / name, dest / name)
    (dest / CONFIG).write_text(config, encoding="utf-8")
    (dest / NOTE).write_text(note, encoding="utf-8")
    return dest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target", required=True, help="the container platform repository")
    parser.add_argument("--contact", required=True, help="whom engineers tell when something fails: it follows 'tell'")
    parser.add_argument("--owner", required=True, help="the app's owner, as the platform's portal names them")
    parser.add_argument("--local-test", action="store_true",
                        help="build from the files as they are, uncommitted changes too; SOURCE.txt says so")
    args = parser.parse_args(argv)
    try:
        dest = build(args.target, args.contact, args.owner, args.local_test)
    except Refused as err:
        print(f"Refused, nothing written: {err}", file=sys.stderr)
        return 1
    print(dest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
