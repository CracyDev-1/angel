import { useState } from "react";
import type { ScannerHit } from "../lib/api";
import { classOf, formatINR, formatPct } from "../lib/format";

const PLAIN_REASON: Record<string, string> = {
  uptrend_breakout_confirmed: "Clean 5m breakout — price punched through the swing high with momentum.",
  downtrend_breakdown_confirmed: "Clean 5m breakdown — price broke the swing low with momentum.",
  uptrend_pullback_bounce: "5m uptrend retraced to support and bounced — entering on the bounce candle.",
  downtrend_pullback_rejection: "5m downtrend rallied into resistance and got rejected.",
  uptrend_continuation_resume: "Tight consolidation above prior breakout — momentum resuming.",
  scalp_call_5m_momentum: "Quick scalp — 5m drifting up + bullish 1m candle.",
  scalp_put_5m_momentum: "Quick scalp — 5m drifting down + bearish 1m candle.",
  warmup: "Still warming up — needs more candles.",
  no_swing: "No clear 5-minute swing high/low yet.",
  no_price: "Broker did not return a last-traded price.",
  "filter:volatility_ok": "Too quiet today — daily range too small.",
  "filter:chop_ok": "Market is choppy / sideways — staying out.",
};

const PLAIN_CHECK: Record<string, string> = {
  volatility_ok: "Daily range vs minimum",
  chop_ok: "Not too sideways",
  trend_15m_up: "15m bias not against",
  trend_15m_not_against: "15m bias not against",
  trend_5m_up: "5m trend points up (PRIMARY)",
  trend_5m_down: "5m trend points down (PRIMARY)",
  breakout_5m: "Broke 5m swing high",
  breakdown_5m: "Broke 5m swing low",
  bullish_1m_close: "Last 1m bar strong bullish",
  bearish_1m_close: "Last 1m bar strong bearish",
  above_twap: "Price above session mean (TWAP)",
  below_twap: "Price below session mean (TWAP)",
  not_late: "Not chasing a move that already ran",
  uptrend_5m_established: "5m uptrend established (2+ green bars)",
  downtrend_5m_established: "5m downtrend established (2+ red bars)",
  pullback_within_band: "Pullback within allowed retracement",
  bounce_candle_1m: "Bounce candle on 1m confirmed",
  rejection_candle_1m: "Rejection candle on 1m confirmed",
  broke_out_earlier: "Already broke out — now consolidating",
  tight_consolidation: "Tight 5m consolidation range",
  resume_up: "Reclaimed consolidation high",
  trend_5m_up_any: "5m drifting up (any visible push)",
  trend_5m_down_any: "5m drifting down (any visible push)",
};

const PATTERN_LABEL: Record<string, string> = {
  breakout: "BREAKOUT",
  pullback: "PULLBACK",
  continuation: "CONT.",
  scalp: "SCALP",
  other: "",
};
const PATTERN_TONE: Record<string, string> = {
  breakout: "pill-blue",
  pullback: "pill-amber",
  continuation: "pill-green",
  scalp: "pill-violet",
  other: "pill-slate",
};

