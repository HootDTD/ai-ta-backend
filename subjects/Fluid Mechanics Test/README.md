Fluid Mechanics Test
====================

This is a self-contained subject bundle created for testing. It does not change the live subject configuration under `backend/subjects`; it is an organized copy for experimentation.

- materials/ — embedded content (textbook + weekly notes)
  - textbook/my_book_index_aero_smoke — textbook FAISS/embeddings
  - weekly-notes/indexes — per-week indexes for notes/slides
- subject-manifest/manifest.json — subject registry copy (paths adjusted to this bundle)
- course-manifest/manifest.json — course weekly manifest copy (paths adjusted to this bundle)

Live runtime continues to use `backend/subjects/Fluid Mechanics` and `backend/subjects/fluid-mechanics/manifest.json` unless you point env vars to this bundle.
