import Link from "next/link";
import { signOutAction } from "@/app/login/actions";

export function NavBar({ tenantName }: { tenantName: string }) {
  return (
    <nav className="border-b bg-muted/30">
      <div className="mx-auto flex max-w-6xl items-center justify-between px-6 py-3">
        <div className="flex items-center gap-6">
          <Link href="/leads" className="font-semibold">AI Voice — {tenantName}</Link>
          <Link href="/leads" className="text-sm text-muted-foreground hover:text-foreground">Leads</Link>
          <Link href="/campaigns" className="text-sm text-muted-foreground hover:text-foreground">Campaigns</Link>
          <Link href="/settings" className="text-sm text-muted-foreground hover:text-foreground">Settings</Link>
        </div>
        <form action={signOutAction}>
          <button type="submit" className="text-sm text-muted-foreground hover:text-foreground">Sign out</button>
        </form>
      </div>
    </nav>
  );
}