export default function CandidateCard({ row, isTop }: { row: ScannerHit; isTop: boolean }) {
  const [expanded, setExpanded] = useState(false);
  const sb = row.score_breakdown;
  const sideClass =
    row.signal_side === "BUY_CALL"
      ? "pill-green"
      : row.signal_side === "BUY_PUT"
      ? "pill-red"
      : "pill-slate";
  const scorePct = Math.round((row.score ?? 0) * 100);

  const reason = row.signal_reason || "";
  const pattern: keyof typeof PATTERN_LABEL = reason.includes("scalp")
    ? "scalp"
    : reason.includes("breakout") || reason.includes("breakdown")
    ? "breakout"
    : reason.includes("pullback")
    ? "pullback"
    : reason.includes("continuation")
    ? "continuation"
    : "other";

  const headline =
    PLAIN_REASON[row.signal_reason] ||
    (row.signal_reason?.startsWith("call_partial_")
      ? `Close to a CALL — ${row.signal_reason.replace("call_partial_", "")} checks passed.`
      : row.signal_reason?.startsWith("put_partial_")
      ? `Close to a PUT — ${row.signal_reason.replace("put_partial_", "")} checks passed.`
      : row.signal_reason || "Analyzing.");

  const failedChecks = (row.checks ?? []).filter((c) => !c.ok).length;
  const totalChecks = row.checks?.length ?? 0;

  return (
    <div
      className={`rounded-lg border ${
        isTop ? "border-sky-500/40 bg-sky-500/5" : "border-white/5 bg-slate-900/40"
      }`}
    >
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left transition hover:bg-white/5"
      >
        <span className="min-w-0 flex-1">
          <span className="flex flex-wrap items-center gap-1.5">
            <span className="truncate text-[13px] font-semibold text-slate-100">{row.name}</span>
            <span className={`${sideClass} text-[9px]`}>{row.signal_side.replace("BUY_", "")}</span>
            {PATTERN_LABEL[pattern] ? (
              <span className={`${PATTERN_TONE[pattern]} text-[9px]`}>{PATTERN_LABEL[pattern]}</span>
            ) : null}
            <span className="text-[10px] text-slate-500">{row.kind}</span>
          </span>
          <span className="mt-0.5 block text-[10px] text-slate-500">
            LTP <span className="text-slate-300 tabular-nums">{formatINR(row.last_price)}</span>
            <span className={`tabular-nums ${classOf(row.change_pct ?? 0)}`}>
              {" "}
              {row.change_pct != null ? formatPct(row.change_pct) : ""}
            </span>
            {row.notional_per_lot ? (
              <>
                {" · 1 lot "}
                <span className="text-slate-300 tabular-nums">
                  {formatINR(row.notional_per_lot, { compact: true })}
                </span>
              </>
            ) : null}
            {totalChecks > 0 ? (
              <>
                {" · "}
                <span className={failedChecks === 0 ? "text-emerald-300" : "text-slate-400"}>
                  {totalChecks - failedChecks}/{totalChecks} checks
                </span>
              </>
            ) : null}
          </span>
        </span>
        <span className="shrink-0 text-right">
          <span
            className={`text-lg font-semibold tabular-nums ${
              scorePct >= 70 ? "text-emerald-300" : scorePct >= 45 ? "text-amber-300" : "text-slate-400"
            }`}
          >
            {scorePct}
          </span>
          <span className="block text-[9px] uppercase tracking-wider text-slate-500">
            score
          </span>
        </span>
        <span className="shrink-0 text-[10px] text-slate-500">{expanded ? "▾" : "▸"}</span>
      </button>

      {expanded ? (
        <div className="space-y-2 border-t border-white/5 px-3 py-2">
          <div className="text-[12px] text-slate-200">{headline}</div>
          {row.affordable_lots !== null && row.affordable_lots < 1 && row.capital_short_for_one_lot ? (
            <div className="text-[11px] text-amber-300">
              Need <span className="font-semibold">{formatINR(row.capital_short_for_one_lot)}</span>{" "}
              more cash to take 1 lot.
            </div>
          ) : null}
          {!row.in_trade_value_range && row.capital_range_reason ? (
            <div className="text-[11px] text-amber-300">
              Lot value out of range — {row.capital_range_reason}.
            </div>
          ) : null}
          <div className="grid grid-cols-3 gap-1.5">
            <FactorBar label="Vol" value={sb.volatility} weight={0.25} />
            <FactorBar label="Mom 5m" value={sb.momentum} weight={0.45} />
            <FactorBar label="Struct" value={sb.breakout} weight={0.30} />
          </div>
          <div className="text-[10px] text-slate-500">
            5m={row.candles_5m} · 1m={row.candles_1m} · 15m={row.candles_15m}
          </div>
          {row.checks?.length ? (
            <div className="space-y-0.5">
              {row.checks.map((c, i) => (
                <CheckRow key={`${c.name}-${i}`} name={c.name} ok={c.ok} detail={c.detail} />
              ))}
            </div>
          ) : (
            <div className="text-[11px] text-slate-500">
              Strategy still warming up — checks appear once enough candles are built.
            </div>
          )}
        </div>
      ) : null}
    </div>
  );
}

function FactorBar({ label, value, weight }: { label: string; value: number; weight: number }) {
  const pct = Math.round((value || 0) * 100);
  return (
    <div className="rounded bg-slate-950/40 px-1.5 py-1">
      <div className="flex items-center justify-between text-[9px] uppercase tracking-wider text-slate-500">
        <span>{label}</span>
        <span>w{weight.toFixed(2)}</span>
      </div>
      <div className="mt-0.5 h-1 w-full overflow-hidden rounded bg-slate-700/40">
        <div
          className={`h-full ${pct >= 70 ? "bg-emerald-400" : pct >= 40 ? "bg-amber-300" : "bg-slate-500"}`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <div className="mt-0.5 text-[10px] tabular-nums text-slate-300">
        {pct}
      </div>
    </div>
  );
}

function CheckRow({ name, ok, detail }: { name: string; ok: boolean; detail: string }) {
  const friendly = PLAIN_CHECK[name] || name;
  return (
    <div className="flex items-start gap-1.5 text-[11px]">
      <span className={ok ? "text-emerald-300" : "text-rose-300"}>{ok ? "✓" : "✗"}</span>
      <div className="min-w-0">
        <div className="text-slate-200">{friendly}</div>
        <div className="text-[10px] text-slate-500">{detail}</div>
      </div>
    </div>
  );
}
