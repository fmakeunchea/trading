"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { Activity, FlaskConical, LayoutDashboard, ListTree, Settings2 } from "lucide-react";
import { cn } from "@/lib/utils";

const nav = [
  { href: "/dashboard", label: "Dashboard", icon: LayoutDashboard },
  { href: "/strategy", label: "Strategy", icon: Settings2 },
  { href: "/incidents", label: "Incidents", icon: ListTree },
  { href: "/smoke-test", label: "Smoke Test", icon: FlaskConical },
];

export function Sidebar() {
  const path = usePathname();
  return (
    <aside className="hidden w-60 shrink-0 border-r border-border bg-card/30 md:flex md:flex-col">
      <div className="flex h-14 items-center gap-2 border-b border-border px-5 font-semibold">
        <span className="h-6 w-6 rounded-md bg-primary" />
        AutoFlow
      </div>
      <nav className="flex-1 space-y-1 p-3">
        {nav.map((n) => {
          const active = path?.startsWith(n.href);
          return (
            <Link
              key={n.href}
              href={n.href}
              className={cn(
                "flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition-colors",
                active
                  ? "bg-accent text-accent-foreground"
                  : "text-muted-foreground hover:bg-accent/50 hover:text-foreground"
              )}
            >
              <n.icon className="h-4 w-4" />
              {n.label}
            </Link>
          );
        })}
      </nav>
      <div className="border-t border-border p-3 text-xs text-muted-foreground">
        <div className="flex items-center gap-2">
          <Activity className="h-3.5 w-3.5" />
          MVP · v0.1
        </div>
      </div>
    </aside>
  );
}
