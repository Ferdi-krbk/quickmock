# Changelog

## Unreleased

- Preserve zero prices and zero stock in the demo form and free-product orders;
  reject negative/non-finite prices and invalid stock quantities.

- Keep in-memory records and the reset snapshot unchanged when a disk write fails.
  Use unique adjacent temporary files; report failed storage operations as HTTP 507.
- Apply Studio configuration atomically, validate integer settings and seed options,
  and inherit the store's read-only mode when creating an app.
- Require same-origin browser access to Studio admin endpoints. The mock API still
  accepts cross-origin frontend requests.
- Point Swagger at the live collection specification, avoid schema-name collisions,
  include PUT/PATCH request bodies, and retain auth/bulk/statistics endpoints.
  Infer nullable and mixed-array OpenAPI types from up to 50 records.
- Infer TypeScript optional properties against the actual 50-record sample and
  infer mixed array element types. Escape generated URL literals and shell snippets.
- Preserve integer precision in filtering/sorting and avoid generated-ID collisions
  with explicit IDs later in a dataset. Return a clear client error for numeric
  aggregate overflow instead of failing JSON serialization.
- Reject OpenAPI exports that would overwrite the source database and invalid CLI
  fault percentages. Show failed delete/seed/reset operations in the Studio.
- Add regression coverage and expand CI to Python 3.9–3.14 on Windows and Ubuntu.
