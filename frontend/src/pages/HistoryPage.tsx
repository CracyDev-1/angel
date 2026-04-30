import { useQuery } from "@tanstack/react-query";
import { apiGet, type HistoryResponse } from "../lib/api";
import { classOf, formatINR, formatTime } from "../lib/format";
import StatCard from "../components/StatCard";
import OrdersTable from "../components/OrdersTable";

export default function HistoryPage() {
  const hist = useQuery({
    queryKey: ["history"],
    queryFn: () => apiGet<HistoryResponse>("/api/history"),
    refetchInterval: 8000,
  });

  const data = hist.data;
  const totals = data?.totals;
  const totalClass = classOf(totals?.realized_pnl ?? null);

  return (
    <div className="space-y-6">
      <section className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <StatCard
          label="All-time realized P&L"
          value={formatINR(totals?.realized_pnl ?? 0)}
          hint={`${totals?.days_traded ?? 0} trading days`}
          valueClass={totalClass}
        />
        <StatCard
          label="All-time trades"
          value={String(totals?.trades ?? 0)}
          hint="Across the SQLite history"
        />
        <StatCard
          label="Orders captured"
          value={String(data?.orders?.length ?? 0)}
          hint="Most recent 200"
          accent="sky"
        />
        <StatCard
          label="Last update"
          value={formatTime(new Date().toISOString())}
          hint="Refreshes every 8s"
        />
      </section>

      <section className="card p-4">
        <h2 className="mb-3 text-sm font-semibold tracking-wide text-slate-200">Daily P&amp;L</h2>
        {data?.all_days?.length ? (
          <div className="overflow-auto">
            <table className="w-full">
              <thead>
                <tr>
                  <th className="table-th">Date</th>
                  <th className="table-th">Trades</th>
                  <th className="table-th">Realized P&amp;L</th>
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
          <EmptyHint text="No daily stats yet. They appear here as the bot books trades." />
        )}
      </section>

      <section>
        <OrdersTable orders={data?.orders ?? []} />
      </section>

      <p className="text-[11px] leading-relaxed text-slate-500">
        History is read straight from the local SQLite store at{" "}
        <code>./data/angel_bot_state.sqlite3</code>. Realized P&amp;L is what the broker
        confirmed; intraday open positions are visible on the Live tab.
      </p>
    </div>
  );
}

function EmptyHint({ text }: { text: string }) {
  return (
    <div className="rounded-lg border border-dashed border-white/10 bg-slate-900/40 p-6 text-center text-sm text-slate-400">
      {text}
    </div>
  );
}
