import type { DecisionRow } from "../lib/api";
import { formatINR, formatTime } from "../lib/format";

function signalPill(s: DecisionRow["signal"]): string {
  if (s === "BUY_CALL") return "pill-green";
  if (s === "BUY_PUT") return "pill-red";
  if (s === "MODE") return "pill-blue";
  return "pill-slate";
}

export default function DecisionsTable({ rows }: { rows: DecisionRow[] }) {
  if (!rows.length) {
    return (
      <div className="rounded-lg border border-dashed border-white/10 bg-slate-900/40 p-6 text-center text-sm text-slate-400">
        No decisions yet. Once the bot is running it will log every iteration here.
      </div>
    );
  }
  return (
    <div className="overflow-auto">
      <table className="w-full">
        <thead>
          <tr>
            <th className="table-th">Time</th>
            <th className="table-th">Instrument</th>
            <th className="table-th">Signal</th>
            <th className="table-th">Side</th>
            <th className="table-th">Qty (lots)</th>
            <th className="table-th">Capital</th>
            <th className="table-th">Reason</th>
            <th className="table-th">Mode</th>
            <th className="table-th">Order id</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((d, i) => (
            <tr key={`${d.ts}-${i}`}>
              <td className="table-td text-xs text-slate-400">{formatTime(d.ts)}</td>
              <td className="table-td">
                <div className="font-medium text-slate-100">{d.name}</div>
                <div className="text-xs text-slate-500">{d.exchange} • {d.token}</div>
              </td>
              <td className="table-td">
                <span className={signalPill(d.signal)}>{d.signal}</span>
              </td>
              <td className="table-td">
                <span
                  className={
                    d.side === "CE" ? "pill-green" : d.side === "PE" ? "pill-red" : "pill-slate"
                  }
                >
                  {d.side}
                </span>
              </td>
              <td className="table-td">{d.quantity} ({d.lots})</td>
              <td className="table-td">{formatINR(d.capital_used)}</td>
              <td className="table-td text-xs text-slate-300">{d.reason}</td>
              <td className="table-td">
                {d.dry_run ? <span className="pill-slate">dry-run</span> : <span className="pill-amber">live</span>}
              </td>
              <td className="table-td font-mono text-xs">{d.broker_order_id || "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
