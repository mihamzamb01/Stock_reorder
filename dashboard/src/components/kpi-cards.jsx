import {
  Activity,
  AlertTriangle,
  Timer,
  Wallet,
  Coins,
  FileJson,
} from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { fmtMs, fmtInt, fmtUsd } from "@/lib/api";

function Kpi({ icon: Icon, label, value, sub, tone }) {
  return (
    <Card>
      <CardContent className="flex items-center gap-4 p-5">
        <div
          className={`flex h-11 w-11 shrink-0 items-center justify-center rounded-lg ${
            tone === "danger"
              ? "bg-red-100 text-red-600"
              : tone === "warn"
                ? "bg-amber-100 text-amber-600"
                : "bg-muted text-foreground"
          }`}
        >
          <Icon className="h-5 w-5" />
        </div>
        <div className="min-w-0">
          <p className="text-xs font-medium text-muted-foreground">{label}</p>
          <p className="truncate text-xl font-semibold tracking-tight">{value}</p>
          {sub && <p className="truncate text-xs text-muted-foreground">{sub}</p>}
        </div>
      </CardContent>
    </Card>
  );
}

export function KpiCards({ summary }) {
  if (!summary) return null;
  const rate = summary.success_rate_pct;
  return (
    <div className="grid grid-cols-2 gap-4 lg:grid-cols-5">
      <Kpi
        icon={Activity}
        label="Success rate"
        value={`${rate}%`}
        sub={`${summary.successful_runs}/${summary.runs} runs`}
        tone={rate >= 90 ? "ok" : rate >= 70 ? "warn" : "danger"}
      />
      <Kpi
        icon={Timer}
        label="Avg latency"
        value={fmtMs(summary.avg_latency_ms)}
        sub="per run, all steps"
      />
      <Kpi
        icon={Wallet}
        label="Total cost (est.)"
        value={fmtUsd(summary.total_cost_usd)}
        sub="LLM tokens, roll-up"
      />
      <Kpi
        icon={Coins}
        label="Total tokens"
        value={fmtInt(summary.total_tokens)}
        sub="prompt + completion"
      />
      <Kpi
        icon={AlertTriangle}
        label="Alerting runs"
        value={fmtInt(summary.alerting_runs)}
        sub={summary.alerting_runs > 0 ? "needs attention" : "all quiet"}
        tone={summary.alerting_runs > 0 ? "danger" : "ok"}
      />
    </div>
  );
}

export function EmptyState() {
  return (
    <Card>
      <CardContent className="flex flex-col items-center gap-3 p-10 text-center">
        <FileJson className="h-8 w-8 text-muted-foreground" />
        <div>
          <p className="font-medium">No runs recorded yet</p>
          <p className="mt-1 text-sm text-muted-foreground">
            Run the pipeline first:{" "}
            <code className="rounded bg-muted px-1.5 py-0.5 text-xs">python main.py --test</code>
          </p>
        </div>
      </CardContent>
    </Card>
  );
}
