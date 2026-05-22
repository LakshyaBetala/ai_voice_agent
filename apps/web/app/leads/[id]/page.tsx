import { notFound } from "next/navigation";
import { requireTenant } from "@/lib/auth";
import { createSupabaseServerClient } from "@/lib/supabase/server";
import { fetchBillingSummary } from "@/lib/billing";
import { LeadStatusBadge } from "@/components/LeadStatusBadge";
import { ScoreBadge } from "@/components/ScoreBadge";
import { SummaryCard } from "@/components/SummaryCard";
import { ExtractedFields } from "@/components/ExtractedFields";
import { TranscriptView } from "@/components/TranscriptView";
import { DncDialog } from "@/components/DncDialog";
import { StartAiCallButton } from "@/components/StartAiCallButton";
import { CallNowButton } from "@/components/CallNowButton";
import { NavBar } from "@/components/NavBar";
import { QualificationPanel } from "@/components/QualificationPanel";

export default async function LeadDetail({
  params,
}: {
  params: { id: string };
}) {
  const { tenantId } = await requireTenant();
  const supabase = createSupabaseServerClient();
  const [{ data: tenant }, billing] = await Promise.all([
    supabase.from("tenants").select("name").eq("id", tenantId).single(),
    fetchBillingSummary(tenantId),
  ]);
  const { data: lead } = await supabase
    .from("leads")
    .select("*")
    .eq("id", params.id)
    .single();
  if (!lead) notFound();

  const [{ data: latestScore }, { data: latestSlots }] = await Promise.all([
    supabase
      .from("lead_scores")
      .select("*")
      .eq("lead_id", lead.id)
      .order("scored_at", { ascending: false })
      .limit(1)
      .maybeSingle(),
    supabase
      .from("qualification_slots")
      .select("*")
      .eq("lead_id", lead.id)
      .order("updated_at", { ascending: false })
      .limit(1)
      .maybeSingle(),
  ]);

  return (
    <>
      <NavBar
        tenantName={tenant?.name ?? "—"}
        unitsUsed={billing.unitsUsed}
        unitsAllowance={billing.unitsAllowance}
        wigglePct={billing.wigglePct}
      />
      <main className="mx-auto max-w-4xl space-y-6 p-6">
        <header className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h1 className="text-2xl font-semibold">{lead.name}</h1>
            <p className="text-sm text-muted-foreground">
              {lead.phone_e164} · {lead.company ?? "—"} ·{" "}
              {lead.industry ?? "—"}
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <CallNowButton leadId={lead.id} phone={lead.phone_e164} />
            <StartAiCallButton leadId={lead.id} />
            <LeadStatusBadge status={lead.status} />
            <DncDialog leadId={lead.id} phone={lead.phone_e164} />
          </div>
        </header>

        <section className="space-y-4 rounded-md border p-4">
          <div className="flex items-center gap-3">
            <h2 className="text-sm font-medium">AI Summary</h2>
            {latestScore && (
              <ScoreBadge
                classification={latestScore.classification}
                score={latestScore.score_0_100}
              />
            )}
          </div>
          {latestScore ? (
            <>
              <SummaryCard
                summary={latestScore.summary}
                reason={latestScore.reason}
                nextAction={latestScore.next_action}
              />
              <div className="border-t pt-4">
                <ExtractedFields extracted={latestScore.extracted ?? {}} />
              </div>
            </>
          ) : (
            <p className="text-sm text-muted-foreground">
              No call yet. Click <strong>Call with AI</strong> to trigger
              Priya. The summary will appear here within ~30 seconds of
              the call ending.
            </p>
          )}
        </section>

        <section className="rounded-md border p-4">
          <h2 className="mb-4 text-sm font-medium">Live qualification</h2>
          <QualificationPanel slots={(latestSlots as any) ?? null} />
        </section>

        <section className="rounded-md border p-4">
          <h2 className="mb-2 text-sm font-medium">Transcript</h2>
          <TranscriptView leadId={lead.id} />
        </section>
      </main>
    </>
  );
}
