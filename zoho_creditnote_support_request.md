Subject: Update credit note custom field without changing invoice associations

Hello Zoho Books Support,

For organisation 941141787, we need to populate the Customer Branch lookup field on existing credit notes while preserving their invoice associations, invoice allocations, customer, line items and amounts.

Some credit notes have multiple invoice allocations but no direct associated invoice on the credit note. Others have no allocation. For CN027182, a credit-note PUT specifying the custom field ID without an invoice ID returned code 12069, asking for an Associated Invoice Number. A dedicated custom-fields request using the document-reported index returned code 120121, invalid custom-field index. Settings metadata and the credit-note response reported different indices; we did not substitute another field's index.

The documented field-only operation is POST /books/v3/creditnotes/{creditnote_id}/customfields, with a custom_fields array containing index and value. The documentation lists ZohoBooks.creditnotes.CREATE for this operation. Our rejected request used index 1 and the mapped branch record ID as the value. Please confirm whether this endpoint supports lookup fields, how to determine the correct index for this organisation, and whether a different identifier or value format is required. In particular, please explain code 120121 for the document-reported index.

Please confirm:

1. Is there a supported API or UI method to update only this lookup field without adding or changing a direct invoice association?
2. What exact endpoint, HTTP method, payload, custom-field identifier and OAuth scopes apply to a credit-note lookup field? Please provide an example with placeholder values.
3. For a credit note allocated to multiple invoices, what happens to existing allocations if an invoice_id is provided on the credit-note update? Can the direct association remain blank?
4. If an association is mandatory, what evidence and procedure should be used for notes with no allocation or multiple allocations?

We can provide request IDs and redacted error details securely if needed. Please do not request OAuth tokens or client secrets.
