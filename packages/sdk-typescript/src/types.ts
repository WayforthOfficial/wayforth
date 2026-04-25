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

export interface AgentIdentity {
  agent_id: string;
  display_name: string;
  trust_score: number;
  reputation_tier: 'elite' | 'trusted' | 'established' | 'new' | 'unknown';
  total_searches: number;
  total_payments: number;
  total_spend_usdc: number;
  member_since: string;
  last_active: string;
}

export interface WayforthQLQuery {
  query: string;
  tier_min?: number;
  price_max?: number;
  category?: string;
  protocol?: 'wayforth' | 'x402' | 'any';
  sort_by?: 'wri' | 'score' | 'price' | 'tier';
  exclude_ids?: string[];
  limit?: number;
  with_similar?: boolean;
  with_payment_calldata?: boolean;
}

export interface SimilarResponse {
  service_id: string;
  similar: SearchResult[];
  total: number;
}

export interface TiersResponse {
  tiers: Array<{
    name: string;
    monthly_usdc: number;
    rpm: number;
    features: string[];
  }>;
}
