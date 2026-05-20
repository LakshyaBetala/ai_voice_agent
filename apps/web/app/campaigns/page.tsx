import { requireTenant } from "@/lib/auth";
import { createSupabaseServerClient } from "@/lib/supabase/server";
import { Button } from "@/components/ui/button";
import { startBulkAction } from "./actions";
import { NavBar } from "@/components/NavBar";

const BLENDED_RUPEES_PER_MIN = 7; // see docs/specs §10

export default async function CampaignsPage() {
  const { tenantId } = await requireTenant();
  const supabase = createSupabaseServerClient();
  const { data: tenant } = await supabase
    .from("tenants")
    .select("name")
    .eq("id", tenantId)
    .single();
  const { count: queued } = await supabase
    .from("leads")
    .select("*", { count: "exact", head: true })
    .eq("status", "new");

  const since = new Date(Date.now() - 7 * 86400_000).toISOString();
  const { data: calls7 } = await supabase
    .from("calls")
    .select("duration_sec,kind,status")
    .gte("created_at", since);

  const ai = (calls7 ?? []).filter((c) => c.kind === "ai_outbound");
  const totalMin = ai.reduce((a, c) => a + (c.duration_sec ?? 0) / 60, 0);
  const estCost = Math.round(totalMin * BLENDED_RUPEES_PER_MIN);
  const avgDur = ai.length
    ? Math.round(
        ai.reduce((a, c) => a + (c.duration_sec ?? 0), 0) / ai.length,
      )
    : 0;

  const { data: scores7 } = await supabase
    .from("lead_scores")
    .select("classification")
    .gte("scored_at", since);
  const hot = (scores7 ?? []).filter((s) => s.classification === "hot").length;
  const hotRate =
    scores7 && scores7.length > 0
      ? Math.round((hot / scores7.length) * 100)
      : 0;

  return (
    <>
      <NavBar tenantName={tenant?.name ?? "—"} />
      <main className="mx-auto max-w-3xl space-y-6 p-6">
        <h1 className="text-2xl font-semibold">Campaigns</h1>

        <section className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          <Stat label="New leads ready" value={String(queued ?? 0)} />
          <Stat label="AI calls (7d)" value={String(ai.length)} />
          <Stat label="Avg duration" value={`${avgDur}s`} />
          <Stat label="Est cost (7d)" value={`₹${estCost}`} />
        </section>

        <section className="rounded-md border p-4">
          <p className="text-sm">
            Hot rate (last 7 days): <strong>{hotRate}%</strong>
          </p>
        </section>

        <form
          action={async () => {
            "use server";
            await startBulkAction();
          }}
        >
          <Button type="submit">
            Dial up to 50 new leads (1 call/second)
          </Button>
        </form>
      </main>
    </>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border p-3">
      <p className="text-xs uppercase tracking-wide text-muted-foreground">
        {label}
      </p>
      <p className="text-2xl font-semibold">{value}</p>
    </div>
  );
}
