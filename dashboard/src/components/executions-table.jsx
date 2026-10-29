import * as React from "react";
import { CheckCircle2, XCircle, X } from "lucide-react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
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
import { fmtMs, fmtInt, fmtUsd } from "@/lib/api";

function StatusBadge({ status }) {
  return status === "ok" ? (
    <Badge variant="success" className="gap-1">
      <CheckCircle2 className="h-3 w-3" /> ok
    </Badge>
  ) : (
    <Badge variant="destructive" className="gap-1">
      <XCircle className="h-3 w-3" /> failed
    </Badge>
  );
}

/**
 * Executions table - one row per pipeline run, keyed by correlation ID.
 * Clicking a row loads that run's full trace into the pipeline panel.
 */
export function ExecutionsTable({ rows, selectedId, onSelect, onClose, loading }) {
  return (
    <Card>
      <CardHeader className="flex-row items-start justify-between space-y-0">
        <div className="space-y-1.5">
          <CardTitle>Executions</CardTitle>
          <CardDescription>
            Every pipeline run, keyed by its correlation ID. Click a line to inspect the
            full LangGraph trace.
          </CardDescription>
        </div>
        {selectedId && (
          <Button variant="ghost" size="sm" onClick={onClose}>
            <X className="h-3.5 w-3.5" /> clear selection
          </Button>
        )}
      </CardHeader>
      <CardContent>
        {loading ? (
          <div className="flex items-center justify-center gap-2 py-10 text-sm text-muted-foreground">
            <span className="h-4 w-4 animate-spin rounded-full border-2 border-muted-foreground border-t-transparent" />
            loading executions…
          </div>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Correlation ID</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Started</TableHead>
                <TableHead className="text-right">Steps</TableHead>
                <TableHead className="text-right">Failed</TableHead>
                <TableHead className="text-right">Duration</TableHead>
                <TableHead className="text-right">Tokens</TableHead>
                <TableHead className="text-right">Cost (est.)</TableHead>
                <TableHead>Alerts</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.length === 0 && (
                <TableRow>
                  <TableCell colSpan={9} className="py-8 text-center text-muted-foreground">
                    No runs recorded yet, run <code className="rounded bg-muted px-1">python main.py --test</code>
                  </TableCell>
                </TableRow>
              )}
              {rows.map((row) => (
                <TableRow
                  key={row.correlation_id}
                  onClick={() => onSelect(row.correlation_id)}
                  data-selected={row.correlation_id === selectedId}
                  className={
                    "cursor-pointer " +
                    (row.correlation_id === selectedId ? "bg-muted" : "")
                  }
                >
                  <TableCell className="font-mono text-xs font-medium">
                    {row.correlation_id}
                  </TableCell>
                  <TableCell><StatusBadge status={row.status} /></TableCell>
                  <TableCell className="text-xs text-muted-foreground whitespace-nowrap">
                    {String(row.started_at).replace("T", " ").slice(0, 19)} UTC
                  </TableCell>
                  <TableCell className="text-right">{row.steps}</TableCell>
                  <TableCell className="text-right">
                    {row.failed > 0 ? (
                      <span className="font-medium text-red-600">{row.failed}</span>
                    ) : (
                      "0"
                    )}
                  </TableCell>
                  <TableCell className="text-right font-mono text-xs">{fmtMs(row.duration_ms)}</TableCell>
                  <TableCell className="text-right font-mono text-xs">
                    {fmtInt(row.input_tokens)}/<span className="text-muted-foreground">{fmtInt(row.output_tokens)}</span>
                  </TableCell>
                  <TableCell className="text-right font-mono text-xs">{fmtUsd(row.est_cost_usd)}</TableCell>
                  <TableCell>
                    {row.alerts?.length ? (
                      <div className="flex flex-wrap gap-1">
                        {row.alerts.map((a) => (
                          <Badge key={a} variant="warning" className="text-[10px]">
                            {a.split(":")[0]}
                          </Badge>
                        ))}
                      </div>
                    ) : (
                      <span className="text-xs text-muted-foreground">-</span>
                    )}
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
