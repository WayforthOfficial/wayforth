class WayforthError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class AuthenticationError(WayforthError):
    pass


class InsufficientCreditsError(WayforthError):
    pass


class ServiceUnavailableError(WayforthError):
    pass
