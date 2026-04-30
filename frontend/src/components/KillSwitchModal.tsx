import { useState } from "react";

type Props = {
  onCancel: () => void;
  onConfirm: (opts: { cancel_pending: boolean; square_off: boolean }) => void;
  submitting: boolean;
};

export default function KillSwitchModal({ onCancel, onConfirm, submitting }: Props) {
  const [cancelPending, setCancelPending] = useState(true);
  const [squareOff, setSquareOff] = useState(true);
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm">
      <div className="card w-full max-w-md p-6">
        <div className="mb-2 flex items-center gap-2">
          <span className="pill-red">EMERGENCY STOP</span>
        </div>
        <h2 className="text-lg font-semibold">Stop everything?</h2>
        <p className="mt-2 text-sm leading-relaxed text-slate-300">
          This will halt the bot, switch it back to dry-run, and (optionally) cancel
          unfilled orders and close every open position with market orders.
        </p>

        <div className="mt-4 space-y-2">
          <label className="flex cursor-pointer items-center gap-2 rounded-lg border border-white/5 bg-slate-900/40 p-3 text-sm">
            <input
              type="checkbox"
              checked={cancelPending}
              onChange={(e) => setCancelPending(e.target.checked)}
            />
            <div>
              <div className="font-medium">Cancel my pending orders</div>
              <div className="text-xs text-slate-400">
                Calls Angel <code>cancelOrder</code> for every still-open order this bot placed.
              </div>
            </div>
          </label>
          <label className="flex cursor-pointer items-center gap-2 rounded-lg border border-white/5 bg-slate-900/40 p-3 text-sm">
            <input
              type="checkbox"
              checked={squareOff}
              onChange={(e) => setSquareOff(e.target.checked)}
            />
            <div>
              <div className="font-medium">Square-off all open positions</div>
              <div className="text-xs text-slate-400">
                Sends a market order in the opposite direction for every open position
                shown by the broker. Slippage on illiquid options is real.
              </div>
            </div>
          </label>
        </div>

        <div className="mt-5 flex gap-2">
          <button className="btn-ghost flex-1" onClick={onCancel} disabled={submitting}>
            Cancel
          </button>
          <button
            className="btn-danger flex-1"
            onClick={() => onConfirm({ cancel_pending: cancelPending, square_off: squareOff })}
            disabled={submitting}
            autoFocus
          >
            {submitting ? "Stopping…" : "Yes, stop everything"}
          </button>
        </div>
      </div>
    </div>
  );
}
