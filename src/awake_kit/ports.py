"""Public port abstractions plus disabled defaults and offline fakes.

Three contracts, deliberately small:

``MailPort``      hand an outbound payload to a transport.
``WakePort``      ask a scheduler/client to start or resume an executor.
``SchedulePort``  book one future execution.

Rules encoded here:

* the *default* implementation of all three is DISABLED and raises - a missing
  configuration is a loud refusal, never a silent no-op that looks like success;
* nothing in this module opens a socket, reads environment secrets, discovers
  accounts, or touches a keyring;
* ``WakePort`` returning an accepted-shaped response does **not** mean a wake
  happened. Confirmation requires evidence from the woken side (an ack the woken
  executor writes itself), never from a third party parsing private log formats.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field

from .errors import NetworkDisabledError

ACCEPTED_IS_NOT_WOKEN = True
PROVIDER_EVIDENCE_KEYS = ("provider_message_id", "provider_run_id", "provider_receipt")


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------- protocols
class MailPort:
    def send(self, *, to: str, subject: str, body: bytes, attachment_name: str = "",
             attachment_bytes: bytes = b"") -> dict:
        raise NotImplementedError


class WakePort:
    def notify(self, *, target: str, message: bytes) -> dict:
        raise NotImplementedError


class SchedulePort:
    def appoint(self, *, at_utc: str, instruction: bytes) -> dict:
        raise NotImplementedError


# ---------------------------------------------------------------- disabled
class DisabledMail(MailPort):
    def send(self, **kw):
        raise NetworkDisabledError("MAIL_ADAPTER_DISABLED_BY_DEFAULT",
                                   adapter="mail", attempted="send",
                                   remedy="provide explicit non-empty config AND allow_network=true")


class DisabledWake(WakePort):
    def notify(self, **kw):
        raise NetworkDisabledError("WAKE_ADAPTER_DISABLED_BY_DEFAULT",
                                   adapter="wake", attempted="notify",
                                   remedy="provide explicit non-empty config AND allow_network=true")


class DisabledSchedule(SchedulePort):
    def appoint(self, **kw):
        raise NetworkDisabledError("SCHEDULE_ADAPTER_DISABLED_BY_DEFAULT",
                                   adapter="schedule", attempted="appoint",
                                   remedy="provide explicit non-empty config AND allow_network=true")


def build_ports(config):
    """Report which adapters *would* be permitted, but still return disabled ones.

    There is no auto-discovery branch on purpose: even a fully populated config
    does not silently acquire a live client. Wiring a real transport is an
    explicit, reviewable act performed by the integrator outside this package.
    """
    gates = {}
    for name in ("mail", "wake", "schedule"):
        try:
            config.require(name)
            gates[name] = "CONFIG_OK_INJECTION_STILL_REQUIRED"
        except NetworkDisabledError as exc:
            gates[name] = f"REFUSED_{exc.reason}"
    return {"mail": DisabledMail(), "wake": DisabledWake(), "schedule": DisabledSchedule(),
            "gates": gates,
            "note": "no adapter is auto-discovered; inject a real one explicitly"}


# ---------------------------------------------------------------- fakes
@dataclass
class FakeMail(MailPort):
    """Scriptable offline transport. ``script`` items: ok | unknown | rejected."""
    script: list = field(default_factory=lambda: ["ok"])
    calls: list = field(default_factory=list)

    def send(self, *, to: str, subject: str, body: bytes, attachment_name: str = "",
             attachment_bytes: bytes = b"") -> dict:
        mode = self.script[min(len(self.calls), len(self.script) - 1)]
        self.calls.append({"to": to, "subject": subject,
                           "body_sha256": hashlib.sha256(_as_bytes(body)).hexdigest(),
                           "attachment_sha256": hashlib.sha256(_as_bytes(attachment_bytes)).hexdigest(),
                           "at": _now(), "mode": mode})
        if mode == "ok":
            return {"provider_message_id": f"synthetic-<{'0' * 8}>-{len(self.calls)}@example.invalid",
                    "at": _now()}
        if mode == "rejected":
            return {"error": "SYNTHETIC_REJECTED_4xx", "at": _now()}
        # "unknown": an error AFTER the provider may already have accepted it
        return {"error": "SYNTHETIC_TIMEOUT_AFTER_POSSIBLE_DELIVERY",
                "provider_evidence": "partial-response-without-id", "at": _now()}


@dataclass
class FakeWake(WakePort):
    script: list = field(default_factory=lambda: ["accepted"])
    calls: list = field(default_factory=list)

    def notify(self, *, target: str, message: bytes) -> dict:
        mode = self.script[min(len(self.calls), len(self.script) - 1)]
        self.calls.append({"target": target, "sha256": hashlib.sha256(_as_bytes(message)).hexdigest(),
                           "at": _now(), "mode": mode})
        if mode == "accepted":
            return {"accepted": True, "target": target, "at": _now(),
                    "meaning": "queued_only_not_woken"}
        if mode == "rejected":
            return {"error": "SYNTHETIC_WAKE_REFUSED", "target": target, "at": _now()}
        return {"error": "SYNTHETIC_WAKE_TIMEOUT", "provider_evidence": "unknown", "at": _now()}


@dataclass
class FakeSchedule(SchedulePort):
    script: list = field(default_factory=lambda: ["created"])
    calls: list = field(default_factory=list)

    def appoint(self, *, at_utc: str, instruction: bytes) -> dict:
        mode = self.script[min(len(self.calls), len(self.script) - 1)]
        self.calls.append({"at_utc": at_utc,
                           "instruction_sha256": hashlib.sha256(_as_bytes(instruction)).hexdigest(),
                           "at": _now(), "mode": mode})
        if mode == "created":
            return {"appointment_id": f"synthetic-appt-{len(self.calls):03d}", "at_utc": at_utc,
                    "revision": 1, "at": _now()}
        if mode == "rejected":
            return {"error": "SYNTHETIC_SCHEDULE_REFUSED", "at": _now()}
        return {"error": "SYNTHETIC_SCHEDULE_TIMEOUT", "provider_evidence": "unknown", "at": _now()}


def _as_bytes(v) -> bytes:
    if isinstance(v, bytes):
        return v
    if isinstance(v, str):
        return v.encode("utf-8")
    return repr(v).encode("utf-8")
