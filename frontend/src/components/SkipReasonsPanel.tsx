import type { DecisionRow } from "../lib/api";

type Props = {
  decisions: DecisionRow[];
};

type Bucket = {
  family: string;
  hint: string;
  count: number;
  examples: string[];
};

// Map a raw skip reason ("filter:chop_ok", "risk:max_concurrent (3/3)",
// "llm:SKIP conf=0.30<0.45 — exhausted move", "no_execution_price for X")
// into a stable family key + a friendly one-liner so the user understands
// at a glance what the bot keeps catching on.
function classify(reason: string): { family: string; hint: string } {
  const r = (reason || "").trim();
  if (!r) return { family: "other", hint: "Unclassified — see decisions table." };
  if (r.startsWith("warmup") || r.startsWith("no_swing"))
    return {
      family: "warmup",
      hint: "Brain is still building 5m / 15m candles. Needs ≥5 5m and ≥2 15m bars (10–25 minutes after start).",
    };
  if (r.startsWith("filter:volatility"))
    return {
      family: "filter:volatility",
      hint: "Intraday range below STRATEGY_MIN_VOLATILITY_PCT — market too quiet.",
    };
  if (r.startsWith("filter:chop"))
    return {
      family: "filter:chop",
      hint: "Price action too sideways (chop_score above STRATEGY_MAX_CHOP_SCORE).",
    };
  if (r.startsWith("filter:"))
    return { family: r.split(" ")[0], hint: "Universe filter stopped this candidate." };
  if (r.startsWith("kind_disabled"))
    return { family: "kind_disabled", hint: "You toggled this category off in the dashboard." };
  if (r.startsWith("market_closed"))
    return { family: "market_closed", hint: "Outside the exchange's session window." };
  if (r.startsWith("resolve:"))
    return {
      family: "resolve",
      hint: "Couldn't turn the index signal into an option contract. Refresh the instrument master.",
    };
  if (r.startsWith("no_execution_price"))
    return {
      family: "no_execution_price",
      hint: "Option premium not in the scanner cache yet — usually clears after one full poll cycle.",
    };
  if (r.startsWith("option_lot_value_below_min") || r.startsWith("option_lot_value_above_max"))
    return {
      family: "lot_value_range",
      hint: "1-lot notional is outside STRATEGY_MIN/MAX_TRADE_VALUE.",
    };
  if (r.startsWith("need_more_capital"))
    return {
      family: "need_more_capital",
      hint: "Not enough cash for one full lot of the resolved option strike.",
    };
  if (r.startsWith("zero_lots_after_funds_cap"))
    return {
      family: "zero_lots",
      hint: "Risk-based size is below 1 lot after the deployable-cash cap.",
    };
  if (r.startsWith("risk:max_concurrent"))
    return {
      family: "risk:max_concurrent",
      hint: "BOT_MAX_CONCURRENT_POSITIONS reached — close one before the bot opens another.",
    };
  if (r.startsWith("risk:max_trades_today"))
    return {
      family: "risk:max_trades_today",
      hint: "RISK_MAX_TRADES_PER_DAY reached for today.",
    };
  if (r.startsWith("risk:max_trades_hour"))
    return {
      family: "risk:max_trades_hour",
      hint: "RISK_MAX_TRADES_PER_HOUR reached for the rolling 60-minute window.",
    };
  if (r.startsWith("risk:no_capital"))
    return { family: "risk:no_capital", hint: "Effective capital resolved to 0 — check broker funds." };
  if (r.startsWith("risk:max_daily_loss"))
    return { family: "risk:max_daily_loss", hint: "Daily realized P&L hit the kill-switch threshold." };
  if (r.startsWith("risk:zero_qty"))
    return { family: "risk:zero_qty", hint: "Risk-based sizing computed 0 quantity (entry vs stop too tight)." };
  if (r.startsWith("llm:"))
    return {
      family: "llm:skip",
      hint: "LLM classifier rejected the setup or fell below LLM_DECISION_THRESHOLD.",
    };
  if (r.startsWith("paper_book_full"))
    return { family: "paper_book_full", hint: "Paper book at PAPER_MAX_OPEN_POSITIONS." };
  if (r.startsWith("paper_open_error"))
    return { family: "paper_open_error", hint: "Internal error opening the paper position." };
  if (r.startsWith("place_order_error"))
    return { family: "place_order_error", hint: "Broker rejected the order — see decisions table for the exact text." };
  if (r.startsWith("invalid_payload"))
    return { family: "invalid_payload", hint: "Built order payload was missing a required field." };
  if (r.startsWith("duplicate_order_window"))
    return { family: "duplicate_order_window", hint: "Same order shape was placed within the last 60s." };
  if (r.startsWith("no_candidate"))
    return { family: "no_candidate", hint: "No instrument cleared score / signal gates this cycle." };
  return { family: r.split(":")[0] || "other", hint: "" };
}

export default function SkipReasonsPanel({ decisions }: Props) {
  const skips = decisions.filter(
    (d) => !d.placed && d.signal !== "MODE",
  );
  if (skips.length === 0) return null;

  const buckets = new Map<string, Bucket>();
  for (const d of skips.slice(0, 200)) {
    const { family, hint } = classify(d.reason);
    const b = buckets.get(family) ?? {
      family,
      hint,
      count: 0,
      examples: [],
    };
    b.count += 1;
    if (b.examples.length < 2 && d.reason && !b.examples.includes(d.reason)) {
      b.examples.push(d.reason);
    }
    buckets.set(family, b);
  }
  const ranked = Array.from(buckets.values()).sort((a, b) => b.count - a.count);
  const top = ranked.slice(0, 6);
  const total = skips.length;

  return (
    <div className="card border border-amber-500/20 bg-amber-500/5 p-4">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold tracking-wide text-amber-100">
          Why aren't trades being placed?
        </h2>
        <span className="text-[11px] uppercase tracking-wider text-amber-300/80">
          {total} skip{total === 1 ? "" : "s"} in the last cycles
        </span>
      </div>
      <ul className="mt-3 space-y-2">
        {top.map((b) => {
          const pct = Math.round((b.count / total) * 100);
          return (
            <li
              key={b.family}
              className="flex items-start justify-between gap-3 rounded-md border border-white/5 bg-slate-900/50 p-3"
            >
              <div className="min-w-0">
                <div className="font-mono text-[12px] text-slate-100">
                  {b.family}
                </div>
                {b.hint ? (
                  <div className="mt-0.5 text-[11px] text-slate-400">{b.hint}</div>
                ) : null}
                {b.examples.length > 0 ? (
                  <div className="mt-1 truncate text-[10px] text-slate-500">
                    e.g. {b.examples[0]}
                  </div>
                ) : null}
              </div>
              <div className="text-right">
                <div className="text-base font-semibold tabular-nums text-amber-200">
                  {b.count}
                </div>
                <div className="text-[10px] text-slate-500">{pct}%</div>
              </div>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
