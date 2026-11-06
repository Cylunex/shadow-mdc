import { expect, it } from "vitest";
import { applyCatalogFiltersToUrl, EMPTY_CATALOG_FILTERS, readCatalogFilters, type Filters } from "./catalogFilters";

it("removes default JSON filters while preserving the page, detail and fragment", () => {
  const url = new URL("https://example.com/mdc/?view=actors&work=fixture&extra=keep#section");
  url.searchParams.set("catalog", JSON.stringify(EMPTY_CATALOG_FILTERS));
  applyCatalogFiltersToUrl(url, readCatalogFilters(url.search));
  expect(url.pathname + url.search + url.hash).toBe("/mdc/?view=actors&work=fixture&extra=keep#section");
});

it("only writes the selected search and year and removes them when cleared", () => {
  const url = new URL("https://example.com/mdc/");
  applyCatalogFiltersToUrl(url, { ...EMPTY_CATALOG_FILTERS, q: "A & B", year_from: 2024, year_to: 2024 });
  expect(url.search).toBe("?q=A+%26+B&year=2024");
  expect(readCatalogFilters(url.search)).toMatchObject({ q: "A & B", year_from: 2024, year_to: 2024 });
  applyCatalogFiltersToUrl(url, EMPTY_CATALOG_FILTERS);
  expect(url.search).toBe("");
});

it("round-trips every active filter without changing AND/OR or repeated values", () => {
  const filters: Filters = {
    q: "标题 / title", actors: ["a", "b"], actors_all: true, actor_name: "演员", studio_name: "片商",
    tags: ["tag one", "tag&two"], tags_all: false, exclude_tags: ["excluded"],
    genres: ["g1", "g2"], forms: ["f"], regions: ["r"], organizations: ["o1", "o2"],
    organization_role: "studio", year_from: 2000, year_to: 2009, unknown_year: false,
    sort: "newest", status: "emby",
  };
  const url = new URL("https://example.com/mdc/?actor=detail-id");
  applyCatalogFiltersToUrl(url, filters);
  expect(readCatalogFilters(url.search)).toEqual(filters);
  expect(url.searchParams.get("actor")).toBe("detail-id");
  expect(url.searchParams.has("catalog")).toBe(false);
});

it("migrates a non-default JSON link without losing its filters", () => {
  const url = new URL("https://example.com/mdc/");
  url.searchParams.set("catalog", JSON.stringify({ tags: ["a", "b"], tags_all: true, unknown_year: true }));
  applyCatalogFiltersToUrl(url, readCatalogFilters(url.search));
  expect(url.search).toBe("?tag=a&tag=b&year=unknown");
  expect(readCatalogFilters(url.search)).toMatchObject({ tags: ["a", "b"], tags_all: true, unknown_year: true });
});

it("keeps legacy tag intersection and actor/studio/year links working", () => {
  expect(readCatalogFilters("?tag=a&tag=b&cast=Name&studio=Studio&year=2020")).toMatchObject({
    tags: ["a", "b"], tags_all: true, actor_name: "Name", studio_name: "Studio", year_from: 2020, year_to: 2020,
  });
});

it("migrates legacy filters into the single paginated catalog", () => {
  const url = new URL("https://example.com/mdc/?cat=Japan&col=collection&page=2&sort=ranked&status=want");
  applyCatalogFiltersToUrl(url, readCatalogFilters(url.search));
  expect(url.searchParams.get("sort")).toBe("ranked");
  expect(url.searchParams.get("status")).toBe("want");
  expect(url.searchParams.get("cat")).toBe("Japan");
  expect(url.searchParams.get("col")).toBe("collection");
  expect(url.searchParams.has("page")).toBe(false);
});

it("keeps JSON filters alongside legacy sorting and nullable year fields", () => {
  const url = new URL("https://example.com/mdc/?sort=ranked");
  url.searchParams.set("catalog", JSON.stringify({ q: "saved", year_from: null, year_to: null }));
  const filters = readCatalogFilters(url.search);
  expect(filters.q).toBe("saved");
  applyCatalogFiltersToUrl(url, filters);
  expect(url.search).toBe("?q=saved&sort=ranked");
});

it.each(["?catalog=bad-json", "?catalog=null", '?catalog={"actors":null}', ""])("safely resets invalid or absent filters: %s", search => {
  expect(readCatalogFilters(search)).toEqual(EMPTY_CATALOG_FILTERS);
});
