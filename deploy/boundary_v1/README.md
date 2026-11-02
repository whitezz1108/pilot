# explicit-think-end-v1 serving patch

The `before/`, `after/`, `*.patch` and `patch_manifest.json` files are copied from the server evidence archive `/home/evan/ai/repairs/qwen-boundary-v1-20261102`. The source hashes were verified before committing.

The base is SGLang image `sha256:e3a4f06001a29542fc4a8638536d9ffc5eb9c9942b08603c09b19a56eae4a1ae`. The deployed GPU0/1 image is `sha256:b1e0264ebb66e7a192b613875f286c4757f79ace76d47d030789a3327f7d0dd0`.

The Dockerfile expects the locally preserved `pilot-sglang:before-boundary-20261102` image. Scripts preserve the original deployment and validation procedure, including environment-specific paths. They require archived evidence and the engine/model runtime; this directory is not a standalone fresh-install package. `test_boundary.py` and `test_wire_capture.py` are server-environment validation scripts, outside the application pytest testpaths.

Raw requests, responses, contract material, container environments, logs and generated validation outputs stay in the evidence archive and are not committed. See `docs/architecture_sync_20261102.md` for scope and validation limits.
