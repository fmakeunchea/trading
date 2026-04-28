"use client";
import { useEffect, useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import { api, type Incident, type IncidentCategory } from "@/lib/api";
import { formatTime, timeAgo, cn } from "@/lib/utils";

const severityVariant = (sev: string) =>
  sev === "error" ? "destructive" : sev === "warn" ? "warning" : "secondary";

const TABS: { key: IncidentCategory; label: string }[] = [
  { key: "critical", label: "Critical" },
  { key: "diagnostic", label: "Diagnostics" },
  { key: "all", label: "All" },
];

export function IncidentsView() {
  const [category, setCategory] = useState<IncidentCategory>("critical");
  const [rows, setRows] = useState<Incident[] | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api
      .incidents(200, category)
      .then((r) => {
        if (!cancelled) setRows(r);
      })
      .catch(() => {
        if (!cancelled) setRows([]);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [category]);

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between gap-4 space-y-0">
        <CardTitle>Recent events</CardTitle>
        <div className="flex gap-1 rounded-lg border border-border p-0.5">
          {TABS.map((t) => (
            <Button
              key={t.key}
              size="sm"
              variant={category === t.key ? "default" : "ghost"}
              onClick={() => setCategory(t.key)}
              className={cn("h-7 rounded-md text-xs")}
            >
              {t.label}
            </Button>
          ))}
        </div>
      </CardHeader>
      <CardContent>
        {loading && rows === null ? (
          <p className="text-sm text-muted-foreground">Loading…</p>
        ) : !rows || rows.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            {category === "critical"
              ? "No critical incidents — strategy is running clean."
              : category === "diagnostic"
                ? "No diagnostic records yet. They appear once the bot starts evaluating signals."
                : "No events recorded."}
          </p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>When</TableHead>
                <TableHead>Kind</TableHead>
                <TableHead>Severity</TableHead>
                <TableHead>Symbols</TableHead>
                <TableHead>Reason</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((i) => (
                <TableRow key={i.id}>
                  <TableCell className="whitespace-nowrap">
                    <div className="text-sm">{formatTime(i.occurred_at)}</div>
                    <div className="text-xs text-muted-foreground">
                      {timeAgo(i.occurred_at)}
                    </div>
                  </TableCell>
                  <TableCell>
                    <Badge
                      variant={i.kind === "DIAGNOSTIC" ? "secondary" : "outline"}
                    >
                      {i.kind === "DIAGNOSTIC"
                        ? // Surface the actual diagnostic decision (no_signal /
                          // risk_denied / bars_fetched) instead of the bare DIAGNOSTIC label.
                          (typeof i.payload?.payload === "object" &&
                          i.payload?.payload !== null
                            ? (i.payload.payload as { decision?: string }).decision ??
                              "DIAGNOSTIC"
                            : "DIAGNOSTIC")
                        : i.kind}
                    </Badge>
                  </TableCell>
                  <TableCell>
                    <Badge variant={severityVariant(i.severity)}>
                      {i.severity}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    {i.symbols?.join(", ") || "—"}
                  </TableCell>
                  <TableCell className="max-w-md truncate text-sm">
                    {i.reason ?? "—"}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}
