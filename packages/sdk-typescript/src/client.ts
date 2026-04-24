import { Service, SearchResponse, HealthResponse } from "./types";

const DEFAULT_BASE_URL = "https://api-production-fd71.up.railway.app";

export class WayforthClient {
  private baseUrl: string;

  constructor(baseUrl: string = DEFAULT_BASE_URL) {
    this.baseUrl = baseUrl.replace(/\/$/, "");
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
    limit?: number;
  }): Promise<Service[]> {
    const params = new URLSearchParams();
    if (options?.category) params.set("category", options.category);
    if (options?.limit) params.set("limit", String(options.limit));
    const res = await fetch(`${this.baseUrl}/services?${params}`);
    if (!res.ok) throw new Error(`Wayforth API error: ${res.status}`);
    return res.json() as Promise<Service[]>;
  }

  async status(): Promise<HealthResponse> {
    const res = await fetch(`${this.baseUrl}/health`);
    if (!res.ok) throw new Error(`Wayforth API error: ${res.status}`);
    return res.json() as Promise<HealthResponse>;
  }
}
