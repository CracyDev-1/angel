type Props = {
  onCancel: () => void;
  onConfirm: () => void;
  submitting: boolean;
};

export default function GoLiveModal({ onCancel, onConfirm, submitting }: Props) {
  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div className="card w-full max-w-md p-6">
        <div className="mb-2 flex items-center gap-2">
          <span className="pill-amber">SWITCH TO LIVE</span>
        </div>
        <h2 className="text-lg font-semibold">Send real orders to Angel One?</h2>
        <p className="mt-2 text-sm leading-relaxed text-slate-300">
          The bot will stop being a paper trader and start placing real orders against
          your funds the moment a setup passes the strategy and risk checks. This
          cannot be undone for orders already in flight.
        </p>
        <ul className="mt-3 list-disc space-y-1 pl-5 text-xs text-slate-400">
          <li>All <code className="text-slate-300">RISK_*</code> caps in <code className="text-slate-300">.env</code> still apply.</li>
          <li>Index signals resolve to ATM CE/PE from the instrument master — refresh the master if strikes fail.</li>
          <li>Switch back to Dry Run from the header at any time — pending positions stay open.</li>
          <li>No system can guarantee profit. You are responsible for losses.</li>
        </ul>
        <div className="mt-5 flex gap-2">
          <button className="btn-ghost flex-1" onClick={onCancel} disabled={submitting}>
            Cancel
          </button>
          <button
            className="btn-danger flex-1"
            onClick={onConfirm}
            disabled={submitting}
            autoFocus
          >
            {submitting ? "Switching…" : "Yes, go live"}
          </button>
        </div>
      </div>
    </div>
  );
}
