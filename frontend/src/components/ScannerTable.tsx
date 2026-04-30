import type { ScannerHit } from "../lib/api";
import { classOf, formatINR, formatNum, formatPct } from "../lib/format";

export default function ScannerTable({ hits }: { hits: ScannerHit[] }) {
  if (!hits.length) {
    return (
      <div className="rounded-lg border border-dashed border-white/10 bg-slate-900/40 p-6 text-center text-sm text-slate-400">
        Scanner has no data yet. Make sure the bot is running and your watchlist tokens are valid.
      </div>
    );
  }
  return (
    <div className="overflow-auto">
      <table className="w-full">
        <thead>
          <tr>
            <th className="table-th">Instrument</th>
            <th className="table-th">Kind</th>
            <th className="table-th">LTP</th>
            <th className="table-th">Δ%</th>
            <th className="table-th">Mom 5</th>
            <th className="table-th">Score</th>
            <th className="table-th">Lot</th>
            <th className="table-th">Notional / lot</th>
            <th className="table-th">Affordable lots</th>
          </tr>
        </thead>
        <tbody>
          {hits.map((h) => (
            <tr key={`${h.exchange}:${h.token}`} className="hover:bg-white/[0.03]">
              <td className="table-td">
                <div className="font-medium text-slate-100">{h.name}</div>
                <div className="text-xs text-slate-500">{h.exchange} • {h.token}</div>
              </td>
              <td className="table-td">
                <span className="pill-blue">{h.kind}</span>
              </td>
              <td className="table-td">{formatINR(h.last_price)}</td>
              <td className={`table-td ${classOf(h.change_pct)}`}>{formatPct(h.change_pct)}</td>
              <td className={`table-td ${classOf(h.momentum_5)}`}>{formatPct(h.momentum_5)}</td>
              <td className="table-td">{formatNum(h.score, 4)}</td>
              <td className="table-td">{h.lot_size ?? "—"}</td>
              <td className="table-td">{formatINR(h.notional_per_lot)}</td>
              <td className="table-td">
                {h.affordable_lots !== null && h.affordable_lots !== undefined ? (
                  <span className={h.affordable_lots > 0 ? "pill-green" : "pill-slate"}>
                    {h.affordable_lots}
                  </span>
                ) : (
                  "—"
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
