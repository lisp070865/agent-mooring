"""Error types. Every refusal carries a machine-readable reason code."""


class KitError(Exception):
    code = "KIT_ERROR"

    def __init__(self, reason, **detail):
        self.reason = reason
        self.detail = detail
        self.code = getattr(type(self), "code", self.code)
        super().__init__(f"{type(self).__name__}/{reason} {detail}")


class IdentityRejected(KitError):
    code = "IDENTITY_REJECTED"


class BoundaryViolation(KitError):
    code = "BOUNDARY_VIOLATION"


class ClaimConflict(KitError):
    code = "CLAIM_CONFLICT"


class ImmutableResultViolation(KitError):
    code = "IMMUTABLE_RESULT"


class LiveWriterPresent(KitError):
    code = "LIVE_WRITER_PRESENT"


class NetworkDisabledError(KitError):
    code = "NETWORK_DISABLED"


class StoppedError(KitError):
    code = "STOPPED"


class BudgetExhausted(KitError):
    code = "BUDGET_EXHAUSTED"


class ConfigIdentityMismatch(KitError):
    code = "CONFIG_IDENTITY_MISMATCH"


class ResendForbidden(KitError):
    code = "RESEND_FORBIDDEN"
