export interface Service {
  id: string;
  name: string;
  description: string | null;
  endpoint_url: string;
  category: "inference" | "data" | "translation";
  coverage_tier: 0 | 1 | 2 | 3;
  pricing_usdc: number | null;
  source: string;
  payment_protocol: 'wayforth' | 'x402';
}

export interface SearchResult extends Service {
  score: number;
  reason: string;
  wayforth_id?: string;
  wri?: number;
}

export interface SearchResponse {
  query: string;
  total_results: number;
  results: SearchResult[];
}

export interface HealthResponse {
  status: string;
  service: string;
  version: string;
  db_status: string;
}

export interface ServicesResponse {
  total: number;
  offset: number;
  limit: number;
  results: Service[];
}

export interface StatsResponse {
  total_services: number;
  by_tier: Record<string, number>;
  by_category: Record<string, number>;
  tier2_services: string[];
  last_updated: string | null;
}
