import { useState } from "react";
import type { ScannerHit } from "../lib/api";
import { classOf, formatINR, formatNum, formatPct } from "../lib/format";

const PLAIN_REASON: Record<string, string> = {
  uptrend_breakout_confirmed: "Uptrend confirmed across 15m → 5m → 1m and price just broke out.",
  downtrend_breakdown_confirmed: "Downtrend confirmed across 15m → 5m → 1m and price just broke down.",
  warmup: "Still warming up — needs more candles to judge multi-timeframe trend.",
  no_swing: "No clear 5-minute swing high/low yet.",
  no_price: "Broker did not return a last-traded price this cycle.",
  "filter:volatility_ok": "Stock is too quiet today — daily range too small.",
  "filter:chop_ok": "Market is choppy / sideways — staying out.",
};

const PLAIN_CHECK: Record<string, string> = {
  volatility_ok: "Daily range vs minimum",
  chop_ok: "Not too sideways",
  trend_15m_up: "15-minute trend points up",
  trend_5m_up: "5-minute trend points up",
  breakout_5m: "Price broke 5m swing high",
  bullish_1m_close: "Last 1-minute candle is strong bullish",
  above_twap: "Price above session mean (TWAP)",
  not_late: "Not chasing a move that already ran",
  trend_15m_down: "15-minute trend points down",
  trend_5m_down: "5-minute trend points down",
  breakdown_5m: "Price broke 5m swing low",
  bearish_1m_close: "Last 1-minute candle is strong bearish",
  below_twap: "Price below session mean (TWAP)",
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

  const headline =
    PLAIN_REASON[row.signal_reason] ||
    (row.signal_reason?.startsWith("call_partial_")
      ? `Close to a CALL — ${row.signal_reason.replace("call_partial_", "")} checks passed.`
      : row.signal_reason?.startsWith("put_partial_")
      ? `Close to a PUT — ${row.signal_reason.replace("put_partial_", "")} checks passed.`
      : row.signal_reason || "Analyzing.");

  return (
    <div className={`rounded-xl border p-4 ${isTop ? "border-sky-500/40 bg-sky-500/5" : "border-white/5 bg-slate-900/40"}`}>
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <span className="text-base font-semibold text-slate-100">{row.name}</span>
            <span className="pill-blue text-[10px]">{row.kind}</span>
            <span className={`${sideClass} text-[10px]`}>{row.signal_side.replace("BUY_", "")}</span>
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
        <FactorBar label="Volatility" value={sb.volatility} weight={0.30} />
        <FactorBar label="Momentum" value={sb.momentum} weight={0.40} />
        <FactorBar label="Breakout" value={sb.breakout} weight={0.30} />
      </div>

      <div className="mt-2 text-[10px] text-slate-500">
        candles loaded: 15m={row.candles_15m} • 5m={row.candles_5m}
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
