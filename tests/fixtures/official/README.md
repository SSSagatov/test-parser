# Official response fixtures

Captured with read-only requests on 2026-10-08 from the official portal.
These are real historical observations, not generated procurement data.
They must never be exported as current opportunities or loaded by the crawler.

- `robots.txt`: https://zakup.gov.kz/robots.txt — original rule order retained to test longest-match precedence.

- `systems.json`: https://zakup.gov.kz/api/core/api/dicts/systems/
- `public_lots.json`: https://zakup.gov.kz/api/core/api/public/lots/?limit=1&offset=0&system_id=1
- `announcement.json`: https://zakup.gov.kz/api/core/api/public/announcements/40745436/
- `announcement_lots.json`: https://zakup.gov.kz/api/core/api/public/announcements/40745436/lots/
- `announcement_files.json`: https://zakup.gov.kz/api/core/api/public/announcements/40745436/general-files/

Unneeded director, contact, commission, organizer, signatory and accounting
metadata was removed. Remaining source values have not been synthesized.
Tests use HTTP transport doubles and altered pagination/error envelopes to test
failures without making live requests or fabricating additional procurements.

Rendered HTML fixtures use these official source routes:

- `legacy_lots.html`: `/ru/search/lots` on old.goszakup.gov.kz
- `legacy_lot_detail.html`: `/ru/subpriceoffer/index/17746286/43519574`
- `legacy_general.html`, `legacy_documents.html`: `/ru/announce/index/17746286?tab=general` and `?tab=documents`
- `legacy_spec_modal.html`: `/ru/announce/actionAjaxModalShowFiles/17746286/125`
- `legacy_lot_modal.html`: read-only POST `/ru/announce/ajax_load_lot/17746277/`, with the plan-root ID supplied by that page's lot control
