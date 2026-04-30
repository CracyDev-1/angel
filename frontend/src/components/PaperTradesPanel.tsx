import { useMutation, useQueryClient } from "@tanstack/react-query";
import { apiPost, type PaperBlock } from "../lib/api";
import { classOf, formatINR, formatTime } from "../lib/format";

type Props = {
  paper: PaperBlock | undefined;
};

/**
 * Visualises the dry-run paper book: open paper trades with live mark-to-market
 * P&L, plus per-row "Close now" so the user can experiment with exits.
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

  return (
    <section className="card p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="text-sm font-semibold tracking-wide text-slate-200">
            Paper trades (dry-run)
          </h2>
          <div className="text-[11px] text-slate-400">
            Same brain & sizing as live. Auto-exits on{" "}
            {cfg ? `+${(cfg.take_profit_pct * 100).toFixed(1)}% target / -${(cfg.stop_loss_pct * 100).toFixed(1)}% stop / ${cfg.max_hold_minutes}m timeout` : "configured rules"}.
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Stat label="Closed today" value={String(today?.trades ?? 0)} />
          <Stat
            label="Realized"
            value={formatINR(today?.realized_pnl ?? 0)}
            cls={classOf(today?.realized_pnl ?? null)}
          />
          <Stat
            label="Unrealized"
            value={formatINR(today?.unrealized_pnl ?? 0)}
            cls={classOf(today?.unrealized_pnl ?? null)}
          />
          <Stat
            label="Net (today)"
            value={formatINR(today?.net_pnl ?? 0)}
            cls={classOf(today?.net_pnl ?? null)}
          />
          <button
            className="btn-ghost text-xs"
            onClick={() => {
              if (window.confirm("Wipe all paper positions, dry-run history and dry-run daily P&L?")) {
                resetAll.mutate();
              }
            }}
            disabled={resetAll.isPending}
            title="Clears the paper book and dry-run ledger only. Live data is untouched."
          >
            {resetAll.isPending ? "Resetting…" : "Reset paper book"}
          </button>
        </div>
      </div>

      <div className="mt-4 overflow-auto">
        <table className="w-full">
          <thead>
            <tr>
              <th className="table-th">Symbol</th>
              <th className="table-th">Side</th>
              <th className="table-th">Lots</th>
              <th className="table-th">Qty</th>
              <th className="table-th">Entry</th>
              <th className="table-th">Mark</th>
              <th className="table-th">Stop / Target</th>
              <th className="table-th">Capital</th>
              <th className="table-th">Unrealized</th>
              <th className="table-th">Opened</th>
              <th className="table-th"></th>
            </tr>
          </thead>
          <tbody>
            {(open?.rows ?? []).map((r) => {
              const sideClass = r.side === "CE" ? "pill-green" : "pill-red";
              return (
                <tr key={r.id}>
                  <td className="table-td">
                    <div className="text-slate-200">{r.tradingsymbol}</div>
                    <div className="text-[10px] text-slate-500">{r.exchange}</div>
                  </td>
                  <td className="table-td">
                    <span className={`${sideClass} text-[10px]`}>{r.side}</span>
                  </td>
                  <td className="table-td">{r.lots}</td>
                  <td className="table-td">{r.qty}</td>
                  <td className="table-td">{formatINR(r.entry_price)}</td>
                  <td className="table-td">{formatINR(r.last_price)}</td>
                  <td className="table-td text-[11px] text-slate-400">
                    {r.stop_price !== null ? formatINR(r.stop_price) : "—"} /{" "}
                    {r.target_price !== null ? formatINR(r.target_price) : "—"}
                  </td>
                  <td className="table-td">{formatINR(r.capital_used)}</td>
                  <td className={`table-td ${classOf(r.unrealized_pnl)}`}>
                    {formatINR(r.unrealized_pnl)}
                  </td>
                  <td className="table-td text-[11px] text-slate-400">{formatTime(r.opened_at)}</td>
                  <td className="table-td">
                    <button
                      className="btn-ghost text-xs"
                      onClick={() => closeOne.mutate(r.id)}
                      disabled={closeOne.isPending}
                    >
                      Close
                    </button>
                  </td>
                </tr>
              );
            })}
            {(open?.rows?.length ?? 0) === 0 ? (
              <tr>
                <td colSpan={11} className="table-td text-center text-xs text-slate-500">
                  No paper positions open. The bot will open one as soon as the brain
                  triggers a CALL/PUT and the lot fits in your dry-run capital.
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function Stat({ label, value, cls = "text-slate-200" }: { label: string; value: string; cls?: string }) {
  return (
    <div className="rounded-lg border border-white/5 bg-slate-900/40 px-3 py-1.5">
      <div className="text-[10px] uppercase tracking-wider text-slate-500">{label}</div>
      <div className={`text-sm font-semibold ${cls}`}>{value}</div>
    </div>
  );
}
