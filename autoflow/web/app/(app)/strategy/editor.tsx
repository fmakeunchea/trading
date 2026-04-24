"use client";
import { useState, useTransition } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { api, type Strategy } from "@/lib/api";

export function StrategyEditor({ strategy }: { strategy: Strategy }) {
  const [form, setForm] = useState(strategy);
  const [pending, startTransition] = useTransition();
  const [msg, setMsg] = useState<string | null>(null);

  function save() {
    setMsg(null);
    startTransition(async () => {
      try {
        const updated = await api.updateStrategy(form.id, {
          name: form.name,
          mode: form.mode,
          symbols: form.symbols,
          daily_loss_cap_pct: form.daily_loss_cap_pct,
          max_concurrent_positions: form.max_concurrent_positions,
          position_notional_pct: form.position_notional_pct,
        });
        setForm(updated);
        setMsg("Saved. Click Apply to write to YAML, then restart the bot.");
      } catch (e) {
        setMsg(e instanceof Error ? e.message : "save failed");
      }
    });
  }

  function apply() {
    setMsg(null);
    startTransition(async () => {
      try {
        const r = await api.applyStrategy(form.id);
        setMsg(r.message ?? (r.ok ? "Applied." : "Apply failed."));
        const refreshed = await api.strategies();
        const me = refreshed.find((s) => s.id === form.id);
        if (me) setForm(me);
      } catch (e) {
        setMsg(e instanceof Error ? e.message : "apply failed");
      }
    });
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <CardTitle>{form.name}</CardTitle>
          <Badge variant={form.mode === "paper" ? "secondary" : "warning"}>{form.mode}</Badge>
        </div>
      </CardHeader>
      <CardContent className="space-y-6">
        <div className="grid gap-4 md:grid-cols-2">
          <Field label="Name">
            <Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          </Field>
          <Field label="Symbols (comma-separated)">
            <Input
              value={form.symbols.join(", ")}
              onChange={(e) =>
                setForm({
                  ...form,
                  symbols: e.target.value
                    .split(",")
                    .map((s) => s.trim().toUpperCase())
                    .filter(Boolean),
                })
              }
            />
          </Field>
          <Field label="Daily loss cap (fraction)">
            <Input
              type="number"
              step="0.001"
              min="0"
              max="1"
              value={form.daily_loss_cap_pct}
              onChange={(e) =>
                setForm({ ...form, daily_loss_cap_pct: Number(e.target.value) })
              }
            />
          </Field>
          <Field label="Max concurrent positions">
            <Input
              type="number"
              step="1"
              min="1"
              max="50"
              value={form.max_concurrent_positions}
              onChange={(e) =>
                setForm({ ...form, max_concurrent_positions: Number(e.target.value) })
              }
            />
          </Field>
          <Field label="Position notional (fraction of equity)">
            <Input
              type="number"
              step="0.01"
              min="0"
              max="1"
              value={form.position_notional_pct}
              onChange={(e) =>
                setForm({ ...form, position_notional_pct: Number(e.target.value) })
              }
            />
          </Field>
          <Field label="Mode">
            <div className="flex h-10 items-center gap-3 rounded-lg border border-border px-3">
              <span className="text-sm text-muted-foreground">paper</span>
              <Switch
                checked={form.mode === "live"}
                onCheckedChange={(live) => setForm({ ...form, mode: live ? "live" : "paper" })}
              />
              <span className="text-sm">live</span>
            </div>
          </Field>
        </div>

        <div className="flex flex-wrap items-center gap-3">
          <Button onClick={save} disabled={pending}>Save changes</Button>
          <Button
            variant="outline"
            onClick={apply}
            disabled={pending || !form.dirty}
            title={form.dirty ? "Write DB values to YAML" : "Nothing to apply"}
          >
            Apply to YAML
          </Button>
          {form.dirty && <Badge variant="warning">Unsaved → YAML</Badge>}
          {!form.dirty && form.restart_required && (
            <Badge variant="warning">Restart bot to take effect</Badge>
          )}
          {msg && <span className="text-sm text-muted-foreground">{msg}</span>}
        </div>
      </CardContent>
    </Card>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="space-y-2">
      <Label className="text-xs uppercase tracking-wider text-muted-foreground">{label}</Label>
      {children}
    </div>
  );
}
