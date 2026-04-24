import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { api, type Incident } from "@/lib/api";
import { formatTime, timeAgo } from "@/lib/utils";

export const dynamic = "force-dynamic";

const severityVariant = (sev: string) =>
  sev === "error" ? "destructive" : sev === "warn" ? "warning" : "secondary";

export default async function IncidentsPage() {
  const incidents = await api.incidents(200).catch(() => [] as Incident[]);
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Incidents</h1>
        <p className="text-sm text-muted-foreground">
          Reconcile events, halts, orphan-order alerts, restart recoveries — mirrored from the
          engine&apos;s append-only audit log.
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Recent events</CardTitle>
        </CardHeader>
        <CardContent>
          {incidents.length === 0 ? (
            <p className="text-sm text-muted-foreground">No incidents recorded yet.</p>
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
                {incidents.map((i) => (
                  <TableRow key={i.id}>
                    <TableCell className="whitespace-nowrap">
                      <div className="text-sm">{formatTime(i.occurred_at)}</div>
                      <div className="text-xs text-muted-foreground">{timeAgo(i.occurred_at)}</div>
                    </TableCell>
                    <TableCell><Badge variant="outline">{i.kind}</Badge></TableCell>
                    <TableCell>
                      <Badge variant={severityVariant(i.severity)}>{i.severity}</Badge>
                    </TableCell>
                    <TableCell className="text-sm text-muted-foreground">
                      {i.symbols?.join(", ") || "—"}
                    </TableCell>
                    <TableCell className="max-w-md truncate text-sm">{i.reason ?? "—"}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
