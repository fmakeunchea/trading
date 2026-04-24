import Link from "next/link";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Check } from "lucide-react";

const tiers = [
  {
    name: "Solo",
    price: "$49",
    cadence: "/mo",
    tagline: "One strategy, one VPS.",
    highlight: false,
    features: [
      "1 active strategy",
      "Paper + live modes",
      "Kill switch + reconcile",
      "7-day incident retention",
      "Email support",
    ],
  },
  {
    name: "Operator",
    price: "$149",
    cadence: "/mo",
    tagline: "For serious paper-to-live workflows.",
    highlight: true,
    features: [
      "5 active strategies",
      "Full smoke-test suite",
      "90-day incident retention",
      "Slack/Discord alerts (soon)",
      "Priority support",
    ],
  },
  {
    name: "Team",
    price: "Contact",
    cadence: "",
    tagline: "Multi-user, multi-tenant.",
    highlight: false,
    features: [
      "Unlimited strategies",
      "Role-based access",
      "Audit export",
      "Custom deploy targets",
      "Dedicated support",
    ],
  },
];

export function Pricing() {
  return (
    <section id="pricing" className="container py-20">
      <div className="mx-auto max-w-2xl text-center">
        <h2 className="text-3xl font-semibold tracking-tight md:text-4xl">Pricing</h2>
        <p className="mt-4 text-muted-foreground">Start on paper. Upgrade when you go live.</p>
      </div>
      <div className="mt-12 grid gap-6 md:grid-cols-3">
        {tiers.map((t) => (
          <Card key={t.name} className={t.highlight ? "border-accent shadow-soft ring-1 ring-accent/40" : ""}>
            <CardHeader>
              <div className="flex items-center justify-between">
                <CardTitle className="text-lg">{t.name}</CardTitle>
                {t.highlight && <Badge>Most popular</Badge>}
              </div>
              <div className="mt-2 flex items-baseline gap-1">
                <span className="text-4xl font-semibold">{t.price}</span>
                <span className="text-muted-foreground">{t.cadence}</span>
              </div>
              <p className="text-sm text-muted-foreground">{t.tagline}</p>
            </CardHeader>
            <CardContent className="space-y-4">
              <ul className="space-y-2 text-sm">
                {t.features.map((f) => (
                  <li key={f} className="flex items-start gap-2">
                    <Check className="mt-0.5 h-4 w-4 shrink-0 text-success" />
                    <span>{f}</span>
                  </li>
                ))}
              </ul>
              <Button className="w-full" variant={t.highlight ? "default" : "outline"} asChild>
                <Link href="#contact">Get started</Link>
              </Button>
            </CardContent>
          </Card>
        ))}
      </div>
    </section>
  );
}
