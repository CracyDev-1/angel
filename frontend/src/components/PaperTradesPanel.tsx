import { useMutation, useQueryClient } from "@tanstack/react-query";
import { apiPost, type PaperBlock } from "../lib/api";
import { classOf, formatINR, formatTime } from "../lib/format";

type Props = {
  paper: PaperBlock | undefined;
};

/**
 * Compact view of the dry-run paper book: open paper trades with live
 * mark-to-market P&L and per-row "Close now" so the user can experiment
 * with exits without leaving the page.
 */
export default function PaperTradesPanel({ paper }: Props) {
  const qc = useQueryClient();

  const closeOne = useMutation({
    mutationFn: (id: number) => apiPost("/api/dryrun/paper/close", { id }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["snapshot"] }),
  });

  const resetAll = useMutation({
    mutationFn: () => apiPost("/api/dryrun/reset", { confirm: "RESET_DRYRUN" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["snapshot"] }),
  });

  const today = paper?.today;
  const open = paper?.open;
  const cfg = paper?.config;
  const rows = open?.rows ?? [];

  return (
    <section className="card overflow-hidden">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-white/5 px-4 py-2.5">
        <div className="flex items-baseline gap-2">
          <h2 className="text-sm font-semibold tracking-wide text-slate-200">
            Paper trades
          </h2>
          <span className="text-[11px] text-slate-500">
            {rows.length} open · same brain & sizing as live
            {cfg ? (
              <>
                {" · auto-exit "}
                <span className="text-emerald-300">+{(cfg.take_profit_pct * 100).toFixed(1)}%</span>
                {" / "}
                <span className="text-rose-300">−{(cfg.stop_loss_pct * 100).toFixed(1)}%</span>
                {" / "}
                <span className="text-slate-300">{cfg.max_hold_minutes}m</span>
              </>
            ) : null}
          </span>
        </div>
        <div className="flex items-center gap-2 text-[11px]">
          <span className="rounded-md border border-white/10 bg-slate-900/50 px-2 py-0.5 text-slate-300">
            Closed today {today?.trades ?? 0}
          </span>
          <span
            className={`rounded-md border px-2 py-0.5 ${
              (today?.realized_pnl ?? 0) >= 0
                ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-200"
                : "border-rose-500/30 bg-rose-500/10 text-rose-200"
            }`}
          >
            Realized {formatINR(today?.realized_pnl ?? 0)}
          </span>
          <span
            className={`rounded-md border px-2 py-0.5 ${
              (today?.unrealized_pnl ?? 0) >= 0
                ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-200"
                : "border-rose-500/30 bg-rose-500/10 text-rose-200"
            }`}
          >
            Unrealized {formatINR(today?.unrealized_pnl ?? 0)}
          </span>
          <button
            className="rounded border border-white/10 bg-slate-800/60 px-2 py-0.5 text-[10px] uppercase tracking-wider text-slate-300 hover:bg-slate-800 disabled:opacity-50"
            onClick={() => {
              if (window.confirm("Wipe all paper positions, dry-run history and dry-run daily P&L?")) {
                resetAll.mutate();
              }
            }}
            disabled={resetAll.isPending}
            title="Clears the paper book and dry-run ledger only. Live data is untouched."
          >
            {resetAll.isPending ? "…" : "Reset"}
          </button>
        </div>
      </div>

      {rows.length === 0 ? (
        <div className="p-5 text-center text-xs text-slate-500">
          No paper positions open. The bot will open one as soon as the brain
          triggers a CALL/PUT and the lot fits in your dry-run capital.
        </div>
      ) : (
        <div className="overflow-auto">
          <table className="w-full text-[12px]">
            <thead>
              <tr className="text-[10px] uppercase tracking-wider text-slate-500">
                <th className="px-3 py-1.5 text-left">Symbol</th>
                <th className="px-2 py-1.5 text-left">Side</th>
                <th className="px-2 py-1.5 text-right">Lots</th>
                <th className="px-2 py-1.5 text-right">Qty</th>
                <th className="px-2 py-1.5 text-right">Entry</th>
                <th className="px-2 py-1.5 text-right">Mark</th>
                <th className="px-2 py-1.5 text-left">SL / TP</th>
                <th className="px-2 py-1.5 text-right">Capital</th>
                <th className="px-2 py-1.5 text-right">P&amp;L</th>
                <th className="px-2 py-1.5 text-left">Opened</th>
                <th className="px-2 py-1.5"></th>
              </tr>
            </thead>
            <tbody className="divide-y divide-white/5">
              {rows.map((r) => {
                const sideTone =
                  r.side === "CE" ? "text-emerald-300" : r.side === "PE" ? "text-rose-300" : "text-slate-300";
                return (
                  <tr key={r.id}>
                    <td className="px-3 py-1.5">
                      <div className="font-medium text-slate-100">{r.tradingsymbol}</div>
                      <div className="text-[10px] text-slate-500">{r.exchange}</div>
                    </td>
                    <td className={`px-2 py-1.5 font-semibold ${sideTone}`}>{r.side}</td>
                    <td className="px-2 py-1.5 text-right tabular-nums">{r.lots}</td>
                    <td className="px-2 py-1.5 text-right tabular-nums">{r.qty}</td>
                    <td className="px-2 py-1.5 text-right tabular-nums">{formatINR(r.entry_price)}</td>
                    <td className="px-2 py-1.5 text-right tabular-nums">{formatINR(r.last_price)}</td>
                    <td className="px-2 py-1.5 text-[10px] text-slate-400">
                      <span className="tabular-nums text-rose-300/80">
                        {r.stop_price !== null ? formatINR(r.stop_price) : "—"}
                      </span>
                      <span className="text-slate-600"> / </span>
                      <span className="tabular-nums text-emerald-300/80">
                        {r.target_price !== null ? formatINR(r.target_price) : "—"}
                      </span>
                    </td>
                    <td className="px-2 py-1.5 text-right tabular-nums">
                      {formatINR(r.capital_used, { compact: true })}
                    </td>
                    <td className={`px-2 py-1.5 text-right font-semibold tabular-nums ${classOf(r.unrealized_pnl)}`}>
                      {formatINR(r.unrealized_pnl)}
                    </td>
                    <td className="px-2 py-1.5 text-[10px] text-slate-400 tabular-nums">
                      {formatTime(r.opened_at)}
                    </td>
                    <td className="px-2 py-1.5 text-right">
                      <button
                        className="rounded border border-rose-500/30 bg-rose-500/10 px-2 py-0.5 text-[10px] uppercase tracking-wider text-rose-200 hover:bg-rose-500/20 disabled:opacity-50"
                        onClick={() => closeOne.mutate(r.id)}
                        disabled={closeOne.isPending}
                      >
                        Close
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
