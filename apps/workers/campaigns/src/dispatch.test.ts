import { describe, it, expect, vi } from "vitest";
import { dispatchSingleCall } from "./dispatch";

function makeSb(opts: { dnc?: boolean; lead?: any; tenant?: any } = {}) {
  const writes: any[] = [];
  const lead = opts.lead ?? {
    id: "L", tenant_id: "T", name: "X", phone_e164: "+91900", status: "new",
  };
  const tenant = opts.tenant ?? {
    samvaad_agent_id: "agt_1",
    exotel_caller_id: "+91444",
    persona_lang_default: "en-IN",
  };
  return {
    writes,
    client: {
      from: (table: string) => {
        if (table === "leads") {
          return {
            select: () => ({ eq: () => ({ single: async () => ({ data: lead }) }) }),
            update: () => ({
              eq: async () => {
                writes.push({ table, op: "update" });
                return { error: null };
              },
            }),
          };
        }
        if (table === "tenants") {
          return {
            select: () => ({ eq: () => ({ single: async () => ({ data: tenant }) }) }),
          };
        }
        if (table === "dnc_list") {
          return {
            select: () => ({
              eq: () => ({
                eq: () => ({
                  single: async () =>
                    opts.dnc
                      ? { data: { tenant_id: "T", phone_e164: "+91900" } }
                      : { data: null },
                }),
              }),
            }),
          };
        }
        if (table === "calls") {
          return {
            insert: async (row: any) => {
              writes.push({ table, op: "insert", row });
              return { data: row, error: null };
            },
          };
        }
        return {};
      },
    } as any,
  };
}

describe("dispatchSingleCall", () => {
  it("refuses when phone is on DNC list", async () => {
    const { client, writes } = makeSb({ dnc: true });
    const provider = { startCall: vi.fn() } as any;
    await expect(dispatchSingleCall(client, provider, { leadId: "L" })).rejects.toThrow(/DNC/);
    expect(provider.startCall).not.toHaveBeenCalled();
    expect(writes.some((w) => w.table === "calls")).toBe(false);
  });

  it("refuses when tenant has no samvaad_agent_id", async () => {
    const { client } = makeSb({
      tenant: { samvaad_agent_id: null, exotel_caller_id: "+91444", persona_lang_default: "en-IN" },
    });
    const provider = { startCall: vi.fn() } as any;
    await expect(dispatchSingleCall(client, provider, { leadId: "L" })).rejects.toThrow(/not provisioned/);
  });

  it("calls provider and writes a calls row on happy path", async () => {
    const { client, writes } = makeSb();
    const provider = { startCall: vi.fn().mockResolvedValue({ providerCallId: "c_xyz" }) } as any;
    const r = await dispatchSingleCall(client, provider, { leadId: "L" });
    expect(r.providerCallId).toBe("c_xyz");
    expect(writes.some((w) => w.table === "calls" && w.op === "insert")).toBe(true);
    expect(provider.startCall).toHaveBeenCalled();
  });

  it("passes the lead's first name in metadata for personalized greeting", async () => {
    const { client } = makeSb({
      lead: { id: "L", tenant_id: "T", name: "Ravi Kumar", phone_e164: "+91900", status: "new" },
    });
    const provider = { startCall: vi.fn().mockResolvedValue({ providerCallId: "c_1" }) } as any;
    await dispatchSingleCall(client, provider, { leadId: "L" });
    const args = provider.startCall.mock.calls[0]![0];
    expect(args.metadata.lead_first_name).toBe("Ravi");
  });

  it("omits placeholder names (Unknown / NA / one-letter) from metadata", async () => {
    const { client } = makeSb({
      lead: { id: "L", tenant_id: "T", name: "Unknown", phone_e164: "+91900", status: "new" },
    });
    const provider = { startCall: vi.fn().mockResolvedValue({ providerCallId: "c_1" }) } as any;
    await dispatchSingleCall(client, provider, { leadId: "L" });
    const args = provider.startCall.mock.calls[0]![0];
    expect(args.metadata.lead_first_name).toBe("");
  });
});
