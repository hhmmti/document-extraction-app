SELECT organization_id, well_id, pump_parameters, wellbore_parameters, run_id, document_sha256, design_date,
       entry_method, confirmation_id, confirmed_at, confirmed_by, environment
FROM (
    SELECT *,
           row_number() OVER (PARTITION BY environment, organization_id, well_id
                              ORDER BY confirmed_at DESC, confirmation_id DESC) AS latest
    FROM esp_design_extraction.well_entries
)
WHERE latest = 1
