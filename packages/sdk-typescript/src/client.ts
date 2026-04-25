import { Service, SearchResponse, ServicesResponse, StatsResponse, HealthResponse, AgentIdentity, WayforthQLQuery, SimilarResponse, TiersResponse } from "./types";

const DEFAULT_BASE_URL = "https://api-production-fd71.up.railway.app";

export class WayforthClient {
  private baseUrl: string;

  constructor(baseUrl: string = DEFAULT_BASE_URL) {
    this.baseUrl = baseUrl.replace(/\/$/, "");
  }

  private async get<T>(path: string, params?: Record<string, unknown>): Promise<T> {
    const url = new URL(`${this.baseUrl}${path}`);
    if (params) {
      Object.entries(params).forEach(([k, v]) => url.searchParams.set(k, String(v)));
    }
    const res = await fetch(url.toString());
    if (!res.ok) throw new Error(`Wayforth API error: ${res.status}`);
    return res.json() as Promise<T>;
  }

  private async post<T>(path: string, body: unknown): Promise<T> {
    const res = await fetch(`${this.baseUrl}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`Wayforth API error: ${res.status}`);
    return res.json() as Promise<T>;
  }

  async search(
    query: string,
    options?: { category?: string; limit?: number; tier?: number }
  ): Promise<SearchResponse> {
    const params = new URLSearchParams({ q: query });
    if (options?.category) params.set("category", options.category);
    if (options?.limit) params.set("limit", String(options.limit));
    if (options?.tier !== undefined) params.set("tier", String(options.tier));
    const res = await fetch(`${this.baseUrl}/search?${params}`);
    if (!res.ok) throw new Error(`Wayforth API error: ${res.status}`);
    return res.json() as Promise<SearchResponse>;
  }

  async listServices(options?: {
    category?: string;
    tier?: number;
    limit?: number;
    offset?: number;
  }): Promise<ServicesResponse> {
    const params = new URLSearchParams();
    if (options?.category) params.set("category", options.category);
    if (options?.tier !== undefined) params.set("tier", String(options.tier));
    if (options?.limit) params.set("limit", String(options.limit));
    if (options?.offset !== undefined) params.set("offset", String(options.offset));
    const res = await fetch(`${this.baseUrl}/services?${params}`);
    if (!res.ok) throw new Error(`Wayforth API error: ${res.status}`);
    return res.json() as Promise<ServicesResponse>;
  }

  async getService(id: string): Promise<Service> {
    const res = await fetch(`${this.baseUrl}/services/${id}`);
    if (res.status === 404) throw new Error(`Service not found: ${id}`);
    if (!res.ok) throw new Error(`Wayforth API error: ${res.status}`);
    return res.json() as Promise<Service>;
  }

  async stats(): Promise<StatsResponse> {
    const res = await fetch(`${this.baseUrl}/stats`);
    if (!res.ok) throw new Error(`Wayforth API error: ${res.status}`);
    return res.json() as Promise<StatsResponse>;
  }

  async status(): Promise<HealthResponse> {
    const res = await fetch(`${this.baseUrl}/health`);
    if (!res.ok) throw new Error(`Wayforth API error: ${res.status}`);
    return res.json() as Promise<HealthResponse>;
  }

  async getIdentity(agentId: string): Promise<AgentIdentity> {
    return this.get<AgentIdentity>(`/identity/${agentId}`);
  }

  async registerIdentity(agentId: string, displayName?: string): Promise<AgentIdentity> {
    return this.post<AgentIdentity>('/identity/register', {
      agent_id: agentId,
      display_name: displayName || ''
    });
  }

  async query(params: WayforthQLQuery): Promise<SearchResponse> {
    return this.post<SearchResponse>('/query', params);
  }

  async getSimilar(serviceId: string, limit = 5): Promise<SimilarResponse> {
    return this.get<SimilarResponse>(`/services/similar/${serviceId}`, { limit });
  }

  async getTiers(): Promise<TiersResponse> {
    return this.get<TiersResponse>('/keys/tiers');
  }
}
