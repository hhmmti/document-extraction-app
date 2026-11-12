"""What the app needs to run hosted, behind a sign-in, for engineers.  Five settings, all read here:

    ESP_SHOW_TECHNICAL             "1" shows the technical views and details (run ids, codes, the pipeline's own files,
                                   error text, set-up hints); unset, or anything else, hides them (`technical`), and
                                   what a screen then leaves out goes to the app's log instead (`hidden`)
    ESP_CONTACT                    whom to tell when something goes wrong, as the screens name them (`contact`)
    ESP_CROSS_ACCOUNT_ROLE_ARN     both set: every AWS call the app makes goes through that role, assumed with that
    ESP_CROSS_ACCOUNT_EXTERNAL_ID  external id (`session`); neither: boto3's default chain; one alone: `SetUp`
    ESP_SIGNED_IN_USER_HEADER      the request header that names the signed-in user: who confirms (app/review.py's
                                   `confirmer` reads it from `request_headers`)

and the pump catalogue snapshot, fetched where no folder holds it (`fetch_catalogue`).  Neither Streamlit nor the AWS
SDK is loaded at import: local mode loads the SDK only for the upload form's well lists.
"""

from __future__ import annotations

import functools
import logging
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

from extraction import confirmation as C
from extraction.assets_dir import ASSETS_ENV
from extraction.stage.stage import CATALOGUE, asset

SHOW_ENV, CONTACT_ENV = "ESP_SHOW_TECHNICAL", "ESP_CONTACT"
ROLE_ENV, EXTERNAL_ENV = "ESP_CROSS_ACCOUNT_ROLE_ARN", "ESP_CROSS_ACCOUNT_EXTERNAL_ID"
USER_HEADER_ENV = "ESP_SIGNED_IN_USER_HEADER"
REGION = "us-east-1"                        # the datascience account's: the bucket, Athena and the role
SESSION_NAME = "esp-design-extraction-app"  # the assumed role's session name, as CloudTrail shows the app's calls
NO_ONE = "whoever looks after this app"

# The app's one logger, to standard error: what a screen leaves out, and nothing that names who is signed in.
LOG = logging.getLogger("esp_design_extraction.app")
if not LOG.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    LOG.addHandler(_handler)
    LOG.setLevel(logging.INFO)
_LOGGED: set = set()                        # the occurrences `hidden` has logged by key, for this app process
_FETCH = threading.Lock()                   # one session at a time fetches the catalogue


class SetUp(Exception):
    """A setting the app cannot run with.  The message names the setting: a technical detail."""


def technical(environ=os.environ) -> bool:
    """Whether the technical views and details are drawn: only when ESP_SHOW_TECHNICAL is exactly "1"."""
    return environ.get(SHOW_ENV) == "1"


def contact(environ=os.environ) -> str:
    """Whom to tell when something goes wrong, to follow `tell` or `ask` in a sentence: ESP_CONTACT, else no one by
    name."""
    return (environ.get(CONTACT_ENV) or "").strip() or NO_ONE


def hidden(what: str, error, key=None, trace: bool = False) -> None:
    """What a screen leaves out, to the log: `<what>: <class>: <message>` for an exception, `<what>: <text>` for a
    detail in words; once per occurrence.  An exception is one occurrence however many reruns show its screen (it is
    marked once logged).  `key` names an occurrence that is not one exception object (a run's failure, a setting not
    set, a read that fails again on every rerun), logged once per app process.  `trace` adds the traceback."""
    if key is not None:
        if key in _LOGGED:
            return
        _LOGGED.add(key)
    elif isinstance(error, BaseException):
        if getattr(error, "_esp_logged", False):
            return
        try:
            error._esp_logged = True
        except AttributeError:              # an exception that takes no attribute: logged each time
            pass
    is_error = isinstance(error, BaseException)
    LOG.error("%s: %s", what, f"{type(error).__name__}: {error}" if is_error else error,
              exc_info=error if trace and is_error else None)


