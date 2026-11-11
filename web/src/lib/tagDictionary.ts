import { z } from "zod";

export const tagTermSchema = z.object({
  id: z.string(), name: z.string(), aliases: z.array(z.string()).default([]),
  group: z.string().default(""),
  hidden: z.boolean().default(false), revision: z.number(), count: z.number().default(0),
});
export type TagTerm = z.infer<typeof tagTermSchema>;
export const tagDictionarySchema = z.object({ terms: z.array(tagTermSchema), total: z.number(), revision: z.number() });
