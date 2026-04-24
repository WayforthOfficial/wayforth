export interface Service {
  id: string;
  name: string;
  description: string | null;
  endpoint_url: string;
  category: "inference" | "data" | "translation";
  coverage_tier: 0 | 1 | 2 | 3;
  pricing_usdc: number | null;
  source: string;
}

export interface SearchResult extends Service {
  score: number;
  reason: string;
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
