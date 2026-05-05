import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  apiPost,
  type PositionsResponse,
  type PositionRow,
  type LiveExitsBlock,
  type LiveExitPlanRow,
} from "../lib/api";
import { classOf, formatINR } from "../lib/format";

type Props = {
  positions: PositionsResponse | null;
  liveExits?: LiveExitsBlock | null;
};

function planFor(
  liveExits: LiveExitsBlock | null | undefined,
  r: PositionRow,
): LiveExitPlanRow | null {
  if (!liveExits || !liveExits.open) return null;
  const ex = (r.exchange || "").toUpperCase();
  const tok = String(r.symboltoken || "");
  return (
    liveExits.open.find(
      (p) => (p.exchange || "").toUpperCase() === ex && String(p.symboltoken) === tok,
    ) ?? null
  );
}

function trailStopActive(plan: LiveExitPlanRow, trailEnabled: boolean | undefined): boolean {
  if (!trailEnabled) return false;
  const init = plan.initial_stop_price ?? plan.stop_price;
  return Math.abs(plan.stop_price - init) > 1e-4;
}

export default function PositionsPanel({ positions, liveExits }: Props) {
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

  const adoptedCount = liveExits?.adopted_count ?? 0;
  const managedCount = liveExits?.managed_count ?? 0;
  const trailEnabled = liveExits?.trail_stop_enabled === true;
  const totalPnl = rows.reduce((s, r) => s + (r.pnl ?? 0), 0);
  const totalCap = rows.reduce((s, r) => s + (r.capital_used ?? 0), 0);

  return (
    <div className="card overflow-hidden">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-white/5 px-4 py-2.5">
        <div className="flex items-baseline gap-2">
          <h2 className="text-sm font-semibold tracking-wide text-slate-200">
            Open positions
          </h2>
          <span className="text-[11px] text-slate-500">
            {rows.length} open
            {managedCount > 0 ? (
              <>
                {" · "}
                <span className="text-emerald-300">{managedCount} managed</span>
                {adoptedCount > 0 ? (
                  <>
                    {" · "}
                    <span className="text-amber-300">{adoptedCount} adopted</span>
                  </>
                ) : null}
              </>
            ) : null}
          </span>
        </div>
        <div className="flex items-center gap-2 text-[11px]">
          <span className="rounded-md border border-white/10 bg-slate-900/50 px-2 py-0.5 text-slate-300">
            Capital {formatINR(totalCap, { compact: true })}
          </span>
          <span
            className={`rounded-md border px-2 py-0.5 ${
              totalPnl >= 0
                ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-200"
                : "border-rose-500/30 bg-rose-500/10 text-rose-200"
            }`}
          >
            P&L {formatINR(totalPnl)}
          </span>
        </div>
      </div>
      {rows.length === 0 ? (
        <div className="p-5 text-center text-xs text-slate-500">
          You have no open positions. They'll show here when the bot opens a
          trade or when one is opened on the Angel One app.
        </div>
      ) : (
        <div className="overflow-auto">
          <table className="w-full text-[12px]">
            <thead>
              <tr className="text-[10px] uppercase tracking-wider text-slate-500">
                <th className="px-3 py-1.5 text-left">Symbol</th>
                <th className="px-2 py-1.5 text-left">Side</th>
                <th className="px-2 py-1.5 text-right">Qty</th>
                <th className="px-2 py-1.5 text-right">Avg buy</th>
                <th className="px-2 py-1.5 text-right">LTP</th>
                <th className="px-2 py-1.5 text-right">P&amp;L</th>
                <th className="px-2 py-1.5 text-left">Bot exit</th>
                <th className="px-2 py-1.5"></th>
              </tr>
            </thead>
            <tbody className="divide-y divide-white/5">
              {rows.map((r) => {
                const key = `${r.exchange}:${r.symboltoken}:${r.tradingsymbol}`;
                const busy = closingKey === key && close.isPending;
                const plan = planFor(liveExits, r);
                const sideTone =
                  r.side === "CE" ? "text-emerald-300" : r.side === "PE" ? "text-rose-300" : "text-slate-300";
                return (
                  <tr key={key}>
                    <td className="px-3 py-1.5">
                      <div className="flex items-center gap-1.5">
                        <span className="font-medium text-slate-100">{r.tradingsymbol}</span>
                        {plan ? (
                          plan.source === "adopted" ? (
                            <span
                              className="rounded-full border border-amber-500/40 bg-amber-500/10 px-1.5 py-0 text-[9px] uppercase tracking-wider text-amber-200"
                              title="Picked up from Angel One — bot will manage SL / TP / max-hold."
                            >
                              ADOPTED
                            </span>
                          ) : (
                            <span
                              className="rounded-full border border-emerald-500/40 bg-emerald-500/10 px-1.5 py-0 text-[9px] uppercase tracking-wider text-emerald-200"
                              title="Bot opened this and is managing the exit."
                            >
                              BOT
                            </span>
                          )
                        ) : (
                          <span
                            className="rounded-full border border-slate-500/30 bg-slate-700/30 px-1.5 py-0 text-[9px] uppercase tracking-wider text-slate-400"
                            title="Will be picked up next cycle."
                          >
                            NEW
                          </span>
                        )}
                      </div>
                      <div className="text-[10px] text-slate-500">
                        {r.exchange} · {r.symboltoken}
                      </div>
                    </td>
                    <td className={`px-2 py-1.5 font-semibold ${sideTone}`}>{r.side}</td>
                    <td className="px-2 py-1.5 text-right tabular-nums">{r.net_qty}</td>
                    <td className="px-2 py-1.5 text-right tabular-nums">{formatINR(r.buy_avg)}</td>
                    <td className="px-2 py-1.5 text-right tabular-nums">{formatINR(r.ltp)}</td>
                    <td className={`px-2 py-1.5 text-right font-semibold tabular-nums ${classOf(r.pnl)}`}>
                      {formatINR(r.pnl)}
                    </td>
                    <td className="px-2 py-1.5 text-[10px]">
                      {plan ? (
                        <div className="text-slate-400">
                          {trailEnabled ? (
                            <>
                              <span className="text-amber-300/90">Trail SL </span>
                              <span
                                className="tabular-nums"
                                title={
                                  plan.peak_premium != null
                                    ? `Peak premium ${formatINR(plan.peak_premium)}`
                                    : "Current stop — ratchets up with peak premium when armed"
                                }
                              >
                                {formatINR(plan.stop_price)}
                              </span>
                              {trailStopActive(plan, trailEnabled) ? (
                                <>
                                  <span className="text-slate-500"> · init </span>
                                  <span className="tabular-nums text-slate-500">
                                    {formatINR(plan.initial_stop_price ?? plan.stop_price)}
                                  </span>{" "}
                                </>
                              ) : null}
                            </>
                          ) : (
                            <>
                              <span className="text-rose-300/80">SL </span>
                              <span className="tabular-nums">{formatINR(plan.stop_price)}</span>{" "}
                            </>
                          )}
                          <span className="text-emerald-300/80">TP </span>
                          <span className="tabular-nums">{formatINR(plan.target_price)}</span>{" "}
                          <span className="text-slate-500">· {plan.max_hold_minutes}m</span>
                        </div>
                      ) : (
                        <span className="text-slate-600">—</span>
                      )}
                    </td>
                    <td className="px-2 py-1.5 text-right">
                      <button
                        className="rounded border border-rose-500/30 bg-rose-500/10 px-2 py-0.5 text-[10px] uppercase tracking-wider text-rose-200 hover:bg-rose-500/20 disabled:opacity-50"
                        title="Place a market order in the opposite direction."
                        disabled={busy}
                        onClick={() => {
                          if (!confirm(`Close ${r.tradingsymbol} (${r.net_qty}) at market?`)) return;
                          setClosingKey(key);
                          close.mutate(r);
                        }}
                      >
                        {busy ? "…" : "Close"}
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
        <div className="border-t border-rose-500/30 bg-rose-500/10 px-3 py-1.5 text-[11px] text-rose-200">
          {String((close.error as Error).message)}
        </div>
      ) : null}
    </div>
  );
}
