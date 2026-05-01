import { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { apiPost } from "../lib/api";
import { formatINR } from "../lib/format";

type Props = {
  liveAvailableCash: number;
  override: number;
  deployable: number;
  useCapitalPct?: number;
};

/**
 * Inline control that lets the user pretend they have ₹X for the dry-run sim.
 * The bot keeps using LIVE broker cash for everything except sizing decisions
 * — it is purely a "what-if" knob. Setting 0 reverts to live cash.
 *
 * Visible only in dry-run; the parent decides when to render.
 */
export default function DryrunCapital({
  liveAvailableCash,
  override,
  deployable,
  useCapitalPct,
}: Props) {
  const qc = useQueryClient();
  const [draft, setDraft] = useState<string>(override > 0 ? String(Math.round(override)) : "");

  // Keep the input in sync if the override is changed by another tab / API call.
  useEffect(() => {
    setDraft(override > 0 ? String(Math.round(override)) : "");
  }, [override]);

  const setCapital = useMutation({
    mutationFn: (amount: number) => apiPost("/api/dryrun/capital", { amount }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["snapshot"] }),
  });

  function commit(amount: number) {
    setCapital.mutate(Math.max(0, amount));
  }

  function applyDraft() {
    const num = Number((draft || "0").replace(/[, ]/g, ""));
    if (!Number.isFinite(num)) return;
    commit(num);
  }

  const usingOverride = override > 0;
  const base = usingOverride ? override : liveAvailableCash;
  // If a known cap was passed, prefer it; otherwise infer from base vs deployable.
  const inferredPct = base > 0 ? (deployable / base) * 100 : 100;
  const pct = typeof useCapitalPct === "number" ? useCapitalPct : inferredPct;
  const isCapped = pct < 99.5 && base > 0 && deployable < base * 0.999;

  return (
    <div className="card flex flex-col gap-3 p-4 md:flex-row md:items-center md:justify-between">
      <div>
        <div className="text-xs uppercase tracking-wider text-slate-400">Dry-run capital</div>
        <div className="mt-1 text-sm text-slate-200">
          Bot is sizing trades against{" "}
          <span className="font-semibold accent-text">{formatINR(deployable)}</span> deployable cash.
        </div>
        <div className="mt-1 text-[11px] text-slate-500">
          {usingOverride
            ? `Override active. Real broker cash is ${formatINR(liveAvailableCash)}.`
            : `Using your real broker cash (${formatINR(liveAvailableCash)}). Set a custom amount to stress-test.`}
          {isCapped ? (
            <>
              {" "}<span className="text-amber-400">
                BOT_USE_CAPITAL_PCT is {pct}% — set it to 100 in .env to deploy every rupee.
              </span>
            </>
          ) : null}
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <div className="flex items-center gap-1 rounded-lg border border-white/10 bg-slate-900/80 px-2">
          <span className="text-xs text-slate-400">₹</span>
          <input
            inputMode="numeric"
            className="w-32 bg-transparent px-2 py-2 text-sm outline-none placeholder:text-slate-500"
            placeholder="e.g. 200000"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") applyDraft();
            }}
          />
        </div>
        <button
          className="btn-primary text-xs"
          onClick={applyDraft}
          disabled={setCapital.isPending}
        >
          {setCapital.isPending ? "…" : "Apply"}
        </button>
        <button
          className="btn-ghost text-xs"
          onClick={() => commit(0)}
          disabled={setCapital.isPending || !usingOverride}
          title="Use live broker cash for sizing"
        >
          Reset
        </button>
        {[100_000, 250_000, 500_000, 1_000_000].map((v) => (
          <button
            key={v}
            className="pill-blue text-[10px] hover:opacity-80"
            onClick={() => commit(v)}
          >
            {formatINR(v, { compact: true })}
          </button>
        ))}
      </div>
    </div>
  );
}
