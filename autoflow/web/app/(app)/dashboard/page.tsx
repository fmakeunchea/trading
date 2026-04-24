import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { StatusCard } from "@/components/app/status-card";
import { KillSwitchToggle } from "@/components/app/kill-switch-toggle";
import { BotControls } from "@/components/app/bot-controls";
import { api } from "@/lib/api";
import { timeAgo } from "@/lib/utils";
import { Activity, Heart, Plug, ShieldAlert } from "lucide-react";

export const dynamic = "force-dynamic";

export default async function DashboardPage() {
  const [status, positions, trades] = await Promise.all([
    api.status().catch(() => null),
    api.positions().catch(() => []),
    api.orders(10).catch(() => []),
  ]);

  const running = !!status?.running;
  const hbFresh = !!status?.heartbeat_fresh;
  const reconcileOk = status?.reconcile_ok;
  const brokerOk = !!status?.broker_connected;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Dashboard</h1>
          <p className="text-sm text-muted-foreground">Live view of the trading engine.</p>
        </div>
        <BotControls running={running} />
      </div>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatusCard
          label="Strategy"
          value={running ? "Running" : "Stopped"}
          detail={status?.mode ? `Mode: ${status.mode}` : undefined}
          tone={running ? "ok" : "idle"}
          icon={<Activity className="h-5 w-5" />}
        />
        <StatusCard
          label="Heartbeat"
          value={hbFresh ? "Fresh" : "Stale"}
          detail={timeAgo(status?.heartbeat_at)}
          tone={hbFresh ? "ok" : "bad"}
          icon={<Heart className="h-5 w-5" />}
        />
        <StatusCard
          label="Reconcile"
          value={reconcileOk === null || reconcileOk === undefined ? "—" : reconcileOk ? "In sync" : "Drift"}
          tone={reconcileOk === null || reconcileOk === undefined ? "idle" : reconcileOk ? "ok" : "bad"}
        />
        <StatusCard
          label="Broker"
          value={brokerOk ? "Connected" : "Unknown"}
          tone={brokerOk ? "ok" : "warn"}
          icon={<Plug className="h-5 w-5" />}
        />
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <div className="space-y-4 lg:col-span-2">
          <Card>
            <CardHeader>
              <CardTitle>Open positions</CardTitle>
            </CardHeader>
            <CardContent>
              {positions.length === 0 ? (
                <p className="text-sm text-muted-foreground">No open positions.</p>
              ) : (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Symbol</TableHead>
                      <TableHead>Side</TableHead>
                      <TableHead className="text-right">Qty</TableHead>
                      <TableHead className="text-right">Avg</TableHead>
                      <TableHead className="text-right">PnL</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {positions.map((p) => (
                      <TableRow key={p.symbol}>
                        <TableCell className="font-medium">{p.symbol}</TableCell>
                        <TableCell>
                          <Badge variant={p.side === "long" ? "success" : "destructive"}>
                            {p.side}
                          </Badge>
                        </TableCell>
                        <TableCell className="text-right tabular-nums">{p.qty}</TableCell>
                        <TableCell className="text-right tabular-nums">
                          {p.avg_price?.toFixed(2) ?? "—"}
                        </TableCell>
                        <TableCell className="text-right tabular-nums">
                          {p.unrealized_pnl?.toFixed(2) ?? "—"}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Recent trades</CardTitle>
            </CardHeader>
            <CardContent>
              {trades.length === 0 ? (
                <p className="text-sm text-muted-foreground">No trades yet.</p>
              ) : (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Time</TableHead>
                      <TableHead>Symbol</TableHead>
                      <TableHead>Side</TableHead>
                      <TableHead className="text-right">Qty</TableHead>
                      <TableHead className="text-right">Fill</TableHead>
                      <TableHead>Status</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {trades.map((t) => (
                      <TableRow key={t.id}>
                        <TableCell className="text-muted-foreground">{timeAgo(t.occurred_at)}</TableCell>
                        <TableCell className="font-medium">{t.symbol}</TableCell>
                        <TableCell>{t.side}</TableCell>
                        <TableCell className="text-right tabular-nums">{t.qty}</TableCell>
                        <TableCell className="text-right tabular-nums">
                          {t.avg_fill_price?.toFixed(2) ?? "—"}
                        </TableCell>
                        <TableCell>
                          <Badge variant="outline">{t.status}</Badge>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </CardContent>
          </Card>
        </div>

        <div className="space-y-4">
          <KillSwitchToggle initialEngaged={!!status?.kill_switch_engaged} />
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <ShieldAlert className="h-4 w-4" /> Today&apos;s signals
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-2 text-sm">
              <Row label="Incidents today" value={String(status?.incidents_today ?? 0)} />
              <Row label="Open positions" value={String(status?.open_positions_count ?? 0)} />
              <Row label="Broker last contact" value={timeAgo(status?.heartbeat_at ?? null)} />
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-muted-foreground">{label}</span>
      <span className="tabular-nums">{value}</span>
    </div>
  );
}
