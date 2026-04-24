"use client";
import { useEffect, useRef, useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { api, type SmokeTestResult } from "@/lib/api";
import { Play, Loader2 } from "lucide-react";

const badgeFor = (s: SmokeTestResult["status"]) =>
  ({
    pass: "success",
    fail: "destructive",
    skip: "warning",
    error: "destructive",
    running: "secondary",
  } as const)[s];

export function SmokeTestRunner() {
  const [run, setRun] = useState<SmokeTestResult | null>(null);
  const [starting, setStarting] = useState(false);
  const poll = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => () => { if (poll.current) clearInterval(poll.current); }, []);

  async function start() {
    setStarting(true);
    try {
      const r = await api.runSmokeTest();
      setRun(r);
      if (poll.current) clearInterval(poll.current);
      poll.current = setInterval(async () => {
        try {
          const latest = await api.getSmokeTest(r.id);
          setRun(latest);
          if (latest.status !== "running" && poll.current) {
            clearInterval(poll.current);
            poll.current = null;
          }
        } catch {
          /* keep polling */
        }
      }, 1500);
    } finally {
      setStarting(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <CardTitle>Run broker smoke test</CardTitle>
          {run && <Badge variant={badgeFor(run.status)}>{run.status.toUpperCase()}</Badge>}
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <Button onClick={start} disabled={starting || run?.status === "running"}>
          {starting || run?.status === "running" ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <Play className="h-4 w-4" />
          )}
          Run smoke test
        </Button>

        {run && (
          <div className="space-y-3">
            <div className="grid gap-2 text-sm sm:grid-cols-3">
              <Stat label="Started" value={new Date(run.started_at).toLocaleTimeString()} />
              <Stat label="Finished" value={run.finished_at ? new Date(run.finished_at).toLocaleTimeString() : "—"} />
              <Stat label="Exit code" value={run.exit_code ?? "—"} />
            </div>
            {run.stdout && (
              <Block title="stdout" content={run.stdout} />
            )}
            {run.stderr && (
              <Block title="stderr" content={run.stderr} />
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="rounded-lg border border-border bg-card/60 p-3">
      <div className="text-xs uppercase tracking-wider text-muted-foreground">{label}</div>
      <div className="mt-1 tabular-nums">{value}</div>
    </div>
  );
}

function Block({ title, content }: { title: string; content: string }) {
  return (
    <div className="rounded-xl border border-border bg-card/60">
      <div className="border-b border-border px-3 py-1.5 text-xs uppercase tracking-wider text-muted-foreground">
        {title}
      </div>
      <pre className="max-h-64 overflow-auto p-3 text-xs leading-relaxed">
        <code>{content}</code>
      </pre>
    </div>
  );
}
