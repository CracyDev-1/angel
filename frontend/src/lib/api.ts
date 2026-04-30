const TOKEN_STORAGE_KEY = "angel.dashboardToken";

export function getDashboardToken(): string {
  return (localStorage.getItem(TOKEN_STORAGE_KEY) || "").trim();
}

export function setDashboardToken(value: string): void {
  if (value && value.trim()) {
    localStorage.setItem(TOKEN_STORAGE_KEY, value.trim());
  } else {
    localStorage.removeItem(TOKEN_STORAGE_KEY);
  }
}

function headers(extra?: HeadersInit): HeadersInit {
  const h: Record<string, string> = { "Content-Type": "application/json" };
  const tok = getDashboardToken();
  if (tok) h["X-Dashboard-Token"] = tok;
  if (extra) Object.assign(h, extra as Record<string, string>);
  return h;
}

async function parse<T>(r: Response): Promise<T> {
  const text = await r.text();
  let payload: unknown = undefined;
  try {
    payload = text ? JSON.parse(text) : undefined;
  } catch {
    /* keep raw text below */
  }
  if (!r.ok) {
    const detail =
      (payload && typeof payload === "object" && (payload as { detail?: string }).detail) ||
      r.statusText ||
      `HTTP ${r.status}`;
    const err = new Error(String(detail)) as Error & { status?: number };
    err.status = r.status;
    throw err;
  }
  return payload as T;
}

export async function apiGet<T>(path: string): Promise<T> {
  const r = await fetch(path, { headers: headers() });
  return parse<T>(r);
}

export async function apiPost<T>(path: string, body?: unknown): Promise<T> {
  const r = await fetch(path, {
    method: "POST",
    headers: headers(),
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  return parse<T>(r);
}

export type StatusResponse = {
  connected: boolean;
  bot_running: boolean;
  last_error: string | null;
  clientcode: string | null;
  trading_enabled: boolean;
  auto_mode: boolean;
};

export type FundsResponse = {
  available_cash: number;
  net: number;
  utilised_margin: number;
  available_margin?: number;
  raw?: unknown;
  error?: string;
};

export type PositionRow = {
  tradingsymbol: string;
  exchange: string;
  symboltoken: string;
  side: "CE" | "PE" | "-";
  net_qty: number;
  buy_qty: number;
  sell_qty: number;
  buy_avg: number | null;
  sell_avg: number | null;
  ltp: number | null;
  capital_used: number;
  pnl: number | null;
  producttype?: string | null;
};

export type PositionsResponse = {
  rows: PositionRow[];
  open_positions: number;
  capital_used_ce: number;
  capital_used_pe: number;
  capital_used_total: number;
  pnl_total: number;
  error?: string;
};

export type ScannerHit = {
  name: string;
  exchange: string;
  token: string;
  kind: string;
  last_price: number | null;
  change_pct: number | null;
  momentum_5: number | null;
  score: number;
  lot_size: number | null;
  notional_per_lot: number | null;
  affordable_lots: number | null;
  as_of: string;
};

export type DecisionRow = {
  ts: string;
  name: string;
  exchange: string;
  token: string;
  signal: "BUY_CALL" | "BUY_PUT" | "NO_TRADE";
  reason: string;
  last_price: number | null;
  quantity: number;
  lots: number;
  capital_used: number;
  side: "CE" | "PE" | "-";
  placed: boolean;
  dry_run: boolean;
  broker_order_id?: string | null;
};

export type OrderRow = {
  id: string | number | null;
  lifecycle: string | null;
  broker_status: string | null;
  filled_qty: number | null;
  pending_qty: number | null;
  avg_price: number | null;
  updated_at: string | null;
  raw?: unknown;
};

export type StatsResponse = {
  trades: number;
  realized_pnl: number;
  loss_limit: number;
  max_trades: number;
  all_days: { day: string; trades: number; pnl: number }[];
};

export type ConfigResponse = {
  trading_enabled: boolean;
  loop_interval_s: number;
  max_concurrent_positions: number;
  use_capital_pct: number;
  min_signal_strength: number;
  default_product: string;
  default_variety: string;
  watchlist: Record<string, { name: string; token: string; kind: string; lot_size: number }[]>;
  risk: {
    capital_rupees: number;
    per_trade_pct: number;
    max_daily_loss_pct: number;
    max_trades_per_day: number;
    one_position_at_a_time: boolean;
  };
};

export type Snapshot = {
  connected: boolean;
  bot_running: boolean;
  trading_enabled: boolean;
  auto_mode: boolean;
  last_loop_at: string | null;
  bot_started_at: string | null;
  last_error: string | null;
  clientcode: string | null;
  funds: FundsResponse | null;
  positions: PositionsResponse | null;
  scanner: ScannerHit[];
  recent_orders: OrderRow[];
  decisions: DecisionRow[];
  daily: StatsResponse;
};
