import { AlertTriangle, Gauge, PiggyBank, XCircle } from "lucide-react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";

const ALERT_ICONS = {
  LATENCY: Gauge,
  FAILURE_RATE: XCircle,
  COST: PiggyBank,
};

const ALERT_LABELS = {
  LATENCY: "Alerte Latence",
  FAILURE_RATE: "Alerte Taux d'échec",
  COST: "Alerte Coût",
};

/** The 3 documented alerts, with the thresholds this backend enforces. */
export function AlertsCatalog({ thresholds }) {
  const catalog = [
    {
      key: "LATENCY",
      icon: Gauge,
      rule: `any single agent step > ${thresholds?.latency_ms ?? 30000} ms`,
      value: thresholds?.latency_ms ? `${thresholds.latency_ms} ms` : null,
    },
    {
      key: "FAILURE_RATE",
      icon: XCircle,
      rule: `more than ${thresholds?.failure_rate_pct ?? 20}% of steps fail`,
      value: thresholds?.failure_rate_pct ? `> ${thresholds.failure_rate_pct}%` : null,
    },
    {
      key: "COST",
      icon: PiggyBank,
      rule: `estimated LLM cost > $${thresholds?.cost_limit_usd ?? 10}`,
      value: thresholds?.cost_limit_usd != null ? `$${thresholds.cost_limit_usd}` : null,
    },
  ];
  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
      {catalog.map((a) => {
        const Icon = a.icon;
        return (
          <div
            key={a.key}
            className="flex items-center gap-3 rounded-lg border bg-card p-4"
          >
            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-amber-500/10 text-amber-600">
              <Icon className="h-4 w-4" />
            </div>
            <div className="min-w-0">
              <p className="truncate text-sm font-semibold">{ALERT_LABELS[a.key]}</p>
              <p className="truncate text-xs text-muted-foreground">{a.rule}</p>
            </div>
          </div>
        );
      })}
    </div>
  );
}

/** The alerts recorded on the latest (or selected) run. */
export function RunAlerts({ alerts }) {
  if (!alerts?.length) return null;
  return (
    <Card className="border-red-200">
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 text-red-700">
          <AlertTriangle className="h-4 w-4" /> Alertes déclenchées
        </CardTitle>
        <CardDescription>
          This run breached {alerts.length} threshold{alerts.length > 1 ? "s" : ""}.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-2">
        {alerts.map((a, i) => {
          const Icon = ALERT_ICONS[a.type] ?? AlertTriangle;
          return (
            <div key={i} className="flex items-start gap-3 rounded-lg bg-muted/50 p-3">
              <Icon className="mt-0.5 h-4 w-4 shrink-0 text-red-600" />
              <div>
                <Badge variant="destructive" className="mb-1">{ALERT_LABELS[a.type] ?? a.type}</Badge>
                <p className="text-sm">{a.message}</p>
              </div>
            </div>
          );
        })}
      </CardContent>
    </Card>
  );
}
