import { z } from "zod";

/** Legacy source/library partitions; they are not production regions or work languages. */
export const mediaCategorySchema = z.enum(["Japan", "China", "Korea", "Europe", "Other"]);
export type MediaCategory = z.infer<typeof mediaCategorySchema>;
export const catalogCategorySchema = z.enum(["all", ...mediaCategorySchema.options]);
export type CatalogCategory = z.infer<typeof catalogCategorySchema>;
export const catalogSortSchema = z.enum(["added", "newest", "oldest", "title", "code", "ranked", "random"]);
export type CatalogSort = z.infer<typeof catalogSortSchema>;
/** Input compatibility presets; wanted preference and media availability are separate axes. */
export const catalogStatusSchema = z.enum(["all", "emby", "files", "metadata", "want", "downloaded", "undownloaded"]);
export type CatalogStatus = z.infer<typeof catalogStatusSchema>;
/** The retired local grid has no Emby binding data; keep its supported preset subset explicit. */
export const legacyWorkStatusSchema = catalogStatusSchema.extract(["all", "undownloaded", "want", "downloaded"]);
export type LegacyWorkStatus = z.infer<typeof legacyWorkStatusSchema>;
export const mediaAvailabilitySchema = z.enum(["all", "files", "emby", "metadata", "undownloaded"]);
export type MediaAvailability = z.infer<typeof mediaAvailabilitySchema>;
export const libraryPresenceSchema = z.enum(["absent", "catalog_only", "in_library"]);
export const organizationKindSchema = z.enum(["organization", "studio", "label", "platform"]);
export type OrganizationKind = z.infer<typeof organizationKindSchema>;
export const organizationRoleSchema = z.enum(["studio", "distributor", "label", "platform", "credited"]);
export type OrganizationRole = z.infer<typeof organizationRoleSchema>;

export function effectiveAvailability(value: { availability?: MediaAvailability; status?: CatalogStatus }): MediaAvailability {
  if (value.availability && value.availability !== "all") return value.availability;
  switch (value.status) {
    case "downloaded": case "files": return "files";
    case "emby": case "metadata": case "undownloaded": return value.status;
    default: return "all";
  }
}
export function effectiveWanted(value: { wanted?: boolean; status?: CatalogStatus }): boolean | undefined {
  return value.wanted ?? (value.status === "want" ? true : undefined);
}
