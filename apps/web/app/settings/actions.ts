"use server";
import { z } from "zod";
import { revalidatePath } from "next/cache";
import { createSupabaseServerClient } from "@/lib/supabase/server";

const Schema = z.object({
  persona_name: z.string().min(1).max(60),
  persona_lang_default: z.enum(["en-IN", "hi-IN", "ta-IN"]),
  exotel_caller_id: z.string().optional().nullable(),
  whatsapp_handoff_number: z.string().optional().nullable(),
});

export async function updateTenantSettingsAction(fd: FormData) {
  const parsed = Schema.safeParse({
    persona_name: fd.get("persona_name"),
    persona_lang_default: fd.get("persona_lang_default"),
    exotel_caller_id: fd.get("exotel_caller_id") || null,
    whatsapp_handoff_number: fd.get("whatsapp_handoff_number") || null,
  });
  if (!parsed.success) return { error: parsed.error.issues[0]!.message };

  const supabase = createSupabaseServerClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();
  if (!user) return { error: "unauthorized" };
  const { data: profile } = await supabase
    .from("users")
    .select("tenant_id")
    .eq("id", user.id)
    .single();
  if (!profile?.tenant_id) return { error: "no tenant" };

  const { error } = await supabase
    .from("tenants")
    .update(parsed.data)
    .eq("id", profile.tenant_id);
  if (error) return { error: error.message };
  revalidatePath("/settings");
  return { ok: true };
}
