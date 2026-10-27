# Studio 3 / initial engineering build

Date: 2026-10-27. Source implementation, not a hosted deployment.

## Validation performed

- 30 CPU/API/contract/integration tests passed.
- React production build passed.
- `pip check` passed for the web environment.
- 16 command sequences checked against the pinned Musubi source: script paths and CLI flag names exist. This is not GPU runtime validation.
- Real HTTP worker/FFmpeg roundtrip tested, including uploading the generated clips back into a dataset.
- Exact web dependency closure locked in requirements-web.lock.

## Not performed

Docker builds (no Docker engine), external PostgreSQL/S3 integration, live YouGPU rental/termination, CUDA imports and model execution, May identity/quality acceptance, mobile/browser visual QA. Managed preview did not have access to the installed backend Python dependencies.

## Deliberate first-build limits

One GPU per job, no warm persistent inference worker, no distributed training, no distilled Klein transfer or Wan5B adapter training/import. Provider offers are displayed and sorted; complete fastest/balanced recommendation requires qualified benchmark coverage. Speed/price estimates remain unknown until suitable measurements exist. External LoRA support requires compatible Musubi tensor names; arbitrary PEFT/Comfy formats are not silently accepted.

## Architecture decision

Single Docker web deployment uses React/Vite + FastAPI, rather than separate Next.js and API services. This reduces Timeweb setup to one web image/domain while keeping PostgreSQL and S3 external. Training/generation run only on the GPU agent image. Provider credentials never go to the agent.

The product specification in PRODUCT_SPEC.md remains the target direction; README and these notes describe the implemented scope and acceptance limits.
