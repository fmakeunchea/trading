"use client";
import { useState, useTransition } from "react";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { Play, Square } from "lucide-react";

export function BotControls({ running }: { running: boolean }) {
  const [msg, setMsg] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();

  const act = (fn: () => Promise<{ ok: boolean; message: string | null }>) =>
    startTransition(async () => {
      setMsg(null);
      try {
        const r = await fn();
        setMsg(r.message);
        if (r.ok) setTimeout(() => location.reload(), 800);
      } catch (e) {
        setMsg(e instanceof Error ? e.message : "failed");
      }
    });

  return (
    <div className="flex items-center gap-2">
      {running ? (
        <Button variant="destructive" size="sm" disabled={pending} onClick={() => act(api.stopBot)}>
          <Square className="h-4 w-4" />
          Stop bot
        </Button>
      ) : (
        <Button size="sm" disabled={pending} onClick={() => act(api.startBot)}>
          <Play className="h-4 w-4" />
          Start bot
        </Button>
      )}
      {msg && <span className="text-xs text-muted-foreground">{msg}</span>}
    </div>
  );
}
