import { z } from "zod";

export const tagTermSchema = z.object({
  id: z.string(), name: z.string(), aliases: z.array(z.string()).default([]),
  group: z.string().default(""),
  parent_id: z.string().nullable().default(null),
  path: z.array(z.object({ id: z.string(), name: z.string() })).default([]),
  direct_count: z.number().default(0),
  hidden: z.boolean().default(false), revision: z.number(), count: z.number().default(0),
});
export type TagTerm = z.infer<typeof tagTermSchema>;
export const tagDictionarySchema = z.object({ terms: z.array(tagTermSchema), total: z.number(), revision: z.number() });
export const tagTreeSchema = z.object({ nodes: z.array(z.object({
  id: z.string(), name: z.string(), parent_id: z.string().nullable(),
  aliases: z.array(z.string()).default([]), hidden: z.boolean(),
})), revision: z.number() });
