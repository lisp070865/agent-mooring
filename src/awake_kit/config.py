"""Configuration that is empty and disabled by default.

Deliberate properties, each of which is asserted by the offline tests:

* every adapter is disabled unless its config is *explicitly* non-empty and
  ``allow_network`` is exactly ``true``;
* the loader never reads environment variables and never looks for a keyring,
  a browser profile, a private scheduler store, or an account to auto-discover;
* the resolved config has a content hash, so a run can prove later that the
  rules it is obeying are the rules it started with;
* attempting a disabled adapter raises instead of silently doing nothing, so a
  missing configuration can never be mistaken for successful delivery.
"""

from __future__ import annotations

import hashlib
import json
import os

from .errors import NetworkDisabledError

DEFAULTS = {
    "schema": "awake_kit_config_v1",
    "allow_network": False,
    "adapters": {
        "mail": {"enabled": False, "endpoint": "", "inbox": "", "recipient": ""},
        "wake": {"enabled": False, "target_session": ""},
        "schedule": {"enabled": False, "kind": "", "at": ""},
    },
    "limits": {"max_resume_attempts": 2, "deadline_seconds": 900, "lease_seconds": 120},
}


def _merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    def __init__(self, data: dict, source: str = "defaults_only"):
        self.data = data
        self.source = source
        raw = json.dumps(data, sort_keys=True).encode("utf-8")
        self.sha256 = hashlib.sha256(raw).hexdigest()
        self.env_credentials_read = False   # this class never touches os.environ
        self.accounts_auto_discovered = False

    @classmethod
    def load(cls, path=None):
        if path is None:
            return cls(json.loads(json.dumps(DEFAULTS)), source="defaults_only")
        if not os.path.isfile(path):
            return cls(json.loads(json.dumps(DEFAULTS)), source="defaults_only_missing_file")
        with open(path, "rb") as f:
            blob = f.read()
        user = json.loads(blob.decode("utf-8"))
        merged = _merge(DEFAULTS, user)
        cfg = cls(merged, source="file")
        cfg.file_sha256 = hashlib.sha256(blob).hexdigest()
        return cfg

    # ---- network gates ----
    @property
    def network_allowed(self) -> bool:
        return self.data.get("allow_network") is True

    def adapter(self, name: str) -> dict:
        return self.data["adapters"][name]

    def require(self, name: str, **needed_non_empty):
        """Return adapter settings or raise. No silent no-op path exists."""
        ad = self.adapter(name)
        if ad.get("enabled") is not True:
            raise NetworkDisabledError("ADAPTER_DISABLED", adapter=name,
                                       enabled=ad.get("enabled"),
                                       hint="set enabled=true and supply non-empty settings")
        if not self.network_allowed:
            raise NetworkDisabledError("GLOBAL_NETWORK_NOT_ALLOWED", adapter=name,
                                       allow_network=self.data.get("allow_network"))
        for key, _ in needed_non_empty.items():
            if not str(ad.get(key, "")).strip():
                raise NetworkDisabledError(f"ADAPTER_SETTING_EMPTY_{key.upper()}", adapter=name)
        return ad

    def limits(self) -> dict:
        return dict(self.data["limits"])
