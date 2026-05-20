"use client";
import { useTransition } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { toast } from "sonner";
import { startAiCallAction } from "@/app/leads/actions";

export function StartAiCallButton({ leadId }: { leadId: string }) {
  const [pending, start] = useTransition();
  const router = useRouter();
  return (
    <Button
      disabled={pending}
      onClick={() =>
        start(async () => {
          const r = await startAiCallAction(leadId);
          if (r.error) toast.error(r.error);
          else {
            toast.success("Calling now — watch the transcript pane");
            router.refresh();
          }
        })
      }
    >
      {pending ? "Dialing…" : "Call with AI"}
    </Button>
  );
}
