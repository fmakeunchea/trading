// Typed client for the FastAPI backend. Keep the DTOs mirrored with
// api/app/schemas.py — they're the contract between the two services.

const BASE =
  (typeof window === "undefined"
    ? process.env.API_URL_INTERNAL
    : process.env.NEXT_PUBLIC_API_URL) ?? "http://localhost:8000";

async function http<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    cache: "no-store",
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}: ${await res.text()}`);
  return res.json() as Promise<T>;
}

export interface Position {
  symbol: string;
  qty: number;
  avg_price: number | null;
  side: "long" | "short";
  unrealized_pnl: number | null;
}

export interface Trade {
  id: number;
  symbol: string;
  side: "buy" | "sell";
  qty: number;
  avg_fill_price: number | null;
  status: string;
  pnl: number | null;
  occurred_at: string;
}

export interface Incident {
  id: number;
  kind: string;
  severity: string;
  phase: string | null;
  symbols: string[];
  reason: string | null;
  payload: Record<string, unknown>;
  occurred_at: string;
}

export interface Strategy {
  id: number;
  name: string;
  mode: "paper" | "live";
  symbols: string[];
  daily_loss_cap_pct: number;
  max_concurrent_positions: number;
  position_notional_pct: number;
  is_active: boolean;
  applied_at: string | null;
  last_restart_at: string | null;
  updated_at: string | null;
  dirty: boolean;
  restart_required: boolean;
}

export interface BotStatus {
  running: boolean;
  mode: string | null;
  kill_switch_engaged: boolean;
  heartbeat_at: string | null;
  heartbeat_fresh: boolean;
  reconcile_ok: boolean | null;
  broker_connected: boolean | null;
  open_positions_count: number;
  incidents_today: number;     // critical only
  diagnostics_today: number;   // observability records
}

export type IncidentCategory = "critical" | "diagnostic" | "all";

export interface SmokeTestResult {
  id: number;
  status: "running" | "pass" | "fail" | "skip" | "error";
  started_at: string;
  finished_at: string | null;
  exit_code: number | null;
  stdout: string | null;
  stderr: string | null;
}

export interface ActionResult {
  ok: boolean;
  message: string | null;
}

export const api = {
  status: () => http<BotStatus>("/status"),
  positions: () => http<Position[]>("/positions"),
  orders: (limit = 50) => http<Trade[]>(`/orders?limit=${limit}`),
  incidents: (limit = 100, category: IncidentCategory = "critical") =>
    http<Incident[]>(`/incidents?limit=${limit}&category=${category}`),
  strategies: () => http<Strategy[]>("/strategies"),
  updateStrategy: (
    id: number,
    body: Pick<
      Strategy,
      | "name"
      | "mode"
      | "symbols"
      | "daily_loss_cap_pct"
      | "max_concurrent_positions"
      | "position_notional_pct"
    >,
  ) => http<Strategy>(`/strategies/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  applyStrategy: (id: number) =>
    http<ActionResult>(`/strategies/${id}/apply`, { method: "POST" }),
  killSwitch: () => http<{ engaged: boolean }>("/kill-switch"),
  setKillSwitch: (engaged: boolean) =>
    http<ActionResult>("/kill-switch", { method: "POST", body: JSON.stringify({ engaged }) }),
  startBot: () => http<ActionResult>("/start-bot", { method: "POST" }),
  stopBot: () => http<ActionResult>("/stop-bot", { method: "POST" }),
  runSmokeTest: () => http<SmokeTestResult>("/run-smoke-test", { method: "POST" }),
  getSmokeTest: (id: number) => http<SmokeTestResult>(`/run-smoke-test/${id}`),
};
