import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiGet, type HistoryResponse } from "../lib/api";
import { classOf, formatINR, formatTime } from "../lib/format";
import StatCard from "../components/StatCard";
import OrdersTable from "../components/OrdersTable";

type Mode = "live" | "dryrun";

export default function HistoryPage() {
  const [mode, setMode] = useState<Mode>("live");

  const hist = useQuery({
    queryKey: ["history", mode],
    queryFn: () => apiGet<HistoryResponse>(`/api/history?mode=${mode}`),
    refetchInterval: 8000,
  });

  const data = hist.data;
  const totals = data?.totals;
  const totalClass = classOf(totals?.realized_pnl ?? null);
  const isDry = mode === "dryrun";

  return (
    <div className="space-y-6">
      {/* Mode tabs */}
      <div className="flex flex-wrap items-center gap-2">
        <ModeTab active={mode === "live"} onClick={() => setMode("live")} label="Live" hint="Real broker orders" />
        <ModeTab
          active={mode === "dryrun"}
          onClick={() => setMode("dryrun")}
          label="Dry-run (paper)"
          hint="Simulated trades — no money at risk"
        />
        <div className="ml-auto text-[11px] text-slate-500">
          {isDry
            ? "Dry-run history is independent of live history. Reset it from the Live tab."
            : "Live history reflects what actually happened in your Angel One account."}
        </div>
      </div>

      <section className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <StatCard
          label={isDry ? "All-time paper P&L" : "All-time realized P&L"}
          value={formatINR(totals?.realized_pnl ?? 0)}
          hint={`${totals?.days_traded ?? 0} trading days`}
          valueClass={totalClass}
        />
        <StatCard
          label={isDry ? "All-time paper trades" : "All-time trades"}
          value={String(totals?.trades ?? 0)}
          hint={isDry ? "Counts every closed paper trade" : "Across the SQLite history"}
        />
        <StatCard
          label="Orders captured"
          value={String(data?.orders?.length ?? 0)}
          hint="Most recent 200"
          accent={isDry ? "sky" : "emerald"}
        />
        <StatCard
          label="Last update"
          value={formatTime(new Date().toISOString())}
          hint="Refreshes every 8s"
        />
      </section>

      <section className="card p-4">
        <h2 className="mb-3 text-sm font-semibold tracking-wide text-slate-200">
          Daily P&amp;L — {isDry ? "Dry-run" : "Live"}
        </h2>
        {data?.all_days?.length ? (
          <div className="overflow-auto">
            <table className="w-full">
              <thead>
                <tr>
                  <th className="table-th">Date</th>
                  <th className="table-th">Trades</th>
                  <th className="table-th">{isDry ? "Paper P&L" : "Realized P&L"}</th>
                </tr>
              </thead>
              <tbody>
                {data.all_days.map((d) => (
                  <tr key={d.day}>
                    <td className="table-td text-slate-200">{d.day}</td>
                    <td className="table-td">{d.trades}</td>
                    <td className={`table-td ${classOf(d.pnl)}`}>{formatINR(d.pnl)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyHint
            text={
              isDry
                ? "No paper trades booked yet. Run the bot in dry-run for a while to populate this."
                : "No live daily stats yet. They appear here as the bot books real trades."
            }
          />
        )}
      </section>

      {isDry && data?.paper_positions?.length ? (
        <section className="card p-4">
          <h2 className="mb-3 text-sm font-semibold tracking-wide text-slate-200">
            Recent paper positions
          </h2>
          <div className="overflow-auto">
            <table className="w-full">
              <thead>
                <tr>
                  <th className="table-th">Symbol</th>
                  <th className="table-th">Side</th>
                  <th className="table-th">Qty</th>
                  <th className="table-th">Entry</th>
                  <th className="table-th">Exit</th>
                  <th className="table-th">Reason</th>
                  <th className="table-th">P&L</th>
                  <th className="table-th">Opened</th>
                  <th className="table-th">Closed</th>
                </tr>
              </thead>
              <tbody>
                {data.paper_positions.map((p) => (
                  <tr key={p.id}>
                    <td className="table-td text-slate-200">{p.tradingsymbol}</td>
                    <td className="table-td">
                      <span className={p.side === "CE" ? "pill-green" : "pill-red"}>{p.side}</span>
                    </td>
                    <td className="table-td">{p.qty}</td>
                    <td className="table-td">{formatINR(p.entry_price)}</td>
                    <td className="table-td">
                      {(p as unknown as { exit_price?: number }).exit_price !== undefined
                        ? formatINR((p as unknown as { exit_price?: number }).exit_price ?? null)
                        : "—"}
                    </td>
                    <td className="table-td text-[11px] text-slate-400">
                      {(p as unknown as { exit_reason?: string }).exit_reason ?? "open"}
                    </td>
                    <td className={`table-td ${classOf((p as unknown as { realized_pnl?: number }).realized_pnl ?? null)}`}>
                      {formatINR((p as unknown as { realized_pnl?: number }).realized_pnl ?? null)}
                    </td>
                    <td className="table-td text-[11px] text-slate-400">{formatTime(p.opened_at)}</td>
                    <td className="table-td text-[11px] text-slate-400">
                      {formatTime((p as unknown as { closed_at?: string }).closed_at ?? null)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ) : null}

      <section>
        <OrdersTable orders={data?.orders ?? []} />
      </section>

      <p className="text-[11px] leading-relaxed text-slate-500">
        History is read straight from the local SQLite store at{" "}
        <code>./data/angel_bot_state.sqlite3</code>. {isDry
          ? "Paper trades never hit the broker. Capital, lot sizes and entry checks mirror live so the result is a fair preview of what live would have done."
          : "Realized P&L is what the broker confirmed; intraday open positions are visible on the Live tab."}
      </p>
    </div>
  );
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

function EmptyHint({ text }: { text: string }) {
  return (
    <div className="rounded-lg border border-dashed border-white/10 bg-slate-900/40 p-6 text-center text-sm text-slate-400">
      {text}
    </div>
  );
}
