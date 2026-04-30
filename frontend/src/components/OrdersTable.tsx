import type { OrderRow } from "../lib/api";
import { formatINR, formatTime } from "../lib/format";

function lifecyclePill(status: string | null): string {
  const s = (status || "").toLowerCase();
  if (s === "executed" || s === "complete") return "pill-green";
  if (s === "rejected") return "pill-red";
  if (s === "cancelled") return "pill-slate";
  if (s === "partial") return "pill-amber";
  return "pill-blue";
}

export default function OrdersTable({ orders }: { orders: OrderRow[] }) {
  return (
    <div className="card p-4">
      <h2 className="mb-3 text-sm font-semibold tracking-wide text-slate-200">Recent orders</h2>
      {orders.length === 0 ? (
        <div className="rounded-lg border border-dashed border-white/10 bg-slate-900/40 p-6 text-center text-sm text-slate-400">
          No orders yet. The bot only sends orders when TRADING_ENABLED=true and a setup passes risk checks.
        </div>
      ) : (
        <div className="overflow-auto">
          <table className="w-full">
            <thead>
              <tr>
                <th className="table-th">Order id</th>
                <th className="table-th">Status</th>
                <th className="table-th">Filled / pending</th>
                <th className="table-th">Avg price</th>
                <th className="table-th">Updated</th>
              </tr>
            </thead>
            <tbody>
              {orders.map((o) => (
                <tr key={String(o.id)}>
                  <td className="table-td font-mono text-xs">{o.id}</td>
                  <td className="table-td">
                    <span className={lifecyclePill(o.lifecycle)}>{o.lifecycle || "—"}</span>
                    {o.broker_status ? (
                      <div className="text-[11px] text-slate-500">{o.broker_status}</div>
                    ) : null}
                  </td>
                  <td className="table-td">
                    {(o.filled_qty ?? 0)} / {(o.pending_qty ?? 0)}
                  </td>
                  <td className="table-td">{formatINR(o.avg_price)}</td>
                  <td className="table-td text-xs text-slate-400">{formatTime(o.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
