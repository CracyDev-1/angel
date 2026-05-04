import type { DecisionRow, LiveClosedRow } from "../lib/api";
import { classOf, formatINR, formatTime } from "../lib/format";

type Props = {
  decisions: DecisionRow[];
  isLive: boolean;
  // Authoritative SQL-backed list of today's closed live trades. When
  // present, the panel sources its rows from here instead of parsing
  // the (in-memory, capped at 120) decision stream — that's how the
  // panel and the realized-pnl tile stay perfectly consistent.
  liveClosed?: LiveClosedRow[];
};

type ClosedRow = {
  ts: string;
  symbol: string;
  side: string;
  qty: number;
  exitPrice: number | null;
  pnl: number;
  reason: string;
  source: "paper" | "live" | "external";
  rawReason: string;
};

const PNL_RX = /pnl ₹([+\-]?\d+(?:\.\d+)?)/i;

// Convert a decision reason like "live_close_target: pnl ₹+225.50" or
// "paper_close_stop: pnl ₹-75.00" or "external_close NIFTY...: pnl ₹+375.00"
// into a typed ClosedRow. Returns null when the row isn't a closure.
function parseClose(d: DecisionRow): ClosedRow | null {
  if (d.signal !== "MODE") return null;
  const r = (d.reason || "").trim();
  let source: ClosedRow["source"];
  let exitReason: string;
  if (r.startsWith("live_close_")) {
    source = "live";
    exitReason = r.replace(/^live_close_/, "").split(/[\s:(]/, 1)[0] || "exit";
  } else if (r.startsWith("paper_close_")) {
    source = "paper";
    exitReason = r.replace(/^paper_close_/, "").split(/[\s:(]/, 1)[0] || "exit";
  } else if (r.startsWith("external_close")) {
    source = "external";
    exitReason = "external";
  } else {
    return null;
  }
  const m = r.match(PNL_RX);
  const pnl = m ? Number(m[1]) : 0;
  return {
    ts: d.ts,
    symbol: d.name,
    side: d.side,
    qty: d.quantity,
    exitPrice: d.last_price,
    pnl: Number.isFinite(pnl) ? pnl : 0,
    reason: exitReason,
    source,
    rawReason: r,
  };
}

function fromLiveClosed(r: LiveClosedRow): ClosedRow {
  // The bot stamps "external_close" on plans that the user squared off
  // on Angel One directly; surface that in the UI so it's clearly
  // distinguishable from a bot-driven stop / target.
  const isExternal = (r.exit_reason || "").toLowerCase() === "external_close";
  return {
    ts: r.ts,
    symbol: r.tradingsymbol,
    side: r.side,
    qty: r.qty,
    exitPrice: r.exit_price || null,
    pnl: Number.isFinite(r.realized_pnl) ? r.realized_pnl : 0,
    reason: isExternal ? "external" : (r.exit_reason || "exit"),
    source: isExternal ? "external" : "live",
    rawReason: r.exit_reason || "",
  };
}

const REASON_LABEL: Record<string, string> = {
  stop: "Stop loss",
  target: "Take profit",
  session_end: "Max-hold timeout",
  manual: "Manual close",
  kill: "Kill switch",
  external: "Closed on Angel One",
  exit: "Exit",
};

const REASON_TONE: Record<string, string> = {
  stop: "border-rose-500/30 bg-rose-500/10 text-rose-200",
  target: "border-emerald-500/30 bg-emerald-500/10 text-emerald-200",
  session_end: "border-slate-500/30 bg-slate-700/30 text-slate-300",
  manual: "border-slate-500/30 bg-slate-700/30 text-slate-300",
  kill: "border-amber-500/30 bg-amber-500/10 text-amber-200",
  external: "border-amber-500/30 bg-amber-500/10 text-amber-200",
  exit: "border-slate-500/30 bg-slate-700/30 text-slate-300",
};

export default function ClosedTradesPanel({ decisions, isLive, liveClosed }: Props) {
  let all: ClosedRow[];
  if (isLive && liveClosed && liveClosed.length > 0) {
    // Prefer the SQL-backed list — it never disagrees with the
    // realized-pnl tile and survives bot restarts mid-session.
    all = liveClosed.map(fromLiveClosed);
  } else if (isLive) {
    // Live mode but no closed-today rows from the backend (or empty
    // list). Don't fall through to decision-parsing: showing 0 closed
    // trades is the correct, consistent answer.
    all = [];
  } else {
    all = decisions
      .map(parseClose)
      .filter((x): x is ClosedRow => x !== null)
      .filter((x) => x.source === "paper");
  }

  const totalPnl = all.reduce((s, r) => s + r.pnl, 0);
  const wins = all.filter((r) => r.pnl > 0).length;
  const losses = all.filter((r) => r.pnl < 0).length;

  return (
    <div className="card overflow-hidden">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-white/5 px-4 py-3">
        <div className="flex items-baseline gap-2">
          <h2 className="text-sm font-semibold tracking-wide text-slate-200">
            {isLive ? "Closed today" : "Closed paper trades"}
          </h2>
          <span className="text-[11px] text-slate-500">
            {all.length} trade{all.length === 1 ? "" : "s"}
          </span>
        </div>
        <div className="flex items-center gap-2 text-[11px]">
          <span className="rounded-md border border-emerald-500/30 bg-emerald-500/10 px-2 py-0.5 text-emerald-200">
            {wins}W
          </span>
          <span className="rounded-md border border-rose-500/30 bg-rose-500/10 px-2 py-0.5 text-rose-200">
            {losses}L
          </span>
          <span className={`rounded-md border px-2 py-0.5 ${totalPnl >= 0 ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-200" : "border-rose-500/30 bg-rose-500/10 text-rose-200"}`}>
            Net {formatINR(totalPnl)}
          </span>
        </div>
      </div>
      {all.length === 0 ? (
        <div className="p-6 text-center text-xs text-slate-500">
          No trades closed yet today. They'll show up here as the bot's
          stop / target / max-hold rules trigger.
        </div>
      ) : (
        <ul className="max-h-[280px] divide-y divide-white/5 overflow-auto">
          {all.map((r, i) => {
            const label = REASON_LABEL[r.reason] ?? r.reason;
            const tone = REASON_TONE[r.reason] ?? REASON_TONE.exit;
            return (
              <li
                key={`${r.ts}-${i}`}
                className="flex items-center justify-between gap-3 px-4 py-2 text-[12px]"
              >
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="truncate font-medium text-slate-100">{r.symbol}</span>
                    <span
                      className={
                        r.side === "CE"
                          ? "pill-green text-[10px]"
                          : r.side === "PE"
                          ? "pill-red text-[10px]"
                          : "pill-slate text-[10px]"
                      }
                    >
                      {r.side}
                    </span>
                    {r.source === "external" ? (
                      <span className="rounded border border-amber-500/30 bg-amber-500/10 px-1 py-0.5 text-[10px] uppercase tracking-wider text-amber-200">
                        manual
                      </span>
                    ) : null}
                  </div>
                  <div className="mt-0.5 flex flex-wrap items-center gap-2 text-[10px] text-slate-500">
                    <span>{formatTime(r.ts)}</span>
                    <span>qty {r.qty}</span>
                    {r.exitPrice != null ? <span>@ {formatINR(r.exitPrice)}</span> : null}
                    <span className={`rounded border px-1.5 py-0.5 ${tone}`}>{label}</span>
                  </div>
                </div>
                <div className={`shrink-0 text-right font-semibold tabular-nums ${classOf(r.pnl)}`}>
                  {formatINR(r.pnl)}
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
