import { useState } from "react";
import type { ScannerHit } from "../lib/api";
import { classOf, formatINR, formatNum, formatPct } from "../lib/format";

const PLAIN_REASON: Record<string, string> = {
  uptrend_breakout_confirmed: "Clean 5m breakout — price punched through the swing high with momentum.",
  downtrend_breakdown_confirmed: "Clean 5m breakdown — price broke the swing low with momentum.",
  uptrend_pullback_bounce: "5m uptrend retraced to support and bounced — entering on the bounce candle.",
  downtrend_pullback_rejection: "5m downtrend rallied into resistance and got rejected.",
  uptrend_continuation_resume: "Tight consolidation above prior breakout — momentum resuming.",
  scalp_call_5m_momentum: "Quick scalp — 5m drifting up + bullish 1m candle. Tight stop, fast target.",
  scalp_put_5m_momentum: "Quick scalp — 5m drifting down + bearish 1m candle. Tight stop, fast target.",
  warmup: "Still warming up — needs more candles to judge 5m trend.",
  no_swing: "No clear 5-minute swing high/low yet.",
  no_price: "Broker did not return a last-traded price this cycle.",
  "filter:volatility_ok": "Instrument is too quiet today — daily range too small.",
  "filter:chop_ok": "Market is choppy / sideways — staying out.",
};

const PLAIN_CHECK: Record<string, string> = {
  volatility_ok: "Daily range vs minimum",
  chop_ok: "Not too sideways",
  trend_15m_up: "15m bias not against",
  trend_15m_not_against: "15m bias not against",
  trend_5m_up: "5-minute trend points up (PRIMARY)",
  trend_5m_down: "5-minute trend points down (PRIMARY)",
  breakout_5m: "Price broke 5m swing high",
  breakdown_5m: "Price broke 5m swing low",
  bullish_1m_close: "Last 1-minute candle is strong bullish",
  bearish_1m_close: "Last 1-minute candle is strong bearish",
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
  continuation: "CONTINUATION",
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
  const [expanded, setExpanded] = useState(isTop);
  const sb = row.score_breakdown;
  const sideClass =
    row.signal_side === "BUY_CALL"
      ? "pill-green"
      : row.signal_side === "BUY_PUT"
      ? "pill-red"
      : "pill-slate";
  const scorePct = Math.round((row.score ?? 0) * 100);

  // Detect the 5m setup pattern from the brain reason. We can't read it
  // structurally yet (Signal.pattern isn't on ScannerHit), so derive from
  // the canonical reason strings emitted by the brain.
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
    (reason.match(/(breakout|pullback|continuation)_buy_(call|put)_partial_/)
      ? `Close to a ${reason.includes("buy_call") ? "CALL" : "PUT"} — ${reason.split("_partial_").pop()} checks passed.`
      : row.signal_reason?.startsWith("call_partial_")
      ? `Close to a CALL — ${row.signal_reason.replace("call_partial_", "")} checks passed.`
      : row.signal_reason?.startsWith("put_partial_")
      ? `Close to a PUT — ${row.signal_reason.replace("put_partial_", "")} checks passed.`
      : row.signal_reason || "Analyzing.");

  return (
    <div className={`rounded-xl border p-4 ${isTop ? "border-sky-500/40 bg-sky-500/5" : "border-white/5 bg-slate-900/40"}`}>
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-base font-semibold text-slate-100">{row.name}</span>
            <span className="pill-blue text-[10px]">{row.kind}</span>
            <span className={`${sideClass} text-[10px]`}>{row.signal_side.replace("BUY_", "")}</span>
            {PATTERN_LABEL[pattern] ? (
              <span className={`${PATTERN_TONE[pattern]} text-[10px]`}>
                {PATTERN_LABEL[pattern]}
              </span>
            ) : null}
          </div>
          <div className="mt-1 text-xs text-slate-400">
            LTP {formatINR(row.last_price)} • Δ {formatPct(row.change_pct)} •{" "}
            {row.affordable_lots ?? 0} lots affordable
            {row.notional_per_lot ? (
              <>
                {" "}
                • 1 lot ={" "}
                <span className="text-slate-200">{formatINR(row.notional_per_lot)}</span>
              </>
            ) : null}
          </div>
          {row.affordable_lots !== null && row.affordable_lots < 1 && row.capital_short_for_one_lot ? (
            <div className="mt-1 text-[11px] text-amber-300">
              Need <span className="font-semibold">{formatINR(row.capital_short_for_one_lot)}</span>{" "}
              more cash to take 1 lot.
            </div>
          ) : null}
          {!row.in_trade_value_range && row.capital_range_reason ? (
            <div className="mt-1 text-[11px] text-amber-300">
              Lot value out of configured range — {row.capital_range_reason}.
            </div>
          ) : null}
          <div className="mt-2 text-sm text-slate-200">{headline}</div>
        </div>
        <div className="text-right">
          <ScoreDial score={scorePct} />
          <div className="mt-1 text-[10px] text-slate-400">score</div>
        </div>
      </div>

      <div className="mt-3 grid grid-cols-3 gap-2 text-[11px]">
        <FactorBar label="Volatility" value={sb.volatility} weight={0.25} />
        <FactorBar label="Momentum (5m)" value={sb.momentum} weight={0.45} />
        <FactorBar label="Structure (5m)" value={sb.breakout} weight={0.30} />
      </div>

      <div className="mt-2 text-[10px] text-slate-500">
        candles loaded: 5m={row.candles_5m} (primary) • 1m={row.candles_1m} • 15m={row.candles_15m} (bias)
      </div>

      <div className="mt-3 border-t border-white/5 pt-2">
        <button
          className="text-[11px] text-slate-400 hover:text-slate-200"
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded ? "▼ Hide entry checks" : "▶ Show entry checks"}
        </button>
        {expanded ? (
          <div className="mt-2 space-y-1">
            {row.checks?.length ? (
              row.checks.map((c, i) => (
                <CheckRow key={`${c.name}-${i}`} name={c.name} ok={c.ok} detail={c.detail} />
              ))
            ) : (
              <div className="text-[11px] text-slate-500">
                Strategy still warming up — checks appear once enough candles are built.
              </div>
            )}
          </div>
        ) : null}
      </div>
    </div>
  );
}

