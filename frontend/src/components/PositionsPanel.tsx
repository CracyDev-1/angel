import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { apiPost, type PositionsResponse, type PositionRow } from "../lib/api";
import { classOf, formatINR } from "../lib/format";

export default function PositionsPanel({ positions }: { positions: PositionsResponse | null }) {
  const rows = positions?.rows?.filter((r) => r.net_qty !== 0) ?? [];
  const qc = useQueryClient();
  const [closingKey, setClosingKey] = useState<string | null>(null);

  const close = useMutation({
    mutationFn: (r: PositionRow) =>
      apiPost("/api/positions/close", {
        tradingsymbol: r.tradingsymbol,
        exchange: r.exchange,
        symboltoken: r.symboltoken,
        net_qty: r.net_qty,
        producttype: r.producttype,
      }),
    onSettled: () => {
      setClosingKey(null);
      qc.invalidateQueries({ queryKey: ["snapshot"] });
    },
  });

  return (
    <div className="card p-4">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold tracking-wide text-slate-200">Open positions</h2>
        <div className="text-xs text-slate-400">
          CE {formatINR(positions?.capital_used_ce ?? 0, { compact: true })} •
          PE {formatINR(positions?.capital_used_pe ?? 0, { compact: true })} •
          Total {formatINR(positions?.capital_used_total ?? 0, { compact: true })}
        </div>
      </div>
      {rows.length === 0 ? (
        <div className="rounded-lg border border-dashed border-white/10 bg-slate-900/40 p-6 text-center text-sm text-slate-400">
          You have no open positions. The bot will place trades here when a setup
          passes the strategy and risk checks (live mode only).
        </div>
      ) : (
        <div className="overflow-auto">
          <table className="w-full">
            <thead>
              <tr>
                <th className="table-th">Symbol</th>
                <th className="table-th">Side</th>
                <th className="table-th">Qty</th>
                <th className="table-th">Avg buy</th>
                <th className="table-th">LTP</th>
                <th className="table-th">P&amp;L</th>
                <th className="table-th text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const key = `${r.exchange}:${r.symboltoken}:${r.tradingsymbol}`;
                const busy = closingKey === key && close.isPending;
                return (
                  <tr key={key}>
                    <td className="table-td">
                      <div className="font-medium text-slate-100">{r.tradingsymbol}</div>
                      <div className="text-xs text-slate-500">{r.exchange} • {r.symboltoken}</div>
                    </td>
                    <td className="table-td">
                      <span
                        className={
                          r.side === "CE" ? "pill-green" : r.side === "PE" ? "pill-red" : "pill-slate"
                        }
                      >
                        {r.side}
                      </span>
                    </td>
                    <td className="table-td">{r.net_qty}</td>
                    <td className="table-td">{formatINR(r.buy_avg)}</td>
                    <td className="table-td">{formatINR(r.ltp)}</td>
                    <td className={`table-td ${classOf(r.pnl)}`}>{formatINR(r.pnl)}</td>
                    <td className="table-td text-right">
                      <button
                        className="btn-danger text-xs"
                        title="Place a market order in the opposite direction to close this position."
                        disabled={busy}
                        onClick={() => {
                          if (!confirm(`Close ${r.tradingsymbol} (${r.net_qty}) at market?`)) return;
                          setClosingKey(key);
                          close.mutate(r);
                        }}
                      >
                        {busy ? "Closing…" : "Close"}
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {close.isError ? (
        <div className="mt-3 rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
          {String((close.error as Error).message)}
        </div>
      ) : null}
    </div>
  );
}
