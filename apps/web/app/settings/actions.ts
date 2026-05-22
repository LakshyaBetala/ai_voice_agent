"use server";
import { z } from "zod";
import { revalidatePath } from "next/cache";
import { createSupabaseServerClient } from "@/lib/supabase/server";

const Schema = z.object({
  persona_name: z.string().min(1).max(60),
  persona_lang_default: z.enum(["en-IN", "hi-IN", "ta-IN"]),
  exotel_caller_id: z.string().optional().nullable(),
  whatsapp_handoff_number: z.string().optional().nullable(),
  agent_enabled: z.boolean(),
  telephony_mode: z.enum(["managed", "byon"]),
  byon_provider: z.enum(["exotel", "plivo", "tata"]).optional().nullable(),
  byon_from_number: z.string().optional().nullable(),
});

export async function updateTenantSettingsAction(fd: FormData) {
  const parsed = Schema.safeParse({
    persona_name: fd.get("persona_name"),
    persona_lang_default: fd.get("persona_lang_default"),
    exotel_caller_id: fd.get("exotel_caller_id") || null,
    whatsapp_handoff_number: fd.get("whatsapp_handoff_number") || null,
    agent_enabled: fd.get("agent_enabled") === "on",
    telephony_mode: fd.get("telephony_mode") || "managed",
    byon_provider: fd.get("byon_provider") || null,
    byon_from_number: fd.get("byon_from_number") || null,
  });
  if (!parsed.success) return { error: parsed.error.issues[0]!.message };
  if (
    parsed.data.telephony_mode === "byon" &&
    (!parsed.data.byon_provider || !parsed.data.byon_from_number)
  ) {
    return { error: "BYON requires provider and from-number" };
  }

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
