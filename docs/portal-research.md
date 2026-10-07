# Verified portal integration evidence

Research date: **2026-10-08**, Asia/Qyzylorda. These observations describe an
external service that can change; adapters fail explicitly on schema changes.

## Public portal

[zakup.gov.kz](https://zakup.gov.kz/) serves a JavaScript application.
`goszakup.gov.kz` currently redirects to it. Public search is `/home/lots`,
`/home/announcements` and `/home/plan-items`. The frontend creates lot links as
`/announcement/{announcement_id}#lot-{id}` using aggregated public IDs.

The official public frontend bundle observed during research was
`/assets/theme-WM3Ny_yL.js`, imported by the application. It declares these
anonymous GET interfaces, which were verified with one real announcement:

| Endpoint | Observed response |
| --- | --- |
| `/api/core/api/public/lots/` | Object with `count`, `next`, `previous`, `results` |
| `/api/core/api/public/announcements/{id}/` | Announcement object; dates are epoch seconds |
| `/api/core/api/public/announcements/{id}/lots/` | Array of lots with complete embedded plan items |
| `/api/core/api/public/announcements/{id}/general-files/` | Array of document groups with nullable `files` arrays |
| `/api/core/api/dicts/systems/` | Array of current system IDs and names |
| `/api/core/api/dicts/lot-statuses/` | Current statuses and external source codes |

The frontend also advertises `/api/core/api/schema/`, which returned **404**.
This project does not claim a discovered OpenAPI schema where none was returned.

Search parameters observed in frontend request construction include `q`, `limit`,
`offset`, `ordering`, `system_id`, `lot_number`, `announcement_number`,
`total_price__gte`, `total_price__lte`, `offer_start_date__gte/__lte`,
`offer_end_date__gte/__lte`, `delivery_address_id__in`, `enstru_id`,
`customer_id`, `organization_id`, `purchase_method_id` and `status_id`.
Multiple IDs generally use `__in` and a `__` separator. User-facing names
must be resolved through official dictionaries rather than guessed IDs.
Submission dates must not be substituted for a publication-date filter.

**Pagination ceiling is real:** read-only `limit=1&offset=10000&system_id=1`
and `limit=1&offset=10001&system_id=1` both returned HTTP **400** with
`Incorrect parameter values`. The frontend also clamps the result window to
10,000. The public adapter reports incomplete traversal at that boundary.
Authenticated OWS keyset pagination is the bulk route for 100,000+ lots;
100,000-record performance has not been demonstrated by these observations.

## Robots and permitted fallback

[Current zakup robots](https://zakup.gov.kz/robots.txt) allows public pages but
disallows `/api/`, `/cabinet/`, `/sso/`, login and callback paths. Consequently,
the default robots-respecting transport refuses direct anonymous API crawling.
Discovery is not authorization to override robots rules.

[Legacy official robots](https://old.goszakup.gov.kz/robots.txt) allows the
public search/detail paths, disallows `/upload/`, and requests a **5-second
crawl delay**. [Legacy lot search](https://old.goszakup.gov.kz/ru/search/lots)
still serves current official records and server-rendered pagination such as
`?count_record=50&page=2`. It is the compliant browser fallback.

Verified legacy form parameters include `filter[name]`, `filter[number]`,
`filter[number_anno]`, `filter[enstru]`, `filter[customer]`,
`filter[method][]`, `filter[status][]`, `filter[kato]`,
`filter[amount_from]`, `filter[amount_to]`, `filter[start_date_from]`,
`filter[start_date_to]`, `filter[end_date_from]`, `filter[end_date_to]`.

Legacy announcement links are `/ru/announce/index/{external_id}`. Search
rows link lots to `/ru/subpriceoffer/index/{announcement_external_id}/{lot_external_id}`.
Announcement tabs include `?tab=lots`, `?tab=documents`, and `?tab=protocols`.
The lot modal uses an AJAX request and a plan-root ID, which differs from the
external lot ID. IDs across public, legacy, and OWS interfaces are not interchangeable.

Official downloads observed use
`https://v3bl.goszakup.gov.kz/files/download_file/{id}/{id}/`.
That host's robots endpoint returned 404 during research.

## OWS bulk API

[Official v3 developer documentation](https://old.goszakup.gov.kz/ru/developer/ows_v3)
requires an initial authorization token from the Electronic Finance Center.
An organization administrator can renew access through the portal. This access
cannot be acquired automatically by an anonymous crawler.

[Official GraphQL query schema](https://ows.goszakup.gov.kz/help/v3/schema/query.doc.html)
documents `Lots(filter: LotsFiltersInput, limit: Int, after: Int)`, with at most
200 items per page and `after` equal to the last ID. The endpoint is
`https://ows.goszakup.gov.kz/v3/graphql`; use Bearer authorization. Pagination
metadata is returned under `extensions.pageInfo` (`hasNextPage`, `lastId`).
[OWS robots](https://ows.goszakup.gov.kz/robots.txt) allows crawling.

The implemented query is based on the official
[lot schema](https://ows.goszakup.gov.kz/help/v3/schema/lots.doc.html),
[plan schema](https://ows.goszakup.gov.kz/help/v3/schema/plnpoint.doc.html),
[delivery schema](https://ows.goszakup.gov.kz/help/v3/schema/plnpointskato.doc.html),
[announcement schema](https://ows.goszakup.gov.kz/help/v3/schema/trdbuy.doc.html),
and [document schema](https://ows.goszakup.gov.kz/help/v3/schema/filelots.doc.html).
No authorized token was available during development, so live authenticated
GraphQL integration remains unverified. Offline tests verify error handling,
request construction and pagination contracts.

## Correctness constraints established with real data

- `status.is_active` describes whether a dictionary entry is enabled. Cancelled
  and completed statuses also have `is_active=true`; it is not proof of open bidding.
- A published lot may have a future submission start. Both dates and the explicit
  status must pass validation.
- List titles/descriptions can be null. Detail plan items supply official titles
  and additional descriptions. ENSTRU text never replaces a technical specification.
- Kazakhstan's ENSTRU classification is not CPV. CPV filters are rejected where
  the source cannot establish a CPV code; no mapping is invented.
- Search plan IDs can be stale. The inspected search plan ID returned 404 while
  the announcement's embedded full plan had a different valid ID.
- Delivery evidence belongs to `plan_items[].deliveries[]`, especially
  `kato_full_name_ru` / `kato.full_name_ru`, or OWS
  `Plans[].PlansKato[].fullDeliveryPlaceNameRu`. Customer addresses do not prove delivery.
- Documents are groups containing `files[].file.file_url` and `filename`.
  A file's `lot.id` scopes it to one lot; null means a common announcement document.
  Another lot's specification must not be assigned to the current lot.
- Missing currency, CPV, warranty, certificates or specification text stays missing
  until an official page or parsed document supplies it.

Reduced genuine responses used in offline tests are documented in
`tests/fixtures/official/README.md`. They are historical test evidence only.

## Live browser verification

A robots-respecting Playwright smoke run on the research date searched for one
real captured lot number, opened its detail and announcement pages, expanded
the lot's modal, and read each document group. It retrieved a matching lot with
an explicit delivery address, publication/submission dates, and nine document
links, including the lot-specific technical specification. A separate browser
discovery run observed the current anonymous JSON lots endpoint. These checks
verify one end-to-end source path, not the completeness of all procurement
methods or a bulk performance target.

The same lot number was observed under announcement numbers `17712609-1` and
`17712609-2` across the legacy/public observations. De-duplication must include
the announcement number; lot number alone can collapse announcement versions.
