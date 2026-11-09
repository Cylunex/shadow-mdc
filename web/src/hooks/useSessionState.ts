import { useEffect, useState } from "react";
import type { z } from "zod";

/** Remember list navigation for this tab; unavailable storage must not block browsing. */
export function useSessionState<T>(key: string, schema: z.ZodType<T>, fallback: T) {
  const [value, setValue] = useState<T>(() => {
    try {
      const parsed = schema.safeParse(JSON.parse(sessionStorage.getItem(key) ?? "null"));
      if (parsed.success) return parsed.data;
    } catch { /* Storage is optional. */ }
    return fallback;
  });
  useEffect(() => {
    try { sessionStorage.setItem(key, JSON.stringify(value)); }
    catch { /* Keep the current state in memory. */ }
  }, [key, value]);
  return [value, setValue] as const;
}
