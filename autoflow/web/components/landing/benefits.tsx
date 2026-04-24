import { Card, CardContent } from "@/components/ui/card";
import { ShieldAlert, Activity, GitCompare, FlaskConical } from "lucide-react";

const items = [
  {
    icon: ShieldAlert,
    title: "Halt before damage",
    body: "One-click kill switch writes a filesystem signal the engine checks every tick. No API round-trip, no race.",
  },
  {
    icon: GitCompare,
    title: "Know when state drifts",
    body: "Continuous reconcile between your local state and broker truth. Mismatches surface as incidents, not silent losses.",
  },
  {
    icon: Activity,
    title: "Prove it's alive",
    body: "Heartbeat file, process liveness check, and last-broker-contact timestamp — so you notice a stuck bot in seconds, not hours.",
  },
  {
    icon: FlaskConical,
    title: "Validate before you trade",
    body: "Built-in paper smoke test exercises OTO, cancel, flatten, and partial-fill flows against your broker before you flip to live.",
  },
];

export function Benefits() {
  return (
    <section className="container py-20">
      <div className="mx-auto max-w-2xl text-center">
        <h2 className="text-3xl font-semibold tracking-tight md:text-4xl">
          Operational safety, not signal alpha
        </h2>
        <p className="mt-4 text-muted-foreground">
          Most trading platforms promise edge. AutoFlow promises that when something goes wrong,
          you find out immediately — and can stop the bleeding.
        </p>
      </div>
      <div className="mt-12 grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        {items.map((it) => (
          <Card key={it.title} className="transition-colors hover:border-accent">
            <CardContent className="p-6">
              <it.icon className="h-6 w-6 text-muted-foreground" />
              <h3 className="mt-4 font-semibold">{it.title}</h3>
              <p className="mt-2 text-sm text-muted-foreground">{it.body}</p>
            </CardContent>
          </Card>
        ))}
      </div>
    </section>
  );
}
