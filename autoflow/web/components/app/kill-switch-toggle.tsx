"use client";
import { useState, useTransition } from "react";
import { Switch } from "@/components/ui/switch";
import { Label } from "@/components/ui/label";
import { api } from "@/lib/api";
import { AlertTriangle } from "lucide-react";

export function KillSwitchToggle({ initialEngaged }: { initialEngaged: boolean }) {
  const [engaged, setEngaged] = useState(initialEngaged);
  const [pending, startTransition] = useTransition();

  function toggle(next: boolean) {
    setEngaged(next);
    startTransition(async () => {
      try {
        await api.setKillSwitch(next);
      } catch {
        setEngaged(!next);
      }
    });
  }

  return (
    <div className="flex items-center justify-between gap-4 rounded-xl border border-border bg-card/60 p-4">
      <div className="flex items-start gap-3">
        <AlertTriangle
          className={engaged ? "mt-0.5 h-5 w-5 text-destructive" : "mt-0.5 h-5 w-5 text-muted-foreground"}
        />
        <div>
          <Label className="text-sm font-semibold">Kill switch</Label>
          <p className="text-xs text-muted-foreground">
            Writes <code className="text-[11px]">var/trading_bot.kill</code> — the engine halts on its next tick.
          </p>
        </div>
      </div>
      <Switch checked={engaged} onCheckedChange={toggle} disabled={pending} />
    </div>
  );
}
