class WayforthError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class AuthenticationError(WayforthError):
    pass


class InsufficientCreditsError(WayforthError):
    def __init__(
        self,
        message: str,
        status_code: int | None = 402,
        *,
        credits_remaining: int | None = None,
        credits_required: int | None = None,
        upgrade_url: str | None = None,
    ) -> None:
        super().__init__(message, status_code)
        self.credits_remaining = credits_remaining
        self.credits_required = credits_required
        self.upgrade_url = upgrade_url


class ServiceUnavailableError(WayforthError):
    pass
