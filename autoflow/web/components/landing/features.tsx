import { Card, CardContent } from "@/components/ui/card";
import { Check } from "lucide-react";

const features = [
  "Kill switch (filesystem-signal, checked every engine tick)",
  "Continuous reconciliation (local state vs. broker truth)",
  "Protective-order lifecycle visibility (OTO, OCO, orphans)",
  "Hash-chained append-only incident log",
  "Heartbeat health + process-liveness monitoring",
  "Paper smoke test (OTO, cancel, flatten, partial-fill)",
  "VPS deployment — single docker-compose up",
  "Systemd + container restart recovery",
  "Daily loss cap, max concurrent positions, spread filter",
  "Session-window gating (UTC open/close)",
];

export function Features() {
  return (
    <section className="border-y border-border bg-card/30 py-20">
      <div className="container">
        <div className="mx-auto max-w-2xl text-center">
          <h2 className="text-3xl font-semibold tracking-tight md:text-4xl">
            What&apos;s in the box
          </h2>
          <p className="mt-4 text-muted-foreground">
            Every control a production trading operator expects, available on day one.
          </p>
        </div>
        <Card className="mx-auto mt-12 max-w-3xl">
          <CardContent className="p-6">
            <ul className="grid gap-3 sm:grid-cols-2">
              {features.map((f) => (
                <li key={f} className="flex items-start gap-2 text-sm">
                  <Check className="mt-0.5 h-4 w-4 shrink-0 text-success" />
                  <span>{f}</span>
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      </div>
    </section>
  );
}
