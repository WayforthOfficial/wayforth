export class WayforthError extends Error {
  constructor(
    message: string,
    public readonly statusCode?: number,
  ) {
    super(message);
    this.name = "WayforthError";
    Object.setPrototypeOf(this, new.target.prototype);
  }
}

export class AuthenticationError extends WayforthError {
  constructor(message = "Invalid or missing API key") {
    super(message, 401);
    this.name = "AuthenticationError";
    Object.setPrototypeOf(this, new.target.prototype);
  }
}

export class InsufficientCreditsError extends WayforthError {
  constructor(message = "Insufficient credits") {
    super(message, 402);
    this.name = "InsufficientCreditsError";
    Object.setPrototypeOf(this, new.target.prototype);
  }
}

export class ServiceUnavailableError extends WayforthError {
  constructor(message = "Service unavailable") {
    super(message, 503);
    this.name = "ServiceUnavailableError";
    Object.setPrototypeOf(this, new.target.prototype);
  }
}
