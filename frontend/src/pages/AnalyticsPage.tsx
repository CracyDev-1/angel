import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { apiGet, type AnalyticsResponse, type AnalyticsTradeRow } from "../lib/api";
import { classOf, formatINR, formatNum, formatPct, formatTime } from "../lib/format";
import StatCard from "../components/StatCard";

type Mode = "live" | "dryrun";

export default function AnalyticsPage() {
  const [mode, setMode] = useState<Mode>("live");

  const q = useQuery({
    queryKey: ["analytics", mode],
    queryFn: () => apiGet<AnalyticsResponse>(`/api/analytics?mode=${mode}`),
    refetchInterval: 15_000,
  });

  const data = q.data;
  const s = data?.summary;
  const isDry = mode === "dryrun";
  const equity = data?.equity_curve ?? [];
  const dailyBars = useMemo(
    () =>
      (data?.equity_curve ?? []).map((row) => ({
        ...row,
        label: shortDay(row.day),
      })),
    [data?.equity_curve],
  );

  const totalClass = classOf(s?.total_pnl ?? null);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-2">
        <ModeTab
          active={mode === "live"}
          onClick={() => setMode("live")}
          label="Live"
          hint="Realized P&amp;L from managed live exits"
        />
        <ModeTab
          active={mode === "dryrun"}
          onClick={() => setMode("dryrun")}
          label="Paper"
          hint="Simulated closes — same logic, no broker fills"
        />
        <div className="ml-auto text-[11px] text-slate-500">
          {isDry ? "Paper analytics exclude live fills." : "Live analytics use closed exit plans in SQLite."}
        </div>
      </div>

      {q.isLoading ? (
        <div className="text-sm text-slate-400">Loading analytics…</div>
      ) : q.isError ? (
        <div className="rounded-lg border border-rose-500/30 bg-rose-950/30 p-4 text-sm text-rose-200">
          Could not load analytics. Is the bot API running?
        </div>
      ) : null}

      <section className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatCard
          label="Total realized P&amp;L"
          value={formatINR(s?.total_pnl ?? 0)}
          hint={`${s?.days_traded ?? 0} days in ledger`}
          valueClass={totalClass}
        />
        <StatCard
          label="Closed trades"
          value={String(s?.total_trades_closed ?? 0)}
          hint={`Win rate ${s?.win_rate != null ? formatPct(s.win_rate) : "—"}`}
          accent="sky"
        />
        <StatCard
          label="Profit factor"
          value={profitFactorLabel(s)}
          hint="Gross profit ÷ gross loss"
          accent="emerald"
        />
        <StatCard
          label="Avg win / loss"
          value={`${formatINR(s?.avg_win ?? null)} / ${formatINR(s?.avg_loss ?? null)}`}
          hint={`${s?.wins ?? 0}W · ${s?.losses ?? 0}L`}
          accent="amber"
        />
      </section>

      <div className="grid gap-6 lg:grid-cols-2">
        <section className="card p-4">
          <h2 className="mb-1 text-sm font-semibold tracking-wide text-slate-200">Cumulative P&amp;L</h2>
          <p className="mb-4 text-[11px] text-slate-500">Running sum of daily realized P&amp;L from the store.</p>
          {equity.length === 0 ? (
            <EmptyChart hint="No daily stats yet — trade for a few sessions to see the curve." />
          ) : (
            <div className="h-56 w-full">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={dailyBars} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                  <XAxis dataKey="label" tick={{ fill: "#64748b", fontSize: 10 }} tickLine={false} />
                  <YAxis
                    tick={{ fill: "#64748b", fontSize: 10 }}
                    tickLine={false}
                    tickFormatter={(v) => formatINR(Number(v), { compact: true })}
                  />
                  <Tooltip content={<PnlTooltip />} />
                  <Line
                    type="monotone"
                    dataKey="cumulative_pnl"
                    stroke="#2dd4bf"
                    strokeWidth={2}
                    dot={false}
                    activeDot={{ r: 4 }}
                  />
                </LineChart>
              </ResponsiveContainer>
            </div>
          )}
        </section>

        <section className="card p-4">
          <h2 className="mb-1 text-sm font-semibold tracking-wide text-slate-200">Daily P&amp;L</h2>
          <p className="mb-4 text-[11px] text-slate-500">One bar per trading day in the ledger.</p>
          {equity.length === 0 ? (
            <EmptyChart hint="No daily breakdown yet." />
          ) : (
            <div className="h-56 w-full">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={dailyBars} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                  <XAxis dataKey="label" tick={{ fill: "#64748b", fontSize: 10 }} tickLine={false} />
                  <YAxis
                    tick={{ fill: "#64748b", fontSize: 10 }}
                    tickLine={false}
                    tickFormatter={(v) => formatINR(Number(v), { compact: true })}
                  />
                  <Tooltip content={<DailyTooltip />} />
                  <Bar dataKey="pnl" radius={[4, 4, 0, 0]} maxBarSize={48}>
                    {dailyBars.map((row, i) => (
                      <Cell key={i} fill={row.pnl >= 0 ? "#34d399" : "#fb7185"} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>
          )}
        </section>
      </div>

      <section className="card p-4">
        <h2 className="mb-1 text-sm font-semibold tracking-wide text-slate-200">Trade distribution</h2>
        <p className="mb-4 text-[11px] text-slate-500">Per-close outcomes (most recent first).</p>
        {!data?.trades?.length ? (
          <EmptyChart hint="No closed trades in the database for this mode." />
        ) : (
          <div className="h-52 w-full max-w-3xl">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart
                data={tradeBarData(data.trades)}
                margin={{ top: 8, right: 8, left: 0, bottom: 0 }}
              >
                <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                <XAxis dataKey="sym" tick={{ fill: "#64748b", fontSize: 9 }} tickLine={false} interval={0} angle={-35} textAnchor="end" height={56} />
                <YAxis tick={{ fill: "#64748b", fontSize: 10 }} tickLine={false} width={56} tickFormatter={(v) => formatINR(Number(v), { compact: true })} />
                <Tooltip content={<TradeBarTooltip />} />
                  <Bar dataKey="pnl" radius={[2, 2, 0, 0]} maxBarSize={14}>
                    {tradeBarData(data.trades).map((row, i) => (
                      <Cell key={i} fill={row.pnl >= 0 ? "#34d399" : "#fb7185"} />
                    ))}
                  </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        )}
      </section>

      <section className="card overflow-hidden p-0">
        <div className="border-b border-white/5 px-4 py-3">
          <h2 className="text-sm font-semibold tracking-wide text-slate-200">Recent closed trades</h2>
          <p className="text-[11px] text-slate-500">Up to 400 rows from SQLite · newest first</p>
        </div>
        <div className="max-h-[420px] overflow-auto">
          <table className="w-full text-sm">
            <thead className="sticky top-0 z-10 bg-slate-950/95 backdrop-blur">
              <tr>
                <th className="table-th">Closed</th>
                <th className="table-th">Symbol</th>
                <th className="table-th">Side</th>
                <th className="table-th">Qty</th>
                <th className="table-th">Entry</th>
                <th className="table-th">Exit</th>
                <th className="table-th">Reason</th>
                <th className="table-th">P&amp;L</th>
                <th className="table-th">{isDry ? "Source" : "Src"}</th>
              </tr>
            </thead>
            <tbody>
              {(data?.trades ?? []).map((t) => (
                <tr key={`${t.id}-${t.closed_at}`}>
                  <td className="table-td whitespace-nowrap text-[11px] text-slate-400">{formatTime(t.closed_at)}</td>
                  <td className="table-td font-medium text-slate-200">{t.tradingsymbol}</td>
                  <td className="table-td">
                    <span className={t.side === "CE" ? "pill-green" : t.side === "PE" ? "pill-red" : "text-slate-400"}>
                      {t.side || "—"}
                    </span>
                  </td>
                  <td className="table-td">{t.qty}</td>
                  <td className="table-td">{formatINR(t.entry_price)}</td>
                  <td className="table-td">{formatINR(t.exit_price)}</td>
                  <td className="table-td text-[11px] text-slate-400">{t.exit_reason || "—"}</td>
                  <td className={`table-td font-medium ${classOf(t.realized_pnl)}`}>{formatINR(t.realized_pnl)}</td>
                  <td className="table-td text-[11px] text-slate-500">{t.source}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {!data?.trades?.length ? (
            <div className="p-8 text-center text-sm text-slate-500">No rows yet.</div>
          ) : null}
        </div>
      </section>

      <p className="text-[11px] leading-relaxed text-slate-500">
        Analytics read from <code className="text-slate-400">daily_stats_mode</code>,{" "}
        <code className="text-slate-400">live_exit_plans</code> (closed), and{" "}
        <code className="text-slate-400">paper_positions</code> (closed). Live mode merges legacy{" "}
        <code className="text-slate-400">daily_stats</code> when present.
      </p>
    </div>
  );
}

function tradeBarData(trades: AnalyticsTradeRow[]) {
  return [...trades]
    .slice(0, 48)
    .reverse()
    .map((t) => ({
      pnl: t.realized_pnl,
      sym: t.tradingsymbol.length > 12 ? `${t.tradingsymbol.slice(0, 10)}…` : t.tradingsymbol,
    }));
}

function profitFactorLabel(s: AnalyticsResponse["summary"] | undefined): string {
  if (!s) return "—";
  if (s.gross_profit > 0 && s.losses === 0) return "∞";
  if (s.profit_factor != null) return formatNum(s.profit_factor, 2);
  return "—";
}

function shortDay(day: string): string {
  if (!day) return "";
  const iso = day.length === 10 ? `${day}T12:00:00` : day;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return day.slice(0, 10);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function ModeTab({
  active,
  onClick,
  label,
  hint,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
  hint: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`rounded-xl border px-4 py-2 text-left transition ${
        active
          ? "border-sky-400/40 bg-sky-500/10 text-sky-100"
          : "border-white/10 bg-slate-900/40 text-slate-300 hover:border-white/20"
      }`}
    >
      <div className="text-sm font-semibold">{label}</div>
      <div className="text-[11px] text-slate-400">{hint}</div>
    </button>
  );
}

function EmptyChart({ hint }: { hint: string }) {
  return (
    <div className="flex h-56 items-center justify-center rounded-lg border border-dashed border-white/10 bg-slate-900/30 text-center text-sm text-slate-500">
      {hint}
    </div>
  );
}

function PnlTooltip({ active, payload }: { active?: boolean; payload?: { payload: { cumulative_pnl: number; pnl: number; trades: number; day: string } }[] }) {
  if (!active || !payload?.length) return null;
  const p = payload[0].payload;
  return (
    <div className="rounded-lg border border-white/10 bg-slate-900/95 px-3 py-2 text-xs shadow-xl">
      <div className="font-medium text-slate-200">{p.day}</div>
      <div className="mt-1 text-slate-300">Cumulative: {formatINR(p.cumulative_pnl)}</div>
      <div className="text-slate-400">Day: {formatINR(p.pnl)} · {p.trades} trades</div>
    </div>
  );
}

function DailyTooltip({ active, payload }: { active?: boolean; payload?: { payload: { pnl: number; trades: number; day: string } }[] }) {
  if (!active || !payload?.length) return null;
  const p = payload[0].payload;
  return (
    <div className="rounded-lg border border-white/10 bg-slate-900/95 px-3 py-2 text-xs shadow-xl">
      <div className="font-medium text-slate-200">{p.day}</div>
      <div className="mt-1 text-slate-300">{formatINR(p.pnl)}</div>
      <div className="text-slate-400">{p.trades} trades</div>
    </div>
  );
}

function TradeBarTooltip({
  active,
  payload,
}: {
  active?: boolean;
  payload?: { payload: { sym: string; pnl: number } }[];
}) {
  if (!active || !payload?.length) return null;
  const p = payload[0].payload;
  return (
    <div className="rounded-lg border border-white/10 bg-slate-900/95 px-3 py-2 text-xs shadow-xl">
      <div className="font-medium text-slate-200">{p.sym}</div>
      <div className={`mt-1 ${classOf(p.pnl)}`}>{formatINR(p.pnl)}</div>
    </div>
  );
}
