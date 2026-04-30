import { useMemo } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPost, type Snapshot } from "../lib/api";
import { formatINR, formatTime, classOf } from "../lib/format";
import Header from "../components/Header";
import StatCard from "../components/StatCard";
import ScannerTable from "../components/ScannerTable";
import PositionsPanel from "../components/PositionsPanel";
import OrdersTable from "../components/OrdersTable";
import DecisionsTable from "../components/DecisionsTable";

export default function DashboardPage() {
  const qc = useQueryClient();
  const snap = useQuery({
    queryKey: ["snapshot"],
    queryFn: () => apiGet<Snapshot>("/api/snapshot"),
    refetchInterval: 4000,
  });

  const startBot = useMutation({
    mutationFn: () => apiPost("/api/bot/start"),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["snapshot"] }),
  });
  const stopBot = useMutation({
    mutationFn: () => apiPost("/api/bot/stop"),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["snapshot"] }),
  });
  const disconnect = useMutation({
    mutationFn: () => apiPost("/api/disconnect"),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["status"] }),
  });

  const data = snap.data;
  const funds = data?.funds;
  const positions = data?.positions;
  const daily = data?.daily;

  const pnlClass = useMemo(() => classOf(positions?.pnl_total ?? null), [positions?.pnl_total]);
  const realizedClass = useMemo(() => classOf(daily?.realized_pnl ?? null), [daily?.realized_pnl]);

  return (
    <div className="mx-auto max-w-7xl px-4 py-6">
      <Header
        connected={!!data?.connected}
        botRunning={!!data?.bot_running}
        tradingEnabled={!!data?.trading_enabled}
        autoMode={!!data?.auto_mode}
        clientCode={data?.clientcode || null}
        lastError={data?.last_error || null}
        lastLoopAt={data?.last_loop_at || null}
        botStartedAt={data?.bot_started_at || null}
        onStart={() => startBot.mutate()}
        onStop={() => stopBot.mutate()}
        onDisconnect={() => disconnect.mutate()}
        starting={startBot.isPending}
        stopping={stopBot.isPending}
      />

      <section className="mt-6 grid grid-cols-2 gap-4 lg:grid-cols-5">
        <StatCard
          label="Available cash"
          value={formatINR(funds?.available_cash ?? 0)}
          hint={`Net ${formatINR(funds?.net ?? 0, { compact: true })}`}
        />
        <StatCard
          label="Capital used (CE)"
          value={formatINR(positions?.capital_used_ce ?? 0)}
          hint="Long calls notional"
          accent="emerald"
        />
        <StatCard
          label="Capital used (PE)"
          value={formatINR(positions?.capital_used_pe ?? 0)}
          hint="Long puts notional"
          accent="rose"
        />
        <StatCard
          label="Open P&L (live)"
          value={formatINR(positions?.pnl_total ?? 0)}
          hint={`${positions?.open_positions ?? 0} open positions`}
          valueClass={pnlClass}
        />
        <StatCard
          label="Realized today"
          value={formatINR(daily?.realized_pnl ?? 0)}
          hint={`${daily?.trades ?? 0} / ${daily?.max_trades ?? 0} trades`}
          valueClass={realizedClass}
        />
      </section>

      <section className="mt-6 grid grid-cols-1 gap-4 lg:grid-cols-3">
        <div className="card lg:col-span-2 p-4">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-sm font-semibold tracking-wide text-slate-200">Scanner — hot instruments</h2>
            <span className="text-xs text-slate-500">last loop {formatTime(data?.last_loop_at)}</span>
          </div>
          <ScannerTable hits={data?.scanner ?? []} />
          <p className="mt-2 text-xs text-slate-500">
            Heuristic ranking from change% + short-window momentum on the configured watchlist.
            “Affordable lots” is computed against your available cash and each instrument&apos;s lot size.
          </p>
        </div>

        <div className="card p-4">
          <h2 className="mb-3 text-sm font-semibold tracking-wide text-slate-200">Daily summary</h2>
          <div className="space-y-2 text-sm">
            <Row label="Trades today" value={`${daily?.trades ?? 0}`} />
            <Row label="Realized P&L" value={formatINR(daily?.realized_pnl ?? 0)} className={realizedClass} />
            <Row label="Loss limit" value={formatINR(daily?.loss_limit ?? 0)} />
            <Row label="Max trades / day" value={`${daily?.max_trades ?? 0}`} />
          </div>
          {data?.daily?.all_days?.length ? (
            <div className="mt-4">
              <div className="stat-label mb-2">Last 30 days</div>
              <div className="max-h-48 overflow-auto pr-1">
                <table className="w-full text-sm">
                  <thead>
                    <tr>
                      <th className="table-th">Date</th>
                      <th className="table-th">Trades</th>
                      <th className="table-th">P&amp;L</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.daily.all_days.map((d) => (
                      <tr key={d.day}>
                        <td className="table-td text-slate-300">{d.day}</td>
                        <td className="table-td">{d.trades}</td>
                        <td className={`table-td ${classOf(d.pnl)}`}>{formatINR(d.pnl)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ) : null}
        </div>
      </section>

      <section className="mt-6 grid grid-cols-1 gap-4 lg:grid-cols-2">
        <PositionsPanel positions={positions ?? null} />
        <OrdersTable orders={data?.recent_orders ?? []} />
      </section>

      <section className="mt-6">
        <div className="card p-4">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-sm font-semibold tracking-wide text-slate-200">Bot decisions (live)</h2>
            <span className="text-xs text-slate-500">
              dry-run shows what the bot WOULD do; turn on TRADING_ENABLED in .env to send live orders.
            </span>
          </div>
          <DecisionsTable rows={data?.decisions ?? []} />
        </div>
      </section>

      <footer className="mt-8 pb-6 text-center text-[11px] text-slate-500">
        UI updates every ~4s. P&amp;L numbers are estimates from broker positions / orders. No system
        guarantees profits — keep your risk caps tight and review trades manually.
        {data?.last_error ? <div className="mt-2 text-rose-300">Last error: {data.last_error}</div> : null}
      </footer>
    </div>
  );
}

function Row({ label, value, className }: { label: string; value: string; className?: string }) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-slate-400">{label}</span>
      <span className={className || "text-slate-100"}>{value}</span>
    </div>
  );
}
