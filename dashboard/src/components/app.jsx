import * as React from "react";
import { BrainCircuit, Database, RefreshCw } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { PipelineGraph } from "@/components/pipeline-graph";
import { KpiCards, EmptyState } from "@/components/kpi-cards";
import { AlertsCatalog, RunAlerts } from "@/components/alerts-panel";
import { ExecutionsTable } from "@/components/executions-table";
import { TokenUsage, TraceMeta } from "@/components/token-usage";
import { fetchExecutions, fetchSummary, fetchTrace } from "@/lib/api";

function Header({ summary, onRefresh, refreshing }) {
  return (
    <header className="sticky top-0 z-10 border-b bg-background/80 backdrop-blur">
      <div className="mx-auto flex max-w-7xl items-center justify-between gap-4 px-6 py-3">
        <div className="flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary text-primary-foreground">
            <BrainCircuit className="h-5 w-5" />
          </div>
          <div>
            <h1 className="text-sm font-semibold leading-tight">Stock reorder · supervision</h1>
            <p className="text-xs text-muted-foreground">LangGraph multi-agent pipeline · logs · metrics · traces</p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Badge variant="outline" className="hidden sm:inline-flex">v1</Badge>
          <Button variant="outline" size="sm" onClick={onRefresh} disabled={refreshing}>
            <RefreshCw className={"h-3.5 w-3.5" + (refreshing ? " animate-spin" : "")} />
            refresh
          </Button>
        </div>
      </div>
    </header>
  );
}

export default function App() {
  const [executions, setExecutions] = React.useState([]);
  const [summary, setSummary] = React.useState(null);
  const [trace, setTrace] = React.useState(null);
  const [selectedId, setSelectedId] = React.useState(null);
  const [loading, setLoading] = React.useState(true);
  const [refreshing, setRefreshing] = React.useState(false);

  const load = React.useCallback(async (isRefresh) => {
    isRefresh ? setRefreshing(true) : setLoading(true);
    try {
      const [rows, sum] = await Promise.all([fetchExecutions(), fetchSummary()]);
      setExecutions(rows);
      setSummary(sum);
      // Default to the latest run's trace for the pipeline panel.
      const live = selectedId ?? (rows[0]?.correlation_id || null);
      if (live) {
        setTrace(await fetchTrace(live));
        setSelectedId(live);
      } else {
        setTrace(null);
      }
    } catch {
      // The API server is not up (dev-server without python). Stay quiet.
    } finally {
      isRefresh ? setRefreshing(false) : setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const selectRun = React.useCallback(async (corrId) => {
    setSelectedId(corrId);
    try {
      setTrace(await fetchTrace(corrId));
    } catch {
      setTrace(null);
    }
  }, []);

  const clearSelection = React.useCallback(async () => {
    setSelectedId(null);
    const latest = executions[0]?.correlation_id || null;
    if (latest) {
      setTrace(await fetchTrace(latest));
      setSelectedId(latest);
    }
  }, [executions]);

  React.useEffect(() => {
    load(false);
    const timer = setInterval(() => load(true), 20000); // live supervision
    return () => clearInterval(timer);
  }, [load]);

  const alerts = trace?.alerts ?? [];
  return (
    <div className="min-h-screen">
      <Header summary={summary} onRefresh={() => load(true)} refreshing={refreshing} />
      <main className="mx-auto max-w-7xl space-y-6 px-6 py-6">
        {summary && <KpiCards summary={summary} />}

        <Card>
          <CardHeader className="pb-2">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="space-y-1.5">
                <CardTitle>LangGraph pipeline</CardTitle>
                <CardDescription>
                  The eight nodes of the compiled graph, with the model, duration and
                  parallel fan-out of {selectedId ? "the selected run" : "the latest run"}
                  {selectedId ? (
                    <>&nbsp;<span className="text-muted-foreground">/</span> <span className="font-mono text-xs">{selectedId}</span></>
                  ) : null}
                </CardDescription>
              </div>
              {trace && <TraceMeta trace={trace} />}
            </div>
          </CardHeader>
          <CardContent>
            <PipelineGraph trace={trace} />
          </CardContent>
        </Card>

        <RunAlerts alerts={alerts} />
        {trace && <TokenUsage usage={trace.usage_by_role} />}

        <div className="space-y-3">
          <h2 className="text-sm font-semibold text-muted-foreground">The 3 documented alerts</h2>
          <AlertsCatalog thresholds={summary?.thresholds} />
        </div>

        <ExecutionsTable
          rows={executions}
          selectedId={selectedId}
          onSelect={selectRun}
          onClose={clearSelection}
          loading={loading && executions.length === 0}
        />

        <footer className="flex items-center justify-center gap-2 pb-6 text-xs text-muted-foreground">
          <Database className="h-3.5 w-3.5" />
          data from runs/ · executions.csv · traces &lt;correlation_id&gt;.json ·
          thresholds configurable in .env
        </footer>
      </main>
    </div>
  );
}
