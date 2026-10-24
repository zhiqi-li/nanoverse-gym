from importlib.metadata import version

import pytest

from environment_servers.nanoverse_sdk.models import PROFILES, model_profile
from portable.gym_version import GYM_VERSION, require_gym_release


def test_gym_distribution_is_the_supported_stable_release():
    receipt = require_gym_release()
    assert receipt['version'] == version('nemo-gym') == GYM_VERSION == '0.7.0'


def test_user_nemotron_gateway_identity_is_preserved():
    exact = 'nvidia/nvidia/nemotron-3.5-super-vl'
    profile = model_profile(exact)
    assert profile == PROFILES['nemotron-3.5-super-vl']
    assert profile.model == exact and profile.transport == 'chat'
    assert profile.model != PROFILES['nemotron-3.5-super-vl-nim'].model
    with pytest.raises(ValueError):
        model_profile('nvidia/nemotron-3.5-super-vl')


def test_chat_adapter_uses_official_gym_converter():
    from responses_api_models.nanoverse_sdk_nemotron.app import NanoverseSDKNemotronModelServer
    from responses_api_models.vllm_model.app import VLLMModel
    assert issubclass(NanoverseSDKNemotronModelServer, VLLMModel)
