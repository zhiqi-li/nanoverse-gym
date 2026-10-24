"""NVIDIA vision-language models through Gym's official chat converter."""

from fastapi import FastAPI
from responses_api_models.vllm_model.app import VLLMModel, VLLMModelConfig

from responses_api_models.nanoverse_sdk_openai.app import NativeRequest, NativeSDKIngress


class NanoverseSDKNemotronModelServer(VLLMModel):
    config: VLLMModelConfig

    def setup_webserver(self, *, on_native_request: NativeRequest | None = None) -> FastAPI:
        app = super().setup_webserver()
        app.add_middleware(NativeSDKIngress, on_native_request=on_native_request,
                           omit_responses_reasoning_hints=True)
        return app


if __name__ == '__main__':
    NanoverseSDKNemotronModelServer.run_webserver()
