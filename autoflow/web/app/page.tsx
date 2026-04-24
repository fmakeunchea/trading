import Link from "next/link";
import { Hero } from "@/components/landing/hero";
import { Benefits } from "@/components/landing/benefits";
import { Features } from "@/components/landing/features";
import { Pricing } from "@/components/landing/pricing";
import { CTA } from "@/components/landing/cta";

export default function LandingPage() {
  return (
    <main>
      <header className="sticky top-0 z-40 border-b border-border bg-background/80 backdrop-blur">
        <div className="container flex h-14 items-center justify-between">
          <Link href="/" className="flex items-center gap-2 font-semibold">
            <span className="h-6 w-6 rounded-md bg-primary" />
            AutoFlow Trader
          </Link>
          <nav className="flex items-center gap-6 text-sm text-muted-foreground">
            <Link href="#pricing" className="hover:text-foreground">Pricing</Link>
            <Link href="/dashboard" className="hover:text-foreground">Dashboard</Link>
            <Link
              href="#contact"
              className="rounded-lg bg-primary px-3 py-1.5 text-primary-foreground hover:bg-primary/90"
            >
              Request demo
            </Link>
          </nav>
        </div>
      </header>
      <Hero />
      <Benefits />
      <Features />
      <Pricing />
      <CTA />
      <footer className="border-t border-border py-8 text-center text-sm text-muted-foreground">
        &copy; {new Date().getFullYear()} AutoFlow Trader. Not investment advice.
      </footer>
    </main>
  );
}
