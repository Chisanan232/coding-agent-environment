---
name: pdf-operations
description: "Extract, OCR, create, merge, split or fill PDF files with document and rendered-output verification. Use for PDF-specific work rather than an editable Word or slide deliverable."
---

# PDF operations

Inspect the input PDF and requested operation before choosing tools. Use text
extraction (`pdftotext`, pdfplumber/pypdf) for text PDFs and OCR for scans; preserve
page order, tables and source attribution. For merge/split/rotation/watermark,
forms or encryption use a suitable local library/tool (pypdf, qpdf, reportlab,
pdf-lib); verify installed dependencies rather than assume availability.

Preserve originals and write requested changes to a separate output unless an
in-place edit is explicit. Inspect form fields and whether the document is
fillable before filling; preserve field names and validate resulting values.
Use rendered pages to verify layout, clipping and page count. Check readable text
and important relationships against the source. Passwords remain opaque; never
print them or place them in command arguments/transcripts. Removing protection
requires authority. Return the requested artifact with actual verification and
remaining OCR/layout uncertainty. Prefer native document skills when the user
wants Word, sheets or slides rather than PDF.
