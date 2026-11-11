import type { Filters } from "./catalogFilters";

type DurationFilter = Pick<Filters, "duration_min" | "duration_max" | "unknown_duration">;
export const DURATION_OPTIONS: { id: string; name: string; filters: DurationFilter }[] = [
  { id: "under30", name: "30 分钟以下", filters: { duration_max: 1800, unknown_duration: false } },
  { id: "30to60", name: "30–60 分钟", filters: { duration_min: 1800, duration_max: 3600, unknown_duration: false } },
  { id: "60to120", name: "60–120 分钟", filters: { duration_min: 3600, duration_max: 7200, unknown_duration: false } },
  { id: "over120", name: "120 分钟及以上", filters: { duration_min: 7200, unknown_duration: false } },
  { id: "unknown", name: "时长待补", filters: { unknown_duration: true } },
];
export const EMPTY_DURATION: DurationFilter = { duration_min: undefined, duration_max: undefined, unknown_duration: false };
export function durationOption(filters: DurationFilter) {
  return DURATION_OPTIONS.find(option => option.filters.duration_min === filters.duration_min
    && option.filters.duration_max === filters.duration_max && option.filters.unknown_duration === filters.unknown_duration);
}
export function durationLabel(filters: DurationFilter): string {
  return durationOption(filters)?.name ?? `时长 ${filters.duration_min === undefined ? "不限" : filters.duration_min / 60}–${filters.duration_max === undefined ? "不限" : filters.duration_max / 60} 分钟`;
}
