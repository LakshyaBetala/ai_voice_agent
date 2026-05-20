import { requireTenant } from "@/lib/auth";
import { createSupabaseServerClient } from "@/lib/supabase/server";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Button } from "@/components/ui/button";
import { updateTenantSettingsAction } from "./actions";
import { NavBar } from "@/components/NavBar";

export default async function SettingsPage() {
  const { tenantId } = await requireTenant();
  const supabase = createSupabaseServerClient();
  const { data: t } = await supabase
    .from("tenants")
    .select("*")
    .eq("id", tenantId)
    .single();
  if (!t) return null;

  return (
    <>
      <NavBar tenantName={t.name} />
      <main className="mx-auto max-w-2xl space-y-6 p-6">
        <h1 className="text-2xl font-semibold">Settings — {t.name}</h1>
        <form
          action={async (fd: FormData) => {
            "use server";
            await updateTenantSettingsAction(fd);
          }}
          className="space-y-4"
        >
          <div>
            <Label>Persona name</Label>
            <Input name="persona_name" defaultValue={t.persona_name} />
          </div>
          <div>
            <Label>Default language</Label>
            <select
              name="persona_lang_default"
              defaultValue={t.persona_lang_default}
              className="block h-10 w-full rounded-md border border-border bg-background px-3 text-sm"
            >
              <option value="en-IN">English (India)</option>
              <option value="hi-IN">Hindi</option>
              <option value="ta-IN">Tamil</option>
            </select>
          </div>
          <div>
            <Label>Exotel caller ID</Label>
            <Input
              name="exotel_caller_id"
              defaultValue={t.exotel_caller_id ?? ""}
              placeholder="+91..."
            />
          </div>
          <div>
            <Label>WhatsApp handoff number</Label>
            <Input
              name="whatsapp_handoff_number"
              defaultValue={t.whatsapp_handoff_number ?? ""}
              placeholder="+91..."
            />
          </div>
          <Button type="submit">Save</Button>
        </form>
      </main>
    </>
  );
}