function ScoreDial({ score }: { score: number }) {
  const tone =
    score >= 70 ? "text-emerald-300" : score >= 45 ? "text-amber-300" : "text-slate-400";
  return <div className={`text-2xl font-semibold tabular-nums ${tone}`}>{score}</div>;
}

function FactorBar({ label, value, weight }: { label: string; value: number; weight: number }) {
  const pct = Math.round((value || 0) * 100);
  return (
    <div className="rounded-lg bg-slate-900/60 p-2">
      <div className="flex items-center justify-between text-slate-400">
        <span>{label}</span>
        <span className="text-[10px] text-slate-500">w {weight.toFixed(2)}</span>
      </div>
      <div className="mt-1 h-1.5 w-full overflow-hidden rounded bg-slate-700/40">
        <div
          className={`h-full ${pct >= 70 ? "bg-emerald-400" : pct >= 40 ? "bg-amber-300" : "bg-slate-500"}`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <div className={`mt-1 text-[11px] ${classOf(value > 0.5 ? 1 : 0)}`}>
        {formatNum(value, 2)}
      </div>
    </div>
  );
}

function CheckRow({ name, ok, detail }: { name: string; ok: boolean; detail: string }) {
  const friendly = PLAIN_CHECK[name] || name;
  return (
    <div className="flex items-start justify-between gap-2 text-[11px]">
      <div className="flex items-start gap-2">
        <span className={ok ? "text-emerald-300" : "text-rose-300"}>{ok ? "✓" : "✗"}</span>
        <div>
          <div className="text-slate-200">{friendly}</div>
          <div className="text-[10px] text-slate-500">{detail}</div>
        </div>
      </div>
    </div>
  );
}
