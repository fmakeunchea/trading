import { SmokeTestRunner } from "./runner";

export const dynamic = "force-dynamic";

export default function SmokeTestPage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Broker smoke test</h1>
        <p className="text-sm text-muted-foreground">
          Exercises OTO placement, cancel, flatten and partial-fill handling against the paper broker.
          Must pass before switching to live mode.
        </p>
      </div>
      <SmokeTestRunner />
    </div>
  );
}