# ---- AWS ----

def role(environ=os.environ) -> tuple[str, str] | None:
    """(the role to assume, its external id), or None for boto3's default chain.  One without the other is SetUp:
    asked before any AWS call, so none is made."""
    arn, external = ((environ.get(name) or "").strip() for name in (ROLE_ENV, EXTERNAL_ENV))
    if bool(arn) != bool(external):
        raise SetUp(f"{ROLE_ENV} and {EXTERNAL_ENV} go together: {ROLE_ENV if arn else EXTERNAL_ENV} is set and "
                    f"{EXTERNAL_ENV if arn else ROLE_ENV} is not")
    return (arn, external) if arn else None


def session(environ=os.environ):
    """The one boto3 session behind every AWS call the app makes (S3, Athena, the catalogue), in us-east-1, built
    once per process for the settings as they are."""
    return _session(role(environ))


@functools.lru_cache(maxsize=1)
def _session(assumed):
    """`assumed` None: boto3's default chain.  Else the role, assumed with its external id through STS by the default
    chain's identity (the container's own), with credentials botocore refreshes before they expire, as the container
    platform's apps do."""
    import boto3
    if assumed is None:
        return boto3.Session(region_name=REGION)
    from botocore.credentials import RefreshableCredentials
    from botocore.session import get_session
    arn, external = assumed

    def assume() -> dict:
        got = boto3.client("sts", region_name=REGION).assume_role(
            RoleArn=arn, RoleSessionName=SESSION_NAME, ExternalId=external)["Credentials"]
        return {"access_key": got["AccessKeyId"], "secret_key": got["SecretAccessKey"], "token": got["SessionToken"],
                "expiry_time": got["Expiration"].isoformat()}

    core = get_session()
    core._credentials = RefreshableCredentials.create_from_metadata(      # botocore has no public setter for these
        metadata=assume(), refresh_using=assume, method="sts-assume-role")
    core.set_config_variable("region", REGION)
    return boto3.Session(botocore_session=core)


# ---- who is signed in, and where ----

def request_headers():
    """The session's request headers, as Streamlit keeps them from the browser's first request of the session."""
    import streamlit as st
    return st.context.headers


def timezone() -> str | None:
    """The browser's time zone, as Streamlit reports it for the session (an IANA name, America/Chicago); None when the
    browser gave none, or outside a session."""
    import streamlit as st
    try:
        return st.context.timezone or None
    except Exception:  # noqa: BLE001 - no session to ask: no time zone
        return None


# ---- the pump catalogue ----

def fetch_catalogue(client, bucket: str) -> Path | None:
    """With no ESP_EXTRACTION_ASSETS_DIR: the pump catalogue snapshot, the one object <ASSETS_PREFIX><CATALOGUE> of
    `bucket`, into a new temporary folder that ESP_EXTRACTION_ASSETS_DIR then names, checked against the SHA-256 its
    manifest records by the loader every reader of it goes through (as lambda_function.fetch_assets does).  Once it is
    there the variable is set, so a process fetches it once.  Nothing else under assets/ is read.  A failure (the
    object not there or not allowed, a hash that is not the manifest's) leaves the variable unset and nothing on disk,
    and is raised: the app then has no catalogue, as on a computer without one.  None when the variable is already
    set."""
    environ = os.environ                    # the loader reads the variable there
    with _FETCH:
        if environ.get(ASSETS_ENV):
            return None
        folder = Path(tempfile.mkdtemp(prefix="esp-design-extraction-assets-"))
        try:
            body = client.get_object(Bucket=bucket, Key=C.ASSETS_PREFIX + CATALOGUE)["Body"].read()
            (folder / CATALOGUE).write_bytes(body)
            environ[ASSETS_ENV] = str(folder)
            return asset(CATALOGUE)                 # the loader: the file's SHA-256 against the manifest's
        except BaseException:
            environ.pop(ASSETS_ENV, None)
            shutil.rmtree(folder, ignore_errors=True)
            raise
