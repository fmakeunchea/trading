import Link from "next/link";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";

export function CTA() {
  return (
    <section id="contact" className="container py-20">
      <Card className="mx-auto max-w-3xl overflow-hidden border-accent">
        <CardContent className="flex flex-col items-center gap-6 p-10 text-center md:p-14">
          <h2 className="text-balance text-3xl font-semibold tracking-tight md:text-4xl">
            Ship strategies with the safety rails on.
          </h2>
          <p className="max-w-xl text-muted-foreground">
            Get a walkthrough of the platform. Paper trial in 10 minutes; live deployment when you&apos;re
            ready.
          </p>
          <div className="flex flex-col gap-3 sm:flex-row">
            <Button size="lg" asChild>
              <Link href="mailto:hello@autoflow.trade?subject=AutoFlow%20demo">Request demo</Link>
            </Button>
            <Button size="lg" variant="outline" asChild>
              <Link href="/dashboard">Open dashboard</Link>
            </Button>
          </div>
        </CardContent>
      </Card>
    </section>
  );
}
