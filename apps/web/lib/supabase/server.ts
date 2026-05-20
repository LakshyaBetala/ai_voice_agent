import { createServerClient, type CookieOptions } from "@supabase/ssr";
import { cookies } from "next/headers";

export function createSupabaseServerClient() {
  const cookieStore = cookies();
  return createServerClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!,
    {
      cookies: {
        getAll: () => cookieStore.getAll(),
        setAll: (toSet: { name: string; value: string; options?: CookieOptions }[]) => {
          try {
            toSet.forEach(({ name, value, options }) =>
              cookieStore.set(name, value, options),
            );
          } catch {
            // Server Component context — ignore
          }
        },
      },
    },
  );
}
