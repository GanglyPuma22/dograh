# Aster first-wake backup

This archive preserves the local first-wake bundle from `~/src/aster-first-wake-20260929`.
The backup branch starts at station-events commit `eee2b47feef3878ad25b5d117f96c8917b62d385`, which retains the local earned-discovery gate and station-event implementation.

All six Python modules from `backend/api/services/game_companion` were copied into their matching repository paths. `persona.py`, `session.py`, and `protocol.py` byte-match image `dograh-api:aster-first-wake-candidate-20260929`; `image-byte-match.json` records the immutable image ID and SHA-256 digests.

The archive retains the backend companion tests and fixture, three changed game regression files, the two original patches, source manifests, historical qualification/deployment receipts, and the image-overlay recipes. Tests and receipts are preserved as historical evidence; they were not rerun during this backup.

`backend.patch` is the complete six-file first-wake delta against the dirty station-event source, including the route capability/error-handling changes. The runtime route remains at the station-event revision; its first-wake changes are retained in that patch. This branch is a source backup, and no service was deployed.

The source bundle was left unchanged. No environment files, credentials, or provider tokens are included.
