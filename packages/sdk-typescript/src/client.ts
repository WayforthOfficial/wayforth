import {
  AuthenticationError,
  InsufficientCreditsError,
  ServiceUnavailableError,
  WayforthError,
} from "./errors";
import { fetchWithRetry } from "./retry";
import type {
  AgentIdentity,
  BalanceResult,
  CompareResponse,
  ExecuteResult,
  HealthResponse,
  RunResult,
  SearchResponse,
  ServicesResponse,
  SimilarResponse,
  StatsResponse,
  TiersResponse,
  WayforthQLQuery,
} from "./types";

const DEFAULT_BASE_URL = "https://gateway.wayforth.io";
const DEFAULT_TIMEOUT_MS = 30_000;

async function raiseForStatus(res: Response): Promise<void> {
  if (res.status < 400) return;
  let detail: string;
  try {
    const body = await res.clone().json() as Record<string, unknown>;
    detail = (typeof body.detail === "string" ? body.detail : null) ?? res.statusText;
  } catch {
    detail = res.statusText;
  }
  if (res.status === 401) throw new AuthenticationError(detail);
  if (res.status === 402) {
    try {
      const body = await res.clone().json() as Record<string, unknown>;
      throw new InsufficientCreditsError(
        typeof body.error === "string" ? body.error : detail,
        typeof body.credits_remaining === "number" ? body.credits_remaining : undefined,
        typeof body.credits_required === "number" ? body.credits_required : undefined,
        typeof body.upgrade_url === "string" ? body.upgrade_url : undefined,
      );
    } catch (e) {
      if (e instanceof InsufficientCreditsError) throw e;
      throw new InsufficientCreditsError(detail);
    }
  }
  if (res.status >= 500) throw new ServiceUnavailableError(detail);
  throw new WayforthError(detail, res.status);
}

export class WayforthClient {
  private baseUrl: string;
  private headers: Record<string, string>;
  private timeoutMs: number;

  constructor(
    apiKey: string,
    baseUrl: string = DEFAULT_BASE_URL,
    timeoutMs: number = DEFAULT_TIMEOUT_MS,
  ) {
    this.baseUrl = baseUrl.replace(/\/$/, "");
    this.timeoutMs = timeoutMs;
    this.headers = {
      "x-wayforth-api-key": apiKey,
      "Content-Type": "application/json",
    };
  }

  // Wrap a request with a client-side timeout. An aborted request surfaces as
  // ServiceUnavailableError (the typed hierarchy), not a raw AbortError — the
  // backoff retries in fetchWithRetry run inside this single time budget.
  private async _fetch(url: string, init: RequestInit): Promise<Response> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      return await fetchWithRetry(url, { ...init, signal: controller.signal });
    } catch (err) {
      if (err instanceof Error && err.name === "AbortError") {
        throw new ServiceUnavailableError(`Request timed out after ${this.timeoutMs}ms`);
      }
      throw err;
    } finally {
      clearTimeout(timer);
    }
  }

  private async get<T>(path: string, params?: Record<string, unknown>): Promise<T> {
    const url = new URL(`${this.baseUrl}${path}`);
    if (params) {
      Object.entries(params).forEach(([k, v]) => {
        if (v !== undefined) url.searchParams.set(k, String(v));
      });
    }
    const res = await this._fetch(url.toString(), { headers: this.headers });
    await raiseForStatus(res);
    return res.json() as Promise<T>;
  }

  private async post<T>(path: string, body: unknown): Promise<T> {
    const res = await this._fetch(`${this.baseUrl}${path}`, {
      method: "POST",
      headers: this.headers,
      body: JSON.stringify(body),
    });
    await raiseForStatus(res);
    return res.json() as Promise<T>;
  }

  // ── Search & discovery ────────────────────────────────────────────────────

  async search(
    query: string,
    options?: { category?: string; limit?: number; tier?: number }
  ): Promise<SearchResponse> {
    return this.post<SearchResponse>("/v1/search", { query, ...options });
  }

  async query(params: WayforthQLQuery): Promise<SearchResponse> {
    return this.post<SearchResponse>("/v1/query", { ql: params.query, ...params });
  }

  async listServices(options?: {
    category?: string;
    tier?: number;
    limit?: number;
    offset?: number;
  }): Promise<ServicesResponse> {
    return this.get<ServicesResponse>("/services", options);
  }

  async getService(id: string): Promise<SearchResponse["results"][number] | null> {
    try {
      return await this.get<SearchResponse["results"][number]>(`/services/${id}`);
    } catch (err) {
      if (err instanceof WayforthError && err.statusCode === 404) return null;
      throw err;
    }
  }

  async getSimilar(serviceId: string, limit = 5): Promise<SimilarResponse> {
    return this.get<SimilarResponse>(`/services/similar/${serviceId}`, { limit });
  }

  /** Compare 2–5 services side by side (WRI, cost, signals, response time) with a
   *  recommendation. Maps to GET /compare?slugs=a,b. */
  async compare(...slugs: string[]): Promise<CompareResponse> {
    return this.get<CompareResponse>("/compare", { slugs: slugs.join(",") });
  }

  async getTiers(): Promise<TiersResponse> {
    return this.get<TiersResponse>("/keys/tiers");
  }

  async stats(): Promise<StatsResponse> {
    return this.get<StatsResponse>("/stats");
  }

  async status(): Promise<HealthResponse> {
    return this.get<HealthResponse>("/health");
  }

  // ── Execution ─────────────────────────────────────────────────────────────

  async execute(slug: string, params: Record<string, unknown> = {}): Promise<ExecuteResult> {
    return this.post<ExecuteResult>(`/v1/execute/${slug}`, params);
  }

  async run(query: string, params: Record<string, unknown> = {}): Promise<RunResult> {
    return this.post<RunResult>("/v1/run", { query, ...params });
  }

  // ── Account ───────────────────────────────────────────────────────────────

  async balance(): Promise<BalanceResult> {
    return this.get<BalanceResult>("/v1/balance");
  }

  // ── Agent identity ────────────────────────────────────────────────────────

  async getIdentity(agentId: string): Promise<AgentIdentity> {
    return this.get<AgentIdentity>(`/identity/${agentId}`);
  }

  async registerIdentity(agentId: string, displayName = ""): Promise<AgentIdentity> {
    return this.post<AgentIdentity>("/identity/register", {
      agent_id: agentId,
      display_name: displayName,
    });
  }
}
