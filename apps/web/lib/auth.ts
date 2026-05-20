import { redirect } from "next/navigation";
import { createSupabaseServerClient } from "./supabase/server";

export async function getCurrentUser() {
  const supabase = createSupabaseServerClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();
  return user;
}

export async function requireTenant() {
  const user = await getCurrentUser();
  if (!user) redirect("/login");
  const supabase = createSupabaseServerClient();
  const { data } = await supabase
    .from("users")
    .select("tenant_id, role")
    .eq("id", user.id)
    .single();
  if (!data?.tenant_id) redirect("/login");
  return { user, tenantId: data.tenant_id, role: data.role as "admin" | "rep" };
}
