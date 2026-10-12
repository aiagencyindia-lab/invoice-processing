"""Turn a credential held as an environment variable into one Google can use.

Platforms hold configuration as environment variables; the Google libraries
want `GOOGLE_APPLICATION_CREDENTIALS` pointing at a file. Something has to
bridge the two, and the obvious place -- a shell entrypoint in the image -- is
the wrong one: a platform that lets a service override its start command (as
Railway does, and as most do) replaces the entrypoint with it, and the bridge
silently disappears. The API kept working because it uses the image's default
command; the worker, which needs a custom one, ran without credentials and
failed on the first call that needed them.

So it lives here, in Python, on the path every entry point already takes.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


class CredentialError(RuntimeError):
    """The credential was present and unusable, which is worth saying loudly.

    Refusing beats starting and failing on the first Google call: the second
    buries the cause under whatever the caller happened to be doing.
    """


VARIABLE = "GOOGLE_CREDENTIALS_JSON"
TARGET = "GOOGLE_APPLICATION_CREDENTIALS"


def materialise_credentials(path: str | Path | None = None) -> str | None:
    """Write the credential to a file and point Google's libraries at it.

    Returns the path written, or None when there was nothing to write or a
    real credential file is already configured. Never logs the value: "no
    credential" and "a bad credential" fail differently and the logs should
    say which, without saying what.
    """
    raw = os.getenv(VARIABLE, "").strip()
    if not raw:
        logger.info(
            "Credentials: %s is not set; relying on ambient credentials.",
            VARIABLE,
        )
        return None

    existing = os.getenv(TARGET, "").strip()
    if existing and Path(existing).exists():
        # An explicitly mounted key file outranks a variable: a deployment
        # that went to the trouble of mounting one means it.
        logger.info("Credentials: using the file already at %s.", TARGET)
        return existing

    # Check it before writing it. Handed a placeholder -- a truncated paste,
    # or a literal "<your key here>" -- google-auth fails three frames deep
    # with "is not a valid json file", naming the file we just wrote rather
    # than the variable that was wrong. Say which, and say it here.
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise CredentialError(
            f"{VARIABLE} is set but is not valid JSON ({exc}). It should be "
            f"the whole service-account key file, starting with a brace. Got "
            f"{len(raw)} characters beginning {raw[:1]!r}."
        ) from exc

    if not isinstance(parsed, dict) or "private_key" not in parsed:
        raise CredentialError(
            f"{VARIABLE} parsed as JSON but is not a service-account key: no "
            f"'private_key' field."
        )

    target = Path(
        path
        or os.getenv("GOOGLE_CREDENTIALS_PATH")
        # Written mode 600 below; GOOGLE_CREDENTIALS_PATH overrides it.
        or "/tmp/gcp-credentials.json"  # noqa: S108
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(raw, encoding="utf-8")
    # The file holds a private key: readable by this process and nothing else.
    target.chmod(0o600)
    os.environ[TARGET] = str(target)
    logger.info("Credentials: wrote %s to %s", VARIABLE, target)
    return str(target)
