import * as React from "react";
import {
  Database,
  GitBranch,
  Bot,
  GitMerge,
  Search,
  Calculator,
  Scale,
  FileText,
  MoveLeft,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import { fmtMs } from "@/lib/api";

/**
 * The LangGraph pipeline, drawn as the graph it really is.
 *
 * Every node has: the agent role behind it, and - once a trace is loaded -
 * the model that ran it, how long it took and the parallel SKU fan-out.
 * The cost-gate retry edge is drawn as a dashed return arrow to the
 * optimizer, live when the gate actually looped.
 */

const NODES = [
  {
    id: "db_scan",
    label: "DB Scan",
    icon: Database,
    kind: "deterministic",
    hint: "read the stock position",
    match: () => false, // instantaneous in Python, not traced as a step
  },
  {
    id: "reorder_dispatch",
    label: "Dispatch",
    icon: GitBranch,
    kind: "deterministic",
    hint: "one task per short SKU",
    match: () => false,
  },
  {
    id: "reorder_analyst",
    label: "Reorder Analysts",
    icon: Bot,
    kind: "fanout",
    hint: "parallel · one agent per SKU",
    match: (step) => step.stage.startsWith("reorder/"),
  },
  {
    id: "reorder_merge",
    label: "Merge",
    icon: GitMerge,
    kind: "deterministic",
    hint: "collect · rank · floor",
    match: () => false,
  },
  {
    id: "supplier_scout",
    label: "Supplier Scout",
    icon: Search,
    kind: "llm",
    hint: "rank the DB offers",
    match: (step) => step.stage === "supplier_scout",
  },
  {
    id: "cost_optimizer",
    label: "Cost Optimizer",
    icon: Calculator,
    kind: "llm",
    hint: "cheapest landed-cost plan",
    match: (step) => step.stage.startsWith("cost_optimizer"),
  },
  {
    id: "cost_gate",
    label: "Cost Gate",
    icon: Scale,
    kind: "deterministic",
    hint: "budget check",
    match: () => false,
  },
  {
    id: "report",
    label: "Report",
    icon: FileText,
    kind: "deterministic",
    hint: "final reorder plan",
    match: () => false,
  },
];

function resolveBranches(steps) {
  const analysts = [];
  let optimizer = null;
  let scout = null;
  let gateStatus = "ok";
  for (const step of steps) {
    if (step.stage.startsWith("reorder/")) analysts.push(step);
    else if (step.stage.startsWith("cost_optimizer")) optimizer = step;
    else if (step.stage === "supplier_scout") scout = step;
  }
  const retries = steps.filter(
    (s) => s.stage.startsWith("cost_optimizer/attempt")
  ).length;
  return { analysts, optimizer, scout, gateStatus, retries: Math.max(retries - 1, 0) };
}

function NodeShell({ node, state, children }) {
  const Icon = node.icon;
  return (
    <div className="flex w-28 shrink-0 flex-col items-center">
      <div
        className={cn(
          "relative flex h-14 w-14 items-center justify-center rounded-xl border-2 bg-background transition-all",
          state === "done" && "border-emerald-500/70 bg-emerald-500/5 shadow-[0_0_16px_rgba(16,185,129,0.15)]",
          state === "failed" && "border-red-500/70 bg-red-500/5 shadow-[0_0_16px_rgba(239,68,68,0.2)]",
          state === "pending" && "border-border opacity-50",
          state === "idle" && "border-dashed border-input opacity-60"
        )}
      >
        <Icon
          className={cn(
            "h-5 w-5",
            state === "done" && "text-emerald-600",
            state === "failed" && "text-red-600",
            state === "idle" && "text-muted-foreground"
          )}
        />
      </div>
      <p className="mt-1.5 text-center text-[11px] font-medium leading-tight">{node.label}</p>
      <p className="text-center text-[10px] leading-tight text-muted-foreground">{children}</p>
    </div>
  );
}

function Connector({ active }) {
  return (
    <div className="relative flex h-14 w-8 shrink-0 items-center">
      <div className={cn("h-px flex-1", active ? "bg-foreground/30" : "bg-border")} />
      <div
        className={cn(
          "absolute right-0 h-2 w-2 -translate-y-[1px] rotate-45 border-r border-t",
          active ? "border-foreground/30" : "border-border"
        )}
      />
    </div>
  );
}

function AnalystChip({ step }) {
  const sku = step.stage.replace("reorder/", "");
  const pct = Math.min(100, Math.round((step.duration_ms / 60000) * 100));
  return (
    <div
      className={cn(
        "w-full rounded-md border bg-card px-2 py-1 text-[10px]",
        step.status === "failed" ? "border-red-300 text-red-700" : "border-emerald-300/60"
      )}
    >
      <div className="flex items-center justify-between gap-1 font-medium">
        <span>{sku}</span>
        <span className="text-muted-foreground">{fmtMs(step.duration_ms)}</span>
      </div>
      <div className="mt-1 h-0.5 w-full overflow-hidden rounded bg-muted">
        <div className={cn("h-full", step.status === "failed" ? "bg-red-500" : "bg-emerald-500")} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

export function PipelineGraph({ trace }) {
  const steps = trace?.steps_detail ?? [];
  const branches = resolveBranches(steps);

  const analystState =
    branches.analysts.length === 0
      ? "idle"
      : branches.analysts.some((s) => s.status === "failed")
        ? "failed"
        : "done";
  const scoutState = branches.scout
    ? branches.scout.status === "failed"
      ? "failed"
      : "done"
    : "idle";
  const optimizerState = branches.optimizer
    ? branches.optimizer.status === "failed"
      ? "failed"
      : "done"
    : "idle";

  const stateOf = (id) =>
    ({ reorder_analyst: analystState, supplier_scout: scoutState, cost_optimizer: optimizerState }[id] ??
      (steps.length > 0 ? "done" : "idle"));

  const analystModel = branches.analysts[0]?.model;
  const optimizerModel = branches.optimizer?.model;
  const scoutModel = branches.scout?.model;

  return (
    <div className="space-y-4">
      <div className="flex items-stretch justify-between gap-1 overflow-x-auto pb-2">
        {NODES.map((node, index) => {
          const isLast = index === NODES.length - 1;
          return (
            <React.Fragment key={node.id}>
              {node.id === "reorder_analyst" ? (
                // Fan-out: N parallel analyst cards stacked vertically
                <div className="flex min-w-40 shrink-0 flex-col items-center">
                  <div
                    className={cn(
                      "flex h-14 w-14 items-center justify-center rounded-xl border-2",
                      analystState === "done" && "border-emerald-500/70 bg-emerald-500/5",
                      analystState === "failed" && "border-red-500/70 bg-red-500/5",
                      analystState === "idle" && "border-dashed border-input opacity-60"
                    )}
                  >
                    <Bot className={cn(
                      "h-5 w-5",
                      analystState === "done" && "text-emerald-600",
                      analystState === "failed" && "text-red-600"
                    )} />
                  </div>
                  <p className="mt-1.5 text-center text-[11px] font-medium leading-tight">Reorder Analysts</p>
                  <div className="mt-1 flex w-full flex-col gap-1 px-1">
                    {branches.analysts.length > 0 ? (
                      branches.analysts.map((s) => <AnalystChip key={s.stage} step={s} />)
                    ) : (
                      <p className="text-center text-[10px] text-muted-foreground">no short SKUs</p>
                    )}
                  </div>
                  {analystModel && (
                    <Badge variant="secondary" className="mt-1.5 truncate text-[9px]">{analystModel}</Badge>
                  )}
                </div>
              ) : (
                <div className="flex flex-col items-center">
                  <div className="flex items-center">
                    <NodeShell node={node} state={stateOf(node.id)}>
                      {node.id === "supplier_scout" && scoutModel
                        ? `${fmtMs(branches.scout.duration_ms)}`
                        : node.id === "cost_optimizer" && optimizerModel
                          ? `${fmtMs(branches.optimizer.duration_ms)}${branches.retries > 0 ? ` · ${branches.retries} retry` : ""}`
                          : node.kind === "deterministic"
                            ? "deterministic"
                            : node.hint}
                    </NodeShell>
                    {!isLast && (
                      <Connector active={stateOf(node.id) !== "idle" && stateOf(NODES[index + 1].id) !== "idle"} />
                    )}
                  </div>
                  {(node.id === "supplier_scout" && scoutModel) ||
                  (node.id === "cost_optimizer" && optimizerModel) ? (
                    <Badge variant="secondary" className="mt-1.5 max-w-28 truncate text-[9px]">
                      {node.id === "supplier_scout" ? scoutModel : optimizerModel}
                    </Badge>
                  ) : null}
                </div>
              )}
            </React.Fragment>
          );
        })}
      </div>

      {/* The LangGraph cost-gate retry loop */}
      <div className="flex items-center gap-2 rounded-lg border border-dashed border-border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
        <MoveLeft className="h-3.5 w-3.5" />
        <span>
          <span className="font-medium text-foreground">conditional edge:</span> when the plan is over
          budget, the graph loops cost_gate → cost_optimizer
          {trace
            ? branches.retries > 0
              ? `, it ran ${branches.retries} retry(ies) in this execution`
              : ", no retry needed this run"
            : ""}
        </span>
      </div>
    </div>
  );
}
