import type { SupabaseClient } from "@supabase/supabase-js";
import type { VoiceProvider } from "@ai-voice/shared";

export async function dispatchSingleCall(
  sb: SupabaseClient,
  provider: VoiceProvider,
  args: { leadId: string; campaignId?: string },
): Promise<{ providerCallId: string }> {
  const { data: lead } = await sb.from("leads").select("*").eq("id", args.leadId).single();
  if (!lead) throw new Error("lead not found");
  if (lead.status === "do_not_call") throw new Error("lead is DNC");

  const { data: tenant } = await sb
    .from("tenants")
    .select("samvaad_agent_id,exotel_caller_id,persona_lang_default")
    .eq("id", lead.tenant_id)
    .single();
  if (!tenant?.samvaad_agent_id || !tenant.exotel_caller_id) {
    throw new Error("tenant not provisioned for voice");
  }

  const { data: dnc } = await sb
    .from("dnc_list")
    .select("phone_e164")
    .eq("tenant_id", lead.tenant_id)
    .eq("phone_e164", lead.phone_e164)
    .single();
  if (dnc) throw new Error("phone on DNC list");

  await sb.from("leads").update({ status: "queued" }).eq("id", lead.id);

  const { providerCallId } = await provider.startCall({
    agentId: tenant.samvaad_agent_id,
    to_e164: lead.phone_e164,
    callerId: tenant.exotel_caller_id,
    langHint: tenant.persona_lang_default as any,
    metadata: {
      lead_id: lead.id,
      tenant_id: lead.tenant_id,
      campaign_id: args.campaignId,
    },
  });

  await sb.from("calls").insert({
    tenant_id: lead.tenant_id,
    lead_id: lead.id,
    campaign_id: args.campaignId ?? null,
    samvaad_call_id: providerCallId,
    status: "queued",
    kind: "ai_outbound",
  });
  await sb.from("leads").update({ status: "calling" }).eq("id", lead.id);
  return { providerCallId };
}
