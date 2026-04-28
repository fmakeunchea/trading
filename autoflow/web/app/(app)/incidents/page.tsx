import { IncidentsView } from "./view";

export const dynamic = "force-dynamic";

export default function IncidentsPage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Incidents</h1>
        <p className="text-sm text-muted-foreground">
          Critical = reconcile drift, halts, orphan orders, restart recoveries.
          Diagnostics = per-symbol decision stream (no_signal, risk_denied,
          bars_fetched). Both are mirrored from the engine&apos;s append-only
          audit log.
        </p>
      </div>
      <IncidentsView />
    </div>
  );
}
