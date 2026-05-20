import { requireTenant } from "@/lib/auth";
import { createSupabaseServerClient } from "@/lib/supabase/server";
import { LeadsTable } from "@/components/LeadsTable";
import { CsvUploadDialog } from "@/components/CsvUpload";
import { ManualLeadDialog } from "@/components/ManualLeadDialog";
import { LeadsRealtimeRefresher } from "@/components/LeadsRealtimeRefresher";
import { NavBar } from "@/components/NavBar";

export default async function LeadsPage() {
  const { tenantId } = await requireTenant();
  const supabase = createSupabaseServerClient();
  const { data: tenant } = await supabase
    .from("tenants")
    .select("name")
    .eq("id", tenantId)
    .single();
  const { data: leads, error } = await supabase
    .from("leads")
    .select(
      "id,name,phone_e164,company,industry,status,created_at,lead_scores(score_0_100,classification,scored_at)",
    )
    .order("created_at", { ascending: false })
    .limit(500);

  return (
    <>
      <NavBar tenantName={tenant?.name ?? "—"} />
      <main className="mx-auto max-w-6xl space-y-6 p-6">
        <LeadsRealtimeRefresher />
        <header className="flex items-center justify-between">
          <h1 className="text-2xl font-semibold">Leads</h1>
          <div className="flex gap-2">
            <ManualLeadDialog />
            <CsvUploadDialog />
          </div>
        </header>
        {error ? (
          <p className="text-sm text-destructive">Error: {error.message}</p>
        ) : (
          <LeadsTable leads={(leads ?? []) as any} />
        )}
      </main>
    </>
  );
}
