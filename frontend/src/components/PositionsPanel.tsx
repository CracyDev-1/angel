import type { PositionsResponse } from "../lib/api";
import { classOf, formatINR } from "../lib/format";

export default function PositionsPanel({ positions }: { positions: PositionsResponse | null }) {
  const rows = positions?.rows ?? [];
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
          No open positions reported by the broker.
        </div>
      ) : (
        <div className="overflow-auto">
          <table className="w-full">
            <thead>
              <tr>
                <th className="table-th">Symbol</th>
                <th className="table-th">Side</th>
                <th className="table-th">Net qty</th>
                <th className="table-th">Avg buy</th>
                <th className="table-th">LTP</th>
                <th className="table-th">Capital</th>
                <th className="table-th">P&L</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={`${r.exchange}:${r.symboltoken}:${r.tradingsymbol}`}>
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
                  <td className="table-td">{formatINR(r.capital_used)}</td>
                  <td className={`table-td ${classOf(r.pnl)}`}>{formatINR(r.pnl)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
