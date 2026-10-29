import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { fmtInt, fmtMs } from "@/lib/api";

/**
 * Token usage per agent role - the "coût par appel LLM" column of the
 * observability course, fed straight from the run's usage_by_role metrics.
 */
export function TokenUsage({ usage }) {
  if (!usage?.length) return null;
  const totalIn = usage.reduce((acc, u) => acc + u.input_tokens, 0);
  const totalOut = usage.reduce((acc, u) => acc + u.output_tokens, 0);
  const max = Math.max(1, usage.reduce((acc, u) => acc + u.input_tokens + u.output_tokens, 0));
  return (
    <Card>
      <CardHeader className="pb-4">
        <CardTitle>Token usage by role</CardTitle>
        <CardDescription>
          {fmtInt(totalIn)} prompt + {fmtInt(totalOut)} completion tokens across{" "}
          {usage.reduce((acc, u) => acc + u.calls, 0)} LLM call(s)
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {usage.map((u) => {
          const share = ((u.input_tokens + u.output_tokens) / max) * 100;
          return (
            <div key={u.role} className="space-y-1">
              <div className="flex items-center justify-between gap-2 text-sm">
                <span className="font-medium">{u.role}</span>
                <span className="text-xs text-muted-foreground">
                  {u.calls} call{u.calls > 1 ? "s" : ""} · {fmtInt(u.input_tokens)} in /{" "}
                  {fmtInt(u.output_tokens)} out
                </span>
              </div>
              <div className="h-2 w-full overflow-hidden rounded-full bg-muted">
                <div className="h-full rounded-full bg-primary" style={{ width: `${share}%` }} />
              </div>
            </div>
          );
        })}
      </CardContent>
    </Card>
  );
}

/** The trace metadata row shown under the pipeline graph. */
export function TraceMeta({ trace }) {
  if (!trace) return null;
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Badge variant="outline" className="font-mono">{trace.correlation_id}</Badge>
      <Badge variant="secondary">{trace.warehouse}</Badge>
      <Badge variant={trace.status === "ok" ? "success" : "destructive"}>{trace.status}</Badge>
      <Badge variant="info">{trace.steps} steps</Badge>
      <Badge variant="secondary">{fmtMs(trace.duration_ms)}</Badge>
    </div>
  );
}
