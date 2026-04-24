import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { api } from "@/lib/api";
import { StrategyEditor } from "./editor";

export const dynamic = "force-dynamic";

export default async function StrategyPage() {
  const strategies = await api.strategies().catch(() => []);
  const active = strategies.find((s) => s.is_active) ?? strategies[0];

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Strategy</h1>
        <p className="text-sm text-muted-foreground">
          Configure symbols, risk parameters, and mode. Changes take effect on the next bot start.
        </p>
      </div>

      {!active ? (
        <Card>
          <CardHeader><CardTitle>No strategies yet</CardTitle></CardHeader>
          <CardContent className="text-sm text-muted-foreground">
            The seed strategy should have been created on first boot. Check the Postgres migration ran.
          </CardContent>
        </Card>
      ) : (
        <StrategyEditor strategy={active} />
      )}
    </div>
  );
}
