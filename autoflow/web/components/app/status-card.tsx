import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

type Tone = "ok" | "warn" | "bad" | "idle";

interface Props {
  label: string;
  value: string;
  detail?: string;
  tone?: Tone;
  icon?: React.ReactNode;
}

const toneBadge: Record<Tone, "success" | "warning" | "destructive" | "secondary"> = {
  ok: "success",
  warn: "warning",
  bad: "destructive",
  idle: "secondary",
};

const toneLabel: Record<Tone, string> = {
  ok: "OK",
  warn: "WARN",
  bad: "BAD",
  idle: "IDLE",
};

export function StatusCard({ label, value, detail, tone = "idle", icon }: Props) {
  return (
    <Card>
      <CardContent className="p-5">
        <div className="flex items-center justify-between">
          <div className="text-xs uppercase tracking-wider text-muted-foreground">{label}</div>
          <Badge variant={toneBadge[tone]} className="text-[10px]">
            {toneLabel[tone]}
          </Badge>
        </div>
        <div className="mt-3 flex items-center gap-2">
          {icon && <div className="text-muted-foreground">{icon}</div>}
          <div className={cn("text-2xl font-semibold tabular-nums")}>{value}</div>
        </div>
        {detail && <div className="mt-1 text-xs text-muted-foreground">{detail}</div>}
      </CardContent>
    </Card>
  );
}
